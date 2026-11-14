"""Unit tests for `serve_cli.py` and `DryRunStore`."""

from __future__ import annotations

import argparse
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import serve_cli
from db_store import PackratStore
from gh_transport import BudgetDeferred, GhApiError, GhCommandResult
from models import PrRecord, SyncStats, TimelineMode, TimelineStats
from rate_budget import RATE_BUDGET_FILENAME, Priority, RateBudget
from serve_cli import (
    DryRunRunner,
    DryRunStore,
    MirrorAdapter,
    TimelineAdapter,
    build_service,
    changes_payload,
    cmd_watch,
    format_service_status,
    github_sync_stopped,
    service_status,
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


class TestDryRunStoreWave4Forwarding(unittest.TestCase):
    """Tests that the store methods added in Waves 1-3 are forwarded or dropped by `DryRunStore`."""

    WRITES = (
        "mark_timelines_synced",
        "update_pr_fork_owners",
        "set_gap_state",
        "touch_live_fetched_at",
        "wal_checkpoint_truncate",
        "set_commits_backfill_state",
        "upsert_indexed_files",
    )
    READS = (
        "get_gap_state",
        "get_live_pr_fingerprints",
        "read_schema_and_views_version",
        "read_snapshot",
        "get_indexed_files",
        "get_parser_overrides",
        "has_any_chunks",
        "query_fts_candidates",
        "read_all_embeddings_for_mmap",
        "resolve_filtered_chunk_ids",
        "stream_filtered_embeddings",
    )

    def test_writes_are_dropped_and_reads_forwarded(self) -> None:
        sentinel = object()

        class Spy:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def __getattr__(self, name: str) -> Any:
                def fn(*_a: Any, **_k: Any) -> object:
                    self.calls.append(name)
                    return sentinel

                return fn

        spy = Spy()
        dry = DryRunStore(spy)  # type: ignore[arg-type]
        for name in self.WRITES:
            with self.subTest(write=name):
                result = getattr(dry, name)(1, 2)
                self.assertIsNot(result, sentinel)
        self.assertEqual(spy.calls, [])
        for name in self.READS:
            with self.subTest(read=name):
                self.assertIs(getattr(dry, name)(), sentinel)
        self.assertEqual(list(spy.calls), list(self.READS))

    def test_take_refresh_requests_has_no_raw_sql_fallback(self) -> None:
        class NoPeekStore:
            conn = object()

        dry = DryRunStore(NoPeekStore())  # type: ignore[arg-type]
        with self.assertRaises(AttributeError):
            dry.take_refresh_requests()


class TestAdapterBudgetHandoff(unittest.TestCase):
    """Tests that adapters pass `budget=None` when the runner is already budgeted."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="packrat-serve-budget-"))
        self.store = PackratStore(self.tmp / "repos.sqlite")
        self.mirror = self.store.register_repo("octo-org", "widgets")
        self.items = self.store.ensure_items_repo("octo-org", "gadgets")
        self.budget = RateBudget(self.tmp / RATE_BUDGET_FILENAME)

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _transport(self, budgeted: bool) -> mock.Mock:
        runner = mock.Mock()
        runner.is_budgeted_runner = budgeted
        transport = mock.Mock()
        transport.as_runner.return_value = runner
        return transport

    def test_mirror_adapter_budget_handoff(self) -> None:
        for budgeted, expected in ((True, None), (False, self.budget)):
            with (
                self.subTest(budgeted=budgeted),
                mock.patch(
                    "serve_cli.sync_github_metadata",
                    return_value=SyncStats(repo_full_name="octo-org/widgets"),
                ) as gh,
                mock.patch(
                    "serve_cli.sync_repo_timelines", return_value=TimelineStats()
                ) as tl,
            ):
                adapter = MirrorAdapter(
                    self.store, self._transport(budgeted), self.budget
                )
                adapter.sync_mirror([self.mirror.repo_id])
                self.assertIs(gh.call_args.kwargs["budget"], expected)
                self.assertIs(tl.call_args.kwargs["budget"], expected)

    def test_timeline_adapter_budget_handoff(self) -> None:
        for budgeted, expected in ((True, None), (False, self.budget)):
            with (
                self.subTest(budgeted=budgeted),
                mock.patch(
                    "serve_cli.sync_repo_timelines", return_value=TimelineStats()
                ) as tl,
            ):
                adapter = TimelineAdapter(
                    self.store, self._transport(budgeted), self.budget
                )
                adapter.sync_items_timelines()
                self.assertIs(tl.call_args.kwargs["budget"], expected)


class TestChangesAndStatus(unittest.TestCase):
    """Tests `changes_payload` snapshotting and the service status health fields."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="packrat-serve-status-"))
        self.store = PackratStore(self.tmp / "repos.sqlite")
        self.repo = self.store.register_repo("octo-org", "widgets")

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_changes_payload_runs_inside_one_read_snapshot(self) -> None:
        for n in (1, 2, 3):
            self.store.append_change(
                f"octo-org/widgets#{n}", "pr", "updated", now=100.0 + n
            )
        in_tx: list[bool] = []
        real_changes_since = self.store.changes_since

        def spy_changes_since(since: int, limit: int) -> Any:
            in_tx.append(self.store.conn.in_transaction)
            return real_changes_since(since, limit)

        with (
            mock.patch.object(
                self.store, "read_snapshot", wraps=self.store.read_snapshot
            ) as snap,
            mock.patch.object(
                self.store, "changes_since", side_effect=spy_changes_since
            ),
        ):
            payload = changes_payload(self.store, 1, 10)
        snap.assert_called_once()
        self.assertEqual(in_tx, [True])
        self.assertFalse(self.store.conn.in_transaction)
        assert payload is not None
        self.assertEqual(payload["seq"], 3)
        self.assertEqual(len(payload["changes"]), 2)

    def test_service_status_surfaces_code_stale_and_health_fields(self) -> None:
        self.store.write_heartbeat(
            4242,
            "packrat-serve/2+000000000000",
            now=1_000.0,
            last_ok_at="2027-01-01T00:00:00Z",
            consecutive_failed_ticks=4,
        )
        with mock.patch.object(
            serve_cli,
            "format_service_version",
            create=True,
            return_value="packrat-serve/2+ffffffffffff",
        ):
            status = service_status(self.store, now=1_030.0)
        self.assertTrue(status["code_stale"])
        self.assertEqual(status["expected_version"], "packrat-serve/2+ffffffffffff")
        self.assertEqual(status["last_ok_at"], "2027-01-01T00:00:00Z")
        self.assertEqual(status["consecutive_failed_ticks"], 4)
        text = format_service_status(status)
        self.assertIn("Service: degraded", text)
        self.assertIn("consecutive_failed_ticks=4", text)
        self.assertIn("last_ok_at=2027-01-01T00:00:00Z", text)
        self.assertIn("stale code", text)

        stale = format_service_status({**status, "beat_age_s": 400})
        self.assertIn("Service: stale", stale)
        healthy = format_service_status(
            {**status, "consecutive_failed_ticks": 0, "code_stale": False}
        )
        self.assertIn("Service: running", healthy)
        self.assertNotIn("stale code", healthy)


class TestWatchAddProbe(unittest.TestCase):
    """Tests `watch add` validation of unregistered repositories against GitHub."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="packrat-serve-watch-"))
        self.store = PackratStore(self.tmp / "repos.sqlite")

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add(self, stdout: str, returncode: int = 0) -> tuple[list[str], str]:
        args = argparse.Namespace(
            watch_action="add",
            items=["new-org/thing#7"],
            consumer="monitor",
            id="c1",
            freshness=60,
            ttl=600,
            json=True,
        )
        calls: list[list[str]] = []

        def runner(argv: list[str], *_a: Any, **_k: Any) -> GhCommandResult:
            calls.append(list(argv))
            return GhCommandResult(returncode=returncode, stdout=stdout, stderr="")

        err = io.StringIO()
        with (
            mock.patch("serve_cli.run_gh_command", side_effect=runner),
            mock.patch("sys.stdout", io.StringIO()),
            mock.patch("sys.stderr", err),
        ):
            cmd_watch(args, self.store)
        return [" ".join(c) for c in calls], err.getvalue()

    def test_200_registers_items_repo(self) -> None:
        calls, err = self._add('HTTP/2.0 200 OK\r\n\r\n{"full_name": "new-org/thing"}')
        self.assertTrue(any("repos/new-org/thing" in c for c in calls), calls)
        repo = self.store.get_repo("new-org/thing")
        assert repo is not None
        self.assertEqual(repo.mode, "items")
        self.assertIn("registered new-org/thing (items)", err)

    def test_404_rejects_without_ghost_repo(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTP 404"):
            self._add('HTTP/2.0 404 Not Found\r\n\r\n{"message": "Not Found"}', 1)
        self.assertIsNone(self.store.get_repo("new-org/thing"))

    def test_offline_falls_back_with_warning(self) -> None:
        _, err = self._add("", returncode=4)
        self.assertIn("Warning: could not verify new-org/thing", err)
        self.assertIsNotNone(self.store.get_repo("new-org/thing"))

    def test_registered_repo_is_not_probed(self) -> None:
        self.store.register_repo("new-org", "thing")
        calls, err = self._add("HTTP/2.0 200 OK\r\n\r\n{}")
        self.assertEqual(calls, [])
        self.assertNotIn("registered", err)


class TestManagedGitAdapterAndDryRun(unittest.TestCase):
    """Covers `ManagedGitAdapter`, `DryRunManagedGit`, `_DRY_RUN_READS`, and `scoped_child_argv`."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="packrat-mgr-serve-"))
        self.store = PackratStore(self.tmp / "repos.sqlite")

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_adapter_polls_most_overdue_first_without_touching_repos_and_backs_off(self) -> None:
        import subprocess
        from serve_cli import ManagedGitAdapter, read_git_poll, scoped_child_argv

        origin = self.tmp / "origin.git"
        work = self.tmp / "work"
        subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(origin)], check=True, capture_output=True)
        subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(work), "config", "user.name", "T"], check=True)
        subprocess.run(["git", "-C", str(work), "config", "user.email", "t@e"], check=True)
        subprocess.run(["git", "-C", str(work), "checkout", "-B", "trunk"], check=True, capture_output=True)
        (work / "a.txt").write_text("hi\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(work), "add", "a.txt"], check=True)
        subprocess.run(["git", "-C", str(work), "commit", "-m", "c1"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(work), "push", "-u", "origin", "trunk"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(origin), "symbolic-ref", "HEAD", "refs/heads/trunk"], check=True)

        repo = self.store.register_repo(
            "octo",
            "widgets",
            default_branch="main",
            git_source="managed",
            update_binding=True,
        )
        with self.store.conn:
            self.store.conn.execute(
                "UPDATE repos SET git_url = ? WHERE repo_id = ?",
                (origin.as_uri(), repo.repo_id),
            )
        adapter = ManagedGitAdapter(
            self.store, extra_git_config=("-c", "protocol.file.allow=always")
        )
        results = adapter.poll_remotes(1000.0, budget_s=20.0)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].remote_default_branch, "trunk")
        # Service tick records remote_default_branch in git_poll without writing repos.default_branch.
        self.assertEqual(self.store.get_repo("octo/widgets").default_branch, "main")  # type: ignore[union-attr]
        poll = read_git_poll(self.store, repo.repo_id)
        self.assertEqual(poll["remote_default_branch"], "trunk")

        # Missing repo triggers clean clone/fetch on `fetch()`.
        fetched = adapter.fetch([repo.repo_id], budget_s=20.0)
        self.assertEqual(len(fetched), 1)
        self.assertEqual(fetched[0].error, "")

        # Scoped child argv carries --git-only --no-fetch and repeatable --repo.
        builder = scoped_child_argv(self.tmp / "repos.sqlite", self.store)
        argv = builder([repo.repo_id])
        self.assertIn("--git-only", argv)
        self.assertIn("--no-fetch", argv)
        self.assertIn("octo/widgets", argv)

    def test_dry_run_managed_git_and_store_reads_run_no_subprocess(self) -> None:
        from serve_cli import DryRunManagedGit

        repo = self.store.register_repo("octo", "widgets", git_source="managed", update_binding=True)
        dry_store = DryRunStore(self.store)
        self.assertEqual(dry_store.resolve_git_dir(repo), self.store.resolve_git_dir(repo))
        with mock.patch("git_cmd.run_git_command", side_effect=AssertionError("git called")):
            dry_git = DryRunManagedGit(dry_store)
            self.assertEqual(dry_git.poll_remotes(100.0, budget_s=10.0), [])
            self.assertEqual(dry_git.fetch([repo.repo_id], budget_s=10.0), [])
            self.assertTrue(dry_git.calls)


if __name__ == "__main__":
    unittest.main()
