#!/usr/bin/env python3
"""Continuously watches GitHub pull requests and exits with a JSON delta on change."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import packrat_source
from a2ui_board import board_data_model, board_dsl, sync_pane_store, write_board_file
from daemon_support import (
    CONTROL_FILE,
    EXIT_ALREADY_RUNNING,
    AckGate,
    ControlReader,
    Liveness,
    Notifier,
    format_batch_message,
    resolve_notifier,
)
from diff_events import (
    DiffContext,
    diff_snapshots,
    filter_events,
    is_terminal,
    snapshot_thread_watermarks,
    summarize,
)
from gh_client import GhError, GhRunner, RateLimitBackoff, SubprocessGh, fetch_prs
from models import (
    EVENT_KINDS,
    SCHEMA_VERSION,
    Event,
    MonitorState,
    PrSnapshot,
    parse_pr_ref,
    pr_key,
)
from normalize import normalize_pr, resolve_stack_parents
from notifications import DEFAULT_POLL_INTERVAL_S, NotificationsUnavailable, poll
from packrat_source import (
    ChangeBatch,
    PackratError,
    PackratRunner,
    PackratSource,
    available,
    gh_config_user,
)
from state import (
    StateError,
    append_event_log,
    eta_seconds,
    load_state,
    record_completed_durations,
    save_state,
)

MAX_WATCHED_PRS = 60
MAX_CONSECUTIVE_ERRORS = 5
FAST_POLL_INTERVAL_S = 10
DEFAULT_SWEEP_S = 600
BASE_BRANCHES = frozenset({"main", "master", "develop"})
PACKRAT_RENEW_S = 300
"""Interval between ``packrat watch renew`` calls (the lease TTL is 900 s)."""
PACKRAT_DEFAULT_WAIT_S = 20
"""``packrat changes --wait`` timeout when ``--debounce`` is 0."""
PACKRAT_STARTUP_WAIT_S = 30
"""How long startup waits for packrat to refresh missing or stale keys before using gh."""
PACKRAT_STALE_ROW_S = 900
"""A ``v_pr_live`` row older than this at startup is refreshed before it is trusted."""
_FEED_HEAD = 2**62
"""A ``--since`` past every row: ``packrat changes`` answers with a reset at the feed head."""


@dataclass
class _Timers:
    """Monotonic deadlines for the refresh loop's independent cadences."""

    next_poll: float
    """Next targeted refresh of flagged keys (``--poll``)."""
    next_sweep: float
    """Next full refresh of every watched PR (``--sweep``)."""
    next_notify: float
    """Next ``GET /notifications`` poll (``--notify-poll``)."""
    backoff_until: float = 0.0
    """No GitHub refresh runs before this time after a failed refresh."""


@dataclass(frozen=True)
class _LoopConfig:
    """Interval settings for ``Monitor._loop``, derived from CLI arguments."""

    poll_s: int
    sweep_s: int
    notify_s: int
    debounce_s: int
    allowed_kinds: set[str] | None


def utcnow_iso() -> str:
    """Returns current UTC timestamp in ISO-8601 ``YYYY-MM-DDTHH:MM:SSZ`` format."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_event_list(raw: str | None) -> set[str] | None:
    if not raw:
        return None
    items = {part.strip() for part in raw.split(",") if part.strip()}
    unknown = sorted(items - set(EVENT_KINDS))
    if unknown:
        raise ValueError(
            f"Unknown event kind(s): {', '.join(unknown)}. Valid kinds: {', '.join(EVENT_KINDS)}"
        )
    return items


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parses CLI arguments for ``monitor_prs.py``."""
    parser = argparse.ArgumentParser(
        prog="monitor_prs.py",
        description="Watch GitHub pull requests and exit with a JSON delta on change.",
    )
    parser.add_argument(
        "prs",
        nargs="*",
        help="PRs to watch: owner/repo#N, https://github.com/owner/repo/pull/N, or N (with --repo).",
    )
    parser.add_argument("--repo", help="Default owner/repo for bare PR numbers.")
    parser.add_argument(
        "--stack",
        action="store_true",
        help="Expand each target PR to include its stacked parent and child PRs.",
    )
    parser.add_argument(
        "--state",
        required=True,
        help="Path to the conversation state snapshot JSON file.",
    )
    parser.add_argument(
        "--events",
        help=f"Comma-separated event kinds to wake on (default: all: {','.join(EVENT_KINDS)}).",
    )
    parser.add_argument(
        "--actionable-only",
        action="store_true",
        help="Update state and the A2UI pane in place on passive events; exit 0 only on actionable events.",
    )
    parser.add_argument(
        "--until",
        choices=("merged", "closed", "all-green"),
        help="Terminal condition that causes exit code 3 (default: closed).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        help="Maximum seconds to watch before exiting with code 2 when nothing changes.",
    )
    parser.add_argument(
        "--debounce",
        type=int,
        default=20,
        help="Fixed window in seconds to batch additional changes after the first event (0 disables).",
    )
    parser.add_argument(
        "--poll",
        type=int,
        default=120,
        help=(
            "Targeted refresh interval in seconds (default: 120). Refetches only flagged PRs "
            "(notifications, pending CI, pushed SHAs) and makes no call when none are flagged. "
            "With --no-notifications, every --poll step is a full refresh."
        ),
    )
    parser.add_argument(
        "--sweep",
        type=int,
        default=DEFAULT_SWEEP_S,
        help=f"Full refresh interval of every watched PR in seconds (default: {DEFAULT_SWEEP_S}).",
    )
    parser.add_argument(
        "--notify-poll",
        type=int,
        default=DEFAULT_POLL_INTERVAL_S,
        help="Notifications poll interval in seconds (default: 60).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Fetch, diff against state, emit JSON once, and exit immediately.",
    )
    parser.add_argument(
        "--a2ui",
        action="store_true",
        help="Enable A2UI board file and store updates (on by default; use --no-a2ui to disable).",
    )
    parser.add_argument(
        "--no-a2ui",
        action="store_true",
        help="Disable writing board.a2ui and updating the A2UI Pane conversation store.",
    )
    parser.add_argument(
        "--a2ui-file",
        help="Path to write the stack_board A2UI Express DSL file (default: <state-dir>/board.a2ui).",
    )
    parser.add_argument(
        "--conversation-id",
        help="Target Jetski conversation ID for direct A2UI Pane JSONL store updates.",
    )
    parser.add_argument(
        "--a2ui-store-dir",
        help="Override the a2ui-pane plugin data directory.",
    )
    parser.add_argument("--log", help="Append each emitted JSON payload to this JSONL file.")
    parser.add_argument("--output", help="Also write the emitted JSON payload to this file.")
    parser.add_argument(
        "--pushed-sha",
        action="append",
        default=[],
        metavar="PR=SHA",
        help="Record a head SHA pushed by the agent so self-triggered head_push is suppressed.",
    )
    parser.add_argument(
        "--mark-rerun",
        action="append",
        default=[],
        metavar="PR=CHECK@SHA",
        help="Record a flaky CI rerun performed by the agent.",
    )
    parser.add_argument(
        "--no-notifications",
        action="store_true",
        help="Disable /notifications polling and use GraphQL polling only.",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run continuously as a daemon, notifying the conversation on change without exiting.",
    )
    parser.add_argument(
        "--control",
        help="Path to control instructions JSONL file (default: <state-dir>/control.jsonl).",
    )
    parser.add_argument(
        "--reminder",
        type=int,
        default=1200,
        help="Seconds before sending a reminder for unacknowledged batches (default: 1200).",
    )
    parser.add_argument(
        "--notify-scope",
        choices=("mine", "all"),
        default="mine",
        help="Which PRs can wake the agent in daemon mode (default: mine).",
    )
    parser.add_argument(
        "--source",
        choices=("auto", "packrat", "github"),
        default="auto",
        help=(
            "Where PR data comes from: packrat (a running `packrat serve`), github (direct "
            "gh polling), or auto (packrat when its heartbeat is fresh, else github)."
        ),
    )
    parser.add_argument(
        "--packrat-db",
        help="packrat database path (default: ~/.local/share/packrat/repos.sqlite).",
    )
    args = parser.parse_args(argv)
    if args.daemon and args.once:
        parser.error("cannot use --daemon and --once together")
    if args.daemon and not args.conversation_id:
        parser.error("--daemon requires --conversation-id")
    return args


def _parse_kv(item: str, default_repo: str | None) -> tuple[str, str]:
    if "=" not in item:
        raise ValueError(f"Expected PR=VALUE format, got {item!r}")
    pr_part, val = item.split("=", 1)
    repo, number = parse_pr_ref(pr_part, default_repo)
    return pr_key(repo, number), val.strip()


def apply_override_items(
    state: MonitorState,
    pushed: list[Any],
    reruns: list[str],
    default_repo: str | None,
) -> None:
    """Applies pushed SHA overrides and flaky check rerun markers to state."""
    for raw in pushed:
        if isinstance(raw, dict):
            for k, v in raw.items():
                repo, num = parse_pr_ref(str(k), default_repo)
                state.pushed_shas[pr_key(repo, num)] = str(v).strip()
        elif isinstance(raw, str):
            key, sha = _parse_kv(raw, default_repo)
            state.pushed_shas[key] = sha
    for raw in reruns:
        if isinstance(raw, str):
            key, token = _parse_kv(raw, default_repo)
            existing = state.reruns.setdefault(key, [])
            if token not in existing:
                existing.append(token)


def apply_cli_overrides(args: argparse.Namespace, state: MonitorState) -> None:
    """Applies ``--pushed-sha`` and ``--mark-rerun`` flags to ``state`` before refreshing."""
    apply_override_items(state, args.pushed_sha or [], args.mark_rerun or [], args.repo)


def _list_prs_by_branch(
    runner: GhRunner, repo: str, flag: str, branch: str
) -> list[dict[str, Any]]:
    code, stdout, _ = runner(
        [
            "pr",
            "list",
            "--repo",
            repo,
            flag,
            branch,
            "--state",
            "all",
            "--limit",
            "5",
            "--json",
            "number,headRefName,baseRefName,state",
        ]
    )
    if code != 0 or not stdout.strip():
        return []
    try:
        items = json.loads(stdout)
    except json.JSONDecodeError:
        return []
    return [it for it in items if isinstance(it, dict) and isinstance(it.get("number"), int)]


def _walk_parent_prs(runner: GhRunner, repo: str, base_ref: str) -> list[str]:
    parent_chain: list[str] = []
    cur_base = base_ref
    depth = 0
    while cur_base and cur_base not in BASE_BRANCHES and depth < 10:
        depth += 1
        matches = _list_prs_by_branch(runner, repo, "--head", cur_base)
        if not matches:
            break
        p_key = pr_key(repo, int(matches[0]["number"]))
        if p_key in parent_chain:
            break
        parent_chain.append(p_key)
        cur_base = str(matches[0].get("baseRefName") or "")
    return list(reversed(parent_chain))


def _gh_stack(runner: GhRunner, seeds: list[str], add_key: Callable[[str], None]) -> None:
    """Expands ``seeds`` with ``gh pr list --head/--base`` walks."""
    _, raw_map = fetch_prs(runner, seeds)
    for key in seeds:
        repo, _ = parse_pr_ref(key)
        node = raw_map.get(key) or {}
        for p_key in _walk_parent_prs(runner, repo, str(node.get("baseRefName") or "")):
            add_key(p_key)
        add_key(key)
        head_ref = str(node.get("headRefName") or "")
        if head_ref:
            for child in _list_prs_by_branch(runner, repo, "--base", head_ref):
                add_key(pr_key(repo, int(child["number"])))


def _expand_stack(
    runner: GhRunner, seeds: list[str], source: PackratSource | None = None
) -> list[str]:
    """Adds each seed's stacked parents and children, in stack order.

    Seeds in repositories that packrat fully mirrors use ``source.stack_neighbors()``
    and make no ``gh`` calls. The others (``items`` repositories, or no packrat)
    use the ``gh pr list`` walk.
    """
    ordered: list[str] = []
    seen: set[str] = set()

    def add_key(key: str) -> None:
        if key not in seen and len(ordered) < MAX_WATCHED_PRS:
            seen.add(key)
            ordered.append(key)

    gh_seeds: list[str] = []
    for key in seeds:
        stack = source.stack_neighbors(key) if source is not None else None
        if stack is None:
            gh_seeds.append(key)
            continue
        for member in stack:
            add_key(member)
    if gh_seeds:
        _gh_stack(runner, gh_seeds, add_key)
    return ordered


def resolve_targets(
    args: argparse.Namespace,
    runner: GhRunner,
    state: MonitorState,
    source: PackratSource | None = None,
) -> list[str]:
    """Resolves CLI PR references (or existing state keys under ``--once``) into canonical keys."""
    seen: set[str] = set()
    keys: list[str] = []
    for raw in args.prs or []:
        repo, number = parse_pr_ref(raw, args.repo)
        key = pr_key(repo, number)
        if key not in seen:
            seen.add(key)
            keys.append(key)
    if not keys and args.once and state.snapshots:
        keys = list(state.snapshots.keys())
    if not keys:
        raise ValueError("At least one PR target is required on the command line.")
    if args.stack:
        keys = _expand_stack(runner, keys, source)
    return keys[:MAX_WATCHED_PRS]


def _consumer_id(args: argparse.Namespace, state_path: Path) -> str:
    """Returns a stable lease id: a hash of the conversation id, else the state directory."""
    basis = getattr(args, "conversation_id", None) or str(state_path.resolve().parent)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _select_wake_events(events: list[Event], actionable_only: bool) -> list[Event]:
    if not actionable_only:
        return list(events)
    return [ev for ev in events if ev.actionable]


def _with_subtitle_note(dm: dict[str, Any], dsl: str, note: str) -> tuple[dict[str, Any], str]:
    """Appends ``note`` to the board subtitle in both the data model and the DSL text."""
    old = str(dm.get("subtitle") or "")
    new = f"{old} · {note}" if old else note
    dsl = dsl.replace(f"$/subtitle = {json.dumps(old)}", f"$/subtitle = {json.dumps(new)}", 1)
    return {**dm, "subtitle": new}, dsl


def _carry_over_unmirrored(snap: PrSnapshot, prev: PrSnapshot | None) -> None:
    """Keeps fields packrat doesn't store from the previous snapshot.

    packrat has no assignees and no head-commit committer. Assignees keep their
    last known value, and the committer is kept while the head SHA is unchanged.
    """
    if prev is None:
        return
    snap.assignees = list(prev.assignees)
    if prev.head_sha == snap.head_sha:
        snap.last_committer = prev.last_committer


class Monitor:
    """Drives the hybrid notifications + GraphQL polling loop for a set of PRs."""

    def __init__(
        self,
        runner: GhRunner,
        state_path: str | os.PathLike[str],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], str] = utcnow_iso,
        epoch_now: Callable[[], float] = time.time,
        notifier: Notifier | None = None,
        packrat_runner: PackratRunner | None = None,
    ) -> None:
        self.runner = runner
        self.packrat_runner = packrat_runner
        self.packrat: PackratSource | None = None
        """Set while reading from ``packrat serve``; None in direct GitHub mode."""
        self.source_mode = "github"
        self._source_note: str | None = None
        """Board subtitle suffix naming the data source, or None for plain GitHub mode."""
        self._packrat_seq = 0
        self._next_renew = 0.0
        self._force_gh: set[str] = set()
        self._arm_meta: dict[str, Any] = {}
        self.state_path = Path(state_path)
        self.clock = clock
        self.sleep = sleep
        self.now = now
        self.epoch_now = epoch_now
        self.notifier = notifier
        self.state = MonitorState.empty()
        self.watched_keys: list[str] = []
        self.viewer: str = ""
        self.backoff = RateLimitBackoff()
        self.notifications_enabled = True
        self._warned_truncated: set[str] = set()
        self._interrupted_code: int | None = None
        self.liveness: Liveness | None = None
        self.control_reader: ControlReader | None = None
        self.gate: AckGate = AckGate()
        self._send_backoff: float = 60.0
        self._next_send_retry: float = 0.0
        self._last_batch_id: int | None = None
        self._pushed_keys: set[str] = set()
        """Keys named in a ``control.jsonl`` ``pushed_sha`` since their last refresh."""

    def _warn_truncation(self, pr: str, fields: list[str]) -> None:
        if not fields or pr in self._warned_truncated:
            return
        self._warned_truncated.add(pr)
        sys.stderr.write(
            f"warning: {pr} has paginated connections exceeding page size: {', '.join(fields)}\n"
        )

    def refresh(self, keys: list[str] | None = None) -> list[Event]:
        """Fetches ``keys`` (or all watched PRs), updates ``self.state``, and returns new events.

        In packrat mode the PR data comes from packrat's views; keys packrat doesn't
        have (or marked for ``gh`` after a slow startup) are fetched with GraphQL.
        """
        target_keys = list(keys) if keys else list(self.watched_keys)
        viewer, raw_map, from_packrat = self._fetch_raw(target_keys)
        if viewer:
            self.viewer = viewer
        prev_snapshots = dict(self.state.snapshots)
        for key in target_keys:
            repo, _ = parse_pr_ref(key)
            snap = normalize_pr(raw_map[key], viewer=self.viewer, repo=repo)
            if key in from_packrat:
                _carry_over_unmirrored(snap, prev_snapshots.get(key))
            self._warn_truncation(key, snap.truncated)
            self.state.snapshots[key] = snap

        resolve_stack_parents(self.state.snapshots)
        ctx = DiffContext(
            viewer=self.viewer,
            pushed_shas=self.state.pushed_shas,
            reported_threads=self.state.reported_threads,
            reruns=self.state.reruns,
        )
        events: list[Event] = []
        for key in target_keys:
            prev = prev_snapshots.get(key)
            cur = self.state.snapshots[key]
            events.extend(diff_snapshots(prev, cur, ctx))
            record_completed_durations(self.state, prev, cur)

        terminal_keys = {
            k for k, s in self.state.snapshots.items() if s.state in ("MERGED", "CLOSED")
        }
        self.state.prune(terminal_keys)
        self._pushed_keys.difference_update(target_keys)
        self.backoff.reset()
        return events

    def _fetch_raw(self, keys: list[str]) -> tuple[str, dict[str, dict[str, Any]], set[str]]:
        """Returns ``(viewer, raw_by_key, keys_served_by_packrat)`` for ``keys``."""
        if self.packrat is None:
            viewer, raw_map = fetch_prs(self.runner, keys)
            return viewer, raw_map, set()
        raw_map = self.packrat.snapshots(keys)
        gh_keys = [k for k in keys if k not in raw_map or k in self._force_gh]
        for key in gh_keys:
            raw_map.pop(key, None)
        served = set(raw_map)
        viewer = ""
        if gh_keys:
            viewer, extra = fetch_prs(self.runner, gh_keys)
            raw_map.update(extra)
            self._force_gh.difference_update(gh_keys)
        return viewer, raw_map, served

    def _compute_etas(self) -> dict[str, int | None]:
        now_ep = self.epoch_now()
        return {
            key: eta_seconds(self.state, snap, now_epoch=now_ep)
            for key, snap in self.state.snapshots.items()
        }

    def _ordered_snapshots(self) -> dict[str, Any]:
        return {k: self.state.snapshots[k] for k in self.watched_keys if k in self.state.snapshots}

    def _sync_board(
        self,
        events: list[Event],
        args: argparse.Namespace,
        observed_at: str | None = None,
    ) -> dict[str, Any] | None:
        if getattr(args, "no_a2ui", False):
            return None
        ts = observed_at or self.now()
        etas = self._compute_etas()
        ordered_snaps = self._ordered_snapshots()
        dm = board_data_model(ordered_snaps, events, etas, observed_at=ts)
        dsl = board_dsl(ordered_snaps, events, etas, observed_at=ts)
        if self._source_note:
            dm, dsl = _with_subtitle_note(dm, dsl, self._source_note)
        board_path = (
            Path(args.a2ui_file)
            if getattr(args, "a2ui_file", None)
            else (self.state_path.parent / "board.a2ui")
        )
        write_board_file(board_path, dsl)
        store_updated = sync_pane_store(
            getattr(args, "conversation_id", None),
            dm,
            store_dir=getattr(args, "a2ui_store_dir", None),
            surface_id="monitor",
            observed_at=ts,
        )
        return {
            "template": "stack_board",
            "surfaceId": "monitor",
            "dsl_file": str(board_path),
            "store_updated": store_updated,
        }

    def _build_payload(
        self,
        exit_reason: str,
        exit_code: int,
        events: list[Event],
        args: argparse.Namespace,
        batch_id: int | None = None,
        notify_mode: str | None = None,
    ) -> dict[str, Any]:
        observed_at = self.now()
        etas = self._compute_etas()
        ordered_snaps = self._ordered_snapshots()
        prs_dict: dict[str, Any] = {}
        for key, snap in ordered_snaps.items():
            entry = snap.to_dict()
            entry["eta_s"] = etas.get(key)
            prs_dict[key] = entry
        effective_mode = notify_mode or ("agentapi" if self.notifier else "exit")
        payload: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "exit_reason": exit_reason,
            "exit_code": exit_code,
            "observed_at": observed_at,
            "summary": summarize(events),
            "prs": prs_dict,
            "events": [ev.to_dict() for ev in events],
            "eta": etas,
            "notify_mode": effective_mode,
            "notifier": list(self.notifier.argv_prefix) if self.notifier else None,
            "source": self.source_mode,
        }
        if batch_id is not None:
            payload["batch_id"] = batch_id
        a2ui_meta = self._sync_board(events, args, observed_at=observed_at)
        if a2ui_meta is not None:
            payload["a2ui"] = a2ui_meta
        return payload

    def _record_threads_after_emit(self) -> None:
        for key, snap in self.state.snapshots.items():
            if snap.state in ("MERGED", "CLOSED"):
                continue
            current_marks = snapshot_thread_watermarks(snap, viewer=self.viewer)
            if current_marks:
                merged = dict(self.state.reported_threads.get(key) or {})
                merged.update(current_marks)
                self.state.reported_threads[key] = merged

    def emit(self, payload: dict[str, Any], args: argparse.Namespace) -> None:
        """Writes ``payload`` to stdout, optional ``--output`` and ``--log``, then saves state."""
        serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        sys.stdout.write(serialized)
        sys.stdout.flush()
        if args.output:
            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(serialized, encoding="utf-8")
        if args.log:
            append_event_log(args.log, payload)
        self._record_threads_after_emit()
        save_state(self.state_path, self.state)

    def _open_keys_where(self, predicate: Callable[[Any], bool]) -> set[str]:
        watched = set(self.watched_keys)
        return {
            key
            for key, snap in self.state.snapshots.items()
            if key in watched and snap.state == "OPEN" and predicate(snap)
        }

    def _unknown_mergeable_keys(self) -> set[str]:
        return self._open_keys_where(
            lambda s: s.mergeable == "UNKNOWN" or s.merge_state == "UNKNOWN"
        )

    def _pending_rollup_keys(self) -> set[str]:
        return self._open_keys_where(lambda s: s.rollup_state in ("PENDING", "EXPECTED"))

    def _fast_keys(self, accumulated: list[Event]) -> set[str]:
        """Returns keys refreshed at ``FAST_POLL_INTERVAL_S`` cadence instead of ``--poll``.

        These are open PRs whose mergeability GitHub is still computing, PRs with events
        in the pending debounce batch, and PRs named in a ``pushed_sha`` control command.
        """
        watched = set(self.watched_keys)
        debounce_keys = {ev.pr for ev in accumulated}
        return self._unknown_mergeable_keys() | ((debounce_keys | self._pushed_keys) & watched)

    def _poll_notifications(self, default_interval: int) -> tuple[set[str], int]:
        if not self.notifications_enabled:
            return set(), default_interval
        try:
            res = poll(
                self.runner,
                since=self.state.notifications_last_modified,
                watched=set(self.watched_keys),
            )
            self.state.notifications_last_modified = res.last_modified
            return res.changed_keys, max(default_interval, res.poll_interval_s)
        except NotificationsUnavailable as exc:
            sys.stderr.write(
                f"warning: disabling /notifications polling ({exc}); using GraphQL polling only\n"
            )
            self.notifications_enabled = False
            return set(), default_interval

    def _handle_refresh_error(self, exc: GhError) -> float:
        delay = self.backoff.next_delay(
            status=exc.status, headers=exc.headers, now_epoch=self.epoch_now()
        )
        sys.stderr.write(
            f"warning: GitHub refresh failed ({exc}); strike {self.backoff.failures}/"
            f"{MAX_CONSECUTIVE_ERRORS}, retrying in {delay}s\n"
        )
        if self.backoff.failures >= MAX_CONSECUTIVE_ERRORS:
            save_state(self.state_path, self.state)
            raise exc
        return float(delay)

    def _install_signal_handlers(self) -> None:
        def _handler(signum: int, _frame: Any) -> None:
            self._interrupted_code = 130 if signum == signal.SIGINT else 143

        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)

    def _next_sleep_duration(
        self,
        now: float,
        timers: _Timers,
        debounce_deadline: float | None,
        timeout_deadline: float | None,
        accumulated: list[Event],
    ) -> float:
        candidates = [timers.next_poll - now, timers.next_sweep - now]
        if self.notifications_enabled:
            candidates.append(timers.next_notify - now)
        if debounce_deadline is not None:
            candidates.append(min(FAST_POLL_INTERVAL_S, max(0.0, debounce_deadline - now)))
        if self._fast_keys(accumulated):
            candidates.append(FAST_POLL_INTERVAL_S)
        if timeout_deadline is not None:
            candidates.append(max(0.0, timeout_deadline - now))
        return max(0.0, min(candidates))

    def _finish_with(
        self, reason: str, code: int, events: list[Event], args: argparse.Namespace
    ) -> int:
        payload = self._build_payload(reason, code, events, args)
        self.emit(payload, args)
        return code

    def _filter_wake_events_for_scope(self, events: list[Event], scope: str) -> list[Event]:
        if scope != "mine":
            return list(events)
        filtered: list[Event] = []
        for ev in events:
            snap = self.state.snapshots.get(ev.pr)
            if (snap and snap.mine) or ev.kind == "terminal":
                filtered.append(ev)
        return filtered

    def _process_control(self, default_repo: str | None) -> bool:
        if self.control_reader is None:
            return False
        cmds = self.control_reader.read_new()
        for ack_id in cmds.acks:
            self.gate.ack(ack_id)
        pushed: list[Any] = []
        for override in cmds.overrides:
            if "pushed_sha" in override:
                pushed.append(override["pushed_sha"])
        before = dict(self.state.pushed_shas)
        apply_override_items(self.state, pushed, cmds.mark_reruns, default_repo)
        newly_pushed = {
            key for key, sha in self.state.pushed_shas.items() if before.get(key) != sha
        }
        self._pushed_keys.update(newly_pushed)
        self._request_packrat_refresh(newly_pushed & set(self.watched_keys))
        self.state.daemon["gate"] = self.gate.to_dict()
        self.state.daemon["control_offset"] = self.control_reader.offset
        return cmds.stop

    def _request_packrat_refresh(self, keys: set[str]) -> None:
        """In packrat mode, asks the service to refetch ``keys`` now (``packrat refresh``)."""
        if self.packrat is None or not keys:
            return
        try:
            self.packrat.refresh(keys)
        except PackratError as exc:
            sys.stderr.write(f"warning: packrat refresh failed: {exc}\n")

    def _dispatch_daemon_batch(
        self,
        now: float,
        accumulated: list[Event],
        args: argparse.Namespace,
    ) -> bool:
        if not self.gate.can_send() or now < self._next_send_retry:
            return False
        events_to_wake = self._filter_wake_events_for_scope(
            accumulated, getattr(args, "notify_scope", "mine")
        )
        if not events_to_wake:
            self._sync_board(accumulated, args)
            save_state(self.state_path, self.state)
            accumulated.clear()
            return True

        batch_id = self.gate.mark_sent(now)
        self._last_batch_id = batch_id
        payload = self._build_payload(
            "change", 0, accumulated, args, batch_id=batch_id, notify_mode="agentapi"
        )
        self.emit(payload, args)

        summary = summarize(events_to_wake)
        last_json = (
            Path(args.output)
            if getattr(args, "output", None)
            else (self.state_path.parent / "last.json")
        )
        control_path = (
            self.control_reader.path
            if self.control_reader
            else (self.state_path.parent / CONTROL_FILE)
        )
        title, body = format_batch_message(
            skill="PR Monitor",
            batch_id=batch_id,
            summary=summary,
            events=[e.to_dict() for e in events_to_wake],
            last_json=last_json,
            control=control_path,
        )

        sent = False
        if self.notifier and getattr(args, "conversation_id", None):
            sent = self.notifier.send(args.conversation_id, title, body)
        if sent:
            self._send_backoff = 60.0
            self._next_send_retry = 0.0
            accumulated.clear()
            self.state.daemon["gate"] = self.gate.to_dict()
            save_state(self.state_path, self.state)
            return True
        else:
            self._next_send_retry = now + self._send_backoff
            self._send_backoff = min(600.0, self._send_backoff * 2)
            self.gate.awaiting = None
            return False

    def _check_reminder(self, now: float, reminder_s: float, conversation_id: str | None) -> None:
        if not self.gate.reminder_due(now, reminder_s) or not self.notifier or not conversation_id:
            return
        title = f"[PR Monitor] Reminder: Batch {self.gate.awaiting} awaiting acknowledgment"
        body = (
            f"Batch {self.gate.awaiting} has unacknowledged events. "
            f'Append `{{"ack": {self.gate.awaiting}}}` to control.jsonl.'
        )
        self.notifier.send(conversation_id, title, body)
        self.gate.record_reminder(now)
        self.state.daemon["gate"] = self.gate.to_dict()
        save_state(self.state_path, self.state)

    def _check_exit_conditions(
        self,
        now: float,
        accumulated: list[Event],
        debounce_deadline: float | None,
        timeout_deadline: float | None,
        args: argparse.Namespace,
    ) -> int | None:
        if self._interrupted_code is not None:
            save_state(self.state_path, self.state)
            return self._interrupted_code
        if is_terminal(self.state.snapshots, args.until):
            return self._finish_with("terminal", 3, accumulated, args)
        if timeout_deadline is not None and now >= timeout_deadline:
            reason = "change" if accumulated else "timeout"
            code = 0 if accumulated else 2
            return self._finish_with(reason, code, accumulated, args)
        if getattr(args, "daemon", False):
            if debounce_deadline is not None and now >= debounce_deadline:
                self._dispatch_daemon_batch(now, accumulated, args)
            self._check_reminder(now, float(args.reminder), getattr(args, "conversation_id", None))
            return None
        if debounce_deadline is not None and now >= debounce_deadline:
            return self._finish_with("change", 0, accumulated, args)
        return None

    def _select_refresh_targets(
        self,
        now: float,
        timers: _Timers,
        accumulated: list[Event],
        cfg: _LoopConfig,
    ) -> list[str] | None:
        """Picks what the current step refetches and advances the poll and notify timers.

        Returns ``None`` for a full refresh of every watched PR (the ``--sweep`` is due,
        or ``--poll`` is due with notifications disabled). Otherwise returns the sorted
        flagged keys, which is empty when nothing needs a GraphQL call.
        """
        if now < timers.backoff_until:
            return []
        poll_due = now >= timers.next_poll
        if now >= timers.next_sweep or (poll_due and not self.notifications_enabled):
            return None
        flagged = self._fast_keys(accumulated)
        if self.notifications_enabled and now >= timers.next_notify:
            changed, interval = self._poll_notifications(cfg.notify_s)
            timers.next_notify = now + interval
            flagged |= changed
        if poll_due:
            flagged |= self._pending_rollup_keys()
            timers.next_poll = now + cfg.poll_s
        return sorted(flagged)

    def _post_sleep_exit(
        self,
        now: float,
        accumulated: list[Event],
        debounce_deadline: float | None,
        timeout_deadline: float | None,
        args: argparse.Namespace,
    ) -> int | None:
        if self._interrupted_code is not None:
            save_state(self.state_path, self.state)
            return self._interrupted_code
        if timeout_deadline is not None and now >= timeout_deadline:
            return self._check_exit_conditions(
                now, accumulated, debounce_deadline, timeout_deadline, args
            )
        return None

    def _run_refresh_step(
        self,
        now: float,
        timers: _Timers,
        debounce_deadline: float | None,
        cfg: _LoopConfig,
        accumulated: list[Event],
        args: argparse.Namespace,
    ) -> float | None:
        targets = self._select_refresh_targets(now, timers, accumulated, cfg)
        if targets is not None and not targets:
            return debounce_deadline
        raw_events = self.refresh(targets)
        if targets is None:
            timers.next_sweep = now + cfg.sweep_s
            timers.next_poll = now + cfg.poll_s
        return self._absorb_events(now, raw_events, debounce_deadline, cfg, accumulated, args)

    def _absorb_events(
        self,
        now: float,
        raw_events: list[Event],
        debounce_deadline: float | None,
        cfg: _LoopConfig,
        accumulated: list[Event],
        args: argparse.Namespace,
    ) -> float | None:
        """Queues wake events (starting the debounce window) or updates the board in place."""
        matched = filter_events(raw_events, cfg.allowed_kinds)
        wake_events = _select_wake_events(matched, bool(args.actionable_only))
        if wake_events:
            accumulated.extend(wake_events)
            if debounce_deadline is None:
                debounce_deadline = now + cfg.debounce_s
        else:
            self._sync_board(matched, args)
            save_state(self.state_path, self.state)
        return debounce_deadline

    def _apply_refresh_backoff(self, now: float, timers: _Timers, exc: GhError) -> None:
        delay = self._handle_refresh_error(exc)
        timers.backoff_until = now + delay
        timers.next_poll = now + delay
        timers.next_notify = now + delay
        if timers.next_sweep <= now:
            timers.next_sweep = now + delay

    def _update_debounce_deadline(
        self,
        accumulated: list[Event],
        debounce_deadline: float | None,
        now: float,
        poll_s: int,
        is_daemon: bool,
    ) -> float | None:
        if not accumulated:
            return None
        if is_daemon and debounce_deadline is not None and now >= debounce_deadline:
            return now + poll_s
        return debounce_deadline

    def _loop(
        self,
        args: argparse.Namespace,
        allowed_kinds: set[str] | None,
        initial_events: list[Event],
    ) -> int:
        accumulated = list(initial_events)
        start = self.clock()
        cfg = _LoopConfig(
            poll_s=max(10, int(args.poll)),
            sweep_s=max(10, int(getattr(args, "sweep", DEFAULT_SWEEP_S))),
            notify_s=max(10, int(args.notify_poll)),
            debounce_s=max(0, int(args.debounce)),
            allowed_kinds=allowed_kinds,
        )
        timeout_deadline = start + int(args.timeout) if args.timeout is not None else None
        debounce_deadline = start + cfg.debounce_s if accumulated else None
        timers = _Timers(
            next_poll=start + cfg.poll_s,
            next_sweep=start + cfg.sweep_s,
            next_notify=start + cfg.notify_s,
        )

        while True:
            now = self.clock()
            exit_code = self._check_exit_conditions(
                now, accumulated, debounce_deadline, timeout_deadline, args
            )
            if exit_code is not None:
                return exit_code

            stop_exit = self._loop_heartbeat_and_control(accumulated, args)
            if stop_exit is not None:
                return stop_exit
            debounce_deadline = self._update_debounce_deadline(
                accumulated, debounce_deadline, now, cfg.poll_s, getattr(args, "daemon", False)
            )
            exit_code, debounce_deadline = self._wait_and_refresh(
                now, timers, debounce_deadline, timeout_deadline, cfg, accumulated, args
            )
            if exit_code is not None:
                return exit_code

    def _wait_and_refresh(
        self,
        now: float,
        timers: _Timers,
        debounce_deadline: float | None,
        timeout_deadline: float | None,
        cfg: _LoopConfig,
        accumulated: list[Event],
        args: argparse.Namespace,
    ) -> tuple[int | None, float | None]:
        """Waits for the next step (sleep, or the packrat change feed), then refreshes.

        Returns ``(exit_code, debounce_deadline)``; ``exit_code`` is set when the
        process must exit after waking (signal or ``--timeout``).
        """
        batch: ChangeBatch | None = None
        if self.packrat is not None:
            wait_s = self._packrat_wait_s(now, debounce_deadline, timeout_deadline, cfg)
            batch = self._packrat_wait(now, wait_s, timers)
        else:
            self.sleep(
                self._next_sleep_duration(
                    now, timers, debounce_deadline, timeout_deadline, accumulated
                )
            )
        now = self.clock()
        exit_code = self._post_sleep_exit(
            now, accumulated, debounce_deadline, timeout_deadline, args
        )
        if exit_code is not None:
            return exit_code, debounce_deadline
        try:
            if batch is not None:
                raw_events = self._refresh_from_batch(batch)
                return None, self._absorb_events(
                    now, raw_events, debounce_deadline, cfg, accumulated, args
                )
            if self.packrat is None:
                return None, self._run_refresh_step(
                    now, timers, debounce_deadline, cfg, accumulated, args
                )
        except PackratError as exc:
            self._fallback_to_github(now, timers, str(exc))
        except GhError as exc:
            self._apply_refresh_backoff(now, timers, exc)
        return None, debounce_deadline

    # ----- packrat mode ---------------------------------------------------------------

    def _packrat_wait_s(
        self,
        now: float,
        debounce_deadline: float | None,
        timeout_deadline: float | None,
        cfg: _LoopConfig,
    ) -> float:
        """Returns the ``packrat changes --wait`` timeout: the debounce window, capped."""
        wait = float(cfg.debounce_s or PACKRAT_DEFAULT_WAIT_S)
        for deadline in (debounce_deadline, timeout_deadline):
            if deadline is not None:
                wait = min(wait, max(0.0, deadline - now))
        return max(1.0, wait)

    def _packrat_wait(self, now: float, wait_s: float, timers: _Timers) -> ChangeBatch | None:
        """Checks the heartbeat, renews the lease when due, and waits on the change feed.

        Returns None (after switching to GitHub mode) when packrat is unavailable.
        """
        source = self.packrat
        assert source is not None
        try:
            if not available(source.db_path):
                raise PackratError("packrat serve heartbeat is stale or missing")
            if now >= self._next_renew:
                source.renew()
                self._next_renew = now + PACKRAT_RENEW_S
            batch = source.wait_changes(self._packrat_seq, wait_s)
        except PackratError as exc:
            self._fallback_to_github(now, timers, str(exc))
            return None
        self._packrat_seq = batch.seq
        return batch

    def _refresh_from_batch(self, batch: ChangeBatch) -> list[Event]:
        """Re-reads the PRs named in ``batch`` (every watched PR after a feed reset)."""
        if batch.reset:
            return self.refresh(None)
        keys = [k for k in self.watched_keys if k in batch.keys]
        return self.refresh(keys) if keys else []

    def _fallback_to_github(self, now: float, timers: _Timers, reason: str) -> None:
        """Switches to direct GitHub polling for the rest of the run, logging it once."""
        if self.packrat is None:
            return
        sys.stderr.write(
            f"warning: packrat unavailable ({reason}); falling back to direct GitHub polling\n"
        )
        self.packrat = None
        self.source_mode = "github"
        self._source_note = "via GitHub (packrat unavailable)"
        self._force_gh.clear()
        timers.next_sweep = now
        timers.next_poll = now
        timers.next_notify = now
        if self.liveness is not None and self._arm_meta:
            self._arm_meta["source"] = "github"
            self.liveness.arm(self._arm_meta)

    def _select_source(self, args: argparse.Namespace) -> PackratSource | None:
        """Picks packrat or GitHub per ``--source``; raises ValueError if packrat is required."""
        mode = getattr(args, "source", "auto") or "auto"
        if mode == "github":
            return None
        db_path = (
            Path(getattr(args, "packrat_db", None) or packrat_source.DEFAULT_PACKRAT_DB)
            .expanduser()
            .resolve()
        )
        if available(db_path):
            return PackratSource(
                db_path,
                _consumer_id(args, self.state_path),
                runner=self.packrat_runner,
            )
        if mode == "packrat":
            raise ValueError(
                f"--source packrat: packrat serve is not running or its heartbeat is stale "
                f"({db_path}). Start it with `packrat auto-sync on`, or use --source github."
            )
        return None

    def _start_packrat(self) -> None:
        """Leases the watched PRs and waits briefly for packrat to refresh missing ones.

        Keys still missing or stale after ``PACKRAT_STARTUP_WAIT_S`` are fetched with
        ``gh`` on the first refresh.
        """
        source = self.packrat
        assert source is not None
        keys = list(self.watched_keys)
        source.lease(keys)
        self._next_renew = self.clock() + PACKRAT_RENEW_S
        self._packrat_seq = source.wait_changes(_FEED_HEAD, 0).seq
        self.viewer = self.viewer or source.viewer_login() or gh_config_user()
        pending = self._stale_packrat_keys(keys, PACKRAT_STALE_ROW_S)
        if not pending:
            return
        source.refresh(pending)
        requested_at = self.clock()
        deadline = requested_at + PACKRAT_STARTUP_WAIT_S
        while pending and self.clock() < deadline:
            self._packrat_seq = source.wait_changes(self._packrat_seq, 2).seq
            pending = self._stale_packrat_keys(pending, self.clock() - requested_at)
        self._force_gh = set(pending)

    def _stale_packrat_keys(self, keys: list[str], max_age_s: float) -> list[str]:
        assert self.packrat is not None
        ages = self.packrat.fetched_ages(keys)
        return [k for k in keys if k not in ages or ages[k] > max_age_s]

    def _release_packrat(self) -> None:
        if self.packrat is None:
            return
        try:
            self.packrat.release()
        except PackratError as exc:
            sys.stderr.write(f"warning: packrat lease release failed: {exc}\n")

    def _loop_heartbeat_and_control(
        self, accumulated: list[Event], args: argparse.Namespace
    ) -> int | None:
        if self.liveness is not None:
            self.liveness.beat()
        if getattr(args, "daemon", False) and self._process_control(args.repo):
            return self._finish_with("stopped", 0, accumulated, args)
        return None

    def _init_run_state(self, args: argparse.Namespace) -> tuple[set[str] | None, list[Event]]:
        self._install_signal_handlers()
        if self.notifier is None:
            self.notifier = resolve_notifier()
        allowed_kinds = _parse_event_list(args.events)
        self.state = load_state(self.state_path)
        apply_cli_overrides(args, self.state)
        self.packrat = self._select_source(args)
        self.source_mode = "packrat" if self.packrat is not None else "github"
        self._source_note = "via packrat" if self.packrat is not None else None
        self.watched_keys = resolve_targets(args, self.runner, self.state, self.packrat)
        self.notifications_enabled = not bool(args.no_notifications)
        if self.packrat is not None:
            try:
                self._start_packrat()
                return allowed_kinds, self.refresh()
            except PackratError as exc:
                timers = _Timers(next_poll=0.0, next_sweep=0.0, next_notify=0.0)
                self._fallback_to_github(self.clock(), timers, str(exc))
        return allowed_kinds, self.refresh()

    def _setup_daemon_mode(self, args: argparse.Namespace) -> int | None:
        if not self.notifier:
            sys.stderr.write(
                "warning: agentapi notifier not found; falling back to exit-on-change mode\n"
            )
            args.daemon = False
            return None

        self.liveness = Liveness(self.state_path.parent)
        ok, existing_pid = self.liveness.acquire()
        if not ok:
            payload = {
                "schema": SCHEMA_VERSION,
                "exit_reason": "already_running",
                "exit_code": EXIT_ALREADY_RUNNING,
                "pid": existing_pid,
                "summary": f"Monitor daemon already running (PID {existing_pid})",
            }
            self.emit(payload, args)
            return EXIT_ALREADY_RUNNING

        self._arm_meta = {
            "argv": sys.argv,
            "cwd": os.getcwd(),
            "skill": "monitor",
            "poll_s": args.poll,
            "sweep_s": getattr(args, "sweep", DEFAULT_SWEEP_S),
            "conversation_id": args.conversation_id,
            "source": self.source_mode,
        }
        if self.packrat is not None:
            self._arm_meta["packrat_db"] = str(self.packrat.db_path)
        self.liveness.arm(self._arm_meta)
        control_path = (
            Path(args.control) if args.control else (self.state_path.parent / CONTROL_FILE)
        )
        self.control_reader = ControlReader(control_path)
        if "gate" in self.state.daemon:
            self.gate = AckGate.from_dict(self.state.daemon["gate"])
        if "control_offset" in self.state.daemon:
            self.control_reader.offset = int(self.state.daemon["control_offset"])
        return None

    def _handle_run_error(self, exc: GhError, args: argparse.Namespace) -> None:
        sys.stderr.write(f"error: {exc}\n")
        if (
            self.notifier
            and getattr(args, "conversation_id", None)
            and getattr(args, "daemon", False)
        ):
            self.notifier.send(
                args.conversation_id,
                "[PR Monitor] Error: GitHub API failure",
                f"Monitor daemon failed after {MAX_CONSECUTIVE_ERRORS} errors: {exc}",
            )

    def run(self, args: argparse.Namespace) -> int:
        """Executes ``monitor_prs.py`` from parsed arguments and returns its exit code."""
        try:
            allowed_kinds, raw_events = self._init_run_state(args)
        except (ValueError, StateError, GhError) as exc:
            sys.stderr.write(f"error: {exc}\n")
            return 1

        if getattr(args, "daemon", False):
            daemon_code = self._setup_daemon_mode(args)
            if daemon_code is not None:
                return daemon_code

        matched = filter_events(raw_events, allowed_kinds)
        if args.once:
            if is_terminal(self.state.snapshots, args.until):
                return self._finish_with("terminal", 3, matched, args)
            reason = "change" if matched else "snapshot"
            return self._finish_with(reason, 0, matched, args)

        wake_events = _select_wake_events(matched, bool(args.actionable_only))
        if not wake_events:
            self._sync_board(matched, args)
            save_state(self.state_path, self.state)
        try:
            return self._loop(args, allowed_kinds, wake_events)
        except GhError as exc:
            self._handle_run_error(exc, args)
            return 1
        finally:
            self._release_packrat()
            if self.liveness is not None:
                self.liveness.disarm()
                self.liveness.release()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = parse_args(argv)
    monitor = Monitor(runner=SubprocessGh(), state_path=args.state)
    return monitor.run(args)


if __name__ == "__main__":
    sys.exit(main())
