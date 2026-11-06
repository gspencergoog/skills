"""Unit tests for packrat_source.py (the /monitor reader of the packrat contract)."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from collections.abc import Callable, Iterator
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from models import parse_pr_ref
from normalize import normalize_pr
from packrat_source import (
    PackratError,
    PackratSource,
    available,
    gh_config_user,
    heartbeat_age,
)
from testing import FIXTURES_DIR, load_fixture

# ----- helpers shared with test_monitor_prs.py ---------------------------------------


def make_packrat_db(path: Path, beat_age_s: int | None = 5, viewer: str | None = "alice") -> Path:
    """Creates a database with the contract views (as tables) and a service heartbeat."""
    conn = sqlite3.connect(path)
    conn.executescript((FIXTURES_DIR / "packrat_views.sql").read_text(encoding="utf-8"))
    conn.execute(
        "INSERT INTO v_service_health (pid, beat_age_s, viewer_login, active_leases) "
        "VALUES (4242, ?, ?, 0)",
        (beat_age_s, viewer),
    )
    conn.commit()
    conn.close()
    return path


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


def _reviewer_row(key: str, repo: str, node: dict[str, Any]) -> tuple[str, str, str]:
    req = node["requestedReviewer"]
    if req.get("slug"):
        return key, f"{repo.split('/')[0]}/{req['slug']}", "Team"
    return key, req["login"], "User"


def _thread_row(key: str, node: dict[str, Any]) -> tuple[Any, ...]:
    last = node["comments"]["nodes"][-1]
    author = last.get("author") or {}
    return (key, node["id"], int(node["isResolved"]), 0, author.get("login"),
            author.get("__typename"), last.get("createdAt"))  # fmt: skip


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


def _live_row(key: str, repo: str, mode: str, pr: dict[str, Any], **extra: Any) -> dict[str, Any]:
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
    conn.execute(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({marks})", list(row.values()))


def store_graphql_pr(
    path: Path, pr: dict[str, Any], mode: str = "mirror", **live_extra: Any
) -> str:
    """Writes one GraphQL PR node into the views the way packrat's hot worker stores it."""
    repo, number = parse_pr_ref(pr["url"])
    key = f"{repo}#{number}"
    live = _live_row(key, repo, mode, pr, **live_extra)
    head = live["head_sha"]
    contexts = _head_commit(pr).get("statusCheckRollup") or {}
    with _db(path) as conn:
        for table in (
            "v_pr_checks",
            "v_pr_review_requests",
            "v_pr_latest_reviews",
            "v_pr_latest_author_reviews",
            "v_pr_threads",
        ):
            conn.execute(f"DELETE FROM {table} WHERE item_key = ?", (key,))
        _insert(conn, "v_pr_live", live)
        basic_cols = ("item_key", "repo_full_name", "repo_mode", "number", "title", "state",
                      "is_draft", "url", "author", "updated_at", "merged_at", "closed_at",
                      "base_ref", "head_ref", "head_sha", "head_repo_owner",
                      "is_cross_repository")  # fmt: skip
        _insert(conn, "v_pr_basic", {c: live[c] for c in basic_cols})
        conn.executemany(
            "INSERT INTO v_pr_checks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [_check_row(key, head, n) for n in (contexts.get("contexts") or {}).get("nodes", [])],
        )
        conn.executemany(
            "INSERT INTO v_pr_review_requests VALUES (?,?,?)",
            [_reviewer_row(key, repo, n) for n in pr["reviewRequests"]["nodes"]],
        )
        conn.executemany(
            "INSERT INTO v_pr_latest_reviews VALUES (?,?,?,?,?)",
            [
                (key, n["author"]["login"], "User", n["state"], n.get("submittedAt"))
                for n in pr["latestOpinionatedReviews"]["nodes"]
            ],
        )
        conn.executemany(
            "INSERT INTO v_pr_latest_author_reviews VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    key,
                    repo,
                    number,
                    n["author"]["login"],
                    n["author"].get("__typename") or "User",
                    n["state"],
                    n.get("submittedAt"),
                    ((n.get("commit") or {}).get("oid") or None),
                )
                for n in ((pr.get("reviews") or {}).get("nodes") or [])
                if n.get("state") != "PENDING" and (n.get("author") or {}).get("login")
            ],
        )
        conn.executemany(
            "INSERT INTO v_pr_threads VALUES (?,?,?,?,?,?,?)",
            [_thread_row(key, n) for n in pr["reviewThreads"]["nodes"]],
        )
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
    rows = [{"seq": seq, "item_key": k, "kind": "pr", "fields": ["state"], "at": "x"} for k in keys]
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
        pr = json.loads(load_fixture("pr_open_ci_running.json"))["data"]["pr0"]["pullRequest"]
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
            keys = [r[0] for r in conn.execute("SELECT run_key FROM v_pr_checks ORDER BY 1")]
        self.assertEqual(keys, ["CheckRun:701", "CheckRun:702"])
        snap = normalize_pr(self.source.snapshots([key])[key], viewer="alice")
        self.assertEqual([(c.name, c.conclusion) for c in snap.checks], [("build", "FAILURE")])
        self.assertEqual(snap.truncated, [])

    def test_rollup_unknown_for_current_head_gives_no_checks(self) -> None:
        pr = json.loads(load_fixture("pr_open_ci_running.json"))["data"]["pr0"]["pullRequest"]
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
        pr = json.loads(load_fixture("pr_open_ci_running.json"))["data"]["pr0"]["pullRequest"]
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
        self.assertIsNone(snap_carol.viewer_last_review)
        self.assertEqual(snap_carol.viewer_opinionated_state, "APPROVED")

    def test_discover_review_keys_orders_by_updated_at_and_filters_repo(self) -> None:
        pr_base = json.loads(load_fixture("pr_open_ci_running.json"))["data"]["pr0"]["pullRequest"]
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
                    ("reviewed", "other/repo#20", "x", "x"),
                    ("review_requested", "acme/widget#30", "x", "x"),
                    ("authored", "acme/widget#99", "x", "x"),
                ],
            )
        self.assertEqual(
            self.source.discover_review_keys(),
            ["other/repo#20", "acme/widget#10"],
        )
        self.assertEqual(
            self.source.discover_review_keys(repo="acme/widget"),
            ["acme/widget#10"],
        )


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
        add_basic_pr(self.db, "acme/widget#9", "main", "a", cross=1)  # fork with branch "a"
        add_basic_pr(self.db, "acme/widget#2", "a", "b")
        self.assertEqual(
            self.source.stack_neighbors("acme/widget#2"), ["acme/widget#1", "acme/widget#2"]
        )

    def test_fork_headed_pr_has_no_children(self) -> None:
        add_basic_pr(self.db, "acme/widget#5", "main", "feature", cross=1)
        add_basic_pr(self.db, "acme/widget#6", "feature", "next")
        self.assertEqual(self.source.stack_neighbors("acme/widget#5"), ["acme/widget#5"])

    def test_null_fork_fields_count_as_same_repo(self) -> None:
        add_basic_pr(self.db, "acme/widget#1", "main", "a", cross=None)
        add_basic_pr(self.db, "acme/widget#2", "a", "b", cross=None)
        self.assertEqual(
            self.source.stack_neighbors("acme/widget#1"), ["acme/widget#1", "acme/widget#2"]
        )
        self.assertEqual(
            self.source.stack_neighbors("acme/widget#2"), ["acme/widget#1", "acme/widget#2"]
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
        self.assertEqual(len(self.cli.calls), 1)  # refresh with no valid keys makes no call
        self.assertEqual(self.source.watched, {"acme/widget#1"})
        self.assertEqual(err.getvalue().count("malformed packrat item key"), 3)

    def test_wait_changes_filters_to_watched_keys_and_reports_reset(self) -> None:
        self.source.lease(["acme/widget#1"])
        self.cli.changes.append(change(12, "acme/widget#1", "acme/widget#7"))
        batch = self.source.wait_changes(10, 20)
        self.assertEqual((batch.seq, batch.keys, batch.reset), (12, {"acme/widget#1"}, False))
        self.assertIn(["changes", "--since", "10", "--wait", "20", "--json"], self.cli.calls)

        self.cli.changes.append({"reset": True, "seq": 99})
        batch = self.source.wait_changes(12, 0)
        self.assertEqual((batch.seq, batch.keys, batch.reset), (99, {"acme/widget#1"}, True))

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


if __name__ == "__main__":
    unittest.main()
