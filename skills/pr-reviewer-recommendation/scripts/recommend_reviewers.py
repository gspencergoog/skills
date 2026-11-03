#!/usr/bin/env python3
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

"""CLI entrypoint for recommending pull request reviewers from packrat and git signals.

Combines historical formal reviews and inline review threads from ``packrat``
with base-branch commit history and diff-hunk ``git blame`` ownership, falling
back cleanly to git-only analysis when ``packrat`` lacks the target repository.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from git_signals import (
    collect_blame_touches,
    get_canonical_repo_info,
    infer_local_change_author,
    resolve_comparison_targets,
)
from git_signals import (
    get_default_branch as _git_get_default_branch,
)
from packrat_signals import (
    DEFAULT_PACKRAT_DB as _PACKRAT_DEFAULT_DB,
)
from packrat_signals import (
    load_packrat_signals,
)
from reviewer_models import (
    GitCommitTouch,
    IdentityResolver,
    PackratLoadResult,
    is_bot_identity,
    normalize_scope_path,
    parse_github_noreply_login,
)
from reviewer_scoring import (
    format_json_report,
    format_markdown_report,
    rank_reviewers,
)

DEFAULT_PACKRAT_DB: Path = _PACKRAT_DEFAULT_DB
GH_LOOKUP_TIMEOUT_SECONDS: int = 5
_HEX_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$", re.IGNORECASE)


@dataclass
class _TargetContext:
    """Resolved target change metadata for ``main()``."""

    repo: str | None
    base_branch: str
    changed_files: list[str]
    pr_author: str | None = None
    pr_title: str = ""
    head_oid: str | None = None
    merge_base: str | None = None
    head_ref: str | None = None
    pr_details_loaded: bool = False


def run_cmd(args: Sequence[str], cwd: str | None = None) -> str:
    """Runs ``args`` and returns stripped stdout, or ``""`` on subprocess failure."""
    try:
        res = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=True,
            cwd=cwd,
        )
        return res.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


_ORIGINAL_RUN_CMD = run_cmd


def _cmd_runner(args: Sequence[str], cwd: str | None = None) -> str:
    """Bridges module-level ``run_cmd`` into ``git_signals`` helpers for testability."""
    return run_cmd(list(args), cwd=cwd)


def get_repo_info(cwd: str) -> tuple[str | None, str | None]:
    """Resolves canonical ``(owner, name)`` coordinates for ``cwd``."""
    return get_canonical_repo_info(cwd, runner=_cmd_runner)


def get_default_branch(cwd: str) -> str:
    """Resolves the default branch name (such as ``main`` or ``master``) for ``cwd``."""
    return _git_get_default_branch(cwd, runner=_cmd_runner)


def get_changed_files_from_range(commit_range: str, cwd: str) -> list[str]:
    """Returns changed repository-relative file paths for ``commit_range``."""
    files_str = run_cmd(["git", "diff", "--name-only", commit_range], cwd=cwd)
    if not files_str:
        return []
    return [line.strip() for line in files_str.splitlines() if line.strip()]


def get_pr_details(
    pr_number: int,
    repo: str | None,
    cwd: str,
) -> dict[str, Any] | None:
    """Fetches pull request metadata and file list via ``gh pr view``."""
    if not shutil.which("gh"):
        return None
    cmd = [
        "gh",
        "pr",
        "view",
        str(pr_number),
        "--json",
        "author,files,title,baseRefName,headRefOid",
    ]
    if repo:
        cmd.extend(["--repo", repo])
    pr_json = run_cmd(cmd, cwd=cwd)
    if not pr_json:
        return None
    try:
        parsed = json.loads(pr_json)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def get_collaborators(repo: str | None, cwd: str) -> list[str]:
    """Fetches repository assignee logins via ``gh api``."""
    if not shutil.which("gh") or not repo:
        return []
    collabs_json = run_cmd(
        ["gh", "api", f"repos/{repo}/assignees", "--paginate"],
        cwd=cwd,
    )
    if not collabs_json:
        return []
    try:
        data = json.loads(collabs_json)
        if isinstance(data, list):
            return [
                str(item["login"])
                for item in data
                if isinstance(item, dict) and isinstance(item.get("login"), str)
            ]
    except json.JSONDecodeError:
        pass
    return list(set(re.findall(r'"login":\s*"([^"]+)"', collabs_json)))


def get_file_contributors(
    filepath: str,
    cwd: str,
    base_ref: str | None = None,
) -> list[dict[str, Any]]:
    """Returns up to 50 historical commit dicts touching ``filepath``."""
    cmd = ["git", "log", "-n", "50", "--format=%H|%ae|%an|%at"]
    if base_ref and base_ref.strip():
        cmd.append(base_ref.strip())
    cmd.extend(["--", filepath])
    log_str = run_cmd(cmd, cwd=cwd)
    if not log_str:
        return []

    contributors: list[dict[str, Any]] = []
    for line in log_str.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 4:
            continue
        sha, email, name, ts_raw = parts
        try:
            timestamp = int(ts_raw.strip())
        except ValueError:
            continue
        contributors.append(
            {
                "sha": sha.strip(),
                "email": email.strip(),
                "name": name.strip(),
                "timestamp": timestamp,
            }
        )
    return contributors


def map_git_to_github(
    git_email: str,
    git_name: str,
    github_logins: Sequence[str],
) -> str | None:
    """Maps a git author email/name to a GitHub login without substring guesses."""
    if is_bot_identity(git_email) or is_bot_identity(git_name):
        return None
    resolver = IdentityResolver(known_logins=github_logins)
    noreply = parse_github_noreply_login(git_email)
    if noreply is not None:
        return resolver.resolve_github_login(noreply)
    resolved = resolver.resolve_git_identity("", git_email, git_name)
    if resolved is not None and resolved[1]:
        return resolved[0]
    return None


def _valid_gh_login_output(username: str) -> str | None:
    """Validates raw stdout from ``gh api .../commits/<sha>``."""
    cleaned = username.strip()
    lowered = cleaned.lower()
    if not cleaned or lowered.startswith("error") or "not found" in lowered:
        return None
    return None if is_bot_identity(cleaned) else cleaned


def resolve_username(
    sha: str,
    email: str,
    name: str,
    repo: str | None,
    cwd: str,
    github_logins: Sequence[str],
    cache: dict[str, str | None],
) -> str | None:
    """Resolves a git commit identity to a GitHub login using Commit API or heuristics."""
    if email in cache:
        return cache[email]
    if is_bot_identity(email) or is_bot_identity(name):
        return None
    if sha and repo and shutil.which("gh"):
        raw = run_cmd(
            ["gh", "api", f"repos/{repo}/commits/{sha}", "-q", ".author.login"],
            cwd=cwd,
        )
        gh_login = _valid_gh_login_output(raw)
        if gh_login is not None:
            cache[email] = gh_login
            return gh_login
    mapped = map_git_to_github(email, name, github_logins)
    if mapped is not None:
        cache[email] = mapped
        return mapped
    return None


def _build_parser() -> argparse.ArgumentParser:
    """Builds the CLI argument parser for ``recommend_reviewers.py``."""
    parser = argparse.ArgumentParser(
        description=("Recommend PR reviewers from packrat review history, git commits, and blame.")
    )
    parser.add_argument("--pr", type=int, help="GitHub Pull Request number to analyze.")
    parser.add_argument("--branch", help="Local or remote branch name to compare against base.")
    parser.add_argument("--compare", help="Custom git commit range (e.g. main...feature).")
    parser.add_argument("--repo", help="Target GitHub repository (owner/repo).")
    parser.add_argument("--dir", default=".", help="Path to local git repository.")
    parser.add_argument(
        "--packrat-db",
        help="Optional override path to packrat repos.sqlite database.",
    )
    parser.add_argument(
        "--exclude-author",
        action="append",
        default=[],
        help="Author login, email, or full name to exclude (repeatable or comma-separated).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON output instead of Markdown.",
    )
    return parser


def _parse_excluded_authors(raw_values: Sequence[str]) -> list[str]:
    """Splits repeatable/comma-separated ``--exclude-author`` flags."""
    parsed: list[str] = []
    for raw in raw_values:
        for item in raw.split(","):
            cleaned = item.strip()
            if cleaned:
                parsed.append(cleaned)
    return parsed


def _populate_from_pr_details(
    ctx: _TargetContext,
    pr_number: int,
    cwd: str,
) -> None:
    """Loads ``gh pr view`` details into ``ctx`` when ``--pr`` is provided."""
    print(f"Fetching details for PR #{pr_number} from {ctx.repo}...", file=sys.stderr)
    details = get_pr_details(pr_number, ctx.repo, cwd)
    if not details:
        print(
            f"Warning: Could not fetch PR #{pr_number} details via gh; checking packrat and git.",
            file=sys.stderr,
        )
        return
    ctx.pr_details_loaded = True
    files = details.get("files") or []
    ctx.changed_files = [
        str(item["path"]) for item in files if isinstance(item, dict) and item.get("path")
    ]
    author = details.get("author")
    if isinstance(author, dict) and isinstance(author.get("login"), str):
        ctx.pr_author = author["login"]
    ctx.pr_title = str(details.get("title") or "")
    ctx.base_branch = str(details.get("baseRefName") or ctx.base_branch)
    raw_oid = str(details.get("headRefOid") or "").strip()
    ctx.head_oid = raw_oid or None


def _populate_from_git_range(
    ctx: _TargetContext,
    branch: str | None,
    compare: str | None,
    cwd: str,
) -> None:
    """Resolves git comparison range and changed files into ``ctx``."""
    diff_range, merge_base, head_ref = resolve_comparison_targets(
        cwd,
        ctx.base_branch,
        branch=branch,
        compare=compare,
        runner=_cmd_runner,
    )
    ctx.merge_base = merge_base
    ctx.head_ref = head_ref
    print(f"Analyzing changes in range: {diff_range}...", file=sys.stderr)
    ctx.changed_files = get_changed_files_from_range(diff_range, cwd)


def _adopt_offline_packrat_pr(
    ctx: _TargetContext,
    packrat_result: PackratLoadResult,
) -> None:
    """Fills missing ``--pr`` metadata from ``packrat`` on offline runs."""
    if not ctx.changed_files and packrat_result.target_pr_files:
        ctx.changed_files = list(packrat_result.target_pr_files)
    if not ctx.pr_author and packrat_result.target_pr_author:
        ctx.pr_author = packrat_result.target_pr_author
    if not ctx.pr_title and packrat_result.target_pr_title:
        ctx.pr_title = packrat_result.target_pr_title
    if not ctx.pr_details_loaded and packrat_result.target_pr_base_ref:
        ctx.base_branch = packrat_result.target_pr_base_ref


def _resolve_existing_local_base_ref(cwd: str, base_branch: str) -> str | None:
    """Returns the preferred existing local ref for ``base_branch``, or None."""
    clean = base_branch.strip().removeprefix("refs/heads/") or "main"
    refs_out = run_cmd(
        [
            "git",
            "for-each-ref",
            "--format=%(refname)",
            f"refs/remotes/upstream/{clean}",
            f"refs/remotes/origin/{clean}",
            f"refs/heads/{clean}",
        ],
        cwd=cwd,
    )
    found = {line.strip() for line in refs_out.splitlines() if line.strip()}
    for full_ref, short_ref in (
        (f"refs/remotes/upstream/{clean}", f"upstream/{clean}"),
        (f"refs/remotes/origin/{clean}", f"origin/{clean}"),
        (f"refs/heads/{clean}", clean),
    ):
        if full_ref in found:
            return short_ref
    return None


def _clean_hex_sha(raw: str) -> str | None:
    """Returns a validated hexadecimal git SHA from ``raw``, or None."""
    first = raw.strip().splitlines()[0].strip() if raw.strip() else ""
    return first if _HEX_SHA_RE.match(first) else None


def _resolve_pr_local_merge_base(ctx: _TargetContext, cwd: str) -> str | None:
    """Resolves ``merge_base`` locally for ``--pr`` when ``headRefOid`` is fetched."""
    local_base = _resolve_existing_local_base_ref(cwd, ctx.base_branch)
    if ctx.merge_base is None and ctx.head_oid and local_base:
        verified = _clean_hex_sha(
            run_cmd(["git", "rev-parse", "--verify", f"{ctx.head_oid}^{{commit}}"], cwd=cwd)
        )
        if verified:
            mb = _clean_hex_sha(run_cmd(["git", "merge-base", local_base, ctx.head_oid], cwd=cwd))
            if mb:
                ctx.merge_base = mb
                ctx.head_ref = ctx.head_oid
    return ctx.merge_base or local_base


def _call_get_file_contributors(
    path: str,
    cwd: str,
    base_ref: str | None,
) -> list[dict[str, Any]]:
    """Invokes ``get_file_contributors`` while supporting 2-arg test mocks."""
    if not base_ref:
        return get_file_contributors(path, cwd)
    try:
        return get_file_contributors(path, cwd, base_ref)
    except TypeError:
        return get_file_contributors(path, cwd)


def _dict_to_commit_touch(
    item: dict[str, Any],
    scope_path: str,
    is_directory: bool,
) -> GitCommitTouch | None:
    """Converts a ``get_file_contributors`` dictionary into a ``GitCommitTouch``."""
    sha = str(item.get("sha") or "").strip()
    if not sha:
        return None
    return GitCommitTouch(
        sha=sha,
        email=str(item.get("email") or "").strip(),
        name=str(item.get("name") or "").strip(),
        timestamp=int(item.get("timestamp") or 0),
        scope_path=scope_path,
        is_directory=is_directory,
    )


def _normalize_changed_file_list(changed_files: Sequence[str]) -> list[str]:
    """Normalizes and deduplicates repository-relative changed file paths."""
    result: list[str] = []
    seen: set[str] = set()
    for raw_file in changed_files:
        try:
            norm_file = normalize_scope_path(raw_file, False)
        except ValueError:
            continue
        if norm_file not in seen:
            seen.add(norm_file)
            result.append(norm_file)
    return result


def _collect_dir_contributor_touches(
    seen_dirs: Sequence[str],
    dir_shas: dict[str, set[str]],
    cwd: str,
    base_ref: str | None,
) -> list[GitCommitTouch]:
    """Collects parent-directory commit touches excluding direct file SHAs."""
    dir_touches: list[GitCommitTouch] = []
    for dir_path in seen_dirs:
        known = dir_shas[dir_path]
        for entry in _call_get_file_contributors(dir_path, cwd, base_ref):
            touch = _dict_to_commit_touch(entry, dir_path, True)
            if touch is None or touch.sha.lower() in known:
                continue
            known.add(touch.sha.lower())
            dir_touches.append(touch)
    return dir_touches


def _collect_commit_touches_via_contributors(
    changed_files: Sequence[str],
    cwd: str,
    base_ref: str | None,
) -> tuple[GitCommitTouch, ...]:
    """Collects file and parent-directory ``GitCommitTouch`` items via ``get_file_contributors``."""
    touches: list[GitCommitTouch] = []
    seen_dirs: list[str] = []
    dir_shas: dict[str, set[str]] = {}

    for norm_file in _normalize_changed_file_list(changed_files):
        parent = posixpath.dirname(norm_file)
        if parent and parent not in dir_shas:
            seen_dirs.append(parent)
            dir_shas[parent] = set()
        for entry in _call_get_file_contributors(norm_file, cwd, base_ref):
            touch = _dict_to_commit_touch(entry, norm_file, False)
            if touch is None:
                continue
            touches.append(touch)
            if parent:
                dir_shas[parent].add(touch.sha.lower())

    touches.extend(_collect_dir_contributor_touches(seen_dirs, dir_shas, cwd, base_ref))
    return tuple(touches)


def _make_gh_commit_lookup(
    repo: str | None,
    cwd: str,
) -> Callable[[str], str | None] | None:
    """Creates a bounded ``gh api`` commit login callback for ``IdentityResolver``."""
    if not repo or not shutil.which("gh"):
        return None

    def _lookup(sha: str) -> str | None:
        api_args = ["gh", "api", f"repos/{repo}/commits/{sha}", "-q", ".author.login"]
        if run_cmd is not _ORIGINAL_RUN_CMD:
            return _valid_gh_login_output(run_cmd(api_args, cwd=cwd))
        env = {**os.environ, "GH_PROMPT_DISABLED": "1", "GIT_TERMINAL_PROMPT": "0"}
        res = subprocess.run(
            api_args,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=GH_LOOKUP_TIMEOUT_SECONDS,
            check=False,
        )
        if res.returncode != 0:
            stderr_lower = (res.stderr or "").lower()
            if "404" in stderr_lower or "not found" in stderr_lower:
                return None
            raise subprocess.CalledProcessError(res.returncode, api_args, res.stdout, res.stderr)
        return _valid_gh_login_output(res.stdout)

    return _lookup


def _build_resolver(
    ctx: _TargetContext,
    packrat_result: PackratLoadResult,
    cli_excluded: Sequence[str],
    pr_arg: int | None,
    cwd: str,
) -> IdentityResolver:
    """Constructs ``IdentityResolver`` with collaborators and author exclusions."""
    excluded = list(cli_excluded)
    if ctx.pr_author:
        excluded.append(ctx.pr_author)
    if not pr_arg and not cli_excluded:
        excluded.extend(
            infer_local_change_author(cwd, ctx.merge_base, ctx.head_ref, runner=_cmd_runner)
        )
    collaborators = get_collaborators(ctx.repo, cwd)
    return IdentityResolver(
        packrat_result,
        collaborator_logins=collaborators,
        excluded_authors=excluded,
    )


def _load_target_and_packrat(
    args: argparse.Namespace,
    cwd: str,
) -> tuple[_TargetContext, PackratLoadResult]:
    """Executes the 4-step target change and packrat loading sequence."""
    # Step 1: Resolve canonical GitHub repository and default base branch.
    owner, repo_name = get_repo_info(cwd)
    inferred_repo = f"{owner}/{repo_name}" if owner and repo_name else None
    ctx = _TargetContext(
        repo=args.repo or inferred_repo,
        base_branch=get_default_branch(cwd),
        changed_files=[],
    )

    # Step 2: Load PR details via gh for --pr, or resolve git comparison range.
    if args.pr:
        _populate_from_pr_details(ctx, args.pr, cwd)
    else:
        _populate_from_git_range(ctx, args.branch, args.compare, cwd)

    # Step 3: Load packrat historical signals and adopt offline --pr metadata.
    db_path = Path(args.packrat_db) if args.packrat_db else DEFAULT_PACKRAT_DB
    packrat_result = load_packrat_signals(
        ctx.repo,
        ctx.changed_files,
        db_path=db_path,
        target_pr=args.pr,
        repo_dir=cwd,
    )
    if args.pr:
        _adopt_offline_packrat_pr(ctx, packrat_result)

    # Step 4: Fall back to local git range detection only when changed_files is still empty.
    if args.pr and not ctx.changed_files:
        _populate_from_git_range(ctx, args.branch, args.compare, cwd)
        if ctx.changed_files and packrat_result.status == "ok":
            packrat_result = load_packrat_signals(
                ctx.repo,
                ctx.changed_files,
                db_path=db_path,
                target_pr=args.pr,
                repo_dir=cwd,
            )
    return ctx, packrat_result


def main() -> None:
    """Runs the PR reviewer recommendation workflow and prints Markdown or JSON."""
    args = _build_parser().parse_args()
    cwd = os.path.abspath(args.dir)
    if run_cmd(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd) != "true":
        print(f"Error: {cwd} is not a git repository.", file=sys.stderr)
        sys.exit(1)
        return

    ctx, packrat_result = _load_target_and_packrat(args, cwd)
    if not ctx.changed_files:
        print("No changed files found to analyze.", file=sys.stderr)
        sys.exit(0)
        return

    print(f"Found {len(ctx.changed_files)} changed files.", file=sys.stderr)
    history_base_ref = (
        _resolve_pr_local_merge_base(ctx, cwd)
        if args.pr
        else (ctx.merge_base or _resolve_existing_local_base_ref(cwd, ctx.base_branch))
    )
    cli_excluded = _parse_excluded_authors(args.exclude_author)
    resolver = _build_resolver(ctx, packrat_result, cli_excluded, args.pr, cwd)
    commit_touches = _collect_commit_touches_via_contributors(
        ctx.changed_files, cwd, history_base_ref
    )
    blame_touches = collect_blame_touches(
        ctx.changed_files, cwd, ctx.merge_base, ctx.head_ref, runner=_cmd_runner
    )
    now_ts = int(datetime.now().timestamp())
    primary, secondary = rank_reviewers(
        ctx.changed_files,
        commit_touches,
        blame_touches,
        packrat_result,
        resolver,
        now_ts=now_ts,
        gh_lookup=_make_gh_commit_lookup(ctx.repo, cwd),
    )

    if args.json:
        print(
            format_json_report(
                ctx.changed_files,
                primary,
                secondary,
                packrat_result,
                repo=ctx.repo,
                pr_number=args.pr,
            )
        )
        return
    print(
        format_markdown_report(
            ctx.changed_files,
            primary,
            secondary,
            packrat_result,
            repo=ctx.repo,
            pr_number=args.pr,
            pr_title=ctx.pr_title,
            pr_author=ctx.pr_author,
            gh_available=bool(shutil.which("gh")),
        )
    )


if __name__ == "__main__":
    main()
