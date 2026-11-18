"""Reads live pull request data for /monitor from a running `packrat serve`.

This module follows the packrat reader contract
(`packrat/references/reader_contract.md`) and never imports packrat
code. On the default packrat path (or when no local SQLite file exists on disk),
it queries `packrat reader --ops-json <json> --json` so both local and remote
`packrat` modes work with zero local `repos.sqlite`. On explicit non-default
SQLite fixture files on disk, it reads the contract's `v_*` views through a
read-only SQLite connection (`file:<db>?mode=ro`). Writes always go through the
`packrat` CLI: `watch` leases, `refresh` requests, and the `changes` feed.

`PackratSource.snapshots()` rebuilds the GraphQL node shape that `normalize.py`
consumes, so `diff_events.py` and `a2ui_board.py` work unchanged. packrat does
not store assignees or the head commit's committer, so those come back empty;
the caller carries them over from earlier snapshots.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from models import parse_pr_ref

DEFAULT_PACKRAT_DB = Path.home() / ".local" / "share" / "packrat" / "repos.sqlite"
STALE_HEARTBEAT_S = 180
"""The service counts as down when `beat_age_s` is NULL or above this."""
UNHEALTHY_FAILED_TICKS = 3
"""The service counts as unhealthy once this many ticks in a row have failed."""
CONSUMER = "monitor"
AUTHORED_OPEN_SET = "authored_open"
"""The viewer set of every open pull request the user authored, drafts included."""
REVIEW_SETS = ("review_requested", "reviewed")
"""The viewer sets that make up the review queue."""
DEFAULT_FRESHNESS_S = 120
DEFAULT_TTL_S = 900
MAX_STACK_DEPTH = 10
BASE_BRANCHES = frozenset({"main", "master", "develop"})
_PACKRAT_SCRIPT = (
    Path(__file__).resolve().parents[2] / "packrat" / "scripts" / "packrat.py"
)

_ITEM_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+#[0-9]+$")

PackratRunner = Callable[[list[str], float], tuple[int, str, str]]
"""Runs `packrat <args>` with a timeout in seconds and returns `(code, stdout, stderr)`."""

RowLike = Mapping[str, Any] | sqlite3.Row


class PackratError(Exception):
    """Raised when the packrat CLI or database can't serve a request."""


class ViewerSetUnavailable(PackratError):
    """Raised when the packrat service has never synced a required viewer set."""


@dataclass(frozen=True)
class ChangeBatch:
    """Result of one `packrat changes` call.

    Attributes:
        seq: The feed position to pass as the next `since`.
        keys: Watched item keys with at least one change row.
        reset: True when `since` fell behind the retained feed; take a full snapshot.
        viewer_changed: True when `watch_viewer_sets` is enabled and a `viewer` row arrived.
    """

    seq: int
    keys: set[str] = field(default_factory=set)
    reset: bool = False
    viewer_changed: bool = False


@dataclass(frozen=True)
class ServiceHealth:
    """The `packrat serve` health fields that /monitor acts on.

    Attributes:
        beat_age_s: Seconds since the service's last heartbeat, or None when it
            has never beaten.
        consecutive_failed_ticks: Ticks that ended with an error since the last
            clean one. `0` when the service does not report it.
        viewer_sets_synced_at: Last sync time of each viewer set, keyed by set
            name. A set that never synced is absent, and the map is empty when
            the service does not report it.
    """

    beat_age_s: int | None
    consecutive_failed_ticks: int
    viewer_sets_synced_at: Mapping[str, str]

    @property
    def heartbeat_fresh(self) -> bool:
        """True when the heartbeat is at most `STALE_HEARTBEAT_S` seconds old."""
        return self.beat_age_s is not None and self.beat_age_s <= STALE_HEARTBEAT_S

    @property
    def healthy(self) -> bool:
        """True when the heartbeat is fresh and under 3 ticks in a row have failed."""
        return (
            self.heartbeat_fresh
            and self.consecutive_failed_ticks < UNHEALTHY_FAILED_TICKS
        )


def _is_default_db_path(db_path: Path) -> bool:
    """Returns True when `db_path` resolves to the default packrat database path."""
    resolved = Path(db_path).expanduser().resolve()
    return resolved in {
        Path(DEFAULT_PACKRAT_DB).expanduser().resolve(),
        (Path.home() / ".local" / "share" / "packrat" / "repos.sqlite").resolve(),
    }


is_default_db_path = _is_default_db_path


def _use_local_views(db_path: Path) -> bool:
    expanded = Path(db_path).expanduser()
    return not _is_default_db_path(expanded) and expanded.is_file()


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Opens the packrat database read-only, raising `PackratError` when that fails."""
    path = Path(db_path)
    if not path.exists():
        raise PackratError(f"packrat database not found: {path}")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise PackratError(f"cannot open packrat database {path}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    return conn


class SubprocessPackrat:
    """Runs the `packrat` CLI (from PATH, else the sibling skill's `packrat.py`)."""

    def __init__(self, db_path: Path = DEFAULT_PACKRAT_DB) -> None:
        self.db_path = Path(db_path)
        exe = shutil.which("packrat")
        self.prefix = [exe] if exe else [sys.executable, str(_PACKRAT_SCRIPT)]

    def run(self, args: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
        """Executes `packrat <args>`, omitting `--db` on the default database path."""
        db_flags = (
            [] if _is_default_db_path(self.db_path) else ["--db", str(self.db_path)]
        )
        argv = [*self.prefix, *db_flags, *args]
        cmd = args[0] if args else "cli"
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise PackratError(f"packrat {cmd} timed out after {timeout:.0f}s") from exc
        except OSError as exc:
            raise PackratError(f"cannot run packrat: {exc}") from exc
        return proc.returncode, proc.stdout, proc.stderr

    def __call__(
        self, args: list[str], timeout_s: float = 30.0
    ) -> tuple[int, str, str]:
        return self.run(args, timeout=timeout_s)


def _invoke_runner(
    runner: Any, args: list[str], timeout_s: float
) -> tuple[int, str, str]:
    if callable(runner):
        return runner(args, timeout_s)
    return runner.run(args, timeout=timeout_s)


def _format_cli_failure(cmd: str, code: int, stdout: str, stderr: str) -> str:
    raw = (stderr or stdout).strip()
    lines = raw.splitlines()
    tail = lines[-1] if lines else "unknown error"
    if "auth_expired" in raw and "auth_expired" not in tail:
        tail = f"[auth_expired] {tail}"
    return f"packrat {cmd} failed (exit {code}): {tail}"


def _run_packrat_json(
    runner: Any,
    args: list[str],
    items: Sequence[str] = (),
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    argv = [*args, "--json", "--", *items] if items else [*args, "--json"]
    code, stdout, stderr = _invoke_runner(runner, argv, timeout_s)
    if code != 0:
        raise PackratError(_format_cli_failure(args[0], code, stdout, stderr))
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError as exc:
        raise PackratError(f"packrat {args[0]} returned invalid JSON: {exc}") from exc
    return payload if isinstance(payload, dict) else {}


def _local_heartbeat_age(db_path: Path) -> int | None:
    try:
        with closing(connect_readonly(db_path)) as conn:
            row = conn.execute("SELECT beat_age_s FROM v_service_health").fetchone()
    except (PackratError, sqlite3.Error):
        return None
    if row is None or row["beat_age_s"] is None:
        return None
    return int(row["beat_age_s"])


def heartbeat_age(
    db_path: Path = DEFAULT_PACKRAT_DB,
    *,
    runner: PackratRunner | None = None,
) -> int | None:
    """Returns `beat_age_s`, or None when unknown, unreachable, or unreadable."""
    path = Path(db_path)
    if _use_local_views(path):
        return _local_heartbeat_age(path)
    if not _is_default_db_path(path) and runner is None:
        return None
    active_runner = runner or SubprocessPackrat(path)
    try:
        payload = _run_packrat_json(
            active_runner,
            ["reader", "--ops-json", json.dumps({"health": True})],
            timeout_s=15.0,
        )
    except PackratError:
        return None
    health = payload.get("health")
    if not isinstance(health, Mapping) or health.get("beat_age_s") is None:
        return None
    return int(health["beat_age_s"])


def available(
    db_path: Path = DEFAULT_PACKRAT_DB,
    max_beat_age_s: int = STALE_HEARTBEAT_S,
    *,
    runner: PackratRunner | None = None,
) -> bool:
    """Returns True when packrat is reachable and the service heartbeat is fresh."""
    age = heartbeat_age(db_path, runner=runner)
    return age is not None and age <= max_beat_age_s


def valid_item_keys(keys: Iterable[str]) -> list[str]:
    """Returns the sorted, de-duplicated keys shaped like `owner/name#N`, warning on others."""
    good: set[str] = set()
    for key in keys:
        if _ITEM_KEY_RE.match(key):
            good.add(key)
        else:
            sys.stderr.write(f"warning: skipping malformed packrat item key {key!r}\n")
    return sorted(good)


def _bool(value: Any) -> bool:
    return bool(value) if value is not None else False


def _row_get(row: RowLike, column: str) -> Any:
    try:
        return row[column]
    except (IndexError, KeyError):
        return None


def _sync_times(raw: Any) -> dict[str, str]:
    """Reads a viewer set sync-time map from a mapping or from JSON object text."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if not isinstance(raw, Mapping):
        return {}
    return {str(name): str(at) for name, at in raw.items() if at}


def _service_health(row: RowLike | None, sync_column: str) -> ServiceHealth:
    """Builds `ServiceHealth` from a health row, reading missing fields as 0 or empty.

    `sync_column` names the field that holds the sync-time map: a mapping on the
    reader path, JSON object text in `v_service_health`.
    """
    if row is None:
        return ServiceHealth(None, 0, {})
    age = _row_get(row, "beat_age_s")
    ticks = _row_get(row, "consecutive_failed_ticks")
    try:
        return ServiceHealth(
            beat_age_s=int(age) if age is not None else None,
            consecutive_failed_ticks=int(ticks or 0),
            viewer_sets_synced_at=_sync_times(_row_get(row, sync_column)),
        )
    except (TypeError, ValueError) as exc:
        raise PackratError(f"packrat health is malformed: {exc}") from exc


def _reviewer_node(row: RowLike) -> dict[str, Any]:
    reviewer = str(_row_get(row, "reviewer") or "")
    if _row_get(row, "reviewer_kind") == "Team":
        node: dict[str, Any] = {
            "requestedReviewer": {"slug": reviewer.split("/", 1)[-1]}
        }
    else:
        node = {"requestedReviewer": {"login": reviewer}}
    requested_at = _row_get(row, "requested_at")
    if requested_at:
        node["requestedAt"] = str(requested_at)
    return node


def _check_node(row: RowLike) -> dict[str, Any]:
    required = _bool(_row_get(row, "is_required"))
    if _row_get(row, "kind") == "StatusContext":
        return {
            "__typename": "StatusContext",
            "context": _row_get(row, "name"),
            "state": _row_get(row, "conclusion"),
            "targetUrl": _row_get(row, "details_url"),
            "createdAt": _row_get(row, "started_at"),
            "isRequired": required,
        }
    run_id = _row_get(row, "workflow_run_id")
    return {
        "__typename": "CheckRun",
        "name": _row_get(row, "name"),
        "status": _row_get(row, "status"),
        "conclusion": _row_get(row, "conclusion"),
        "detailsUrl": _row_get(row, "details_url"),
        "startedAt": _row_get(row, "started_at"),
        "completedAt": _row_get(row, "completed_at"),
        "isRequired": required,
        "checkSuite": {"workflowRun": {"databaseId": run_id}}
        if run_id is not None
        else None,
    }


def _thread_row_participants(row: RowLike) -> list[str]:
    raw = _row_get(row, "participants_json")
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x)]
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(x) for x in parsed if str(x)]


def _thread_node(row: RowLike) -> dict[str, Any]:
    author = {
        "login": _row_get(row, "last_author") or "",
        "__typename": _row_get(row, "last_author_type") or "User",
    }
    return {
        "id": _row_get(row, "thread_id"),
        "isResolved": _bool(_row_get(row, "is_resolved")),
        "isOutdated": _bool(_row_get(row, "is_outdated")),
        "participants": _thread_row_participants(row),
        "comments": {
            "nodes": [{"author": author, "createdAt": _row_get(row, "last_at")}]
        },
    }


def _comment_node(row: RowLike) -> dict[str, Any]:
    author_type = str(_row_get(row, "author_type") or "User")
    return {
        "id": str(_row_get(row, "comment_id") or ""),
        "createdAt": str(_row_get(row, "created_at") or ""),
        "author": {
            "login": str(_row_get(row, "author") or ""),
            "is_bot": str(_row_get(row, "author_type") or "") == "Bot",
            "__typename": author_type,
        },
    }


def _review_node(row: RowLike) -> dict[str, Any]:
    return {
        "state": _row_get(row, "state"),
        "author": {"login": _row_get(row, "author")},
        "submittedAt": _row_get(row, "submitted_at"),
    }


def _author_review_node(row: RowLike) -> dict[str, Any]:
    commit_oid = _row_get(row, "commit_oid")
    return {
        "state": _row_get(row, "state"),
        "submittedAt": _row_get(row, "submitted_at"),
        "commit": {"oid": commit_oid} if commit_oid else None,
        "author": {
            "login": _row_get(row, "author"),
            "__typename": _row_get(row, "author_type") or "User",
        },
    }


def _commit_node(live: RowLike, checks: list[dict[str, Any]]) -> dict[str, Any]:
    rollup = None
    rollup_state = _row_get(live, "rollup_state")
    if rollup_state is not None or checks:
        total = _row_get(live, "rollup_total")
        rollup = {
            "state": rollup_state,
            "contexts": {
                "totalCount": total if isinstance(total, int) else len(checks),
                "nodes": checks,
            },
        }
    return {
        "nodes": [
            {
                "commit": {
                    "oid": _row_get(live, "head_sha"),
                    "committedDate": _row_get(live, "head_committed_date"),
                    "committer": {"user": None},
                    "statusCheckRollup": rollup,
                }
            }
        ]
    }


def build_raw_pr(
    live: RowLike,
    checks: Sequence[RowLike],
    reviewers: Sequence[RowLike],
    reviews: Sequence[RowLike],
    threads: Sequence[RowLike],
    author_reviews: Sequence[RowLike] = (),
    comments: Sequence[RowLike] = (),
) -> dict[str, Any]:
    """Builds the GraphQL `PullRequest` node shape `normalize_pr()` reads from view rows."""
    auto_merge = _row_get(live, "auto_merge_enabled_at")
    state = _row_get(live, "state")
    return {
        "id": _row_get(live, "node_id"),
        "number": _row_get(live, "number"),
        "title": _row_get(live, "title"),
        "url": _row_get(live, "url"),
        "state": state,
        "isDraft": _bool(_row_get(live, "is_draft")),
        "merged": state == "MERGED",
        "mergedAt": _row_get(live, "merged_at"),
        "closedAt": _row_get(live, "closed_at"),
        "updatedAt": _row_get(live, "updated_at"),
        "author": {
            "login": _row_get(live, "author") or "",
            "__typename": _row_get(live, "author_type") or "User",
        },
        "headRefOid": _row_get(live, "head_sha"),
        "headRefName": _row_get(live, "head_ref"),
        "baseRefName": _row_get(live, "base_ref"),
        "mergeable": _row_get(live, "mergeable"),
        "mergeStateStatus": _row_get(live, "merge_state_status"),
        "reviewDecision": _row_get(live, "review_decision"),
        "autoMergeRequest": {"enabledAt": auto_merge} if auto_merge else None,
        "reviewRequests": {
            "totalCount": len(reviewers),
            "nodes": [_reviewer_node(r) for r in reviewers],
        },
        "assignees": {"nodes": []},
        "latestOpinionatedReviews": {"nodes": [_review_node(r) for r in reviews]},
        "reviews": {"nodes": [_author_review_node(r) for r in author_reviews]},
        "reviewThreads": {
            "totalCount": len(threads),
            "nodes": [_thread_node(t) for t in threads],
        },
        "comments": {"nodes": [_comment_node(c) for c in comments]},
        "commits": _commit_node(live, [_check_node(c) for c in checks]),
    }


def _rows_by_key(
    conn: sqlite3.Connection, sql: str, keys: Sequence[str]
) -> dict[str, list[sqlite3.Row]]:
    marks = ",".join("?" for _ in keys)
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute(sql.format(marks=marks), list(keys)):
        grouped.setdefault(str(row["item_key"]), []).append(row)
    return grouped


def _optional_rows_by_key(
    conn: sqlite3.Connection, sql: str, keys: Sequence[str]
) -> dict[str, list[sqlite3.Row]]:
    try:
        return _rows_by_key(conn, sql, keys)
    except sqlite3.OperationalError:
        return {}


def _group_bundle_rows(
    rows: Any, *, kind_filter: str | None = None
) -> dict[str, list[Mapping[str, Any]]]:
    if not isinstance(rows, list):
        return {}
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if kind_filter is not None and row.get("kind") not in (None, kind_filter):
            continue
        key = str(row.get("item_key") or "")
        if key:
            grouped.setdefault(key, []).append(row)
    return grouped


def _parse_viewer_row(
    row: Any, set_names: Sequence[str], repo_prefix: str
) -> tuple[str, str, str] | None:
    """Returns `(key, set_name, updated_at)` for an open member of `set_names`, else None."""
    if not isinstance(row, Mapping):
        return None
    set_name = str(row.get("set_name") or "")
    key = str(row.get("item_key") or "")
    if set_name not in set_names or not key:
        return None
    if repo_prefix and not key.lower().startswith(repo_prefix):
        return None
    state = row.get("state")
    if state is not None and str(state) != "OPEN":
        return None
    return key, set_name, str(row.get("updated_at") or row.get("last_seen_at") or "")


def _live_items_ops(keys: Sequence[str]) -> dict[str, Any]:
    return {
        "items": {
            "keys": list(keys),
            "include_live": True,
            "include_basic": False,
            "include_issues": False,
            "include_mentions": False,
        }
    }


class PackratSource:
    """Leases, change feed, and snapshots for /monitor over the packrat reader contract."""

    def __init__(
        self,
        db_path: Path,
        consumer_id: str,
        runner: PackratRunner | None = None,
        freshness_s: int = DEFAULT_FRESHNESS_S,
        ttl_s: int = DEFAULT_TTL_S,
    ) -> None:
        self.db_path = Path(db_path)
        self.consumer_id = consumer_id
        self.runner: Any = runner or SubprocessPackrat(self.db_path)
        self.freshness_s = freshness_s
        self.ttl_s = ttl_s
        self.watched: set[str] = set()
        self._cached_ages: dict[str, float | None] = {}
        self.last_health: ServiceHealth | None = None
        """The last health that `health()` or `discover_authored_keys()` read."""

    def _use_reader_cli(self) -> bool:
        return not _use_local_views(self.db_path)

    # ----- CLI operations -----------------------------------------------------------

    def _run_json(
        self, args: list[str], items: Sequence[str] = (), timeout_s: float = 30.0
    ) -> dict[str, Any]:
        """Runs `packrat <args> --json [-- <items>]` and returns the decoded JSON object.

        Item keys follow `--`, so a key can never be parsed as an option.
        """
        return _run_packrat_json(self.runner, args, items=items, timeout_s=timeout_s)

    def _reader_batch(
        self, ops: Mapping[str, Any], timeout_s: float = 30.0
    ) -> dict[str, Any]:
        return self._run_json(
            ["reader", "--ops-json", json.dumps(dict(ops))],
            timeout_s=timeout_s,
        )

    @property
    def session_id(self) -> str:
        """Alias for `consumer_id` matching the packrat watch session identifier."""
        return self.consumer_id

    def _watch_args(self, action: str) -> list[str]:
        return ["watch", action, "--consumer", CONSUMER, "--id", self.consumer_id]

    def lease(self, keys: Iterable[str]) -> None:
        """Leases `keys` for this monitor instance (`packrat watch add`)."""
        items = valid_item_keys(keys)
        if not items:
            return
        args = [*self._watch_args("add"), "--freshness", str(self.freshness_s)]
        self._run_json([*args, "--ttl", str(self.ttl_s)], items)
        self.watched.update(items)

    def unlease(self, keys: Iterable[str]) -> None:
        """Releases leases on `keys` for this monitor instance (`packrat watch remove`)."""
        items = valid_item_keys(keys)
        if not items:
            return
        self._run_json(self._watch_args("remove"), items)
        self.watched.difference_update(items)

    def renew(self) -> None:
        """Extends every lease of this instance by the TTL (`packrat watch renew`)."""
        self._run_json([*self._watch_args("renew"), "--ttl", str(self.ttl_s)])

    def release(self) -> None:
        """Removes every lease of this instance (`packrat watch remove`)."""
        self._run_json(self._watch_args("remove"))
        self.watched.clear()

    def refresh(self, keys: Iterable[str]) -> None:
        """Asks the service for an immediate refresh of `keys` (`packrat refresh`)."""
        items = valid_item_keys(keys)
        if items:
            self._cached_ages.clear()
            self._run_json(["refresh"], items)

    def wait_changes(
        self,
        since: float,
        timeout_s: float | None = None,
        *,
        watch_viewer_sets: bool = False,
    ) -> ChangeBatch:
        """Waits up to `timeout_s` for change rows after `since`, filtered to watched keys."""
        self._cached_ages.clear()
        if timeout_s is None:
            since_seq = 0
            wait = max(0.0, float(since))
        else:
            since_seq = int(since)
            wait = max(0.0, float(timeout_s))
        payload = self._run_json(
            ["changes", "--since", str(since_seq), "--wait", f"{wait:g}"],
            timeout_s=wait + 30.0,
        )
        seq = int(payload.get("seq") or since_seq)
        if payload.get("reset"):
            return ChangeBatch(
                seq=seq,
                keys=set(self.watched),
                reset=True,
                viewer_changed=bool(watch_viewer_sets),
            )
        raw_changes = [
            row for row in (payload.get("changes") or []) if isinstance(row, dict)
        ]
        viewer_changed = bool(
            watch_viewer_sets
            and any(row.get("kind") == "viewer" for row in raw_changes)
        )
        changed = {
            str(row.get("item_key"))
            for row in raw_changes
            if row.get("item_key") in self.watched
        }
        return ChangeBatch(seq=seq, keys=changed, viewer_changed=viewer_changed)

    # ----- reader / view queries ----------------------------------------------------

    def _snapshots_via_reader(self, keys: Sequence[str]) -> dict[str, dict[str, Any]]:
        payload = self._reader_batch(_live_items_ops(keys))
        raw_bundle = payload.get("items")
        bundle = raw_bundle if isinstance(raw_bundle, Mapping) else {}
        live = _group_bundle_rows(bundle.get("pr_live"))
        for req_key in keys:
            self._cached_ages[req_key] = None
        for item_key, live_rows in live.items():
            raw_age = live_rows[0].get("age_s")
            self._cached_ages[item_key] = float(raw_age) if raw_age is not None else 0.0
        checks = _group_bundle_rows(bundle.get("checks"))
        reviewers = _group_bundle_rows(bundle.get("review_requests"))
        reviews = _group_bundle_rows(bundle.get("latest_reviews"))
        author_reviews = _group_bundle_rows(bundle.get("latest_author_reviews"))
        threads = _group_bundle_rows(bundle.get("threads"))
        comments_by_key = _group_bundle_rows(bundle.get("comments"), kind_filter="pr")
        return {
            key: build_raw_pr(
                rows[0],
                checks.get(key, []),
                reviewers.get(key, []),
                reviews.get(key, []),
                threads.get(key, []),
                author_reviews.get(key, []),
                comments_by_key.get(key, []),
            )
            for key, rows in live.items()
        }

    def _snapshots_via_views(self, keys: Sequence[str]) -> dict[str, dict[str, Any]]:
        try:
            with closing(connect_readonly(self.db_path)) as conn:
                conn.execute("BEGIN")
                live = _rows_by_key(
                    conn, "SELECT * FROM v_pr_live WHERE item_key IN ({marks})", keys
                )
                extra = [
                    _rows_by_key(
                        conn,
                        f"SELECT * FROM {view} WHERE item_key IN ({{marks}})",
                        keys,
                    )
                    for view in _DETAIL_VIEWS
                ]
                author_reviews = _optional_rows_by_key(
                    conn,
                    "SELECT * FROM v_pr_latest_author_reviews WHERE item_key IN ({marks})",
                    keys,
                )
                comments_by_key = _optional_rows_by_key(
                    conn,
                    "SELECT * FROM v_item_comments WHERE kind = 'pr' AND item_key IN ({marks}) "
                    "ORDER BY created_at ASC",
                    keys,
                )
                conn.execute("COMMIT")
        except sqlite3.Error as exc:
            raise PackratError(f"packrat view read failed: {exc}") from exc
        checks, reviewers, reviews, threads = extra
        return {
            key: build_raw_pr(
                rows[0],
                checks.get(key, []),
                reviewers.get(key, []),
                reviews.get(key, []),
                threads.get(key, []),
                author_reviews.get(key, []),
                comments_by_key.get(key, []),
            )
            for key, rows in live.items()
        }

    def snapshots(self, keys: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Returns GraphQL-shaped PR nodes for the keys present in `pr_live` / `v_pr_live`."""
        if not keys:
            return {}
        if self._use_reader_cli():
            return self._snapshots_via_reader(keys)
        return self._snapshots_via_views(keys)

    def fetch_snapshots(self, keys: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Alias for `snapshots(keys)`."""
        return self.snapshots(keys)

    def _open_viewer_rows_via_reader(
        self, set_names: Sequence[str], cleaned_repo: str, with_health: bool
    ) -> tuple[list[tuple[str, str, str]], ServiceHealth | None]:
        spec: dict[str, Any] = {"sets": list(set_names)}
        if cleaned_repo:
            spec["repo"] = cleaned_repo
        ops: dict[str, Any] = {"viewer_items": spec}
        if with_health:
            ops["health"] = True
        payload = self._reader_batch(ops)
        raw_rows = payload.get("viewer_items")
        repo_prefix = f"{cleaned_repo.lower()}#" if cleaned_repo else ""
        rows: list[tuple[str, str, str]] = []
        for raw_row in raw_rows if isinstance(raw_rows, list) else []:
            parsed = _parse_viewer_row(raw_row, set_names, repo_prefix)
            if parsed is not None:
                rows.append(parsed)
        if not with_health:
            return rows, None
        raw_health = payload.get("health")
        health_row = raw_health if isinstance(raw_health, Mapping) else None
        return rows, _service_health(health_row, "viewer_sets_synced_at")

    def _open_viewer_rows_via_views(
        self, set_names: Sequence[str], cleaned_repo: str, with_health: bool
    ) -> tuple[list[tuple[str, str, str]], ServiceHealth | None]:
        marks = ",".join("?" for _ in set_names)
        sql = (
            "SELECT vi.item_key AS item_key, vi.set_name AS set_name, "
            "COALESCE(pl.updated_at, '') AS updated_at "
            "FROM v_viewer_items vi "
            "LEFT JOIN v_pr_live pl ON pl.item_key = vi.item_key "
            f"WHERE vi.set_name IN ({marks}) "
            "AND (pl.state IS NULL OR pl.state = 'OPEN')"
        )
        params: list[Any] = list(set_names)
        if cleaned_repo:
            sql += " AND LOWER(vi.item_key) LIKE LOWER(?) || '#%'"
            params.append(cleaned_repo)
        health_row = None
        try:
            with closing(connect_readonly(self.db_path)) as conn:
                conn.execute("BEGIN")
                found = conn.execute(sql, params).fetchall()
                if with_health:
                    health_row = conn.execute(
                        "SELECT * FROM v_service_health"
                    ).fetchone()
                conn.execute("COMMIT")
        except sqlite3.Error as exc:
            raise PackratError(f"packrat view read failed: {exc}") from exc
        rows = [
            (str(row["item_key"]), str(row["set_name"]), str(row["updated_at"]))
            for row in found
        ]
        if not with_health:
            return rows, None
        return rows, _service_health(health_row, "viewer_sets_synced_json")

    def _open_viewer_rows(
        self, set_names: Sequence[str], repo: str | None, *, with_health: bool = False
    ) -> tuple[list[tuple[str, str, str]], ServiceHealth | None]:
        """Returns `(key, set_name, updated_at)` for each open member of `set_names`.

        A member counts as open when its live row says `OPEN` or it has no live
        row yet. `repo` limits the rows to one repository. `updated_at` is `""`
        when unknown.

        With `with_health`, the service health is read together with the rows:
        in the same reader batch, or in the same transaction on a database file.
        """
        cleaned_repo = (repo or "").strip()
        if self._use_reader_cli():
            return self._open_viewer_rows_via_reader(
                set_names, cleaned_repo, with_health
            )
        return self._open_viewer_rows_via_views(set_names, cleaned_repo, with_health)

    def discover_review_keys(self, repo: str | None = None) -> list[str]:
        """Returns open PR keys in the viewer's `review_requested` and `reviewed` sets.

        Requested reviews come first, then the most recently updated.
        """
        rows, _ = self._open_viewer_rows(REVIEW_SETS, repo)
        priority: dict[str, int] = {}
        updated: dict[str, str] = {}
        for key, set_name, row_updated in rows:
            prio = 0 if set_name == "review_requested" else 1
            priority[key] = min(priority.get(key, 1), prio)
            updated[key] = max(updated.get(key, ""), row_updated)
        keys = sorted(priority)
        keys.sort(key=lambda k: updated[k], reverse=True)
        keys.sort(key=lambda k: priority[k])
        return keys

    def discover_authored_keys(self, repo: str | None = None) -> list[str]:
        """Returns every key in the viewer's `authored_open` set, newest update first.

        Keys with the same update time are ordered by key. `repo` limits the
        result to one repository. The result is not capped.

        Raises:
            ViewerSetUnavailable: The service has never synced `authored_open`.
                The check covers the whole set, whatever `repo` is.
        """
        rows, health = self._open_viewer_rows(
            (AUTHORED_OPEN_SET,), repo, with_health=True
        )
        if health is not None:
            self.last_health = health
        if health is None or AUTHORED_OPEN_SET not in health.viewer_sets_synced_at:
            raise ViewerSetUnavailable(
                f"packrat has never synced the `{AUTHORED_OPEN_SET}` viewer set, so "
                "the running service is too old or has not finished its first tick. "
                "Restart it on the current code with `packrat auto-sync on`, then retry."
            )
        updated: dict[str, str] = {}
        for key, _, row_updated in rows:
            updated[key] = max(updated.get(key, ""), row_updated)
        keys = sorted(updated)
        keys.sort(key=lambda k: updated[k], reverse=True)
        return keys

    def known_keys(self, keys: Sequence[str]) -> dict[str, str]:
        """Maps each key present in `v_pr_live` or `v_pr_basic` to its canonical `owner/repo#N`."""
        if not keys:
            return {}
        if self._use_reader_cli():
            raw = self._reader_batch({"known_keys": {"keys": list(keys)}}).get(
                "known_keys"
            )
            if not isinstance(raw, Mapping):
                return {}
            return {str(k): str(v) for k, v in raw.items() if v}
        marks = ",".join("?" for _ in keys)
        out: dict[str, str] = {}
        try:
            with closing(connect_readonly(self.db_path)) as conn:
                for view in ("v_pr_basic", "v_pr_live"):
                    sql = f"SELECT item_key, repo_full_name, number, url FROM {view} WHERE item_key IN ({marks})"
                    for row in conn.execute(sql, list(keys)).fetchall():
                        key, canon = _canonical_key_from_row(row)
                        out[key] = canon
        except sqlite3.Error as exc:
            raise PackratError(f"packrat view read failed: {exc}") from exc
        return out

    def _fetched_ages_via_reader(self, keys: Sequence[str]) -> dict[str, float]:
        if all(k in self._cached_ages for k in keys):
            return {
                k: age for k in keys if (age := self._cached_ages.get(k)) is not None
            }
        payload = self._reader_batch(_live_items_ops(keys))
        bundle = payload.get("items")
        live = _group_bundle_rows(
            bundle.get("pr_live") if isinstance(bundle, Mapping) else None
        )
        for req_key in keys:
            self._cached_ages[req_key] = None
        out: dict[str, float] = {}
        for item_key, rows in live.items():
            raw_age = rows[0].get("age_s")
            age = float(raw_age) if raw_age is not None else 0.0
            self._cached_ages[item_key] = age
            out[item_key] = age
        return out

    def fetched_ages(self, keys: Sequence[str]) -> dict[str, float]:
        """Returns seconds since each key's `live_fetched_at`, for keys in `pr_live`."""
        if not keys:
            return {}
        if self._use_reader_cli():
            return self._fetched_ages_via_reader(keys)
        marks = ",".join("?" for _ in keys)
        sql = (
            "SELECT item_key, (julianday('now') - julianday(live_fetched_at)) * 86400 AS age "
            f"FROM v_pr_live WHERE item_key IN ({marks})"
        )
        try:
            with closing(connect_readonly(self.db_path)) as conn:
                rows = conn.execute(sql, list(keys)).fetchall()
        except sqlite3.Error as exc:
            raise PackratError(f"packrat view read failed: {exc}") from exc
        return {str(r["item_key"]): float(r["age"] or 0.0) for r in rows}

    def viewer_login(self) -> str:
        """Returns the authenticated user's login, or "" when packrat can't tell.

        Prefers `health.viewer_login` (`v_service_health.viewer_login`), then on local
        SQLite fixtures the author of a PR in the viewer's `authored` set.
        """
        if self._use_reader_cli():
            try:
                health = self._reader_batch({"health": True}).get("health")
            except PackratError as exc:
                if "auth_expired" in str(exc):
                    raise
                return ""
            login = health.get("viewer_login") if isinstance(health, Mapping) else None
            return str(login) if login else ""
        queries = (
            "SELECT viewer_login AS login FROM v_service_health",
            (
                "SELECT b.author AS login FROM v_viewer_items v "
                "JOIN v_pr_basic b ON b.item_key = v.item_key "
                "WHERE v.set_name = 'authored' AND b.author IS NOT NULL LIMIT 1"
            ),
        )
        try:
            with closing(connect_readonly(self.db_path)) as conn:
                for sql in queries:
                    login = _first_login(conn, sql)
                    if login:
                        return login
        except PackratError:
            return ""
        return ""

    def health(self) -> ServiceHealth:
        """Reads service health via the reader CLI, or from a database file's views.

        Raises:
            PackratError: The reader CLI or the database read failed.
        """
        if self._use_reader_cli():
            raw = self._reader_batch({"health": True}, timeout_s=15.0).get("health")
            row = raw if isinstance(raw, Mapping) else None
            self.last_health = _service_health(row, "viewer_sets_synced_at")
            return self.last_health
        try:
            with closing(connect_readonly(self.db_path)) as conn:
                row = conn.execute("SELECT * FROM v_service_health").fetchone()
        except sqlite3.Error as exc:
            raise PackratError(f"packrat view read failed: {exc}") from exc
        self.last_health = _service_health(row, "viewer_sets_synced_json")
        return self.last_health

    # ----- stack discovery ----------------------------------------------------------

    def stack_neighbors(self, key: str) -> list[str] | None:
        """Returns the stack around `key` (parents root-first, `key`, then children).

        Returns None unless `key` is in a `mirror` repository, so the caller falls
        back to `gh`.
        """
        repo, _ = parse_pr_ref(key)
        if self._use_reader_cli():
            try:
                neighbors = self._reader_batch(
                    {"stack_neighbors": {"keys": [key]}}
                ).get("stack_neighbors")
            except PackratError:
                return None
            if not isinstance(neighbors, Mapping):
                return None
            val = neighbors.get(key)
            return [str(x) for x in val] if isinstance(val, list) else None
        try:
            with closing(connect_readonly(self.db_path)) as conn:
                me = conn.execute(
                    "SELECT * FROM v_pr_basic WHERE item_key = ?", (key,)
                ).fetchone()
                if me is None or me["repo_mode"] != "mirror":
                    return None
                parents = _walk_parents(conn, repo, str(me["base_ref"] or ""), key)
                children = _children(conn, repo, me)
        except (PackratError, sqlite3.Error):
            return None
        return [*parents, key, *children]


_DETAIL_VIEWS = (
    "v_pr_checks",
    "v_pr_review_requests",
    "v_pr_latest_reviews",
    "v_pr_threads",
)


def _canonical_key_from_row(row: sqlite3.Row) -> tuple[str, str]:
    key = str(row["item_key"])
    url = str(row["url"] or "").strip()
    repo = str(row["repo_full_name"] or "")
    num = int(row["number"])
    if url:
        try:
            repo, num = parse_pr_ref(url)
        except ValueError:
            pass
    return key, f"{repo}#{num}" if repo else key


def _first_login(conn: sqlite3.Connection, sql: str) -> str:
    try:
        row = conn.execute(sql).fetchone()
    except sqlite3.Error:
        return ""
    return str(row["login"]) if row is not None and row["login"] else ""


def gh_config_user(config_dir: Path | None = None) -> str:
    """Returns the `github.com` user from gh's `hosts.yml` without calling GitHub, or ""."""
    base = config_dir or Path(
        os.environ.get("GH_CONFIG_DIR") or Path.home() / ".config" / "gh"
    )
    try:
        lines = (Path(base) / "hosts.yml").read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    in_github = False
    for line in lines:
        if line and not line[0].isspace():
            in_github = line.strip().rstrip(":") == "github.com"
            continue
        key, _, value = line.strip().partition(":")
        if in_github and key == "user" and value.strip():
            return value.strip().strip("'\"")
    return ""


_SAME_REPO_OPEN = (
    "SELECT item_key, base_ref FROM v_pr_basic WHERE repo_full_name = ? AND state = 'OPEN' "
    "AND COALESCE(is_cross_repository, 0) = 0"
)


def _walk_parents(
    conn: sqlite3.Connection, repo: str, base_ref: str, key: str
) -> list[str]:
    chain: list[str] = []
    seen = {key}
    cur_base = base_ref
    while cur_base and cur_base not in BASE_BRANCHES and len(chain) < MAX_STACK_DEPTH:
        row = conn.execute(
            f"{_SAME_REPO_OPEN} AND head_ref = ? ORDER BY number DESC LIMIT 1",
            (repo, cur_base),
        ).fetchone()
        if row is None or row["item_key"] in seen:
            break
        seen.add(str(row["item_key"]))
        chain.append(str(row["item_key"]))
        cur_base = str(row["base_ref"] or "")
    return list(reversed(chain))


def _children(conn: sqlite3.Connection, repo: str, me: sqlite3.Row) -> list[str]:
    head_ref = str(me["head_ref"] or "")
    if not head_ref or me["is_cross_repository"] == 1:
        return []
    rows = conn.execute(
        "SELECT item_key FROM v_pr_basic WHERE repo_full_name = ? AND state = 'OPEN' "
        "AND base_ref = ? ORDER BY number",
        (repo, head_ref),
    ).fetchall()
    return [str(r["item_key"]) for r in rows if r["item_key"] != me["item_key"]]
