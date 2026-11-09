"""Tests for the `hot` worker (hot_sync.py)."""

from __future__ import annotations

import copy
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from db_store import PackratStore
from gh_transport import BudgetDeferred
from hot_sync import (
    HotRefresher,
    build_contexts_page_query,
    build_hot_query,
    build_node_id_query,
    build_threads_page_query,
    parse_hot_node,
    parse_thread,
)
from models import LatestReviewRecord, ReviewRequestRecord
from rate_budget import Priority

FIXTURES = Path(__file__).resolve().parent / "fixtures"
KEY7 = "octo-org/widgets#7"
KEY9 = "octo-org/widgets#9"


def _fixture(name: str) -> dict[str, Any]:
    """Loads a JSON fixture from fixtures/."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeTransport:
    """Answers GraphQL queries from fixtures, choosing by query shape."""

    def __init__(self, batch: dict[str, Any] | None = None) -> None:
        self.batch = batch or _fixture("hot_nodes_batch.json")
        self.queries: list[str] = []
        self.calls: list[tuple[str, Priority]] = []
        self.deny = False

    def graphql(
        self, query: str, variables: dict[str, Any], *, worker: str, priority: Priority
    ) -> dict[str, Any]:
        if self.deny:
            raise BudgetDeferred("graphql", priority)
        self.queries.append(query)
        self.calls.append((worker, priority))
        if "k0: repository" in query:
            return self._node_ids(query)
        if "reviewThreads(first: 50, after:" in query:
            return _fixture("hot_nodes_threads_page.json")
        if "contexts(first: 100, after:" in query:
            return _fixture("hot_nodes_contexts_page.json")
        return self._batch_for(query)

    def _batch_for(self, query: str) -> dict[str, Any]:
        """Returns the fixture nodes matching the query's `prN: node(id:)` aliases."""
        by_id = {
            n["id"]: n
            for n in self.batch.get("data", {}).values()
            if isinstance(n, dict) and "id" in n
        }
        data: dict[str, Any] = {}
        for alias, node_id in re.findall(r'(pr\d+): node\(id: "([^"]+)"\)', query):
            data[alias] = copy.deepcopy(by_id.get(node_id))
        return {"data": data}

    @staticmethod
    def _node_ids(query: str) -> dict[str, Any]:
        """Answers aliased id lookups; PR number 404 does not exist."""
        data: dict[str, Any] = {}
        for alias, number in re.findall(
            r"(k\d+): repository\(.*?pullRequest\(number: (\d+)\)", query
        ):
            found = number != "404"
            data[alias] = {"pullRequest": {"id": f"PR_kwDOA{number}"} if found else None}
        return {"data": data}


class TestQueryBuilders(unittest.TestCase):
    """Tests the per-batch GraphQL documents."""

    def test_hot_query_has_one_alias_and_literal_is_required_per_pr(self) -> None:
        query = build_hot_query([("PR_a", 7), ("PR_b", 9)])
        self.assertIn('pr0: node(id: "PR_a")', query)
        self.assertIn('pr1: node(id: "PR_b")', query)
        self.assertEqual(query.count("isRequired(pullRequestNumber: 7)"), 2)
        self.assertEqual(query.count("isRequired(pullRequestNumber: 9)"), 2)
        self.assertEqual(query.count("fragment HotPrFields on PullRequest"), 1)
        for field in (
            "mergeStateStatus",
            "autoMergeRequest",
            "headRepositoryOwner",
            "__typename",
            "reviews(last: 20)",
            "comments(last: 10)",
            "firstComment: comments(first: 1)",
            "REVIEW_REQUESTED_EVENT",
            "body",
        ):
            self.assertIn(field, query)
        with self.assertRaises(ValueError):
            build_hot_query([])

    def test_node_id_and_page_queries_escape_values(self) -> None:
        query = build_node_id_query([KEY7])
        self.assertIn('k0: repository(owner: "octo-org", name: "widgets")', query)
        self.assertIn("pullRequest(number: 7)", query)
        page = build_contexts_page_query("PR_x", 12, 'C"1')
        self.assertIn('after: "C\\"1"', page)
        self.assertIn("isRequired(pullRequestNumber: 12)", page)
        threads_page = build_threads_page_query("PR_x", 'T"1')
        self.assertIn('after: "T\\"1"', threads_page)
        self.assertIn("firstComment: comments(first: 1)", threads_page)
        self.assertIn("comments(last: 10)", threads_page)


class TestParsing(unittest.TestCase):
    """Tests parsing of fixture PR nodes."""

    def test_parse_user_bot_checks_and_requests(self) -> None:
        node = _fixture("hot_nodes_batch.json")["data"]["pr0"]
        snap = parse_hot_node(1, node)
        rec = snap.record
        self.assertEqual((rec.number, rec.author, rec.author_type), (7, "alice", "User"))
        self.assertEqual(rec.merge_state_status, "BLOCKED")
        self.assertEqual(rec.auto_merge_enabled_at, "2026-11-03T09:30:00Z")
        self.assertEqual((rec.rollup_state, rec.rollup_total), ("FAILURE", 3))
        self.assertFalse(rec.is_cross_repository)
        checks = {c.name: c for c in snap.checks}
        self.assertEqual(checks["build"].is_required, True)
        self.assertEqual(checks["build"].workflow_run_id, 111)
        self.assertEqual(checks["ci/lint"].kind, "StatusContext")
        self.assertEqual(checks["ci/lint"].conclusion, "SUCCESS")
        self.assertIs(checks["ci/lint"].is_required, False)
        requests = {(r.reviewer, r.reviewer_kind) for r in snap.review_requests}
        self.assertEqual(requests, {("bob", "User"), ("octo-org/ui-reviewers", "Team")})
        types = {r.author: r.author_type for r in snap.reviews}
        self.assertEqual(types, {"carol": "User", "gemini-code-assist": "Bot"})
        self.assertEqual(snap.latest_reviews, [])
        last = {t.thread_id: (t.last_author, t.last_author_type) for t in snap.threads}
        self.assertEqual(last["PRRT_2"], ("gemini-code-assist", "Bot"))

    def test_parse_latest_reviews_deduplicates_and_filters_pending(self) -> None:
        node = copy.deepcopy(_fixture("hot_nodes_batch.json")["data"]["pr0"])
        node["reviews"] = {
            "nodes": [
                {
                    "state": "CHANGES_REQUESTED",
                    "submittedAt": "2026-11-02T10:00:00Z",
                    "commit": {"oid": "111aaa"},
                    "author": {"login": "carol", "__typename": "User"},
                },
                {
                    "state": "COMMENTED",
                    "submittedAt": "2026-11-02T14:00:00Z",
                    "commit": {"oid": "222bbb"},
                    "author": {"login": "carol", "__typename": "User"},
                },
                {
                    "state": "APPROVED",
                    "submittedAt": "2026-11-02T11:00:00Z",
                    "commit": {"oid": "000999"},
                    "author": {"login": "carol", "__typename": "User"},
                },
                {
                    "state": "PENDING",
                    "submittedAt": "2026-11-02T16:00:00Z",
                    "commit": {"oid": "333ccc"},
                    "author": {"login": "carol", "__typename": "User"},
                },
                {
                    "state": "COMMENTED",
                    "submittedAt": "2026-11-02T13:00:00Z",
                    "commit": None,
                    "author": {"login": "gemini-code-assist", "__typename": "Bot"},
                },
                {
                    "state": "APPROVED",
                    "submittedAt": "2026-11-02T15:00:00Z",
                    "commit": {"oid": "444ddd"},
                    "author": None,
                },
            ]
        }
        snap = parse_hot_node(1, node)
        self.assertEqual(
            snap.latest_reviews,
            [
                LatestReviewRecord(
                    author="carol",
                    state="COMMENTED",
                    author_type="User",
                    submitted_at="2026-11-02T14:00:00Z",
                    commit_oid="222bbb",
                ),
                LatestReviewRecord(
                    author="gemini-code-assist",
                    state="COMMENTED",
                    author_type="Bot",
                    submitted_at="2026-11-02T13:00:00Z",
                    commit_oid=None,
                ),
            ],
        )

    def test_parse_bot_author_and_fork(self) -> None:
        node = _fixture("hot_nodes_batch.json")["data"]["pr1"]
        rec = parse_hot_node(1, node).record
        self.assertEqual((rec.author, rec.author_type), ("dependabot", "Bot"))
        self.assertTrue(rec.is_cross_repository)
        self.assertEqual(rec.head_repo_owner, "forker")
        self.assertTrue(rec.is_draft)
        self.assertIsNone(rec.auto_merge_enabled_at)

    def test_parse_thread_merges_first_and_last_comments_without_duplicates(self) -> None:
        thread = parse_thread(
            {
                "id": "PRRT_99",
                "isResolved": False,
                "isOutdated": True,
                "firstComment": {"nodes": [{"author": {"login": "alice"}}]},
                "comments": {
                    "nodes": [
                        {
                            "createdAt": "2026-11-03T09:00:00Z",
                            "author": {"login": "bob", "__typename": "User"},
                        },
                        {
                            "createdAt": "2026-11-03T09:05:00Z",
                            "author": {"login": "Alice", "__typename": "User"},
                        },
                        {
                            "createdAt": "2026-11-03T09:10:00Z",
                            "author": None,
                        },
                        {
                            "createdAt": "2026-11-03T09:15:00Z",
                            "author": {"login": "carol", "__typename": "User"},
                        },
                    ]
                },
            }
        )
        assert thread is not None
        self.assertTrue(thread.is_outdated)
        self.assertEqual(thread.participants, ("alice", "bob", "carol"))
        self.assertEqual(
            (thread.last_author, thread.last_author_type, thread.last_at),
            ("carol", "User", "2026-11-03T09:15:00Z"),
        )

    def test_parse_hot_node_populates_requested_at_for_user_and_team(self) -> None:
        node = copy.deepcopy(_fixture("hot_nodes_batch.json")["data"]["pr0"])
        node["reviewRequests"]["nodes"].append(
            {"requestedReviewer": {"__typename": "User", "login": "erin"}}
        )
        node["timelineItems"] = {
            "nodes": [
                {
                    "createdAt": "2026-11-02T09:00:00Z",
                    "requestedReviewer": {"__typename": "User", "login": "Bob"},
                },
                {
                    "createdAt": "2026-11-03T10:30:00Z",
                    "requestedReviewer": {"__typename": "User", "login": "bob"},
                },
                {
                    "createdAt": "2026-11-03T08:45:00Z",
                    "requestedReviewer": {
                        "__typename": "Team",
                        "slug": "ui-reviewers",
                        "organization": {"login": "Octo-Org"},
                    },
                },
                {"createdAt": "2026-11-03T11:00:00Z", "requestedReviewer": None},
                {},
            ]
        }
        snap = parse_hot_node(1, node)
        self.assertEqual(
            snap.review_requests,
            [
                ReviewRequestRecord("bob", "User", "2026-11-03T10:30:00Z"),
                ReviewRequestRecord("octo-org/ui-reviewers", "Team", "2026-11-03T08:45:00Z"),
                ReviewRequestRecord("erin", "User", None),
            ],
        )


class TestHotRefresher(unittest.TestCase):
    """Tests HotRefresher against a real store and a fixture transport."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="packrat-hot-"))
        self.store = PackratStore(self.tmp / "repos.sqlite")
        self.transport = FakeTransport()
        self.hot = HotRefresher(self.transport, self.store)

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rows(self, sql: str, *params: Any) -> list[tuple[Any, ...]]:
        return [tuple(r) for r in self.store.conn.execute(sql, params).fetchall()]

    def test_refresh_stores_everything_and_creates_items_repo(self) -> None:
        changed = self.hot.refresh_hot([KEY7, KEY9, "octo-org/widgets#404"])

        self.assertEqual(changed, [KEY7, KEY9])
        repo = self.store.get_repo("octo-org/widgets")
        assert repo is not None
        self.assertEqual(repo.mode, "items")
        self.assertEqual(self.store.get_node_ids([KEY7]), {KEY7: "PR_kwDOA7"})
        live = self._rows(
            "SELECT item_key, author_type, rollup_state, is_cross_repository FROM v_pr_live "
            "ORDER BY item_key"
        )
        self.assertEqual(live, [(KEY7, "User", "FAILURE", 0), (KEY9, "Bot", "PENDING", 1)])
        checks = self._rows(
            "SELECT name, kind, is_required FROM v_pr_checks WHERE item_key = ? ORDER BY name",
            KEY7,
        )
        self.assertEqual(
            checks,
            [
                ("build", "CheckRun", 1),
                ("ci/lint", "StatusContext", 0),
                ("test", "CheckRun", 1),
            ],
        )
        self.assertEqual(len(self._rows("SELECT * FROM v_pr_review_requests")), 2)
        comments = self._rows(
            "SELECT author, author_type FROM v_item_comments WHERE item_key = ? "
            "ORDER BY created_at",
            KEY7,
        )
        self.assertEqual(comments, [("carol", "User"), ("gemini-code-assist", "Bot")])
        body = self._rows("SELECT body FROM v_pr_basic WHERE item_key = ?", KEY7)
        self.assertEqual(body, [("Adds a visible focus ring.",)])
        self.assertEqual(len(self._rows("SELECT * FROM v_pr_latest_reviews")), 2)
        self.assertTrue(all(p == Priority.HOT for _w, p in self.transport.calls))

    def test_follow_up_pages_for_threads_and_contexts(self) -> None:
        self.hot.refresh_hot([KEY9])
        threads = self._rows("SELECT thread_id FROM v_pr_threads WHERE item_key = ?", KEY9)
        self.assertEqual(sorted(t[0] for t in threads), ["PRRT_9a", "PRRT_9b"])
        checks = self._rows(
            "SELECT name, is_required FROM v_pr_checks WHERE item_key = ? ORDER BY name",
            KEY9,
        )
        self.assertEqual(checks, [("build", 1), ("ci/legacy", 1)])
        self.assertTrue(any('after: "THREADS_P1"' in q for q in self.transport.queries))
        self.assertTrue(any('after: "CTX_P1"' in q for q in self.transport.queries))

    def test_unchanged_refresh_appends_no_change_rows(self) -> None:
        self.hot.refresh_hot([KEY7])
        seq = self.store.max_change_seq()
        self.assertEqual(self.hot.refresh_hot([KEY7]), [])
        self.assertEqual(self.store.max_change_seq(), seq)
        # Node ids are cached: the second refresh made no id lookup.
        lookups = [q for q in self.transport.queries if "k0: repository" in q]
        self.assertEqual(len(lookups), 1)

    def test_new_head_replaces_checks(self) -> None:
        self.hot.refresh_hot([KEY7])
        self.transport.batch = _fixture("hot_nodes_new_head.json")
        self.assertEqual(self.hot.refresh_hot([KEY7]), [KEY7])
        checks = self._rows("SELECT head_oid, name, status FROM v_pr_checks")
        self.assertEqual(checks, [("ccc333", "build", "IN_PROGRESS")])
        old = self._rows("SELECT COUNT(*) FROM check_runs WHERE head_oid = 'aaa111'")
        self.assertEqual(old, [(0,)])
        kinds = {c.kind for c in self.store.changes_since(0)}
        self.assertIn("checks", kinds)
        pr_change = [c for c in self.store.changes_since(0) if c.kind == "pr"][-1]
        self.assertIn("head_sha", pr_change.fields)

    def test_same_named_check_runs_are_all_stored(self) -> None:
        batch = _fixture("hot_nodes_batch.json")
        contexts = batch["data"]["pr0"]["commits"]["nodes"][0]["commit"]["statusCheckRollup"]
        rerun = copy.deepcopy(contexts["contexts"]["nodes"][0])
        rerun.update({"databaseId": 999, "conclusion": "FAILURE"})
        contexts["contexts"]["nodes"].append(rerun)
        contexts["contexts"]["totalCount"] += 1
        self.transport.batch = batch
        self.hot.refresh_hot([KEY7])
        builds = self._rows(
            "SELECT run_key, conclusion FROM v_pr_checks WHERE item_key = ? AND name = 'build' "
            "ORDER BY run_key",
            KEY7,
        )
        self.assertEqual(builds, [("CheckRun:111", "SUCCESS"), ("CheckRun:999", "FAILURE")])
        total = self._rows("SELECT rollup_total FROM v_pr_live WHERE item_key = ?", KEY7)[0][0]
        stored = self._rows("SELECT COUNT(*) FROM v_pr_checks WHERE item_key = ?", KEY7)[0][0]
        self.assertEqual(stored, total)

    def test_batches_of_twenty(self) -> None:
        keys = [f"octo-org/widgets#{n}" for n in range(1, 46)]
        for n, key in enumerate(keys, start=1):
            self.store.set_node_id(key, f"PR_{n}")
        self.hot.refresh_hot(keys)
        batches = [q for q in self.transport.queries if "pr0: node" in q]
        self.assertEqual(len(batches), 3)
        self.assertIn("pr19: node", batches[0])
        self.assertNotIn("pr20: node", batches[0])
        self.assertIn("pr4: node", batches[2])

    def test_budget_deferral_propagates(self) -> None:
        self.transport.deny = True
        with self.assertRaises(BudgetDeferred):
            self.hot.refresh_hot([KEY7])
        self.assertEqual(self.store.max_change_seq(), 0)

    def test_write_calls_replace_thread_activity_on_fake_store(self) -> None:
        replaced: list[tuple[int, int, int]] = []
        latest_reviews_calls: list[tuple[int, int, int]] = []

        class FakeHotStore:
            def get_node_ids(self, item_keys: Any) -> dict[str, str]:
                return {KEY7: "PR_kwDOA7"}

            def set_node_id(self, item_key: str, node_id: str) -> None:
                pass

            def ensure_items_repo(self, owner: str, name: str) -> Any:
                return self_outer.store.ensure_items_repo(owner, name)

            def upsert_pr_live(self, record: Any, now: float | None = None) -> list[str]:
                return ["updated_at"]

            def replace_check_runs(self, *args: Any, **kwargs: Any) -> bool:
                return False

            def replace_review_requests(self, *args: Any, **kwargs: Any) -> bool:
                return False

            def replace_latest_opinionated_reviews(self, *args: Any, **kwargs: Any) -> bool:
                return False

            def replace_latest_reviews(
                self,
                repo_id: int,
                pr_number: int,
                reviews: Any,
                now: float | None = None,
            ) -> bool:
                latest_reviews_calls.append((repo_id, pr_number, len(reviews)))
                return False

            def replace_thread_activity(self, repo_id: int, pr_number: int, threads: Any) -> bool:
                replaced.append((repo_id, pr_number, len(threads)))
                return True

            def upsert_item_comments(self, *args: Any, **kwargs: Any) -> bool:
                return False

        self_outer = self
        refresher = HotRefresher(self.transport, FakeHotStore())
        changed = refresher.refresh_hot([KEY7])
        self.assertEqual(changed, [KEY7])
        self.assertEqual(len(replaced), 1)
        self.assertEqual(replaced[0][1], 7)
        self.assertEqual(replaced[0][2], 2)
        self.assertEqual(len(latest_reviews_calls), 1)
        self.assertEqual(latest_reviews_calls[0][1], 7)

    def test_refresh_stores_latest_reviews_with_commit_oids(self) -> None:
        batch = _fixture("hot_nodes_batch.json")
        batch["data"]["pr0"]["reviews"] = {
            "nodes": [
                {
                    "state": "CHANGES_REQUESTED",
                    "submittedAt": "2026-11-02T12:00:00Z",
                    "commit": {"oid": "old000"},
                    "author": {"login": "carol", "__typename": "User"},
                },
                {
                    "state": "COMMENTED",
                    "submittedAt": "2026-11-03T09:15:00Z",
                    "commit": {"oid": "aaa111"},
                    "author": {"login": "carol", "__typename": "User"},
                },
                {
                    "state": "PENDING",
                    "submittedAt": "2026-11-03T10:00:00Z",
                    "commit": {"oid": "aaa111"},
                    "author": {"login": "carol", "__typename": "User"},
                },
                {
                    "state": "COMMENTED",
                    "submittedAt": "2026-11-02T13:00:00Z",
                    "commit": {"oid": "aaa111"},
                    "author": {"login": "gemini-code-assist", "__typename": "Bot"},
                },
            ]
        }
        self.transport.batch = batch
        self.assertEqual(self.hot.refresh_hot([KEY7]), [KEY7])
        rows = self._rows(
            "SELECT item_key, author, author_type, state, submitted_at, commit_oid "
            "FROM v_pr_latest_author_reviews ORDER BY author"
        )
        self.assertEqual(
            rows,
            [
                (
                    KEY7,
                    "carol",
                    "User",
                    "COMMENTED",
                    "2026-11-03T09:15:00Z",
                    "aaa111",
                ),
                (
                    KEY7,
                    "gemini-code-assist",
                    "Bot",
                    "COMMENTED",
                    "2026-11-02T13:00:00Z",
                    "aaa111",
                ),
            ],
        )

    def test_redirected_repo_merges_alias_and_writes_canonical_repo(self) -> None:
        old_key = "old-org/old-widgets#7"
        leased = self.store.add_watch("monitor", "c1", [old_key], 60, 600)
        self.assertEqual(leased, [old_key])
        self.assertEqual([r.full_name for r in self.store.list_repos()], ["old-org/old-widgets"])

        changed = self.hot.refresh_hot([old_key])
        self.assertEqual(changed, [KEY7])
        self.assertEqual([r.full_name for r in self.store.list_repos()], ["octo-org/widgets"])
        self.assertEqual(self.store.canonical_item_key(old_key), KEY7)
        self.assertEqual([w.item_key for w in self.store.active_watches()], [KEY7])
        live = self._rows("SELECT item_key FROM v_pr_live")
        self.assertEqual(live, [(KEY7,)])


if __name__ == "__main__":
    unittest.main()
