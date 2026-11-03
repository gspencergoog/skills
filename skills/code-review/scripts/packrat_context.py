#!/usr/bin/env python3
"""Queries read-only packrat historical context for code-review changed files.

Probes `~/.local/share/packrat/repos.sqlite` in read-only URI mode (`mode=ro`)
and inspects `.git/config` directly so preflight stays under 50 ms without
creating SQLite files, running `git fetch`, or launching `duckdb` on unsynced
repositories. When synced, returns file churn and review hotspots, prior review
threads, and linked pull requests or issues wrapped in
`<untrusted_repo_content>` tags.

Usage:
  git diff --name-only main...HEAD | packrat_context.py [--root DIR] -
  packrat_context.py [--root DIR] [--pr N] PATH...
  packrat_context.py --check-skill-budget agents/skills/code-review/SKILL.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.parse
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "packrat" / "repos.sqlite"
_CLOSING_UNTRUSTED_RE = re.compile(
    r"</\s*untrusted_repo_content\b[^>]*>", re.IGNORECASE
)
_BACKFILL_STREAMS = ("issues", "prs", "discussions")
_COUNT_TABLES = ("chunks", "commits", "pull_requests", "issues", "review_threads")
_HEX_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_REMOTE_SECTION_RE = re.compile(r'^\s*\[\s*remote\s+"([^"]+)"\s*\]\s*(?:[#;].*)?$')
_SECTION_HEADER_RE = re.compile(r"^\s*\[.*\]\s*(?:[#;].*)?$")
_SCP_REMOTE_RE = re.compile(r"^(?:[^@/\s]+@)?([A-Za-z0-9._-]+):([^\s]+)$")
_SQL_BATCH_SIZE = 400


@dataclass(frozen=True)
class PackratProbeResult:
    """Read-only probe status for a repository's local packrat mirror.

    Attributes:
        status: One of `"ready"`, `"not_synced"`, `"unavailable"`, or `"not_github"`.
        available: Whether `packrat` is installed or a ready mirror is present.
        synced: Whether the target repository has indexed chunks in `repos.sqlite`.
        repo: Canonical GitHub `<owner>/<name>` slug, or `None` when not on GitHub.
        checkout_root: Resolved git worktree root path, or `None` outside git.
        is_ephemeral_checkout: Whether the checkout lives in temp or `pr-review-*`.
        db_path: Path to the inspected `repos.sqlite` database.
        packrat_cmd: Discovered command argv for invoking `packrat`, if found.
        default_branch: Repository default branch recorded in `packrat`.
        last_indexed_sha: Last indexed commit SHA recorded in `packrat`.
        local_canonical_sha: Local remote-tracking SHA for `default_branch`.
        counts: Per-table row counts for the matched repository.
        warnings: Non-fatal backfill or SHA drift notices.
        user_note: Single-line setup note for unsynced or unavailable GitHub repos.
    """

    status: str
    available: bool
    synced: bool
    repo: str | None
    checkout_root: str | None
    is_ephemeral_checkout: bool
    db_path: str
    packrat_cmd: list[str] | None
    default_branch: str | None = None
    last_indexed_sha: str | None = None
    local_canonical_sha: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    user_note: str = ""


@dataclass(frozen=True)
class FileHotspot:
    """Historical commit and review-thread churn for one changed file.

    Attributes:
        file_path: Repository-relative file path.
        commit_count: Distinct indexed commits touching `file_path`.
        review_thread_count: Distinct historical review threads on `file_path`.
        unresolved_thread_count: Distinct unresolved review threads on `file_path`.
    """

    file_path: str
    commit_count: int
    review_thread_count: int
    unresolved_thread_count: int


@dataclass(frozen=True)
class HistoricalThread:
    """One prior inline review thread on a changed file.

    Attributes:
        pr_number: Pull request number where the review thread appeared.
        file_path: Repository-relative file path.
        line: Anchored line number, if recorded.
        is_resolved: Whether the thread was marked resolved on GitHub.
        reviewer: Reviewer handle, coerced to `"unknown"` when null or blank.
        untrusted_excerpt: Sanitized excerpt wrapped in `<untrusted_repo_content>`.
    """

    pr_number: int
    file_path: str
    line: int | None
    is_resolved: bool
    reviewer: str
    untrusted_excerpt: str


@dataclass(frozen=True)
class RelatedItem:
    """A historical pull request or linked issue related to changed files.

    Attributes:
        kind: Either `"pr"` or `"issue"`.
        number: GitHub pull request or issue number.
        state: GitHub state string (for example, `"OPEN"`, `"MERGED"`, `"CLOSED"`).
        link_type: Relationship label (`"touched_file"`, `"closes"`, `"references"`).
        untrusted_title: Sanitized title wrapped in `<untrusted_repo_content>`.
    """

    kind: str
    number: int
    state: str
    link_type: str
    untrusted_title: str


def discover_packrat_cmd(override: Sequence[str] | None = None) -> list[str] | None:
    """Locates the `packrat` CLI command without executing it.

    Args:
        override: Explicit command tokens from `--packrat-cmd`. Passing an empty
            sequence forces `None` for testing unavailable states.

    Returns:
        Command argv list for invoking `packrat`, or `None` when not installed.
    """
    if override is not None:
        cleaned = [str(part).strip() for part in override if str(part).strip()]
        return cleaned or None

    which_bin = shutil.which("packrat")
    if which_bin:
        return [which_bin]

    home = Path.home()
    local_bin = home / ".local" / "bin" / "packrat"
    if local_bin.is_file() and os.access(local_bin, os.X_OK):
        return [str(local_bin)]

    skill_roots = (
        Path(__file__).resolve().parents[2],
        home / ".gemini" / "config" / "skills",
        home / ".gemini" / "skills",
        home / ".claude" / "skills",
        home / ".agents" / "skills",
    )
    for skill_root in skill_roots:
        script = skill_root / "packrat" / "scripts" / "packrat.py"
        if script.is_file():
            return [sys.executable or "python3", str(script)]
    return None


def _resolve_commondir(git_dir: Path) -> Path:
    commondir_file = git_dir / "commondir"
    if commondir_file.is_file():
        try:
            rel = commondir_file.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return git_dir
        if rel:
            return (git_dir / rel).resolve()
    return git_dir


def _parse_gitdir_file(checkout_root: Path, dot_git: Path) -> Path | None:
    try:
        lines = dot_git.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.lower().startswith("gitdir:"):
            raw = line.split(":", 1)[1].strip()
            if raw:
                return (checkout_root / raw).resolve()
    return None


def _locate_git_dirs(root: Path) -> tuple[Path, Path, Path] | None:
    start = root.expanduser().resolve()
    if start.is_file():
        start = start.parent
    for candidate in (start, *start.parents):
        dot_git = candidate / ".git"
        if dot_git.is_dir():
            return candidate, dot_git, _resolve_commondir(dot_git)
        if dot_git.is_file():
            git_dir = _parse_gitdir_file(candidate, dot_git)
            if git_dir is not None:
                return candidate, git_dir, _resolve_commondir(git_dir)
    return None


def _is_github_host(host: str) -> bool:
    lowered = host.lower()
    return lowered == "github.com" or lowered.startswith("github.com-")


def _extract_github_slug(url: str) -> str | None:
    text = url.strip()
    if not text:
        return None
    if "://" in text:
        parsed = urllib.parse.urlsplit(text)
        host = parsed.hostname or ""
        raw_path = parsed.path
    else:
        match = _SCP_REMOTE_RE.match(text)
        if not match:
            return None
        host, raw_path = match.group(1), match.group(2)

    if not _is_github_host(host):
        return None
    trimmed = raw_path.strip("/").removesuffix(".git").strip("/")
    parts = trimmed.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return f"{parts[0]}/{parts[1]}"


def _normalize_repo_override(repo_override: str) -> str | None:
    from_url = _extract_github_slug(repo_override)
    if from_url:
        return from_url
    trimmed = repo_override.strip().strip("/").removesuffix(".git").strip("/")
    parts = trimmed.split("/")
    if len(parts) == 2 and parts[0] and parts[1]:
        return f"{parts[0]}/{parts[1]}"
    return None


def _clean_config_value(raw_val: str) -> str:
    chars: list[str] = []
    in_quotes = False
    escaped = False
    for ch in raw_val:
        if escaped:
            chars.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == '"':
            in_quotes = not in_quotes
        elif ch in ("#", ";") and not in_quotes:
            break
        else:
            chars.append(ch)
    return "".join(chars).strip()


def _parse_git_config_remotes(config_text: str) -> dict[str, list[str]]:
    remotes: dict[str, list[str]] = {}
    current_remote: str | None = None
    for line in config_text.splitlines():
        rem_match = _REMOTE_SECTION_RE.match(line)
        if rem_match:
            current_remote = rem_match.group(1)
            remotes.setdefault(current_remote, [])
            continue
        if _SECTION_HEADER_RE.match(line):
            current_remote = None
            continue
        if current_remote is not None and "=" in line:
            key, raw_val = line.split("=", 1)
            if key.strip().lower() == "url":
                cleaned = _clean_config_value(raw_val)
                if cleaned:
                    remotes[current_remote].append(cleaned)
    return remotes


def _read_remotes_via_git(checkout_root: Path) -> dict[str, list[str]]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "PAGER": "cat"}
    cmd = [
        "git",
        "-C",
        str(checkout_root),
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "remote",
        "-v",
    ]
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            env=env,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if res.returncode != 0:
        return {}
    remotes: dict[str, list[str]] = {}
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] not in remotes.setdefault(parts[0], []):
            remotes[parts[0]].append(parts[1])
    return remotes


def _load_remotes(checkout_root: Path, common_dir: Path) -> dict[str, list[str]]:
    config_path = common_dir / "config"
    try:
        config_text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _read_remotes_via_git(checkout_root)
    return _parse_git_config_remotes(config_text)


def _select_github_slug(remotes: dict[str, list[str]]) -> str | None:
    ordered = [name for name in ("upstream", "origin") if name in remotes]
    ordered.extend(
        sorted(name for name in remotes if name not in ("upstream", "origin"))
    )
    for name in ordered:
        for url in remotes[name]:
            slug = _extract_github_slug(url)
            if slug:
                return slug
    return None


def resolve_github_repo(
    root: Path, repo_override: str | None = None
) -> tuple[str | None, Path | None]:
    """Resolves the canonical GitHub `<owner>/<name>` slug and checkout root.

    Checks `.git` (including worktree `commondir` files) and falls back to
    `git remote -v` with hooks and fsmonitor disabled only when `.git/config` is
    unreadable. Evaluates remotes in order (`upstream`, `origin`, then sorted
    remaining remotes) and picks the first `github.com` or `github.com-*` match.

    Args:
        root: Working directory or subdirectory inside a checkout.
        repo_override: Optional explicit `<owner>/<name>` slug or GitHub URL.

    Returns:
        A tuple of `(repo_slug, checkout_root)`. Either element is `None` when
        not applicable.
    """
    located = _locate_git_dirs(root)
    checkout_root = located[0] if located is not None else None
    if repo_override is not None:
        return _normalize_repo_override(repo_override), checkout_root
    if located is None:
        return None, None
    remotes = _load_remotes(located[0], located[2])
    return _select_github_slug(remotes), checkout_root


def _read_packed_ref(common_dir: Path, ref_name: str) -> str | None:
    packed_path = common_dir / "packed-refs"
    try:
        lines = packed_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        if not line or line.startswith(("#", "^")):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == ref_name and _HEX_SHA_RE.match(parts[0]):
            return parts[0].lower()
    return None


def _read_ref_sha(
    git_dir: Path, common_dir: Path, ref_name: str, depth: int = 0
) -> str | None:
    if depth > 4:
        return None
    for base in (git_dir, common_dir):
        candidate = base / ref_name
        if not candidate.is_file():
            continue
        try:
            raw = candidate.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if raw.startswith("ref:"):
            target = raw.split(":", 1)[1].strip()
            return _read_ref_sha(git_dir, common_dir, target, depth + 1)
        if _HEX_SHA_RE.match(raw):
            return raw.lower()
    return _read_packed_ref(common_dir, ref_name)


def _resolve_head_sha(root: Path) -> str | None:
    located = _locate_git_dirs(root)
    if located is None:
        return None
    checkout_root, git_dir, common_dir = located
    sha = _read_ref_sha(git_dir, common_dir, "HEAD")
    if sha:
        return sha
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "PAGER": "cat"}
    cmd = [
        "git",
        "-C",
        str(checkout_root),
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "rev-parse",
        "--verify",
        "HEAD",
    ]
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, env=env, timeout=2, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    candidate = res.stdout.strip()
    return (
        candidate.lower()
        if res.returncode == 0 and _HEX_SHA_RE.match(candidate)
        else None
    )


def _resolve_canonical_sha(root: Path, default_branch: str) -> str | None:
    located = _locate_git_dirs(root)
    if located is None:
        return None
    _checkout_root, git_dir, common_dir = located
    for remote in ("upstream", "origin"):
        sha = _read_ref_sha(
            git_dir, common_dir, f"refs/remotes/{remote}/{default_branch}"
        )
        if sha:
            return sha
    return None


def _is_ephemeral_checkout(target_path: Path, local_checkout: Path | None) -> bool:
    if local_checkout is not None:
        return False
    resolved = target_path.expanduser().resolve()
    if any(part.startswith("pr-review-") for part in resolved.parts):
        return True
    tmp_root = Path(tempfile.gettempdir()).expanduser().resolve()
    return resolved.is_relative_to(tmp_root)


def _format_hint_dir(
    root: Path, checkout_root: Path | None, local_checkout: Path | None
) -> str:
    chosen = local_checkout or checkout_root or root.expanduser().resolve()
    raw = str(chosen)
    return shlex.quote(raw) if any(ch.isspace() for ch in raw) else raw


def _build_code_review_note(
    status: str,
    repo: str,
    *,
    zero_chunks: bool,
    ephemeral: bool,
    hint_dir: str,
) -> str:
    if status == "unavailable":
        return (
            f"Packrat context: unavailable (syncing {repo} with `packrat` "
            "speeds up historical review context)."
        )
    if status != "not_synced":
        return ""
    if zero_chunks:
        return (
            f"Packrat context: skipped ({repo} has 0 indexed chunks; run "
            f"`packrat sync --repo {repo}` for faster history lookups)."
        )
    if ephemeral:
        return (
            f"Packrat context: skipped ({repo} is not synced; run "
            f"`packrat repo add {repo} && packrat sync --repo {repo} --github-only` "
            "for faster history lookups)."
        )
    return (
        f"Packrat context: skipped ({repo} is not synced; run "
        f"`packrat repo add {repo} --path {hint_dir} && packrat sync` "
        "for faster history lookups)."
    )


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.expanduser().resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=1.0)
    conn.row_factory = sqlite3.Row
    return conn


def _safe_repo_counts(conn: sqlite3.Connection, repo_id: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in _COUNT_TABLES:
        try:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE repo_id = ?",
                (repo_id,),
            ).fetchone()
            counts[table] = int(row["n"]) if row else 0
        except sqlite3.Error:
            counts[table] = 0
    return counts


def _collect_probe_warnings(
    row: sqlite3.Row,
    default_branch: str,
    last_indexed_sha: str | None,
    local_canonical_sha: str | None,
) -> list[str]:
    warnings: list[str] = []
    keys = set(row.keys())
    for stream in _BACKFILL_STREAMS:
        done_col = f"{stream}_backfill_done_at"
        if done_col not in keys or row[done_col]:
            continue
        cursor_col = f"{stream}_backfill_cursor"
        if cursor_col in keys and row[cursor_col]:
            warnings.append(
                f"{stream} backfill incomplete; resumes on next packrat sync"
            )
        else:
            warnings.append(
                f"{stream} backfill incomplete; history not fetched yet, run packrat sync"
            )
    if (
        last_indexed_sha
        and local_canonical_sha
        and last_indexed_sha.lower() != local_canonical_sha.lower()
    ):
        warnings.append(
            f"packrat last_indexed_sha ({last_indexed_sha[:10]}) differs from "
            f"local {default_branch} ({local_canonical_sha[:10]}); run packrat sync"
        )
    return warnings


def _unregistered_result(
    repo: str,
    checkout_str: str | None,
    ephemeral: bool,
    db_str: str,
    resolved_cmd: list[str] | None,
    hint_dir: str,
) -> PackratProbeResult:
    status = "not_synced" if resolved_cmd is not None else "unavailable"
    note = _build_code_review_note(
        status, repo, zero_chunks=False, ephemeral=ephemeral, hint_dir=hint_dir
    )
    return PackratProbeResult(
        status=status,
        available=resolved_cmd is not None,
        synced=False,
        repo=repo,
        checkout_root=checkout_str,
        is_ephemeral_checkout=ephemeral,
        db_path=db_str,
        packrat_cmd=resolved_cmd,
        user_note=note,
    )


def _read_repo_probe_row(
    resolved_db: Path, repo_slug: str
) -> tuple[sqlite3.Row, dict[str, int]] | None:
    if not resolved_db.is_file():
        return None
    try:
        conn = _connect_readonly(resolved_db)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT * FROM repos WHERE lower(full_name) = lower(?)", (repo_slug,)
        ).fetchone()
        if row is None:
            return None
        return row, _safe_repo_counts(conn, int(row["repo_id"]))
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _resolve_indexed_status(
    chunk_count: int,
    resolved_cmd: list[str] | None,
    canonical_repo: str,
    ephemeral: bool,
    hint_dir: str,
) -> tuple[str, bool, bool, str]:
    if chunk_count > 0:
        return "ready", True, True, ""
    status = "not_synced" if resolved_cmd is not None else "unavailable"
    note = _build_code_review_note(
        status, canonical_repo, zero_chunks=True, ephemeral=ephemeral, hint_dir=hint_dir
    )
    return status, resolved_cmd is not None, False, note


def probe_packrat(
    root: Path,
    *,
    repo_override: str | None = None,
    local_checkout: Path | None = None,
    db_path: Path | None = None,
    packrat_cmd: Sequence[str] | None = None,
) -> PackratProbeResult:
    """Probes whether `packrat` has indexed context for the target repository.

    Opens `repos.sqlite` strictly in read-only mode (`mode=ro`) without creating
    files on disk or running `git fetch`.

    Args:
        root: Checkout directory to inspect.
        repo_override: Optional explicit `<owner>/<name>` slug.
        local_checkout: Optional persistent local clone path when `root` is temporary.
        db_path: Optional override path to `repos.sqlite`.
        packrat_cmd: Optional override command argv for `packrat`.

    Returns:
        A `PackratProbeResult` describing readiness, counts, warnings, and user notes.
    """
    resolved_db = (db_path or DEFAULT_DB_PATH).expanduser()
    resolved_cmd = discover_packrat_cmd(packrat_cmd)
    repo_slug, checkout_root = resolve_github_repo(root, repo_override)
    checkout_str = str(checkout_root) if checkout_root is not None else None
    ephemeral = _is_ephemeral_checkout(checkout_root or root, local_checkout)

    if repo_slug is None:
        return PackratProbeResult(
            status="not_github",
            available=resolved_cmd is not None,
            synced=False,
            repo=None,
            checkout_root=checkout_str,
            is_ephemeral_checkout=ephemeral,
            db_path=str(resolved_db),
            packrat_cmd=resolved_cmd,
            user_note="",
        )

    hint_dir = _format_hint_dir(root, checkout_root, local_checkout)
    loaded = _read_repo_probe_row(resolved_db, repo_slug)
    if loaded is None:
        return _unregistered_result(
            repo_slug, checkout_str, ephemeral, str(resolved_db), resolved_cmd, hint_dir
        )

    row, counts = loaded
    keys = set(row.keys())
    canonical_repo = str(row["full_name"])
    default_branch = (
        str(row["default_branch"] or "main") if "default_branch" in keys else "main"
    )
    last_sha = (
        str(row["last_indexed_sha"])
        if "last_indexed_sha" in keys and row["last_indexed_sha"]
        else None
    )
    ref_root = local_checkout or checkout_root
    local_sha = _resolve_canonical_sha(ref_root, default_branch) if ref_root else None
    warnings = _collect_probe_warnings(row, default_branch, last_sha, local_sha)
    status, available, synced, note = _resolve_indexed_status(
        counts.get("chunks", 0), resolved_cmd, canonical_repo, ephemeral, hint_dir
    )

    return PackratProbeResult(
        status=status,
        available=available,
        synced=synced,
        repo=canonical_repo,
        checkout_root=checkout_str,
        is_ephemeral_checkout=ephemeral,
        db_path=str(resolved_db),
        packrat_cmd=resolved_cmd,
        default_branch=default_branch,
        last_indexed_sha=last_sha,
        local_canonical_sha=local_sha,
        counts=counts,
        warnings=warnings,
        user_note=note,
    )


def wrap_untrusted_snippet(source: str, text: str, max_chars: int = 160) -> str:
    """Wraps untrusted repository prose in an escaped `<untrusted_repo_content>` tag.

    XML-escapes `source`, collapses whitespace and newlines, neutralizes any
    closing `</untrusted_repo_content>` tags case-insensitively, and truncates
    the excerpt to `max_chars`.

    Args:
        source: Provenance label such as `"review_thread:PR#42"` or `"issue#7"`.
        text: Untrusted prose from GitHub comments, titles, or bodies.
        max_chars: Maximum character length of the inner snippet text.

    Returns:
        A single-line `<untrusted_repo_content source="...">...</untrusted_repo_content>` string.
    """
    safe_source = (
        source.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    collapsed = re.sub(r"\s+", " ", text or "").strip()
    sanitized = _CLOSING_UNTRUSTED_RE.sub("&lt;/untrusted_repo_content&gt;", collapsed)
    if max_chars > 0 and len(sanitized) > max_chars:
        if max_chars > 3:
            sanitized = sanitized[: max_chars - 3].rstrip() + "..."
        else:
            sanitized = sanitized[:max_chars]
    sanitized = _CLOSING_UNTRUSTED_RE.sub("&lt;/untrusted_repo_content&gt;", sanitized)
    return f'<untrusted_repo_content source="{safe_source}">{sanitized}</untrusted_repo_content>'


def check_single_read_budget(
    path: Path, max_raw_bytes: int = 43800, max_formatted_bytes: int = 46080
) -> tuple[bool, int, int]:
    """Checks that a file fits within raw and line-numbered single-read byte caps.

    Args:
        path: File path to measure.
        max_raw_bytes: Strict upper bound (`<`) on raw UTF-8 byte length.
        max_formatted_bytes: Strict upper bound (`<`) on `"N: <line>"` byte length.

    Returns:
        A tuple of `(within_budget, raw_bytes, formatted_bytes)`.
    """
    raw = path.read_bytes()
    raw_bytes = len(raw)
    lines = raw.decode("utf-8", errors="replace").splitlines(keepends=True)
    formatted_bytes = sum(
        len(f"{idx}: {line}".encode()) for idx, line in enumerate(lines, 1)
    )
    within = raw_bytes < max_raw_bytes and formatted_bytes < max_formatted_bytes
    return within, raw_bytes, formatted_bytes


def _batched(
    items: Sequence[str], size: int = _SQL_BATCH_SIZE
) -> Iterable[Sequence[str]]:
    for idx in range(0, len(items), size):
        yield items[idx : idx + size]


def _normalize_changed_paths(changed_files: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for raw in changed_files:
        cleaned = raw.strip().replace("\\", "/")
        while cleaned.startswith("./"):
            cleaned = cleaned[2:]
        cleaned = cleaned.lstrip("/")
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            normalized.append(cleaned)
    return normalized


def _find_excluded_prs(
    conn: sqlite3.Connection,
    repo_id: int,
    exclude_pr: int | None,
    exclude_head_sha: str | None,
) -> set[int]:
    excluded: set[int] = set()
    if exclude_pr is not None:
        excluded.add(int(exclude_pr))
    if exclude_head_sha:
        try:
            rows = conn.execute(
                "SELECT number FROM pull_requests "
                "WHERE repo_id = ? AND head_sha IS NOT NULL AND lower(head_sha) = lower(?)",
                (repo_id, exclude_head_sha.strip()),
            ).fetchall()
            excluded.update(int(row["number"]) for row in rows)
        except sqlite3.Error:
            pass
    return excluded


def _pr_exclusion_sql(excluded_prs: set[int]) -> tuple[str, list[int]]:
    if not excluded_prs:
        return "", []
    ordered = sorted(excluded_prs)
    placeholders = ",".join("?" for _ in ordered)
    return f" AND pr_number NOT IN ({placeholders})", ordered


def _query_commit_counts(
    conn: sqlite3.Connection, repo_id: int, files: Sequence[str]
) -> dict[str, int]:
    counts: dict[str, int] = {}
    try:
        for batch in _batched(files):
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"SELECT file_path, COUNT(DISTINCT sha) AS commit_count "
                f"FROM commit_files WHERE repo_id = ? AND file_path IN ({placeholders}) "
                f"GROUP BY file_path",
                [repo_id, *batch],
            ).fetchall()
            for row in rows:
                counts[str(row["file_path"])] = int(row["commit_count"])
    except sqlite3.Error:
        return counts
    return counts


def _query_thread_counts(
    conn: sqlite3.Connection,
    repo_id: int,
    files: Sequence[str],
    excluded_prs: set[int],
) -> dict[str, tuple[int, int]]:
    counts: dict[str, tuple[int, int]] = {}
    excl_clause, excl_params = _pr_exclusion_sql(excluded_prs)
    try:
        for batch in _batched(files):
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                "SELECT file_path, "
                "COUNT(DISTINCT thread_id) AS review_thread_count, "
                "COUNT(DISTINCT CASE WHEN is_resolved = 0 THEN thread_id END) "
                "AS unresolved_thread_count "
                f"FROM review_threads WHERE repo_id = ? AND file_path IN ({placeholders})"
                f"{excl_clause} GROUP BY file_path",
                [repo_id, *batch, *excl_params],
            ).fetchall()
            for row in rows:
                counts[str(row["file_path"])] = (
                    int(row["review_thread_count"]),
                    int(row["unresolved_thread_count"]),
                )
    except sqlite3.Error:
        return counts
    return counts


def _build_hotspots(
    conn: sqlite3.Connection,
    repo_id: int,
    files: Sequence[str],
    excluded_prs: set[int],
) -> list[dict[str, Any]]:
    commit_counts = _query_commit_counts(conn, repo_id, files)
    thread_counts = _query_thread_counts(conn, repo_id, files, excluded_prs)
    all_paths = set(commit_counts) | set(thread_counts)
    hotspots: list[FileHotspot] = []
    for path in all_paths:
        commits = commit_counts.get(path, 0)
        threads, unresolved = thread_counts.get(path, (0, 0))
        if commits > 0 or threads > 0:
            hotspots.append(
                FileHotspot(
                    file_path=path,
                    commit_count=commits,
                    review_thread_count=threads,
                    unresolved_thread_count=unresolved,
                )
            )
    hotspots.sort(
        key=lambda h: (
            -h.unresolved_thread_count,
            -h.review_thread_count,
            -h.commit_count,
            h.file_path,
        )
    )
    return [asdict(item) for item in hotspots]


def _extract_thread_body(row: sqlite3.Row) -> str:
    body = str(row["body"] or "").strip()
    if body:
        return body
    cols = set(row.keys())
    raw_comments = row["comments_json"] if "comments_json" in cols else "[]"
    try:
        parsed = json.loads(raw_comments or "[]")
    except (TypeError, ValueError):
        return ""
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
        return str(parsed[0].get("body") or "").strip()
    return ""


def _row_to_historical_thread(row: sqlite3.Row) -> dict[str, Any]:
    pr_num = int(row["pr_number"])
    raw_reviewer = str(row["reviewer"] or "").strip()
    reviewer = raw_reviewer or "unknown"
    line_val = int(row["line"]) if row["line"] is not None else None
    excerpt = wrap_untrusted_snippet(
        f"review_thread:PR#{pr_num}", _extract_thread_body(row)
    )
    return asdict(
        HistoricalThread(
            pr_number=pr_num,
            file_path=str(row["file_path"]),
            line=line_val,
            is_resolved=bool(row["is_resolved"]),
            reviewer=reviewer,
            untrusted_excerpt=excerpt,
        )
    )


def _query_prior_threads(
    conn: sqlite3.Connection,
    repo_id: int,
    files: Sequence[str],
    excluded_prs: set[int],
    max_threads: int,
) -> list[dict[str, Any]]:
    if max_threads <= 0:
        return []
    excl_clause, excl_params = _pr_exclusion_sql(excluded_prs)
    collected: list[sqlite3.Row] = []
    try:
        for batch in _batched(files):
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                "SELECT pr_number, file_path, line, is_resolved, reviewer, body, "
                "comments_json, COALESCE(updated_at, created_at, '') AS sort_ts, thread_id "
                f"FROM review_threads WHERE repo_id = ? AND file_path IN ({placeholders})"
                f"{excl_clause}",
                [repo_id, *batch, *excl_params],
            ).fetchall()
            collected.extend(rows)
    except sqlite3.Error:
        return []

    # Sort unresolved first, then newest timestamp/PR descending.
    collected.sort(
        key=lambda r: (str(r["sort_ts"] or ""), int(r["pr_number"])),
        reverse=True,
    )
    collected.sort(key=lambda r: int(r["is_resolved"] or 0))
    return [_row_to_historical_thread(row) for row in collected[:max_threads]]


def _collect_pr_query_rows(
    conn: sqlite3.Connection,
    sql_tpl: str,
    repo_id: int,
    files: Sequence[str],
    excluded_prs: set[int],
    by_number: dict[int, sqlite3.Row],
) -> None:
    try:
        for batch in _batched(files):
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                sql_tpl.format(placeholders=placeholders), [repo_id, *batch]
            ).fetchall()
            for row in rows:
                num = int(row["number"])
                if num not in excluded_prs:
                    by_number[num] = row
    except sqlite3.Error:
        return


def _fetch_touching_prs(
    conn: sqlite3.Connection,
    repo_id: int,
    files: Sequence[str],
    excluded_prs: set[int],
) -> list[sqlite3.Row]:
    by_number: dict[int, sqlite3.Row] = {}
    queries = (
        (
            "SELECT DISTINCT p.number, p.state, p.title, "
            "COALESCE(p.updated_at, '') AS sort_ts "
            "FROM pr_files pf JOIN pull_requests p "
            "ON p.repo_id = pf.repo_id AND p.number = pf.pr_number "
            "WHERE pf.repo_id = ? AND pf.file_path IN ({placeholders})"
        ),
        (
            "SELECT DISTINCT p.number, p.state, p.title, "
            "COALESCE(p.updated_at, '') AS sort_ts "
            "FROM commit_files cf "
            "JOIN commits c ON c.repo_id = cf.repo_id AND c.sha = cf.sha "
            "JOIN pull_requests p ON p.repo_id = c.repo_id AND p.number = c.pr_number "
            "WHERE cf.repo_id = ? AND cf.file_path IN ({placeholders})"
        ),
    )
    for sql_tpl in queries:
        _collect_pr_query_rows(conn, sql_tpl, repo_id, files, excluded_prs, by_number)
    return sorted(
        by_number.values(),
        key=lambda r: (str(r["sort_ts"]), int(r["number"])),
        reverse=True,
    )


def _normalize_link_type(raw_link: str) -> str:
    lowered = raw_link.strip().lower()
    return "closes" if lowered.startswith("close") else "references"


def _fetch_linked_issues(
    conn: sqlite3.Connection, repo_id: int, pr_numbers: Sequence[int]
) -> list[RelatedItem]:
    if not pr_numbers:
        return []
    by_issue: dict[int, tuple[str, str, str, str]] = {}
    try:
        placeholders = ",".join("?" for _ in pr_numbers)
        rows = conn.execute(
            "SELECT i.number, i.state, i.title, COALESCE(i.updated_at, '') AS sort_ts, "
            "ipl.link_type FROM issue_pr_links ipl "
            "JOIN issues i ON i.repo_id = ipl.repo_id AND i.number = ipl.issue_number "
            f"WHERE ipl.repo_id = ? AND ipl.pr_number IN ({placeholders})",
            [repo_id, *pr_numbers],
        ).fetchall()
    except sqlite3.Error:
        return []

    for row in rows:
        num = int(row["number"])
        link_type = _normalize_link_type(str(row["link_type"] or "references"))
        existing = by_issue.get(num)
        if existing is None or link_type == "closes":
            by_issue[num] = (
                str(row["state"] or "OPEN"),
                link_type,
                str(row["title"] or ""),
                str(row["sort_ts"] or ""),
            )

    ordered = sorted(
        by_issue.items(),
        key=lambda kv: (kv[1][0].upper() == "OPEN", kv[1][3], kv[0]),
        reverse=True,
    )
    return [
        RelatedItem(
            kind="issue",
            number=num,
            state=meta[0],
            link_type=meta[1],
            untrusted_title=wrap_untrusted_snippet(f"issue#{num}", meta[2]),
        )
        for num, meta in ordered
    ]


def _query_related_items(
    conn: sqlite3.Connection,
    repo_id: int,
    files: Sequence[str],
    excluded_prs: set[int],
    max_items: int,
) -> list[dict[str, Any]]:
    if max_items <= 0:
        return []
    pr_rows = _fetch_touching_prs(conn, repo_id, files, excluded_prs)
    pr_items = [
        RelatedItem(
            kind="pr",
            number=int(row["number"]),
            state=str(row["state"] or ""),
            link_type="touched_file",
            untrusted_title=wrap_untrusted_snippet(
                f"pr#{int(row['number'])}", str(row["title"] or "")
            ),
        )
        for row in pr_rows
    ]
    issue_items = _fetch_linked_issues(
        conn, repo_id, [int(row["number"]) for row in pr_rows]
    )
    issue_quota = max(1, max_items // 2) if issue_items else 0
    selected_issues = issue_items[:issue_quota]
    selected_prs = pr_items[: max_items - len(selected_issues)]
    remaining = max_items - len(selected_issues) - len(selected_prs)
    if remaining > 0:
        selected_issues.extend(
            issue_items[len(selected_issues) : len(selected_issues) + remaining]
        )
    return [asdict(item) for item in [*selected_issues, *selected_prs]]


def query_changed_files_context(
    db_path: Path,
    repo_full_name: str,
    changed_files: Sequence[str],
    *,
    exclude_pr: int | None = None,
    exclude_head_sha: str | None = None,
    max_threads: int = 8,
    max_items: int = 8,
    probe: PackratProbeResult | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Queries historical hotspots, review threads, and related items for changed files.

    Excludes any pull request matching `exclude_pr` or `exclude_head_sha` so a
    pull request under review never leaks its own metadata or review threads.
    Queries `commit_files` and `review_threads` separately and merges them in
    Python so `--github-only` mirrors retain thread hotspots without join fan-out.

    Args:
        db_path: Path to `repos.sqlite`.
        repo_full_name: Canonical GitHub `<owner>/<name>` slug.
        changed_files: Repository-relative changed file paths.
        exclude_pr: Optional PR number to exclude from results.
        exclude_head_sha: Optional HEAD commit SHA to exclude matching PRs.
        max_threads: Maximum historical review threads to return.
        max_items: Maximum related PRs and issues to return.
        probe: Optional precomputed `PackratProbeResult`.
        root: Optional checkout path used when `probe` or `exclude_head_sha` is omitted.

    Returns:
        A dict with keys `"probe"`, `"hotspots"`, `"prior_review_threads"`, and
        `"related_items"`.
    """
    active_probe = probe or probe_packrat(
        root or Path("."), repo_override=repo_full_name, db_path=db_path
    )
    clean_files = _normalize_changed_paths(changed_files)
    resolved_db = db_path.expanduser()
    if active_probe.status != "ready" or not clean_files or not resolved_db.is_file():
        return {
            "probe": asdict(active_probe),
            "hotspots": [],
            "prior_review_threads": [],
            "related_items": [],
        }

    head_sha = exclude_head_sha
    if head_sha is None and root is not None:
        head_sha = _resolve_head_sha(root)

    try:
        conn = _connect_readonly(resolved_db)
    except sqlite3.Error:
        return {
            "probe": asdict(active_probe),
            "hotspots": [],
            "prior_review_threads": [],
            "related_items": [],
        }

    try:
        repo_row = conn.execute(
            "SELECT repo_id FROM repos WHERE lower(full_name) = lower(?)",
            (repo_full_name,),
        ).fetchone()
        if repo_row is None:
            return {
                "probe": asdict(active_probe),
                "hotspots": [],
                "prior_review_threads": [],
                "related_items": [],
            }
        repo_id = int(repo_row["repo_id"])
        excluded_prs = _find_excluded_prs(conn, repo_id, exclude_pr, head_sha)
        hotspots = _build_hotspots(conn, repo_id, clean_files, excluded_prs)
        threads = _query_prior_threads(
            conn, repo_id, clean_files, excluded_prs, max_threads
        )
        related = _query_related_items(
            conn, repo_id, clean_files, excluded_prs, max_items
        )
    finally:
        conn.close()

    return {
        "probe": asdict(active_probe),
        "hotspots": hotspots,
        "prior_review_threads": threads,
        "related_items": related,
    }


def _append_bounded_lines(
    lines: list[str], candidates: Iterable[str], max_bytes: int
) -> None:
    for candidate in candidates:
        trial = "\n".join([*lines, candidate])
        if len(trial.encode()) <= max_bytes:
            lines.append(candidate)


def format_markdown_context(payload: dict[str, Any], max_bytes: int = 600) -> str:
    """Formats `query_changed_files_context` output into a byte-capped Markdown block.

    Args:
        payload: Result dictionary from `query_changed_files_context`.
        max_bytes: Maximum UTF-8 byte budget for the rendered block.

    Returns:
        Formatted Markdown sub-list, `user_note` when unsynced, or `""` when empty.
    """
    probe_data = payload.get("probe", {})
    if probe_data.get("status") != "ready":
        return str(probe_data.get("user_note") or "")

    hotspots = payload.get("hotspots", [])
    threads = payload.get("prior_review_threads", [])
    related = payload.get("related_items", [])
    if not hotspots and not threads and not related:
        return ""

    lines = ["- **Historical Context (packrat, upstream/main)**:"]
    candidates: list[str] = []
    if hotspots:
        parts = [
            f"`{h['file_path']}` ({h['commit_count']}c/{h['review_thread_count']}t)"
            for h in hotspots[:3]
        ]
        candidates.append(f"  - Hotspots: {', '.join(parts)}")
    for item in related[:2]:
        label = "Issue" if item["kind"] == "issue" else "PR"
        candidates.append(
            f"  - {label} #{item['number']} ({item['state']}): {item['untrusted_title']}"
        )
    for th in threads[:2]:
        loc = f"{th['file_path']}:{th['line']}" if th["line"] else th["file_path"]
        candidates.append(
            f"  - PR#{th['pr_number']} (`{loc}`, @{th['reviewer']}): "
            f"{th['untrusted_excerpt']}"
        )
    _append_bounded_lines(lines, candidates, max_bytes)
    return "\n".join(lines) if len(lines) > 1 else ""


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Query read-only packrat context for code-review.",
        exit_on_error=False,
    )
    parser.add_argument("--root", default=".", help="Repository checkout root.")
    parser.add_argument(
        "--local-checkout",
        default=None,
        help="Persistent local checkout path when --root is temporary.",
    )
    parser.add_argument("--repo", default=None, help="Explicit GitHub OWNER/NAME slug.")
    parser.add_argument("--pr", type=int, default=None, help="PR number to exclude.")
    parser.add_argument(
        "--exclude-head-sha",
        default=None,
        help="Commit SHA whose matching PRs should be excluded.",
    )
    parser.add_argument(
        "--packrat-db",
        default=None,
        help="Override path to packrat repos.sqlite.",
    )
    parser.add_argument(
        "--packrat-cmd",
        default=None,
        help="Override command string for invoking packrat.",
    )
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="Emit a <=600-byte Markdown summary instead of JSON.",
    )
    parser.add_argument(
        "--check-skill-budget",
        nargs="?",
        const=True,
        default=False,
        metavar="PATH",
        help="Check raw (<43,800) and line-numbered (<46,080) byte caps on SKILL.md.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Changed file paths, or - to read newline-delimited paths from stdin.",
    )
    return parser.parse_args(list(argv))


def _run_budget_gate(args: argparse.Namespace) -> int:
    targets: list[Path] = []
    if isinstance(args.check_skill_budget, str):
        targets.append(Path(args.check_skill_budget))
    targets.extend(Path(p) for p in args.paths)
    if not targets:
        targets.append(Path(__file__).resolve().parents[1] / "SKILL.md")

    reports: list[dict[str, Any]] = []
    all_ok = True
    for target in targets:
        if not target.is_file():
            print(f"Error: file not found: {target}", file=sys.stderr)
            return 2
        ok, raw_bytes, formatted_bytes = check_single_read_budget(target)
        all_ok = all_ok and ok
        reports.append(
            {
                "path": str(target),
                "ok": ok,
                "raw_bytes": raw_bytes,
                "max_raw_bytes": 43800,
                "formatted_bytes": formatted_bytes,
                "max_formatted_bytes": 46080,
            }
        )
    out: Any = reports[0] if len(reports) == 1 else reports
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if all_ok else 1


def _read_cli_paths(raw_paths: Sequence[str]) -> list[str]:
    expanded: list[str] = []
    for entry in raw_paths:
        if entry == "-":
            expanded.extend(line.strip() for line in sys.stdin if line.strip())
        elif entry.strip():
            expanded.append(entry.strip())
    return expanded


def main(argv: Sequence[str] | None = None) -> int:
    """Runs the CLI entrypoint and returns the process exit code."""
    try:
        args = _parse_args(sys.argv[1:] if argv is None else argv)
    except (argparse.ArgumentError, SystemExit, ValueError):
        print(__doc__, file=sys.stderr)
        return 2

    if args.check_skill_budget is not False:
        return _run_budget_gate(args)

    root = Path(args.root)
    local_checkout = Path(args.local_checkout) if args.local_checkout else None
    db_path = Path(args.packrat_db) if args.packrat_db else DEFAULT_DB_PATH
    cmd_override = (
        shlex.split(args.packrat_cmd) if args.packrat_cmd is not None else None
    )
    changed_files = _read_cli_paths(args.paths)

    probe = probe_packrat(
        root,
        repo_override=args.repo,
        local_checkout=local_checkout,
        db_path=db_path,
        packrat_cmd=cmd_override,
    )
    head_sha = args.exclude_head_sha
    if head_sha is None and probe.checkout_root is not None:
        head_sha = _resolve_head_sha(Path(probe.checkout_root))

    if probe.status == "ready" and probe.repo is not None:
        payload = query_changed_files_context(
            db_path,
            probe.repo,
            changed_files,
            exclude_pr=args.pr,
            exclude_head_sha=head_sha,
            probe=probe,
            root=root,
        )
    else:
        payload = {
            "probe": asdict(probe),
            "hotspots": [],
            "prior_review_threads": [],
            "related_items": [],
        }

    if args.markdown:
        rendered = format_markdown_context(payload)
        if rendered:
            sys.stdout.write(rendered + "\n")
        return 0

    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
