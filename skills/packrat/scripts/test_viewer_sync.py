"""Unit tests for the `viewer` worker (`viewer_sync.ViewerSync`)."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from unittest import mock

from db_store import PackratStore
from gh_transport import BudgetDeferred, GhApiError, RateLimited
from live_store import iso_at
from models import REPO_MODE_ITEMS, REPO_MODE_MIRROR
from rate_budget import Priority
from viewer_sync import (
    SEARCH_QUERY,
    VIEWER_CONSUMER,
    VIEWER_CONSUMER_ID,
    VIEWER_FRESHNESS_S,
    VIEWER_LEASE_TTL_S,
    ViewerSync,
    lookback_date,
    search_strings,
)

T0 = 1_800_000_000.0
"""2027-01-15T08:00:00Z, the fixed clock for these tests."""


def _pr(repo: str, number: int, state: str = "OPEN") -> dict[str, Any]:
    return {
        "id": f"PR_{repo}_{number}",
        "number": number,
        "state": state,
        "repository": {"nameWithOwner": repo},
    }


def _issue(repo: str, number: int, **overrides: Any) -> dict[str, Any]:
    node = {
        "id": f"I_{repo}_{number}",
        "number": number,
        "title": f"Issue {number}",
        "body": "Details",
        "state": "OPEN",
        "url": f"https://github.com/{repo}/issues/{number}",
        "createdAt": "2027-01-10T00:00:00Z",
        "updatedAt": "2027-01-14T00:00:00Z",
        "closedAt": None,
        "repository": {"nameWithOwner": repo},
        "author": {"login": "carol"},
        "authorAssociation": "MEMBER",
        "labels": {"nodes": [{"name": "bug"}, {"name": "P1"}]},
        "assignees": {"nodes": [{"login": "me"}]},
    }
    node.update(overrides)
    return node


def _conn(nodes: Sequence[Any], cursor: str | None = None) -> dict[str, Any]:
    return {
        "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
        "nodes": list(nodes),
    }


def _page(
    authored: Sequence[Any] = (),
    review: Sequence[Any] = (),
    issues: Sequence[Any] = (),
    cursors: Mapping[str, str] | None = None,
    reviewed: Sequence[Any] = (),
) -> dict[str, Any]:
    """Builds one search payload; `cursors` maps an alias to its next-page cursor."""
    cursors = cursors or {}
    return {
        "data": {
            "rateLimit": {"cost": 1},
            "authored": _conn(authored, cursors.get("authored")),
            "reviewRequested": _conn(review, cursors.get("reviewRequested")),
            "reviewed": _conn(reviewed, cursors.get("reviewed")),
            "issues": _conn(issues, cursors.get("issues")),
        }
    }


class FakeTransport:
    """Scripted `GraphqlTransport`; each response is a payload or an exception to raise."""

    def __init__(self, *responses: dict[str, Any] | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def graphql(
        self,
        query: str,
        variables: Mapping[str, Any],
        *,
        worker: str,
        priority: Priority,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "query": query,
                "variables": dict(variables),
                "worker": worker,
                "priority": priority,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeHot:
    """`HotRefresh` that records the keys it was asked to refresh."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[list[str]] = []
        self.error = error

    def refresh_hot(self, keys: Sequence[str]) -> list[str]:
        self.calls.append(list(keys))
        if self.error is not None:
            raise self.error
        return list(keys)


class TestViewerSync(unittest.TestCase):
    """Tests viewer searches, set churn, issue upserts, leases, and hot refresh."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="packrat-viewer-"))
        self.store = PackratStore(self.tmp_dir / "repos.sqlite")
        self.hot = FakeHot()

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _sync(
        self, *responses: dict[str, Any] | Exception
    ) -> tuple[ViewerSync, FakeTransport]:
        transport = FakeTransport(*responses)
        return ViewerSync(transport, self.store, self.hot, clock=lambda: T0), transport

    def _viewer_items(self) -> dict[str, list[str]]:
        rows = self.store.conn.execute(
            "SELECT set_name, item_key FROM v_viewer_items ORDER BY set_name, item_key"
        ).fetchall()
        out: dict[str, list[str]] = {}
        for row in rows:
            out.setdefault(row["set_name"], []).append(row["item_key"])
        return out

    def _leases(self, now: float = T0) -> list[str]:
        return [
            w.item_key
            for w in self.store.active_watches(now=now)
            if (w.consumer, w.consumer_id) == (VIEWER_CONSUMER, VIEWER_CONSUMER_ID)
        ]

    def _first_page(self) -> dict[str, Any]:
        return _page(
            authored=[_pr("me/app", 1), _pr("me/app", 2)],
            review=[_pr("acme/lib", 9)],
            issues=[_issue("acme/lib", 30)],
        )

    def test_first_refresh_writes_sets_node_ids_leases_and_hot(self) -> None:
        sync, transport = self._sync(self._first_page())
        changed = sync.refresh_viewer()
        self.assertEqual(changed, ["acme/lib#30", "acme/lib#9", "me/app#1", "me/app#2"])
        self.assertEqual(
            self._viewer_items(),
            {
                "authored": ["me/app#1", "me/app#2"],
                "issues": ["acme/lib#30"],
                "review_requested": ["acme/lib#9"],
            },
        )
        self.assertEqual(self.hot.calls, [["acme/lib#9", "me/app#1", "me/app#2"]])
        self.assertEqual(
            self.store.get_node_ids(["me/app#1", "acme/lib#9"]),
            {"me/app#1": "PR_me/app_1", "acme/lib#9": "PR_acme/lib_9"},
        )
        call = transport.calls[0]
        self.assertEqual(
            (call["worker"], call["priority"]), ("viewer", Priority.VIEWER)
        )
        self.assertEqual(call["variables"]["first"], 20)
        self.assertIs(call["query"], SEARCH_QUERY)

    def test_leases_written_and_renewed(self) -> None:
        sync, _ = self._sync(self._first_page())
        sync.refresh_viewer()
        leases = [
            w
            for w in self.store.active_watches(now=T0)
            if w.consumer == VIEWER_CONSUMER
        ]
        self.assertEqual(
            [w.item_key for w in leases], ["acme/lib#9", "me/app#1", "me/app#2"]
        )
        self.assertTrue(all(w.freshness_s == VIEWER_FRESHNESS_S for w in leases))
        expires = iso_at(T0 + VIEWER_LEASE_TTL_S)
        self.assertTrue(all(w.lease_expires_at == expires for w in leases))
        self.assertEqual(self._leases(T0 + VIEWER_LEASE_TTL_S), [])

        later = T0 + 900
        sync2, _ = self._sync(self._first_page())
        sync2.refresh_viewer(now=later)
        self.assertEqual(len(self._leases(T0 + VIEWER_LEASE_TTL_S)), 3)

    def test_issue_upserted_in_items_repo(self) -> None:
        sync, _ = self._sync(self._first_page())
        sync.refresh_viewer()
        repo = self.store.get_repo("acme/lib")
        self.assertIsNotNone(repo)
        self.assertEqual(repo.mode, REPO_MODE_ITEMS)
        row = self.store.conn.execute(
            "SELECT * FROM issues WHERE repo_id = ? AND number = 30", (repo.repo_id,)
        ).fetchone()
        self.assertEqual(row["title"], "Issue 30")
        self.assertEqual(row["node_id"], "I_acme/lib_30")
        self.assertEqual(row["author"], "carol")
        self.assertEqual(row["author_association"], "MEMBER")
        self.assertEqual(json.loads(row["labels_json"]), ["bug", "P1"])
        self.assertEqual(json.loads(row["assignees_json"]), ["me"])
        self.assertEqual(row["updated_at"], "2027-01-14T00:00:00Z")
        self.assertEqual(self._leases(), ["acme/lib#9", "me/app#1", "me/app#2"])

    def test_merged_authored_pr_stays_in_set_but_is_not_leased(self) -> None:
        page = _page(authored=[_pr("me/app", 1), _pr("me/app", 3, state="MERGED")])
        sync, _ = self._sync(page)
        sync.refresh_viewer()
        self.assertEqual(self._viewer_items(), {"authored": ["me/app#1", "me/app#3"]})
        self.assertEqual(self._leases(), ["me/app#1"])
        self.assertEqual(self.hot.calls, [["me/app#1", "me/app#3"]])

    def test_pr_that_merges_is_refreshed_once_when_its_lease_drops(self) -> None:
        # H2: the merged PR stays in `authored` (no `added` key) but loses its lease.
        sync, _ = self._sync(_page(authored=[_pr("me/app", 1)]))
        sync.refresh_viewer()
        self.assertEqual(self._leases(), ["me/app#1"])
        self.hot.calls.clear()
        sync2, _ = self._sync(_page(authored=[_pr("me/app", 1, state="MERGED")]))
        sync2.refresh_viewer(now=T0 + 900)
        self.assertEqual(self._leases(T0 + 900), [])
        self.assertEqual(self.hot.calls, [["me/app#1"]])

    def test_deferred_refresh_is_queued_for_the_service(self) -> None:
        self.hot.error = BudgetDeferred("graphql", Priority.HOT, "rate limited")
        sync, _ = self._sync(_page(authored=[_pr("me/app", 1)]))
        sync.refresh_viewer()
        self.assertEqual(self.store.take_refresh_requests(), ["me/app#1"])

    def test_rate_limited_or_failed_refresh_of_unleased_pr_is_queued(self) -> None:
        # H2 follow-up: a dropped-lease PR must not lose its one refresh.
        for error in (
            RateLimited("graphql", Priority.HOT, "API rate limit exceeded"),
            GhApiError(502, "bad gateway"),
        ):
            with self.subTest(error=type(error).__name__):
                sync, _ = self._sync(_page(authored=[_pr("me/app", 1)]))
                sync.refresh_viewer()
                self.store.take_refresh_requests()
                self.hot.error = error
                merged = _page(authored=[_pr("me/app", 1, state="MERGED")])
                sync2, _ = self._sync(merged)
                sync2.refresh_viewer(now=T0 + 900)
                self.assertEqual(self.store.take_refresh_requests(), ["me/app#1"])
                self.hot.error = None

    def test_viewer_login_and_issue_comments_are_stored(self) -> None:
        comments = {
            "nodes": [
                {
                    "id": "IC_1",
                    "createdAt": "2027-01-12T00:00:00Z",
                    "author": {"login": "carol", "__typename": "User"},
                },
                {
                    "id": "IC_2",
                    "createdAt": "2027-01-13T00:00:00Z",
                    "author": {"login": "ci-bot", "__typename": "Bot"},
                },
            ]
        }
        page = _page(issues=[_issue("acme/lib", 30, comments=comments)])
        page["data"]["viewer"] = {"login": "me"}
        sync, _ = self._sync(page)
        sync.refresh_viewer()
        self.assertEqual(self.store.get_state("viewer_login"), "me")
        health = self.store.conn.execute(
            "SELECT viewer_login FROM v_service_health"
        ).fetchone()
        self.assertEqual(health[0], "me")
        rows = self.store.conn.execute(
            "SELECT item_key, kind, author, author_type FROM v_item_comments ORDER BY created_at"
        ).fetchall()
        self.assertEqual(
            [tuple(r) for r in rows],
            [
                ("acme/lib#30", "issue", "carol", "User"),
                ("acme/lib#30", "issue", "ci-bot", "Bot"),
            ],
        )
        body = self.store.conn.execute("SELECT body FROM v_issue_basic").fetchone()[0]
        self.assertEqual(body, "Details")

    def test_membership_churn(self) -> None:
        sync, _ = self._sync(
            self._first_page(),
            _page(
                authored=[_pr("me/app", 2), _pr("me/app", 3)],
                review=[_pr("acme/lib", 9)],
                issues=[],
            ),
            _page(
                authored=[_pr("me/app", 2), _pr("me/app", 3)],
                review=[_pr("acme/lib", 9)],
                issues=[],
            ),
        )
        sync.refresh_viewer(now=T0)
        changed = sync.refresh_viewer(now=T0 + 60)
        self.assertEqual(changed, ["acme/lib#30", "me/app#1", "me/app#3"])
        self.assertEqual(self.hot.calls[-1], ["me/app#1", "me/app#3"])
        self.assertEqual(self._leases(T0 + 60), ["acme/lib#9", "me/app#2", "me/app#3"])
        self.assertEqual(
            self._viewer_items(),
            {"authored": ["me/app#2", "me/app#3"], "review_requested": ["acme/lib#9"]},
        )

        self.assertEqual(sync.refresh_viewer(now=T0 + 120), [])
        self.assertEqual(len(self.hot.calls), 2)

    def test_pr_moving_between_sets_keeps_lease(self) -> None:
        sync, _ = self._sync(
            _page(authored=[_pr("me/app", 1)]),
            _page(review=[_pr("me/app", 1)]),
        )
        sync.refresh_viewer(now=T0)
        self.assertEqual(sync.refresh_viewer(now=T0 + 60), ["me/app#1"])
        self.assertEqual(self._leases(T0 + 60), ["me/app#1"])
        self.assertEqual(self._viewer_items(), {"review_requested": ["me/app#1"]})

    def test_lookback_filter_in_search_strings(self) -> None:
        self.assertEqual(lookback_date(7, T0), "2027-01-08")
        sync, transport = self._sync(_page(), _page())
        sync.refresh_viewer()
        sync.refresh_viewer(lookback_days=30)
        week, month = (c["variables"] for c in transport.calls)
        self.assertEqual(
            week["authored"], "is:pr author:@me archived:false updated:>=2027-01-08"
        )
        self.assertEqual(
            week["issues"], "is:issue involves:@me archived:false updated:>=2027-01-08"
        )
        self.assertEqual(
            week["reviewReq"], "is:pr is:open review-requested:@me archived:false"
        )
        self.assertEqual(
            week["reviewed"],
            "is:pr is:open reviewed-by:@me -author:@me archived:false",
        )
        self.assertIn("updated:>=2026-12-16", month["authored"])
        self.assertIn("updated:>=2026-12-16", month["issues"])
        self.assertEqual(search_strings(30, T0)["reviewRequested"], month["reviewReq"])
        self.assertEqual(search_strings(30, T0)["reviewed"], month["reviewed"])

    def test_keys_use_registered_repo_casing(self) -> None:
        self.store.register_repo("Octo-Org", "Widgets")
        sync, _ = self._sync(
            _page(
                authored=[_pr("octo-org/widgets", 5)],
                issues=[_issue("octo-org/widgets", 6)],
            )
        )
        self.assertEqual(
            sync.refresh_viewer(), ["Octo-Org/Widgets#5", "Octo-Org/Widgets#6"]
        )
        self.assertEqual(
            [r.full_name for r in self.store.list_repos()], ["Octo-Org/Widgets"]
        )
        self.assertEqual(self.store.get_repo("Octo-Org/Widgets").mode, REPO_MODE_MIRROR)
        self.assertEqual(self._leases(), ["Octo-Org/Widgets#5"])

    def test_pagination_follows_each_alias_cursor(self) -> None:
        sync, transport = self._sync(
            _page(
                authored=[_pr("me/app", 1), None, {}],
                review=[_pr("me/app", 7)],
                cursors={"authored": "c1", "reviewed": "r1"},
                reviewed=[_pr("acme/lib", 11)],
            ),
            _page(authored=[_pr("me/app", 2)], reviewed=[_pr("acme/lib", 12)]),
        )
        changed = sync.refresh_viewer()
        self.assertEqual(
            changed,
            ["acme/lib#11", "acme/lib#12", "me/app#1", "me/app#2", "me/app#7"],
        )
        first, second = (c["variables"] for c in transport.calls)
        self.assertEqual(
            (
                first["afterAuthored"],
                first["afterReviewed"],
                first["withReview"],
                first["withReviewed"],
                first["withIssues"],
            ),
            (None, None, True, True, True),
        )
        self.assertEqual(second["afterAuthored"], "c1")
        self.assertEqual(second["afterReviewed"], "r1")
        self.assertEqual(
            (
                second["withAuthored"],
                second["withReview"],
                second["withReviewed"],
                second["withIssues"],
            ),
            (True, False, True, False),
        )
        self.assertEqual(
            self._viewer_items(),
            {
                "authored": ["me/app#1", "me/app#2"],
                "review_requested": ["me/app#7"],
                "reviewed": ["acme/lib#11", "acme/lib#12"],
            },
        )
        self.assertEqual(
            self._leases(),
            ["acme/lib#11", "acme/lib#12", "me/app#1", "me/app#2", "me/app#7"],
        )

    def test_reviewed_pr_closing_or_merging_gets_final_h2_refresh(self) -> None:
        # When an open `reviewed` PR merges or closes, it leaves the `is:open`
        # search results completely (`unleased` is no longer in `pr_keys`),
        # and still receives one final H2 hot refresh so `v_pr_live` updates.
        sync, _ = self._sync(
            _page(reviewed=[_pr("acme/lib", 42)]),
            _page(reviewed=[]),
        )
        self.assertEqual(sync.refresh_viewer(now=T0), ["acme/lib#42"])
        self.assertEqual(self._viewer_items(), {"reviewed": ["acme/lib#42"]})
        self.assertEqual(self._leases(T0), ["acme/lib#42"])
        self.hot.calls.clear()

        self.assertEqual(sync.refresh_viewer(now=T0 + 900), ["acme/lib#42"])
        self.assertEqual(self._viewer_items(), {})
        self.assertEqual(self._leases(T0 + 900), [])
        self.assertEqual(self.hot.calls, [["acme/lib#42"]])

    def test_missing_alias_ends_its_paging(self) -> None:
        sync, transport = self._sync({"data": {"authored": _conn([_pr("me/app", 1)])}})
        self.assertEqual(sync.refresh_viewer(), ["me/app#1"])
        self.assertEqual(len(transport.calls), 1)

    def test_server_error_halves_page_size(self) -> None:
        sync, transport = self._sync(
            GhApiError(502, "Bad Gateway"),
            GhApiError(502, "Bad Gateway"),
            _page(authored=[_pr("me/app", 1)]),
            _page(),
        )
        self.assertEqual(sync.refresh_viewer(), ["me/app#1"])
        sizes = [c["variables"]["first"] for c in transport.calls]
        self.assertEqual(sizes, [20, 10, 5])
        sync.refresh_viewer(now=T0 + 1)
        self.assertEqual(transport.calls[-1]["variables"]["first"], 20)

    def test_failures_write_nothing(self) -> None:
        failures: list[Exception] = [
            GhApiError(400, "Bad query"),
            BudgetDeferred("graphql", Priority.VIEWER),
        ]
        for error in failures:
            with self.subTest(error=type(error).__name__):
                sync, _ = self._sync(error)
                with self.assertRaises(type(error)):
                    sync.refresh_viewer()
        self.assertEqual(self._viewer_items(), {})
        self.assertEqual(self.store.list_repos(), [])

    def test_failure_on_later_page_writes_nothing(self) -> None:
        sync, _ = self._sync(
            _page(authored=[_pr("me/app", 1)], cursors={"authored": "c1"}),
            GhApiError(502, "Bad Gateway"),
            GhApiError(502, "Bad Gateway"),
            GhApiError(502, "Bad Gateway"),
        )
        with self.assertRaises(GhApiError):
            sync.refresh_viewer()
        self.assertEqual(self._viewer_items(), {})
        self.assertEqual(self._leases(), [])
        self.assertEqual(self.hot.calls, [])

    def test_hot_budget_deferral_is_ignored(self) -> None:
        self.hot = FakeHot(error=BudgetDeferred("graphql", Priority.HOT))
        sync, _ = self._sync(self._first_page())
        self.assertEqual(len(sync.refresh_viewer()), 4)
        self.assertEqual(len(self.hot.calls), 1)
        self.assertEqual(self._leases(), ["acme/lib#9", "me/app#1", "me/app#2"])

    def test_empty_results_clear_sets_and_leases(self) -> None:
        sync, _ = self._sync(self._first_page(), _page())
        sync.refresh_viewer(now=T0)
        changed = sync.refresh_viewer(now=T0 + 60)
        self.assertEqual(changed, ["acme/lib#30", "acme/lib#9", "me/app#1", "me/app#2"])
        self.assertEqual(self._viewer_items(), {})
        self.assertEqual(self._leases(T0 + 60), [])
        self.assertEqual(
            self.hot.calls,
            [
                ["acme/lib#9", "me/app#1", "me/app#2"],
                ["acme/lib#9", "me/app#1", "me/app#2"],
            ],
        )

    def test_renamed_repo_in_viewer_search_merges_alias(self) -> None:
        pr_old = {
            "id": "PR_shared_1",
            "number": 1,
            "state": "OPEN",
            "repository": {"nameWithOwner": "old-org/widgets"},
        }
        pr_new = {
            "id": "PR_shared_1",
            "number": 1,
            "state": "OPEN",
            "repository": {"nameWithOwner": "octo-org/widgets"},
        }
        issue_old = _issue("old-org/widgets", 30, id="I_shared_30")
        issue_new = _issue("octo-org/widgets", 30, id="I_shared_30")
        sync, _ = self._sync(
            _page(authored=[pr_old], issues=[issue_old]),
            _page(authored=[pr_new], issues=[issue_new]),
        )
        sync.refresh_viewer(now=T0)
        self.assertEqual(
            [r.full_name for r in self.store.list_repos()], ["old-org/widgets"]
        )

        changed = sync.refresh_viewer(now=T0 + 60)
        self.assertEqual(changed, [])
        self.assertEqual(
            [r.full_name for r in self.store.list_repos()], ["octo-org/widgets"]
        )
        self.assertEqual(
            self.store.canonical_item_key("old-org/widgets#1"), "octo-org/widgets#1"
        )
        self.assertEqual(
            self._viewer_items(),
            {"authored": ["octo-org/widgets#1"], "issues": ["octo-org/widgets#30"]},
        )
        self.assertEqual(self._leases(T0 + 60), ["octo-org/widgets#1"])

    def test_unchanged_second_refresh_skips_set_node_id_and_repo_node_scans(
        self,
    ) -> None:
        sync, _ = self._sync(self._first_page(), self._first_page())
        sync.refresh_viewer(now=T0)
        with (
            mock.patch.object(
                self.store, "set_node_id", wraps=self.store.set_node_id
            ) as set_node_spy,
            mock.patch.object(
                self.store,
                "find_repo_by_node_id",
                wraps=self.store.find_repo_by_node_id,
            ) as find_node_spy,
            mock.patch.object(
                self.store,
                "ensure_items_repo",
                wraps=self.store.ensure_items_repo,
            ) as ensure_repo_spy,
        ):
            self.assertEqual(sync.refresh_viewer(now=T0 + 60), [])
            self.assertEqual(set_node_spy.call_count, 0)
            self.assertEqual(find_node_spy.call_count, 0)
            self.assertEqual(ensure_repo_spy.call_count, 0)

    def test_page_size_recovers_after_transient_502_across_clean_pages(self) -> None:
        sync, transport = self._sync(
            GhApiError(502, "Bad Gateway"),
            GhApiError(502, "Bad Gateway"),
            _page(authored=[_pr("me/app", 1)], cursors={"authored": "c1"}),
            _page(authored=[_pr("me/app", 2)], cursors={"authored": "c2"}),
            _page(authored=[_pr("me/app", 3)]),
        )
        self.assertEqual(sync.refresh_viewer(), ["me/app#1", "me/app#2", "me/app#3"])
        sizes = [c["variables"]["first"] for c in transport.calls]
        self.assertEqual(sizes, [20, 10, 5, 10, 20])


if __name__ == "__main__":
    unittest.main()
