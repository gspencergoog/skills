"""Parameterized batch reader engine shared by `packrat reader` and `/api/v1/reader/batch`.

Executes typed reader queries (`health`, `viewer_items`, `items`, `known_keys`,
`stack_neighbors`, `repo_probe`, `context`, `review_signals`) against
`PackratStore` and optional `PersonalStore`, excluding `pending_delete`
repositories across every operation.
"""

from __future__ import annotations

import json
import posixpath
import re
import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

try:
    from .db_store import PackratStore
    from .models import BACKFILL_STREAMS, RepoConfig, parse_item_key
    from .personal_store import PersonalStore
except ImportError:
    from db_store import PackratStore  # type: ignore[no-redef]
    from models import (  # type: ignore[no-redef]
        BACKFILL_STREAMS,
        RepoConfig,
        parse_item_key,
    )
    from personal_store import PersonalStore  # type: ignore[no-redef]

_BASE_BRANCHES: frozenset[str] = frozenset({"main", "master", "develop"})
_MAX_STACK_DEPTH = 10
_SQL_BATCH_SIZE = 400
_DIR_EVENT_LIMIT = 200
_VALID_REVIEW_STATES: frozenset[str] = frozenset(
    {"APPROVED", "CHANGES_REQUESTED", "COMMENTED"}
)
_CLOSING_UNTRUSTED_RE = re.compile(
    r"</\s*untrusted_repo_content\b[^>]*>", re.IGNORECASE
)
_GH_PR_URL_RE = re.compile(
    r"^https?://[^/]+/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([0-9]+)(?:[/?#].*)?$"
)
_BOT_TOKENS = (
    "[bot]",
    "-bot",
    "_bot",
    "dependabot",
    "renovate",
    "github-actions",
    "codecov",
    "gemini-code-assist",
    "copilot",
)


def _batched(
    items: Sequence[str], size: int = _SQL_BATCH_SIZE
) -> Iterable[Sequence[str]]:
    for idx in range(0, len(items), size):
        yield items[idx : idx + size]


def _clean_rel_path(path: str) -> str:
    cleaned = (path or "").strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.strip("/")


def _wrap_snippet(source: str, text: str, max_chars: int = 160) -> str:
    safe_src = (
        source.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    collapsed = re.sub(r"\s+", " ", text or "").strip()
    sanitized = _CLOSING_UNTRUSTED_RE.sub("&lt;/untrusted_repo_content&gt;", collapsed)
    if max_chars > 0 and len(sanitized) > max_chars:
        sanitized = (
            sanitized[: max_chars - 3].rstrip() + "..."
            if max_chars > 3
            else sanitized[:max_chars]
        )
    sanitized = _CLOSING_UNTRUSTED_RE.sub("&lt;/untrusted_repo_content&gt;", sanitized)
    return f'<untrusted_repo_content source="{safe_src}">{sanitized}</untrusted_repo_content>'


def _parse_iso_epoch(raw: str | None) -> float | None:
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    candidate = f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _is_bot_identity(value: str | None) -> bool:
    cleaned = (value or "").strip().lower()
    if not cleaned:
        return False
    return any(tok in cleaned for tok in _BOT_TOKENS)


def _normalize_multi_token_name(raw_name: str | None) -> str | None:
    tokens = [t for t in re.split(r"\s+", (raw_name or "").strip().lower()) if t]
    if len(tokens) < 2:
        return None
    return " ".join(tokens)


def _resolve_effective_viewer(
    store: PackratStore,
    personal_store: PersonalStore | None,
    owner_id: str,
    viewer_login: str,
) -> str:
    if viewer_login.strip():
        return viewer_login.strip().lstrip("@")
    if personal_store is not None:
        ident = personal_store.get_identity(owner_id)
        if ident is not None and ident.github_login:
            return ident.github_login
        return ""
    return store.get_state("viewer_login") or ""


# ----- 1. health -----------------------------------------------------------------


def _latest_rate_pair(
    conn: sqlite3.Connection, resource: str
) -> tuple[int | None, int | None]:
    row = conn.execute(
        "SELECT remaining, reset_at FROM api_calls "
        "WHERE resource = ? AND remaining IS NOT NULL ORDER BY rowid DESC LIMIT 1",
        (resource,),
    ).fetchone()
    if row is None:
        return None, None
    rem = int(row["remaining"])
    rst = int(row["reset_at"]) if row["reset_at"] is not None else None
    return rem, rst


def _heartbeat_fields(hb: Any, moment: float) -> dict[str, Any]:
    if hb is None:
        return {
            "pid": None,
            "started_at": None,
            "beat_at": None,
            "version": None,
            "beat_age_s": None,
            "last_ok_at": None,
            "consecutive_failed_ticks": 0,
        }
    beat_epoch = _parse_iso_epoch(hb.beat_at) if hb.beat_at else None
    beat_age = round(moment - beat_epoch) if beat_epoch is not None else None
    return {
        "pid": hb.pid,
        "started_at": hb.started_at,
        "beat_at": hb.beat_at,
        "version": hb.version,
        "beat_age_s": beat_age,
        "last_ok_at": hb.last_ok_at,
        "consecutive_failed_ticks": hb.consecutive_failed_ticks,
    }


def _op_health(
    store: PackratStore,
    personal_store: PersonalStore | None,
    owner_id: str,
    viewer_login: str,
    moment: float,
) -> dict[str, Any]:
    hb_fields = _heartbeat_fields(store.read_heartbeat(), moment)
    gql_rem, gql_rst = _latest_rate_pair(store.conn, "graphql")
    core_rem, core_rst = _latest_rate_pair(store.conn, "core")
    active_leases = (
        len(personal_store.active_watches(moment, owner_id=owner_id))
        if personal_store is not None
        else len(store.active_watches(moment))
    )
    resolved_viewer = _resolve_effective_viewer(
        store, personal_store, owner_id, viewer_login
    )
    sync_times = (
        personal_store.viewer_set_sync_times(resolved_viewer)
        if personal_store is not None
        else store.viewer_set_sync_times()
    )
    return {
        **hb_fields,
        "graphql_remaining": gql_rem,
        "graphql_reset_at": gql_rst,
        "core_remaining": core_rem,
        "core_reset_at": core_rst,
        "active_leases": active_leases,
        "viewer_login": resolved_viewer or None,
        "viewer_sets_synced_at": sync_times,
    }


# ----- 2. viewer_items -----------------------------------------------------------


def _load_pr_meta_map(
    conn: sqlite3.Connection, pairs: Sequence[tuple[int, int]]
) -> dict[tuple[int, int], tuple[str | None, str | None]]:
    if not pairs:
        return {}
    uniq = list(dict.fromkeys(pairs))
    vals = ", ".join("(?, ?)" for _ in uniq)
    params = [v for p in uniq for v in p]
    rows = conn.execute(
        f"SELECT repo_id, number, state, updated_at FROM pull_requests "
        f"WHERE (repo_id, number) IN (VALUES {vals})",
        params,
    ).fetchall()
    return {
        (int(r["repo_id"]), int(r["number"])): (r["state"], r["updated_at"])
        for r in rows
    }


def _enrich_personal_viewer_rows(
    store: PackratStore, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not rows:
        return []
    repo_cache: dict[str, RepoConfig | None] = {}
    pairs_to_fetch: list[tuple[int, int]] = []
    resolved_entries: list[tuple[dict[str, Any], RepoConfig]] = []
    for row in rows:
        rname = str(row["repo_full_name"])
        repo = repo_cache.setdefault(rname.lower(), store.get_repo(rname))
        if repo is None:
            continue
        pairs_to_fetch.append((repo.repo_id, int(row["item_number"])))
        resolved_entries.append((row, repo))
    pr_meta = _load_pr_meta_map(store.conn, pairs_to_fetch)
    out: list[dict[str, Any]] = []
    for row, repo in resolved_entries:
        num = int(row["item_number"])
        state, updated_at = pr_meta.get((repo.repo_id, num), (None, None))
        out.append(
            {
                "set_name": str(row["set_name"]),
                "item_key": f"{repo.full_name}#{num}",
                "first_seen_at": str(row["first_seen_at"]),
                "last_seen_at": str(row["last_seen_at"]),
                "state": state,
                "updated_at": updated_at,
            }
        )
    return out


def _op_viewer_items(
    store: PackratStore,
    personal_store: PersonalStore | None,
    owner_id: str,
    viewer_login: str,
    spec: Any,
) -> list[dict[str, Any]]:
    opts = spec if isinstance(spec, Mapping) else {}
    sets = [str(s) for s in (opts.get("sets") or []) if str(s)]
    repo_filter = str(opts.get("repo") or "").strip()
    if personal_store is not None:
        effective = _resolve_effective_viewer(
            store, personal_store, owner_id, viewer_login
        )
        raw_rows = personal_store.viewer_items_for_login(
            effective, sets=sets or None, repo=repo_filter or None
        )
        return _enrich_personal_viewer_rows(store, raw_rows)
    clauses = [
        "NOT EXISTS (SELECT 1 FROM service_state s WHERE s.key = 'pending_delete:' || r.repo_id)"
    ]
    params: list[Any] = []
    if sets:
        marks = ",".join("?" for _ in sets)
        clauses.append(f"v.set_name IN ({marks})")
        params.extend(sets)
    if repo_filter:
        clauses.append("lower(r.full_name) = lower(?)")
        params.append(repo_filter)
    rows = store.conn.execute(
        f"""
        SELECT v.set_name, r.full_name || '#' || v.item_number AS item_key,
               v.first_seen_at, v.last_seen_at,
               p.state AS state, p.updated_at AS updated_at
        FROM viewer_sets v
        JOIN repos r ON r.repo_id = v.repo_id
        LEFT JOIN pull_requests p ON p.repo_id = v.repo_id AND p.number = v.item_number
        WHERE {" AND ".join(clauses)}
        ORDER BY v.set_name ASC, item_key ASC
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


# ----- 3. items & known_keys & stack_neighbors -----------------------------------


def _resolve_requested_pairs(
    store: PackratStore, raw_keys: Sequence[str]
) -> tuple[list[tuple[int, int]], dict[str, str]]:
    """Maps requested `owner/name#N` keys to active `(repo_id, number)` pairs."""
    repo_cache: dict[str, RepoConfig | None] = {}
    pairs: list[tuple[int, int]] = []
    req_to_canon: dict[str, str] = {}
    for raw in raw_keys:
        try:
            owner, name, num = parse_item_key(str(raw))
        except ValueError:
            continue
        slug = f"{owner}/{name}"
        low = slug.lower()
        if low not in repo_cache:
            repo_cache[low] = store.get_repo(slug)
        repo = repo_cache[low]
        if repo is None:
            continue
        pair = (repo.repo_id, num)
        if pair not in pairs:
            pairs.append(pair)
        req_to_canon[str(raw)] = f"{repo.full_name}#{num}"
    return pairs, req_to_canon


def _query_pair_rows(
    conn: sqlite3.Connection, sql_tpl: str, pairs: Sequence[tuple[int, int]]
) -> list[dict[str, Any]]:
    if not pairs:
        return []
    vals = ", ".join("(?, ?)" for _ in pairs)
    params = [v for pair in pairs for v in pair]
    return [dict(r) for r in conn.execute(sql_tpl.format(vals=vals), params).fetchall()]


def _query_live_bundle(
    conn: sqlite3.Connection, pairs: Sequence[tuple[int, int]], moment: float
) -> dict[str, list[dict[str, Any]]]:
    pr_live = _query_pair_rows(
        conn,
        """
        SELECT r.full_name || '#' || p.number AS item_key,
               r.full_name AS repo_full_name, r.mode AS repo_mode,
               p.number, p.node_id, p.title, p.url, p.state, p.is_draft,
               p.author, p.author_type, p.base_ref, p.head_ref, p.head_sha,
               p.head_committed_date, p.head_repo_owner, p.is_cross_repository,
               p.mergeable, p.merge_state_status, p.review_decision,
               p.auto_merge_enabled_at, p.created_at, p.updated_at,
               p.merged_at, p.closed_at, p.live_fetched_at,
               ro.state AS rollup_state, ro.total AS rollup_total,
               ro.fetched_at AS rollup_fetched_at
        FROM pull_requests p
        JOIN repos r ON r.repo_id = p.repo_id
        LEFT JOIN pr_rollup ro
          ON ro.repo_id = p.repo_id AND ro.pr_number = p.number AND ro.head_oid = p.head_sha
        WHERE p.live_fetched_at IS NOT NULL AND (p.repo_id, p.number) IN (VALUES {vals})
        """,
        pairs,
    )
    for row in pr_live:
        fetched_epoch = _parse_iso_epoch(row.get("live_fetched_at"))
        row["age_s"] = (
            round(max(0.0, moment - fetched_epoch), 3)
            if fetched_epoch is not None
            else 0.0
        )
    checks = _query_pair_rows(
        conn,
        """
        SELECT r.full_name || '#' || p.number AS item_key,
               c.head_oid, c.run_key, c.kind, c.name, c.status, c.conclusion,
               c.details_url, c.started_at, c.completed_at, c.is_required, c.workflow_run_id
        FROM check_runs c
        JOIN pull_requests p
          ON p.repo_id = c.repo_id AND p.number = c.pr_number AND p.head_sha = c.head_oid
        JOIN repos r ON r.repo_id = c.repo_id
        WHERE (p.repo_id, p.number) IN (VALUES {vals})
        """,
        pairs,
    )
    requests = _query_pair_rows(
        conn,
        """
        SELECT r.full_name || '#' || q.pr_number AS item_key,
               q.reviewer, q.reviewer_kind, q.requested_at
        FROM review_requests q
        JOIN repos r ON r.repo_id = q.repo_id
        WHERE (q.repo_id, q.pr_number) IN (VALUES {vals})
        """,
        pairs,
    )
    reviews = _query_pair_rows(
        conn,
        """
        SELECT r.full_name || '#' || o.pr_number AS item_key,
               o.author, o.author_type, o.state, o.submitted_at
        FROM latest_opinionated_reviews o
        JOIN repos r ON r.repo_id = o.repo_id
        WHERE (o.repo_id, o.pr_number) IN (VALUES {vals})
        """,
        pairs,
    )
    author_reviews = _query_pair_rows(
        conn,
        """
        SELECT r.full_name || '#' || lr.pr_number AS item_key,
               r.full_name AS repo_full_name, lr.pr_number, lr.author,
               lr.author_type, lr.state, lr.submitted_at, lr.commit_oid
        FROM latest_reviews lr
        JOIN repos r ON r.repo_id = lr.repo_id
        WHERE (lr.repo_id, lr.pr_number) IN (VALUES {vals})
        """,
        pairs,
    )
    threads = _query_pair_rows(
        conn,
        """
        SELECT r.full_name || '#' || t.pr_number AS item_key,
               t.thread_id, t.is_resolved, t.is_outdated,
               t.last_author, t.last_author_type, t.last_at, t.participants_json
        FROM thread_activity t
        JOIN repos r ON r.repo_id = t.repo_id
        WHERE (t.repo_id, t.pr_number) IN (VALUES {vals})
        """,
        pairs,
    )
    return {
        "pr_live": pr_live,
        "checks": checks,
        "review_requests": requests,
        "latest_reviews": reviews,
        "latest_author_reviews": author_reviews,
        "threads": threads,
    }


def _query_item_notifications(
    conn: sqlite3.Connection,
    personal_store: PersonalStore | None,
    owner_id: str,
    canon_keys: Sequence[str],
) -> list[dict[str, Any]]:
    if personal_store is not None:
        return personal_store.notifications_for_owner(owner_id, item_keys=canon_keys)
    if not canon_keys:
        return []
    marks = ",".join("?" for _ in canon_keys)
    rows = conn.execute(
        f"SELECT thread_id, reason, subject_type, item_key, updated_at, unread, seen_at "
        f"FROM notifications WHERE lower(item_key) IN ({marks})",
        [k.lower() for k in canon_keys],
    ).fetchall()
    return [dict(r) for r in rows]


def _empty_items_bundle() -> dict[str, Any]:
    return {
        "pr_live": [],
        "checks": [],
        "review_requests": [],
        "latest_reviews": [],
        "latest_author_reviews": [],
        "threads": [],
        "comments": [],
        "pr_basic": [],
        "issue_basic": [],
        "notifications": [],
    }


def _op_items(
    store: PackratStore,
    personal_store: PersonalStore | None,
    owner_id: str,
    moment: float,
    spec: Any,
) -> dict[str, Any]:
    opts = spec if isinstance(spec, Mapping) else {}
    raw_keys = [str(k) for k in (opts.get("keys") or []) if str(k)]
    pairs, req_to_canon = _resolve_requested_pairs(store, raw_keys)
    bundle = _empty_items_bundle()
    if not pairs:
        return bundle
    conn = store.conn
    if bool(opts.get("include_live", True)):
        bundle.update(_query_live_bundle(conn, pairs, moment))
    bundle["comments"] = _query_pair_rows(
        conn,
        """
        SELECT item_key, kind, comment_id, author, author_type, created_at
        FROM (
          SELECT r.full_name || '#' || c.parent_number AS item_key,
                 c.parent_kind AS kind, c.comment_id, c.author, c.author_type, c.created_at,
                 ROW_NUMBER() OVER (
                   PARTITION BY c.repo_id, c.parent_kind, c.parent_number
                   ORDER BY c.created_at DESC, c.comment_id DESC
                 ) AS rn
          FROM comments c
          JOIN repos r ON r.repo_id = c.repo_id
          WHERE c.parent_kind IN ('pr', 'issue')
            AND (c.repo_id, c.parent_number) IN (VALUES {vals})
        )
        WHERE rn <= 10
        ORDER BY created_at ASC
        """,
        pairs,
    )
    if bool(opts.get("include_basic", True)):
        bundle["pr_basic"] = _query_pair_rows(
            conn,
            """
            SELECT r.full_name || '#' || p.number AS item_key,
                   r.full_name AS repo_full_name, r.mode AS repo_mode,
                   p.number, p.title, p.state, p.is_draft, p.url, p.author,
                   p.created_at, p.updated_at, p.merged_at, p.closed_at,
                   p.base_ref, p.head_ref, p.head_sha, p.head_repo_owner,
                   p.is_cross_repository, p.body
            FROM pull_requests p
            JOIN repos r ON r.repo_id = p.repo_id
            WHERE (p.repo_id, p.number) IN (VALUES {vals})
            """,
            pairs,
        )
    if bool(opts.get("include_issues", True)):
        bundle["issue_basic"] = _query_pair_rows(
            conn,
            """
            SELECT r.full_name || '#' || i.number AS item_key,
                   r.full_name AS repo_full_name, r.mode AS repo_mode,
                   i.number, i.title, i.state, i.url, i.author,
                   i.labels_json, i.assignees_json,
                   i.created_at, i.updated_at, i.closed_at, i.body
            FROM issues i
            JOIN repos r ON r.repo_id = i.repo_id
            WHERE (i.repo_id, i.number) IN (VALUES {vals})
            """,
            pairs,
        )
    if bool(opts.get("include_mentions", True)):
        canon_keys = list(dict.fromkeys(req_to_canon.values()))
        bundle["notifications"] = _query_item_notifications(
            conn, personal_store, owner_id, canon_keys
        )
    return bundle


def _op_known_keys(store: PackratStore, spec: Any) -> dict[str, str]:
    opts = spec if isinstance(spec, Mapping) else {}
    raw_keys = [str(k) for k in (opts.get("keys") or []) if str(k)]
    pairs, _ = _resolve_requested_pairs(store, raw_keys)
    if not pairs:
        return {}
    rows = _query_pair_rows(
        store.conn,
        """
        SELECT p.repo_id, p.number, p.url, r.full_name AS repo_full_name
        FROM pull_requests p
        JOIN repos r ON r.repo_id = p.repo_id
        WHERE (p.repo_id, p.number) IN (VALUES {vals})
        """,
        pairs,
    )
    by_pair: dict[tuple[int, int], str] = {}
    for row in rows:
        pair = (int(row["repo_id"]), int(row["number"]))
        url = str(row.get("url") or "").strip()
        m = _GH_PR_URL_RE.match(url)
        if m:
            by_pair[pair] = f"{m.group(1)}#{int(m.group(2))}"
        else:
            by_pair[pair] = f"{row['repo_full_name']}#{int(row['number'])}"
    out: dict[str, str] = {}
    for raw in raw_keys:
        try:
            owner, name, num = parse_item_key(raw)
        except ValueError:
            continue
        repo = store.get_repo(f"{owner}/{name}")
        if repo is not None and (repo.repo_id, num) in by_pair:
            out[raw] = by_pair[(repo.repo_id, num)]
    return out


def _walk_stack_parents(
    conn: sqlite3.Connection, repo: RepoConfig, start_base: str, canon_me: str
) -> list[str]:
    parents: list[str] = []
    seen = {canon_me}
    cur_base = start_base
    while (
        cur_base and cur_base not in _BASE_BRANCHES and len(parents) < _MAX_STACK_DEPTH
    ):
        parent_row = conn.execute(
            """
            SELECT number, base_ref FROM pull_requests
            WHERE repo_id = ? AND state = 'OPEN'
              AND COALESCE(is_cross_repository, 0) = 0
              AND head_ref = ?
            ORDER BY number DESC LIMIT 1
            """,
            (repo.repo_id, cur_base),
        ).fetchone()
        if parent_row is None:
            break
        p_key = f"{repo.full_name}#{int(parent_row['number'])}"
        if p_key in seen:
            break
        seen.add(p_key)
        parents.append(p_key)
        cur_base = str(parent_row["base_ref"] or "")
    parents.reverse()
    return parents


def _query_stack_children(
    conn: sqlite3.Connection, repo: RepoConfig, me: sqlite3.Row
) -> list[str]:
    head_ref = str(me["head_ref"] or "")
    if not head_ref or me["is_cross_repository"] == 1:
        return []
    me_num = int(me["number"])
    child_rows = conn.execute(
        """
        SELECT number FROM pull_requests
        WHERE repo_id = ? AND state = 'OPEN' AND base_ref = ?
        ORDER BY number ASC
        """,
        (repo.repo_id, head_ref),
    ).fetchall()
    return [
        f"{repo.full_name}#{int(r['number'])}"
        for r in child_rows
        if int(r["number"]) != me_num
    ]


def _stack_for_one_key(store: PackratStore, raw_key: str) -> list[str] | None:
    try:
        owner, name, num = parse_item_key(raw_key)
    except ValueError:
        return None
    repo = store.get_repo(f"{owner}/{name}")
    if repo is None or not repo.is_mirror:
        return None
    me = store.conn.execute(
        "SELECT number, base_ref, head_ref, is_cross_repository "
        "FROM pull_requests WHERE repo_id = ? AND number = ?",
        (repo.repo_id, num),
    ).fetchone()
    if me is None:
        return None
    canon_me = f"{repo.full_name}#{int(me['number'])}"
    parents = _walk_stack_parents(store.conn, repo, str(me["base_ref"] or ""), canon_me)
    children = _query_stack_children(store.conn, repo, me)
    return [*parents, canon_me, *children]


def _op_stack_neighbors(store: PackratStore, spec: Any) -> dict[str, list[str] | None]:
    opts = spec if isinstance(spec, Mapping) else {}
    raw_keys = [str(k) for k in (opts.get("keys") or []) if str(k)]
    return {key: _stack_for_one_key(store, key) for key in raw_keys}


# ----- 4. repo_probe & context ---------------------------------------------------


def _backfill_warnings_for_repo(repo: RepoConfig) -> list[str]:
    if not repo.is_mirror:
        return []
    warnings: list[str] = []
    for stream in BACKFILL_STREAMS:
        bf = repo.backfill(stream)
        if bf.complete:
            continue
        if bf.cursor is not None:
            warnings.append(
                f"{stream} backfill incomplete (resumes on next `packrat sync --repo {repo.full_name}`)"
            )
        else:
            warnings.append(
                f"{stream} backfill incomplete (run `packrat sync --repo {repo.full_name}`)"
            )
    return warnings


def _op_repo_probe(store: PackratStore, spec: Any) -> dict[str, Any]:
    opts = spec if isinstance(spec, Mapping) else {}
    repo_slug = str(opts.get("repo") or "").strip()
    if not repo_slug:
        return {"found": False, "repo": None}
    repo = store.get_repo(repo_slug)
    if repo is None:
        return {"found": False, "repo": repo_slug}
    counts = store.table_counts(repo.repo_id)
    backfill = {
        stream: {
            "cursor": repo.backfill(stream).cursor,
            "done_at": repo.backfill(stream).done_at,
        }
        for stream in BACKFILL_STREAMS
    }
    return {
        "found": True,
        "repo": repo.full_name,
        "mode": repo.mode,
        "default_branch": repo.default_branch or "main",
        "last_indexed_sha": repo.last_indexed_sha,
        "counts": counts,
        "backfill": backfill,
        "warnings": _backfill_warnings_for_repo(repo),
    }


def _find_excluded_prs(
    conn: sqlite3.Connection,
    repo_id: int,
    exclude_pr: int | None,
    exclude_head_sha: str | None,
) -> set[int]:
    excluded: set[int] = set()
    if exclude_pr is not None:
        excluded.add(int(exclude_pr))
    if exclude_head_sha and exclude_head_sha.strip():
        rows = conn.execute(
            "SELECT number FROM pull_requests "
            "WHERE repo_id = ? AND head_sha IS NOT NULL AND lower(head_sha) = lower(?)",
            (repo_id, exclude_head_sha.strip()),
        ).fetchall()
        excluded.update(int(r["number"]) for r in rows)
    return excluded


def _pr_excl_clause(excluded: set[int]) -> tuple[str, list[int]]:
    if not excluded:
        return "", []
    ordered = sorted(excluded)
    marks = ",".join("?" for _ in ordered)
    return f" AND pr_number NOT IN ({marks})", ordered


def _context_hotspots(
    conn: sqlite3.Connection,
    repo_id: int,
    files: Sequence[str],
    excluded_prs: set[int],
) -> list[dict[str, Any]]:
    commit_counts: dict[str, int] = {}
    for batch in _batched(files):
        marks = ",".join("?" for _ in batch)
        for row in conn.execute(
            f"SELECT file_path, COUNT(DISTINCT sha) AS cnt FROM commit_files "
            f"WHERE repo_id = ? AND file_path IN ({marks}) GROUP BY file_path",
            [repo_id, *batch],
        ).fetchall():
            commit_counts[str(row["file_path"])] = int(row["cnt"])
    thread_counts: dict[str, tuple[int, int]] = {}
    excl_sql, excl_params = _pr_excl_clause(excluded_prs)
    for batch in _batched(files):
        marks = ",".join("?" for _ in batch)
        for row in conn.execute(
            "SELECT file_path, COUNT(DISTINCT thread_id) AS total_cnt, "
            "COUNT(DISTINCT CASE WHEN is_resolved = 0 THEN thread_id END) AS unres_cnt "
            f"FROM review_threads WHERE repo_id = ? AND file_path IN ({marks})"
            f"{excl_sql} GROUP BY file_path",
            [repo_id, *batch, *excl_params],
        ).fetchall():
            thread_counts[str(row["file_path"])] = (
                int(row["total_cnt"]),
                int(row["unres_cnt"]),
            )
    items: list[dict[str, Any]] = []
    for path in set(commit_counts) | set(thread_counts):
        commits = commit_counts.get(path, 0)
        threads, unres = thread_counts.get(path, (0, 0))
        if commits > 0 or threads > 0:
            items.append(
                {
                    "file_path": path,
                    "commit_count": commits,
                    "review_thread_count": threads,
                    "unresolved_thread_count": unres,
                }
            )
    items.sort(
        key=lambda h: (
            -h["unresolved_thread_count"],
            -h["review_thread_count"],
            -h["commit_count"],
            h["file_path"],
        )
    )
    return items


def _thread_body_from_row(row: sqlite3.Row) -> str:
    body = str(row["body"] or "").strip()
    if body:
        return body
    try:
        parsed = json.loads(row["comments_json"] or "[]")
    except (TypeError, ValueError):
        return ""
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
        return str(parsed[0].get("body") or "").strip()
    return ""


def _context_prior_threads(
    conn: sqlite3.Connection,
    repo_id: int,
    files: Sequence[str],
    excluded_prs: set[int],
    max_threads: int,
) -> list[dict[str, Any]]:
    if max_threads <= 0:
        return []
    excl_sql, excl_params = _pr_excl_clause(excluded_prs)
    collected: list[sqlite3.Row] = []
    for batch in _batched(files):
        marks = ",".join("?" for _ in batch)
        collected.extend(
            conn.execute(
                "SELECT pr_number, file_path, line, is_resolved, reviewer, body, "
                "comments_json, COALESCE(updated_at, created_at, '') AS sort_ts "
                f"FROM review_threads WHERE repo_id = ? AND file_path IN ({marks})"
                f"{excl_sql}",
                [repo_id, *batch, *excl_params],
            ).fetchall()
        )
    collected.sort(
        key=lambda r: (str(r["sort_ts"] or ""), int(r["pr_number"])),
        reverse=True,
    )
    collected.sort(key=lambda r: int(r["is_resolved"] or 0))
    out: list[dict[str, Any]] = []
    for row in collected[:max_threads]:
        pr_num = int(row["pr_number"])
        out.append(
            {
                "pr_number": pr_num,
                "file_path": str(row["file_path"]),
                "line": int(row["line"]) if row["line"] is not None else None,
                "is_resolved": bool(row["is_resolved"]),
                "reviewer": str(row["reviewer"] or "").strip() or "unknown",
                "untrusted_excerpt": _wrap_snippet(
                    f"review_thread:PR#{pr_num}", _thread_body_from_row(row)
                ),
            }
        )
    return out


_TOUCHED_PRS_TEMPLATES: tuple[str, str] = (
    (
        "SELECT DISTINCT p.number, p.state, p.title, COALESCE(p.updated_at, '') AS sort_ts "
        "FROM pr_files pf JOIN pull_requests p ON p.repo_id = pf.repo_id AND p.number = pf.pr_number "
        "WHERE pf.repo_id = ? AND pf.file_path IN ({marks})"
    ),
    (
        "SELECT DISTINCT p.number, p.state, p.title, COALESCE(p.updated_at, '') AS sort_ts "
        "FROM commit_files cf JOIN commits c ON c.repo_id = cf.repo_id AND c.sha = cf.sha "
        "JOIN pull_requests p ON p.repo_id = c.repo_id AND p.number = c.pr_number "
        "WHERE cf.repo_id = ? AND cf.file_path IN ({marks})"
    ),
)


def _query_touched_prs_for_tpl(
    conn: sqlite3.Connection,
    sql_tpl: str,
    repo_id: int,
    files: Sequence[str],
    excluded_prs: set[int],
    by_pr: dict[int, sqlite3.Row],
) -> None:
    for batch in _batched(files):
        marks = ",".join("?" for _ in batch)
        for row in conn.execute(
            sql_tpl.format(marks=marks), [repo_id, *batch]
        ).fetchall():
            num = int(row["number"])
            if num not in excluded_prs:
                by_pr[num] = row


def _context_related_items(
    conn: sqlite3.Connection,
    repo_id: int,
    files: Sequence[str],
    excluded_prs: set[int],
    max_items: int,
) -> list[dict[str, Any]]:
    if max_items <= 0:
        return []
    by_pr: dict[int, sqlite3.Row] = {}
    for sql_tpl in _TOUCHED_PRS_TEMPLATES:
        _query_touched_prs_for_tpl(conn, sql_tpl, repo_id, files, excluded_prs, by_pr)
    pr_rows = sorted(
        by_pr.values(),
        key=lambda r: (str(r["sort_ts"]), int(r["number"])),
        reverse=True,
    )
    pr_items = [
        {
            "kind": "pr",
            "number": int(r["number"]),
            "state": str(r["state"] or ""),
            "link_type": "touched_file",
            "untrusted_title": _wrap_snippet(
                f"pr#{int(r['number'])}", str(r["title"] or "")
            ),
        }
        for r in pr_rows
    ]
    issue_items = _context_linked_issues(
        conn, repo_id, [int(r["number"]) for r in pr_rows]
    )
    issue_quota = max(1, max_items // 2) if issue_items else 0
    sel_issues = issue_items[:issue_quota]
    sel_prs = pr_items[: max_items - len(sel_issues)]
    rem = max_items - len(sel_issues) - len(sel_prs)
    if rem > 0:
        sel_issues.extend(issue_items[len(sel_issues) : len(sel_issues) + rem])
    return [*sel_issues, *sel_prs]


def _accumulate_issue_link_row(
    by_issue: dict[int, tuple[str, str, str, str]], row: sqlite3.Row
) -> None:
    num = int(row["number"])
    raw_lt = str(row["link_type"] or "references").strip().lower()
    lt = "closes" if raw_lt.startswith("close") else "references"
    if num not in by_issue or lt == "closes":
        by_issue[num] = (
            str(row["state"] or "OPEN"),
            lt,
            str(row["title"] or ""),
            str(row["sort_ts"] or ""),
        )


def _context_linked_issues(
    conn: sqlite3.Connection, repo_id: int, pr_numbers: Sequence[int]
) -> list[dict[str, Any]]:
    if not pr_numbers:
        return []
    by_issue: dict[int, tuple[str, str, str, str]] = {}
    for batch_start in range(0, len(pr_numbers), _SQL_BATCH_SIZE):
        batch = pr_numbers[batch_start : batch_start + _SQL_BATCH_SIZE]
        marks = ",".join("?" for _ in batch)
        rows = conn.execute(
            "SELECT i.number, i.state, i.title, COALESCE(i.updated_at, '') AS sort_ts, ipl.link_type "
            "FROM issue_pr_links ipl JOIN issues i ON i.repo_id = ipl.repo_id AND i.number = ipl.issue_number "
            f"WHERE ipl.repo_id = ? AND ipl.pr_number IN ({marks})",
            [repo_id, *batch],
        ).fetchall()
        for row in rows:
            _accumulate_issue_link_row(by_issue, row)
    ordered = sorted(
        by_issue.items(),
        key=lambda kv: (kv[1][0].upper() == "OPEN", kv[1][3], kv[0]),
        reverse=True,
    )
    return [
        {
            "kind": "issue",
            "number": num,
            "state": meta[0],
            "link_type": meta[1],
            "untrusted_title": _wrap_snippet(f"issue#{num}", meta[2]),
        }
        for num, meta in ordered
    ]


def _op_context(store: PackratStore, spec: Any) -> dict[str, Any]:
    opts = spec if isinstance(spec, Mapping) else {}
    probe = _op_repo_probe(store, opts)
    raw_files = [
        _clean_rel_path(str(f))
        for f in (opts.get("files") or [])
        if _clean_rel_path(str(f))
    ]
    clean_files = list(dict.fromkeys(raw_files))
    if (
        not probe.get("found")
        or not clean_files
        or int(probe["counts"].get("chunks", 0)) <= 0
    ):
        return {
            "probe": probe,
            "hotspots": [],
            "prior_review_threads": [],
            "related_items": [],
        }
    repo = store.get_repo(str(probe["repo"]))
    if repo is None:
        return {
            "probe": {"found": False, "repo": opts.get("repo")},
            "hotspots": [],
            "prior_review_threads": [],
            "related_items": [],
        }
    exclude_pr_raw = opts.get("exclude_pr")
    exclude_pr = int(exclude_pr_raw) if exclude_pr_raw is not None else None
    exclude_sha = str(opts.get("exclude_head_sha") or "").strip() or None
    max_threads = int(opts.get("max_threads", 8))
    max_items = int(opts.get("max_items", 8))
    excluded = _find_excluded_prs(store.conn, repo.repo_id, exclude_pr, exclude_sha)
    return {
        "probe": probe,
        "hotspots": _context_hotspots(store.conn, repo.repo_id, clean_files, excluded),
        "prior_review_threads": _context_prior_threads(
            store.conn, repo.repo_id, clean_files, excluded, max_threads
        ),
        "related_items": _context_related_items(
            store.conn, repo.repo_id, clean_files, excluded, max_items
        ),
    }


# ----- 5. review_signals ---------------------------------------------------------


def _register_login(
    canonical_by_lower: dict[str, str], raw_login: str | None
) -> str | None:
    cleaned = (raw_login or "").strip().lstrip("@")
    if not cleaned or _is_bot_identity(cleaned):
        return None
    return canonical_by_lower.setdefault(cleaned.lower(), cleaned)


def _unique_parent_dirs(files: Sequence[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for fp in files:
        parent = posixpath.dirname(fp)
        if parent and parent != ".":
            seen.setdefault(parent, None)
    return tuple(seen)


def _is_merge_title(title: str | None) -> bool:
    low = (title or "").strip().lower()
    return low.startswith(
        ("merge pull request #", "merge branch ", "merge remote-tracking branch ")
    )


def _record_commit_identity_row(
    row: sqlite3.Row,
    scoped_shas: set[str] | None,
    canonical_by_lower: dict[str, str],
    known_logins: set[str],
    sha_cand: dict[str, set[str]],
    email_cand: dict[str, set[str]],
    name_cand: dict[str, set[str]],
) -> None:
    if _is_merge_title(row["title"]):
        return
    canon = _register_login(canonical_by_lower, row["pr_author"])
    if not canon:
        return
    known_logins.add(canon)
    low_login = canon.lower()
    sha_key = str(row["sha"] or "").strip().lower()
    if sha_key and (scoped_shas is None or sha_key in scoped_shas):
        sha_cand.setdefault(sha_key, set()).add(low_login)
    email = str(row["author_email"] or "").strip().lower()
    if email and not _is_bot_identity(email):
        email_cand.setdefault(email, set()).add(low_login)
    norm_name = _normalize_multi_token_name(row["author_name"])
    if norm_name and not _is_bot_identity(norm_name):
        name_cand.setdefault(norm_name, set()).add(low_login)


def _unique_candidate_map(
    cand: dict[str, set[str]], canonical_by_lower: dict[str, str]
) -> dict[str, str]:
    return {
        k: canonical_by_lower[next(iter(v))] for k, v in cand.items() if len(v) == 1
    }


def _bounded_identity_maps(
    conn: sqlite3.Connection,
    repo_id: int,
    effective_files: tuple[str, ...],
    parent_dirs: tuple[str, ...],
    canonical_by_lower: dict[str, str],
    known_logins: set[str],
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
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
    scoped_shas = _scoped_commit_shas(conn, repo_id, effective_files, parent_dirs)
    sha_cand: dict[str, set[str]] = {}
    email_cand: dict[str, set[str]] = {}
    name_cand: dict[str, set[str]] = {}
    for row in rows:
        _record_commit_identity_row(
            row,
            scoped_shas,
            canonical_by_lower,
            known_logins,
            sha_cand,
            email_cand,
            name_cand,
        )
    return (
        _unique_candidate_map(sha_cand, canonical_by_lower),
        _unique_candidate_map(email_cand, canonical_by_lower),
        _unique_candidate_map(name_cand, canonical_by_lower),
    )


def _scoped_commit_shas(
    conn: sqlite3.Connection,
    repo_id: int,
    effective_files: tuple[str, ...],
    parent_dirs: tuple[str, ...],
) -> set[str] | None:
    if not effective_files:
        return None
    shas: set[str] = set()
    for batch in _batched(effective_files):
        marks = ",".join("?" for _ in batch)
        for (sha,) in conn.execute(
            f"SELECT DISTINCT lower(sha) FROM commit_files "
            f"WHERE repo_id = ? AND file_path IN ({marks})",
            [repo_id, *batch],
        ).fetchall():
            if sha:
                shas.add(str(sha))
    for dirpath in parent_dirs:
        prefix = f"{dirpath}/"
        upper = f"{dirpath}0"
        for (sha,) in conn.execute(
            """
            SELECT DISTINCT lower(sha) FROM commit_files
            WHERE repo_id = ? AND file_path >= ? AND file_path < ?
              AND INSTR(SUBSTR(file_path, LENGTH(?) + 1), '/') = 0
            LIMIT 2000
            """,
            (repo_id, prefix, upper, prefix),
        ).fetchall():
            if sha:
                shas.add(str(sha))
    return shas


def _sibling_params(
    repo_id: int,
    dirpath: str,
    effective_files: tuple[str, ...],
    path_col: str,
    target_pr: int | None,
    pr_col: str,
) -> tuple[str, str, list[Any]]:
    prefix = f"{dirpath}/"
    upper = f"{dirpath}0"
    excluded = [f for f in effective_files if posixpath.dirname(f) == dirpath]
    excl_sql = ""
    params: list[Any] = [repo_id, prefix, upper, prefix]
    if excluded:
        marks = ",".join("?" for _ in excluded)
        excl_sql = f"AND {path_col} NOT IN ({marks})"
        params.extend(excluded)
    pr_sql = f"AND {pr_col} != ?" if target_pr is not None else ""
    if target_pr is not None:
        params.append(target_pr)
    params.append(_DIR_EVENT_LIMIT)
    return excl_sql, pr_sql, params


def _collect_file_review_events(
    conn: sqlite3.Connection,
    repo_id: int,
    effective_files: tuple[str, ...],
    target_pr: int | None,
    canonical_by_lower: dict[str, str],
    known_logins: set[str],
    events: list[dict[str, Any]],
) -> None:
    pr_sql = "AND r.pr_number != ?" if target_pr is not None else ""
    pr_tail = [target_pr] if target_pr is not None else []
    for batch in _batched(effective_files):
        marks = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"""
            SELECT r.pr_number, r.author AS reviewer_login, r.state,
                   COALESCE(r.submitted_at, p.updated_at, p.created_at) AS event_time,
                   pf.file_path, p.changed_files AS pr_changed_files, p.author AS pr_author
            FROM pr_files pf
            JOIN reviews r ON r.repo_id = pf.repo_id AND r.pr_number = pf.pr_number
            JOIN pull_requests p ON p.repo_id = r.repo_id AND p.number = r.pr_number
            WHERE pf.repo_id = ? AND pf.file_path IN ({marks})
              AND UPPER(r.state) IN ('APPROVED', 'CHANGES_REQUESTED', 'COMMENTED')
              AND r.author IS NOT NULL
              AND (p.author IS NULL OR LOWER(r.author) != LOWER(p.author))
              {pr_sql}
            ORDER BY r.pr_number DESC, r.review_id ASC, pf.file_path ASC
            """,
            [repo_id, *batch, *pr_tail],
        ).fetchall()
        for row in rows:
            ev = _format_review_event_dict(
                row, str(row["file_path"]), False, canonical_by_lower, known_logins
            )
            if ev is not None:
                events.append(ev)


def _collect_dir_review_events(
    conn: sqlite3.Connection,
    repo_id: int,
    effective_files: tuple[str, ...],
    parent_dirs: tuple[str, ...],
    target_pr: int | None,
    canonical_by_lower: dict[str, str],
    known_logins: set[str],
    events: list[dict[str, Any]],
) -> None:
    for dirpath in parent_dirs:
        excl_sql, pr_sql, d_params = _sibling_params(
            repo_id, dirpath, effective_files, "pf.file_path", target_pr, "r.pr_number"
        )
        rows = conn.execute(
            f"""
            SELECT DISTINCT r.review_id, r.pr_number, r.author AS reviewer_login, r.state,
                   COALESCE(r.submitted_at, p.updated_at, p.created_at) AS event_time,
                   p.changed_files AS pr_changed_files, p.author AS pr_author
            FROM pr_files pf
            JOIN reviews r ON r.repo_id = pf.repo_id AND r.pr_number = pf.pr_number
            JOIN pull_requests p ON p.repo_id = r.repo_id AND p.number = r.pr_number
            WHERE pf.repo_id = ? AND pf.file_path >= ? AND pf.file_path < ?
              AND INSTR(SUBSTR(pf.file_path, LENGTH(?) + 1), '/') = 0
              {excl_sql}
              AND UPPER(r.state) IN ('APPROVED', 'CHANGES_REQUESTED', 'COMMENTED')
              AND r.author IS NOT NULL
              AND (p.author IS NULL OR LOWER(r.author) != LOWER(p.author))
              {pr_sql}
            ORDER BY event_time DESC, r.pr_number DESC, r.review_id ASC
            LIMIT ?
            """,
            d_params,
        ).fetchall()
        for row in rows:
            ev = _format_review_event_dict(
                row, dirpath, True, canonical_by_lower, known_logins
            )
            if ev is not None:
                events.append(ev)


def _collect_review_events(
    conn: sqlite3.Connection,
    repo_id: int,
    effective_files: tuple[str, ...],
    parent_dirs: tuple[str, ...],
    target_pr: int | None,
    canonical_by_lower: dict[str, str],
    known_logins: set[str],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    _collect_file_review_events(
        conn,
        repo_id,
        effective_files,
        target_pr,
        canonical_by_lower,
        known_logins,
        events,
    )
    _collect_dir_review_events(
        conn,
        repo_id,
        effective_files,
        parent_dirs,
        target_pr,
        canonical_by_lower,
        known_logins,
        events,
    )
    return events


def _format_review_event_dict(
    row: sqlite3.Row,
    scope_path: str,
    is_directory: bool,
    canonical_by_lower: dict[str, str],
    known_logins: set[str],
) -> dict[str, Any] | None:
    canon = _register_login(canonical_by_lower, row["reviewer_login"])
    state = str(row["state"] or "").strip().upper()
    pr_author = str(row["pr_author"] or "").strip()
    if not canon or state not in _VALID_REVIEW_STATES:
        return None
    if pr_author and canon.lower() == pr_author.lower():
        return None
    known_logins.add(canon)
    ts = _parse_iso_epoch(row["event_time"]) or 0.0
    return {
        "pr_number": int(row["pr_number"]),
        "reviewer_login": canon,
        "state": state,
        "timestamp": ts,
        "scope_path": scope_path,
        "is_directory": is_directory,
        "pr_changed_files": max(1, int(row["pr_changed_files"] or 1)),
    }


def _extract_comment_node_author(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    author = node.get("author")
    if isinstance(author, dict) and isinstance(author.get("login"), str):
        return author["login"].strip()
    if isinstance(author, str):
        return author.strip()
    return ""


def _raw_comment_authors(raw_json: Any) -> list[str]:
    if not raw_json:
        return []
    try:
        parsed = json.loads(raw_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [a for node in parsed if (a := _extract_comment_node_author(node))]


def _thread_logins_from_row(row: sqlite3.Row) -> list[str]:
    candidates: list[str] = []
    if row["reviewer"] and str(row["reviewer"]).strip():
        candidates.append(str(row["reviewer"]).strip())
    candidates.extend(_raw_comment_authors(row["comments_json"]))
    pr_author_low = str(row["pr_author"] or "").strip().lower()
    seen: set[str] = set()
    out: list[str] = []
    for cand in candidates:
        cleaned = cand.strip().lstrip("@")
        low = cleaned.lower()
        if (
            not cleaned
            or low in seen
            or low == pr_author_low
            or _is_bot_identity(cleaned)
        ):
            continue
        seen.add(low)
        out.append(cleaned)
    return out


def _append_single_thread_row(
    row: sqlite3.Row,
    is_directory: bool,
    dir_override: str | None,
    seen_keys: set[tuple[str, str, str, bool]],
    canonical_by_lower: dict[str, str],
    known_logins: set[str],
    dest: list[dict[str, Any]],
) -> None:
    tid = str(row["thread_id"] or "").strip()
    scope = (
        dir_override if is_directory else _clean_rel_path(str(row["file_path"] or ""))
    )
    if not tid or not scope:
        return
    ts = _parse_iso_epoch(row["event_time"]) or 0.0
    pr_num = int(row["pr_number"])
    for raw_login in _thread_logins_from_row(row):
        canon = _register_login(canonical_by_lower, raw_login)
        if not canon:
            continue
        dedup = (tid, canon.lower(), scope, is_directory)
        if dedup not in seen_keys:
            seen_keys.add(dedup)
            known_logins.add(canon)
            dest.append(
                {
                    "thread_id": tid,
                    "pr_number": pr_num,
                    "reviewer_login": canon,
                    "timestamp": ts,
                    "scope_path": scope,
                    "is_directory": is_directory,
                }
            )


def _append_thread_rows(
    rows: Sequence[sqlite3.Row],
    is_directory: bool,
    dir_override: str | None,
    seen_keys: set[tuple[str, str, str, bool]],
    canonical_by_lower: dict[str, str],
    known_logins: set[str],
    dest: list[dict[str, Any]],
) -> None:
    for row in rows:
        _append_single_thread_row(
            row,
            is_directory,
            dir_override,
            seen_keys,
            canonical_by_lower,
            known_logins,
            dest,
        )


def _collect_thread_events(
    conn: sqlite3.Connection,
    repo_id: int,
    effective_files: tuple[str, ...],
    parent_dirs: tuple[str, ...],
    target_pr: int | None,
    canonical_by_lower: dict[str, str],
    known_logins: set[str],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str, bool]] = set()
    for batch in _batched(effective_files):
        marks = ",".join("?" for _ in batch)
        params: list[Any] = [repo_id, *batch]
        pr_sql = "AND rt.pr_number != ?" if target_pr is not None else ""
        if target_pr is not None:
            params.append(target_pr)
        rows = conn.execute(
            f"""
            SELECT rt.thread_id, rt.pr_number, rt.file_path, rt.reviewer, rt.comments_json,
                   COALESCE(rt.updated_at, rt.created_at, p.updated_at, p.created_at) AS event_time,
                   p.author AS pr_author
            FROM review_threads rt
            JOIN pull_requests p ON p.repo_id = rt.repo_id AND p.number = rt.pr_number
            WHERE rt.repo_id = ? AND rt.file_path IN ({marks}) {pr_sql}
            ORDER BY rt.pr_number DESC, rt.thread_id ASC
            """,
            params,
        ).fetchall()
        _append_thread_rows(
            rows, False, None, seen_keys, canonical_by_lower, known_logins, events
        )
    for dirpath in parent_dirs:
        excl_sql, pr_sql, d_params = _sibling_params(
            repo_id, dirpath, effective_files, "rt.file_path", target_pr, "rt.pr_number"
        )
        rows = conn.execute(
            f"""
            SELECT rt.thread_id, rt.pr_number, rt.file_path, rt.reviewer, rt.comments_json,
                   COALESCE(rt.updated_at, rt.created_at, p.updated_at, p.created_at) AS event_time,
                   p.author AS pr_author
            FROM review_threads rt
            JOIN pull_requests p ON p.repo_id = rt.repo_id AND p.number = rt.pr_number
            WHERE rt.repo_id = ? AND rt.file_path >= ? AND rt.file_path < ?
              AND INSTR(SUBSTR(rt.file_path, LENGTH(?) + 1), '/') = 0
              {excl_sql} {pr_sql}
            ORDER BY event_time DESC, rt.pr_number DESC, rt.thread_id ASC
            LIMIT ?
            """,
            d_params,
        ).fetchall()
        _append_thread_rows(
            rows, True, dirpath, seen_keys, canonical_by_lower, known_logins, events
        )
    return events


def _load_target_pr_meta(
    conn: sqlite3.Connection, repo_id: int, target_pr: int | None
) -> tuple[str | None, str | None, str | None, tuple[str, ...]]:
    if target_pr is None:
        return None, None, None, ()
    pr_row = conn.execute(
        "SELECT author, title, base_ref FROM pull_requests WHERE repo_id = ? AND number = ?",
        (repo_id, target_pr),
    ).fetchone()
    pr_author: str | None = None
    pr_title: str | None = None
    pr_base_ref: str | None = None
    if pr_row is not None:
        pr_author = str(pr_row["author"] or "").strip() or None
        pr_title = str(pr_row["title"]) if pr_row["title"] is not None else None
        pr_base_ref = str(pr_row["base_ref"] or "").strip() or None
    f_rows = conn.execute(
        "SELECT file_path FROM pr_files WHERE repo_id = ? AND pr_number = ? ORDER BY file_path ASC",
        (repo_id, target_pr),
    ).fetchall()
    pr_files = tuple(
        dict.fromkeys(
            cleaned for r in f_rows if (cleaned := _clean_rel_path(str(r["file_path"])))
        )
    )
    return pr_author, pr_title, pr_base_ref, pr_files


def _load_repo_known_logins(
    conn: sqlite3.Connection, repo_id: int
) -> tuple[dict[str, str], set[str]]:
    canonical_by_lower: dict[str, str] = {}
    known_logins: set[str] = set()
    for table, col in (
        ("pull_requests", "author"),
        ("reviews", "author"),
        ("review_threads", "reviewer"),
    ):
        for (raw_login,) in conn.execute(
            f"SELECT DISTINCT {col} FROM {table} WHERE repo_id = ? AND {col} IS NOT NULL",
            (repo_id,),
        ).fetchall():
            canon = _register_login(canonical_by_lower, str(raw_login))
            if canon:
                known_logins.add(canon)
    return canonical_by_lower, known_logins


def _parse_target_pr_opt(opts: Mapping[str, Any]) -> int | None:
    raw = opts.get("target_pr")
    if raw is None:
        raw = opts.get("pr_number")
    return int(raw) if raw is not None else None


def _op_review_signals(store: PackratStore, spec: Any) -> dict[str, Any]:
    opts = spec if isinstance(spec, Mapping) else {}
    clean_repo = str(opts.get("repo") or "").strip()
    if not clean_repo:
        return {"status": "no_github_repo", "repo_full_name": None}
    repo = store.get_repo(clean_repo)
    if repo is None:
        return {"status": "untracked_repo", "repo_full_name": clean_repo}
    conn = store.conn
    target_pr = _parse_target_pr_opt(opts)
    pr_author, pr_title, pr_base_ref, pr_files = _load_target_pr_meta(
        conn, repo.repo_id, target_pr
    )
    raw_changed = [
        cleaned
        for p in (opts.get("files") or [])
        if (cleaned := _clean_rel_path(str(p)))
    ]
    effective_files = tuple(dict.fromkeys(raw_changed)) or pr_files
    parent_dirs = _unique_parent_dirs(effective_files)
    canonical_by_lower, known_logins = _load_repo_known_logins(conn, repo.repo_id)
    sha_map, email_map, name_map = _bounded_identity_maps(
        conn,
        repo.repo_id,
        effective_files,
        parent_dirs,
        canonical_by_lower,
        known_logins,
    )
    review_events = (
        _collect_review_events(
            conn,
            repo.repo_id,
            effective_files,
            parent_dirs,
            target_pr,
            canonical_by_lower,
            known_logins,
        )
        if effective_files
        else []
    )
    thread_events = (
        _collect_thread_events(
            conn,
            repo.repo_id,
            effective_files,
            parent_dirs,
            target_pr,
            canonical_by_lower,
            known_logins,
        )
        if effective_files
        else []
    )
    return {
        "status": "ok",
        "repo_full_name": repo.full_name,
        "prs_backfill_complete": bool((repo.prs_backfill_done_at or "").strip()),
        "target_pr_author": pr_author,
        "target_pr_title": pr_title,
        "target_pr_base_ref": pr_base_ref,
        "target_pr_files": list(pr_files),
        "known_logins": sorted(known_logins),
        "sha_to_login": sha_map,
        "email_to_login": email_map,
        "name_to_login": name_map,
        "review_events": review_events,
        "thread_events": thread_events,
    }


_SIMPLE_STORE_OPS: tuple[tuple[str, Any], ...] = (
    ("known_keys", _op_known_keys),
    ("stack_neighbors", _op_stack_neighbors),
    ("repo_probe", _op_repo_probe),
    ("context", _op_context),
    ("review_signals", _op_review_signals),
)


def execute_reader_batch(
    store: PackratStore,
    ops: Mapping[str, Any],
    *,
    personal_store: PersonalStore | None = None,
    owner_id: str = "local",
    viewer_login: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    """Executes typed reader batch operations inside a consistent read snapshot."""
    moment = time.time() if now is None else now
    result: dict[str, Any] = {}
    with store.read_snapshot():
        if ops.get("health"):
            result["health"] = _op_health(
                store, personal_store, owner_id, viewer_login, moment
            )
        if ops.get("viewer_items") is not None and ops.get("viewer_items") is not False:
            result["viewer_items"] = _op_viewer_items(
                store, personal_store, owner_id, viewer_login, ops["viewer_items"]
            )
        if ops.get("items") is not None and ops.get("items") is not False:
            result["items"] = _op_items(
                store, personal_store, owner_id, moment, ops["items"]
            )
        for key, handler in _SIMPLE_STORE_OPS:
            val = ops.get(key)
            if val is not None and val is not False:
                result[key] = handler(store, val)
    return result
