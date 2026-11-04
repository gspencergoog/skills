"""Reads live pull request data for /monitor from a running `packrat serve`.

This module follows the packrat reader contract
(`agents/skills/packrat/references/reader_contract.md`) and never imports packrat
code. It reads the contract's SQLite views through a read-only connection
(`file:<db>?mode=ro`) and writes only through the `packrat` CLI: `watch` leases,
`refresh` requests, and the `changes` feed.

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
from collections.abc import Callable, Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from models import parse_pr_ref

DEFAULT_PACKRAT_DB = Path.home() / ".local" / "share" / "packrat" / "repos.sqlite"
STALE_HEARTBEAT_S = 180
"""The service counts as down when `v_service_health.beat_age_s` is NULL or above this."""
CONSUMER = "monitor"
DEFAULT_FRESHNESS_S = 120
DEFAULT_TTL_S = 900
MAX_STACK_DEPTH = 10
BASE_BRANCHES = frozenset({"main", "master", "develop"})
_PACKRAT_SCRIPT = Path(__file__).resolve().parents[2] / "packrat" / "scripts" / "packrat.py"

_ITEM_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+#[0-9]+$")

PackratRunner = Callable[[list[str], float], tuple[int, str, str]]
"""Runs `packrat <args>` with a timeout in seconds and returns `(code, stdout, stderr)`."""


class PackratError(Exception):
    """Raised when the packrat CLI or database can't serve a request."""


@dataclass(frozen=True)
class ChangeBatch:
    """Result of one `packrat changes` call.

    Attributes:
        seq: The feed position to pass as the next `since`.
        keys: Watched item keys with at least one change row.
        reset: True when `since` fell behind the retained feed; take a full snapshot.
    """

    seq: int
    keys: set[str] = field(default_factory=set)
    reset: bool = False


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


def heartbeat_age(db_path: Path) -> int | None:
    """Returns `v_service_health.beat_age_s`, or None when unknown or unreadable."""
    try:
        with closing(connect_readonly(db_path)) as conn:
            row = conn.execute("SELECT beat_age_s FROM v_service_health").fetchone()
    except (PackratError, sqlite3.Error):
        return None
    if row is None or row["beat_age_s"] is None:
        return None
    return int(row["beat_age_s"])


def available(db_path: Path) -> bool:
    """Returns True when the database exists and the service heartbeat is fresh."""
    age = heartbeat_age(db_path)
    return age is not None and age <= STALE_HEARTBEAT_S


class SubprocessPackrat:
    """Runs the `packrat` CLI (from PATH, else the sibling skill's `packrat.py`)."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        exe = shutil.which("packrat")
        self.prefix = [exe] if exe else [sys.executable, str(_PACKRAT_SCRIPT)]

    def __call__(self, args: list[str], timeout_s: float) -> tuple[int, str, str]:
        argv = [*self.prefix, "--db", str(self.db_path), *args]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout_s, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise PackratError(f"packrat {args[0]} timed out after {timeout_s:.0f}s") from exc
        except OSError as exc:
            raise PackratError(f"cannot run packrat: {exc}") from exc
        return proc.returncode, proc.stdout, proc.stderr


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


def _reviewer_node(reviewer: str, kind: str | None) -> dict[str, Any]:
    if kind == "Team":
        return {"requestedReviewer": {"slug": reviewer.split("/", 1)[-1]}}
    return {"requestedReviewer": {"login": reviewer}}


def _check_node(row: sqlite3.Row) -> dict[str, Any]:
    required = _bool(row["is_required"])
    if row["kind"] == "StatusContext":
        return {
            "__typename": "StatusContext",
            "context": row["name"],
            "state": row["conclusion"],
            "targetUrl": row["details_url"],
            "createdAt": row["started_at"],
            "isRequired": required,
        }
    run_id = row["workflow_run_id"]
    return {
        "__typename": "CheckRun",
        "name": row["name"],
        "status": row["status"],
        "conclusion": row["conclusion"],
        "detailsUrl": row["details_url"],
        "startedAt": row["started_at"],
        "completedAt": row["completed_at"],
        "isRequired": required,
        "checkSuite": {"workflowRun": {"databaseId": run_id}} if run_id is not None else None,
    }


def _thread_node(row: sqlite3.Row) -> dict[str, Any]:
    author = {
        "login": row["last_author"] or "",
        "__typename": row["last_author_type"] or "User",
    }
    return {
        "id": row["thread_id"],
        "isResolved": _bool(row["is_resolved"]),
        "comments": {"nodes": [{"author": author, "createdAt": row["last_at"]}]},
    }


def _review_node(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "state": row["state"],
        "author": {"login": row["author"]},
        "submittedAt": row["submitted_at"],
    }


def _commit_node(live: sqlite3.Row, checks: list[dict[str, Any]]) -> dict[str, Any]:
    rollup = None
    if live["rollup_state"] is not None or checks:
        total = live["rollup_total"]
        rollup = {
            "state": live["rollup_state"],
            "contexts": {
                "totalCount": total if isinstance(total, int) else len(checks),
                "nodes": checks,
            },
        }
    return {
        "nodes": [
            {
                "commit": {
                    "oid": live["head_sha"],
                    "committedDate": live["head_committed_date"],
                    "committer": {"user": None},
                    "statusCheckRollup": rollup,
                }
            }
        ]
    }


def build_raw_pr(
    live: sqlite3.Row,
    checks: list[sqlite3.Row],
    reviewers: list[sqlite3.Row],
    reviews: list[sqlite3.Row],
    threads: list[sqlite3.Row],
) -> dict[str, Any]:
    """Builds the GraphQL `PullRequest` node shape `normalize_pr()` reads from view rows."""
    auto_merge = live["auto_merge_enabled_at"]
    return {
        "id": live["node_id"],
        "number": live["number"],
        "title": live["title"],
        "url": live["url"],
        "state": live["state"],
        "isDraft": _bool(live["is_draft"]),
        "merged": live["state"] == "MERGED",
        "mergedAt": live["merged_at"],
        "closedAt": live["closed_at"],
        "updatedAt": live["updated_at"],
        "author": {
            "login": live["author"] or "",
            "__typename": live["author_type"] or "User",
        },
        "headRefOid": live["head_sha"],
        "headRefName": live["head_ref"],
        "baseRefName": live["base_ref"],
        "mergeable": live["mergeable"],
        "mergeStateStatus": live["merge_state_status"],
        "reviewDecision": live["review_decision"],
        "autoMergeRequest": {"enabledAt": auto_merge} if auto_merge else None,
        "reviewRequests": {
            "totalCount": len(reviewers),
            "nodes": [_reviewer_node(r["reviewer"], r["reviewer_kind"]) for r in reviewers],
        },
        "assignees": {"nodes": []},
        "latestOpinionatedReviews": {"nodes": [_review_node(r) for r in reviews]},
        "reviewThreads": {
            "totalCount": len(threads),
            "nodes": [_thread_node(t) for t in threads],
        },
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
        self.runner: PackratRunner = runner or SubprocessPackrat(self.db_path)
        self.freshness_s = freshness_s
        self.ttl_s = ttl_s
        self.watched: set[str] = set()

    # ----- CLI writes ---------------------------------------------------------------

    def _run_json(
        self, args: list[str], items: Sequence[str] = (), timeout_s: float = 30.0
    ) -> dict[str, Any]:
        """Runs `packrat <args> --json [-- <items>]` and returns the decoded JSON object.

        Item keys follow `--`, so a key can never be parsed as an option.
        """
        argv = [*args, "--json", "--", *items] if items else [*args, "--json"]
        code, stdout, stderr = self.runner(argv, timeout_s)
        if code != 0:
            detail = (stderr or stdout).strip().splitlines()
            raise PackratError(f"packrat {args[0]} failed (exit {code}): {detail[-1:]}")
        try:
            payload = json.loads(stdout or "{}")
        except json.JSONDecodeError as exc:
            raise PackratError(f"packrat {args[0]} returned invalid JSON: {exc}") from exc
        return payload if isinstance(payload, dict) else {}

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
            self._run_json(["refresh"], items)

    def wait_changes(self, since: int, timeout_s: float) -> ChangeBatch:
        """Waits up to `timeout_s` for change rows after `since`, filtered to watched keys."""
        wait = max(0.0, float(timeout_s))
        payload = self._run_json(
            ["changes", "--since", str(since), "--wait", f"{wait:g}"],
            timeout_s=wait + 30.0,
        )
        seq = int(payload.get("seq") or since)
        if payload.get("reset"):
            return ChangeBatch(seq=seq, keys=set(self.watched), reset=True)
        changed = {
            str(row.get("item_key"))
            for row in payload.get("changes") or []
            if isinstance(row, dict) and row.get("item_key") in self.watched
        }
        return ChangeBatch(seq=seq, keys=changed)

    # ----- view reads ---------------------------------------------------------------

    def snapshots(self, keys: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Returns GraphQL-shaped PR nodes for the keys present in `v_pr_live`."""
        if not keys:
            return {}
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
            )
            for key, rows in live.items()
        }

    def known_keys(self, keys: Sequence[str]) -> dict[str, str]:
        """Maps each key present in `v_pr_live` or `v_pr_basic` to its canonical `owner/repo#N`."""
        if not keys:
            return {}
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

    def fetched_ages(self, keys: Sequence[str]) -> dict[str, float]:
        """Returns seconds since each key's `live_fetched_at`, for keys in `v_pr_live`."""
        if not keys:
            return {}
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

        Prefers `v_service_health.viewer_login` (newer packrat), then the author of
        a PR in the viewer's `authored` set.
        """
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

    # ----- stack discovery ----------------------------------------------------------

    def stack_neighbors(self, key: str) -> list[str] | None:
        """Returns the stack around `key` (parents root-first, `key`, then children).

        Returns None unless `key` is in a `mirror` repository in `v_pr_basic`, so the
        caller falls back to `gh`. Parents are open same-repository PRs (fork heads
        excluded; NULL `is_cross_repository` counts as same-repository) whose
        `head_ref` equals the current `base_ref`, up to 10 levels. Children are open
        PRs whose `base_ref` equals this PR's `head_ref`, only when this PR's head is
        not on a fork.
        """
        repo, _ = parse_pr_ref(key)
        try:
            with closing(connect_readonly(self.db_path)) as conn:
                me = conn.execute("SELECT * FROM v_pr_basic WHERE item_key = ?", (key,)).fetchone()
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
    base = config_dir or Path(os.environ.get("GH_CONFIG_DIR") or Path.home() / ".config" / "gh")
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


def _walk_parents(conn: sqlite3.Connection, repo: str, base_ref: str, key: str) -> list[str]:
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
