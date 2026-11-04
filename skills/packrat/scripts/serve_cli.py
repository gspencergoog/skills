"""`packrat serve`, `watch`, `changes`, and `refresh` subcommands.

These commands wire the Wave 2 workers (signals, hot, viewer, and the service
scheduler) to the CLI and give readers a write path that never touches the
database directly: they lease items with `watch`, follow the change feed with
`changes`, and ask for immediate refreshes with `refresh`.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from db_store import PackratStore
from gh_transport import BudgetDeferred, GhApiError, GhTransport
from github_sync import GhCommandResult, sync_github_metadata
from hot_sync import HotRefresher
from models import REPO_MODE_ITEMS, RepoConfig, SyncStats
from rate_budget import RATE_BUDGET_FILENAME, Priority, RateBudget
from service import PackratService, TickReport
from signals import SignalPoller
from sync_runtime import acquire_serve_lock
from viewer_sync import ViewerSync

EXIT_SERVE_LOCKED = 4
"""Exit status of `packrat serve` when another service holds `serve.lock`."""

DEFAULT_WATCH_FRESHNESS_S = 120
DEFAULT_WATCH_TTL_S = 900
DEFAULT_CHANGES_LIMIT = 500
CHANGES_POLL_S = 1.0
MIRROR_MAX_PAGES = 10

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
    "set_rollup_etags": _returns(None),
    "set_node_id": _returns(None),
    "append_change": _returns(0),
    "upsert_pr_live": _returns([]),
    "upsert_notifications": _returns([]),
    "upsert_thread_activity": _returns(False),
    "replace_thread_activity": _returns(False),
    "upsert_item_comments": _returns(False),
    "replace_check_runs": _returns(False),
    "replace_review_requests": _returns(False),
    "replace_latest_opinionated_reviews": _returns(False),
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
    "prune",
    "record_",
    "remove_",
    "renew_",
    "replace_",
    "request_",
    "reset_",
    "set_",
    "take_",
    "update_",
    "upsert_",
    "write_",
)
"""Name prefixes of store writes; unlisted writes with these prefixes return None."""

_DRY_RUN_READS: frozenset[str] = frozenset(
    {
        "get_node_ids",
        "get_live_fetched_at",
        "get_pr_head_sha",
        "get_rollup_etags",
        "get_state",
        "read_heartbeat",
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
        if hasattr(self._store, "peek_refresh_requests"):
            return list(self._store.peek_refresh_requests())
        rows = self._store.conn.execute(
            "SELECT item_key FROM refresh_requests ORDER BY requested_at, item_key"
        ).fetchall()
        return [str(r["item_key"]) for r in rows]

    def ensure_items_repo(self, owner: str, name: str) -> RepoConfig:
        """Returns the registered repository, or an unsaved `items` placeholder."""
        full_name = f"{owner}/{name}"
        existing = self._store.get_repo(full_name)
        if existing is not None:
            return existing
        return RepoConfig(
            repo_id=0, owner=owner, name=name, full_name=full_name, mode=REPO_MODE_ITEMS
        )


class MirrorAdapter:
    """Runs the incremental `sync_github_metadata` walk for repos with event changes.

    Calls go through the transport's `as_runner` at `Priority.MIRROR`, so they
    are budgeted and logged in `api_calls`.
    """

    def __init__(
        self, store: PackratStore, transport: GhTransport, budget: RateBudget
    ) -> None:
        """Initializes the adapter."""
        self.store = store
        self.runner = transport.as_runner("mirror", Priority.MIRROR)
        self.budget = budget

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
        for repo_id in sorted(repo_ids):
            repo = self.store.get_repo_by_id(repo_id)
            if repo is None or not repo.is_mirror:
                continue
            results.append(
                sync_github_metadata(
                    self.store,
                    repo,
                    max_pages=MIRROR_MAX_PAGES,
                    runner=self.runner,
                    budget=self.budget,
                    skip_events_poll=True,
                )
            )
        _raise_if_incomplete(results)
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
    hot = HotRefresher(transport, store)
    viewer = ViewerSync(transport, store, hot)
    return PackratService(
        store,
        SignalPoller(transport, store),
        hot,
        viewer,
        MirrorAdapter(store, transport, budget),
        None if once or runner is not None else backfill_child_argv(db_path),
        budget=budget,
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
        f"checks={report.checks_polled} viewer={report.viewer_ran} mirror={report.mirror_ran}"
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


def cmd_watch(args: argparse.Namespace, store: PackratStore) -> int:
    """Handles `packrat watch add|renew|remove`."""
    items = list(args.items or [])
    if args.watch_action == "add":
        if not items:
            raise ValueError("watch add needs at least one ITEM (owner/name#number).")
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
    return {
        "pid": beat.pid if beat else None,
        "started_at": beat.started_at if beat else None,
        "beat_at": beat.beat_at if beat else None,
        "beat_age_s": _beat_age_s(beat.beat_at if beat else None, moment),
        "version": beat.version if beat else None,
        "leases_by_consumer": dict(sorted(leases.items())),
    }


def format_service_status(service: dict[str, Any]) -> str:
    """Formats `service_status()` as one line of `packrat status` text."""
    leases = service.get("leases_by_consumer") or {}
    lease_text = ", ".join(f"{k}={v}" for k, v in leases.items()) or "none"
    age = service.get("beat_age_s")
    if age is None:
        return f"Service: never ran; leases: {lease_text}"
    state = "running" if age <= 180 else "stale"
    return f"Service: {state} (pid={service.get('pid')}, heartbeat {age}s ago); leases: {lease_text}"


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
