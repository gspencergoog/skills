# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Git remote resolution, base-branch commit history, and hunk blame collectors.

Extracts canonical GitHub repository coordinates, comparison diff targets,
single-author local branch identities, base-branch commit touches, and line-level
blame ownership while keeping subprocess execution bounded and non-interactive.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from urllib.parse import urlsplit

try:
    from reviewer_models import (
        BlameLineTouch,
        GitCommitTouch,
        is_bot_identity,
        normalize_scope_path,
    )
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from reviewer_models import (
        BlameLineTouch,
        GitCommitTouch,
        is_bot_identity,
        normalize_scope_path,
    )

CommandRunner = Callable[[Sequence[str], str | None], str]

DEFAULT_CMD_TIMEOUT_SECONDS: int = 15
MAX_LOG_COMMITS: int = 50
MAX_BLAME_FILES: int = 25
MAX_BLAME_HUNKS_PER_FILE: int = 5
MAX_BLAME_LINES_PER_FILE: int = 200

_HEX_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$", re.IGNORECASE)
_DIFF_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+")
_BLAME_HEADER_RE = re.compile(r"^([0-9a-f]{40,64})\s+\d+\s+\d+(?:\s+\d+)?$", re.IGNORECASE)
_SUPPORTED_URL_SCHEMES: frozenset[str] = frozenset({"http", "https", "ssh", "git"})


def run_bounded_cmd(
    args: Sequence[str],
    cwd: str | None = None,
    timeout: int = DEFAULT_CMD_TIMEOUT_SECONDS,
) -> str:
    """Runs a non-interactive subprocess with a wall-clock timeout.

    Merges ``GIT_TERMINAL_PROMPT=0`` and ``GH_PROMPT_DISABLED=1`` into the child
    environment so credential or interactive CLI prompts fail fast instead of
    hanging. Returns stripped standard output on exit status 0, or an empty
    string when the command fails, times out, or cannot be executed.
    """
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GH_PROMPT_DISABLED": "1",
    }
    try:
        result = subprocess.run(
            list(args),
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    return result.stdout.strip()


def _exec_cmd(
    args: Sequence[str],
    cwd: str | None,
    runner: CommandRunner | None,
) -> str:
    """Dispatches ``args`` through an injected ``runner`` or ``run_bounded_cmd``."""
    if runner is None:
        return run_bounded_cmd(args, cwd=cwd)
    try:
        return runner(list(args), cwd)
    except (subprocess.SubprocessError, OSError):
        return ""


def _is_github_host(host: str) -> bool:
    """Returns whether ``host`` is ``github.com`` or a ``github.com-*`` SSH alias."""
    lowered = host.lower()
    if lowered == "github.com":
        return True
    return lowered.startswith("github.com-") and len(lowered) > len("github.com-")


def _extract_github_repo_path(cleaned_url: str) -> str | None:
    """Extracts the ``owner/repo`` path portion from a GitHub HTTPS or SSH URL."""
    if "://" in cleaned_url:
        parsed = urlsplit(cleaned_url)
        if parsed.scheme.lower() not in _SUPPORTED_URL_SCHEMES:
            return None
        host = parsed.hostname or ""
        return parsed.path.strip("/") if _is_github_host(host) else None

    if cleaned_url.startswith(("/", ".")):
        return None
    host_part, sep, path_part = cleaned_url.partition(":")
    if not sep or "/" in host_part:
        return None
    _, _, host = host_part.rpartition("@")
    return path_part.strip("/") if _is_github_host(host) else None


def parse_github_remote_url(url: str) -> tuple[str, str] | None:
    """Parses a GitHub HTTPS or SSH remote URL into ``(owner, repo)``.

    Supports ``github.com`` and ``github.com-<alias>`` SSH hostnames and strips
    trailing ``.git`` suffixes via ``removesuffix(".git")`` so repository names
    such as ``octo.js`` and ``packrat`` retain all characters.
    """
    cleaned = (url or "").strip()
    if not cleaned:
        return None
    repo_path = _extract_github_repo_path(cleaned)
    if not repo_path:
        return None
    segments = repo_path.split("/")
    if len(segments) != 2:
        return None
    owner = segments[0].strip()
    repo = segments[1].strip()
    if repo.lower().endswith(".git"):
        repo = repo[:-4] if not repo.endswith(".git") else repo.removesuffix(".git")
    if not owner or not repo or owner in {".", ".."} or repo in {".", ".."}:
        return None
    return owner, repo


def _list_git_remotes(cwd: str, runner: CommandRunner | None) -> dict[str, str]:
    """Reads configured remote URLs from ``git remote -v``, preferring fetch URLs."""
    output = _exec_cmd(["git", "remote", "-v"], cwd, runner)
    remotes: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name, url = parts[0], parts[1]
        is_fetch = len(parts) < 3 or parts[2] == "(fetch)"
        if is_fetch or name not in remotes:
            remotes[name] = url
    return remotes


def _extract_gh_owner_name(node: object) -> tuple[str, str] | None:
    """Extracts ``(owner, name)`` from a ``gh repo view`` JSON object."""
    if not isinstance(node, dict):
        return None
    raw_owner = node.get("owner")
    if isinstance(raw_owner, dict):
        owner = raw_owner.get("login")
    elif isinstance(raw_owner, str):
        owner = raw_owner
    else:
        owner = None
    name = node.get("name")
    if isinstance(owner, str) and owner.strip() and isinstance(name, str) and name.strip():
        return owner.strip(), name.strip()
    return None


def _resolve_fork_or_repo_via_gh(
    cwd: str,
    fallback: tuple[str, str],
    runner: CommandRunner | None,
) -> tuple[str, str]:
    """Queries ``gh repo view`` for fork ``parent`` coordinates, falling back to git."""
    if not shutil.which("gh"):
        return fallback
    raw_json = _exec_cmd(
        ["gh", "repo", "view", "--json", "owner,name,parent"],
        cwd,
        runner,
    )
    if not raw_json:
        return fallback
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        return fallback
    if not isinstance(payload, dict):
        return fallback
    parent_coords = _extract_gh_owner_name(payload.get("parent"))
    if parent_coords is not None:
        return parent_coords
    direct_coords = _extract_gh_owner_name(payload)
    return direct_coords if direct_coords is not None else fallback


def get_canonical_repo_info(
    cwd: str,
    runner: CommandRunner | None = None,
) -> tuple[str | None, str | None]:
    """Resolves canonical ``(owner, name)`` coordinates for a GitHub repository.

    Inspects ``git remote -v`` in priority order (``upstream``, ``origin``, then
    remaining sorted remotes). Returns ``(None, None)`` without invoking ``gh``
    when no GitHub remote exists, and returns ``upstream`` directly without
    invoking ``gh`` when ``upstream`` points at GitHub. Invokes
    ``gh repo view --json owner,name,parent`` only when ``origin`` or a fallback
    remote points at GitHub without a GitHub ``upstream``.
    """
    remotes = _list_git_remotes(cwd, runner)
    if "upstream" in remotes:
        upstream_repo = parse_github_remote_url(remotes["upstream"])
        if upstream_repo is not None:
            return upstream_repo

    ordered_names: list[str] = []
    if "origin" in remotes:
        ordered_names.append("origin")
    ordered_names.extend(sorted(name for name in remotes if name not in {"upstream", "origin"}))

    for remote_name in ordered_names:
        parsed = parse_github_remote_url(remotes[remote_name])
        if parsed is not None:
            return _resolve_fork_or_repo_via_gh(cwd, parsed, runner)
    return None, None


def _parse_symbolic_remote_head(ref_output: str, remote_name: str) -> str | None:
    """Extracts the branch name from ``refs/remotes/<remote>/...`` output."""
    cleaned = ref_output.strip()
    if not cleaned:
        return None
    prefix = f"refs/remotes/{remote_name}/"
    branch = cleaned.removeprefix(prefix) if cleaned.startswith(prefix) else cleaned.split("/")[-1]
    return branch.strip() or None


def get_default_branch(cwd: str, runner: CommandRunner | None = None) -> str:
    """Determines the repository default branch name.

    Checks ``refs/remotes/upstream/HEAD`` and ``refs/remotes/origin/HEAD`` first,
    then verifies ``main`` or ``master`` across ``upstream``, ``origin``, and
    local heads, defaulting to ``"main"``.
    """
    for remote_name in ("upstream", "origin"):
        sym_out = _exec_cmd(
            ["git", "symbolic-ref", f"refs/remotes/{remote_name}/HEAD"],
            cwd,
            runner,
        )
        branch = _parse_symbolic_remote_head(sym_out, remote_name)
        if branch and branch != "HEAD":
            return branch

    refs_out = _exec_cmd(
        [
            "git",
            "for-each-ref",
            "--format=%(refname)",
            "refs/remotes/upstream",
            "refs/remotes/origin",
            "refs/heads",
        ],
        cwd,
        runner,
    )
    existing = {line.strip() for line in refs_out.splitlines() if line.strip()}
    candidates = (
        ("refs/remotes/upstream/main", "main"),
        ("refs/remotes/upstream/master", "master"),
        ("refs/remotes/origin/main", "main"),
        ("refs/remotes/origin/master", "master"),
        ("refs/heads/main", "main"),
        ("refs/heads/master", "master"),
    )
    for full_ref, short_name in candidates:
        if full_ref in existing:
            return short_name
    return "main"


def _clean_sha(raw: str) -> str | None:
    """Returns a validated hexadecimal git object SHA, or None."""
    first_line = raw.strip().splitlines()[0].strip() if raw.strip() else ""
    return first_line if _HEX_SHA_RE.match(first_line) else None


def _resolve_merge_base(
    cwd: str,
    left_ref: str,
    right_ref: str,
    runner: CommandRunner | None,
) -> str | None:
    """Resolves ``git merge-base <left_ref> <right_ref>`` to a commit SHA."""
    out = _exec_cmd(["git", "merge-base", left_ref, right_ref], cwd, runner)
    return _clean_sha(out)


def _resolve_commit_sha(
    cwd: str,
    ref: str,
    runner: CommandRunner | None,
) -> str | None:
    """Resolves ``ref`` to a verified commit SHA."""
    out = _exec_cmd(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd, runner)
    return _clean_sha(out)


def _resolve_preferred_base_ref(
    cwd: str,
    base_branch: str,
    runner: CommandRunner | None,
) -> str:
    """Selects ``upstream/<base>``, ``origin/<base>``, or ``<base>`` by ref presence."""
    clean_branch = (base_branch or "").strip().removeprefix("refs/heads/") or "main"
    if clean_branch.startswith(("upstream/", "origin/")):
        return clean_branch
    refs_out = _exec_cmd(
        [
            "git",
            "for-each-ref",
            "--format=%(refname)",
            f"refs/remotes/upstream/{clean_branch}",
            f"refs/remotes/origin/{clean_branch}",
            f"refs/heads/{clean_branch}",
        ],
        cwd,
        runner,
    )
    found = {line.strip() for line in refs_out.splitlines() if line.strip()}
    if f"refs/remotes/upstream/{clean_branch}" in found:
        return f"upstream/{clean_branch}"
    if f"refs/remotes/origin/{clean_branch}" in found:
        return f"origin/{clean_branch}"
    if f"refs/heads/{clean_branch}" in found:
        return clean_branch
    return f"origin/{clean_branch}"


def _resolve_explicit_compare(
    cwd: str,
    compare_spec: str,
    runner: CommandRunner | None,
) -> tuple[str, str | None, str | None]:
    """Parses an explicit ``--compare`` range into ``(diff_range, merge_base, head_ref)``."""
    if "..." in compare_spec:
        left, _, right = compare_spec.partition("...")
        left_ref = left.strip() or "HEAD"
        head_ref = right.strip() or "HEAD"
        return compare_spec, _resolve_merge_base(cwd, left_ref, head_ref, runner), head_ref
    if ".." in compare_spec:
        left, _, right = compare_spec.partition("..")
        left_ref = left.strip() or "HEAD"
        head_ref = right.strip() or "HEAD"
        return compare_spec, _resolve_commit_sha(cwd, left_ref, runner), head_ref
    return compare_spec, _resolve_commit_sha(cwd, compare_spec, runner), None


def resolve_comparison_targets(
    cwd: str,
    base_branch: str,
    branch: str | None = None,
    compare: str | None = None,
    runner: CommandRunner | None = None,
) -> tuple[str, str | None, str | None]:
    """Resolves git diff arguments into ``(diff_range, merge_base, head_ref)``.

    Prefers ``upstream/<base_branch>`` over ``origin/<base_branch>`` over local
    ``<base_branch>``, resolves ``git merge-base`` on three-dot ranges, and sets
    ``head_ref`` to None when comparing working-tree edits against ``HEAD`` or
    ``HEAD~1``.
    """
    if compare and compare.strip():
        return _resolve_explicit_compare(cwd, compare.strip(), runner)

    target_branch = (branch or "").strip()
    if not target_branch:
        current = _exec_cmd(["git", "branch", "--show-current"], cwd, runner).strip()
        if current and current != base_branch:
            target_branch = current

    if target_branch:
        base_ref = _resolve_preferred_base_ref(cwd, base_branch, runner)
        diff_range = f"{base_ref}...{target_branch}"
        merge_base = _resolve_merge_base(cwd, base_ref, target_branch, runner)
        return diff_range, merge_base, target_branch

    dirty_files = _exec_cmd(["git", "diff", "--name-only", "HEAD"], cwd, runner).strip()
    fallback_ref = "HEAD" if dirty_files else "HEAD~1"
    return fallback_ref, _resolve_commit_sha(cwd, fallback_ref, runner), None


def _parse_author_line(raw_line: str) -> tuple[str, tuple[str, str]] | None:
    """Parses a ``%ae|%an`` line into ``(dedup_key, (email, name))``."""
    email_part, sep, name_part = raw_line.strip().partition("|")
    if not sep:
        return None
    email = email_part.strip()
    name = name_part.strip()
    if not email and not name:
        return None
    key = email.lower() if email else f"name:{name.lower()}"
    return key, (email, name)


def _parse_unique_commit_author(log_output: str) -> tuple[str, ...] | None:
    """Returns ``(email, name)`` for a single-author log, ``()`` for multi-author, or None if empty."""
    authors: dict[str, tuple[str, str]] = {}
    for raw_line in log_output.splitlines():
        parsed = _parse_author_line(raw_line)
        if parsed is None:
            continue
        authors.setdefault(parsed[0], parsed[1])
        if len(authors) > 1:
            return ()
    if not authors:
        return None
    sole_email, sole_name = next(iter(authors.values()))
    return tuple(val for val in (sole_email, sole_name) if val)


def _read_git_config_author(cwd: str, runner: CommandRunner | None) -> tuple[str, ...]:
    """Reads ``(user.email, user.name)`` from ``git config`` for uncommitted changes."""
    email = _exec_cmd(["git", "config", "user.email"], cwd, runner).strip()
    name = _exec_cmd(["git", "config", "user.name"], cwd, runner).strip()
    return tuple(val for val in (email, name) if val)


def infer_local_change_author(
    cwd: str,
    merge_base: str | None,
    head_ref: str | None,
    runner: CommandRunner | None = None,
) -> tuple[str, ...]:
    """Infers the local author ``(email, name)`` for self-review exclusion.

    Returns the sole commit author across ``<merge_base>..<head_ref>`` when a
    branch range has exactly one unique author, or ``git config`` ``user.email``
    and ``user.name`` when inspecting an uncommitted diff (``head_ref`` is None)
    with zero commits between ``merge_base`` and ``HEAD``. Returns ``()`` when
    multiple authors contributed or when git/mock output is empty.
    """
    clean_base = (merge_base or "").strip()
    clean_head = (head_ref or "").strip()

    if clean_head:
        if not clean_base:
            return ()
        log_out = _exec_cmd(
            ["git", "log", "--format=%ae|%an", f"{clean_base}..{clean_head}"],
            cwd,
            runner,
        )
        parsed = _parse_unique_commit_author(log_out)
        return parsed if parsed is not None else ()

    if clean_base:
        log_out = _exec_cmd(
            ["git", "log", "--format=%ae|%an", f"{clean_base}..HEAD"],
            cwd,
            runner,
        )
        parsed = _parse_unique_commit_author(log_out)
        if parsed is not None:
            return parsed
    return _read_git_config_author(cwd, runner)


def _normalize_unique_files(changed_files: Sequence[str]) -> list[str]:
    """Normalizes and deduplicates repository-relative file paths."""
    result: list[str] = []
    seen: set[str] = set()
    for raw_path in changed_files:
        try:
            norm = normalize_scope_path(raw_path, False)
        except ValueError:
            continue
        if norm not in seen:
            seen.add(norm)
            result.append(norm)
    return result


def _parse_log_line(raw_line: str) -> tuple[str, str, str, int] | None:
    """Parses a ``%H|%ae|%an|%at`` line from ``git log``."""
    line = raw_line.strip()
    if not line:
        return None
    sha, sep1, rest = line.partition("|")
    email, sep2, rest2 = rest.partition("|")
    name, sep3, ts_text = rest2.rpartition("|")
    if not (sep1 and sep2 and sep3):
        return None
    clean_sha = sha.strip()
    if not clean_sha:
        return None
    try:
        timestamp = int(ts_text.strip())
    except ValueError:
        return None
    return clean_sha, email.strip(), name.strip(), timestamp


def _query_git_log_entries(
    target_path: str,
    cwd: str,
    base_ref: str | None,
    runner: CommandRunner | None,
) -> list[tuple[str, str, str, int]]:
    """Runs ``git log -n 50`` on ``target_path`` anchored at ``base_ref`` when given."""
    cmd = ["git", "log", "-n", str(MAX_LOG_COMMITS), "--format=%H|%ae|%an|%at"]
    clean_base = (base_ref or "").strip()
    if clean_base:
        cmd.append(clean_base)
    cmd.extend(["--", target_path])
    output = _exec_cmd(cmd, cwd, runner)
    entries: list[tuple[str, str, str, int]] = []
    for raw_line in output.splitlines():
        parsed = _parse_log_line(raw_line)
        if parsed is not None:
            entries.append(parsed)
    return entries


def _collect_directory_commits(
    seen_dirs: Sequence[str],
    dir_file_shas: dict[str, set[str]],
    cwd: str,
    base_ref: str | None,
    runner: CommandRunner | None,
) -> list[GitCommitTouch]:
    """Collects parent-directory commits excluding SHAs already matched on changed files."""
    dir_touches: list[GitCommitTouch] = []
    for dir_path in seen_dirs:
        known_shas = dir_file_shas[dir_path]
        for sha, email, name, ts in _query_git_log_entries(dir_path, cwd, base_ref, runner):
            sha_lower = sha.lower()
            if sha_lower in known_shas:
                continue
            known_shas.add(sha_lower)
            dir_touches.append(
                GitCommitTouch(
                    sha=sha,
                    email=email,
                    name=name,
                    timestamp=ts,
                    scope_path=dir_path,
                    is_directory=True,
                )
            )
    return dir_touches


def collect_file_commits(
    changed_files: Sequence[str],
    cwd: str,
    base_ref: str | None = None,
    runner: CommandRunner | None = None,
) -> tuple[GitCommitTouch, ...]:
    """Collects historical commits touching changed files and parent directories.

    Walks ``base_ref`` when provided so unmerged feature-branch commits do not
    inflate historical ownership, and deduplicates parent-directory commits whose
    SHAs already matched a changed file in that directory.
    """
    files = _normalize_unique_files(changed_files)
    touches: list[GitCommitTouch] = []
    seen_dirs: list[str] = []
    dir_file_shas: dict[str, set[str]] = {}

    for file_path in files:
        parent_dir = posixpath.dirname(file_path)
        if parent_dir and parent_dir not in dir_file_shas:
            seen_dirs.append(parent_dir)
            dir_file_shas[parent_dir] = set()
        for sha, email, name, ts in _query_git_log_entries(file_path, cwd, base_ref, runner):
            touches.append(
                GitCommitTouch(
                    sha=sha,
                    email=email,
                    name=name,
                    timestamp=ts,
                    scope_path=file_path,
                    is_directory=False,
                )
            )
            if parent_dir:
                dir_file_shas[parent_dir].add(sha.lower())

    touches.extend(_collect_directory_commits(seen_dirs, dir_file_shas, cwd, base_ref, runner))
    return tuple(touches)


def _extract_deleted_or_modified_hunks(diff_output: str) -> list[tuple[int, int]]:
    """Extracts capped ``(start_line, line_count)`` base-side ranges from ``git diff -U0``."""
    ranges: list[tuple[int, int]] = []
    remaining_lines = MAX_BLAME_LINES_PER_FILE
    for line in diff_output.splitlines():
        if len(ranges) >= MAX_BLAME_HUNKS_PER_FILE or remaining_lines <= 0:
            break
        match = _DIFF_HUNK_RE.match(line)
        if not match:
            continue
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        if start <= 0 or count <= 0:
            continue
        clamped = min(count, remaining_lines)
        ranges.append((start, clamped))
        remaining_lines -= clamped
    return ranges


def _record_blame_entry(
    entry: dict[str, str],
    aggregated: dict[str, tuple[str, str, str, int, int]],
) -> None:
    """Validates and folds one ``--line-porcelain`` line block into ``aggregated``."""
    sha = entry.get("sha", "")
    if not sha or not sha.strip("0"):
        return
    email = entry.get("email", "").strip()
    name = entry.get("name", "").strip()
    if (not email and not name) or is_bot_identity(email) or is_bot_identity(name):
        return
    try:
        timestamp = int(entry.get("time", "0"))
    except ValueError:
        timestamp = 0
    sha_key = sha.lower()
    if sha_key in aggregated:
        orig_sha, orig_email, orig_name, orig_ts, count = aggregated[sha_key]
        aggregated[sha_key] = (orig_sha, orig_email, orig_name, orig_ts, count + 1)
    else:
        aggregated[sha_key] = (sha, email, name, timestamp, 1)


def _parse_porcelain_blame(
    porcelain_output: str,
    aggregated: dict[str, tuple[str, str, str, int, int]],
) -> None:
    """Parses ``git blame --line-porcelain`` output into ``aggregated`` per commit SHA."""
    current: dict[str, str] = {}
    for line in porcelain_output.splitlines():
        if line.startswith("\t"):
            continue
        header_match = _BLAME_HEADER_RE.match(line)
        if header_match:
            if current:
                _record_blame_entry(current, aggregated)
            current = {"sha": header_match.group(1)}
        elif line.startswith("author "):
            current["name"] = line[7:].strip()
        elif line.startswith("author-mail "):
            mail = line[12:].strip().removeprefix("<").removesuffix(">").strip()
            current["email"] = mail
        elif line.startswith("author-time "):
            current["time"] = line[12:].strip()
    if current:
        _record_blame_entry(current, aggregated)


def _blame_single_file(
    file_path: str,
    cwd: str,
    merge_base: str,
    head_ref: str | None,
    runner: CommandRunner | None,
) -> list[BlameLineTouch]:
    """Collects aggregated ``BlameLineTouch`` entries across modified hunks of one file."""
    diff_cmd = ["git", "diff", "-U0", merge_base]
    if head_ref:
        diff_cmd.append(head_ref)
    diff_cmd.extend(["--", file_path])
    diff_out = _exec_cmd(diff_cmd, cwd, runner)
    if not diff_out:
        return []

    hunks = _extract_deleted_or_modified_hunks(diff_out)
    if not hunks:
        return []

    aggregated: dict[str, tuple[str, str, str, int, int]] = {}
    for start, count in hunks:
        blame_out = _exec_cmd(
            [
                "git",
                "blame",
                "--line-porcelain",
                "-L",
                f"{start},+{count}",
                merge_base,
                "--",
                file_path,
            ],
            cwd,
            runner,
        )
        if blame_out:
            _parse_porcelain_blame(blame_out, aggregated)

    return [
        BlameLineTouch(
            sha=sha,
            email=email,
            name=name,
            timestamp=ts,
            file_path=file_path,
            line_count=line_count,
        )
        for sha, email, name, ts, line_count in aggregated.values()
    ]


def collect_blame_touches(
    changed_files: Sequence[str],
    cwd: str,
    merge_base: str | None,
    head_ref: str | None = None,
    runner: CommandRunner | None = None,
) -> tuple[BlameLineTouch, ...]:
    """Collects line-level git blame ownership on base-branch hunks modified by a diff.

    Returns ``()`` immediately when ``merge_base`` is None. Inspects up to 25
    changed files, up to 5 modified or deleted hunks per file, and up to 200
    blamed lines per file against ``merge_base``, skipping uncommitted all-zero
    SHAs, bot accounts, pure insertions, added files, and binary diffs.
    """
    clean_base = (merge_base or "").strip()
    if not clean_base:
        return ()
    clean_head = (head_ref or "").strip() or None
    files = _normalize_unique_files(changed_files)[:MAX_BLAME_FILES]

    touches: list[BlameLineTouch] = []
    for file_path in files:
        touches.extend(_blame_single_file(file_path, cwd, clean_base, clean_head, runner))
    return tuple(touches)
