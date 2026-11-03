#!/usr/bin/env python3
"""Read-only packrat availability and sync-state probe for the plan skill.

Inspects git remotes directly from `.git/config` (including git worktree
`commondir` layouts) and queries `~/.local/share/packrat/repos.sqlite` in
read-only URI mode (`mode=ro`) without creating files or mutating repository
state.

Typical usage:
    python3 packrat_probe.py --root .
    python3 packrat_probe.py --root . --text
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
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_TABLE_COUNT_NAMES: tuple[str, ...] = (
    "chunks",
    "commits",
    "pull_requests",
    "issues",
    "review_threads",
)
_BACKFILL_STREAMS: tuple[str, ...] = ("issues", "prs", "discussions")

_REMOTE_SECTION_RE = re.compile(
    r'^\s*\[\s*remote\s+(?:"((?:[^"\\]|\\.)+)"|\.([A-Za-z0-9_.-]+))\s*\]\s*(?:[#;].*)?$',
    re.IGNORECASE,
)
_SECTION_HEADER_RE = re.compile(r"^\s*\[.+\]\s*(?:[#;].*)?$")
_CONFIG_KV_RE = re.compile(r"^\s*([A-Za-z0-9-]+)\s*=\s*(.*?)\s*$")
_REMOTE_V_LINE_RE = re.compile(r"^(\S+)\s+(\S+)(?:\s+\((fetch|push)\))?\s*$")


@dataclass(frozen=True)
class PackratProbeResult:
    """Read-only probe snapshot of packrat sync state for a repository.

    Attributes:
        status: One of "ready", "not_synced", "unavailable", or "not_github".
        available: Whether a packrat CLI or synced database is available.
        synced: Whether the repository is registered with at least one chunk.
        repo: Canonical GitHub "owner/name" slug, or None for non-GitHub paths.
        checkout_root: Resolved local checkout directory path, or None.
        is_ephemeral_checkout: Whether checkout_root is a temporary checkout.
        db_path: Resolved path to packrat's repos.sqlite file.
        packrat_cmd: Command argv list to invoke packrat, or None.
        default_branch: Tracked default branch from repos.sqlite, or None.
        last_indexed_sha: Last git commit SHA indexed by packrat, or None.
        local_canonical_sha: Local remote-tracking SHA for default_branch, or None.
        counts: Per-table row counts for the repository when registered.
        warnings: Diagnostic warnings for SHA drift or incomplete backfills.
        user_note: Single-line setup hint for unsynced GitHub repositories.
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

    def to_dict(self) -> dict[str, Any]:
        """Returns a JSON-serializable dictionary of the probe result."""
        return asdict(self)


def _default_db_path() -> Path:
    """Returns the default user-level packrat SQLite database path."""
    return Path.home() / ".local" / "share" / "packrat" / "repos.sqlite"


def _parse_override_cmd(override: Sequence[str] | str) -> list[str] | None:
    """Normalizes an explicit command override into an argv list or None."""
    if isinstance(override, str):
        tokens = shlex.split(override)
    else:
        tokens = [str(part) for part in override if str(part).strip()]
    return tokens if tokens else None


def _candidate_skill_scripts(home: Path) -> list[Path]:
    """Returns candidate packrat.py paths across companion skill directories."""
    sibling_skills = Path(__file__).resolve().parents[2]
    skill_roots = (
        sibling_skills,
        home / ".gemini" / "config" / "skills",
        home / ".gemini" / "skills",
        home / ".claude" / "skills",
        home / ".agents" / "skills",
    )
    return [root / "packrat" / "scripts" / "packrat.py" for root in skill_roots]


def discover_packrat_cmd(override: Sequence[str] | None = None) -> list[str] | None:
    """Locates the packrat CLI executable or companion skill entrypoint.

    Checks `override` first, then `shutil.which("packrat")`,
    `~/.local/bin/packrat`, and `packrat/scripts/packrat.py` across sibling and
    user skill directories.

    Args:
        override: Optional command argv sequence (or command string) overriding
            automatic discovery. Passing an empty sequence disables discovery.

    Returns:
        Command argv list for invoking packrat, or None when unavailable.
    """
    if override is not None:
        return _parse_override_cmd(override)

    which_hit = shutil.which("packrat")
    if which_hit:
        return [which_hit]

    home = Path.home()
    local_bin = home / ".local" / "bin" / "packrat"
    if local_bin.is_file() and os.access(local_bin, os.X_OK):
        return [str(local_bin)]

    for script_path in _candidate_skill_scripts(home):
        if script_path.is_file():
            return ["python3", str(script_path)]

    return None


def _is_github_host(host: str) -> bool:
    """Returns whether host matches github.com or a github.com-* SSH alias."""
    lowered = host.lower()
    return lowered == "github.com" or lowered.startswith("github.com-")


def _extract_host_and_path(url: str) -> tuple[str, str] | None:
    """Extracts (host, path) from URL-style or SCP-style git remote strings."""
    cleaned = url.strip()
    if not cleaned:
        return None
    if "://" in cleaned:
        parsed = urlsplit(cleaned)
        return (parsed.hostname or "", parsed.path)
    if cleaned.startswith(("/", "./", "../", "~")) or ":" not in cleaned:
        return None
    head, _, path = cleaned.partition(":")
    if not head or "/" in head:
        return None
    host = head.rpartition("@")[2]
    return (host, path)


def _parse_github_slug_from_url(url: str) -> str | None:
    """Extracts `owner/name` from a GitHub remote URL, or None for other hosts."""
    extracted = _extract_host_and_path(url)
    if extracted is None:
        return None
    host, raw_path = extracted
    if not _is_github_host(host):
        return None
    parts = raw_path.strip("/").split("/")
    if len(parts) != 2:
        return None
    owner, raw_name = parts[0], parts[1]
    name = raw_name.removesuffix(".git")
    if not owner or not name:
        return None
    return f"{owner}/{name}"


def _normalize_repo_override(repo_override: str) -> str:
    """Validates and normalizes an explicit OWNER/NAME or GitHub URL override."""
    cleaned = repo_override.strip()
    url_slug = _parse_github_slug_from_url(cleaned)
    if url_slug is not None:
        return url_slug
    parts = cleaned.strip("/").split("/")
    if (
        len(parts) == 2
        and parts[0]
        and parts[1]
        and not any(c.isspace() for c in cleaned)
    ):
        name = parts[1].removesuffix(".git")
        if name:
            return f"{parts[0]}/{name}"
    raise ValueError(
        f"Invalid GitHub repository override {repo_override!r}; expected OWNER/NAME."
    )


def _resolve_commondir(git_dir: Path) -> Path:
    """Resolves the shared git common directory for a worktree gitdir."""
    commondir_file = git_dir / "commondir"
    if not commondir_file.is_file():
        return git_dir
    try:
        raw = commondir_file.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return git_dir
    if not raw:
        return git_dir
    target = Path(raw) if os.path.isabs(raw) else (git_dir / raw)
    return target.resolve()


def _parse_gitdir_pointer(dot_git: Path, checkout_root: Path) -> Path | None:
    """Parses a `.git` worktree file (`gitdir: ...`) into a resolved directory."""
    try:
        lines = dot_git.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("gitdir:"):
            raw_dir = stripped.partition(":")[2].strip()
            if not raw_dir:
                return None
            target = (
                Path(raw_dir) if os.path.isabs(raw_dir) else (checkout_root / raw_dir)
            )
            return target.resolve()
    return None


def _locate_git_dirs(root: Path) -> tuple[Path, Path | None, Path | None] | None:
    """Walks upward from `root` to locate `(checkout_root, git_dir, common_dir)`."""
    resolved = root.expanduser().resolve()
    start = resolved.parent if resolved.is_file() else resolved
    for candidate in (start, *start.parents):
        dot_git = candidate / ".git"
        if dot_git.is_dir():
            git_dir = dot_git.resolve()
            return candidate, git_dir, _resolve_commondir(git_dir)
        if dot_git.is_file() or dot_git.is_symlink():
            git_dir = _parse_gitdir_pointer(dot_git, candidate)
            if git_dir is not None and git_dir.is_dir():
                return candidate, git_dir, _resolve_commondir(git_dir)
            return candidate, None, None
        if (candidate / "HEAD").is_file() and (candidate / "config").is_file():
            return candidate, candidate, candidate
    return None


def _unquote_git_config_value(raw: str) -> str:
    """Strips inline comments and unescapes double quotes in a git-config value."""
    chars: list[str] = []
    in_quotes = False
    escaped = False
    for ch in raw:
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
    """Extracts remote URL lists keyed by remote name from `.git/config` text."""
    remotes: dict[str, list[str]] = {}
    current_remote: str | None = None
    for line in config_text.splitlines():
        remote_match = _REMOTE_SECTION_RE.match(line)
        if remote_match:
            raw_name = remote_match.group(1) or remote_match.group(2) or ""
            current_remote = raw_name.replace('\\"', '"').replace("\\\\", "\\")
            remotes.setdefault(current_remote, [])
            continue
        if _SECTION_HEADER_RE.match(line):
            current_remote = None
            continue
        if current_remote is None:
            continue
        kv_match = _CONFIG_KV_RE.match(line)
        if kv_match and kv_match.group(1).lower() == "url":
            value = _unquote_git_config_value(kv_match.group(2))
            if value:
                remotes[current_remote].append(value)
    return remotes


def _git_remote_v_fallback(checkout_root: Path) -> dict[str, list[str]]:
    """Queries `git remote -v` with hooks and fsmonitor disabled as a fallback."""
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(checkout_root),
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "remote",
                "-v",
            ],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    return _parse_remote_v_output(proc.stdout)


def _parse_remote_v_output(stdout: str) -> dict[str, list[str]]:
    """Parses `git remote -v` output, prioritizing fetch URLs over push URLs."""
    fetch_urls: dict[str, list[str]] = {}
    push_urls: dict[str, list[str]] = {}
    for line in stdout.splitlines():
        match = _REMOTE_V_LINE_RE.match(line.strip())
        if not match:
            continue
        name, url, kind = match.group(1), match.group(2), match.group(3)
        target = push_urls if kind == "push" else fetch_urls
        bucket = target.setdefault(name, [])
        if url not in bucket:
            bucket.append(url)
    merged: dict[str, list[str]] = {}
    for name in sorted(set(fetch_urls) | set(push_urls)):
        urls = list(fetch_urls.get(name, []))
        for url in push_urls.get(name, []):
            if url not in urls:
                urls.append(url)
        merged[name] = urls
    return merged


def _load_remotes(checkout_root: Path, common_dir: Path | None) -> dict[str, list[str]]:
    """Loads remote definitions from `.git/config`, falling back to `git remote -v`."""
    if common_dir is not None:
        config_path = common_dir / "config"
        try:
            text = config_path.read_text(encoding="utf-8", errors="replace")
            return _parse_git_config_remotes(text)
        except OSError:
            pass
    return _git_remote_v_fallback(checkout_root)


def _select_github_slug(remotes: dict[str, list[str]]) -> str | None:
    """Selects the first GitHub slug across upstream, origin, and sorted remotes."""
    ordered_names: list[str] = [
        name for name in ("upstream", "origin") if name in remotes
    ]
    ordered_names.extend(
        sorted(name for name in remotes if name not in ("upstream", "origin"))
    )
    for remote_name in ordered_names:
        for url in remotes[remote_name]:
            slug = _parse_github_slug_from_url(url)
            if slug is not None:
                return slug
    return None


def resolve_github_repo(
    root: Path,
    repo_override: str | None = None,
) -> tuple[str | None, Path | None]:
    """Resolves the canonical GitHub `owner/name` slug and git checkout root.

    Checks `upstream`, `origin`, and remaining sorted remotes for the first
    `github.com` or `github.com-*` URL, stripping any `.git` suffix with
    `removesuffix(".git")`.

    Args:
        root: Directory or file path inside the target workspace.
        repo_override: Optional explicit `OWNER/NAME` slug or GitHub URL.

    Returns:
        Tuple of `(repo_slug, checkout_root)`. Either element is None when not
        applicable.
    """
    git_dirs = _locate_git_dirs(root)
    checkout_root = git_dirs[0] if git_dirs is not None else None

    if repo_override is not None:
        slug = _normalize_repo_override(repo_override)
        effective_root = checkout_root or root.expanduser().resolve()
        return slug, effective_root

    if git_dirs is None:
        return None, None

    remotes = _load_remotes(git_dirs[0], git_dirs[2])
    return _select_github_slug(remotes), checkout_root


def _is_ephemeral_checkout(
    checkout_root: Path | None,
    local_checkout: Path | None,
) -> bool:
    """Returns whether checkout_root is an ephemeral temp or pr-review directory."""
    if local_checkout is not None or checkout_root is None:
        return False
    if any(part.startswith("pr-review-") for part in checkout_root.parts):
        return True
    temp_root = Path(tempfile.gettempdir()).resolve()
    return checkout_root.is_relative_to(temp_root)


def _read_packed_refs(packed_refs_path: Path) -> dict[str, str]:
    """Reads non-peeled `<sha> <ref>` entries from `.git/packed-refs`."""
    if not packed_refs_path.is_file():
        return {}
    try:
        lines = packed_refs_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError:
        return {}
    refs: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "^")):
            continue
        parts = stripped.split()
        if len(parts) >= 2:
            refs[parts[1]] = parts[0]
    return refs


def _read_ref_sha(
    common_dir: Path,
    ref_name: str,
    packed_refs: dict[str, str],
    depth: int = 0,
) -> str | None:
    """Resolves a loose or packed git ref to a commit SHA without subprocesses."""
    loose_path = common_dir / ref_name
    if loose_path.is_file():
        try:
            content = loose_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            content = ""
        if content.startswith("ref:"):
            if depth >= 4:
                return None
            target_ref = content.partition(":")[2].strip()
            return (
                _read_ref_sha(common_dir, target_ref, packed_refs, depth + 1)
                if target_ref
                else None
            )
        if content:
            return content.split()[0]
    return packed_refs.get(ref_name)


def _resolve_local_canonical_sha(
    common_dir: Path | None,
    default_branch: str,
) -> tuple[str | None, str | None]:
    """Resolves `refs/remotes/{upstream,origin}/<default_branch>` to `(sha, label)`."""
    if common_dir is None:
        return None, None
    packed_refs = _read_packed_refs(common_dir / "packed-refs")
    for remote in ("upstream", "origin"):
        ref_name = f"refs/remotes/{remote}/{default_branch}"
        sha = _read_ref_sha(common_dir, ref_name, packed_refs)
        if sha:
            return sha, f"{remote}/{default_branch}"
    return None, None


def _connect_readonly_sqlite(db_path: Path) -> sqlite3.Connection | None:
    """Opens `db_path` in read-only URI mode without creating files on disk."""
    if not db_path.is_file():
        return None
    uri = f"{db_path.as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        return conn
    except sqlite3.OperationalError as exc:
        if "readonly database" not in str(exc).lower():
            return None
    try:
        conn = sqlite3.connect(f"{uri}&immutable=1", uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        return conn
    except sqlite3.Error:
        return None


def _query_table_counts(conn: sqlite3.Connection, repo_id: int) -> dict[str, int]:
    """Queries per-table row counts for `repo_id`, tolerating missing tables."""
    counts: dict[str, int] = {}
    for table in _TABLE_COUNT_NAMES:
        try:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE repo_id = ?",
                (repo_id,),
            ).fetchone()
            counts[table] = int(row["n"]) if row is not None else 0
        except sqlite3.Error:
            counts[table] = 0
    return counts


def _collect_backfill_warnings(row: sqlite3.Row, repo_slug: str) -> list[str]:
    """Collects backfill warnings defensively across optional backfill columns."""
    keys = set(row.keys())
    warnings: list[str] = []
    for stream in _BACKFILL_STREAMS:
        done_col = f"{stream}_backfill_done_at"
        if done_col not in keys or row[done_col] is not None:
            continue
        cursor_col = f"{stream}_backfill_cursor"
        cursor = row[cursor_col] if cursor_col in keys else None
        if cursor is not None:
            warnings.append(
                f"{stream} backfill incomplete (resumes on next `packrat sync --repo {repo_slug}`)"
            )
        else:
            warnings.append(
                f"{stream} backfill incomplete (run `packrat sync --repo {repo_slug}`)"
            )
    return warnings


def _build_plan_user_note(
    status: str,
    repo: str | None,
    checkout_root: str | None,
    has_repo_row: bool,
) -> str:
    """Formats the exact single-line `/plan` user_note for the resolved status."""
    if not repo or status in ("ready", "not_github"):
        return ""
    if status == "unavailable":
        return (
            "Note: Codebase discovery could have been faster with `packrat` "
            f"installed and synced for `{repo}`."
        )
    if has_repo_row:
        return (
            "Note: Codebase discovery could have been faster with `packrat` "
            f"(`packrat sync --repo {repo}`)."
        )
    path_arg = checkout_root or "."
    return (
        "Note: Codebase discovery could have been faster with `packrat` "
        f"(`packrat repo add {repo} --path {path_arg} && packrat sync`)."
    )


def _build_unregistered_result(
    *,
    repo: str,
    checkout_str: str | None,
    ephemeral: bool,
    resolved_db: Path,
    cmd: list[str] | None,
) -> PackratProbeResult:
    """Builds a `not_synced` or `unavailable` result when no DB row exists."""
    status = "not_synced" if cmd is not None else "unavailable"
    return PackratProbeResult(
        status=status,
        available=cmd is not None,
        synced=False,
        repo=repo,
        checkout_root=checkout_str,
        is_ephemeral_checkout=ephemeral,
        db_path=str(resolved_db),
        packrat_cmd=cmd,
        user_note=_build_plan_user_note(status, repo, checkout_str, has_repo_row=False),
    )


def probe_packrat(
    root: Path,
    *,
    repo_override: str | None = None,
    local_checkout: Path | None = None,
    db_path: Path | None = None,
    packrat_cmd: Sequence[str] | None = None,
) -> PackratProbeResult:
    """Probes packrat availability, repository registration, and index freshness.

    Opens `repos.sqlite` strictly in read-only mode without creating database
    files on disk or running git network commands.

    Args:
        root: Target workspace directory to inspect.
        repo_override: Optional explicit `OWNER/NAME` slug overriding remotes.
        local_checkout: Optional persistent local clone overriding `root`.
        db_path: Optional override path to `repos.sqlite`.
        packrat_cmd: Optional explicit command override (empty disables lookup).

    Returns:
        Populated `PackratProbeResult` describing sync readiness and notes.
    """
    resolved_db = (
        (db_path if db_path is not None else _default_db_path()).expanduser().resolve()
    )
    cmd = discover_packrat_cmd(packrat_cmd)
    repo, detected_checkout = resolve_github_repo(root, repo_override=repo_override)

    effective_checkout = (
        local_checkout.expanduser().resolve()
        if local_checkout is not None
        else detected_checkout
    )
    ephemeral = _is_ephemeral_checkout(effective_checkout, local_checkout)
    checkout_str = str(effective_checkout) if effective_checkout is not None else None

    if repo is None:
        return PackratProbeResult(
            status="not_github",
            available=cmd is not None,
            synced=False,
            repo=None,
            checkout_root=checkout_str,
            is_ephemeral_checkout=ephemeral,
            db_path=str(resolved_db),
            packrat_cmd=cmd,
            user_note="",
        )

    conn = _connect_readonly_sqlite(resolved_db)
    if conn is None:
        return _build_unregistered_result(
            repo=repo,
            checkout_str=checkout_str,
            ephemeral=ephemeral,
            resolved_db=resolved_db,
            cmd=cmd,
        )

    try:
        try:
            row = conn.execute(
                "SELECT * FROM repos WHERE lower(full_name) = lower(?)",
                (repo,),
            ).fetchone()
        except sqlite3.Error:
            row = None

        if row is None:
            return _build_unregistered_result(
                repo=repo,
                checkout_str=checkout_str,
                ephemeral=ephemeral,
                resolved_db=resolved_db,
                cmd=cmd,
            )

        return _build_registered_result(
            conn=conn,
            row=row,
            fallback_repo=repo,
            ref_lookup_root=local_checkout if local_checkout is not None else root,
            checkout_str=checkout_str,
            ephemeral=ephemeral,
            resolved_db=resolved_db,
            cmd=cmd,
        )
    finally:
        conn.close()


def _build_registered_result(
    *,
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    fallback_repo: str,
    ref_lookup_root: Path,
    checkout_str: str | None,
    ephemeral: bool,
    resolved_db: Path,
    cmd: list[str] | None,
) -> PackratProbeResult:
    """Builds a probe result from a matched `repos` table row."""
    keys = set(row.keys())
    repo_id = int(row["repo_id"])
    repo_slug = (
        str(row["full_name"])
        if "full_name" in keys and row["full_name"]
        else fallback_repo
    )
    default_branch = (
        str(row["default_branch"] or "main") if "default_branch" in keys else "main"
    )
    last_indexed_sha = (
        str(row["last_indexed_sha"])
        if "last_indexed_sha" in keys and row["last_indexed_sha"]
        else None
    )

    counts = _query_table_counts(conn, repo_id)
    git_dirs = _locate_git_dirs(ref_lookup_root)
    common_dir = git_dirs[2] if git_dirs is not None else None
    local_sha, ref_label = _resolve_local_canonical_sha(common_dir, default_branch)

    warnings = _collect_backfill_warnings(row, repo_slug)
    if last_indexed_sha and local_sha and last_indexed_sha.lower() != local_sha.lower():
        warnings.append(
            f"SHA drift: last_indexed_sha ({last_indexed_sha[:12]}) differs from "
            f"local {ref_label} ({local_sha[:12]}); run `packrat sync --repo {repo_slug}`"
        )

    synced = counts.get("chunks", 0) > 0
    if synced:
        status = "ready"
    elif cmd is not None:
        status = "not_synced"
    else:
        status = "unavailable"

    return PackratProbeResult(
        status=status,
        available=synced or (cmd is not None),
        synced=synced,
        repo=repo_slug,
        checkout_root=checkout_str,
        is_ephemeral_checkout=ephemeral,
        db_path=str(resolved_db),
        packrat_cmd=cmd,
        default_branch=default_branch,
        last_indexed_sha=last_indexed_sha,
        local_canonical_sha=local_sha,
        counts=counts,
        warnings=warnings,
        user_note=_build_plan_user_note(
            status, repo_slug, checkout_str, has_repo_row=True
        ),
    )


def format_text_summary(result: PackratProbeResult) -> str:
    """Formats a single-line human-readable summary of `result`."""
    if result.status == "ready":
        sha_short = (result.last_indexed_sha or "none")[:12]
        chunks = result.counts.get("chunks", 0)
        commits = result.counts.get("commits", 0)
        prs = result.counts.get("pull_requests", 0)
        issues = result.counts.get("issues", 0)
        warn_part = f" warnings={len(result.warnings)}" if result.warnings else ""
        return (
            f"status=ready repo={result.repo} branch={result.default_branch} "
            f"sha={sha_short} chunks={chunks} commits={commits} "
            f"prs={prs} issues={issues}{warn_part}"
        )
    if result.status == "not_github":
        return "status=not_github repo=none"
    return f"status={result.status} repo={result.repo} | {result.user_note}"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parses command-line flags for `packrat_probe.py`."""
    parser = argparse.ArgumentParser(
        description="Read-only probe for packrat repository sync status.",
    )
    parser.add_argument(
        "--root",
        type=str,
        default=".",
        help="Workspace or repository directory to inspect (default: current directory).",
    )
    parser.add_argument(
        "--local-checkout",
        type=str,
        default=None,
        help="Optional persistent local checkout path overriding --root.",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="Explicit GitHub repository slug (OWNER/NAME) overriding git remotes.",
    )
    parser.add_argument(
        "--packrat-db",
        type=str,
        default=None,
        help="Override path to packrat's repos.sqlite database.",
    )
    parser.add_argument(
        "--packrat-cmd",
        type=str,
        default=None,
        help="Explicit packrat command override (empty string disables discovery).",
    )
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON probe payload (default).",
    )
    output_group.add_argument(
        "--text",
        action="store_true",
        help="Emit a single-line text summary.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint returning 0 across probe states and 2 on invalid arguments."""
    args = parse_args(argv)
    root_path = Path(args.root).expanduser()
    if not root_path.exists():
        print(f"Error: Root path does not exist: {root_path}", file=sys.stderr)
        return 2

    local_checkout = (
        Path(args.local_checkout).expanduser()
        if args.local_checkout is not None
        else None
    )
    db_path = (
        Path(args.packrat_db).expanduser() if args.packrat_db is not None else None
    )
    cmd_override: Sequence[str] | None = (
        shlex.split(args.packrat_cmd) if args.packrat_cmd is not None else None
    )

    try:
        result = probe_packrat(
            root_path,
            repo_override=args.repo,
            local_checkout=local_checkout,
            db_path=db_path,
            packrat_cmd=cmd_override,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if args.text:
        print(format_text_summary(result))
    else:
        print(json.dumps(result.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
