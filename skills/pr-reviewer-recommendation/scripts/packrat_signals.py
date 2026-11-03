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

"""Read-only SQLite signal loader for packrat historical PR reviews and threads.

Queries ``~/.local/share/packrat/repos.sqlite`` via Python's built-in ``sqlite3``
in ``?mode=ro`` URI mode with a 2.0-second busy timeout so reviewer ranking needs
no external packages, spawns no ``duckdb`` subprocesses, and never blocks or
mutates ``packrat sync``.
"""

from __future__ import annotations

import contextlib
import json
import posixpath
import shlex
import sqlite3
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from reviewer_models import (
    PackratLoadResult,
    PackratReviewEvent,
    PackratThreadEvent,
    is_bot_identity,
    normalize_multi_token_name,
    parse_iso_timestamp_or_zero,
)

DEFAULT_PACKRAT_DB: Path = Path.home() / ".local" / "share" / "packrat" / "repos.sqlite"
DIRECTORY_EVENT_LIMIT: int = 200
MAX_DIRECT_FILE_BATCH: int = 400
VALID_REVIEW_STATES: frozenset[str] = frozenset({"APPROVED", "CHANGES_REQUESTED", "COMMENTED"})


def _clean_rel_path(path: str) -> str:
    """Normalizes a repository-relative path without leading ``./`` or slashes."""
    cleaned = (path or "").strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.strip("/")


def _unique_parent_dirs(file_paths: Sequence[str]) -> tuple[str, ...]:
    """Returns unique non-empty immediate parent directories in encounter order."""
    seen: dict[str, None] = {}
    for file_path in file_paths:
        parent = posixpath.dirname(file_path)
        if parent and parent != ".":
            seen.setdefault(parent, None)
    return tuple(seen)


def _format_path_arg(repo_dir: str | None) -> str:
    """Formats the ``--path`` argument for ``packrat repo add`` hints."""
    cleaned = (repo_dir or "").strip() or "."
    return shlex.quote(cleaned) if any(ch.isspace() for ch in cleaned) else cleaned


def _build_fallback_note(repo_full_name: str, repo_dir: str | None, reason: str) -> str:
    """Builds a single-line ``--repo``-scoped ``packrat`` setup hint."""
    path_arg = _format_path_arg(repo_dir)
    sync_cmd = (
        f"packrat repo add {repo_full_name} --path {path_arg} "
        f"&& packrat sync --repo {repo_full_name}"
    )
    return (
        f"Note: {reason}; using git-only signals. "
        f"Run `{sync_cmd}` to enable historical PR review signals."
    )


def _build_backfill_warning(repo_full_name: str) -> str:
    """Builds a single-line warning when ``prs`` backfill has not completed."""
    return (
        f"Note: packrat PR backfill for {repo_full_name} is incomplete; "
        f"run `packrat sync --repo {repo_full_name}` for full review history."
    )


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    """Lists all table names present in the SQLite database."""
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row["name"]) for row in rows}


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    """Returns column names for ``table_name`` via ``PRAGMA table_info``."""
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]) for row in rows}


def _load_repo_row(conn: sqlite3.Connection, repo_full_name: str) -> tuple[int, str, bool] | None:
    """Fetches ``(repo_id, canonical_full_name, prs_backfill_complete)``."""
    repo_cols = _table_columns(conn, "repos")
    if "repo_id" not in repo_cols or "full_name" not in repo_cols:
        raise sqlite3.OperationalError("Malformed repos table schema")
    row = conn.execute(
        "SELECT * FROM repos WHERE LOWER(full_name) = LOWER(?)",
        (repo_full_name,),
    ).fetchone()
    if row is None:
        return None
    repo_id = int(row["repo_id"])
    canonical_name = str(row["full_name"] or repo_full_name)
    backfill_done = False
    if "prs_backfill_done_at" in repo_cols:
        raw_done = row["prs_backfill_done_at"]
        backfill_done = bool(raw_done and str(raw_done).strip())
    return repo_id, canonical_name, backfill_done


def _query_target_pr_files(
    conn: sqlite3.Connection, repo_id: int, target_pr: int
) -> tuple[str, ...]:
    """Loads repository-relative file paths recorded for ``target_pr``."""
    rows = conn.execute(
        """
        SELECT file_path
        FROM pr_files
        WHERE repo_id = ? AND pr_number = ?
        ORDER BY file_path ASC
        """,
        (repo_id, target_pr),
    ).fetchall()
    cleaned = [_clean_rel_path(str(r["file_path"])) for r in rows]
    return tuple(dict.fromkeys(p for p in cleaned if p))


def _load_target_pr_metadata(
    conn: sqlite3.Connection,
    repo_id: int,
    target_pr: int | None,
    tables: set[str],
) -> tuple[str | None, str | None, str | None, tuple[str, ...]]:
    """Loads cached metadata and changed files for ``target_pr`` when available."""
    if target_pr is None:
        return None, None, None, ()
    author: str | None = None
    title: str | None = None
    base_ref: str | None = None
    if "pull_requests" in tables:
        pr_row = conn.execute(
            """
            SELECT author, title, base_ref
            FROM pull_requests
            WHERE repo_id = ? AND number = ?
            """,
            (repo_id, target_pr),
        ).fetchone()
        if pr_row is not None:
            author = str(pr_row["author"] or "").strip() or None
            title = str(pr_row["title"]) if pr_row["title"] is not None else None
            base_ref = str(pr_row["base_ref"] or "").strip() or None

    pr_files = _query_target_pr_files(conn, repo_id, target_pr) if "pr_files" in tables else ()
    return author, title, base_ref, pr_files


def _register_login_casing(canonical_by_lower: dict[str, str], raw_login: str | None) -> str | None:
    """Normalizes a non-bot GitHub login and records its first-seen casing."""
    cleaned = (raw_login or "").strip().lstrip("@")
    if not cleaned or is_bot_identity(cleaned):
        return None
    return canonical_by_lower.setdefault(cleaned.lower(), cleaned)


def _collect_table_logins(
    conn: sqlite3.Connection,
    repo_id: int,
    table_name: str,
    col_name: str,
    canonical_by_lower: dict[str, str],
) -> set[str]:
    """Collects distinct non-bot logins from ``table_name.col_name``."""
    rows = conn.execute(
        f"SELECT DISTINCT {col_name} AS login FROM {table_name} "
        f"WHERE repo_id = ? AND {col_name} IS NOT NULL",
        (repo_id,),
    ).fetchall()
    result: set[str] = set()
    for row in rows:
        canonical = _register_login_casing(canonical_by_lower, str(row["login"]))
        if canonical:
            result.add(canonical)
    return result


def _is_merge_commit_title(title: str | None) -> bool:
    """Returns whether ``title`` is a GitHub or git branch merge commit subject."""
    lowered = (title or "").strip().lower()
    return lowered.startswith(
        ("merge pull request #", "merge branch ", "merge remote-tracking branch ")
    )


def _record_candidate_login(
    candidates: dict[str, set[str]], key: str | None, login_lower: str
) -> None:
    """Records a candidate ``key -> login_lower`` association for ambiguity checks."""
    if key:
        candidates.setdefault(key, set()).add(login_lower)


def _finalize_unambiguous_map(
    candidates: dict[str, set[str]], canonical_by_lower: dict[str, str]
) -> dict[str, str]:
    """Keeps only keys that map to exactly one GitHub login."""
    return {
        key: canonical_by_lower[next(iter(logins))]
        for key, logins in candidates.items()
        if len(logins) == 1
    }


def _ingest_commit_identity_row(
    row: sqlite3.Row,
    canonical_by_lower: dict[str, str],
    known_logins: set[str],
    sha_candidates: dict[str, set[str]],
    email_candidates: dict[str, set[str]],
    name_candidates: dict[str, set[str]],
) -> None:
    """Processes one non-merge commit/PR join row into candidate identity maps."""
    if _is_merge_commit_title(row["title"]):
        return
    canonical = _register_login_casing(canonical_by_lower, row["pr_author"])
    if not canonical:
        return
    known_logins.add(canonical)
    login_lower = canonical.lower()
    sha_key = str(row["sha"] or "").strip().lower() or None
    _record_candidate_login(sha_candidates, sha_key, login_lower)
    raw_email = str(row["author_email"] or "").strip().lower()
    if raw_email and not is_bot_identity(raw_email):
        _record_candidate_login(email_candidates, raw_email, login_lower)
    raw_name = str(row["author_name"] or "").strip()
    if raw_name and not is_bot_identity(raw_name):
        _record_candidate_login(name_candidates, normalize_multi_token_name(raw_name), login_lower)


def _load_commit_identity_maps(
    conn: sqlite3.Connection,
    repo_id: int,
    tables: set[str],
    canonical_by_lower: dict[str, str],
    known_logins: set[str],
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Links non-merge ``commits`` to ``pull_requests.author`` unambiguously."""
    if "commits" not in tables or "pull_requests" not in tables:
        return {}, {}, {}
    rows = conn.execute(
        """
        SELECT c.sha, c.author_email, c.author_name, c.title, p.author AS pr_author
        FROM commits c
        JOIN pull_requests p ON p.repo_id = c.repo_id AND p.number = c.pr_number
        WHERE c.repo_id = ? AND p.author IS NOT NULL
        UNION
        SELECT c.sha, c.author_email, c.author_name, c.title, p.author AS pr_author
        FROM pull_requests p
        JOIN commits c ON c.repo_id = p.repo_id AND c.sha = p.head_sha
        WHERE p.repo_id = ? AND p.head_sha IS NOT NULL AND p.author IS NOT NULL
        UNION
        SELECT c.sha, c.author_email, c.author_name, c.title, p.author AS pr_author
        FROM pull_requests p
        JOIN commits c ON c.repo_id = p.repo_id AND c.sha = p.merge_commit_sha
        WHERE p.repo_id = ? AND p.merge_commit_sha IS NOT NULL AND p.author IS NOT NULL
        """,
        (repo_id, repo_id, repo_id),
    ).fetchall()

    sha_candidates: dict[str, set[str]] = {}
    email_candidates: dict[str, set[str]] = {}
    name_candidates: dict[str, set[str]] = {}
    for row in rows:
        _ingest_commit_identity_row(
            row,
            canonical_by_lower,
            known_logins,
            sha_candidates,
            email_candidates,
            name_candidates,
        )

    return (
        _finalize_unambiguous_map(sha_candidates, canonical_by_lower),
        _finalize_unambiguous_map(email_candidates, canonical_by_lower),
        _finalize_unambiguous_map(name_candidates, canonical_by_lower),
    )


def _row_to_review_event(
    row: sqlite3.Row,
    scope_path: str,
    is_directory: bool,
    canonical_by_lower: dict[str, str],
    known_logins: set[str],
) -> PackratReviewEvent | None:
    """Converts a joined review SQL row into a validated ``PackratReviewEvent``."""
    canonical = _register_login_casing(canonical_by_lower, row["reviewer_login"])
    state = str(row["state"] or "").strip().upper()
    if not canonical or state not in VALID_REVIEW_STATES:
        return None
    pr_author = str(row["pr_author"] or "").strip()
    if pr_author and canonical.lower() == pr_author.lower():
        return None
    known_logins.add(canonical)
    changed_count = max(1, int(row["pr_changed_files"] or 1))
    return PackratReviewEvent(
        pr_number=int(row["pr_number"]),
        reviewer_login=canonical,
        state=state,
        timestamp=parse_iso_timestamp_or_zero(row["event_time"]),
        scope_path=scope_path,
        is_directory=is_directory,
        pr_changed_files=changed_count,
    )


def _query_direct_reviews(
    conn: sqlite3.Connection,
    repo_id: int,
    effective_files: tuple[str, ...],
    target_pr: int | None,
    rev_col: str,
    canonical_by_lower: dict[str, str],
    known_logins: set[str],
) -> list[PackratReviewEvent]:
    """Queries formal PR reviews touching ``effective_files`` directly."""
    events: list[PackratReviewEvent] = []
    for start in range(0, len(effective_files), MAX_DIRECT_FILE_BATCH):
        batch = effective_files[start : start + MAX_DIRECT_FILE_BATCH]
        placeholders = ",".join("?" for _ in batch)
        params: list[object] = [repo_id, *batch]
        pr_filter = ""
        if target_pr is not None:
            pr_filter = "AND r.pr_number != ?"
            params.append(target_pr)
        query = f"""
            SELECT
                r.pr_number,
                r.{rev_col} AS reviewer_login,
                r.state,
                COALESCE(r.submitted_at, p.updated_at, p.created_at) AS event_time,
                pf.file_path,
                p.changed_files AS pr_changed_files,
                p.author AS pr_author
            FROM pr_files pf
            JOIN reviews r
                ON r.repo_id = pf.repo_id AND r.pr_number = pf.pr_number
            JOIN pull_requests p
                ON p.repo_id = r.repo_id AND p.number = r.pr_number
            WHERE pf.repo_id = ?
              AND pf.file_path IN ({placeholders})
              AND UPPER(r.state) IN ('APPROVED', 'CHANGES_REQUESTED', 'COMMENTED')
              AND r.{rev_col} IS NOT NULL
              AND (p.author IS NULL OR LOWER(r.{rev_col}) != LOWER(p.author))
              {pr_filter}
            ORDER BY r.pr_number DESC, r.review_id ASC, pf.file_path ASC
        """
        for row in conn.execute(query, params).fetchall():
            event = _row_to_review_event(
                row, str(row["file_path"]), False, canonical_by_lower, known_logins
            )
            if event is not None:
                events.append(event)
    return events


def _build_sibling_sql_filters(
    repo_id: int,
    dirpath: str,
    effective_files: tuple[str, ...],
    path_col: str,
    target_pr: int | None,
    pr_col: str,
) -> tuple[str, str, list[object]]:
    """Builds SQL clauses and bind parameters for immediate sibling file queries."""
    prefix = f"{dirpath}/"
    upper = f"{dirpath}0"
    dir_excluded = [f for f in effective_files if posixpath.dirname(f) == dirpath]
    excl_clause = ""
    params: list[object] = [repo_id, prefix, upper, prefix]
    if dir_excluded:
        placeholders = ",".join("?" for _ in dir_excluded)
        excl_clause = f"AND {path_col} NOT IN ({placeholders})"
        params.extend(dir_excluded)
    pr_filter = ""
    if target_pr is not None:
        pr_filter = f"AND {pr_col} != ?"
        params.append(target_pr)
    params.append(DIRECTORY_EVENT_LIMIT)
    return excl_clause, pr_filter, params


def _query_directory_reviews(
    conn: sqlite3.Connection,
    repo_id: int,
    parent_dirs: tuple[str, ...],
    effective_files: tuple[str, ...],
    target_pr: int | None,
    rev_col: str,
    canonical_by_lower: dict[str, str],
    known_logins: set[str],
) -> list[PackratReviewEvent]:
    """Queries formal PR reviews on immediate sibling files in ``parent_dirs``."""
    events: list[PackratReviewEvent] = []
    for dirpath in parent_dirs:
        excl_clause, pr_filter, params = _build_sibling_sql_filters(
            repo_id, dirpath, effective_files, "pf.file_path", target_pr, "r.pr_number"
        )
        query = f"""
            SELECT DISTINCT
                r.review_id,
                r.pr_number,
                r.{rev_col} AS reviewer_login,
                r.state,
                COALESCE(r.submitted_at, p.updated_at, p.created_at) AS event_time,
                p.changed_files AS pr_changed_files,
                p.author AS pr_author
            FROM pr_files pf
            JOIN reviews r
                ON r.repo_id = pf.repo_id AND r.pr_number = pf.pr_number
            JOIN pull_requests p
                ON p.repo_id = r.repo_id AND p.number = r.pr_number
            WHERE pf.repo_id = ?
              AND pf.file_path >= ?
              AND pf.file_path < ?
              AND INSTR(SUBSTR(pf.file_path, LENGTH(?) + 1), '/') = 0
              {excl_clause}
              AND UPPER(r.state) IN ('APPROVED', 'CHANGES_REQUESTED', 'COMMENTED')
              AND r.{rev_col} IS NOT NULL
              AND (p.author IS NULL OR LOWER(r.{rev_col}) != LOWER(p.author))
              {pr_filter}
            ORDER BY event_time DESC, r.pr_number DESC, r.review_id ASC
            LIMIT ?
        """
        for row in conn.execute(query, params).fetchall():
            event = _row_to_review_event(row, dirpath, True, canonical_by_lower, known_logins)
            if event is not None:
                events.append(event)
    return events


def _extract_comment_author_login(node: object) -> str | None:
    """Extracts ``author.login`` safely from a ``comments_json`` element."""
    if not isinstance(node, dict):
        return None
    author_val = node.get("author")
    if isinstance(author_val, dict):
        login = author_val.get("login")
        return login.strip() if isinstance(login, str) and login.strip() else None
    if isinstance(author_val, str) and author_val.strip():
        return author_val.strip()
    return None


def _parse_comments_json_logins(comments_json: str | None) -> list[str]:
    """Parses author logins from ``review_threads.comments_json`` safely."""
    if not comments_json:
        return []
    try:
        parsed = json.loads(comments_json)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    logins: list[str] = []
    for node in parsed:
        login = _extract_comment_author_login(node)
        if login:
            logins.append(login)
    return logins


def _extract_thread_logins(
    reviewer: str | None, comments_json: str | None, pr_author: str | None
) -> list[str]:
    """Extracts unique non-bot, non-PR-author logins from a review thread row."""
    raw_candidates: list[str] = []
    if reviewer and reviewer.strip():
        raw_candidates.append(reviewer.strip())
    raw_candidates.extend(_parse_comments_json_logins(comments_json))

    pr_author_lower = pr_author.strip().lower() if pr_author else ""
    deduped: list[str] = []
    seen_lower: set[str] = set()
    for candidate in raw_candidates:
        cleaned = candidate.strip().lstrip("@")
        lowered = cleaned.lower()
        if (
            not cleaned
            or lowered in seen_lower
            or lowered == pr_author_lower
            or is_bot_identity(cleaned)
        ):
            continue
        seen_lower.add(lowered)
        deduped.append(cleaned)
    return deduped


def _thread_row_to_events(
    row: sqlite3.Row,
    is_directory: bool,
    dir_override: str | None,
    seen_keys: set[tuple[str, str, str, bool]],
    canonical_by_lower: dict[str, str],
    known_logins: set[str],
) -> list[PackratThreadEvent]:
    """Converts a single ``review_threads`` SQL row into thread events."""
    thread_id = str(row["thread_id"] or "").strip()
    scope_path = dir_override if is_directory else _clean_rel_path(str(row["file_path"] or ""))
    if not thread_id or not scope_path:
        return []
    timestamp = parse_iso_timestamp_or_zero(row["event_time"])
    pr_number = int(row["pr_number"])
    events: list[PackratThreadEvent] = []
    for raw_login in _extract_thread_logins(
        row["reviewer"], row["comments_json"], row["pr_author"]
    ):
        canonical = _register_login_casing(canonical_by_lower, raw_login)
        if not canonical:
            continue
        dedup_key = (thread_id, canonical.lower(), scope_path, is_directory)
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        known_logins.add(canonical)
        events.append(
            PackratThreadEvent(
                thread_id=thread_id,
                pr_number=pr_number,
                reviewer_login=canonical,
                timestamp=timestamp,
                scope_path=scope_path,
                is_directory=is_directory,
            )
        )
    return events


def _rows_to_thread_events(
    rows: Iterable[sqlite3.Row],
    is_directory: bool,
    dir_override: str | None,
    seen_keys: set[tuple[str, str, str, bool]],
    canonical_by_lower: dict[str, str],
    known_logins: set[str],
) -> list[PackratThreadEvent]:
    """Converts ``review_threads`` SQL rows into deduplicated ``PackratThreadEvent``s."""
    events: list[PackratThreadEvent] = []
    for row in rows:
        events.extend(
            _thread_row_to_events(
                row,
                is_directory,
                dir_override,
                seen_keys,
                canonical_by_lower,
                known_logins,
            )
        )
    return events


def _query_direct_threads(
    conn: sqlite3.Connection,
    repo_id: int,
    effective_files: tuple[str, ...],
    target_pr: int | None,
    seen_keys: set[tuple[str, str, str, bool]],
    canonical_by_lower: dict[str, str],
    known_logins: set[str],
) -> list[PackratThreadEvent]:
    """Queries inline review threads directly on ``effective_files``."""
    events: list[PackratThreadEvent] = []
    for start in range(0, len(effective_files), MAX_DIRECT_FILE_BATCH):
        batch = effective_files[start : start + MAX_DIRECT_FILE_BATCH]
        placeholders = ",".join("?" for _ in batch)
        params: list[object] = [repo_id, *batch]
        pr_filter = ""
        if target_pr is not None:
            pr_filter = "AND rt.pr_number != ?"
            params.append(target_pr)
        query = f"""
            SELECT
                rt.thread_id,
                rt.pr_number,
                rt.file_path,
                rt.reviewer,
                rt.comments_json,
                COALESCE(rt.updated_at, rt.created_at, p.updated_at, p.created_at) AS event_time,
                p.author AS pr_author
            FROM review_threads rt
            JOIN pull_requests p
                ON p.repo_id = rt.repo_id AND p.number = rt.pr_number
            WHERE rt.repo_id = ?
              AND rt.file_path IN ({placeholders})
              {pr_filter}
            ORDER BY rt.pr_number DESC, rt.thread_id ASC
        """
        rows = conn.execute(query, params).fetchall()
        events.extend(
            _rows_to_thread_events(rows, False, None, seen_keys, canonical_by_lower, known_logins)
        )
    return events


def _query_directory_threads(
    conn: sqlite3.Connection,
    repo_id: int,
    parent_dirs: tuple[str, ...],
    effective_files: tuple[str, ...],
    target_pr: int | None,
    seen_keys: set[tuple[str, str, str, bool]],
    canonical_by_lower: dict[str, str],
    known_logins: set[str],
) -> list[PackratThreadEvent]:
    """Queries inline review threads on immediate sibling files in ``parent_dirs``."""
    events: list[PackratThreadEvent] = []
    for dirpath in parent_dirs:
        excl_clause, pr_filter, params = _build_sibling_sql_filters(
            repo_id, dirpath, effective_files, "rt.file_path", target_pr, "rt.pr_number"
        )
        query = f"""
            SELECT
                rt.thread_id,
                rt.pr_number,
                rt.file_path,
                rt.reviewer,
                rt.comments_json,
                COALESCE(rt.updated_at, rt.created_at, p.updated_at, p.created_at) AS event_time,
                p.author AS pr_author
            FROM review_threads rt
            JOIN pull_requests p
                ON p.repo_id = rt.repo_id AND p.number = rt.pr_number
            WHERE rt.repo_id = ?
              AND rt.file_path >= ?
              AND rt.file_path < ?
              AND INSTR(SUBSTR(rt.file_path, LENGTH(?) + 1), '/') = 0
              {excl_clause}
              {pr_filter}
            ORDER BY event_time DESC, rt.pr_number DESC, rt.thread_id ASC
            LIMIT ?
        """
        rows = conn.execute(query, params).fetchall()
        events.extend(
            _rows_to_thread_events(rows, True, dirpath, seen_keys, canonical_by_lower, known_logins)
        )
    return events


def _collect_all_known_logins(
    conn: sqlite3.Connection,
    repo_id: int,
    tables: set[str],
    rev_col: str | None,
    canonical_by_lower: dict[str, str],
) -> set[str]:
    """Collects known logins across pull requests, reviews, and review threads."""
    known: set[str] = set()
    if "pull_requests" in tables:
        known |= _collect_table_logins(conn, repo_id, "pull_requests", "author", canonical_by_lower)
    if rev_col and "reviews" in tables:
        known |= _collect_table_logins(conn, repo_id, "reviews", rev_col, canonical_by_lower)
    if "review_threads" in tables:
        known |= _collect_table_logins(
            conn, repo_id, "review_threads", "reviewer", canonical_by_lower
        )
    return known


def _resolve_reviews_author_column(conn: sqlite3.Connection, tables: set[str]) -> str | None:
    """Detects the reviewer handle column on ``reviews`` (``author`` in packrat DDL)."""
    if "reviews" not in tables:
        return None
    cols = _table_columns(conn, "reviews")
    if "author" in cols:
        return "author"
    return "reviewer" if "reviewer" in cols else None


def _query_packrat_db(
    conn: sqlite3.Connection,
    clean_repo: str,
    normalized_changed: tuple[str, ...],
    target_pr: int | None,
    repo_dir: str | None,
) -> PackratLoadResult:
    """Executes read-only queries against an open ``repos.sqlite`` connection."""
    tables = _existing_tables(conn)
    if "repos" not in tables:
        raise sqlite3.OperationalError("Missing repos table")
    repo_info = _load_repo_row(conn, clean_repo)
    if repo_info is None:
        return PackratLoadResult(
            status="untracked_repo",
            repo_full_name=clean_repo,
            fallback_note=_build_fallback_note(
                clean_repo, repo_dir, f"{clean_repo} is not tracked in packrat"
            ),
        )
    repo_id, canonical_repo, backfill_complete = repo_info
    pr_author, pr_title, pr_base_ref, pr_files = _load_target_pr_metadata(
        conn, repo_id, target_pr, tables
    )
    effective_files = normalized_changed or pr_files
    parent_dirs = _unique_parent_dirs(effective_files)

    rev_col = _resolve_reviews_author_column(conn, tables)
    canonical_by_lower: dict[str, str] = {}
    known_logins = _collect_all_known_logins(conn, repo_id, tables, rev_col, canonical_by_lower)
    sha_map, email_map, name_map = _load_commit_identity_maps(
        conn, repo_id, tables, canonical_by_lower, known_logins
    )

    review_events: list[PackratReviewEvent] = []
    if effective_files and rev_col and {"pr_files", "pull_requests"} <= tables:
        review_events.extend(
            _query_direct_reviews(
                conn,
                repo_id,
                effective_files,
                target_pr,
                rev_col,
                canonical_by_lower,
                known_logins,
            )
        )
        if parent_dirs:
            review_events.extend(
                _query_directory_reviews(
                    conn,
                    repo_id,
                    parent_dirs,
                    effective_files,
                    target_pr,
                    rev_col,
                    canonical_by_lower,
                    known_logins,
                )
            )

    thread_events: list[PackratThreadEvent] = []
    if effective_files and {"review_threads", "pull_requests"} <= tables:
        seen_threads: set[tuple[str, str, str, bool]] = set()
        thread_events.extend(
            _query_direct_threads(
                conn,
                repo_id,
                effective_files,
                target_pr,
                seen_threads,
                canonical_by_lower,
                known_logins,
            )
        )
        if parent_dirs:
            thread_events.extend(
                _query_directory_threads(
                    conn,
                    repo_id,
                    parent_dirs,
                    effective_files,
                    target_pr,
                    seen_threads,
                    canonical_by_lower,
                    known_logins,
                )
            )

    return PackratLoadResult(
        status="ok",
        repo_full_name=canonical_repo,
        prs_backfill_complete=backfill_complete,
        fallback_note=None,
        backfill_warning=(None if backfill_complete else _build_backfill_warning(canonical_repo)),
        target_pr_author=pr_author,
        target_pr_title=pr_title,
        target_pr_base_ref=pr_base_ref,
        target_pr_files=pr_files,
        known_logins=frozenset(known_logins),
        sha_to_login=sha_map,
        email_to_login=email_map,
        name_to_login=name_map,
        review_events=tuple(review_events),
        thread_events=tuple(thread_events),
    )


def load_packrat_signals(
    repo_full_name: str | None,
    changed_files: Sequence[str],
    *,
    db_path: Path = DEFAULT_PACKRAT_DB,
    target_pr: int | None = None,
    repo_dir: str | None = None,
) -> PackratLoadResult:
    """Loads historical review, thread, and identity signals from ``repos.sqlite``.

    Opens ``db_path`` strictly in read-only URI mode (``?mode=ro``) after checking
    ``db_path.is_file()`` so missing databases never create empty files on disk.

    Args:
        repo_full_name: Canonical GitHub ``owner/name`` repository identifier, or
            None when the target directory has no GitHub remote.
        changed_files: Repository-relative paths modified by the target change.
        db_path: Path to ``packrat``'s ``repos.sqlite`` database.
        target_pr: Optional pull request number being analyzed (excluded from
            historical review events and used for offline ``pr_files`` lookup).
        repo_dir: Optional repository working directory path for sync hints.

    Returns:
        ``PackratLoadResult`` describing status, fallback/backfill notes, cached
        target PR metadata, unambiguous identity maps, and review/thread events.
    """
    clean_repo = (repo_full_name or "").strip()
    if not clean_repo:
        return PackratLoadResult(status="no_github_repo", fallback_note=None)

    resolved_db = Path(db_path)
    try:
        exists = resolved_db.is_file()
    except OSError:
        return PackratLoadResult(
            status="unreadable_db",
            repo_full_name=clean_repo,
            fallback_note=_build_fallback_note(
                clean_repo, repo_dir, "packrat database could not be read"
            ),
        )
    if not exists:
        return PackratLoadResult(
            status="missing_db",
            repo_full_name=clean_repo,
            fallback_note=_build_fallback_note(clean_repo, repo_dir, "packrat database not found"),
        )

    cleaned_paths = [_clean_rel_path(path) for path in changed_files]
    normalized_changed = tuple(dict.fromkeys(p for p in cleaned_paths if p))
    try:
        uri = f"{resolved_db.resolve().as_uri()}?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=2.0)) as conn:
            conn.row_factory = sqlite3.Row
            return _query_packrat_db(conn, clean_repo, normalized_changed, target_pr, repo_dir)
    except (sqlite3.Error, OSError):
        return PackratLoadResult(
            status="unreadable_db",
            repo_full_name=clean_repo,
            fallback_note=_build_fallback_note(
                clean_repo, repo_dir, "packrat database could not be read"
            ),
        )
