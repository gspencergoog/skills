#!/usr/bin/env python3
"""Continuously watches GitHub pull requests and exits with a JSON delta on change."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from a2ui_board import board_data_model, board_dsl, sync_pane_store, write_board_file
from diff_events import (
    DiffContext,
    diff_snapshots,
    filter_events,
    is_terminal,
    snapshot_thread_watermarks,
    summarize,
)
from gh_client import GhError, GhRunner, RateLimitBackoff, SubprocessGh, fetch_prs
from models import EVENT_KINDS, SCHEMA_VERSION, Event, MonitorState, parse_pr_ref, pr_key
from normalize import normalize_pr, resolve_stack_parents
from notifications import DEFAULT_POLL_INTERVAL_S, NotificationsUnavailable, poll
from state import (
    StateError,
    append_event_log,
    eta_seconds,
    load_state,
    record_completed_durations,
    save_state,
)

MAX_WATCHED_PRS = 20
MAX_CONSECUTIVE_ERRORS = 5
FAST_POLL_INTERVAL_S = 10
BASE_BRANCHES = frozenset({"main", "master", "develop"})


def utcnow_iso() -> str:
    """Returns current UTC timestamp in ISO-8601 ``YYYY-MM-DDTHH:MM:SSZ`` format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        help="Full GraphQL refresh interval in seconds (default: 120).",
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
    return parser.parse_args(argv)


def _parse_kv(item: str, default_repo: str | None) -> tuple[str, str]:
    if "=" not in item:
        raise ValueError(f"Expected PR=VALUE format, got {item!r}")
    pr_part, val = item.split("=", 1)
    repo, number = parse_pr_ref(pr_part, default_repo)
    return pr_key(repo, number), val.strip()


def apply_cli_overrides(args: argparse.Namespace, state: MonitorState) -> None:
    """Applies ``--pushed-sha`` and ``--mark-rerun`` flags to ``state`` before refreshing."""
    for raw in args.pushed_sha or []:
        key, sha = _parse_kv(raw, args.repo)
        state.pushed_shas[key] = sha
    for raw in args.mark_rerun or []:
        key, token = _parse_kv(raw, args.repo)
        existing = state.reruns.setdefault(key, [])
        if token not in existing:
            existing.append(token)


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


def _expand_stack(runner: GhRunner, seeds: list[str]) -> list[str]:
    _, raw_map = fetch_prs(runner, seeds)
    ordered: list[str] = []
    seen: set[str] = set()

    def add_key(key: str) -> None:
        if key not in seen and len(ordered) < MAX_WATCHED_PRS:
            seen.add(key)
            ordered.append(key)

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
    return ordered


def resolve_targets(args: argparse.Namespace, runner: GhRunner, state: MonitorState) -> list[str]:
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
        keys = _expand_stack(runner, keys)
    return keys[:MAX_WATCHED_PRS]


def _select_wake_events(events: list[Event], actionable_only: bool) -> list[Event]:
    if not actionable_only:
        return list(events)
    return [ev for ev in events if ev.actionable]


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
    ) -> None:
        self.runner = runner
        self.state_path = Path(state_path)
        self.clock = clock
        self.sleep = sleep
        self.now = now
        self.epoch_now = epoch_now
        self.state = MonitorState.empty()
        self.watched_keys: list[str] = []
        self.viewer: str = ""
        self.backoff = RateLimitBackoff()
        self.notifications_enabled = True
        self._warned_truncated: set[str] = set()
        self._interrupted_code: int | None = None

    def _warn_truncation(self, pr: str, fields: list[str]) -> None:
        if not fields or pr in self._warned_truncated:
            return
        self._warned_truncated.add(pr)
        sys.stderr.write(
            f"warning: {pr} has paginated connections exceeding page size: {', '.join(fields)}\n"
        )

    def refresh(self, keys: list[str] | None = None) -> list[Event]:
        """Fetches ``keys`` (or all watched PRs), updates ``self.state``, and returns new events."""
        target_keys = list(keys) if keys else list(self.watched_keys)
        viewer, raw_map = fetch_prs(self.runner, target_keys)
        if viewer:
            self.viewer = viewer
        prev_snapshots = dict(self.state.snapshots)
        for key in target_keys:
            repo, _ = parse_pr_ref(key)
            snap = normalize_pr(raw_map[key], viewer=self.viewer, repo=repo)
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
        self.backoff.reset()
        return events

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
    ) -> dict[str, Any]:
        observed_at = self.now()
        etas = self._compute_etas()
        ordered_snaps = self._ordered_snapshots()
        prs_dict: dict[str, Any] = {}
        for key, snap in ordered_snaps.items():
            entry = snap.to_dict()
            entry["eta_s"] = etas.get(key)
            prs_dict[key] = entry
        payload: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "exit_reason": exit_reason,
            "exit_code": exit_code,
            "observed_at": observed_at,
            "summary": summarize(events),
            "prs": prs_dict,
            "events": [ev.to_dict() for ev in events],
            "eta": etas,
        }
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

    def _has_unknown_mergeable(self) -> bool:
        return any(
            s.state == "OPEN" and (s.mergeable == "UNKNOWN" or s.merge_state == "UNKNOWN")
            for s in self.state.snapshots.values()
        )

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
        next_full: float,
        next_notify: float,
        debounce_deadline: float | None,
        timeout_deadline: float | None,
    ) -> float:
        candidates = [next_full - now]
        if self.notifications_enabled:
            candidates.append(next_notify - now)
        if debounce_deadline is not None:
            candidates.append(min(FAST_POLL_INTERVAL_S, max(0.0, debounce_deadline - now)))
        if self._has_unknown_mergeable():
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
        if debounce_deadline is not None and now >= debounce_deadline:
            return self._finish_with("change", 0, accumulated, args)
        if timeout_deadline is not None and now >= timeout_deadline:
            reason = "change" if accumulated else "timeout"
            code = 0 if accumulated else 2
            return self._finish_with(reason, code, accumulated, args)
        return None

    def _select_refresh_targets(
        self,
        now: float,
        next_full: float,
        next_notify: float,
        debounce_deadline: float | None,
        notify_s: int,
    ) -> tuple[bool, list[str] | None, float]:
        need_full = (
            now >= next_full or debounce_deadline is not None or self._has_unknown_mergeable()
        )
        if need_full or not self.notifications_enabled or now < next_notify:
            return need_full, None, next_notify
        changed, interval = self._poll_notifications(notify_s)
        return False, (sorted(changed) if changed else None), now + interval

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
        next_full: float,
        next_notify: float,
        debounce_deadline: float | None,
        poll_s: int,
        notify_s: int,
        allowed_kinds: set[str] | None,
        accumulated: list[Event],
        debounce_s: int,
        args: argparse.Namespace,
    ) -> tuple[float, float, float | None]:
        need_full, refresh_keys, next_notify = self._select_refresh_targets(
            now, next_full, next_notify, debounce_deadline, notify_s
        )
        if not (need_full or refresh_keys):
            return next_full, next_notify, debounce_deadline
        raw_events = self.refresh(None if need_full else refresh_keys)
        if need_full:
            next_full = now + poll_s
        matched = filter_events(raw_events, allowed_kinds)
        wake_events = _select_wake_events(matched, bool(args.actionable_only))
        if wake_events:
            accumulated.extend(wake_events)
            if debounce_deadline is None:
                debounce_deadline = now + debounce_s
        else:
            self._sync_board(matched, args)
            save_state(self.state_path, self.state)
        return next_full, next_notify, debounce_deadline

    def _loop(
        self,
        args: argparse.Namespace,
        allowed_kinds: set[str] | None,
        initial_events: list[Event],
    ) -> int:
        accumulated = list(initial_events)
        start = self.clock()
        debounce_s = max(0, int(args.debounce))
        poll_s = max(10, int(args.poll))
        notify_s = max(10, int(args.notify_poll))
        timeout_deadline = start + int(args.timeout) if args.timeout is not None else None
        debounce_deadline = start + debounce_s if accumulated else None
        next_full = start + poll_s
        next_notify = start + notify_s

        while True:
            now = self.clock()
            exit_code = self._check_exit_conditions(
                now, accumulated, debounce_deadline, timeout_deadline, args
            )
            if exit_code is not None:
                return exit_code

            wait_s = self._next_sleep_duration(
                now, next_full, next_notify, debounce_deadline, timeout_deadline
            )
            self.sleep(wait_s)
            now = self.clock()
            exit_code = self._post_sleep_exit(
                now, accumulated, debounce_deadline, timeout_deadline, args
            )
            if exit_code is not None:
                return exit_code

            try:
                next_full, next_notify, debounce_deadline = self._run_refresh_step(
                    now,
                    next_full,
                    next_notify,
                    debounce_deadline,
                    poll_s,
                    notify_s,
                    allowed_kinds,
                    accumulated,
                    debounce_s,
                    args,
                )
            except GhError as exc:
                delay = self._handle_refresh_error(exc)
                next_full = now + delay
                next_notify = now + delay

    def run(self, args: argparse.Namespace) -> int:
        """Executes ``monitor_prs.py`` from parsed arguments and returns its exit code."""
        self._install_signal_handlers()
        try:
            allowed_kinds = _parse_event_list(args.events)
            self.state = load_state(self.state_path)
            apply_cli_overrides(args, self.state)
            self.watched_keys = resolve_targets(args, self.runner, self.state)
            self.notifications_enabled = not bool(args.no_notifications)
            raw_events = self.refresh()
        except (ValueError, StateError, GhError) as exc:
            sys.stderr.write(f"error: {exc}\n")
            return 1

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
            sys.stderr.write(f"error: {exc}\n")
            return 1


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = parse_args(argv)
    monitor = Monitor(runner=SubprocessGh(), state_path=args.state)
    return monitor.run(args)


if __name__ == "__main__":
    sys.exit(main())
