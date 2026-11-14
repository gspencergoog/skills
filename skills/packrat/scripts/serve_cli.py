"""`packrat serve`, `watch`, `changes`, and `refresh` subcommands.

These commands wire the Wave 2 workers (signals, hot, viewer, and the service
scheduler) to the CLI and give readers a write path that never touches the
database directly: they lease items with `watch`, follow the change feed with
`changes`, and ask for immediate refreshes with `refresh`.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from db_store import PackratStore
from gh_transport import (
    BudgetDeferred,
    GhApiError,
    GhCommandResult,
    GhTransport,
    run_gh_command,
)
from managed_git import (
    GitResult,
    check_remote,
    fetch_git,
    git_status,
)
from models import (
    REPO_MODE_ITEMS,
    RepoConfig,
    SyncStats,
    TimelineMode,
    TimelineStats,
    parse_item_key,
)
from rate_budget import RATE_BUDGET_FILENAME, Priority, RateBudget
from sync_runtime import acquire_serve_lock

if TYPE_CHECKING:
    from service import PackratService, TickReport

_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "HotRefresher": ("hot_sync", "HotRefresher"),
    "PackratService": ("service", "PackratService"),
    "SignalPoller": ("signals", "SignalPoller"),
    "TickReport": ("service", "TickReport"),
    "ViewerSync": ("viewer_sync", "ViewerSync"),
    "format_service_version": ("service", "format_service_version"),
    "sync_github_metadata": ("github_sync", "sync_github_metadata"),
    "sync_repo_timelines": ("timeline_sync", "sync_repo_timelines"),
}


def __getattr__(name: str) -> Any:
    """Lazily imports worker classes and sync functions on first access (PEP 562)."""
    target = _LAZY_ATTRS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    mod_name, attr_name = target
    value = getattr(importlib.import_module(mod_name), attr_name)
    globals()[name] = value
    return value


def _lazy(name: str) -> Any:
    """Resolves a lazy module attribute through `globals()` or `__getattr__`."""
    if name in globals():
        return globals()[name]
    return __getattr__(name)


EXIT_SERVE_LOCKED = 4
"""Exit status of `packrat serve` when another service holds `serve.lock`."""

DEFAULT_WATCH_FRESHNESS_S = 120
DEFAULT_WATCH_TTL_S = 900
DEFAULT_CHANGES_LIMIT = 500
CHANGES_POLL_S = 1.0
MIRROR_MAX_PAGES = 10
MIRROR_TIMELINE_MAX_BATCHES = 25
"""Timeline batches per pass after each service mirror run."""
ITEMS_TIMELINE_MAX_BATCHES = 10
"""Timeline batches per pass for each `items` repository per timeline tick."""

_GITHUB_STOP_MARKERS = ("keyring_locked", "rate_limit")
"""Substrings of a GitHub sync error or skip reason that make the timeline pass skip."""

DRY_RUN_GRAPHQL_STDOUT = 'HTTP/2.0 200 OK\r\n\r\n{"data": {}}'
"""Canned dry-run GraphQL answer: an empty `data` object, so workers find nothing."""

DRY_RUN_REST_STDOUT = "HTTP/2.0 304 Not Modified\r\n\r\n"
"""Canned dry-run REST answer: 304, so pollers see "nothing changed"."""


@dataclass
class DryRunRunner:
    """A `GhRunner` that records intended `gh` calls instead of running them.

    REST calls answer `304 Not Modified`, so pollers see "nothing changed".
    GraphQL calls answer `{"data": {}}`, so workers find nothing to write and
    report no errors.
    """

    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, args: Sequence[str], timeout: int = 30) -> GhCommandResult:
        """Records `args` and returns a canned result."""
        del timeout
        self.calls.append(list(args))
        if "graphql" in args:
            return GhCommandResult(
                returncode=0, stdout=DRY_RUN_GRAPHQL_STDOUT, stderr=""
            )
        return GhCommandResult(returncode=0, stdout=DRY_RUN_REST_STDOUT, stderr="")

    def summary(self) -> list[str]:
        """Describes each recorded call on one line (GraphQL queries abbreviated)."""
        out: list[str] = []
        for args in self.calls:
            query = next((a for a in args if a.startswith("query=")), None)
            if query is not None:
                first = " ".join(query[len("query=") :].split())[:120]
                out.append(f"graphql {first}")
            else:
                path = next(
                    (a for a in args[1:] if not a.startswith("-") and ":" not in a), ""
                )
                out.append(f"GET {path}")
        return out


def _returns(value: Any) -> Callable[..., Any]:
    """Builds a no-op store method that returns a fresh copy of `value`."""

    def no_op(*_args: Any, **_kwargs: Any) -> Any:
        return type(value)(value) if isinstance(value, (list, dict, tuple)) else value

    return no_op


def _echo_keys(item_keys: Iterable[str], *_args: Any, **_kwargs: Any) -> list[str]:
    """No-op `request_refresh` that reports the keys it would have queued."""
    return list(item_keys)


def _echo_watch(
    _consumer: str,
    _consumer_id: str,
    item_keys: Iterable[str],
    *_args: Any,
    **_kwargs: Any,
) -> list[str]:
    """No-op `add_watch` that reports the keys it would have leased."""
    return list(item_keys)


_DRY_RUN_WRITES: dict[str, Callable[..., Any]] = {
    "write_heartbeat": _returns(None),
    "record_api_call": _returns(None),
    "prune": _returns({}),
    "prune_expired_watches": _returns(0),
    "request_refresh": _echo_keys,
    "set_state": _returns(None),
    "set_gap_state": _returns(None),
    "set_commits_backfill_state": _returns(None),
    "set_rollup_etags": _returns(None),
    "set_node_id": _returns(None),
    "append_change": _returns(0),
    "upsert_pr_live": _returns([]),
    "update_pr_fork_owners": _returns(0),
    "upsert_indexed_files": _returns(None),
    "upsert_timeline_events": _returns(0),
    "take_timeline_dirty": _returns([]),
    "enqueue_timeline_dirty": _returns(0),
    "mark_timeline_synced": _returns(None),
    "mark_timelines_synced": _returns(None),
    "touch_live_fetched_at": _returns(None),
    "wal_checkpoint_truncate": _returns((0, 0, 0)),
    "upsert_notifications": _returns([]),
    "upsert_thread_activity": _returns(False),
    "replace_thread_activity": _returns(False),
    "upsert_item_comments": _returns(False),
    "replace_check_runs": _returns(False),
    "replace_review_requests": _returns(False),
    "replace_latest_opinionated_reviews": _returns(False),
    "replace_latest_reviews": _returns(False),
    "replace_viewer_set": _returns(([], [])),
    "add_watch": _echo_watch,
    "renew_watch": _returns(0),
    "remove_watch": _returns(0),
}
"""Store writes the service, workers, and transport make, with their dry-run results."""

_WRITE_PREFIXES = (
    "add_",
    "append_",
    "delete_",
    "mark_",
    "prune",
    "record_",
    "remove_",
    "renew_",
    "replace_",
    "request_",
    "reset_",
    "set_",
    "take_",
    "touch_",
    "update_",
    "upsert_",
    "wal_",
    "write_",
)
"""Name prefixes of store writes; unlisted writes with these prefixes return None."""

_DRY_RUN_READS: frozenset[str] = frozenset(
    {
        "find_repo_by_node_id",
        "git_root",
        "resolve_git_dir",
        "get_node_ids",
        "get_live_fetched_at",
        "get_live_pr_fingerprints",
        "get_pr_head_sha",
        "get_rollup_etags",
        "get_state",
        "get_gap_state",
        "get_indexed_files",
        "get_parser_overrides",
        "has_any_chunks",
        "query_fts_candidates",
        "read_all_embeddings_for_mmap",
        "resolve_filtered_chunk_ids",
        "stream_filtered_embeddings",
        "read_heartbeat",
        "read_schema_and_views_version",
        "read_snapshot",
        "active_watches",
        "changes_since",
        "max_change_seq",
        "min_change_seq",
        "list_repos",
        "get_repo",
        "get_repo_by_id",
        "canonical_item_key",
        "peek_refresh_requests",
        "db_path",
        "init_sql_path",
        "table_counts",
        "timeline_candidates",
        "count_timeline_pending",
        "get_item_updated_at",
    }
)
"""Explicit allowlist of read-only store attributes exposed through `DryRunStore`."""


class DryRunStore:
    """Proxies a `PackratStore` for `serve --dry-run`, dropping every write.

    Reads go to the wrapped store. Writes return a plausible result without
    touching the database, so a dry run leaves no heartbeat, audit rows,
    pruned rows, consumed refresh requests, or saved ETags behind.
    """

    _DRY_RUN_READS = _DRY_RUN_READS

    def __init__(self, store: PackratStore) -> None:
        """Wraps `store`."""
        self._store = store

    def __getattr__(self, name: str) -> Any:
        """Returns a no-op for store writes and allowlisted reads on the wrapped store."""
        if name in _DRY_RUN_WRITES:
            return _DRY_RUN_WRITES[name]
        if name.startswith(_WRITE_PREFIXES):
            return _returns(None)
        if name in _DRY_RUN_READS:
            return getattr(self._store, name)
        raise AttributeError(f"DryRunStore blocks unverified store attribute {name!r}")

    def take_refresh_requests(self) -> list[str]:
        """Returns the queued refresh requests without removing them."""
        return list(self._store.peek_refresh_requests())

    def ensure_items_repo(self, owner: str, name: str) -> RepoConfig:
        """Returns the registered repository, or an unsaved `items` placeholder."""
        full_name = f"{owner}/{name}"
        existing = self._store.get_repo(full_name)
        if existing is not None:
            return existing
        return RepoConfig(
            repo_id=0, owner=owner, name=name, full_name=full_name, mode=REPO_MODE_ITEMS
        )

    def merge_repo_alias(
        self, _alias_repo_id: int, canonical_full_name: str, *_args: Any, **_kwargs: Any
    ) -> RepoConfig:
        """Returns the canonical repository without mutating the database."""
        owner, name = canonical_full_name.split("/", 1)
        return self.ensure_items_repo(owner, name)


def github_sync_stopped(stats: SyncStats) -> bool:
    """Returns whether a GitHub sync stopped for a locked keyring or a rate limit.

    A timeline pass after such a run would fail the same way, so callers skip
    it. Budget deferrals of single streams don't count: each timeline call
    checks the budget at its own priority.
    """
    texts = [*stats.errors, stats.skipped_reason or ""]
    return any(marker in text for text in texts for marker in _GITHUB_STOP_MARKERS)


def fold_timeline_stats(stats: SyncStats, timeline: TimelineStats) -> None:
    """Attaches `timeline` to `stats` and folds its outcome into the run's status.

    A deferred timeline run adds "timeline" to `deferred_streams` and turns
    its errors (the rate-limit reason) into warnings; otherwise its errors
    become run errors.
    """
    stats.timeline = timeline
    if timeline.deferred:
        stats.deferred = True
        if "timeline" not in stats.deferred_streams:
            stats.deferred_streams.append("timeline")
        stats.warnings.extend(timeline.errors)
        return
    stats.errors.extend(timeline.errors)


class MirrorAdapter:
    """Runs the incremental `sync_github_metadata` walk for repos with event changes.

    Each walk is followed by an incremental timeline pass that also drains the
    repository's cross-reference queue. Calls go through the transport's
    `as_runner` at `Priority.MIRROR`, so they are budgeted and logged in
    `api_calls`.
    """

    def __init__(
        self, store: PackratStore, transport: GhTransport, budget: RateBudget
    ) -> None:
        """Initializes the adapter."""
        self.store = store
        self.runner = transport.as_runner("mirror", Priority.MIRROR)
        self.budget = budget

    def _effective_budget(self) -> RateBudget | None:
        """Returns None when `self.runner` already gates and records the rate budget."""
        return (
            None if getattr(self.runner, "is_budgeted_runner", False) else self.budget
        )

    def sync_mirror(self, repo_ids: Collection[int]) -> list[SyncStats]:
        """Syncs each `mirror`-mode repository in `repo_ids`.

        The signals worker already polled each repository's `/events`, so the
        walk skips that poll. Every repository is attempted. An incomplete run
        then raises, so the service keeps the repositories pending and retries
        them.

        Raises:
            BudgetDeferred: At least one run was deferred by the rate budget.
            GhApiError: At least one run recorded an error (and none deferred).
        """
        results: list[SyncStats] = []
        sync_gh = _lazy("sync_github_metadata")
        effective_budget = self._effective_budget()
        for repo_id in sorted(repo_ids):
            repo = self.store.get_repo_by_id(repo_id)
            if repo is None or not repo.is_mirror:
                continue
            stats = sync_gh(
                self.store,
                repo,
                max_pages=MIRROR_MAX_PAGES,
                backfill_max_pages=0,
                runner=self.runner,
                budget=effective_budget,
                skip_events_poll=True,
            )
            if not github_sync_stopped(stats):
                fold_timeline_stats(stats, self._sync_timeline(repo))
            results.append(stats)
        _raise_if_incomplete(results)
        return results

    def _sync_timeline(self, repo: RepoConfig) -> TimelineStats:
        """Runs the incremental timeline pass and xref drain for one mirror repository."""
        sync_tl = _lazy("sync_repo_timelines")
        return sync_tl(
            self.store,
            repo,
            mode=TimelineMode.INCREMENTAL,
            runner=self.runner,
            budget=self._effective_budget(),
            max_batches=MIRROR_TIMELINE_MAX_BATCHES,
            drain_xrefs=True,
            priority=Priority.MIRROR,
        )


class TimelineAdapter:
    """Fetches timelines for stored PRs and issues of `items`-mode repositories.

    `items` repositories hold only watched, notified, or viewer items, so
    there is no cross-reference queue to drain. Calls go through the
    transport's `as_runner("timeline", Priority.VIEWER)`, so the service gates
    them at `VIEWER` priority and logs them in `api_calls` under `timeline`.
    """

    def __init__(
        self, store: PackratStore, transport: GhTransport, budget: RateBudget
    ) -> None:
        """Initializes the adapter."""
        self.store = store
        self.runner = transport.as_runner("timeline", Priority.VIEWER)
        self.budget = budget

    def _effective_budget(self) -> RateBudget | None:
        """Returns None when `self.runner` already gates and records the rate budget."""
        return (
            None if getattr(self.runner, "is_budgeted_runner", False) else self.budget
        )

    def sync_items_timelines(self) -> list[TimelineStats]:
        """Runs one bounded timeline pass for each `items`-mode repository.

        Stops at the first deferred repository.

        Raises:
            BudgetDeferred: A run was deferred by the rate budget.
            GhApiError: At least one run recorded an error (and none deferred).
        """
        results: list[TimelineStats] = []
        sync_tl = _lazy("sync_repo_timelines")
        effective_budget = self._effective_budget()
        for repo in self.store.list_repos(REPO_MODE_ITEMS):
            stats = sync_tl(
                self.store,
                repo,
                mode=TimelineMode.ALL,
                runner=self.runner,
                budget=effective_budget,
                max_batches=ITEMS_TIMELINE_MAX_BATCHES,
                drain_xrefs=False,
                priority=Priority.VIEWER,
            )
            if stats.deferred:
                detail = "; ".join(stats.errors) or "budget below threshold"
                raise BudgetDeferred(
                    "graphql",
                    Priority.VIEWER,
                    f"timeline deferred ({repo.full_name}: {detail})",
                )
            results.append(stats)
        failed = [err for stats in results for err in stats.errors]
        if failed:
            raise GhApiError(0, f"timeline sync incomplete ({failed[0]})")
        return results


def _raise_if_incomplete(results: Sequence[SyncStats]) -> None:
    """Raises when any mirror run was deferred or recorded errors."""
    deferred = [s for s in results if s.deferred or s.deferred_streams]
    if deferred:
        detail = "; ".join(
            f"{s.repo_full_name}: {s.skipped_reason or ', '.join(s.deferred_streams) or 'deferred'}"
            for s in deferred
        )
        raise BudgetDeferred("graphql", Priority.MIRROR, f"mirror deferred ({detail})")
    failed = [s for s in results if s.errors]
    if failed:
        detail = "; ".join(f"{s.repo_full_name}: {s.errors[0]}" for s in failed)
        raise GhApiError(0, f"mirror sync incomplete ({detail})")


GIT_POLL_INTERVAL_S = 120.0
GIT_BACKOFF_S = 600.0
GIT_ERROR_LOG_INTERVAL_S = 3600.0


def git_poll_key(repo_id: int) -> str:
    """Returns the machine-local `service_state` key for a managed clone."""
    return f"git_poll:{int(repo_id)}"


def read_git_poll(store: Any, repo_id: int) -> dict[str, Any]:
    """Reads `service_state['git_poll:<repo_id>']` JSON cleanly, or `{}`."""
    raw = store.get_state(git_poll_key(repo_id))
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
        return loaded if isinstance(loaded, dict) else {}
    except json.JSONDecodeError:
        return {}


def write_git_poll(
    store: Any, repo_id: int, data: dict[str, Any], *, now: float | None = None
) -> None:
    """Persists `service_state['git_poll:<repo_id>']` JSON."""
    store.set_state(git_poll_key(repo_id), json.dumps(data, sort_keys=True), now=now)


class ManagedGitAdapter:
    """Implements `service.ManagedGit` using `managed_git` and `git_poll:<repo_id>` state."""

    def __init__(
        self,
        store: Any,
        *,
        clock: Callable[[], float] = time.time,
        extra_git_config: Sequence[str] = (),
    ) -> None:
        self._store = store
        self._clock = clock
        self._extra_git_config = tuple(extra_git_config)
        self._last_err_log: dict[int, float] = {}

    def _log_poll_error(self, repo: RepoConfig, error: str, now: float) -> None:
        last = self._last_err_log.get(repo.repo_id)
        if last is not None and (now - last) < GIT_ERROR_LOG_INTERVAL_S:
            return
        self._last_err_log[repo.repo_id] = now
        sys.stderr.write(f"git poll failed for {repo.full_name}: {error}\n")

    def _due_managed_repos(self, now: float) -> list[tuple[float, RepoConfig]]:
        due: list[tuple[float, RepoConfig]] = []
        for repo in self._store.list_repos("mirror"):
            if repo.git_source != "managed":
                continue
            state = read_git_poll(self._store, repo.repo_id)
            if float(state.get("backoff_until") or 0.0) > now:
                continue
            checked_at = float(state.get("checked_at") or 0.0)
            if checked_at == 0.0 or (now - checked_at) >= GIT_POLL_INTERVAL_S:
                due.append((checked_at, repo))
        due.sort(key=lambda pair: (pair[0], pair[1].repo_id))
        return due

    def _poll_one(self, repo: RepoConfig, now: float, remaining: float) -> GitResult:
        res = check_remote(
            repo,
            timeout_s=min(20, max(1, int(remaining))),
            extra_git_config=self._extra_git_config,
        )
        root = self._store.git_root
        local_info = (
            git_status(root, repo)
            if root is not None
            else {"exists": False, "local_tip": ""}
        )
        old_tip = str(local_info.get("local_tip") or "")
        state = read_git_poll(self._store, repo.repo_id)
        state["checked_at"] = now
        if res.error or res.timed_out:
            err = res.error or "ls-remote timed out"
            state["last_error"] = err
            state["backoff_until"] = now + GIT_BACKOFF_S
            self._log_poll_error(repo, err, now)
        else:
            state["remote_sha"] = res.new_tip
            if res.remote_default_branch:
                state["remote_default_branch"] = res.remote_default_branch
            state["last_error"] = ""
            state["backoff_until"] = 0.0
        write_git_poll(self._store, repo.repo_id, state, now=now)
        # Missing clone directory still requires fetch/clone when remote has refs.
        needs_clone = (
            not bool(local_info.get("exists")) and not res.error and not res.timed_out
        )
        eff_old = "" if needs_clone else old_tip
        eff_new = res.new_tip or ("__init__" if needs_clone else "")
        return GitResult(
            repo.repo_id,
            eff_old,
            eff_new,
            res.remote_default_branch,
            res.duration_s,
            res.error,
            res.timed_out,
        )

    def poll_remotes(self, now: float, *, budget_s: float) -> list[GitResult]:
        """Polls due managed remotes most-overdue-first within `budget_s`."""
        deadline = time.monotonic() + max(0.0, budget_s)
        results: list[GitResult] = []
        for _, repo in self._due_managed_repos(now):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            results.append(self._poll_one(repo, now, remaining))
        return results

    def _fetch_one(
        self, root: Path, repo: RepoConfig, remaining: float
    ) -> GitResult | None:
        now = self._clock()
        state = read_git_poll(self._store, repo.repo_id)
        discovered_branch = str(state.get("remote_default_branch") or "")
        eff_repo = (
            replace(repo, default_branch=discovered_branch)
            if discovered_branch and discovered_branch != repo.default_branch
            else repo
        )
        res = fetch_git(
            root,
            eff_repo,
            timeout_s=max(1, int(remaining)),
            maintenance="skip",
            blocking_lock=False,
            extra_git_config=self._extra_git_config,
        )
        if res.error == "repo lock held":
            return None
        if res.timed_out or res.error:
            state["last_error"] = res.error or "fetch timed out"
            state["backoff_until"] = now + GIT_BACKOFF_S
        else:
            state["fetched_at"] = now
            state["remote_sha"] = res.new_tip
            state["last_error"] = ""
            state["backoff_until"] = 0.0
        write_git_poll(self._store, repo.repo_id, state, now=now)
        return res

    def fetch(self, repo_ids: Sequence[int], *, budget_s: float) -> list[GitResult]:
        """Fetches queued managed repositories under non-blocking repo locks."""
        root = self._store.git_root
        if root is None:
            return []
        deadline = time.monotonic() + max(0.0, budget_s)
        results: list[GitResult] = []
        for rid in repo_ids:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            repo = self._store.get_repo_by_id(int(rid))
            if repo is None or repo.git_source != "managed":
                continue
            res = self._fetch_one(root, repo, remaining)
            if res is not None:
                results.append(res)
        return results


class DryRunManagedGit:
    """No-op `ManagedGit` for `packrat serve --dry-run` recording intended calls."""

    def __init__(self, store: Any) -> None:
        self._store = store
        self.calls: list[str] = []

    def poll_remotes(self, now: float, *, budget_s: float) -> list[GitResult]:
        del now, budget_s
        for repo in self._store.list_repos("mirror"):
            if repo.git_source == "managed":
                self.calls.append(f"git ls-remote {repo.clone_url}")
        return []

    def fetch(self, repo_ids: Sequence[int], *, budget_s: float) -> list[GitResult]:
        del budget_s
        for rid in repo_ids:
            self.calls.append(f"git fetch repo_id={rid}")
        return []


def scoped_child_argv(
    db_path: Path, store: Any
) -> Callable[[Sequence[int]], list[str]]:
    """Builds the argv generator for ungated post-fetch git indexing children."""
    script = Path(__file__).resolve().parent / "packrat.py"

    def _build(repo_ids: Sequence[int]) -> list[str]:
        cmd = [
            sys.executable,
            str(script),
            "--db",
            str(db_path),
            "sync",
            "--auto",
            "--git-only",
            "--no-fetch",
        ]
        for rid in repo_ids:
            repo = store.get_repo_by_id(int(rid))
            if repo is not None:
                cmd.extend(["--repo", repo.full_name])
        cmd.append("--json")
        return cmd

    return _build


def backfill_child_argv(db_path: Path) -> list[str]:
    """Returns the command the service runs as its niced backfill child."""
    script = Path(__file__).resolve().parent / "packrat.py"
    return [
        sys.executable,
        str(script),
        "--db",
        str(db_path),
        "sync",
        "--auto",
        "--backfill-only",
        "--fetch",
        "--json",
    ]


def build_service(
    store: PackratStore,
    db_path: Path,
    *,
    once: bool,
    runner: DryRunRunner | None = None,
) -> PackratService:
    """Wires the real workers into a `PackratService`.

    Args:
        store: The opened store (or a `DryRunStore`).
        db_path: Database path, for the budget file and the backfill child.
        once: One tick only: never spawn the backfill child.
        runner: Optional `gh` runner override, such as a `DryRunRunner`.
    """
    budget = RateBudget(db_path.parent / RATE_BUDGET_FILENAME)
    transport = GhTransport(budget, store, runner=runner)
    hot = _lazy("HotRefresher")(transport, store)
    viewer = _lazy("ViewerSync")(transport, store, hot)
    managed: Any = (
        DryRunManagedGit(store) if runner is not None else ManagedGitAdapter(store)
    )
    scoped_builder = (
        None if once or runner is not None else scoped_child_argv(db_path, store)
    )
    return _lazy("PackratService")(
        store,
        _lazy("SignalPoller")(transport, store),
        hot,
        viewer,
        MirrorAdapter(store, transport, budget),
        None if once or runner is not None else backfill_child_argv(db_path),
        budget=budget,
        timeline=TimelineAdapter(store, transport, budget),
        managed_git=managed,
        scoped_child_argv=scoped_builder,
    )


def _print_tick(report: TickReport, runner: DryRunRunner | None, as_json: bool) -> None:
    """Prints one tick report (and the dry run's intended calls)."""
    payload: dict[str, Any] = {"dry_run": runner is not None, "report": asdict(report)}
    if runner is not None:
        payload["intended_calls"] = runner.summary()
    if as_json:
        print(json.dumps(payload, indent=2))
        return
    print(
        f"tick: requested={len(report.requested)} hot={len(report.hot_keys)} "
        f"changed={len(report.changed)} signals={report.signals_polled} "
        f"checks={report.checks_polled} viewer={report.viewer_ran} "
        f"timeline={report.timeline_ran} mirror={report.mirror_ran}"
    )
    for item in report.deferred:
        print(f"  deferred: {item}")
    for err in report.errors:
        print(f"  error: {err}")
    for call in payload.get("intended_calls", []):
        print(f"  would call: {call}")


def cmd_serve(args: argparse.Namespace, db_path: Path) -> int:
    """Handles `packrat serve [--once] [--dry-run] [--json]`."""
    with acquire_serve_lock(db_path) as lock:
        if not lock.acquired:
            sys.stderr.write(
                f"Error: packrat serve is already running (pid={lock.holder_pid}, "
                f"started_at={lock.holder_started_at}).\n"
            )
            return EXIT_SERVE_LOCKED
        store = PackratStore(db_path)
        try:
            runner = DryRunRunner() if args.dry_run else None
            target: Any = DryRunStore(store) if runner is not None else store
            once = bool(args.once or args.dry_run)
            service = build_service(target, db_path, once=once, runner=runner)
            if once:
                _print_tick(service.run_once(), runner, bool(args.json))
                return 0
            return service.run(threading.Event())
        finally:
            store.close()


def _probe_watch_repo(store: PackratStore, owner: str, name: str) -> None:
    """Probes `GET /repos/{owner}/{name}` at `HOT` priority before creating an `items` repo."""
    full_name = f"{owner}/{name}"
    budget = RateBudget(store.db_path.parent / RATE_BUDGET_FILENAME)
    transport = GhTransport(budget, store, runner=run_gh_command)
    try:
        transport.rest_get(f"repos/{full_name}", worker="watch", priority=Priority.HOT)
    except GhApiError as exc:
        if exc.status == 404:
            raise ValueError(
                f"Repository '{full_name}' was not found on GitHub (HTTP 404)."
            ) from exc
        sys.stderr.write(
            f"Warning: could not verify {full_name} on GitHub ({exc}); registering offline.\n"
        )
    except BudgetDeferred as exc:
        sys.stderr.write(
            f"Warning: could not verify {full_name} on GitHub ({exc}); registering offline.\n"
        )
    store.ensure_items_repo(owner, name)
    sys.stderr.write(f"registered {full_name} (items)\n")


def _validate_unregistered_watch_repos(
    store: PackratStore, items: Sequence[str]
) -> None:
    """Validates and registers any unknown `<owner>/<name>` repositories referenced in `items`."""
    seen: set[str] = set()
    for item in items:
        owner, name, _ = parse_item_key(item)
        full_name = f"{owner}/{name}"
        lowered = full_name.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        if store.get_repo(full_name) is None:
            _probe_watch_repo(store, owner, name)


def cmd_watch(args: argparse.Namespace, store: PackratStore) -> int:
    """Handles `packrat watch add|renew|remove`."""
    items = list(args.items or [])
    if args.watch_action == "add":
        if not items:
            raise ValueError("watch add needs at least one ITEM (owner/name#number).")
        _validate_unregistered_watch_repos(store, items)
        keys = store.add_watch(args.consumer, args.id, items, args.freshness, args.ttl)
        result: dict[str, Any] = {
            "leased": keys,
            "ttl_s": args.ttl,
            "freshness_s": args.freshness,
        }
    elif args.watch_action == "renew":
        count = store.renew_watch(args.consumer, args.id, args.ttl, items or None)
        result = {"renewed": count, "ttl_s": args.ttl}
    else:
        result = {"removed": store.remove_watch(args.consumer, args.id, items or None)}
    if args.json:
        print(json.dumps(result))
    else:
        print(", ".join(f"{k}={v}" for k, v in result.items()))
    return 0


def changes_payload(
    store: PackratStore, since: int, limit: int
) -> dict[str, Any] | None:
    """Builds the `packrat changes` response, or None when there is nothing new yet.

    Returns `{"reset": True, "seq": max}` when `since` is older than the
    oldest retained row (or newer than anything ever written), so the reader
    must take a full snapshot.
    """
    snap_fn = getattr(store, "read_snapshot", None)
    ctx = snap_fn() if callable(snap_fn) else contextlib.nullcontext()
    with ctx:
        max_seq = store.max_change_seq()
        if since < store.min_change_seq() or since > max_seq:
            return {"reset": True, "seq": max_seq}
        rows = store.changes_since(since, limit)
    if not rows:
        return None
    return {"seq": rows[-1].seq, "changes": [asdict(r) for r in rows]}


def cmd_changes(args: argparse.Namespace, store: PackratStore) -> int:
    """Handles `packrat changes --since SEQ [--wait SECONDS] [--limit N]`."""
    deadline = time.monotonic() + max(0.0, float(args.wait or 0))
    while True:
        payload = changes_payload(store, args.since, args.limit)
        if payload is not None or time.monotonic() >= deadline:
            break
        time.sleep(min(CHANGES_POLL_S, max(0.0, deadline - time.monotonic())))
    if payload is None:
        payload = {"seq": args.since, "changes": []}
    print(json.dumps(payload) if args.json else json.dumps(payload, indent=2))
    return 0


def cmd_refresh(args: argparse.Namespace, store: PackratStore) -> int:
    """Handles `packrat refresh ITEM...`."""
    keys = store.request_refresh(args.items)
    if args.json:
        print(json.dumps({"requested": keys}))
    else:
        print(f"Queued refresh for {len(keys)} item(s): {', '.join(keys)}")
    return 0


def _beat_age_s(beat_at: str | None, now: float) -> int | None:
    """Returns seconds since an ISO-8601 heartbeat, or None."""
    if not beat_at:
        return None
    try:
        then = datetime.fromisoformat(beat_at).timestamp()
    except ValueError:
        return None
    return max(0, round(now - then))


def service_status(store: PackratStore, now: float | None = None) -> dict[str, Any]:
    """Returns the service heartbeat and per-consumer lease counts for `packrat status`."""
    moment = time.time() if now is None else now
    beat = store.read_heartbeat()
    leases = Counter(w.consumer for w in store.active_watches(moment))
    expected_version = _lazy("format_service_version")()
    version = beat.version if beat else None
    code_stale = bool(version and version != expected_version)
    return {
        "pid": beat.pid if beat else None,
        "started_at": beat.started_at if beat else None,
        "beat_at": beat.beat_at if beat else None,
        "beat_age_s": _beat_age_s(beat.beat_at if beat else None, moment),
        "version": version,
        "expected_version": expected_version,
        "code_stale": code_stale,
        "last_ok_at": beat.last_ok_at if beat else None,
        "consecutive_failed_ticks": (beat.consecutive_failed_ticks if beat else 0),
        "leases_by_consumer": dict(sorted(leases.items())),
    }


def _service_health_state(age: int, failed_ticks: int) -> str:
    """Classifies service health from heartbeat age and consecutive failed ticks."""
    if age > 180:
        return "stale"
    if failed_ticks >= 3:
        return "degraded"
    return "running"


def format_service_status(service: dict[str, Any]) -> str:
    """Formats `service_status()` as one or two lines of `packrat status` text."""
    leases = service.get("leases_by_consumer") or {}
    lease_text = ", ".join(f"{k}={v}" for k, v in leases.items()) or "none"
    age = service.get("beat_age_s")
    if age is None:
        return f"Service: never ran; leases: {lease_text}"
    failed = int(service.get("consecutive_failed_ticks") or 0)
    state = _service_health_state(int(age), failed)
    details = [f"pid={service.get('pid')}", f"heartbeat {age}s ago"]
    if service.get("last_ok_at"):
        details.append(f"last_ok_at={service['last_ok_at']}")
    if failed:
        details.append(f"consecutive_failed_ticks={failed}")
    line = f"Service: {state} ({', '.join(details)}); leases: {lease_text}"
    if service.get("code_stale"):
        line += (
            f"\n  Warning: service is running stale code ({service.get('version')}); "
            "run `packrat auto-sync on` to restart onto the current checkout."
        )
    return line


def _int_at_least(minimum: int) -> Callable[[str], int]:
    """Builds an argparse `type=` that accepts integers of at least `minimum`."""

    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"expected an integer, got {text!r}"
            ) from None
        if value < minimum:
            raise argparse.ArgumentTypeError(f"must be at least {minimum}, got {value}")
        return value

    parse.__name__ = f"integer >= {minimum}"
    return parse


def add_live_parsers(sub: Any, func: Any) -> None:
    """Registers `serve`, `watch`, `changes`, and `refresh` on the top-level subparsers.

    Args:
        sub: The top-level `add_subparsers()` object.
        func: Dispatcher stored as each parser's `func` default; it reads
            `command_kind` ("serve" or "store") and `store_func`.
    """
    p_serve = sub.add_parser(
        "serve", help="Run the long-lived GitHub service (packrat.service)."
    )
    p_serve.add_argument("--once", action="store_true", help="Run one tick and exit.")
    p_serve.add_argument(
        "--dry-run",
        action="store_true",
        help="Run one tick that records intended GitHub calls without making them.",
    )
    p_serve.add_argument(
        "--json", action="store_true", help="Emit the tick report as JSON."
    )
    p_serve.set_defaults(func=func, command_kind="serve")

    p_watch = sub.add_parser(
        "watch", help="Lease items so the service keeps them fresh."
    )
    p_watch.add_argument("watch_action", choices=("add", "renew", "remove"))
    p_watch.add_argument("items", nargs="*", help="Items as owner/name#number.")
    p_watch.add_argument(
        "--consumer", required=True, help="Consumer kind, e.g. monitor."
    )
    p_watch.add_argument("--id", required=True, help="Consumer instance id.")
    p_watch.add_argument(
        "--freshness",
        type=_int_at_least(1),
        default=DEFAULT_WATCH_FRESHNESS_S,
        help="Max data age (s).",
    )
    p_watch.add_argument(
        "--ttl",
        type=_int_at_least(1),
        default=DEFAULT_WATCH_TTL_S,
        help="Lease TTL (s).",
    )
    p_watch.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_watch.set_defaults(func=func, command_kind="store", store_func=cmd_watch)

    p_changes = sub.add_parser(
        "changes", help="Read the change feed after a sequence number."
    )
    p_changes.add_argument(
        "--since", type=_int_at_least(0), required=True, help="Last seq already seen."
    )
    p_changes.add_argument(
        "--wait", type=float, default=0.0, help="Wait up to N s for rows."
    )
    p_changes.add_argument(
        "--limit",
        type=_int_at_least(1),
        default=DEFAULT_CHANGES_LIMIT,
        help="Max rows.",
    )
    p_changes.add_argument("--json", action="store_true", help="Emit compact JSON.")
    p_changes.set_defaults(func=func, command_kind="store", store_func=cmd_changes)

    p_refresh = sub.add_parser("refresh", help="Ask the service to refresh items now.")
    p_refresh.add_argument("items", nargs="+", help="Items as owner/name#number.")
    p_refresh.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_refresh.set_defaults(func=func, command_kind="store", store_func=cmd_refresh)
