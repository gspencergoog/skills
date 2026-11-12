"""Unit tests for the `packrat serve` scheduler."""

from __future__ import annotations

import contextlib
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gh_transport import BudgetDeferred, GhApiError
from models import ServiceHeartbeat, WatchLease, iso_at
from rate_budget import Priority
from service import (
    BACKFILL_INTERVAL_S,
    MAX_CHECK_LEASES_PER_TICK,
    MAX_REFRESH_ATTEMPTS,
    SERVICE_VERSION,
    VIEWER_INTERVAL_S,
    PackratService,
    format_service_version,
    spawn_niced_child,
)
from sync_runtime import SyncLockInfo, acquire_sync_lock

T0 = 1_000_000.0


@dataclass
class Dirty:
    hot: set[str] = field(default_factory=set)
    viewer: bool = False
    mirror_repos: set[int] = field(default_factory=set)


class FakeStore:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.requests: list[str] = []
        self.requeued: list[list[str]] = []
        self.watches: list[WatchLease] = []
        self.heartbeats: list[tuple[int, str, float | None]] = []
        self.heartbeat_records: list[ServiceHeartbeat] = []
        self.initial_heartbeat: ServiceHeartbeat | None = None
        self.prunes: list[float | None] = []
        self.expired_prunes = 0
        self.fetched_at: dict[str, float] = {}
        self.requeue_for_real = False
        self.schema_version = 13
        self.views_version = 2
        self.wal_checkpoints: list[tuple[int, int, int]] = []
        self.mirror_repos: list[Any] = [object()]

    def take_refresh_requests(self) -> list[str]:
        self.log.append("take_requests")
        taken, self.requests = self.requests, []
        return taken

    def request_refresh(
        self, item_keys: Iterable[str], now: float | None = None
    ) -> list[str]:
        keys = list(item_keys)
        self.requeued.append(keys)
        if self.requeue_for_real:
            self.requests.extend(k for k in keys if k not in self.requests)
        return keys

    def active_watches(self, now: float | None = None) -> list[WatchLease]:
        return list(self.watches)

    def prune_expired_watches(self, now: float | None = None) -> int:
        self.expired_prunes += 1
        return 0

    def write_heartbeat(
        self,
        pid: int,
        version: str,
        now: float | None = None,
        *,
        last_ok_at: str | None = None,
        consecutive_failed_ticks: int | None = None,
    ) -> None:
        self.heartbeats.append((pid, version, now))
        self.heartbeat_records.append(
            ServiceHeartbeat(
                pid=pid,
                started_at=iso_at(now),
                beat_at=iso_at(now),
                version=version,
                last_ok_at=last_ok_at,
                consecutive_failed_ticks=consecutive_failed_ticks or 0,
            )
        )

    def read_heartbeat(self) -> ServiceHeartbeat | None:
        if self.heartbeat_records:
            return self.heartbeat_records[-1]
        return self.initial_heartbeat

    def read_schema_and_views_version(self) -> tuple[int, int]:
        return (self.schema_version, self.views_version)

    def wal_checkpoint_truncate(self) -> tuple[int, int, int]:
        result = (0, 4, 4)
        self.wal_checkpoints.append(result)
        return result

    def list_repos(self, mode: str | None = None) -> list[Any]:
        if mode == "mirror":
            return list(self.mirror_repos)
        return list(self.mirror_repos)

    def prune(self, now: float | None = None) -> dict[str, int]:
        self.prunes.append(now)
        return {}

    def get_live_fetched_at(self, item_keys: Iterable[str]) -> dict[str, float]:
        return {k: self.fetched_at[k] for k in item_keys if k in self.fetched_at}


class FakeSignals:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.poll_interval_s = 60
        self.notifications: list[Dirty | Exception] = []
        self.events: list[Dirty | Exception] = []
        self.check_calls: list[list[str]] = []
        self.fail_checks = False

    @staticmethod
    def _next(queue: list[Dirty | Exception]) -> Dirty:
        item = queue.pop(0) if queue else Dirty()
        if isinstance(item, Exception):
            raise item
        return item

    def poll_notifications(self, now: float) -> Dirty:
        self.log.append("notifications")
        return self._next(self.notifications)

    def poll_repo_events(self, now: float) -> Dirty:
        self.log.append("events")
        return self._next(self.events)

    def poll_check_runs(self, leases: Sequence[WatchLease], now: float) -> Dirty:
        self.check_calls.append([lease.item_key for lease in leases])
        if self.fail_checks:
            raise GhApiError(502, "bad gateway")
        return Dirty()


class FakeHot:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.calls: list[list[str]] = []
        self.raise_next: Exception | None = None
        self.always_fail: set[str] = set()

    def refresh_hot(self, keys: Sequence[str]) -> list[str]:
        self.log.append(f"hot:{','.join(keys)}")
        self.calls.append(list(keys))
        if self.always_fail & set(keys):
            raise GhApiError(502, "bad gateway")
        if self.raise_next is not None:
            exc, self.raise_next = self.raise_next, None
            raise exc
        return list(keys)


class FakeViewer:
    def __init__(self) -> None:
        self.calls = 0
        self.raise_next: Exception | None = None

    def refresh_viewer(self, lookback_days: int = 7) -> list[str]:
        self.calls += 1
        if self.raise_next is not None:
            exc, self.raise_next = self.raise_next, None
            raise exc
        return []


class FakeMirror:
    def __init__(self) -> None:
        self.calls: list[list[int]] = []

    def sync_mirror(self, repo_ids: Collection[int]) -> None:
        self.calls.append(list(repo_ids))


class FakeBudget:
    def __init__(self, allowed: set[Priority] | None = None) -> None:
        self.allowed = allowed if allowed is not None else set(Priority)

    def allows(
        self, resource: str, priority: Priority, now: float | None = None
    ) -> bool:
        return priority in self.allowed


class FakeChild:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.code: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.code

    def terminate_group(self) -> None:
        self.terminated = True
        self.code = -15


def lease(key: str, freshness: int = 120, consumer: str = "monitor") -> WatchLease:
    return WatchLease(
        consumer=consumer,
        consumer_id="c1",
        item_key=key,
        freshness_s=freshness,
        lease_expires_at="2099-01-01T00:00:00Z",
    )


class ServiceHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.events: list[str] = []
        self.store = FakeStore(self.events)
        self.signals = FakeSignals(self.events)
        self.hot = FakeHot(self.events)
        self.viewer = FakeViewer()
        self.mirror = FakeMirror()
        self.budget = FakeBudget()
        self.lock_free = True
        self.lock_holder_pid: int | None = None
        self.lock_calls = 0
        self.children: list[FakeChild] = []
        self.logs: list[str] = []

    @contextlib.contextmanager
    def _lock(self) -> Iterator[SyncLockInfo]:
        self.lock_calls += 1
        yield SyncLockInfo(
            acquired=self.lock_free,
            lock_path=Path("/tmp/sync.lock"),
            holder_pid=None if self.lock_free else self.lock_holder_pid,
        )

    def _spawn(self, argv: Sequence[str]) -> FakeChild:
        child = FakeChild(pid=4000 + len(self.children))
        self.children.append(child)
        return child

    def make(self, child_argv: Sequence[str] | None = None) -> PackratService:
        return PackratService(
            self.store,
            self.signals,
            self.hot,
            self.viewer,
            self.mirror,
            child_argv,
            clock=lambda: T0,
            budget=self.budget,
            sync_lock=self._lock,
            spawn=self._spawn,
            log=self.logs.append,
        )


class TestScheduling(ServiceHarness):
    def test_signals_and_viewer_follow_due_times(self) -> None:
        self.signals.poll_interval_s = 120  # X-Poll-Interval raised above 60 s
        svc = self.make()
        first = svc.run_once(T0)
        self.assertTrue(first.signals_polled)
        self.assertTrue(first.viewer_ran)
        self.assertFalse(svc.run_once(T0 + 60).signals_polled)
        self.assertTrue(svc.run_once(T0 + 120).signals_polled)
        self.assertFalse(svc.run_once(T0 + 899).viewer_ran)
        self.assertTrue(svc.run_once(T0 + 900).viewer_ran)

    def test_viewer_signal_runs_viewer_early(self) -> None:
        svc = self.make()
        svc.run_once(T0)
        self.signals.notifications.append(Dirty(viewer=True))
        self.assertTrue(svc.run_once(T0 + 60).viewer_ran)
        self.assertEqual(self.viewer.calls, 2)

    def test_refresh_requests_are_served_first(self) -> None:
        self.store.requests = ["o/r#1", "o/r#1"]
        self.signals.notifications.append(Dirty(hot={"o/r#2", "o/r#1"}))
        report = self.make().run_once(T0)
        self.assertEqual(self.events[:2], ["take_requests", "hot:o/r#1"])
        self.assertLess(
            self.events.index("hot:o/r#1"), self.events.index("notifications")
        )
        self.assertEqual(report.requested, ["o/r#1"])
        self.assertEqual(self.hot.calls, [["o/r#1"], ["o/r#2"]])

    def test_failing_request_is_retried_once_per_tick_then_dropped(self) -> None:
        # M5: one bad key must not be retried by every drain in a tick, forever.
        self.store.requeue_for_real = True
        self.store.requests = ["o/r#bad"]
        self.hot.always_fail = {"o/r#bad"}
        svc = self.make()
        svc.run_once(T0)
        self.assertEqual(self.hot.calls, [["o/r#bad"]])
        for tick in range(1, MAX_REFRESH_ATTEMPTS + 2):
            svc.run_once(T0 + 5 * tick)
        attempts = sum(call == ["o/r#bad"] for call in self.hot.calls)
        self.assertEqual(attempts, MAX_REFRESH_ATTEMPTS)
        self.assertEqual(self.store.requests, [])
        self.assertTrue(any("o/r#bad dropped" in line for line in self.logs))

    def test_failed_request_refresh_is_requeued(self) -> None:
        self.store.requests = ["o/r#1"]
        self.hot.raise_next = BudgetDeferred("graphql", Priority.HOT, "rate limited")
        report = self.make().run_once(T0)
        self.assertEqual(self.store.requeued, [["o/r#1"]])
        self.assertIn("hot", report.deferred)

    def test_lease_sweep_refreshes_without_a_signal(self) -> None:
        self.store.watches = [lease("o/r#1", 120), lease("o/r#1", 900, "inflight")]
        svc = self.make()
        self.assertEqual(svc.run_once(T0).swept, ["o/r#1"])
        self.assertEqual(svc.run_once(T0 + 300).swept, [])
        self.assertEqual(svc.run_once(T0 + 600).swept, ["o/r#1"])
        self.assertEqual(self.hot.calls, [["o/r#1"], ["o/r#1"]])

    def test_signalled_key_resets_sweep_clock(self) -> None:
        self.store.watches = [lease("o/r#1", 120)]
        svc = self.make()
        svc.run_once(T0)
        self.signals.notifications.append(Dirty(hot={"o/r#1"}))
        report = svc.run_once(T0 + 540)
        self.assertEqual((report.hot_keys, report.swept), (["o/r#1"], []))
        self.assertEqual(svc.run_once(T0 + 600).swept, [])
        self.assertEqual(svc.run_once(T0 + 1140).swept, ["o/r#1"])

    def test_check_runs_polled_per_lease_freshness(self) -> None:
        self.store.watches = [lease("o/r#1", 120), lease("o/r#2", 300)]
        svc = self.make()
        svc.run_once(T0)
        svc.run_once(T0 + 120)
        svc.run_once(T0 + 300)
        self.assertEqual(
            self.signals.check_calls,
            [["o/r#1", "o/r#2"], ["o/r#1"], ["o/r#2", "o/r#1"]],
        )

    def test_failed_check_poll_does_not_mark_leases_polled(self) -> None:
        # L6
        self.store.watches = [lease("o/r#1", 120)]
        self.signals.fail_checks = True
        svc = self.make()
        svc.run_once(T0)
        self.signals.fail_checks = False
        svc.run_once(T0 + 5)
        self.assertEqual(self.signals.check_calls, [["o/r#1"], ["o/r#1"]])

    def test_sweep_uses_stored_live_fetched_at(self) -> None:
        # M9: PRs fresh before a restart (or refreshed by the viewer) aren't swept again.
        self.store.watches = [lease("o/r#1", 120), lease("o/r#2", 120)]
        self.store.fetched_at = {"o/r#1": T0 - 30}
        report = self.make().run_once(T0)
        self.assertEqual(report.swept, ["o/r#2"])

    def test_check_polls_are_capped_per_tick_most_overdue_first(self) -> None:
        self.store.watches = [lease(f"o/r#{n}", 120) for n in range(1, 26)]
        svc = self.make()
        self.assertEqual(svc.run_once(T0).checks_polled, MAX_CHECK_LEASES_PER_TICK)
        second = svc.run_once(T0 + 5)
        self.assertEqual(second.checks_polled, 25 - MAX_CHECK_LEASES_PER_TICK)
        self.assertTrue(
            set(self.signals.check_calls[1]).isdisjoint(self.signals.check_calls[0])
        )

    def test_refresh_request_queued_during_check_polls_is_served_same_tick(
        self,
    ) -> None:
        self.store.watches = [lease("o/r#1", 120)]
        original = self.signals.poll_check_runs

        def poll_and_enqueue(leases: Any, now: float) -> Any:
            self.store.requests.append("o/r#9")
            return original(leases, now)

        self.signals.poll_check_runs = poll_and_enqueue  # type: ignore[method-assign]
        report = self.make().run_once(T0)
        self.assertEqual(report.requested, ["o/r#9"])
        self.assertIn(["o/r#9"], self.hot.calls)


class TestBudgetAndLocks(ServiceHarness):
    def test_deferred_budget_skips_low_priorities_and_logs_once(self) -> None:
        self.budget.allowed = {Priority.HOT}
        self.store.requests = ["o/r#1"]
        self.signals.events.append(Dirty(mirror_repos={7}))
        svc = self.make(child_argv=["backfill"])
        report = svc.run_once(T0)
        self.assertEqual(self.hot.calls, [["o/r#1"]])
        self.assertEqual(
            (self.viewer.calls, self.mirror.calls, self.children), (0, [], [])
        )
        self.assertEqual(sorted(report.deferred), ["backfill", "mirror", "viewer"])
        svc.run_once(T0 + 5)
        self.assertEqual(sum("viewer deferred" in line for line in self.logs), 1)

        self.budget.allowed = set(Priority)
        report = svc.run_once(T0 + 10)
        self.assertTrue(
            report.viewer_ran and report.mirror_ran and report.child_spawned
        )
        self.assertIn("viewer resumed", self.logs)

    def test_worker_budget_deferral_is_caught_and_logged_per_state_change(self) -> None:
        deferred = BudgetDeferred("graphql", Priority.HOT, "rate limited")
        self.signals.notifications.extend([deferred, deferred, Dirty()])
        svc = self.make()
        self.assertIn("signals", svc.run_once(T0).deferred)
        self.assertIn("signals", svc.run_once(T0 + 60).deferred)
        self.assertNotIn("signals", svc.run_once(T0 + 120).deferred)
        self.assertEqual(
            [line for line in self.logs if line.startswith("signals")],
            ["signals deferred: rate limited", "signals resumed"],
        )

    def test_api_error_in_one_worker_does_not_stop_others(self) -> None:
        self.viewer.raise_next = GhApiError(502, "bad gateway")
        self.signals.events.append(Dirty(mirror_repos={3}))
        report = self.make().run_once(T0)
        self.assertFalse(report.viewer_ran)
        self.assertEqual(report.errors, ["viewer: HTTP 502: bad gateway"])
        self.assertTrue(report.mirror_ran)

    def test_mirror_skipped_while_sync_lock_held_then_rate_limited(self) -> None:
        self.signals.events.append(Dirty(mirror_repos={7}))
        self.lock_free = False
        svc = self.make()
        report = svc.run_once(T0)
        self.assertTrue(report.mirror_skipped_locked)
        self.assertEqual(self.mirror.calls, [])

        self.lock_free = True
        self.assertTrue(svc.run_once(T0 + 5).mirror_ran)
        self.signals.events.append(Dirty(mirror_repos={8}))
        self.assertFalse(svc.run_once(T0 + 60).mirror_ran)  # within 2 min of last run
        self.assertTrue(svc.run_once(T0 + 125).mirror_ran)
        self.assertEqual(self.mirror.calls, [[7], [8]])

    def test_mirror_waits_for_an_events_change(self) -> None:
        svc = self.make()
        self.assertFalse(svc.run_once(T0).mirror_ran)
        self.assertFalse(svc.run_once(T0 + 600).mirror_ran)
        self.assertEqual(self.mirror.calls, [])

    def test_default_sync_lock_uses_store_db_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.store.db_path = Path(tmp) / "repos.sqlite"  # type: ignore[attr-defined]
            self.signals.events.append(Dirty(mirror_repos={1}))
            svc = PackratService(
                self.store,
                self.signals,
                self.hot,
                self.viewer,
                self.mirror,
                None,
                log=self.logs.append,
            )
            with acquire_sync_lock(self.store.db_path, mode="fail") as held:  # type: ignore[attr-defined]
                self.assertTrue(held.acquired)
                self.assertTrue(svc.run_once(T0).mirror_skipped_locked)
            self.assertTrue(svc.run_once(T0 + 5).mirror_ran)


class TestHousekeepingAndChild(ServiceHarness):
    def test_heartbeat_and_expired_leases_every_tick(self) -> None:
        expected_ver = format_service_version()
        self.assertTrue(expected_ver.startswith(SERVICE_VERSION + "+"))
        svc = self.make()
        svc.run_once(T0)
        svc.run_once(T0 + 5)
        self.assertEqual(
            [hb[1:] for hb in self.store.heartbeats],
            [
                (expected_ver, T0),
                (expected_ver, T0 + 5),
            ],
        )
        # First tick (hourly housekeeping) prunes expired leases; idle 5s tick skips it
        # when no watched leases are approaching expiry.
        self.assertEqual(self.store.expired_prunes, 1)

    def test_expired_watch_is_pruned_when_earliest_lease_expires(self) -> None:
        self.store.watches = [
            WatchLease(
                consumer="monitor",
                consumer_id="c1",
                item_key="o/r#1",
                freshness_s=120,
                lease_expires_at=iso_at(T0 + 10),
            )
        ]
        svc = self.make()
        svc.run_once(T0)
        self.assertEqual(self.store.expired_prunes, 1)
        svc.run_once(T0 + 5)
        self.assertEqual(self.store.expired_prunes, 1)
        svc.run_once(T0 + 10)
        self.assertEqual(self.store.expired_prunes, 2)

    def test_end_of_tick_heartbeat_uses_the_clock_after_a_long_tick(self) -> None:
        svc = self.make()
        svc.clock = lambda: T0 + 70.0
        svc.run_once(T0)
        self.assertEqual([hb[2] for hb in self.store.heartbeats], [T0 + 70.0])

    def test_prune_and_wal_checkpoint_run_hourly(self) -> None:
        svc = self.make()
        reports = [svc.run_once(T0 + dt) for dt in (0, 5, 3599, 3600, 3605)]
        self.assertEqual([r.pruned for r in reports], [True, False, False, True, False])
        self.assertEqual(self.store.prunes, [T0, T0 + 3600])
        self.assertEqual(len(self.store.wal_checkpoints), 2)
        self.assertEqual(reports[0].wal_checkpoint, (0, 4, 4))
        self.assertIsNone(reports[1].wal_checkpoint)
        self.assertEqual(reports[3].wal_checkpoint, (0, 4, 4))

    def test_child_not_spawned_twice_and_reaped(self) -> None:
        svc = self.make(child_argv=["packrat.py", "sync", "--backfill-only"])
        self.assertTrue(svc.run_once(T0).child_spawned)
        self.assertFalse(svc.run_once(T0 + 5).child_spawned)  # still running
        self.children[0].code = 0
        report = svc.run_once(T0 + 10)
        self.assertEqual((report.child_exit, report.child_spawned), (0, False))
        self.assertTrue(svc.run_once(T0 + BACKFILL_INTERVAL_S).child_spawned)
        self.assertEqual(len(self.children), 2)

    def test_zero_mirror_repos_skips_backfill_child_spawn(self) -> None:
        self.store.mirror_repos = []
        svc = self.make(child_argv=["packrat.py", "sync", "--backfill-only"])
        report = svc.run_once(T0)
        self.assertFalse(report.child_spawned)
        self.assertEqual(self.children, [])

    def test_no_child_argv_never_spawns(self) -> None:
        svc = self.make(child_argv=None)
        self.assertFalse(svc.run_once(T0).child_spawned)
        self.assertEqual(self.children, [])

    def test_run_finishes_tick_stops_child_and_returns_zero(self) -> None:
        class OneTickEvent(threading.Event):
            def wait(self, timeout: float | None = None) -> bool:
                self.set()
                return True

        svc = self.make(child_argv=["backfill"])
        stop = OneTickEvent()
        self.assertEqual(svc.run(stop), 0)
        # Initial startup heartbeat before loop + 1 end-of-tick heartbeat
        self.assertEqual(len(self.store.heartbeats), 2)
        self.assertTrue(self.children[0].terminated)
        self.assertIsNone(svc.child)

    def test_consecutive_failed_ticks_and_last_ok_at_tracking(self) -> None:
        import os

        self.store.initial_heartbeat = ServiceHeartbeat(
            pid=os.getpid(),
            started_at=iso_at(T0 - 10),
            beat_at=iso_at(T0 - 10),
            version=format_service_version(),
            last_ok_at=iso_at(T0 - 10),
            consecutive_failed_ticks=2,
        )
        svc = self.make()
        self.viewer.raise_next = GhApiError(502, "bad gateway")
        r1 = svc.run_once(T0)
        self.assertTrue(r1.errors)
        hb1 = self.store.heartbeat_records[-1]
        self.assertEqual(hb1.consecutive_failed_ticks, 3)
        self.assertEqual(hb1.last_ok_at, iso_at(T0 - 10))

        r2 = svc.run_once(T0 + 5)
        self.assertEqual(r2.errors, [])
        hb2 = self.store.heartbeat_records[-1]
        self.assertEqual(hb2.consecutive_failed_ticks, 0)
        self.assertEqual(hb2.last_ok_at, iso_at(T0 + 5))

    def test_upgrade_detected_on_schema_views_and_scripts_hash_drift(self) -> None:
        from unittest import mock

        for kind in ("schema", "views", "scripts"):
            with self.subTest(kind=kind):
                self.setUp()
                svc = self.make(child_argv=["backfill"])
                svc.run_once(T0)
                beats_before = len(self.store.heartbeats)
                if kind == "schema":
                    self.store.schema_version += 1
                    report = svc.run_once(T0 + 3600)
                elif kind == "views":
                    self.store.views_version += 1
                    report = svc.run_once(T0 + 3600)
                else:
                    with mock.patch(
                        "service.compute_scripts_sha256",
                        return_value="f" * 64,
                    ):
                        report = svc.run_once(T0 + 3600)
                self.assertTrue(report.upgrade_detected)
                # Does not overwrite heartbeat over newer schema/code
                self.assertEqual(len(self.store.heartbeats), beats_before)
                self.assertTrue(
                    any("upgrade detected" in line for line in self.logs),
                    self.logs,
                )

    def test_run_exits_cleanly_when_upgrade_detected(self) -> None:
        svc = self.make(child_argv=["backfill"])
        self.store.schema_version += 1
        stop = threading.Event()
        self.assertEqual(svc.run(stop), 0)
        self.assertEqual(self.store.heartbeats, [])
        self.assertIsNone(svc.child)
        self.assertTrue(any("upgrade detected" in line for line in self.logs))

    def test_operational_error_with_schema_drift_triggers_upgrade_detected(
        self,
    ) -> None:
        import sqlite3

        svc = self.make()
        svc.run_once(T0)

        def fail_with_upgrade(lookback_days: int = 7) -> list[str]:
            self.store.schema_version += 1
            raise sqlite3.OperationalError("no such column: new_col")

        self.viewer.refresh_viewer = fail_with_upgrade  # type: ignore[method-assign]
        self.signals.notifications.append(Dirty(viewer=True))
        beats_before = len(self.store.heartbeats)
        report = svc.run_once(T0 + 60)
        self.assertTrue(report.upgrade_detected)
        self.assertEqual(report.errors, [])
        self.assertEqual(len(self.store.heartbeats), beats_before)

    def test_steady_state_idle_tick_executes_at_most_five_sql_statements(self) -> None:
        from db_store import PackratStore

        with tempfile.TemporaryDirectory() as tmp:
            real_store = PackratStore(Path(tmp) / "repos.sqlite")
            try:
                real_store.add_watch(
                    "monitor",
                    "c1",
                    ["o/r#1", "o/r#2"],
                    freshness_s=120,
                    ttl_s=3600,
                    now=T0,
                )
                svc = PackratService(
                    real_store,
                    self.signals,
                    self.hot,
                    self.viewer,
                    self.mirror,
                    None,
                    clock=lambda: T0,
                    budget=self.budget,
                    sync_lock=self._lock,
                    spawn=self._spawn,
                    log=self.logs.append,
                )
                # Initial tick at T0 warms _last_hot, _last_checks, and housekeeping
                first = svc.run_once(T0)
                self.assertFalse(first.upgrade_detected)

                sql_statements: list[str] = []
                real_store.conn.set_trace_callback(sql_statements.append)
                try:
                    svc.clock = lambda: T0 + 5
                    idle_report = svc.run_once(T0 + 5)
                finally:
                    real_store.conn.set_trace_callback(None)

                self.assertFalse(idle_report.signals_polled)
                self.assertFalse(idle_report.viewer_ran)
                self.assertEqual(idle_report.swept, [])
                self.assertEqual(idle_report.checks_polled, 0)
                self.assertLessEqual(
                    len(sql_statements),
                    5,
                    f"Expected <= 5 SQL statements on idle tick, got {len(sql_statements)}: {sql_statements}",
                )
                write_txs = [
                    s
                    for s in sql_statements
                    if s.strip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
                ]
                self.assertEqual(len(write_txs), 1, sql_statements)
            finally:
                real_store.close()

    def test_spawn_niced_child_runs_in_own_group_and_terminates(self) -> None:
        script = "import os, time; os.nice(0); time.sleep(30)"
        child = spawn_niced_child([sys.executable, "-c", script])
        try:
            self.assertIsNone(child.poll())
            start = time.monotonic()
            child.terminate_group()
            self.assertIsNotNone(child.poll())
            self.assertLess(time.monotonic() - start, 5)
        finally:
            if child.poll() is None:
                child.terminate_group()


class TestExternalSyncLockYield(ServiceHarness):
    """Locked SQLite errors yield only while another process holds `sync.lock`."""

    CHILD_PID = 4321
    EXTERNAL_PID = 9876

    def _locked(self) -> sqlite3.OperationalError:
        return sqlite3.OperationalError("database is locked")

    def _fail_prune(self, *, drift: bool = False) -> None:
        def prune(now: float | None = None) -> dict[str, int]:
            if drift:
                self.store.schema_version += 1
            raise self._locked()

        self.store.prune = prune  # type: ignore[method-assign]

    def _fail_hot_request(self) -> None:
        self.store.requests = ["o/r#1"]
        self.hot.raise_next = self._locked()

    def _fail_active_watches(self) -> None:
        def active_watches(now: float | None = None) -> list[WatchLease]:
            raise self._locked()

        self.store.active_watches = active_watches  # type: ignore[method-assign]

    def _hold_externally(self) -> None:
        self.lock_free = False
        self.lock_holder_pid = self.EXTERNAL_PID

    def _assert_yielded(self, svc: PackratService, worker: str) -> None:
        report = svc.run_once(T0)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.yielded, [worker])
        self.assertEqual(svc._consecutive_failed_ticks, 0)
        self.assertEqual(self.store.heartbeat_records[-1].consecutive_failed_ticks, 0)
        self.assertTrue(
            any(line.startswith(f"{worker} yielded") for line in self.logs), self.logs
        )

    def _assert_counted(self, svc: PackratService, worker: str) -> None:
        report = svc.run_once(T0)
        self.assertEqual(report.errors, [f"{worker}: database is locked"])
        self.assertEqual(report.yielded, [])
        self.assertEqual(svc._consecutive_failed_ticks, 1)
        self.assertEqual(self.store.heartbeat_records[-1].consecutive_failed_ticks, 1)
        self.assertFalse(any("yielded" in line for line in self.logs), self.logs)

    def test_external_holder_yields_housekeeping_error(self) -> None:
        self._hold_externally()
        self._fail_prune()
        self._assert_yielded(self.make(), "housekeeping")

    def test_external_holder_yields_guarded_worker_error(self) -> None:
        self._hold_externally()
        self._fail_hot_request()
        self._assert_yielded(self.make(), "hot")

    def test_external_holder_yields_run_once_sqlite_error(self) -> None:
        self._hold_externally()
        self._fail_active_watches()
        self._assert_yielded(self.make(), "sqlite")

    def test_external_holder_yields_while_own_child_runs(self) -> None:
        self._hold_externally()
        self._fail_prune()
        svc = self.make()
        svc.child = FakeChild(pid=self.CHILD_PID)
        self._assert_yielded(svc, "housekeeping")

    def test_own_child_holding_lock_still_counts_failure(self) -> None:
        for holder in (self.CHILD_PID, None):
            with self.subTest(holder=holder):
                self.setUp()
                self.lock_free = False
                self.lock_holder_pid = holder
                self._fail_prune()
                svc = self.make()
                svc.child = FakeChild(pid=self.CHILD_PID)
                self._assert_counted(svc, "housekeeping")

    def test_own_child_holding_lock_counts_guarded_worker_failure(self) -> None:
        self.lock_free = False
        self.lock_holder_pid = self.CHILD_PID
        self._fail_hot_request()
        svc = self.make()
        svc.child = FakeChild(pid=self.CHILD_PID)
        self._assert_counted(svc, "hot")

    def test_yielded_tick_keeps_prior_failure_count_and_last_ok_at(self) -> None:
        import os

        self.store.initial_heartbeat = ServiceHeartbeat(
            pid=os.getpid(),
            started_at=iso_at(T0 - 10),
            beat_at=iso_at(T0 - 10),
            version=format_service_version(),
            last_ok_at=iso_at(T0 - 10),
            consecutive_failed_ticks=2,
        )
        self._hold_externally()
        self._fail_prune()
        svc = self.make()
        report = svc.run_once(T0)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.yielded, ["housekeeping"])
        self.assertEqual(svc._consecutive_failed_ticks, 2)
        self.assertEqual(svc._last_ok_at, iso_at(T0 - 10))
        beat = self.store.heartbeat_records[-1]
        self.assertEqual(beat.consecutive_failed_ticks, 2)
        self.assertEqual(beat.last_ok_at, iso_at(T0 - 10))

    def test_lock_held_by_this_process_counts_failure(self) -> None:
        import os

        self.lock_free = False
        self.lock_holder_pid = os.getpid()
        self._fail_prune()
        self._assert_counted(self.make(), "housekeeping")

    def test_free_lock_counts_failure(self) -> None:
        for worker, arrange in (
            ("housekeeping", self._fail_prune),
            ("hot", self._fail_hot_request),
            ("sqlite", self._fail_active_watches),
        ):
            with self.subTest(worker=worker):
                self.setUp()
                arrange()
                self._assert_counted(self.make(), worker)

    def test_non_lock_error_counts_without_probing_sync_lock(self) -> None:
        self._hold_externally()

        def prune(now: float | None = None) -> dict[str, int]:
            raise sqlite3.OperationalError("disk I/O error")

        self.store.prune = prune  # type: ignore[method-assign]
        svc = self.make()
        report = svc.run_once(T0)
        self.assertEqual(report.errors, ["housekeeping: disk I/O error"])
        self.assertEqual(svc._consecutive_failed_ticks, 1)
        self.assertEqual(self.lock_calls, 0)

    def test_upgrade_check_runs_before_lock_probe_in_housekeeping(self) -> None:
        self._hold_externally()
        self._fail_prune(drift=True)
        svc = self.make()
        report = svc.run_once(T0)
        self.assertTrue(report.upgrade_detected)
        self.assertEqual(report.errors, [])
        self.assertEqual(self.lock_calls, 0)
        self.assertEqual(svc._consecutive_failed_ticks, 0)
        self.assertEqual(self.store.heartbeats, [])
        self.assertFalse(any("yielded" in line for line in self.logs), self.logs)

    def test_upgrade_check_runs_before_lock_probe_in_guarded_worker(self) -> None:
        self._hold_externally()
        self.store.requests = ["o/r#1"]

        def refresh_hot(keys: Sequence[str]) -> list[str]:
            self.store.schema_version += 1
            raise self._locked()

        self.hot.refresh_hot = refresh_hot  # type: ignore[method-assign]
        svc = self.make()
        report = svc.run_once(T0)
        self.assertTrue(report.upgrade_detected)
        self.assertEqual(report.errors, [])
        self.assertEqual(self.lock_calls, 0)
        self.assertEqual(svc._consecutive_failed_ticks, 0)
        self.assertEqual(self.store.heartbeats, [])


class FakeTimeline:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.calls = 0
        self.raise_next: Exception | None = None

    def sync_items_timelines(self) -> list[Any]:
        self.log.append("timeline")
        self.calls += 1
        if self.raise_next is not None:
            exc, self.raise_next = self.raise_next, None
            raise exc
        return []


class TestTimelineWorker(ServiceHarness):
    def make_with_timeline(self) -> tuple[PackratService, FakeTimeline]:
        timeline = FakeTimeline(self.events)
        svc = PackratService(
            self.store,
            self.signals,
            self.hot,
            self.viewer,
            self.mirror,
            None,
            clock=lambda: T0,
            budget=self.budget,
            sync_lock=self._lock,
            spawn=self._spawn,
            log=self.logs.append,
            timeline=timeline,
        )
        return svc, timeline

    def test_timeline_runs_once_per_viewer_interval(self) -> None:
        svc, timeline = self.make_with_timeline()
        self.assertTrue(svc.run_once(T0).timeline_ran)
        self.assertFalse(svc.run_once(T0 + VIEWER_INTERVAL_S - 1).timeline_ran)
        self.assertTrue(svc.run_once(T0 + VIEWER_INTERVAL_S).timeline_ran)
        self.assertEqual(timeline.calls, 2)

    def test_timeline_waits_for_viewer_budget_then_resumes(self) -> None:
        self.budget.allowed = {Priority.HOT, Priority.MIRROR}
        svc, timeline = self.make_with_timeline()
        report = svc.run_once(T0)
        self.assertFalse(report.timeline_ran)
        self.assertIn("timeline", report.deferred)
        self.assertEqual(timeline.calls, 0)
        self.budget.allowed = set(Priority)
        self.assertTrue(svc.run_once(T0 + 5).timeline_ran)
        self.assertIn("timeline resumed", self.logs)

    def test_timeline_failures_are_reported_not_raised(self) -> None:
        svc, timeline = self.make_with_timeline()
        timeline.raise_next = BudgetDeferred("graphql", Priority.VIEWER, "rate limited")
        report = svc.run_once(T0)
        self.assertFalse(report.timeline_ran)
        self.assertIn("timeline", report.deferred)
        timeline.raise_next = GhApiError(502, "bad gateway")
        report = svc.run_once(T0 + VIEWER_INTERVAL_S)
        self.assertEqual(report.errors, ["timeline: HTTP 502: bad gateway"])

    def test_no_timeline_worker_is_skipped(self) -> None:
        report = self.make().run_once(T0)
        self.assertFalse(report.timeline_ran)
        self.assertNotIn("timeline", report.deferred)

    def test_sqlite_operational_error_on_heartbeat_is_logged_not_raised(self) -> None:
        import sqlite3
        from unittest import mock

        svc = self.make()
        with mock.patch.object(
            self.store,
            "write_heartbeat",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            report = svc.run_once(T0)
        self.assertTrue(
            any(
                err.startswith("heartbeat: database is locked") for err in report.errors
            )
        )


if __name__ == "__main__":
    unittest.main()
