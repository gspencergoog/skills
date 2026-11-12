"""Unit tests for the signal poller (notifications, repo events, check runs)."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from collections.abc import Callable, Sequence
from pathlib import Path
from unittest import mock

import signals
from db_store import PackratStore
from gh_transport import GhTransport, RateLimited
from github_sync import GhCommandResult
from models import PrRecord, WatchLease
from rate_budget import RATE_BUDGET_FILENAME, RateBudget
from signals import (
    EVENTS_LAST_ID_KEY_PREFIX,
    EVENTS_STATE_KEY_PREFIX,
    NOTIFICATIONS_CURSOR_KEY,
    DirtySet,
    SignalPoller,
)

NOW = time.time()
_RESET = int(NOW) + 3600
CURSOR = "Tue, 03 Nov 2026 12:00:00 GMT"  # 2026-11-03T12:00:00Z


def _http(
    status: str, body: str = "", *, headers: Sequence[str] = (), remaining: int = 4900
) -> str:
    """Builds `gh api -i` output with core rate-limit headers."""
    lines = [
        f"HTTP/2.0 {status}",
        "X-Ratelimit-Limit: 5000",
        f"X-Ratelimit-Remaining: {remaining}",
        f"X-Ratelimit-Reset: {_RESET}",
        "X-Ratelimit-Resource: core",
        *headers,
    ]
    return "\r\n".join(lines) + "\r\n\r\n" + body


def _ok(stdout: str) -> GhCommandResult:
    return GhCommandResult(returncode=0, stdout=stdout, stderr="")


def _not_modified(*headers: str) -> GhCommandResult:
    return GhCommandResult(
        returncode=1, stdout=_http("304 Not Modified", headers=headers), stderr=""
    )


def _notification(
    thread_id: str, repo: str, number: int, updated_at: str, *, kind: str = "pulls"
) -> dict[str, object]:
    return {
        "id": thread_id,
        "reason": "review_requested",
        "unread": True,
        "updated_at": updated_at,
        "repository": {"full_name": repo},
        "subject": {
            "type": "PullRequest" if kind == "pulls" else "Issue",
            "url": f"https://api.github.com/repos/{repo}/{kind}/{number}",
        },
    }


Handler = Callable[[list[str]], GhCommandResult]


class RoutingRunner:
    """Fake GhRunner that answers each call with the first handler whose marker is in the path."""

    def __init__(self, routes: dict[str, Handler | list[GhCommandResult]]) -> None:
        self.routes = routes
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], timeout: int) -> GhCommandResult:
        argv = list(args)
        self.calls.append(argv)
        path = next(a for a in argv[1:] if not a.startswith("-"))
        for marker, handler in self.routes.items():
            if marker in path:
                if isinstance(handler, list):
                    return handler.pop(0)
                return handler(argv)
        raise AssertionError(f"unexpected gh call: {argv}")

    def paths(self, marker: str) -> list[str]:
        return [
            next(a for a in c[1:] if not a.startswith("-"))
            for c in self.calls
            if any(marker in a for a in c)
        ]

    def header_values(self, marker: str, header: str) -> list[str | None]:
        out: list[str | None] = []
        for call in self.calls:
            if not any(marker in a for a in call):
                continue
            values = [a.split(": ", 1)[1] for a in call if a.startswith(f"{header}: ")]
            out.append(values[0] if values else None)
        return out


class _SignalsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = PackratStore(Path(self._tmp.name) / "repos.sqlite")
        self.addCleanup(self.store.close)
        self.budget = RateBudget(Path(self._tmp.name) / RATE_BUDGET_FILENAME)
        self.repo = self.store.register_repo("octo-org", "widgets")

    def poller(self, runner: RoutingRunner) -> SignalPoller:
        return SignalPoller(GhTransport(self.budget, self.store, runner), self.store)

    def lease(self, *keys: str, freshness_s: int = 120) -> list[str]:
        return self.store.add_watch("monitor", "c1", keys, freshness_s, 900, now=NOW)

    def api_call_workers(self) -> list[str]:
        rows = self.store.conn.execute(
            "SELECT worker FROM api_calls ORDER BY rowid"
        ).fetchall()
        return [str(r[0]) for r in rows]


class TestDirtySet(unittest.TestCase):
    def test_merge_unions_in_place(self) -> None:
        a = DirtySet(hot={"o/r#1"}, mirror_repos={1})
        b = DirtySet(hot={"o/r#2"}, viewer=True, mirror_repos={2})
        self.assertIs(a.merge(b), a)
        self.assertEqual(
            a, DirtySet(hot={"o/r#1", "o/r#2"}, viewer=True, mirror_repos={1, 2})
        )
        self.assertTrue(DirtySet().is_empty())
        self.assertFalse(a.is_empty())


class TestPollNotifications(_SignalsTestCase):
    def test_304_returns_empty_dirty_set_and_sends_cursor(self) -> None:
        self.store.set_state(NOTIFICATIONS_CURSOR_KEY, CURSOR)
        runner = RoutingRunner(
            {"notifications": [_not_modified("X-Poll-Interval: 90")]}
        )
        poller = self.poller(runner)

        dirty = poller.poll_notifications(NOW)

        self.assertTrue(dirty.is_empty())
        self.assertEqual(
            runner.header_values("notifications", "If-Modified-Since"), [CURSOR]
        )
        self.assertEqual(poller.poll_interval_s, 90)
        # X-Poll-Interval is honored: no second call before it elapses.
        self.assertTrue(poller.poll_notifications(NOW + 30).is_empty())
        self.assertEqual(len(runner.calls), 1)

    def test_leased_pr_notification_marks_hot_and_unknown_repo_becomes_items(
        self,
    ) -> None:
        (leased,) = self.lease("octo-org/widgets#7")
        body = json.dumps(
            [
                _notification("t1", "octo-org/widgets", 7, "2026-11-03T13:00:00Z"),
                _notification(
                    "t2", "Other-Org/gadgets", 3, "2026-11-03T12:30:00Z", kind="issues"
                ),
                {
                    "id": "t3",
                    "reason": "subscribed",
                    "updated_at": "2026-11-03T12:10:00Z",
                    "repository": {"full_name": "octo-org/widgets"},
                    "subject": {
                        "type": "Release",
                        "url": "https://api.github.com/repos/x/y/releases/1",
                    },
                },
            ]
        )
        new_cursor = "Tue, 03 Nov 2026 13:00:00 GMT"
        runner = RoutingRunner(
            {
                "notifications": [
                    _ok(_http("200 OK", body, headers=[f"Last-Modified: {new_cursor}"]))
                ]
            }
        )

        dirty = self.poller(runner).poll_notifications(NOW)

        self.assertEqual(dirty.hot, {leased})
        self.assertTrue(dirty.viewer)
        other = self.store.get_repo("Other-Org/gadgets")
        assert other is not None
        self.assertEqual(other.mode, "items")
        self.assertEqual(self.store.get_state(NOTIFICATIONS_CURSOR_KEY), new_cursor)
        rows = self.store.conn.execute(
            "SELECT thread_id, item_key, subject_type FROM notifications ORDER BY thread_id"
        ).fetchall()
        self.assertEqual(
            [tuple(r) for r in rows],
            [
                ("t1", "octo-org/widgets#7", "PullRequest"),
                ("t2", "Other-Org/gadgets#3", "Issue"),
                ("t3", None, "Release"),
            ],
        )
        self.assertEqual(self.api_call_workers(), ["signals"])

    def test_unchanged_notification_does_not_mark_hot_again(self) -> None:
        self.lease("octo-org/widgets#7")
        body = json.dumps(
            [_notification("t1", "octo-org/widgets", 7, "2026-11-03T13:00:00Z")]
        )
        runner = RoutingRunner(
            {"notifications": [_ok(_http("200 OK", body)), _ok(_http("200 OK", body))]}
        )
        poller = self.poller(runner)
        self.assertEqual(len(poller.poll_notifications(NOW).hot), 1)
        again = poller.poll_notifications(NOW + 3600)
        self.assertTrue(again.is_empty())

    def test_multi_page_burst_is_read_until_cursor(self) -> None:
        self.store.set_state(NOTIFICATIONS_CURSOR_KEY, CURSOR)
        page1 = [
            _notification(f"a{i}", "octo-org/widgets", i, "2026-11-03T15:00:00Z")
            for i in range(1, 4)
        ]
        page2 = [
            _notification(f"b{i}", "octo-org/widgets", i, "2026-11-03T14:00:00Z")
            for i in range(4, 6)
        ]
        page3 = [
            _notification("c1", "octo-org/widgets", 6, "2026-11-03T12:30:00Z"),
            _notification("c2", "octo-org/widgets", 7, "2026-11-03T11:00:00Z"),
        ]

        def link(page: int) -> str:
            return (
                f"Link: <https://api.github.com/notifications?per_page=50&all=true&page={page}>;"
                ' rel="next", <https://api.github.com/notifications?page=9>; rel="last"'
            )

        runner = RoutingRunner(
            {
                "page=2": [_ok(_http("200 OK", json.dumps(page2), headers=[link(3)]))],
                "page=3": [_ok(_http("200 OK", json.dumps(page3), headers=[link(4)]))],
                "notifications": [
                    _ok(_http("200 OK", json.dumps(page1), headers=[link(2)]))
                ],
            }
        )

        self.poller(runner).poll_notifications(NOW)

        self.assertEqual(
            runner.paths("notifications"),
            [
                "notifications?per_page=50&all=true",
                "notifications?per_page=50&all=true&page=2",
                "notifications?per_page=50&all=true&page=3",
            ],
        )
        count = self.store.conn.execute(
            "SELECT COUNT(*) FROM notifications"
        ).fetchone()[0]
        self.assertEqual(count, 6)

    def test_notification_pages_decoded_once_and_filtered_by_cursor(self) -> None:
        self.store.set_state(NOTIFICATIONS_CURSOR_KEY, CURSOR)
        page1 = [
            _notification("n1", "octo-org/widgets", 1, "2026-11-03T13:00:00Z"),
        ]
        page2 = [
            _notification("n2", "octo-org/widgets", 2, "2026-11-03T12:00:00Z"),
            _notification("old1", "octo-org/widgets", 3, "2026-11-03T11:59:59Z"),
        ]
        link2 = 'Link: <https://api.github.com/notifications?per_page=50&all=true&page=2>; rel="next"'
        runner = RoutingRunner(
            {
                "page=2": [_ok(_http("200 OK", json.dumps(page2)))],
                "notifications": [
                    _ok(_http("200 OK", json.dumps(page1), headers=[link2]))
                ],
            }
        )
        with mock.patch(
            "signals._decode_list", wraps=signals._decode_list
        ) as decode_spy:
            self.poller(runner).poll_notifications(NOW)
            self.assertEqual(decode_spy.call_count, 2)
        stored_ids = [
            r[0]
            for r in self.store.conn.execute(
                "SELECT thread_id FROM notifications ORDER BY thread_id"
            ).fetchall()
        ]
        self.assertEqual(stored_ids, ["n1", "n2"])

    def test_rate_limited_page_keeps_cursor_and_raises(self) -> None:
        self.store.set_state(NOTIFICATIONS_CURSOR_KEY, CURSOR)
        limited = GhCommandResult(
            returncode=1,
            stdout=_http(
                "403 Forbidden", '{"message":"API rate limit exceeded"}', remaining=0
            ),
            stderr="gh: API rate limit exceeded (HTTP 403)",
        )
        runner = RoutingRunner({"notifications": [limited]})
        with self.assertRaises(RateLimited):
            self.poller(runner).poll_notifications(NOW)
        self.assertEqual(self.store.get_state(NOTIFICATIONS_CURSOR_KEY), CURSOR)


class TestPollRepoEvents(_SignalsTestCase):
    def test_events_go_through_transport_runner_with_own_etag(self) -> None:
        self.store.update_repo_sync_state(self.repo.repo_id, events_etag='"sync-etag"')
        self.store.ensure_items_repo("someone", "else")  # items repos are not polled
        (leased,) = self.lease("octo-org/widgets#5")
        events = [
            {
                "type": "PullRequestEvent",
                "payload": {"number": 5, "pull_request": {"number": 5}},
            },
            {"type": "IssueCommentEvent", "payload": {"issue": {"number": 9}}},
        ]
        runner = RoutingRunner(
            {
                "/events": [
                    _ok(
                        _http(
                            "200 OK",
                            json.dumps(events),
                            headers=['ETag: "sig-1"', "X-Poll-Interval: 60"],
                        )
                    ),
                    _not_modified('ETag: "sig-1"'),
                ]
            }
        )
        poller = self.poller(runner)

        dirty = poller.poll_repo_events(NOW)

        self.assertEqual(dirty.mirror_repos, {self.repo.repo_id})
        self.assertEqual(dirty.hot, {leased})
        self.assertIn("-i", runner.calls[0])
        self.assertEqual(runner.header_values("/events", "If-None-Match"), [None])
        self.assertEqual(self.api_call_workers(), ["signals"])
        state = json.loads(
            self.store.get_state(f"{EVENTS_STATE_KEY_PREFIX}{self.repo.repo_id}")
            or "{}"
        )
        self.assertEqual(state["etag"], '"sig-1"')
        repo = self.store.get_repo_by_id(self.repo.repo_id)
        assert repo is not None
        self.assertEqual(
            repo.events_etag, '"sync-etag"'
        )  # the mirror's ETag is untouched

        self.assertTrue(
            poller.poll_repo_events(NOW + 10).is_empty()
        )  # poll interval not elapsed
        dirty2 = poller.poll_repo_events(NOW + 61)
        self.assertTrue(dirty2.is_empty())
        self.assertEqual(
            runner.header_values("/events", "If-None-Match"), [None, '"sig-1"']
        )

    def test_low_core_budget_defers_events_without_gh_call(self) -> None:
        self.budget.record(
            {
                "x-ratelimit-resource": "core",
                "x-ratelimit-limit": "5000",
                "x-ratelimit-remaining": "100",
                "x-ratelimit-reset": str(_RESET),
            },
            "t",
        )
        self.store.register_repo("octo-org", "second")
        runner = RoutingRunner({})
        dirty = self.poller(runner).poll_repo_events(NOW)
        self.assertTrue(dirty.is_empty())
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.api_call_workers(), [])

    def test_last_event_id_stops_rescanning_old_events_for_leased_prs(self) -> None:
        (leased5,) = self.lease("octo-org/widgets#5")
        poll1_events = [
            {
                "id": "100",
                "type": "PullRequestEvent",
                "payload": {"number": 5, "pull_request": {"number": 5}},
            }
        ]
        poll2_events = [
            {
                "id": "101",
                "type": "PullRequestEvent",
                "payload": {"number": 99, "pull_request": {"number": 99}},
            },
            {
                "id": "100",
                "type": "PullRequestEvent",
                "payload": {"number": 5, "pull_request": {"number": 5}},
            },
        ]
        runner = RoutingRunner(
            {
                "/events": [
                    _ok(
                        _http(
                            "200 OK",
                            json.dumps(poll1_events),
                            headers=['ETag: "sig-1"', "X-Poll-Interval: 60"],
                        )
                    ),
                    _ok(
                        _http(
                            "200 OK",
                            json.dumps(poll2_events),
                            headers=['ETag: "sig-2"', "X-Poll-Interval: 60"],
                        )
                    ),
                ]
            }
        )
        poller = self.poller(runner)

        dirty1 = poller.poll_repo_events(NOW)
        self.assertEqual(dirty1.hot, {leased5})
        self.assertEqual(
            self.store.get_state(f"{EVENTS_LAST_ID_KEY_PREFIX}{self.repo.repo_id}"),
            "100",
        )

        dirty2 = poller.poll_repo_events(NOW + 61)
        self.assertEqual(dirty2.mirror_repos, {self.repo.repo_id})
        self.assertEqual(dirty2.hot, set())
        self.assertEqual(
            self.store.get_state(f"{EVENTS_LAST_ID_KEY_PREFIX}{self.repo.repo_id}"),
            "101",
        )


class TestPollCheckRuns(_SignalsTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store.upsert_pull_requests(
            [
                PrRecord(
                    repo_id=self.repo.repo_id,
                    number=5,
                    title="t",
                    state="OPEN",
                    updated_at="2026-11-03T00:00:00Z",
                    head_sha="sha1",
                )
            ]
        )
        (self.key,) = self.lease("octo-org/widgets#5", freshness_s=120)

    def leases(self) -> list[WatchLease]:
        return self.store.active_watches(NOW)

    def test_etags_are_reused_and_updated(self) -> None:
        runner = RoutingRunner(
            {
                "check-runs": [
                    _ok(_http("200 OK", '{"check_runs":[]}', headers=['ETag: "c1"'])),
                    _not_modified('ETag: "c1"'),
                    _ok(_http("200 OK", '{"check_runs":[{}]}', headers=['ETag: "c2"'])),
                ],
                "/status": [
                    _ok(_http("200 OK", '{"state":"pending"}', headers=['ETag: "s1"'])),
                    _not_modified('ETag: "s1"'),
                    _not_modified('ETag: "s1"'),
                ],
            }
        )
        poller = self.poller(runner)

        first = poller.poll_check_runs(self.leases(), NOW)
        self.assertEqual(first.hot, {self.key})
        self.assertEqual(
            self.store.get_rollup_etags(self.key), ("sha1", '"c1"', '"s1"')
        )
        self.assertEqual(
            runner.paths("commits"),
            [
                "repos/octo-org/widgets/commits/sha1/check-runs?per_page=100",
                "repos/octo-org/widgets/commits/sha1/status",
            ],
        )

        self.assertTrue(
            poller.poll_check_runs(self.leases(), NOW + 60).is_empty()
        )  # fresh
        self.assertEqual(len(runner.calls), 2)

        second = poller.poll_check_runs(self.leases(), NOW + 121)
        self.assertTrue(second.is_empty())
        self.assertEqual(
            runner.header_values("check-runs", "If-None-Match"), [None, '"c1"']
        )
        self.assertEqual(
            runner.header_values("/status", "If-None-Match"), [None, '"s1"']
        )

        third = poller.poll_check_runs(self.leases(), NOW + 250)
        self.assertEqual(third.hot, {self.key})
        self.assertEqual(
            self.store.get_rollup_etags(self.key), ("sha1", '"c2"', '"s1"')
        )
        rows = self.store.conn.execute(
            "SELECT worker, status, cost FROM api_calls"
        ).fetchall()
        self.assertEqual([r[1] for r in rows], [200, 200, 304, 304, 200, 304])
        self.assertEqual({r[0] for r in rows}, {"signals"})

    def test_new_head_drops_old_etags(self) -> None:
        self.store.set_rollup_etags(self.key, "sha0", '"old-c"', '"old-s"', now=NOW)
        runner = RoutingRunner(
            {
                "check-runs": [_ok(_http("200 OK", "{}", headers=['ETag: "c1"']))],
                "/status": [_ok(_http("200 OK", "{}", headers=['ETag: "s1"']))],
            }
        )
        dirty = self.poller(runner).poll_check_runs(self.leases(), NOW)
        self.assertEqual(dirty.hot, {self.key})
        self.assertEqual(runner.header_values("commits", "If-None-Match"), [None, None])
        self.assertEqual(
            self.store.get_rollup_etags(self.key), ("sha1", '"c1"', '"s1"')
        )

    def test_unknown_head_marks_hot_once_without_requests(self) -> None:
        (issue_key,) = self.lease("octo-org/widgets#77")
        lease = [w for w in self.leases() if w.item_key == issue_key]
        runner = RoutingRunner({})
        poller = self.poller(runner)
        self.assertEqual(poller.poll_check_runs(lease, NOW).hot, {issue_key})
        self.assertTrue(poller.poll_check_runs(lease, NOW + 500).is_empty())
        self.assertEqual(runner.calls, [])

    def test_not_found_head_is_skipped(self) -> None:
        missing = GhCommandResult(
            1,
            _http("404 Not Found", '{"message":"Not Found"}'),
            "gh: Not Found (HTTP 404)",
        )
        runner = RoutingRunner({"check-runs": [missing]})
        dirty = self.poller(runner).poll_check_runs(self.leases(), NOW)
        self.assertTrue(dirty.is_empty())
        self.assertEqual(self.store.get_rollup_etags(self.key), (None, None, None))

    def test_deferred_work_is_returned_by_next_poll(self) -> None:
        self.store.upsert_pull_requests(
            [
                PrRecord(
                    repo_id=self.repo.repo_id,
                    number=6,
                    title="t",
                    state="OPEN",
                    updated_at="2026-11-03T00:00:00Z",
                    head_sha="sha6",
                )
            ]
        )
        (key6,) = self.lease("octo-org/widgets#6")
        limited = GhCommandResult(
            1,
            _http(
                "403 Forbidden", '{"message":"API rate limit exceeded"}', remaining=0
            ),
            "gh: API rate limit exceeded (HTTP 403)",
        )
        runner = RoutingRunner(
            {
                "sha1/check-runs": [_ok(_http("200 OK", "{}", headers=['ETag: "c1"']))],
                "sha1/status": [_ok(_http("200 OK", "{}", headers=['ETag: "s1"']))],
                "sha6/check-runs": [limited],
            }
        )
        poller = self.poller(runner)
        with self.assertRaises(RateLimited):
            poller.poll_check_runs(self.leases(), NOW)
        # The sha1 change found before the rate limit is not lost.
        later = poller.poll_check_runs([], NOW + 1)
        self.assertEqual(later.hot, {self.key})
        self.assertNotIn(key6, later.hot)

    def test_304_check_poll_skips_rollup_write_and_canonicalizes_once(self) -> None:
        self.store.set_rollup_etags(self.key, "sha1", '"c1"', '"s1"', now=NOW)
        runner = RoutingRunner(
            {
                "check-runs": [_not_modified('ETag: "c1"')],
                "/status": [_not_modified('ETag: "s1"')],
            }
        )
        poller = self.poller(runner)
        with (
            mock.patch.object(
                self.store, "set_rollup_etags", wraps=self.store.set_rollup_etags
            ) as set_spy,
            mock.patch.object(
                self.store,
                "canonical_item_key",
                wraps=self.store.canonical_item_key,
            ) as canon_spy,
        ):
            dirty = poller.poll_check_runs(self.leases(), NOW)
            self.assertTrue(dirty.is_empty())
            self.assertEqual(set_spy.call_count, 0)
            self.assertEqual(canon_spy.call_count, 1)


if __name__ == "__main__":
    unittest.main()
