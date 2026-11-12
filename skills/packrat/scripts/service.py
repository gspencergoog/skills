"""Single-threaded scheduler for `packrat serve`.

`PackratService` owns every GitHub read the long-running service makes. Each
tick it drains queued refresh requests, polls cheap change signals, refreshes
dirty and leased pull requests, and runs the viewer, timeline, and mirror
workers on their own cadences. Backfill and indexing run in a niced child process so that slow
git and embedding work never delays the hot path.

The workers (`signals`, `hot`, `viewer`, `timeline`, `mirror`) are injected and used only
through the `Protocol`s below, so this module does not import the worker
modules. `packrat.py serve` wires the real implementations together.

Locks: the caller holds `serve.lock` (`sync_runtime.acquire_serve_lock`) for the
life of the service. The service takes `sync.lock` without blocking only around
each mirror run and skips the run while another writer (the backfill child or a
manual `packrat sync`) holds it.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Collection, Iterable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from gh_transport import BudgetDeferred, GhApiError
from models import WatchLease, compute_scripts_sha256, iso_at
from rate_budget import Priority
from sync_runtime import SyncLockInfo, acquire_sync_lock

SERVICE_VERSION = "packrat-serve/2"
"""Version prefix written to `service_heartbeat.version`."""


def format_service_version(scripts_sha: str | None = None) -> str:
    """Formats the `service_heartbeat.version` string with a 12-char scripts SHA-256 suffix."""
    sha = scripts_sha if scripts_sha is not None else compute_scripts_sha256()
    return f"{SERVICE_VERSION}+{sha[:12]}"


TICK_S = 5.0
"""Seconds between ticks; bounds how long a queued refresh request waits."""
SIGNALS_INTERVAL_S = 60
"""Minimum interval between signal polls; GitHub's `X-Poll-Interval` can raise it."""
DEFAULT_FRESHNESS_S = 120
"""Check-run poll interval for a lease that does not set `freshness_s`."""
LEASE_SWEEP_FLOOR_S = 600
"""Every leased item gets a hot refresh at least every `max(freshness_s, this)` seconds."""
VIEWER_INTERVAL_S = 15 * 60
"""Interval of the viewer refresh when no signal asks for one sooner."""
MIRROR_MIN_INTERVAL_S = 120
"""Minimum spacing between mirror runs."""
BACKFILL_INTERVAL_S = 600
"""Minimum spacing between backfill child spawns."""
PRUNE_INTERVAL_S = 3600
"""Interval between `store.prune()` calls."""
MAX_CHECK_LEASES_PER_TICK = 20
"""Most leases whose check runs are polled in one tick (2 REST calls each).

Polling every due lease at once took minutes with ~90 leases after a restart,
and refresh requests waited behind it; the most overdue leases go first.
"""

MAX_REFRESH_ATTEMPTS = 5
"""Ticks a failing refresh request is retried before it is dropped (M5)."""

CHILD_NICE = 10
"""`os.nice()` increment applied to the backfill child."""
CHILD_TERM_GRACE_S = 10.0
"""Seconds to wait after SIGTERM before SIGKILL when stopping the child."""
_BUDGET_RESOURCE = "graphql"


class DirtySignals(Protocol):
    """What a signal poll found: items to refresh hot, viewer sets, and mirror repos."""

    hot: set[str]
    viewer: bool
    mirror_repos: set[int]


class SignalSource(Protocol):
    """Cheap change detection (Track 2A `SignalPoller`)."""

    def poll_notifications(self, now: float) -> DirtySignals: ...

    def poll_repo_events(self, now: float) -> DirtySignals: ...

    def poll_check_runs(
        self, leases: Sequence[WatchLease], now: float
    ) -> DirtySignals: ...


class HotRefresh(Protocol):
    """Full refresh of specific items (Track 2B `HotRefresher`)."""

    def refresh_hot(self, keys: Sequence[str]) -> list[str]: ...


class ViewerRefresh(Protocol):
    """Viewer search sets (Track 2C `ViewerSync`)."""

    def refresh_viewer(self, lookback_days: int = 7) -> list[str]: ...


class TimelineRefresh(Protocol):
    """Timeline events for stored `items`-mode PRs and issues (`serve_cli.TimelineAdapter`)."""

    def sync_items_timelines(self) -> Any: ...


class MirrorSync(Protocol):
    """Incremental repository delta (Wave 3 adapter over `sync_github_metadata()`)."""

    def sync_mirror(self, repo_ids: Collection[int]) -> Any: ...


class BudgetGate(Protocol):
    """Pre-call budget check, implemented by `RateBudget`."""

    def allows(
        self, resource: str, priority: Priority, now: float | None = None
    ) -> bool: ...


class ServiceStore(Protocol):
    """The `PackratStore` methods the scheduler uses."""

    def take_refresh_requests(self) -> list[str]: ...

    def request_refresh(
        self, item_keys: Iterable[str], now: float | None = None
    ) -> list[str]: ...

    def active_watches(self, now: float | None = None) -> list[WatchLease]: ...

    def prune_expired_watches(self, now: float | None = None) -> int: ...

    def write_heartbeat(
        self,
        pid: int,
        version: str,
        now: float | None = None,
        *,
        last_ok_at: str | None = None,
        consecutive_failed_ticks: int | None = None,
    ) -> None: ...

    def get_live_fetched_at(self, item_keys: Iterable[str]) -> dict[str, float]: ...

    def prune(self, now: float | None = None) -> dict[str, int]: ...


class ChildProcess(Protocol):
    """Handle on the backfill child process."""

    pid: int

    def poll(self) -> int | None: ...

    def terminate_group(self) -> None: ...


SyncLockFactory = Callable[[], AbstractContextManager[SyncLockInfo]]
ChildSpawner = Callable[[Sequence[str]], ChildProcess]


@dataclass
class TickReport:
    """What one `run_once()` tick did.

    Attributes:
        at: Epoch seconds of the tick.
        requested: Keys taken from `refresh_requests`.
        hot_keys: Keys passed to `refresh_hot()` after signals and the lease sweep.
        swept: Subset of `hot_keys` refreshed only because their sweep was due.
        changed: Keys `refresh_hot()` reported as changed, across both hot calls.
        signals_polled: Whether notifications and repo events were polled.
        checks_polled: Number of leases whose check runs were polled.
        viewer_ran: Whether the viewer refresh ran.
        timeline_ran: Whether the `items`-repo timeline refresh ran.
        mirror_ran: Whether a mirror run happened.
        mirror_skipped_locked: Whether a due mirror run was skipped for `sync.lock`.
        child_spawned: Whether a backfill child was started.
        child_exit: Exit code of a backfill child reaped this tick.
        pruned: Whether `store.prune()` ran.
        deferred: Workers skipped or stopped for lack of budget.
        errors: Worker failures other than budget deferrals, as `worker: message`.
        upgrade_detected: Whether a schema, views, or scripts hash upgrade was detected.
        wal_checkpoint: `(busy, log, checkpointed)` from hourly `wal_checkpoint(TRUNCATE)`.
    """

    at: float
    requested: list[str] = field(default_factory=list)
    hot_keys: list[str] = field(default_factory=list)
    swept: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    signals_polled: bool = False
    checks_polled: int = 0
    viewer_ran: bool = False
    timeline_ran: bool = False
    mirror_ran: bool = False
    mirror_skipped_locked: bool = False
    child_spawned: bool = False
    child_exit: int | None = None
    pruned: bool = False
    deferred: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    upgrade_detected: bool = False
    wal_checkpoint: tuple[int, int, int] | None = None


class _PopenChild:
    """`ChildProcess` over `subprocess.Popen` started in its own session."""

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc
        self.pid = proc.pid

    def poll(self) -> int | None:
        return self._proc.poll()

    def terminate_group(self) -> None:
        """Sends SIGTERM to the child's process group, then SIGKILL after a grace period."""
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.pid, signal.SIGTERM)
        try:
            self._proc.wait(timeout=CHILD_TERM_GRACE_S)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.pid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._proc.wait(timeout=2)


def _nice_child() -> None:
    os.nice(CHILD_NICE)


def spawn_niced_child(argv: Sequence[str]) -> ChildProcess:
    """Starts `argv` in a new session with `os.nice(10)`, detached from stdio."""
    proc = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        preexec_fn=_nice_child,  # noqa: PLW1509 - the service is single-threaded
    )
    return _PopenChild(proc)


def _stderr_log(message: str) -> None:
    sys.stderr.write(f"packrat serve: {message}\n")


def _leases_by_key(leases: Iterable[WatchLease]) -> dict[str, WatchLease]:
    """Keeps one lease per item: the one with the smallest `freshness_s`."""
    best: dict[str, WatchLease] = {}
    for lease in leases:
        cur = best.get(lease.item_key)
        if cur is None or _freshness(lease) < _freshness(cur):
            best[lease.item_key] = lease
    return best


def _freshness(lease: WatchLease) -> int:
    return lease.freshness_s if lease.freshness_s > 0 else DEFAULT_FRESHNESS_S


def _read_store_versions(store: Any) -> tuple[int, int] | None:
    """Returns `(schema_version, views_version)` from `store` when supported."""
    reader = getattr(store, "read_schema_and_views_version", None)
    if reader is None:
        return None
    try:
        pair = reader()
    except sqlite3.OperationalError:
        return None
    return int(pair[0]), int(pair[1])


def _init_heartbeat_health(store: Any) -> tuple[str | None, int]:
    """Loads `(last_ok_at, consecutive_failed_ticks)` when the existing heartbeat PID matches."""
    reader = getattr(store, "read_heartbeat", None)
    if reader is None:
        return None, 0
    try:
        beat = reader()
    except sqlite3.OperationalError:
        return None, 0
    if beat is not None and beat.pid == os.getpid():
        return beat.last_ok_at, int(beat.consecutive_failed_ticks or 0)
    return None, 0


def _file_header_token(path: Path) -> tuple[int, int, bytes]:
    """Reads `(mtime_ns, size, header_136b)` for change detection without SQL queries."""
    try:
        with path.open("rb") as handle:
            st = os.fstat(handle.fileno())
            return st.st_mtime_ns, st.st_size, handle.read(136)
    except OSError:
        return 0, 0, b""


def _store_db_token(store: Any) -> Any:
    """Returns a zero-SQL mutation token for a SQLite store, or None for test doubles."""
    conn = getattr(store, "conn", None)
    if conn is None or "read_schema_and_views_version" in getattr(
        store, "__dict__", {}
    ):
        return None
    db_path = getattr(store, "db_path", None)
    if db_path is None:
        return conn.total_changes
    path = Path(db_path)
    return (
        conn.total_changes,
        _file_header_token(path),
        _file_header_token(Path(f"{path}-wal")),
    )


def _earliest_lease_expiry(leases: Iterable[WatchLease]) -> str | None:
    """Returns the earliest non-empty `lease_expires_at` timestamp among `leases`."""
    expiries = [w.lease_expires_at for w in leases if w.lease_expires_at]
    return min(expiries) if expiries else None


@dataclass
class _Dirty:
    """Union of `DirtySignals` gathered during one tick."""

    hot: set[str] = field(default_factory=set)
    viewer: bool = False
    mirror_repos: set[int] = field(default_factory=set)

    def add(self, found: DirtySignals | None) -> None:
        if found is None:
            return
        self.hot |= set(found.hot)
        self.viewer = self.viewer or bool(found.viewer)
        self.mirror_repos |= set(found.mirror_repos)


class PackratService:
    """Schedules the signals, hot, viewer, mirror, and backfill work of `packrat serve`."""

    def __init__(
        self,
        store: ServiceStore,
        signals: SignalSource,
        hot: HotRefresh,
        viewer: ViewerRefresh,
        mirror: MirrorSync,
        child_argv: Sequence[str] | None,
        clock: Callable[[], float] = time.time,
        *,
        budget: BudgetGate | None = None,
        sync_lock: SyncLockFactory | None = None,
        spawn: ChildSpawner = spawn_niced_child,
        log: Callable[[str], None] = _stderr_log,
        tick_s: float = TICK_S,
        timeline: TimelineRefresh | None = None,
    ) -> None:
        """Initializes the scheduler; nothing runs until `run()` or `run_once()`.

        Args:
            store: The packrat store (`PackratStore`).
            signals: Signal poller (Track 2A).
            hot: Hot refresher (Track 2B).
            viewer: Viewer sync (Track 2C).
            mirror: Mirror adapter run for repos with event changes.
            child_argv: Backfill command, or None to never spawn a child.
            clock: Epoch-seconds clock.
            budget: Pre-call budget gate; None allows everything.
            sync_lock: Factory for a non-blocking `sync.lock` context. Defaults to
                `acquire_sync_lock(store.db_path, mode="skip")`.
            spawn: Starts the backfill child.
            log: Receives one-line status messages.
            tick_s: Seconds between ticks in `run()`.
            timeline: Timeline refresh for `items` repositories, run at
                `VIEWER` priority every `VIEWER_INTERVAL_S`; None skips it.
        """
        self.store = store
        self.signals = signals
        self.hot = hot
        self.viewer = viewer
        self.mirror = mirror
        self.child_argv = list(child_argv) if child_argv else None
        self.clock = clock
        self.budget = budget
        self.sync_lock = sync_lock or self._default_sync_lock
        self.spawn = spawn
        self.log = log
        self.tick_s = tick_s
        self.timeline = timeline
        self.child: ChildProcess | None = None
        self._next_signals = 0.0
        self._next_viewer = 0.0
        self._next_timeline = 0.0
        self._next_prune = 0.0
        self._next_child = 0.0
        self._last_mirror: float | None = None
        self._pending_mirror: set[int] = set()
        self._last_hot: dict[str, float] = {}
        self._last_checks: dict[str, float] = {}
        self._deferred: set[str] = set()
        self._requeue: list[str] = []
        self._request_attempts: dict[str, int] = {}
        self._next_watch_expiry: str | None = None
        versions = _read_store_versions(store)
        self._has_version_reader = versions is not None
        self._startup_schema_version = versions[0] if versions is not None else 0
        self._startup_views_version = versions[1] if versions is not None else 0
        self._startup_scripts_sha = compute_scripts_sha256()
        self._version_string = format_service_version(self._startup_scripts_sha)
        self._last_ok_at, self._consecutive_failed_ticks = _init_heartbeat_health(store)
        self._last_db_token = _store_db_token(store)

    def _default_sync_lock(self) -> AbstractContextManager[SyncLockInfo]:
        db_path = getattr(self.store, "db_path", None)
        if db_path is None:
            raise ValueError("PackratService needs sync_lock or a store with db_path")
        return acquire_sync_lock(Path(db_path), mode="skip")

    # ----- budget and error handling -------------------------------------------------

    def _allows(self, priority: Priority, now: float) -> bool:
        return self.budget is None or self.budget.allows(
            _BUDGET_RESOURCE, priority, now
        )

    def _note_deferred(self, worker: str, report: TickReport, detail: str) -> None:
        report.deferred.append(worker)
        if worker not in self._deferred:
            self._deferred.add(worker)
            self.log(f"{worker} deferred: {detail}")

    def _note_ok(self, worker: str) -> None:
        if worker in self._deferred:
            self._deferred.discard(worker)
            self.log(f"{worker} resumed")

    def _guarded(
        self, worker: str, report: TickReport, call: Callable[[], Any]
    ) -> tuple[bool, Any]:
        """Runs `call`, turning budget deferrals and API errors into report entries.

        Returns:
            `(ok, result)`, where `ok` is False when the call raised.
        """
        try:
            result = call()
        except BudgetDeferred as exc:
            self._note_deferred(worker, report, exc.detail)
            return False, None
        except GhApiError as exc:
            report.errors.append(f"{worker}: {exc}")
            self.log(f"{worker} failed: {exc}")
            return False, None
        except sqlite3.OperationalError as exc:
            if self._check_upgrade(report, force_db=True):
                return False, None
            report.errors.append(f"{worker}: {exc}")
            self.log(f"{worker} failed: {exc}")
            return False, None
        self._note_ok(worker)
        return True, result

    def _gate(
        self, worker: str, priority: Priority, now: float, report: TickReport
    ) -> bool:
        """Checks the budget before a low-priority worker and records a deferral."""
        if self._allows(priority, now):
            return True
        self._note_deferred(
            worker, report, f"budget below threshold for {priority.name}"
        )
        return False

    # ----- workers -------------------------------------------------------------------

    def _refresh_hot(self, keys: list[str], now: float, report: TickReport) -> bool:
        ok, changed = self._guarded("hot", report, lambda: self.hot.refresh_hot(keys))
        if not ok:
            return False
        for key in keys:
            self._last_hot[key] = now
        report.changed.extend(k for k in changed or [] if k not in report.changed)
        return True

    def _drain_requests(self, now: float, report: TickReport) -> None:
        """Serves queued refresh requests; called at the start and again mid-tick.

        Failed keys are held until the end of the tick (`_requeue_failed`), so
        one bad key is retried once per tick, not by every drain.
        """
        keys = list(dict.fromkeys(self.store.take_refresh_requests()))
        if not keys:
            return
        report.requested.extend(k for k in keys if k not in report.requested)
        if self._refresh_hot(keys, now, report):
            for key in keys:
                self._request_attempts.pop(key, None)
        else:
            self._requeue.extend(k for k in keys if k not in self._requeue)

    def _requeue_failed(self, now: float) -> None:
        """Re-queues this tick's failed refresh requests, dropping repeat failures."""
        keep: list[str] = []
        for key in self._requeue:
            attempts = self._request_attempts.get(key, 0) + 1
            if attempts >= MAX_REFRESH_ATTEMPTS:
                self._request_attempts.pop(key, None)
                self.log(f"refresh of {key} dropped after {attempts} failed attempts")
                continue
            self._request_attempts[key] = attempts
            keep.append(key)
        self._requeue = []
        if keep:
            self.store.request_refresh(keep, now)

    def _poll_signals(self, now: float, dirty: _Dirty, report: TickReport) -> None:
        if now < self._next_signals:
            return
        report.signals_polled = True
        ok, found = self._guarded(
            "signals", report, lambda: self.signals.poll_notifications(now)
        )
        if ok:
            dirty.add(found)
        ok, found = self._guarded(
            "events", report, lambda: self.signals.poll_repo_events(now)
        )
        if ok:
            dirty.add(found)
        interval = getattr(self.signals, "poll_interval_s", SIGNALS_INTERVAL_S)
        self._next_signals = now + max(SIGNALS_INTERVAL_S, int(interval or 0))

    def _poll_checks(
        self,
        leases: dict[str, WatchLease],
        now: float,
        dirty: _Dirty,
        report: TickReport,
    ) -> None:
        due = sorted(
            (
                lease
                for key, lease in leases.items()
                if now - self._last_checks.get(key, float("-inf")) >= _freshness(lease)
            ),
            key=lambda lease: self._last_checks.get(lease.item_key, float("-inf")),
        )[:MAX_CHECK_LEASES_PER_TICK]
        if not due:
            return
        report.checks_polled = len(due)
        ok, found = self._guarded(
            "checks", report, lambda: self.signals.poll_check_runs(due, now)
        )
        if ok:
            for lease in due:
                self._last_checks[lease.item_key] = now
            dirty.add(found)

    def _sweep_due(self, leases: dict[str, WatchLease], now: float) -> list[str]:
        """Returns leased keys whose last hot refresh is older than their sweep interval.

        Filters against in-memory `self._last_hot` first before querying stored
        `live_fetched_at`, caching any fresher database timestamps back into
        `self._last_hot` so idle ticks avoid repeated SQLite lookups (PR-H8).
        """
        if not leases:
            return []
        never = float("-inf")
        candidates = {
            key: lease
            for key, lease in leases.items()
            if now - self._last_hot.get(key, never)
            >= max(_freshness(lease), LEASE_SWEEP_FLOOR_S)
        }
        if not candidates:
            return []
        fetched = self.store.get_live_fetched_at(candidates)
        due: list[str] = []
        for key, lease in candidates.items():
            db_ts = fetched.get(key, never)
            if db_ts > self._last_hot.get(key, never):
                self._last_hot[key] = db_ts
            if now - self._last_hot.get(key, never) >= max(
                _freshness(lease), LEASE_SWEEP_FLOOR_S
            ):
                due.append(key)
        return due

    def _run_hot(
        self,
        leases: dict[str, WatchLease],
        now: float,
        dirty: _Dirty,
        report: TickReport,
    ) -> None:
        done = set(report.requested)
        signalled = sorted(dirty.hot - done)
        swept = [
            k
            for k in self._sweep_due(leases, now)
            if k not in done and k not in dirty.hot
        ]
        keys = signalled + swept
        if not keys:
            return
        report.hot_keys = keys
        report.swept = swept
        self._refresh_hot(keys, now, report)

    def _run_viewer(self, now: float, dirty: _Dirty, report: TickReport) -> None:
        if not (dirty.viewer or now >= self._next_viewer):
            return
        if not self._gate("viewer", Priority.VIEWER, now, report):
            return
        ok, _ = self._guarded("viewer", report, self.viewer.refresh_viewer)
        report.viewer_ran = ok
        self._next_viewer = now + VIEWER_INTERVAL_S

    def _run_timeline(self, now: float, report: TickReport) -> None:
        """Refreshes `items`-repo timelines every `VIEWER_INTERVAL_S` at `VIEWER` priority.

        Like the hot and viewer workers, it writes without `sync.lock`; each
        batch is a short transaction.
        """
        timeline = self.timeline
        if timeline is None or now < self._next_timeline:
            return
        if not self._gate("timeline", Priority.VIEWER, now, report):
            return
        ok, _ = self._guarded("timeline", report, timeline.sync_items_timelines)
        report.timeline_ran = ok
        self._next_timeline = now + VIEWER_INTERVAL_S

    def _mirror_due(self, now: float) -> bool:
        if not self._pending_mirror:
            return False
        return (
            self._last_mirror is None
            or now - self._last_mirror >= MIRROR_MIN_INTERVAL_S
        )

    def _run_mirror(self, now: float, dirty: _Dirty, report: TickReport) -> None:
        self._pending_mirror |= dirty.mirror_repos
        if not self._mirror_due(now) or not self._gate(
            "mirror", Priority.MIRROR, now, report
        ):
            return
        repos = sorted(self._pending_mirror)
        with self.sync_lock() as lock:
            if not lock.acquired:
                report.mirror_skipped_locked = True
                return
            ok, _ = self._guarded(
                "mirror", report, lambda: self.mirror.sync_mirror(repos)
            )
        self._last_mirror = now
        if ok:
            self._pending_mirror.difference_update(repos)
            report.mirror_ran = True

    # ----- backfill child ------------------------------------------------------------

    def _reap_child(self, report: TickReport) -> None:
        if self.child is None:
            return
        code = self.child.poll()
        if code is None:
            return
        report.child_exit = code
        self.log(f"backfill child {self.child.pid} exited with {code}")
        self.child = None

    def _maybe_spawn_child(self, now: float, report: TickReport) -> None:
        if self.child_argv is None or self.child is not None or now < self._next_child:
            return
        list_repos = getattr(self.store, "list_repos", None)
        if list_repos is not None and not list_repos("mirror"):
            self._next_child = now + BACKFILL_INTERVAL_S
            return
        if not self._gate("backfill", Priority.BACKFILL, now, report):
            return
        try:
            self.child = self.spawn(self.child_argv)
        except OSError as exc:
            report.errors.append(f"backfill: {exc}")
            self.log(f"backfill spawn failed: {exc}")
        else:
            report.child_spawned = True
            self.log(f"backfill child {self.child.pid} started")
        self._next_child = now + BACKFILL_INTERVAL_S

    def stop_child(self) -> None:
        """Terminates the backfill child's process group, if one is running."""
        if self.child is not None and self.child.poll() is None:
            self.child.terminate_group()
        self.child = None

    # ----- housekeeping and the loop -------------------------------------------------

    def _upgrade_drift_reason(self, *, force_db: bool) -> str | None:
        """Returns a human-readable upgrade reason if schema, views, or scripts drifted."""
        current_sha = compute_scripts_sha256()
        if current_sha != self._startup_scripts_sha:
            return f"scripts sha changed {self._startup_scripts_sha[:12]} -> {current_sha[:12]}"
        if not self._has_version_reader:
            return None
        current_token = _store_db_token(self.store)
        if (
            not force_db
            and current_token is not None
            and current_token == self._last_db_token
        ):
            return None
        versions = _read_store_versions(self.store)
        if versions is None:
            return None
        schema_ver, views_ver = versions
        if schema_ver != self._startup_schema_version:
            return (
                f"schema version changed {self._startup_schema_version} -> {schema_ver}"
            )
        if views_ver != self._startup_views_version:
            return f"views version changed {self._startup_views_version} -> {views_ver}"
        return None

    def _check_upgrade(self, report: TickReport, *, force_db: bool = False) -> bool:
        """Checks for schema, views, or code drift and flags `report.upgrade_detected`."""
        if report.upgrade_detected:
            return True
        reason = self._upgrade_drift_reason(force_db=force_db)
        if reason is None:
            return False
        report.upgrade_detected = True
        self.log(f"upgrade detected ({reason}); exiting for restart")
        return True

    def _write_store_heartbeat(self, beat_at: float) -> None:
        """Persists the heartbeat, falling back for 3-arg test doubles."""
        try:
            self.store.write_heartbeat(
                os.getpid(),
                self._version_string,
                beat_at,
                last_ok_at=self._last_ok_at,
                consecutive_failed_ticks=self._consecutive_failed_ticks,
            )
        except TypeError:
            self.store.write_heartbeat(os.getpid(), self._version_string, beat_at)
        self._last_db_token = _store_db_token(self.store)

    def _beat(self, now: float, report: TickReport | None = None) -> None:
        """Writes the heartbeat stamped with the later of `now` and the clock."""
        beat_at = max(now, self.clock())
        try:
            self._write_store_heartbeat(beat_at)
        except sqlite3.OperationalError as exc:
            if report is not None:
                report.errors.append(f"heartbeat: {exc}")
            self.log(f"heartbeat failed: {exc}")

    def _run_wal_checkpoint(self, report: TickReport) -> None:
        """Runs `PRAGMA wal_checkpoint(TRUNCATE)` when supported by `self.store`."""
        checkpoint = getattr(self.store, "wal_checkpoint_truncate", None)
        if checkpoint is None:
            return
        result = checkpoint()
        if isinstance(result, tuple) and len(result) == 3:
            triple = (int(result[0]), int(result[1]), int(result[2]))
            report.wal_checkpoint = triple
            self.log(
                f"wal_checkpoint(TRUNCATE): busy={triple[0]} log={triple[1]} checkpointed={triple[2]}"
            )

    def _update_watch_expiry_cache(self, active: Sequence[WatchLease]) -> None:
        """Updates the earliest seen `lease_expires_at` across active watches."""
        earliest = _earliest_lease_expiry(active)
        if earliest is not None and (
            self._next_watch_expiry is None or earliest < self._next_watch_expiry
        ):
            self._next_watch_expiry = earliest

    def _should_prune_watches(self, now: float) -> bool:
        """Returns True when a cached lease has expired or hourly prune is due."""
        if now >= self._next_prune:
            return True
        return (
            self._next_watch_expiry is not None
            and self._next_watch_expiry <= iso_at(now)
        )

    def _housekeeping(
        self, now: float, active: Sequence[WatchLease], report: TickReport
    ) -> None:
        prune_due = now >= self._next_prune
        if self._check_upgrade(report, force_db=prune_due):
            return
        try:
            if self._should_prune_watches(now):
                self.store.prune_expired_watches(now)
                self._next_watch_expiry = _earliest_lease_expiry(active)
            if prune_due:
                self.store.prune(now)
                report.pruned = True
                self._next_prune = now + PRUNE_INTERVAL_S
                self._run_wal_checkpoint(report)
        except sqlite3.OperationalError as exc:
            if self._check_upgrade(report, force_db=True):
                return
            report.errors.append(f"housekeeping: {exc}")
            self.log(f"housekeeping failed: {exc}")
        if report.errors:
            self._consecutive_failed_ticks += 1
        else:
            self._consecutive_failed_ticks = 0
            self._last_ok_at = iso_at(now)
        self._beat(now, report)

    def run_once(self, now: float | None = None) -> TickReport:
        """Runs one scheduler tick and reports what it did.

        Order: reap the child, drain refresh requests, poll signals and due
        check runs, refresh dirty and sweep-due leased items, then the viewer,
        `items` timelines, mirror, and backfill child when due, and finally
        housekeeping (upgrade check, pruning, WAL truncate, and heartbeat).
        """
        moment = self.clock() if now is None else now
        report = TickReport(at=moment)
        self._reap_child(report)
        active: list[WatchLease] = []
        try:
            self._drain_requests(moment, report)
            active = self.store.active_watches(moment)
            self._update_watch_expiry_cache(active)
            leases = _leases_by_key(active)
            dirty = _Dirty()
            self._poll_signals(moment, dirty, report)
            self._poll_checks(leases, moment, dirty, report)
            if report.signals_polled or report.checks_polled:
                self._drain_requests(moment, report)
            self._run_hot(leases, moment, dirty, report)
            self._run_viewer(moment, dirty, report)
            self._run_timeline(moment, report)
            if report.viewer_ran or report.timeline_ran:
                self._drain_requests(moment, report)
            self._run_mirror(moment, dirty, report)
            if report.mirror_ran:
                self._drain_requests(moment, report)
            self._requeue_failed(moment)
        except sqlite3.OperationalError as exc:
            report.errors.append(f"sqlite: {exc}")
            self.log(f"sqlite error during tick: {exc}")
            self._check_upgrade(report, force_db=True)
        self._maybe_spawn_child(moment, report)
        self._housekeeping(moment, active, report)
        return report

    def run(self, stop: threading.Event) -> int:
        """Ticks until `stop` is set or an upgrade is detected, then stops the child and returns 0.

        When called from the main thread, SIGTERM and SIGINT set `stop`, so the
        current tick finishes before the service exits.
        """
        restore = install_stop_handlers(stop)
        try:
            if self._upgrade_drift_reason(force_db=True) is None:
                self._beat(self.clock())
            while not stop.is_set():
                report = self.run_once()
                if report.upgrade_detected:
                    break
                stop.wait(self.tick_s)
        finally:
            self.stop_child()
            restore()
        return 0


def install_stop_handlers(stop: threading.Event) -> Callable[[], None]:
    """Makes SIGTERM and SIGINT set `stop`; returns a function restoring the old handlers.

    Does nothing (and returns a no-op) outside the main thread, where Python
    does not allow installing signal handlers.
    """
    if threading.current_thread() is not threading.main_thread():
        return lambda: None

    def _handler(_signum: int, _frame: Any) -> None:
        stop.set()

    previous = {
        sig: signal.signal(sig, _handler) for sig in (signal.SIGTERM, signal.SIGINT)
    }

    def _restore() -> None:
        for sig, handler in previous.items():
            signal.signal(sig, handler)

    return _restore
