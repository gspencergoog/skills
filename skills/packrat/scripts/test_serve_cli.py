"""Unit tests for `serve_cli.py` and `DryRunStore`."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from db_store import PackratStore
from gh_transport import BudgetDeferred, GhApiError
from models import PrRecord, SyncStats, TimelineMode, TimelineStats
from rate_budget import RATE_BUDGET_FILENAME, Priority, RateBudget
from serve_cli import (
    DryRunRunner,
    DryRunStore,
    MirrorAdapter,
    TimelineAdapter,
    build_service,
    github_sync_stopped,
)


class TestDryRunStore(unittest.TestCase):
    """Tests that `DryRunStore` blocks unverified store attributes and drops writes."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="packrat-serve-cli-"))
        self.store = PackratStore(self.tmp / "repos.sqlite")

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_blocks_unprefixed_write_methods_and_conn(self) -> None:
        dry = DryRunStore(self.store)
        for blocked in (
            "register_repo",
            "rename_repo",
            "conn",
            "close",
            "unknown_method",
        ):
            with (
                self.subTest(blocked=blocked),
                self.assertRaisesRegex(
                    AttributeError, "DryRunStore blocks unverified store attribute"
                ),
            ):
                getattr(dry, blocked)

    def test_drops_writes_and_allows_verified_reads(self) -> None:
        self.store.register_repo("octo-org", "widgets")
        self.store.request_refresh(["octo-org/widgets#7"], now=100.0)
        dry = DryRunStore(self.store)

        self.assertEqual(dry.take_refresh_requests(), ["octo-org/widgets#7"])
        # Queue remains intact on the underlying store.
        self.assertEqual(dry.take_refresh_requests(), ["octo-org/widgets#7"])
        self.assertFalse(dry.replace_thread_activity(1, 7, []))
        self.assertEqual(len(dry.list_repos()), 1)
        self.assertIsNotNone(dry.get_repo("octo-org/widgets"))
        placeholder = dry.ensure_items_repo("other", "repo")
        self.assertEqual(placeholder.repo_id, 0)
        self.assertIsNone(self.store.get_repo("other/repo"))

    def test_take_refresh_requests_delegates_to_peek_refresh_requests(self) -> None:
        class FakePeekStore:
            def peek_refresh_requests(self) -> list[str]:
                return ["octo-org/widgets#42"]

        dry = DryRunStore(FakePeekStore())  # type: ignore[arg-type]
        self.assertEqual(dry.take_refresh_requests(), ["octo-org/widgets#42"])


class TestDryRunRunner(unittest.TestCase):
    """Tests `DryRunRunner` canned responses and summary formatting."""

    def test_records_rest_and_graphql_calls(self) -> None:
        runner = DryRunRunner()
        rest_res = runner(["api", "-i", "notifications"])
        gql_res = runner(
            ["api", "-i", "graphql", "-f", "query=query { viewer { login } }"]
        )
        self.assertIn("304 Not Modified", rest_res.stdout)
        self.assertIn('"data": {}', gql_res.stdout)
        self.assertEqual(
            runner.summary(),
            ["GET notifications", "graphql query { viewer { login } }"],
        )


class TestTimelineAdapters(unittest.TestCase):
    """Tests the timeline passes of `MirrorAdapter`, `TimelineAdapter`, and a dry run."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="packrat-serve-timeline-"))
        self.db_path = self.tmp / "repos.sqlite"
        self.store = PackratStore(self.db_path)
        self.mirror = self.store.register_repo("octo-org", "widgets")
        self.items = self.store.ensure_items_repo("octo-org", "gadgets")
        self.budget = RateBudget(self.tmp / RATE_BUDGET_FILENAME)
        self.transport = mock.Mock()
        self.transport.as_runner.side_effect = lambda worker, priority: (
            worker,
            priority,
        )

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mirror_sync(self, gh: SyncStats, timeline: TimelineStats) -> tuple[Any, Any]:
        """Runs `MirrorAdapter.sync_mirror` with faked GitHub and timeline syncs."""
        adapter = MirrorAdapter(self.store, self.transport, self.budget)
        with (
            mock.patch("serve_cli.sync_github_metadata", return_value=gh) as gh_mock,
            mock.patch("serve_cli.sync_repo_timelines", return_value=timeline) as tl,
        ):
            try:
                result: Any = adapter.sync_mirror(
                    [self.mirror.repo_id, self.items.repo_id]
                )
            except (BudgetDeferred, GhApiError) as exc:
                result = exc
        self.assertEqual(gh_mock.call_args.kwargs.get("backfill_max_pages"), 0)
        return result, tl

    def test_mirror_run_adds_incremental_timeline_pass(self) -> None:
        gh = SyncStats(repo_full_name="octo-org/widgets")
        result, tl = self._mirror_sync(
            gh, TimelineStats(items_synced=3, graphql_cost=2)
        )
        self.assertEqual([s.timeline.items_synced for s in result], [3])
        tl.assert_called_once()
        kwargs = tl.call_args.kwargs
        self.assertEqual(tl.call_args.args[1].full_name, "octo-org/widgets")
        self.assertEqual(kwargs["mode"], TimelineMode.INCREMENTAL)
        self.assertTrue(kwargs["drain_xrefs"])
        self.assertEqual(kwargs["runner"], ("mirror", Priority.MIRROR))
        self.assertEqual(kwargs["priority"], Priority.MIRROR)

    def test_deferred_mirror_timeline_raises_budget_deferred(self) -> None:
        gh = SyncStats(repo_full_name="octo-org/widgets")
        deferred = TimelineStats(
            deferred=True, errors=["timeline: rate_limited: slow down"]
        )
        result, _ = self._mirror_sync(gh, deferred)
        self.assertIsInstance(result, BudgetDeferred)
        self.assertIn("timeline", str(result))
        self.assertEqual(gh.deferred_streams, ["timeline"])
        self.assertEqual(gh.errors, [])

    def test_failed_mirror_timeline_raises_api_error(self) -> None:
        gh = SyncStats(repo_full_name="octo-org/widgets")
        result, _ = self._mirror_sync(
            gh, TimelineStats(errors=["timeline: http_error: 502"])
        )
        self.assertIsInstance(result, GhApiError)

    def test_mirror_skips_timeline_after_keyring_lock(self) -> None:
        gh = SyncStats(
            repo_full_name="octo-org/widgets",
            errors=["github prs sync failed after 0 page(s) (keyring_locked: locked)"],
        )
        result, tl = self._mirror_sync(gh, TimelineStats())
        self.assertIsInstance(result, GhApiError)
        tl.assert_not_called()
        self.assertTrue(github_sync_stopped(gh))
        self.assertFalse(
            github_sync_stopped(
                SyncStats(repo_full_name="x/y", deferred=True, deferred_streams=["prs"])
            )
        )

    def test_items_adapter_skips_mirror_repos_and_uses_viewer_runner(self) -> None:
        adapter = TimelineAdapter(self.store, self.transport, self.budget)
        self.transport.as_runner.assert_called_with("timeline", Priority.VIEWER)
        with mock.patch(
            "serve_cli.sync_repo_timelines", return_value=TimelineStats(items_synced=1)
        ) as tl:
            results = adapter.sync_items_timelines()
        self.assertEqual([r.items_synced for r in results], [1])
        self.assertEqual(
            [c.args[1].full_name for c in tl.call_args_list], ["octo-org/gadgets"]
        )
        kwargs = tl.call_args.kwargs
        self.assertEqual(kwargs["mode"], TimelineMode.ALL)
        self.assertFalse(kwargs["drain_xrefs"])
        self.assertEqual(kwargs["priority"], Priority.VIEWER)
        self.assertEqual(kwargs["runner"], ("timeline", Priority.VIEWER))

        for stats, expected in (
            (TimelineStats(deferred=True), BudgetDeferred),
            (TimelineStats(errors=["timeline: http_error: 502"]), GhApiError),
        ):
            with (
                self.subTest(expected=expected.__name__),
                mock.patch("serve_cli.sync_repo_timelines", return_value=stats),
                self.assertRaises(expected),
            ):
                adapter.sync_items_timelines()

    def test_dry_run_tick_with_timeline_work_writes_nothing(self) -> None:
        self.store.upsert_pull_requests(
            [
                PrRecord(
                    repo_id=self.items.repo_id,
                    number=5,
                    title="Add gadgets",
                    state="OPEN",
                    updated_at="2027-01-05T00:00:00Z",
                    node_id="PR_5",
                ),
                PrRecord(
                    repo_id=self.mirror.repo_id,
                    number=6,
                    title="Add widgets",
                    state="OPEN",
                    updated_at="2027-01-05T00:00:00Z",
                    node_id="PR_6",
                ),
            ]
        )
        with self.store.conn:
            self.store.conn.execute(
                "UPDATE pull_requests SET timeline_synced_at = '2027-01-01T00:00:00Z' "
                "WHERE number = 6"
            )
        self.store.enqueue_timeline_dirty([(self.mirror.repo_id, 6)], reason="mention")
        dry = DryRunStore(self.store)
        self.assertEqual(dry.take_timeline_dirty(self.mirror.repo_id, 20), [])
        self.assertEqual(dry.upsert_timeline_events([], repo_full_name="x/y"), 0)
        self.assertEqual(dry.count_timeline_pending(self.items.repo_id), 1)

        runner = DryRunRunner()
        service = build_service(dry, self.db_path, once=True, runner=runner)
        report = service.run_once()
        self.assertTrue(report.timeline_ran)
        self.assertEqual(report.errors, [])
        self.assertTrue(
            any("PR_5" in call for call in runner.summary()), runner.summary()
        )
        synced = self.store.conn.execute(
            "SELECT number, timeline_synced_at FROM pull_requests ORDER BY number"
        ).fetchall()
        self.assertEqual(
            [tuple(r) for r in synced], [(5, None), (6, "2027-01-01T00:00:00Z")]
        )
        self.assertEqual(self.store.table_counts()["timeline_dirty"], 1)
        self.assertEqual(self.store.table_counts()["timeline_events"], 0)


if __name__ == "__main__":
    unittest.main()
