"""Unit tests for packrat_source.py (the /monitor reader of the packrat contract)."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
import unittest.mock
from collections.abc import Callable, Iterator, Mapping
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from models import ViewerReview, parse_pr_ref
from normalize import _thread_participants, normalize_pr
from packrat_source import (
    DEFAULT_PACKRAT_DB,
    PackratError,
    PackratSource,
    ServiceHealth,
    SubprocessPackrat,
    ViewerSetUnavailable,
    _is_default_db_path,
    available,
    gh_config_user,
    heartbeat_age,
)
from testing import FIXTURES_DIR, load_fixture

# ----- helpers shared with test_monitor_prs.py ---------------------------------------


def make_packrat_db(
    path: Path,
    beat_age_s: int | None = 5,
    viewer: str | None = "alice",
    synced_sets: Mapping[str, str] | None = None,
) -> Path:
    """Creates a database with the contract views (as tables) and a service heartbeat.

    `synced_sets` maps each viewer set that has synced to its sync time. It fills
    `v_service_health.viewer_sets_synced_json`, which is `'{}'` without it.
    """
    conn = sqlite3.connect(path)
    conn.executescript((FIXTURES_DIR / "packrat_views.sql").read_text(encoding="utf-8"))
    conn.execute(
        "INSERT INTO v_service_health "
        "(pid, beat_age_s, viewer_login, active_leases, viewer_sets_synced_json) "
        "VALUES (4242, ?, ?, 0, ?)",
        (beat_age_s, viewer, json.dumps(dict(synced_sets or {}))),
    )
    conn.commit()
    conn.close()
    return path


def set_synced_sets(path: Path, synced_sets: Mapping[str, str]) -> None:
    """Replaces the viewer set sync times that the service health row reports."""
    with _db(path) as conn:
        conn.execute(
            "UPDATE v_service_health SET viewer_sets_synced_json = ?",
            (json.dumps(dict(synced_sets)),),
        )


def set_failed_ticks(path: Path, ticks: int) -> None:
    with _db(path) as conn:
        conn.execute(
            "UPDATE v_service_health SET consecutive_failed_ticks = ?", (ticks,)
        )


def set_heartbeat(path: Path, beat_age_s: int | None) -> None:
    with _db(path) as conn:
        conn.execute("UPDATE v_service_health SET beat_age_s = ?", (beat_age_s,))


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_row(key: str, head: str, node: dict[str, Any]) -> tuple[Any, ...]:
    required = node.get("isRequired")
    req = None if required is None else int(bool(required))
    if node["__typename"] == "StatusContext":
        run_key = f"StatusContext:{node['context']}"
        return (key, head, run_key, "StatusContext", node["context"], None, node["state"],
                node.get("targetUrl"), node.get("createdAt"), None, req, None)  # fmt: skip
    run = ((node.get("checkSuite") or {}).get("workflowRun") or {}).get("databaseId")
    run_key = f"CheckRun:{node.get('databaseId') or node['name']}"
    return (key, head, run_key, "CheckRun", node["name"], node.get("status"),
            node.get("conclusion"), node.get("detailsUrl"), node.get("startedAt"),
            node.get("completedAt"), req, run)  # fmt: skip


def _requested_at_map(pr: dict[str, Any], repo: str) -> dict[str, str]:
    owner = repo.split("/")[0]
    nodes = (
        ((pr.get("timelineItems") or {}).get("nodes") or [])
        if isinstance(pr, dict)
        else []
    )
    out: dict[str, str] = {}
    for node in nodes:
        if not isinstance(node, dict) or not node.get("createdAt"):
            continue
        req = node.get("requestedReviewer") or {}
        ident = (
            f"{owner}/{req['slug']}" if req.get("slug") else str(req.get("login") or "")
        ).lower()
        created_at = str(node["createdAt"])
        if ident and created_at >= out.get(ident, ""):
            out[ident] = created_at
    return out


def _reviewer_row(
    key: str, repo: str, node: dict[str, Any], req_map: dict[str, str] | None = None
) -> tuple[str, str, str, str | None]:
    req = node["requestedReviewer"]
    if req.get("slug"):
        reviewer = f"{repo.split('/')[0]}/{req['slug']}"
        kind = "Team"
    else:
        reviewer = req["login"]
        kind = "User"
    requested_at = node.get("requestedAt") or (req_map or {}).get(reviewer.lower())
    return key, reviewer, kind, requested_at


def _thread_row(key: str, node: dict[str, Any]) -> tuple[Any, ...]:
    last = node["comments"]["nodes"][-1]
    author = last.get("author") or {}
    participants_json = json.dumps(list(_thread_participants(node)))
    return (
        key,
        node["id"],
        int(bool(node.get("isResolved", False))),
        int(bool(node.get("isOutdated", False))),
        author.get("login"),
        author.get("__typename"),
        last.get("createdAt"),
        participants_json,
    )


def _comment_row(key: str, node: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    author = node.get("author") or {}
    return (
        key,
        "pr",
        str(node.get("id") or ""),
        str(author.get("login") or ""),
        str(author.get("__typename") or "User"),
        str(node.get("createdAt") or ""),
    )


def _head_commit(pr: dict[str, Any]) -> dict[str, Any]:
    nodes = (pr.get("commits") or {}).get("nodes") or []
    return nodes[-1]["commit"] if nodes else {}


@contextlib.contextmanager
def _db(path: Path) -> Iterator[sqlite3.Connection]:
    """Opens `path`, commits on success, and always closes the connection."""
    conn = sqlite3.connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _live_row(
    key: str, repo: str, mode: str, pr: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    commit = _head_commit(pr)
    rollup = commit.get("statusCheckRollup") or {}
    auto = pr.get("autoMergeRequest")
    row = {
        "item_key": key, "repo_full_name": repo, "repo_mode": mode, "number": pr["number"],
        "node_id": pr["id"], "title": pr["title"], "url": pr["url"],
        "state": "MERGED" if pr.get("merged") else pr["state"], "is_draft": int(pr["isDraft"]),
        "author": pr["author"]["login"], "author_type": pr["author"]["__typename"],
        "base_ref": pr["baseRefName"], "head_ref": pr["headRefName"], "head_sha": pr["headRefOid"],
        "head_committed_date": commit.get("committedDate"), "head_repo_owner": repo.split("/")[0],
        "is_cross_repository": 0, "mergeable": pr["mergeable"],
        "merge_state_status": pr["mergeStateStatus"], "review_decision": pr["reviewDecision"],
        "auto_merge_enabled_at": auto.get("enabledAt") if auto else None,
        "updated_at": pr["updatedAt"], "merged_at": pr.get("mergedAt"),
        "closed_at": pr.get("closedAt"), "live_fetched_at": _utc_now(),
        "rollup_state": rollup.get("state"),
        "rollup_total": (rollup.get("contexts") or {}).get("totalCount"),
    }  # fmt: skip
    row.update(extra)
    return row


def _insert(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    cols = ",".join(row)
    marks = ",".join("?" for _ in row)
    conn.execute(
        f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({marks})", list(row.values())
    )


def _author_review_rows(
    key: str, repo: str, number: int, pr: dict[str, Any]
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for node in (pr.get("reviews") or {}).get("nodes") or []:
        login = (node.get("author") or {}).get("login")
        if node.get("state") == "PENDING" or not login:
            continue
        rows.append(
            (
                key,
                repo,
                number,
                login,
                (node.get("author") or {}).get("__typename") or "User",
                node["state"],
                node.get("submittedAt"),
                (node.get("commit") or {}).get("oid") or None,
            )
        )
    return rows


def _insert_pr_relations(
    conn: sqlite3.Connection,
    key: str,
    repo: str,
    number: int,
    head: str,
    pr: dict[str, Any],
) -> None:
    contexts = _head_commit(pr).get("statusCheckRollup") or {}
    req_map = _requested_at_map(pr, repo)
    conn.executemany(
        "INSERT INTO v_pr_checks ("
        "item_key, head_oid, run_key, kind, name, status, conclusion, "
        "details_url, started_at, completed_at, is_required, workflow_run_id"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            _check_row(key, head, n)
            for n in (contexts.get("contexts") or {}).get("nodes", [])
        ],
    )
    conn.executemany(
        "INSERT INTO v_pr_review_requests (item_key, reviewer, reviewer_kind, requested_at) "
        "VALUES (?,?,?,?)",
        [_reviewer_row(key, repo, n, req_map) for n in pr["reviewRequests"]["nodes"]],
    )
    conn.executemany(
        "INSERT INTO v_pr_latest_reviews (item_key, author, author_type, state, submitted_at) "
        "VALUES (?,?,?,?,?)",
        [
            (key, n["author"]["login"], "User", n["state"], n.get("submittedAt"))
            for n in pr["latestOpinionatedReviews"]["nodes"]
        ],
    )
    conn.executemany(
        "INSERT INTO v_pr_latest_author_reviews ("
        "item_key, repo_full_name, pr_number, author, author_type, state, submitted_at, commit_oid"
        ") VALUES (?,?,?,?,?,?,?,?)",
        _author_review_rows(key, repo, number, pr),
    )
    conn.executemany(
        "INSERT INTO v_pr_threads ("
        "item_key, thread_id, is_resolved, is_outdated, last_author, "
        "last_author_type, last_at, participants_json"
        ") VALUES (?,?,?,?,?,?,?,?)",
        [_thread_row(key, n) for n in pr["reviewThreads"]["nodes"]],
    )
    comment_nodes = (pr.get("comments") or {}).get("nodes") or []
    conn.executemany(
        "INSERT INTO v_item_comments (item_key, kind, comment_id, author, author_type, created_at) "
        "VALUES (?,?,?,?,?,?)",
        [_comment_row(key, n) for n in comment_nodes],
    )


def store_graphql_pr(
    path: Path, pr: dict[str, Any], mode: str = "mirror", **live_extra: Any
) -> str:
    """Writes one GraphQL PR node into the views the way packrat's hot worker stores it."""
    repo, number = parse_pr_ref(pr["url"])
    key = f"{repo}#{number}"
    live = _live_row(key, repo, mode, pr, **live_extra)
    basic_cols = (
        "item_key",
        "repo_full_name",
        "repo_mode",
        "number",
        "title",
        "state",
        "is_draft",
        "url",
        "author",
        "updated_at",
        "merged_at",
        "closed_at",
        "base_ref",
        "head_ref",
        "head_sha",
        "head_repo_owner",
        "is_cross_repository",
    )
    with _db(path) as conn:
        for table in (
            "v_pr_checks",
            "v_pr_review_requests",
            "v_pr_latest_reviews",
            "v_pr_latest_author_reviews",
            "v_pr_threads",
            "v_item_comments",
        ):
            conn.execute(f"DELETE FROM {table} WHERE item_key = ?", (key,))
        _insert(conn, "v_pr_live", live)
        _insert(conn, "v_pr_basic", {c: live[c] for c in basic_cols})
        _insert_pr_relations(conn, key, repo, number, str(live["head_sha"]), pr)
    return key


def store_fixture_prs(path: Path, fixture: str, mode: str = "mirror") -> list[str]:
    """Stores every `prN` node of a GraphQL fixture; returns their item keys."""
    data = json.loads(load_fixture(fixture))["data"]
    nodes = [v["pullRequest"] for k, v in sorted(data.items()) if k.startswith("pr")]
    return [store_graphql_pr(path, node, mode) for node in nodes]


def add_basic_pr(
    path: Path,
    key: str,
    base_ref: str,
    head_ref: str,
    *,
    state: str = "OPEN",
    cross: int | None = 0,
    mode: str = "mirror",
) -> None:
    repo, number = parse_pr_ref(key)
    row = {
        "item_key": key, "repo_full_name": repo, "repo_mode": mode, "number": number,
        "title": f"PR {number}", "state": state, "is_draft": 0, "author": "alice",
        "base_ref": base_ref, "head_ref": head_ref, "is_cross_repository": cross,
    }  # fmt: skip
    with _db(path) as conn:
        _insert(conn, "v_pr_basic", row)


class FakePackratCli:
    """Scripted `packrat` CLI: records calls and answers `changes` from a queue.

    Each queued entry is a payload dict or a callable returning one (to mutate the
    database the way the service would before answering). An empty queue answers
    "no changes" after calling `on_wait(seconds)`.
    """

    def __init__(self, on_wait: Callable[[float], None] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.changes: list[dict[str, Any] | Callable[[], dict[str, Any]]] = []
        self.head_seq = 10
        self.on_wait = on_wait
        self.fail_next: str | None = None

    def __call__(self, args: list[str], timeout_s: float) -> tuple[int, str, str]:
        self.calls.append(list(args))
        if self.fail_next is not None:
            message, self.fail_next = self.fail_next, None
            return 1, "", message
        if args[0] != "changes":
            return 0, json.dumps({"ok": True}), ""
        since = int(args[args.index("--since") + 1])
        wait = float(args[args.index("--wait") + 1])
        if since > self.head_seq:
            return 0, json.dumps({"reset": True, "seq": self.head_seq}), ""
        if self.changes:
            entry = self.changes.pop(0)
            payload = entry() if callable(entry) else entry
            self.head_seq = max(self.head_seq, int(payload.get("seq") or 0))
            return 0, json.dumps(payload), ""
        if self.on_wait is not None and wait > 0:
            self.on_wait(wait)
        return 0, json.dumps({"seq": since, "changes": []}), ""

    def by_command(self, name: str) -> list[list[str]]:
        return [c for c in self.calls if c[0] == name or c[:2] == ["watch", name]]


def change(seq: int, *keys: str) -> dict[str, Any]:
    rows = [
        {"seq": seq, "item_key": k, "kind": "pr", "fields": ["state"], "at": "x"}
        for k in keys
    ]
    return {"seq": seq, "changes": rows}


# ----- tests -------------------------------------------------------------------------


class PackratDbCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = make_packrat_db(Path(self.tmp.name) / "repos.sqlite")
        self.cli = FakePackratCli()
        self.source = PackratSource(self.db, "conv123", runner=self.cli)

    def tearDown(self) -> None:
        self.tmp.cleanup()


class TestAvailability(PackratDbCase):
    def test_fresh_stale_never_and_missing(self) -> None:
        self.assertTrue(available(self.db))
        set_heartbeat(self.db, 181)
        self.assertFalse(available(self.db))
        set_heartbeat(self.db, None)
        self.assertFalse(available(self.db))
        self.assertIsNone(heartbeat_age(self.db))
        self.assertFalse(available(Path(self.tmp.name) / "missing.sqlite"))

    def test_db_without_contract_views_is_unavailable(self) -> None:
        bare = Path(self.tmp.name) / "bare.sqlite"
        sqlite3.connect(bare).close()
        self.assertFalse(available(bare))


class TestServiceHealth(PackratDbCase):
    def test_thresholds(self) -> None:
        self.assertTrue(ServiceHealth(180, 2, {}).healthy)
        self.assertFalse(ServiceHealth(181, 0, {}).heartbeat_fresh)
        self.assertFalse(ServiceHealth(None, 0, {}).heartbeat_fresh)
        stuck = ServiceHealth(3, 3, {})
        self.assertTrue(stuck.heartbeat_fresh)
        self.assertFalse(stuck.healthy)

    def test_fixture_file_without_new_columns_reads_zero_and_empty(self) -> None:
        # A database written by a service that predates `consecutive_failed_ticks`
        # and the sync-time column.
        old = Path(self.tmp.name) / "old.sqlite"
        with _db(old) as conn:
            conn.execute(
                "CREATE TABLE v_service_health "
                "(pid INTEGER, beat_age_s INTEGER, viewer_login TEXT)"
            )
            conn.execute("INSERT INTO v_service_health VALUES (4242, 5, 'alice')")
        source = PackratSource(old, "conv123", runner=self.cli)
        self.assertEqual(source.health(), ServiceHealth(5, 0, {}))
        set_heartbeat(old, None)
        self.assertFalse(source.health().heartbeat_fresh)
        self.assertEqual(self.cli.calls, [])

    def test_fixture_file_defaults_to_zero_ticks_and_no_synced_sets(self) -> None:
        self.assertEqual(self.source.health(), ServiceHealth(5, 0, {}))

    def test_fixture_file_reads_failed_ticks_and_sync_times(self) -> None:
        synced = {"authored_open": "2026-11-17T01:19:05Z"}
        set_failed_ticks(self.db, 3)
        set_synced_sets(self.db, synced)
        health = self.source.health()
        self.assertEqual(health, ServiceHealth(5, 3, synced))
        self.assertTrue(health.heartbeat_fresh)
        self.assertFalse(health.healthy)

    def test_fixture_file_without_the_view_raises(self) -> None:
        bare = Path(self.tmp.name) / "bare.sqlite"
        sqlite3.connect(bare).close()
        with self.assertRaises(PackratError):
            PackratSource(bare, "conv123", runner=self.cli).health()


class TestSnapshots(PackratDbCase):
    def _assert_round_trip(self, fixture: str) -> None:
        pr = json.loads(load_fixture(fixture))["data"]["pr0"]["pullRequest"]
        key = store_graphql_pr(self.db, pr)
        expected = normalize_pr(pr, viewer="alice", repo="acme/widget")
        raw = self.source.snapshots([key, "acme/widget#1"])
        self.assertEqual(list(raw), [key])
        actual = normalize_pr(raw[key], viewer="alice", repo="acme/widget")
        # packrat stores neither assignees nor the head committer (see module docstring).
        self.assertEqual(actual.assignees, [])
        self.assertIsNone(actual.last_committer)
        expected = replace(expected, assignees=[], last_committer=None)
        self.assertEqual(actual.to_dict(), expected.to_dict())

    def test_ci_running_fixture_round_trips_through_normalize(self) -> None:
        self._assert_round_trip("pr_open_ci_running.json")

    def test_ci_failed_fixture_round_trips_through_normalize(self) -> None:
        self._assert_round_trip("pr_ci_failed.json")

    def test_same_named_checks_keep_the_failing_run(self) -> None:
        pr = json.loads(load_fixture("pr_open_ci_running.json"))["data"]["pr0"][
            "pullRequest"
        ]
        failing = {
            "__typename": "CheckRun", "databaseId": 701, "name": "build", "status": "COMPLETED",
            "conclusion": "FAILURE", "isRequired": True,
        }  # fmt: skip
        passing = {**failing, "databaseId": 702, "conclusion": "SUCCESS"}
        contexts = pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]
        contexts["nodes"] = [failing, passing]
        contexts["totalCount"] = 2
        key = store_graphql_pr(self.db, pr, rollup_state="FAILURE")
        with _db(self.db) as conn:
            keys = [
                r[0] for r in conn.execute("SELECT run_key FROM v_pr_checks ORDER BY 1")
            ]
        self.assertEqual(keys, ["CheckRun:701", "CheckRun:702"])
        snap = normalize_pr(self.source.snapshots([key])[key], viewer="alice")
        self.assertEqual(
            [(c.name, c.conclusion) for c in snap.checks], [("build", "FAILURE")]
        )
        self.assertEqual(snap.truncated, [])

    def test_rollup_unknown_for_current_head_gives_no_checks(self) -> None:
        pr = json.loads(load_fixture("pr_open_ci_running.json"))["data"]["pr0"][
            "pullRequest"
        ]
        key = store_graphql_pr(self.db, pr, rollup_state=None, rollup_total=None)
        with _db(self.db) as conn:
            conn.execute("DELETE FROM v_pr_checks")
        snap = normalize_pr(self.source.snapshots([key])[key], viewer="alice")
        self.assertIsNone(snap.rollup_state)
        self.assertEqual(snap.checks, [])

    def test_fetched_ages_and_viewer_login(self) -> None:
        key = store_fixture_prs(self.db, "pr_open_ci_running.json")[0]
        ages = self.source.fetched_ages([key, "acme/widget#1"])
        self.assertEqual(list(ages), [key])
        self.assertLess(ages[key], 60)
        self.assertEqual(self.source.viewer_login(), "alice")

    def test_viewer_login_from_authored_set_without_health_column(self) -> None:
        store_fixture_prs(self.db, "pr_open_ci_running.json")
        with _db(self.db) as conn:
            conn.execute("UPDATE v_service_health SET viewer_login = NULL")
            conn.execute(
                "INSERT INTO v_viewer_items VALUES ('authored', 'acme/widget#3339', 'x', 'x')"
            )
        self.assertEqual(self.source.viewer_login(), "alice")

    def test_author_reviews_hydration_and_pre_v8_fallback(self) -> None:
        pr = json.loads(load_fixture("pr_open_ci_running.json"))["data"]["pr0"][
            "pullRequest"
        ]
        pr["reviews"] = {
            "nodes": [
                {
                    "state": "COMMENTED",
                    "submittedAt": "2026-10-27T21:03:00Z",
                    "commit": {"oid": "deadbeef1234"},
                    "author": {"login": "bob", "__typename": "User"},
                },
                {
                    "state": "APPROVED",
                    "submittedAt": "2026-10-27T20:50:00Z",
                    "commit": None,
                    "author": {"login": "carol", "__typename": "User"},
                },
            ]
        }
        key = store_graphql_pr(self.db, pr)
        raw = self.source.snapshots([key])[key]
        self.assertEqual(
            raw["reviews"],
            {
                "nodes": [
                    {
                        "state": "COMMENTED",
                        "submittedAt": "2026-10-27T21:03:00Z",
                        "commit": {"oid": "deadbeef1234"},
                        "author": {"login": "bob", "__typename": "User"},
                    },
                    {
                        "state": "APPROVED",
                        "submittedAt": "2026-10-27T20:50:00Z",
                        "commit": None,
                        "author": {"login": "carol", "__typename": "User"},
                    },
                ]
            },
        )
        snap = normalize_pr(raw, viewer="bob")
        assert snap.viewer_last_review is not None
        self.assertEqual(snap.viewer_last_review.state, "COMMENTED")
        self.assertEqual(snap.viewer_last_review.commit_oid, "deadbeef1234")

        # Simulate pre-v8 DB without v_pr_latest_author_reviews
        with _db(self.db) as conn:
            conn.execute("DROP VIEW v_pr_latest_author_reviews")
            conn.execute("DROP TABLE latest_reviews")
        raw_fallback = self.source.snapshots([key])[key]
        self.assertEqual(raw_fallback["reviews"], {"nodes": []})
        snap_carol = normalize_pr(raw_fallback, viewer="carol")
        self.assertEqual(
            snap_carol.viewer_last_review,
            ViewerReview(
                state="APPROVED",
                submitted_at="2026-10-27T20:50:00Z",
                commit_oid=None,
            ),
        )
        self.assertEqual(snap_carol.viewer_opinionated_state, "APPROVED")

    def test_v9_threads_requests_and_comments_round_trip_and_pre_v9_fallback(
        self,
    ) -> None:
        pr = json.loads(load_fixture("pr_open_ci_running.json"))["data"]["pr0"][
            "pullRequest"
        ]
        pr["timelineItems"] = {
            "nodes": [
                {
                    "createdAt": "2026-10-27T21:02:00Z",
                    "requestedReviewer": {"__typename": "User", "login": "bob"},
                }
            ]
        }
        pr["reviews"] = {
            "nodes": [
                {
                    "state": "CHANGES_REQUESTED",
                    "submittedAt": "2026-10-27T20:30:00Z",
                    "commit": {"oid": "11112222"},
                    "author": {"login": "bob", "__typename": "User"},
                }
            ]
        }
        pr["reviewThreads"] = {
            "totalCount": 1,
            "nodes": [
                {
                    "id": "PRRT_v9",
                    "isResolved": False,
                    "isOutdated": True,
                    "firstComment": {
                        "nodes": [{"author": {"login": "bob", "__typename": "User"}}]
                    },
                    "comments": {
                        "nodes": [
                            {
                                "author": {"login": "carol", "__typename": "User"},
                                "createdAt": "2026-10-27T20:40:00Z",
                            },
                            {
                                "author": {"login": "alice", "__typename": "User"},
                                "createdAt": "2026-10-27T20:55:00Z",
                            },
                        ]
                    },
                }
            ],
        }
        pr["comments"] = {
            "nodes": [
                {
                    "id": "IC_10",
                    "createdAt": "2026-10-27T21:03:00Z",
                    "author": {"login": "bob", "__typename": "User"},
                },
                {
                    "id": "IC_11",
                    "createdAt": "2026-10-27T21:06:00Z",
                    "author": {"login": "alice", "__typename": "User"},
                },
                {
                    "id": "IC_bot",
                    "createdAt": "2026-10-27T21:08:00Z",
                    "author": {"login": "ci-bot", "__typename": "Bot"},
                },
            ]
        }
        key = store_graphql_pr(self.db, pr)
        raw = self.source.snapshots([key])[key]
        snap = normalize_pr(raw, viewer="bob")
        self.assertEqual(snap.head_committed_date, "2026-10-27T21:00:00Z")
        self.assertEqual(snap.viewer_requested_at, "2026-10-27T21:02:00Z")
        self.assertEqual(snap.viewer_last_comment_at, "2026-10-27T21:03:00Z")
        self.assertEqual(snap.author_last_comment_at, "2026-10-27T21:06:00Z")
        self.assertEqual(snap.discussion_watermark, "IC_11:2026-10-27T21:06:00Z")
        self.assertEqual(len(snap.threads), 1)
        self.assertTrue(snap.threads[0].outdated)
        self.assertEqual(snap.threads[0].participants, ("bob", "carol", "alice"))
        self.assertEqual(snap.threads[0].last_author, "alice")

        # Downgrade DB to pre-v9 schema: drop v_item_comments, requested_at, and participants_json
        with _db(self.db) as conn:
            conn.execute("DROP TABLE v_item_comments")
            conn.execute("ALTER TABLE v_pr_review_requests DROP COLUMN requested_at")
            conn.execute("ALTER TABLE v_pr_threads DROP COLUMN participants_json")
        raw_pre_v9 = self.source.snapshots([key])[key]
        snap_pre_v9 = normalize_pr(raw_pre_v9, viewer="bob")
        self.assertEqual(
            snap_pre_v9.requested_reviewers, ["bob", "team:core-reviewers"]
        )
        self.assertIsNone(snap_pre_v9.viewer_requested_at)
        self.assertIsNone(snap_pre_v9.viewer_last_comment_at)
        self.assertIsNone(snap_pre_v9.author_last_comment_at)
        self.assertIsNone(snap_pre_v9.discussion_watermark)
        self.assertTrue(snap_pre_v9.threads[0].outdated)
        self.assertEqual(snap_pre_v9.threads[0].participants, ())

    def test_discover_review_keys_orders_by_updated_at_and_filters_repo(self) -> None:
        pr_base = json.loads(load_fixture("pr_open_ci_running.json"))["data"]["pr0"][
            "pullRequest"
        ]
        pr1 = {
            **pr_base,
            "number": 10,
            "url": "https://github.com/acme/widget/pull/10",
            "updatedAt": "2026-10-27T20:00:00Z",
        }
        pr2 = {
            **pr_base,
            "number": 20,
            "url": "https://github.com/other/repo/pull/20",
            "updatedAt": "2026-10-27T22:00:00Z",
        }
        pr3_closed = {
            **pr_base,
            "number": 30,
            "url": "https://github.com/acme/widget/pull/30",
            "state": "CLOSED",
            "updatedAt": "2026-10-27T23:00:00Z",
        }
        store_graphql_pr(self.db, pr1)
        store_graphql_pr(self.db, pr2)
        store_graphql_pr(self.db, pr3_closed)
        with _db(self.db) as conn:
            conn.executemany(
                "INSERT INTO v_viewer_items VALUES (?,?,?,?)",
                [
                    ("review_requested", "acme/widget#10", "x", "x"),
                    ("reviewed", "acme/widget#10", "x", "x"),
                    ("review_requested", "acme/widget#40", "x", "x"),
                    ("reviewed", "other/repo#20", "x", "x"),
                    ("review_requested", "acme/widget#30", "x", "x"),
                    ("authored", "acme/widget#99", "x", "x"),
                ],
            )
        self.assertEqual(
            self.source.discover_review_keys(),
            ["acme/widget#10", "acme/widget#40", "other/repo#20"],
        )
        self.assertEqual(
            self.source.discover_review_keys(repo="ACME/Widget"),
            ["acme/widget#10", "acme/widget#40"],
        )

    def _store_authored(self) -> None:
        pr_base = json.loads(load_fixture("pr_open_ci_running.json"))["data"]["pr0"][
            "pullRequest"
        ]
        specs = (
            ("acme/widget", 10, "OPEN", "2026-10-27T20:00:00Z"),
            ("other/repo", 20, "OPEN", "2026-10-27T22:00:00Z"),
            ("acme/widget", 30, "CLOSED", "2026-10-27T23:00:00Z"),
            ("acme/widget", 50, "OPEN", "2026-10-27T20:00:00Z"),
        )
        for repo, number, state, updated in specs:
            store_graphql_pr(
                self.db,
                {
                    **pr_base,
                    "number": number,
                    "url": f"https://github.com/{repo}/pull/{number}",
                    "state": state,
                    "updatedAt": updated,
                },
            )
        with _db(self.db) as conn:
            conn.executemany(
                "INSERT INTO v_viewer_items VALUES (?,?,?,?)",
                [
                    ("authored_open", "acme/widget#50", "x", "x"),
                    ("authored_open", "acme/widget#10", "x", "x"),
                    ("authored_open", "other/repo#20", "x", "x"),
                    ("authored_open", "acme/widget#30", "x", "x"),  # row says CLOSED
                    ("authored_open", "acme/widget#40", "x", "x"),  # no live row yet
                    ("authored", "acme/widget#99", "x", "x"),
                    ("review_requested", "acme/widget#77", "x", "x"),
                ],
            )

    def test_discover_authored_keys_orders_by_update_then_key_and_filters_repo(
        self,
    ) -> None:
        self._store_authored()
        set_synced_sets(self.db, {"authored_open": "2026-11-17T01:19:05Z"})
        self.assertEqual(
            self.source.discover_authored_keys(),
            ["other/repo#20", "acme/widget#10", "acme/widget#50", "acme/widget#40"],
        )
        self.assertEqual(
            self.source.discover_authored_keys(repo="ACME/Widget"),
            ["acme/widget#10", "acme/widget#50", "acme/widget#40"],
        )
        self.assertEqual(self.source.discover_authored_keys(repo="no/such"), [])
        self.assertEqual(self.cli.calls, [])  # views only, no CLI call

    def test_discover_authored_keys_raises_when_the_set_never_synced(self) -> None:
        self._store_authored()
        for synced in ({}, {"authored": "2026-11-17T01:19:05Z"}):
            set_synced_sets(self.db, synced)
            for repo in (None, "acme/widget", "no/such"):
                with self.subTest(synced=synced, repo=repo):
                    with self.assertRaises(ViewerSetUnavailable) as ctx:
                        self.source.discover_authored_keys(repo=repo)
                    self.assertIn("packrat auto-sync on", str(ctx.exception))
                    self.assertIsInstance(ctx.exception, PackratError)
        # A set that synced empty is available.
        with _db(self.db) as conn:
            conn.execute("DELETE FROM v_viewer_items")
        set_synced_sets(self.db, {"authored_open": "2026-11-17T01:19:05Z"})
        self.assertEqual(self.source.discover_authored_keys(), [])


class TestStackNeighbors(PackratDbCase):
    def test_three_pr_stack_from_the_middle(self) -> None:
        add_basic_pr(self.db, "acme/widget#1", "main", "a")
        add_basic_pr(self.db, "acme/widget#2", "a", "b")
        add_basic_pr(self.db, "acme/widget#3", "b", "c")
        add_basic_pr(self.db, "acme/widget#4", "a", "old", state="MERGED")
        self.assertEqual(
            self.source.stack_neighbors("acme/widget#2"),
            ["acme/widget#1", "acme/widget#2", "acme/widget#3"],
        )
        self.assertEqual(
            self.source.stack_neighbors("acme/widget#3"),
            ["acme/widget#1", "acme/widget#2", "acme/widget#3"],
        )

    def test_same_named_fork_branch_is_never_a_parent(self) -> None:
        add_basic_pr(self.db, "acme/widget#1", "main", "a")
        add_basic_pr(
            self.db, "acme/widget#9", "main", "a", cross=1
        )  # fork with branch "a"
        add_basic_pr(self.db, "acme/widget#2", "a", "b")
        self.assertEqual(
            self.source.stack_neighbors("acme/widget#2"),
            ["acme/widget#1", "acme/widget#2"],
        )

    def test_fork_headed_pr_has_no_children(self) -> None:
        add_basic_pr(self.db, "acme/widget#5", "main", "feature", cross=1)
        add_basic_pr(self.db, "acme/widget#6", "feature", "next")
        self.assertEqual(
            self.source.stack_neighbors("acme/widget#5"), ["acme/widget#5"]
        )

    def test_null_fork_fields_count_as_same_repo(self) -> None:
        add_basic_pr(self.db, "acme/widget#1", "main", "a", cross=None)
        add_basic_pr(self.db, "acme/widget#2", "a", "b", cross=None)
        self.assertEqual(
            self.source.stack_neighbors("acme/widget#1"),
            ["acme/widget#1", "acme/widget#2"],
        )
        self.assertEqual(
            self.source.stack_neighbors("acme/widget#2"),
            ["acme/widget#1", "acme/widget#2"],
        )

    def test_items_repo_and_unknown_pr_return_none(self) -> None:
        add_basic_pr(self.db, "other/repo#1", "main", "a", mode="items")
        self.assertIsNone(self.source.stack_neighbors("other/repo#1"))
        self.assertIsNone(self.source.stack_neighbors("acme/widget#404"))


class TestCliCalls(PackratDbCase):
    def test_lease_renew_release_and_refresh_arguments(self) -> None:
        self.source.lease(["acme/widget#2", "acme/widget#1"])
        self.source.renew()
        self.source.refresh({"acme/widget#2"})
        self.source.release()
        base = ["--consumer", "monitor", "--id", "conv123"]
        self.assertEqual(
            self.cli.calls,
            [
                [
                    "watch",
                    "add",
                    *base,
                    "--freshness",
                    "120",
                    "--ttl",
                    "900",
                    "--json",
                    "--",
                    "acme/widget#1",
                    "acme/widget#2",
                ],
                ["watch", "renew", *base, "--ttl", "900", "--json"],
                ["refresh", "--json", "--", "acme/widget#2"],
                ["watch", "remove", *base, "--json"],
            ],
        )

    def test_malformed_keys_are_skipped_not_passed_as_options(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            self.source.lease(["--help", "acme/widget#1", "acme/widget#x"])
            self.source.refresh(["-rf"])
        self.assertEqual(self.cli.calls[0][-2:], ["--", "acme/widget#1"])
        self.assertEqual(
            len(self.cli.calls), 1
        )  # refresh with no valid keys makes no call
        self.assertEqual(self.source.watched, {"acme/widget#1"})
        self.assertEqual(err.getvalue().count("malformed packrat item key"), 3)

    def test_wait_changes_filters_to_watched_keys_and_reports_reset(self) -> None:
        self.source.lease(["acme/widget#1"])
        self.cli.changes.append(change(12, "acme/widget#1", "acme/widget#7"))
        batch = self.source.wait_changes(10, 20)
        self.assertEqual(
            (batch.seq, batch.keys, batch.reset), (12, {"acme/widget#1"}, False)
        )
        self.assertIn(
            ["changes", "--since", "10", "--wait", "20", "--json"], self.cli.calls
        )

        self.cli.changes.append({"reset": True, "seq": 99})
        batch = self.source.wait_changes(12, 0)
        self.assertEqual(
            (batch.seq, batch.keys, batch.reset), (99, {"acme/widget#1"}, True)
        )

    def test_unlease_and_viewer_changed_on_unwatched_pr(self) -> None:
        self.source.lease(["acme/widget#1", "acme/widget#2"])
        self.source.unlease([])
        self.assertEqual(len(self.cli.calls), 1)
        self.source.unlease(["acme/widget#2"])
        self.assertEqual(self.source.watched, {"acme/widget#1"})
        self.assertEqual(
            self.cli.calls[-1],
            [
                "watch",
                "remove",
                "--consumer",
                "monitor",
                "--id",
                "conv123",
                "--json",
                "--",
                "acme/widget#2",
            ],
        )

        # Unwatched viewer set change triggers viewer_changed when watch_viewer_sets=True
        self.cli.changes.append(
            {
                "seq": 15,
                "changes": [
                    {
                        "seq": 15,
                        "item_key": "other/repo#88",
                        "kind": "viewer",
                        "fields": ["review_requested"],
                        "at": "x",
                    }
                ],
            }
        )
        batch_watched = self.source.wait_changes(10, 5, watch_viewer_sets=True)
        self.assertEqual(batch_watched.seq, 15)
        self.assertEqual(batch_watched.keys, set())
        self.assertTrue(batch_watched.viewer_changed)

        self.cli.changes.append(
            {
                "seq": 16,
                "changes": [
                    {
                        "seq": 16,
                        "item_key": "other/repo#88",
                        "kind": "viewer",
                        "fields": ["review_requested"],
                        "at": "x",
                    }
                ],
            }
        )
        batch_unwatched = self.source.wait_changes(15, 5, watch_viewer_sets=False)
        self.assertFalse(batch_unwatched.viewer_changed)

    def test_cli_failure_raises_packrat_error(self) -> None:
        self.cli.fail_next = "database is locked"
        with self.assertRaises(PackratError):
            self.source.renew()


class TestGhConfigUser(unittest.TestCase):
    def test_reads_github_user_from_hosts_yml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "hosts.yml").write_text(
                "ghe.example.com:\n    user: other\ngithub.com:\n"
                "    git_protocol: ssh\n    user: alice\n",
                encoding="utf-8",
            )
            self.assertEqual(gh_config_user(Path(tmp)), "alice")
            self.assertEqual(gh_config_user(Path(tmp) / "missing"), "")


class FakeReaderRunner:
    """Scripted `packrat` CLI runner serving `packrat reader --ops-json <ops> --json`.

    `changes` answers "no changes" after calling `on_wait(seconds)`, so a fake clock
    can advance while the monitor waits on the feed. A call that waits answers from
    the `changes` queue first: each entry is a payload dict or a callable returning
    one, as in `FakePackratCli`.
    """

    def __init__(
        self,
        *,
        beat_age_s: int | None = 4,
        viewer_login: str | None = "alice",
        on_wait: Callable[[float], None] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.beat_age_s = beat_age_s
        self.viewer_login = viewer_login
        self.health_extra: dict[str, Any] = {}
        self.on_wait = on_wait
        self.viewer_items: list[dict[str, Any]] = []
        self.items_bundle: dict[str, list[dict[str, Any]]] = {}
        self.known_keys_map: dict[str, str] = {}
        self.stacks_map: dict[str, list[str] | None] = {}
        self.changes: list[dict[str, Any] | Callable[[], dict[str, Any]]] = []
        self.fail_next: tuple[int, str] | None = None

    def __call__(self, args: list[str], timeout_s: float) -> tuple[int, str, str]:
        self.calls.append(list(args))
        if self.fail_next is not None:
            code, err = self.fail_next
            self.fail_next = None
            return code, "", err
        if args[0] == "reader":
            ops = json.loads(args[args.index("--ops-json") + 1])
            resp: dict[str, Any] = {}
            if ops.get("health"):
                resp["health"] = {
                    "pid": 9001,
                    "beat_age_s": self.beat_age_s,
                    "viewer_login": self.viewer_login,
                    **self.health_extra,
                }
            if ops.get("viewer_items"):
                resp["viewer_items"] = list(self.viewer_items)
            if ops.get("items"):
                resp["items"] = dict(self.items_bundle)
            if ops.get("known_keys"):
                req = set(ops["known_keys"].get("keys") or [])
                resp["known_keys"] = {
                    k: v for k, v in self.known_keys_map.items() if k in req
                }
            if ops.get("stack_neighbors"):
                req_keys = ops["stack_neighbors"].get("keys") or []
                resp["stack_neighbors"] = {k: self.stacks_map.get(k) for k in req_keys}
            return 0, json.dumps(resp), ""
        if args[0] == "changes":
            since = int(args[args.index("--since") + 1])
            wait = float(args[args.index("--wait") + 1])
            if self.changes and wait > 0:
                entry = self.changes.pop(0)
                return 0, json.dumps(entry() if callable(entry) else entry), ""
            if self.on_wait is not None and wait > 0:
                self.on_wait(wait)
            return 0, json.dumps({"seq": since, "changes": []}), ""
        return 0, json.dumps({"ok": True}), ""


class TestReaderCliMode(unittest.TestCase):
    """Tests zero-local-DB `packrat reader` execution, `--db` omission, and `auth_expired`."""

    def test_default_db_detection_and_subprocess_omits_db_flag(self) -> None:
        self.assertTrue(_is_default_db_path(DEFAULT_PACKRAT_DB))
        self.assertTrue(
            _is_default_db_path(Path("~/.local/share/packrat/repos.sqlite"))
        )
        with tempfile.TemporaryDirectory() as tmp:
            custom = Path(tmp) / "custom.sqlite"
            self.assertFalse(_is_default_db_path(custom))
            proc_ok = unittest.mock.MagicMock(returncode=0, stdout="{}", stderr="")
            with unittest.mock.patch(
                "subprocess.run", return_value=proc_ok
            ) as run_mock:
                SubprocessPackrat(DEFAULT_PACKRAT_DB).run(
                    ["reader", "--health", "--json"]
                )
                default_argv = run_mock.call_args_list[-1].args[0]
                self.assertNotIn("--db", default_argv)

                SubprocessPackrat(custom).run(["reader", "--health", "--json"])
                custom_argv = run_mock.call_args_list[-1].args[0]
                self.assertIn("--db", custom_argv)
                self.assertEqual(
                    custom_argv[custom_argv.index("--db") + 1], str(custom)
                )

    def test_available_and_heartbeat_age_via_reader_and_auth_expired_probe(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_db = Path(tmp) / "no-local-db.sqlite"
            runner = FakeReaderRunner(beat_age_s=12)
            self.assertEqual(heartbeat_age(missing_db, runner=runner), 12)
            self.assertTrue(available(missing_db, runner=runner))

            runner.beat_age_s = 300
            self.assertFalse(available(missing_db, runner=runner))

            runner.beat_age_s = 5
            runner.fail_next = (4, "Error [auth_expired]: remote token expired")
            self.assertFalse(available(missing_db, runner=runner))

    def test_health_via_reader(self) -> None:
        runner = FakeReaderRunner(beat_age_s=12)
        with tempfile.TemporaryDirectory() as tmp:
            source = PackratSource(Path(tmp) / "remote.sqlite", "conv-h", runner=runner)
            # A service that predates the new fields reports neither.
            self.assertEqual(source.health(), ServiceHealth(12, 0, {}))
            self.assertEqual(len(runner.calls), 1)
            ops = runner.calls[0][runner.calls[0].index("--ops-json") + 1]
            self.assertEqual(json.loads(ops), {"health": True})

            synced = {"authored": "2026-11-17T01:19:05Z"}
            runner.health_extra = {
                "consecutive_failed_ticks": 4,
                "viewer_sets_synced_at": synced,
            }
            health = source.health()
            self.assertEqual(health, ServiceHealth(12, 4, synced))
            self.assertTrue(health.heartbeat_fresh)
            self.assertFalse(health.healthy)

            runner.beat_age_s = None
            self.assertFalse(source.health().heartbeat_fresh)

    def test_health_via_reader_raises_when_the_cli_fails(self) -> None:
        runner = FakeReaderRunner()
        runner.fail_next = (1, "packrat: cannot reach the service")
        with tempfile.TemporaryDirectory() as tmp:
            source = PackratSource(Path(tmp) / "remote.sqlite", "conv-h", runner=runner)
            with self.assertRaises(PackratError) as ctx:
                source.health()
        self.assertIn("cannot reach the service", str(ctx.exception))

    def test_snapshots_caches_ages_and_invalidates_on_refresh_or_changes(self) -> None:
        pr = json.loads(load_fixture("pr_open_ci_running.json"))["data"]["pr0"][
            "pullRequest"
        ]
        live = _live_row("acme/widget#3339", "acme/widget", "mirror", pr, age_s=7.25)
        runner = FakeReaderRunner(viewer_login="alice")
        runner.items_bundle = {
            "pr_live": [live],
            "checks": [],
            "review_requests": [
                {
                    "item_key": "acme/widget#3339",
                    "reviewer": "bob",
                    "reviewer_kind": "User",
                }
            ],
            "latest_reviews": [],
            "latest_author_reviews": [],
            "threads": [],
            "comments": [
                {
                    "item_key": "acme/widget#3339",
                    "kind": "pr",
                    "comment_id": "IC_1",
                    "author": "bob",
                    "author_type": "User",
                    "created_at": "2026-10-27T21:05:00Z",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            no_db = Path(tmp) / "remote-only.sqlite"
            source = PackratSource(no_db, "conv-remote", runner=runner)
            snaps = source.snapshots(["acme/widget#3339", "acme/widget#404"])
            self.assertEqual(list(snaps), ["acme/widget#3339"])
            snap = normalize_pr(
                snaps["acme/widget#3339"], viewer="alice", repo="acme/widget"
            )
            self.assertEqual(snap.requested_reviewers, ["bob"])
            self.assertEqual(len(runner.calls), 1)

            # Cached age_s avoids a redundant reader subprocess call!
            ages = source.fetched_ages(["acme/widget#3339", "acme/widget#404"])
            self.assertEqual(ages, {"acme/widget#3339": 7.25})
            self.assertEqual(len(runner.calls), 1)

            # refresh() invalidates the age cache so the next fetched_ages() re-queries.
            runner.items_bundle["pr_live"] = [{**live, "age_s": 1.0}]
            source.refresh(["acme/widget#3339"])
            self.assertEqual(
                source.fetched_ages(["acme/widget#3339"]), {"acme/widget#3339": 1.0}
            )
            self.assertEqual(len(runner.calls), 3)

            # wait_changes() also invalidates the age cache.
            runner.items_bundle["pr_live"] = [{**live, "age_s": 0.5}]
            source.wait_changes(10, 0)
            self.assertEqual(
                source.fetched_ages(["acme/widget#3339"]), {"acme/widget#3339": 0.5}
            )

    def test_discover_review_keys_known_keys_viewer_and_stacks_via_reader(self) -> None:
        runner = FakeReaderRunner(viewer_login="alice")
        runner.viewer_items = [
            {
                "set_name": "reviewed",
                "item_key": "other/repo#20",
                "state": "OPEN",
                "updated_at": "2026-10-27T22:00:00Z",
            },
            {
                "set_name": "review_requested",
                "item_key": "acme/widget#10",
                "state": "OPEN",
                "updated_at": "2026-10-27T20:00:00Z",
            },
            {
                "set_name": "review_requested",
                "item_key": "acme/widget#40",
                "state": None,
                "updated_at": None,
            },
            {
                "set_name": "review_requested",
                "item_key": "acme/widget#30",
                "state": "CLOSED",
                "updated_at": "2026-10-27T23:00:00Z",
            },
        ]
        runner.known_keys_map = {"legacy/Widget#10": "acme/widget#10"}
        runner.stacks_map = {
            "acme/widget#2": ["acme/widget#1", "acme/widget#2", "acme/widget#3"],
            "items/repo#9": None,
        }
        with tempfile.TemporaryDirectory() as tmp:
            source = PackratSource(Path(tmp) / "remote.sqlite", "conv-r", runner=runner)
            self.assertEqual(
                source.discover_review_keys(),
                ["acme/widget#10", "acme/widget#40", "other/repo#20"],
            )
            self.assertEqual(
                source.discover_review_keys(repo="ACME/Widget"),
                ["acme/widget#10", "acme/widget#40"],
            )
            self.assertEqual(
                source.known_keys(["legacy/Widget#10", "acme/widget#99"]),
                {"legacy/Widget#10": "acme/widget#10"},
            )
            self.assertEqual(source.viewer_login(), "alice")
            self.assertEqual(
                source.stack_neighbors("acme/widget#2"),
                ["acme/widget#1", "acme/widget#2", "acme/widget#3"],
            )
            self.assertIsNone(source.stack_neighbors("items/repo#9"))

            # The shared helper filters to the requested sets, so a review query
            # ignores authored rows that a server returns anyway.
            runner.viewer_items.append(
                {
                    "set_name": "authored_open",
                    "item_key": "acme/widget#5",
                    "state": "OPEN",
                }
            )
            self.assertEqual(
                source.discover_review_keys(),
                ["acme/widget#10", "acme/widget#40", "other/repo#20"],
            )

            runner.fail_next = (
                4,
                "Error [auth_expired]: token expired\nplease re-authenticate",
            )
            with self.assertRaises(PackratError) as ctx:
                source.snapshots(["acme/widget#10"])
            self.assertIn("auth_expired", str(ctx.exception))

    def test_discover_authored_keys_via_reader_reads_health_in_the_same_batch(
        self,
    ) -> None:
        def row(key: str, state: str | None, updated: str | None) -> dict[str, Any]:
            return {
                "set_name": "authored_open",
                "item_key": key,
                "state": state,
                "updated_at": updated,
                "last_seen_at": "2026-10-27T19:00:00Z",
            }

        runner = FakeReaderRunner(viewer_login="alice")
        runner.viewer_items = [
            row("acme/widget#50", "OPEN", "2026-10-27T20:00:00Z"),
            row("acme/widget#10", "OPEN", "2026-10-27T20:00:00Z"),
            row("other/repo#20", "OPEN", "2026-10-27T22:00:00Z"),
            row("acme/widget#30", "MERGED", "2026-10-27T23:00:00Z"),
            row("acme/widget#40", None, None),  # no live row: ordered by last_seen_at
            {"set_name": "review_requested", "item_key": "acme/widget#77"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            source = PackratSource(Path(tmp) / "remote.sqlite", "conv-a", runner=runner)
            for repo in (None, "acme/widget"):
                with self.subTest(repo=repo), self.assertRaises(ViewerSetUnavailable):
                    source.discover_authored_keys(repo=repo)

            runner.health_extra = {
                "viewer_sets_synced_at": {"authored_open": "2026-11-17T01:19:05Z"}
            }
            runner.calls.clear()
            self.assertEqual(
                source.discover_authored_keys(),
                ["other/repo#20", "acme/widget#10", "acme/widget#50", "acme/widget#40"],
            )
            self.assertEqual(len(runner.calls), 1)
            ops = json.loads(runner.calls[0][runner.calls[0].index("--ops-json") + 1])
            self.assertEqual(
                ops, {"viewer_items": {"sets": ["authored_open"]}, "health": True}
            )

            runner.calls.clear()
            self.assertEqual(
                source.discover_authored_keys(repo="ACME/Widget"),
                ["acme/widget#10", "acme/widget#50", "acme/widget#40"],
            )
            ops = json.loads(runner.calls[0][runner.calls[0].index("--ops-json") + 1])
            self.assertEqual(
                ops["viewer_items"], {"sets": ["authored_open"], "repo": "ACME/Widget"}
            )


if __name__ == "__main__":
    unittest.main()
