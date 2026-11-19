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
from a2ui_board import (
    REVIEW_BOARD_COMPONENTS,
    MineBoard,
    board_data_model,
    board_dsl,
    review_board_data_model,
    review_board_dsl,
    sort_mine_snapshots,
    sort_review_snapshots,
    sync_pane_store,
    write_board_file,
)
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
    diff_reviewer_snapshots,
    diff_snapshots,
    filter_events,
    is_terminal,
    pr_added_event,
    pr_removed_event,
    snapshot_thread_watermarks,
    summarize,
)
from gh_client import (
    GhError,
    GhRunner,
    RateLimitBackoff,
    SubprocessGh,
    fetch_prs,
    search_review_keys,
)
from mine_mode import (
    GATE_LAPSE_S,
    GATED_WAIT_S,
    REMOVAL_REASONS,
    RETIRED_REMOVED,
    Discovery,
    GateBook,
    GateReason,
    MineRows,
    RecentRetired,
    Resolution,
    SourceState,
)
from models import (
    EVENT_KINDS,
    REVIEWER_EVENT_KINDS,
    SCHEMA_VERSION,
    Event,
    Membership,
    MonitorState,
    PrSnapshot,
    ReviewRole,
    TriageVerdict,
    effective_viewer_review,
    is_dormant_review,
    is_mute_active,
    parse_event_list,
    parse_pr_ref,
    pr_key,
)
from normalize import canonical_pr_repo, normalize_pr, resolve_stack_parents
from notifications import (
    DEFAULT_POLL_INTERVAL_S,
    NotificationPoller,
    NotificationsUnavailable,
)
from packrat_source import (
    AUTHORED_OPEN_SET,
    ChangeBatch,
    PackratError,
    PackratRunner,
    PackratSource,
    available,
    gh_config_user,
    is_default_db_path,
)
from review_overrides import (
    OverridesWatcher,
    ReviewOverrides,
    default_overrides_path,
    load_overrides,
    mute_pr,
    normalize_override_key,
    record_triage_entry,
    save_overrides,
    unmute_pr,
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
DEFAULT_STALE_DAYS = 30
BASE_BRANCHES = frozenset({"main", "master", "develop"})
PACKRAT_RENEW_S = 300
"""Interval between ``packrat watch renew`` calls (the lease TTL is 900 s)."""
PACKRAT_HEALTH_S = 60
"""Interval between service health reads on the ``packrat reader`` path."""
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


def _parse_event_list(
    raw: str | None, role: ReviewRole | str = ReviewRole.AUTHOR
) -> set[str] | None:
    return parse_event_list(raw, role=role)


def _validate_override_entries(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> None:
    try:
        for raw_ref in [*(args.mute or []), *(args.unmute or [])]:
            normalize_override_key(raw_ref, default_repo=args.repo)
        for item in args.record_triage or []:
            _key, verdict = _parse_kv(item, args.repo)
            TriageVerdict(verdict)
        for item in args.triage_summary or []:
            _parse_kv(item, args.repo)
    except ValueError as exc:
        parser.error(str(exc))


def _validate_override_args(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    if int(getattr(args, "stale_days", DEFAULT_STALE_DAYS)) < 0:
        parser.error("--stale-days must be >= 0")
    has_mutations = bool(
        args.mute or args.unmute or args.record_triage or args.triage_summary
    )
    if has_mutations and not args.reviews:
        parser.error(
            "--mute, --unmute, --record-triage, and --triage-summary require --reviews"
        )
    if has_mutations:
        _validate_override_entries(parser, args)


def _validate_mine_args(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    """Checks ``--mine`` and ``--wake``, and sets ``args.membership``.

    Runs before ``_validate_review_args()``, whose ``--until`` default of
    ``closed`` would otherwise look like a value the user passed.
    """
    if args.wake and not args.mine:
        parser.error("--wake requires --mine")
    if not args.mine:
        args.membership = Membership.REVIEW_QUEUE if args.reviews else Membership.STATIC
        return
    if args.prs:
        parser.error("cannot pass positional PR arguments with --mine")
    for flag in ("reviews", "stack"):
        if getattr(args, flag):
            parser.error(f"cannot use --mine with --{flag}")
    if args.source == "github":
        parser.error("cannot use --mine with --source github: it reads only packrat")
    if args.until not in (None, "none"):
        parser.error("cannot use --mine with --until other than none")
    args.membership = Membership.MINE
    args.until = "none"
    args.source = "packrat"


def _validate_review_args(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    if args.wake_on_review and not args.reviews:
        parser.error("--wake-on-review requires --reviews")
    if args.reviews:
        if args.stack:
            parser.error("cannot use --reviews with --stack")
        if args.until == "all-green":
            parser.error("cannot use --reviews with --until all-green")
        if args.prs:
            parser.error("cannot pass positional PR arguments with --reviews")
    _validate_override_args(parser, args)
    if args.until is None:
        args.until = "none" if args.reviews else "closed"
    args.role = ReviewRole.REVIEWER if args.reviews else ReviewRole.AUTHOR
    try:
        parse_event_list(args.events, role=args.role)
    except ValueError as exc:
        parser.error(str(exc))


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
        "--reviews",
        action="store_true",
        help="Watch incoming and active pull request reviews for the authenticated user.",
    )
    parser.add_argument(
        "--wake-on-review",
        action="store_true",
        help="Send agentapi wake messages on actionable reviewer events under --reviews --daemon.",
    )
    parser.add_argument(
        "--mine",
        action="store_true",
        help=(
            "Watch every open PR the authenticated user authored, drafts included, "
            "reading only from packrat. Takes no PR arguments; --repo limits it to "
            "one repository."
        ),
    )
    parser.add_argument(
        "--wake",
        action="store_true",
        help="Send agentapi wake messages on actionable events under --mine --daemon.",
    )
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
        help=(
            f"Comma-separated event kinds to wake on (default: all: {','.join(EVENT_KINDS)}; "
            f"under --reviews: {','.join(REVIEWER_EVENT_KINDS)})."
        ),
    )
    parser.add_argument(
        "--actionable-only",
        action="store_true",
        help="Update state and the A2UI pane in place on passive events; exit 0 only on actionable events.",
    )
    parser.add_argument(
        "--until",
        choices=("merged", "closed", "all-green", "none"),
        help="Terminal condition that causes exit code 3 (default: closed, or none with --reviews).",
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
    parser.add_argument(
        "--log", help="Append each emitted JSON payload to this JSONL file."
    )
    parser.add_argument(
        "--output", help="Also write the emitted JSON payload to this file."
    )
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
    parser.add_argument(
        "--stale-days",
        type=int,
        default=DEFAULT_STALE_DAYS,
        help=(
            "Days of inactivity before unrequested Waiting or Approved PRs move to the Dormant "
            f"drawer under --reviews (default: {DEFAULT_STALE_DAYS}; 0 disables)."
        ),
    )
    parser.add_argument(
        "--overrides-file",
        help="Path to cross-session reviewer overrides JSON (default: ~/.config/monitor/review_overrides.json).",
    )
    parser.add_argument(
        "--mute",
        action="append",
        default=[],
        metavar="PR",
        help="Mute a pull request in --reviews until a post-mute push or review request arrives.",
    )
    parser.add_argument(
        "--unmute",
        action="append",
        default=[],
        metavar="PR",
        help="Unhide a muted or dormant pull request in --reviews and pin dormant rows active.",
    )
    parser.add_argument(
        "--record-triage",
        action="append",
        default=[],
        metavar="PR=VERDICT",
        help="Record a watermarked discussion triage verdict (active, likely_shelved, deferred).",
    )
    parser.add_argument(
        "--triage-summary",
        action="append",
        default=[],
        metavar="PR=TEXT",
        help="Optional plain-text rationale paired with --record-triage for PR.",
    )
    args = parser.parse_args(argv)
    if args.daemon and args.once:
        parser.error("cannot use --daemon and --once together")
    if args.daemon and not args.conversation_id:
        parser.error("--daemon requires --conversation-id")
    _validate_mine_args(parser, args)
    _validate_review_args(parser, args)
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
    return [
        it for it in items if isinstance(it, dict) and isinstance(it.get("number"), int)
    ]


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


def _gh_stack_seed(
    runner: GhRunner,
    key: str,
    node: dict[str, Any],
    add_key: Callable[[str], None],
    source: PackratSource | None = None,
) -> None:
    fallback_repo, fallback_num = parse_pr_ref(key)
    repo = canonical_pr_repo(node, fallback_repo)
    canon_key = pr_key(repo, int(node.get("number") or fallback_num))
    if source is not None and canon_key != key:
        stack = source.stack_neighbors(canon_key)
        if stack is not None:
            for member in stack:
                add_key(member)
            return
    for p_key in _walk_parent_prs(runner, repo, str(node.get("baseRefName") or "")):
        add_key(p_key)
    add_key(canon_key)
    head_ref = str(node.get("headRefName") or "")
    if head_ref:
        for child in _list_prs_by_branch(runner, repo, "--base", head_ref):
            add_key(pr_key(repo, int(child["number"])))


def _gh_stack(
    runner: GhRunner,
    seeds: list[str],
    add_key: Callable[[str], None],
    source: PackratSource | None = None,
) -> None:
    """Expands ``seeds`` with ``gh pr list --head/--base`` walks."""
    _, raw_map = fetch_prs(runner, seeds)
    for key in seeds:
        _gh_stack_seed(runner, key, raw_map.get(key) or {}, add_key, source)


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
        _gh_stack(runner, gh_seeds, add_key, source)
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


def _with_subtitle_note(
    dm: dict[str, Any], dsl: str, note: str
) -> tuple[dict[str, Any], str]:
    """Appends ``note`` to the board subtitle in both the data model and the DSL text."""
    old = str(dm.get("subtitle") or "")
    new = f"{old} · {note}" if old else note
    dsl = dsl.replace(
        f"$/subtitle = {json.dumps(old)}", f"$/subtitle = {json.dumps(new)}", 1
    )
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


def _packrat_viewer(source: PackratSource) -> str:
    viewer_fn = getattr(source, "viewer", None) or source.viewer_login
    return str(viewer_fn() or gh_config_user())


def _membership_for_role(role: ReviewRole) -> Membership:
    """Returns the membership of a session that was not started with ``--mine``."""
    if role == ReviewRole.REVIEWER:
        return Membership.REVIEW_QUEUE
    return Membership.STATIC


def _merge_key_lists(primary: list[str], secondary: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for key in [*primary, *secondary]:
        if key not in seen and len(out) < MAX_WATCHED_PRS:
            seen.add(key)
            out.append(key)
    return out


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
        role: ReviewRole = ReviewRole.AUTHOR,
        repo: str | None = None,
        wake_on_review: bool = False,
    ) -> None:
        self.runner = runner
        self.packrat_runner = packrat_runner
        self.packrat_db: Path = packrat_source.DEFAULT_PACKRAT_DB
        self.packrat: PackratSource | None = None
        """Set while reading from ``packrat serve``; None in direct GitHub mode."""
        self.role: ReviewRole = role
        self.membership: Membership = _membership_for_role(role)
        """Which PRs are watched. ``role`` says which part the viewer plays in them."""
        self.repo: str | None = repo
        self.wake_on_review: bool = wake_on_review
        self.wake: bool = False
        """Whether a ``--mine`` daemon wakes the conversation (``--wake``)."""
        self.gates: GateBook = GateBook()
        """Fresh-fetch gates of ``--mine``. Empty in every other mode."""
        self.rows: MineRows = MineRows(cap=MAX_WATCHED_PRS)
        self.source_state: SourceState = SourceState.LIVE
        self.stale_since: str | None = None
        """When ``--mine`` last lost packrat, until it is ``live`` again."""
        self._mine_set: set[str] = set()
        """The ``authored_open`` keys at the last discovery, before the cap."""
        self._mine_announce = True
        """False during the first sync of a fresh state, which emits no ``pr_added``."""
        self._source_state_dirty = False
        """True when the board has not been written since the source state changed."""
        self._pane_heading: tuple[Any, ...] | None = None
        """The dynamic board title, subtitle, and source state last written to the pane store."""
        self.poller: NotificationPoller = NotificationPoller(runner)
        self.source_mode = "github"
        self._source_note: str | None = None
        """Board subtitle suffix naming the data source, or None for plain GitHub mode."""
        self._packrat_seq = 0
        self._next_renew = 0.0
        self._next_health = 0.0
        self._force_gh: set[str] = set()
        self._missing_review_keys: set[str] = set()
        self._prefetched_raw: dict[str, dict[str, Any]] = {}
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
        self.stale_days: int = DEFAULT_STALE_DAYS
        self.overrides_watcher: OverridesWatcher | None = None

    @property
    def _dynamic(self) -> bool:
        """Whether the watched set changes during the run."""
        return self.membership != Membership.STATIC

    @property
    def _silent_daemon(self) -> bool:
        """Whether a daemon updates the board and logs without waking the conversation."""
        if self.membership == Membership.REVIEW_QUEUE:
            return not self.wake_on_review
        return self.membership == Membership.MINE and not self.wake

    def _current_overrides(self) -> ReviewOverrides | None:
        return (
            self.overrides_watcher.current()
            if self.overrides_watcher is not None
            else None
        )

    def _sync_board_if_overrides_changed(self, args: argparse.Namespace) -> None:
        if self.overrides_watcher is not None and self.overrides_watcher.changed():
            self._sync_board([], args)

    def _warn_truncation(self, pr: str, fields: list[str]) -> None:
        if not fields or pr in self._warned_truncated:
            return
        self._warned_truncated.add(pr)
        sys.stderr.write(
            f"warning: {pr} has paginated connections exceeding page size: {', '.join(fields)}\n"
        )

    def _rekey_state_entry(self, old: str, new: str) -> None:
        if old in self.state.snapshots:
            snap = self.state.snapshots.pop(old)
            self.state.snapshots.setdefault(new, snap)
        if old in self.state.pushed_shas:
            sha = self.state.pushed_shas.pop(old)
            self.state.pushed_shas.setdefault(new, sha)
        if old in self.state.reruns:
            tokens = self.state.reruns.pop(old)
            cur = self.state.reruns.setdefault(new, [])
            cur.extend(tok for tok in tokens if tok not in cur)
        if old in self.state.reported_threads:
            threads = self.state.reported_threads.pop(old)
            self.state.reported_threads.setdefault(new, {}).update(threads)
        if old in self._pushed_keys:
            self._pushed_keys.discard(old)
            self._pushed_keys.add(new)

    def _rekey_watched(self, mapping: dict[str, str]) -> None:
        """Rewrites non-canonical PR keys in ``watched_keys`` and ``state`` to canonical ones."""
        if not mapping:
            return
        new_watched: list[str] = []
        for key in self.watched_keys:
            canon = mapping.get(key, key)
            if canon not in new_watched:
                new_watched.append(canon)
        self.watched_keys = new_watched
        for old, new in mapping.items():
            if old != new:
                self._rekey_state_entry(old, new)

    def _normalize_target_keys(
        self,
        target_keys: list[str],
        raw_map: dict[str, dict[str, Any]],
        from_packrat: set[str],
        prev_snapshots: dict[str, PrSnapshot],
    ) -> list[str]:
        resolved_targets: list[str] = []
        mapping: dict[str, str] = {}
        for key in target_keys:
            repo, _ = parse_pr_ref(key)
            snap = normalize_pr(raw_map[key], viewer=self.viewer, repo=repo)
            canon_key = snap.key
            if canon_key != key:
                mapping[key] = canon_key
            if key in from_packrat:
                _carry_over_unmirrored(
                    snap, prev_snapshots.get(canon_key) or prev_snapshots.get(key)
                )
            self._warn_truncation(canon_key, snap.truncated)
            self.state.snapshots[canon_key] = snap
            if canon_key not in resolved_targets:
                resolved_targets.append(canon_key)
        if mapping:
            self._rekey_watched(mapping)
            for old, new in mapping.items():
                old_snap = prev_snapshots.pop(old, None)
                if old_snap is not None and new not in prev_snapshots:
                    prev_snapshots[new] = old_snap
        return resolved_targets

    def _update_pr_thread_watermarks(self, key: str, snap: PrSnapshot) -> None:
        if snap.state in ("MERGED", "CLOSED"):
            return
        current_marks = snapshot_thread_watermarks(snap, viewer=self.viewer)
        if current_marks:
            merged = dict(self.state.reported_threads.get(key) or {})
            merged.update(current_marks)
            self.state.reported_threads[key] = merged

    def _diff_target_keys(
        self,
        target_keys: list[str],
        prev_snapshots: dict[str, PrSnapshot],
        ctx: DiffContext,
    ) -> list[Event]:
        events: list[Event] = []
        is_reviewer = self.role == ReviewRole.REVIEWER
        for key in target_keys:
            prev = prev_snapshots.get(key)
            cur = self.state.snapshots[key]
            if is_reviewer:
                events.extend(diff_reviewer_snapshots(prev, cur, ctx))
                self._update_pr_thread_watermarks(key, cur)
            else:
                events.extend(diff_snapshots(prev, cur, ctx))
            record_completed_durations(self.state, prev, cur)
        return events

    def _is_unassigned_open_review(self, key: str, snap: PrSnapshot) -> bool:
        if (
            snap.state != "OPEN"
            or effective_viewer_review(snap, self.viewer) is not None
        ):
            return False
        viewer_norm = self.viewer.strip().lower()
        if viewer_norm and any(
            r.lower() == viewer_norm for r in snap.requested_reviewers
        ):
            return False
        if key in self._missing_review_keys or not snap.requested_reviewers:
            return True
        return not any(r.startswith("team:") for r in snap.requested_reviewers)

    def _retire_terminal_reviews(self, terminal_keys: set[str]) -> None:
        retired = [k for k in self.watched_keys if k in terminal_keys]
        self.watched_keys = [k for k in self.watched_keys if k not in terminal_keys]
        if self.packrat is not None:
            leased_terminal = sorted(
                set(retired) | (self.packrat.watched & terminal_keys)
            )
            if leased_terminal:
                self.packrat.unlease(leased_terminal)

    def _pruned_recent_retired(self) -> list[dict[str, Any]]:
        """Prunes expired ``recent_retired`` entries in place and returns a copy."""
        recent = RecentRetired(self.state.recent_retired)
        recent.prune(self.epoch_now())
        pruned = recent.to_list()
        self.state.recent_retired = pruned
        return pruned

    def _clear_reopened_retired(self, keys: list[str]) -> None:
        """Drops ``recent_retired`` entries for reviewer PRs that are open again."""
        if self.role != ReviewRole.REVIEWER or not self.state.recent_retired:
            return
        recent = RecentRetired(self.state.recent_retired)
        for key in keys:
            snap = self.state.snapshots.get(key)
            if snap is not None and snap.state == "OPEN":
                recent.remove(key)
        self.state.recent_retired = recent.to_list()

    def _retire_finished_reviews(self, terminal_keys: set[str]) -> None:
        unassigned_keys = {
            k
            for k, s in self.state.snapshots.items()
            if self._is_unassigned_open_review(k, s)
        }
        recent = RecentRetired(self.state.recent_retired)
        now_iso = self.now()
        for key in sorted(terminal_keys):
            snap = self.state.snapshots.pop(key, None)
            if snap is not None:
                recent.add(key, snap.title, snap.state, now_iso)
        recent.prune(self.epoch_now())
        self.state.recent_retired = recent.to_list()
        for key in unassigned_keys:
            self.state.snapshots.pop(key, None)
        retired_keys = terminal_keys | unassigned_keys
        self.state.prune(retired_keys)
        if retired_keys:
            self._retire_terminal_reviews(retired_keys)

    def refresh(self, keys: list[str] | None = None) -> list[Event]:
        """Fetches ``keys`` (or all watched PRs), updates ``self.state``, and returns new events.

        In packrat mode the PR data comes from packrat's views; keys packrat doesn't
        have (or marked for ``gh`` after a slow startup) are fetched with GraphQL.
        """
        target_keys = list(self.watched_keys) if keys is None else list(keys)
        if not target_keys:
            return []
        viewer, raw_map, from_packrat = self._fetch_raw(target_keys)
        if viewer:
            self.viewer = viewer
        mine = self.membership == Membership.MINE
        lapsed_exits: set[str] = set()
        if mine:
            target_keys, lapsed_exits = self._mine_accept_rows(target_keys, raw_map)
        prev_snapshots = dict(self.state.snapshots)
        target_keys = self._normalize_target_keys(
            target_keys, raw_map, from_packrat, prev_snapshots
        )
        self._clear_reopened_retired(target_keys)

        resolve_stack_parents(self.state.snapshots)
        ctx = DiffContext(
            viewer=self.viewer,
            pushed_shas=self.state.pushed_shas,
            reported_threads=self.state.reported_threads,
            reruns=self.state.reruns,
            overrides=self._current_overrides(),
        )
        events = self._diff_target_keys(target_keys, prev_snapshots, ctx)

        terminal_keys = {
            k
            for k, s in self.state.snapshots.items()
            if s.state in ("MERGED", "CLOSED")
        }
        if mine:
            events = self._mine_settle(target_keys, lapsed_exits, events)
        elif self.role == ReviewRole.REVIEWER:
            self._retire_finished_reviews(terminal_keys)
        else:
            self.state.prune(terminal_keys)
        self._missing_review_keys.difference_update(target_keys)
        self._pushed_keys.difference_update(target_keys)
        self.backoff.reset()
        return events

    def _fetch_raw(
        self, keys: list[str]
    ) -> tuple[str, dict[str, dict[str, Any]], set[str]]:
        """Returns ``(viewer, raw_by_key, keys_served_by_packrat)`` for ``keys``.

        Under ``--mine`` every row comes from packrat, and a key without one is
        left out: no path here calls ``gh``.
        """
        if self.membership == Membership.MINE:
            raw_map = self._require_packrat().snapshots(keys)
            return "", raw_map, set(raw_map)
        if self.packrat is None:
            viewer, raw_map = fetch_prs(self.runner, keys)
            return viewer, raw_map, set()
        raw_map = self.packrat.snapshots(keys)
        gh_keys = [k for k in keys if k not in raw_map or k in self._force_gh]
        for key in gh_keys:
            raw_map.pop(key, None)
        served = set(raw_map)
        viewer = ""
        to_fetch: list[str] = []
        for key in gh_keys:
            cached = self._prefetched_raw.pop(key, None)
            if cached is not None:
                raw_map[key] = cached
            else:
                to_fetch.append(key)
        if to_fetch:
            viewer, extra = fetch_prs(self.runner, to_fetch)
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
        if self.role == ReviewRole.REVIEWER:
            return {
                snap.key: snap
                for snap, _, _, _ in sort_review_snapshots(
                    self.state.snapshots, self.viewer
                )
            }
        ordered = {
            k: self.state.snapshots[k]
            for k in self.watched_keys
            if k in self.state.snapshots
        }
        if self.membership == Membership.MINE:
            return sort_mine_snapshots(ordered)
        return ordered

    def _mine_board(self) -> MineBoard | None:
        """Returns the ``--mine`` board inputs, or None in the other modes."""
        if self.membership != Membership.MINE:
            return None
        health = self.packrat.last_health if self.packrat is not None else None
        synced = health.viewer_sets_synced_at if health is not None else {}
        return MineBoard(
            total_open=self.rows.total_open,
            pending=len(self.rows.pending),
            recent_retired=tuple(self._pruned_recent_retired()),
            set_synced_at=synced.get(AUTHORED_OPEN_SET),
            source_state=self.source_state,
            stale_since=self.stale_since,
        )

    def _build_board_artifacts(
        self, events: list[Event], ts: str
    ) -> tuple[dict[str, Any], str, list[dict[str, Any]] | None]:
        if self.role == ReviewRole.REVIEWER:
            now_ep = self.epoch_now()
            ov = self._current_overrides()
            dm = review_board_data_model(
                self.state.snapshots,
                viewer=self.viewer,
                durations=self.state.check_durations,
                events=events,
                now=now_ep,
                observed_at=ts,
                overrides=ov,
                stale_days=self.stale_days,
                now_epoch=now_ep,
                recent_retired=self._pruned_recent_retired(),
            )
            return dm, review_board_dsl(dm), REVIEW_BOARD_COMPONENTS
        etas = self._compute_etas()
        ordered_snaps = self._ordered_snapshots()
        mine = self._mine_board()
        dm = board_data_model(ordered_snaps, events, etas, observed_at=ts, mine=mine)
        dsl = board_dsl(ordered_snaps, events, etas, observed_at=ts, mine=mine)
        return dm, dsl, None

    def _dynamic_heading(self, dm: dict[str, Any]) -> tuple[Any, ...] | None:
        """Returns the board heading fields that should force a pane write when changed.

        The pane store skips a write whose rows are unchanged, but dynamic
        boards (``--mine`` and ``--reviews``) update their subtitle or source
        badge on their own (for example when ``recent_retired`` entries expire).
        """
        if self.membership == Membership.MINE:
            return dm.get("title"), dm.get("subtitle"), self.source_state
        if self.role == ReviewRole.REVIEWER:
            return dm.get("title"), dm.get("subtitle")
        return None

    def _sync_board(
        self,
        events: list[Event],
        args: argparse.Namespace,
        observed_at: str | None = None,
    ) -> dict[str, Any] | None:
        if getattr(args, "no_a2ui", False):
            return None
        ts = observed_at or self.now()
        dm, dsl, components = self._build_board_artifacts(events, ts)
        if self._source_note:
            dm, dsl = _with_subtitle_note(dm, dsl, self._source_note)
        board_path = (
            Path(args.a2ui_file)
            if getattr(args, "a2ui_file", None)
            else (self.state_path.parent / "board.a2ui")
        )
        write_board_file(board_path, dsl)
        # A dismissed board stays gone on passive ticks. It comes back for --once, an
        # actionable event, or the first sync of this process (the user just started it).
        allow_create = (
            bool(getattr(args, "once", False))
            or not getattr(self, "_board_synced", False)
            or any(ev.actionable for ev in events)
        )
        heading = self._dynamic_heading(dm)
        store_status = sync_pane_store(
            getattr(args, "conversation_id", None),
            dm,
            store_dir=getattr(args, "a2ui_store_dir", None),
            surface_id="monitor",
            observed_at=ts,
            allow_create=allow_create,
            components=components,
            force=heading is not None and heading != self._pane_heading,
        )
        if store_status == "written":
            self._pane_heading = heading
        self._board_synced = True
        return {
            "template": "stack_board",
            "surfaceId": "monitor",
            "dsl_file": str(board_path),
            "store_updated": store_status == "written",
            "store_status": store_status,
        }

    def _is_untriaged_review_snapshot(
        self,
        key: str,
        snap: PrSnapshot,
        ov: ReviewOverrides,
        now_ep: float,
    ) -> bool:
        if snap.state != "OPEN" or not snap.discussion_watermark:
            return False
        if is_mute_active(snap, ov.get_mute(key), self.viewer):
            return False
        if is_dormant_review(
            snap,
            self.viewer,
            self.stale_days,
            now_ep,
            pinned_at=ov.get_pin(key),
        ):
            return False
        cached = ov.get_triage(key)
        return cached is None or cached.watermark != snap.discussion_watermark

    def _compute_untriaged_discussions(
        self,
        ordered_snaps: dict[str, Any],
    ) -> list[str]:
        ov = self._current_overrides() or ReviewOverrides()
        now_ep = self.epoch_now()
        return [
            key
            for key, snap in ordered_snaps.items()
            if self._is_untriaged_review_snapshot(key, snap, ov, now_ep)
        ]

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
        if self.role == ReviewRole.REVIEWER:
            payload["viewer"] = self.viewer
            payload["stale_days"] = self.stale_days
            payload["untriaged_discussions"] = self._compute_untriaged_discussions(
                ordered_snaps
            )
            payload["recent_retired"] = self._pruned_recent_retired()
        if self.membership == Membership.MINE:
            payload.update(self._mine_payload())
        if batch_id is not None:
            payload["batch_id"] = batch_id
        a2ui_meta = self._sync_board(events, args, observed_at=observed_at)
        if a2ui_meta is not None:
            payload["a2ui"] = a2ui_meta
        return payload

    def _record_threads_after_emit(self) -> None:
        for key, snap in self.state.snapshots.items():
            self._update_pr_thread_watermarks(key, snap)

    def emit(
        self, payload: dict[str, Any], args: argparse.Namespace, *, output: bool = True
    ) -> None:
        """Writes ``payload`` to stdout, optional ``--output`` and ``--log``, then saves state.

        ``output=False`` skips ``--output``, which then keeps the batch it holds.
        """
        serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        sys.stdout.write(serialized)
        sys.stdout.flush()
        if output and args.output:
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
        return self._open_keys_where(
            lambda s: s.rollup_state in ("PENDING", "EXPECTED")
        )

    def _fast_keys(self, accumulated: list[Event]) -> set[str]:
        """Returns keys refreshed at ``FAST_POLL_INTERVAL_S`` cadence instead of ``--poll``.

        These are open PRs whose mergeability GitHub is still computing, PRs with events
        in the pending debounce batch, and PRs named in a ``pushed_sha`` control command.
        """
        watched = set(self.watched_keys)
        debounce_keys = {ev.pr for ev in accumulated}
        return self._unknown_mergeable_keys() | (
            (debounce_keys | self._pushed_keys) & watched
        )

    def _poll_notifications(self, default_interval: int) -> tuple[set[str], int]:
        if not self.notifications_enabled:
            return set(), default_interval
        try:
            res = self.poller.poll(
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
            candidates.append(
                min(FAST_POLL_INTERVAL_S, max(0.0, debounce_deadline - now))
            )
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

    def _filter_wake_events_for_scope(
        self, events: list[Event], scope: str
    ) -> list[Event]:
        if self.membership == Membership.MINE:
            # Every PR is the viewer's, including one whose snapshot was just retired.
            return list(events)
        if self.role == ReviewRole.REVIEWER:
            return [ev for ev in events if ev.actionable]
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

    def _emit_silent_daemon_batch(
        self, accumulated: list[Event], args: argparse.Namespace
    ) -> bool:
        payload = self._build_payload(
            "change", 0, accumulated, args, notify_mode="none"
        )
        self.emit(payload, args)
        if not getattr(args, "output", None):
            last_json = self.state_path.parent / "last.json"
            last_json.parent.mkdir(parents=True, exist_ok=True)
            last_json.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        accumulated.clear()
        return True

    def _dispatch_daemon_batch(
        self,
        now: float,
        accumulated: list[Event],
        args: argparse.Namespace,
    ) -> bool:
        if self._silent_daemon:
            return self._emit_silent_daemon_batch(accumulated, args)
        if not self.gate.can_send() or now < self._next_send_retry:
            return False
        events_to_wake = self._filter_wake_events_for_scope(
            accumulated, getattr(args, "notify_scope", "mine")
        )
        if not events_to_wake:
            if self._dynamic:
                return self._emit_silent_daemon_batch(accumulated, args)
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

    def _check_reminder(
        self, now: float, reminder_s: float, conversation_id: str | None
    ) -> None:
        if (
            not self.gate.reminder_due(now, reminder_s)
            or not self.notifier
            or not conversation_id
        ):
            return
        title = (
            f"[PR Monitor] Reminder: Batch {self.gate.awaiting} awaiting acknowledgment"
        )
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
            self._check_reminder(
                now, float(args.reminder), getattr(args, "conversation_id", None)
            )
            return None
        if debounce_deadline is not None and now >= debounce_deadline:
            return self._finish_with("change", 0, accumulated, args)
        return None

    def _discover_github_review_delta(self) -> list[str]:
        viewer, discovered = search_review_keys(
            self.runner, repo=self.repo, backoff=self.backoff
        )
        if viewer:
            self.viewer = viewer
        existing = set(self.watched_keys)
        added: list[str] = []
        for key in discovered[:MAX_WATCHED_PRS]:
            if key not in existing and len(self.watched_keys) < MAX_WATCHED_PRS:
                self.watched_keys.append(key)
                existing.add(key)
                added.append(key)
        return added

    def _consume_unwatched_review_request(self) -> set[str]:
        if self.role != ReviewRole.REVIEWER:
            return set()
        if not (
            self.poller.review_requested_unwatched
            or NotificationPoller.review_requested_unwatched
        ):
            return set()
        added = set(self._discover_github_review_delta())
        self.poller.review_requested_unwatched = False
        NotificationPoller.review_requested_unwatched = False
        return added

    def _sweep_github_review_keys(self) -> None:
        viewer, discovered = search_review_keys(
            self.runner, repo=self.repo, backoff=self.backoff
        )
        if viewer:
            self.viewer = viewer
        open_set = set(discovered)
        self._missing_review_keys.update(
            k for k in self.watched_keys if k not in open_set
        )
        self.watched_keys = _merge_key_lists(
            discovered[:MAX_WATCHED_PRS], self.watched_keys
        )

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
            flagged |= self._consume_unwatched_review_request()
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
        if self.membership == Membership.MINE:
            # A GitHub-mode step. ``--mine`` reads only packrat and never gets here.
            return debounce_deadline
        sweep_due = now >= timers.next_sweep
        targets = self._select_refresh_targets(now, timers, accumulated, cfg)
        if targets is not None and not targets:
            self._sync_board_if_overrides_changed(args)
            return debounce_deadline
        if (
            sweep_due
            and self.membership == Membership.REVIEW_QUEUE
            and now >= timers.backoff_until
        ):
            self._sweep_github_review_keys()
        raw_events = self.refresh(targets)
        if targets is None:
            timers.next_sweep = now + cfg.sweep_s
            timers.next_poll = now + cfg.poll_s
        return self._absorb_events(
            now, raw_events, debounce_deadline, cfg, accumulated, args
        )

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
        if self.membership == Membership.MINE:
            return self._absorb_mine_events(
                now, matched, debounce_deadline, cfg, accumulated, args
            )
        wake_events = _select_wake_events(matched, bool(args.actionable_only))
        if wake_events:
            accumulated.extend(wake_events)
            if debounce_deadline is None:
                debounce_deadline = now + cfg.debounce_s
        elif self._dynamic and getattr(args, "daemon", False) and matched:
            self._emit_silent_daemon_batch(matched, args)
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
        timeout_deadline = (
            start + int(args.timeout) if args.timeout is not None else None
        )
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
                accumulated,
                debounce_deadline,
                now,
                cfg.poll_s,
                getattr(args, "daemon", False),
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
            sweep_deadline = timers.next_sweep if self._dynamic else None
            wait_s = self._packrat_wait_s(
                now, debounce_deadline, timeout_deadline, cfg, sweep_deadline
            )
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
                raw_events = self._refresh_from_batch(batch, now, timers, cfg)
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
        sweep_deadline: float | None = None,
    ) -> float:
        """Returns the ``packrat changes --wait`` timeout: the debounce window, capped.

        While a ``--mine`` gate holds, the wait is at most ``GATED_WAIT_S``. A
        refetch that changes nothing writes no change row, so the gated keys are
        re-read on a timer.
        """
        wait = float(cfg.debounce_s or PACKRAT_DEFAULT_WAIT_S)
        for deadline in (debounce_deadline, timeout_deadline, sweep_deadline):
            if deadline is not None:
                wait = min(wait, max(0.0, deadline - now))
        if self.gates.keys():
            wait = min(wait, GATED_WAIT_S)
        return max(1.0, wait)

    def _packrat_wait(
        self, now: float, wait_s: float, timers: _Timers
    ) -> ChangeBatch | None:
        """Checks the heartbeat, renews the lease when due, and waits on the change feed.

        Returns None (after switching to GitHub mode) when packrat is unavailable.
        Under ``--mine`` it returns None while the source state is ``stale``.
        """
        source = self.packrat
        assert source is not None
        if self.source_state == SourceState.STALE:
            return self._mine_stale_wait(source, now, wait_s)
        try:
            self._check_packrat_health(source, now)
            if now >= self._next_renew:
                source.renew()
                self._next_renew = now + PACKRAT_RENEW_S
            batch = source.wait_changes(
                self._packrat_seq,
                wait_s,
                watch_viewer_sets=self._dynamic,
            )
        except PackratError as exc:
            self._fallback_to_github(now, timers, str(exc))
            return None
        self._packrat_seq = batch.seq
        return batch

    def _check_packrat_health(self, source: PackratSource, now: float) -> None:
        """Raises ``PackratError`` when the service heartbeat is stale or missing.

        A database file is checked on every wait. On the ``packrat reader`` path each
        check costs a CLI call, so it runs once every ``PACKRAT_HEALTH_S`` seconds.

        ``--mine`` reads the full health every ``PACKRAT_HEALTH_S`` seconds on both
        routes, and also fails when 3 or more service ticks in a row have failed.
        """
        if self.membership == Membership.MINE:
            self._check_mine_health(source, now)
            return
        if not is_default_db_path(source.db_path) and source.db_path.is_file():
            fresh = available(source.db_path)
        elif now >= self._next_health:
            self._next_health = now + PACKRAT_HEALTH_S
            fresh = source.health().heartbeat_fresh
        else:
            return
        if not fresh:
            raise PackratError("packrat serve heartbeat is stale or missing")

    def _discover_packrat_review_delta(self) -> tuple[list[str], list[str]]:
        source = self.packrat
        assert source is not None
        all_open_keys = source.discover_review_keys(repo=self.repo)
        open_keys = all_open_keys[:MAX_WATCHED_PRS]
        self.viewer = self.viewer or _packrat_viewer(source)
        old_set = set(self.watched_keys)
        all_open_set = set(all_open_keys)
        newly_added = [k for k in open_keys if k not in old_set]
        newly_missing = [k for k in self.watched_keys if k not in all_open_set]
        self._missing_review_keys.update(newly_missing)
        if newly_added:
            source.lease(newly_added)
        self.watched_keys = _merge_key_lists(open_keys, self.watched_keys)
        return newly_added, newly_missing

    def _refresh_reviewer_batch(
        self,
        batch: ChangeBatch,
        now: float,
        timers: _Timers | None,
        cfg: _LoopConfig | None,
    ) -> list[Event]:
        sweep_due = timers is not None and now >= timers.next_sweep
        delta_keys: list[str] = []
        if batch.viewer_changed or batch.reset or sweep_due:
            added, missing = self._discover_packrat_review_delta()
            delta_keys = _merge_key_lists(added, missing)
            if sweep_due and timers is not None and cfg is not None:
                timers.next_sweep = now + cfg.sweep_s
        if batch.reset or sweep_due:
            targets = _merge_key_lists(list(self.watched_keys), delta_keys)
        else:
            changed = [k for k in self.watched_keys if k in batch.keys]
            targets = _merge_key_lists(delta_keys, changed)
        return self.refresh(targets) if targets else []

    def _refresh_from_batch(
        self,
        batch: ChangeBatch,
        now: float = 0.0,
        timers: _Timers | None = None,
        cfg: _LoopConfig | None = None,
    ) -> list[Event]:
        """Re-reads the PRs named in ``batch`` (every watched PR after a feed reset)."""
        if self.membership == Membership.MINE:
            return self._refresh_mine_batch(batch, now, timers, cfg)
        if self.membership == Membership.REVIEW_QUEUE:
            return self._refresh_reviewer_batch(batch, now, timers, cfg)
        if batch.reset:
            return self.refresh(None)
        keys = [k for k in self.watched_keys if k in batch.keys]
        return self.refresh(keys) if keys else []

    def _fallback_to_github(self, now: float, timers: _Timers, reason: str) -> None:
        """Switches to direct GitHub polling for the rest of the run, logging it once.

        ``--mine`` never polls GitHub. It enters the ``stale`` source state instead.
        """
        if self.membership == Membership.MINE:
            self._enter_stale(now, reason)
            return
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
        self.packrat_db = (
            Path(getattr(args, "packrat_db", None) or packrat_source.DEFAULT_PACKRAT_DB)
            .expanduser()
            .resolve()
        )
        if available(self.packrat_db, runner=self.packrat_runner):
            return PackratSource(
                self.packrat_db,
                _consumer_id(args, self.state_path),
                runner=self.packrat_runner,
            )
        if mode == "packrat":
            raise ValueError(
                f"--source packrat: packrat serve is not running or its heartbeat is stale "
                f"({self.packrat_db}). Start it with `packrat auto-sync on`, or use --source github."
            )
        return None

    def _canonicalize_with_gh(self, keys: list[str]) -> None:
        """Fetches ``keys`` via ``gh``, caches the raw PRs, and rekeys to canonical slugs."""
        if not keys:
            return
        viewer, raw_map = fetch_prs(self.runner, keys)
        if viewer:
            self.viewer = viewer
        mapping: dict[str, str] = {}
        for key in keys:
            raw = raw_map.get(key)
            if not raw:
                continue
            repo, number = parse_pr_ref(key)
            canon_repo = canonical_pr_repo(raw, repo)
            canon_key = pr_key(canon_repo, int(raw.get("number") or number))
            self._prefetched_raw[canon_key] = raw
            mapping[key] = canon_key
        self._rekey_watched(mapping)

    def _start_packrat(self) -> None:
        """Leases the watched PRs and waits briefly for packrat to refresh missing ones.

        Keys still missing or stale after ``PACKRAT_STARTUP_WAIT_S`` are fetched with
        ``gh`` on the first refresh.
        """
        source = self.packrat
        assert source is not None
        known = source.known_keys(self.watched_keys)
        self._rekey_watched({k: v for k, v in known.items() if k != v})
        unknown = [k for k in self.watched_keys if k not in known]
        if unknown:
            self._canonicalize_with_gh(unknown)
        keys = list(self.watched_keys)
        source.lease(keys)
        self._next_renew = self.clock() + PACKRAT_RENEW_S
        self._next_health = self.clock() + PACKRAT_HEALTH_S
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

    # ----- --mine mode ----------------------------------------------------------------

    def _require_packrat(self) -> PackratSource:
        """Returns the packrat source, which ``--mine`` never runs without."""
        if self.packrat is None:
            raise PackratError("--mine has no packrat source")
        return self.packrat

    def _mine_wake_events(self, matched: list[Event]) -> list[Event]:
        """Returns the events that open a wake window under ``--mine``.

        Nothing without ``--wake``. With it, every matched event when at least one
        is actionable, so passive events travel with the batch that wakes the
        conversation. ``--actionable-only`` has no effect here.
        """
        if not self.wake or not any(ev.actionable for ev in matched):
            return []
        return list(matched)

    def _mine_log_or_sync(self, matched: list[Event], args: argparse.Namespace) -> None:
        """Handles events that open no wake window: a silent batch, or a board update.

        While a woken batch awaits acknowledgment, a silent batch is logged and
        shown on the board but not written to ``--output`` or ``last.json``: the
        woken conversation reads the unacknowledged batch from there.
        """
        if matched and getattr(args, "daemon", False):
            if self.gate.can_send():
                self._emit_silent_daemon_batch(list(matched), args)
                return
            payload = self._build_payload(
                "change", 0, list(matched), args, notify_mode="none"
            )
            self.emit(payload, args, output=False)
            return
        self._sync_board(matched, args)
        save_state(self.state_path, self.state)

    def _mine_startup_wake(
        self, matched: list[Event], args: argparse.Namespace
    ) -> list[Event]:
        """Applies the wake rule to the startup events and returns the ones that wake."""
        wake_events = self._mine_wake_events(matched)
        if wake_events:
            self._sync_board(matched, args)
            save_state(self.state_path, self.state)
        else:
            self._mine_log_or_sync(matched, args)
        return wake_events

    def _absorb_mine_events(
        self,
        now: float,
        matched: list[Event],
        debounce_deadline: float | None,
        cfg: _LoopConfig,
        accumulated: list[Event],
        args: argparse.Namespace,
    ) -> float | None:
        """Applies the ``--mine`` wake rule to one tick's events.

        An actionable event opens the wake window, and every event seen while it
        is open joins the batch. Outside a window, events go through the silent
        path under ``--daemon``. The board and ``state.json`` are written on every
        path, so a retired row leaves the board before the debounce ends.
        """
        window_open = debounce_deadline is not None
        if not matched or not (window_open or self._mine_wake_events(matched)):
            self._mine_log_or_sync(matched, args)
            return debounce_deadline
        accumulated.extend(matched)
        self._sync_board(matched, args)
        save_state(self.state_path, self.state)
        return debounce_deadline if window_open else now + cfg.debounce_s

    def _mine_payload(self) -> dict[str, Any]:
        """Returns the payload fields that only ``--mine`` reports."""
        return {
            "membership": self.membership.value,
            "source_state": self.source_state.value,
            "stale_since": self.stale_since,
            "total_open": self.rows.total_open,
            "pending": len(self.rows.pending),
            "recent_retired": self._pruned_recent_retired(),
        }

    def _sync_board_if_source_state_changed(self, args: argparse.Namespace) -> None:
        if not self._source_state_dirty:
            return
        self._source_state_dirty = False
        self._sync_board([], args)
        save_state(self.state_path, self.state)

    def _check_mine_health(self, source: PackratSource, now: float) -> None:
        """Raises ``PackratError`` when a due health read finds the service unhealthy."""
        if now < self._next_health:
            return
        self._next_health = now + PACKRAT_HEALTH_S
        if not source.health().healthy:
            raise PackratError(
                "packrat serve is unhealthy: its heartbeat is stale or missing, "
                "or 3 or more ticks have failed"
            )

    def _enter_stale(self, now: float, reason: str) -> None:
        """Stops trusting packrat under ``--mine`` until a health read is good again.

        The board keeps its last rows. While ``stale`` the loop reads nothing but
        health, so no event fires and no row is retired. ``--mine`` never polls
        GitHub instead.
        """
        if self.source_state == SourceState.STALE:
            return
        sys.stderr.write(
            f"warning: packrat unavailable ({reason}); --mine keeps the last board "
            "until the service recovers\n"
        )
        self.source_state = SourceState.STALE
        self.stale_since = self.now()
        self._next_health = now + PACKRAT_HEALTH_S
        self._source_state_dirty = True

    def _mine_stale_wait(
        self, source: PackratSource, now: float, wait_s: float
    ) -> ChangeBatch | None:
        """Sleeps through one wait while ``stale``, reading health when it is due.

        Returns a reset batch on recovery, so the caller rediscovers the set and
        re-reads every watched key. Otherwise returns None.
        """
        if now >= self._next_health:
            self._next_health = now + PACKRAT_HEALTH_S
            try:
                if source.health().healthy:
                    return self._mine_recover(source, now)
            except PackratError:
                pass
        self.sleep(wait_s)
        return None

    def _mine_recover(self, source: PackratSource, now: float) -> ChangeBatch:
        """Moves from ``stale`` to ``catching_up`` after a good health read.

        The stored rows predate the outage, so every watched key is leased again,
        gated at the recovery time, and requested from packrat. The feed position
        moves to the head: rows missed during the outage are covered by the full
        re-read.
        """
        source.lease(self.watched_keys)
        head = source.wait_changes(_FEED_HEAD, 0, watch_viewer_sets=True)
        self._packrat_seq = head.seq
        self._next_renew = now + PACKRAT_RENEW_S
        for key in self.watched_keys:
            self.gates.set(key, now, GateReason.RECOVERY)
        self._request_packrat_refresh(set(self.watched_keys))
        self.source_state = SourceState.CATCHING_UP
        sys.stderr.write("monitor: packrat is healthy again; catching up\n")
        return ChangeBatch(seq=head.seq, reset=True, viewer_changed=True)

    def _mine_settle_source_state(self) -> None:
        """Returns to ``live`` once no recovery gate holds."""
        if self.source_state != SourceState.CATCHING_UP:
            return
        if self.gates.holds_reason(GateReason.RECOVERY):
            return
        self.source_state = SourceState.LIVE
        self.stale_since = None

    def _mine_stop_watching(self, keys: list[str]) -> None:
        """Removes ``keys`` from the watch list and drops their leases."""
        if not keys:
            return
        gone = set(keys)
        self.watched_keys = [k for k in self.watched_keys if k not in gone]
        self.state.prune(gone)
        try:
            self._require_packrat().unlease(keys)
        except PackratError as exc:
            sys.stderr.write(f"warning: packrat lease release failed: {exc}\n")

    def _mine_discover(self, now: float, lease_all: bool = False) -> Discovery:
        """Reads the ``authored_open`` set and applies it to the watch list.

        New keys are leased, or every watched key with ``lease_all``. A shown key
        that left the set is gated at ``now``: its row stays until a fresh one
        says why it left. Keys dropped by the set or the cap lose their row and
        lease with no event.

        Returns the discovery, whose ``gate`` keys still need a refresh request.
        """
        source = self._require_packrat()
        set_keys = source.discover_authored_keys(repo=self.repo)
        shown = [k for k in self.watched_keys if k in self.state.snapshots]
        plan = self.rows.apply_discovery(set_keys, shown, now)
        self._mine_set = set(set_keys)
        for key in plan.drop:
            self.state.snapshots.pop(key, None)
            self.gates.clear(key)
        for key in plan.regained:
            if self.gates.reason(key) == GateReason.SET_EXIT:
                self.gates.clear(key)
        for key in plan.gate:
            # A recovery or startup gate keeps its reason, so catching_up holds
            # and a return to the set does not clear it before the refetch.
            self.gates.set(key, now, self.gates.reason(key) or GateReason.SET_EXIT)
        self._mine_stop_watching(plan.drop)
        self.watched_keys = list(plan.watched)
        source.lease(plan.watched if lease_all else plan.lease)
        return plan

    def _refresh_mine_batch(
        self,
        batch: ChangeBatch,
        now: float,
        timers: _Timers | None,
        cfg: _LoopConfig | None,
    ) -> list[Event]:
        """Re-reads the changed keys and every gated key after one wait.

        The set is rediscovered on a ``viewer`` change row, a feed reset, or a due
        sweep. Every key gated here gets one ``packrat refresh`` request, and a
        sweep adds the keys that still have no row.
        """
        sweep_due = timers is not None and now >= timers.next_sweep
        full = batch.reset or sweep_due
        to_refresh: set[str] = set()
        leased: list[str] = []
        if batch.viewer_changed or full:
            plan = self._mine_discover(now)
            to_refresh.update(plan.gate)
            leased = plan.lease
        if sweep_due and timers is not None and cfg is not None:
            timers.next_sweep = now + cfg.sweep_s
            to_refresh.update(self.rows.pending)
        self._request_packrat_refresh(to_refresh)
        if full:
            targets = list(self.watched_keys)
        else:
            named = batch.keys | set(leased)
            targets = [k for k in self.watched_keys if k in named]
        targets = _merge_key_lists(targets, self.gates.keys())
        events = self.refresh(targets) if targets else []
        self._mine_settle_source_state()
        return events

    def _mine_expire_gates(self, now: float) -> set[str]:
        """Acts on gates that have lapsed, and returns the lapsed set-exit keys.

        A lapsed startup or recovery gate accepts the stored row with a warning.
        A lapsed gate on a row that left the set is left for ``MineRows.resolve()``,
        which retires the row as unconfirmed.
        """
        lapsed_exits: set[str] = set()
        for key, reason in self.gates.lapsed(now):
            if key in self.rows.pending_exit:
                lapsed_exits.add(key)
                continue
            sys.stderr.write(
                f"warning: packrat did not refetch {key} within {GATE_LAPSE_S}s of "
                f"its {reason.value} gate; using the stored row\n"
            )
            self.gates.clear(key)
        return lapsed_exits

    def _mine_accept_rows(
        self, target_keys: list[str], raw_map: dict[str, dict[str, Any]]
    ) -> tuple[list[str], set[str]]:
        """Returns the keys whose rows can be used now, and the lapsed set-exit keys.

        A key is skipped, keeping its previous snapshot, when packrat has no row
        for it, when its row was fetched before its gate, or when it has no board
        row yet and its row is not open. A row that passes a gate clears it.
        """
        now = self.clock()
        lapsed_exits = self._mine_expire_gates(now)
        gated = [k for k in target_keys if k in raw_map and self.gates.holds(k)]
        ages = self._require_packrat().fetched_ages(gated)
        accepted: list[str] = []
        for key in target_keys:
            raw = raw_map.get(key)
            if raw is None or not self.gates.accepts(key, ages.get(key), now):
                continue
            self.gates.clear(key)
            if key in self.rows.pending and raw.get("state") != "OPEN":
                continue
            accepted.append(key)
        return accepted, lapsed_exits

    def _mine_retire(self, key: str, snap: PrSnapshot, state: str) -> None:
        """Drops the row of ``key`` and records it in the recent removals."""
        recent = RecentRetired(self.state.recent_retired)
        recent.add(key, snap.title, state, self.now())
        recent.prune(self.epoch_now())
        self.state.recent_retired = recent.to_list()
        self.state.snapshots.pop(key, None)
        self.gates.clear(key)

    def _mine_settle(
        self, accepted: list[str], lapsed_exits: set[str], events: list[Event]
    ) -> list[Event]:
        """Announces rows that first showed and retires rows that finished or left.

        ``accepted`` holds the keys whose rows were just read. A retirement drops
        the snapshot, the watched key, and the lease in this tick. A finished PR
        already has its ``terminal`` event in ``events``; a row that left the set
        while open gets a ``pr_removed`` event here.
        """
        snapshots = self.state.snapshots
        shown = [k for k in accepted if self.rows.mark_shown(k)]
        added = [pr_added_event(snapshots[k]) for k in shown if self._mine_announce]
        removed = self._mine_retire_rows(accepted, lapsed_exits)
        return [*added, *events, *removed]

    def _mine_retire_rows(
        self, accepted: list[str], lapsed_exits: set[str]
    ) -> list[Event]:
        """Retires each row that ``MineRows.resolve()`` no longer keeps.

        Returns a ``pr_removed`` event for each row that left the set while open.
        """
        fresh = set(accepted)
        removed: list[Event] = []
        retired: list[str] = []
        for key in dict.fromkeys([*accepted, *self.rows.pending_exit]):
            snap = self.state.snapshots.get(key)
            if snap is None:
                continue
            resolution = self.rows.resolve(
                key,
                snap.state if key in fresh else None,
                key in self._mine_set,
                key in lapsed_exits,
            )
            if resolution == Resolution.KEEP:
                continue
            reason = REMOVAL_REASONS.get(resolution)
            if reason is not None:
                removed.append(pr_removed_event(snap, reason))
            self._mine_retire(key, snap, RETIRED_REMOVED if reason else snap.state)
            retired.append(key)
        self._mine_stop_watching(retired)
        return removed

    def _prune_startup_mine_snapshots(self) -> list[str]:
        """Drops saved snapshots that are closed, not the viewer's, or outside ``--repo``."""
        repo = (self.repo or "").lower()
        drop = {
            key
            for key, snap in self.state.snapshots.items()
            if snap.state in ("MERGED", "CLOSED")
            or not snap.mine
            or (repo and snap.repo.lower() != repo)
        }
        for key in drop:
            self.state.snapshots.pop(key, None)
        if drop:
            self.state.prune(drop)
        return list(self.state.snapshots.keys())

    def _mine_gate_old_rows(self, source: PackratSource, now: float) -> set[str]:
        """Gates each watched key whose row is older than ``PACKRAT_STALE_ROW_S``."""
        ages = source.fetched_ages(self.watched_keys)
        old = {
            key
            for key, age in ages.items()
            if age > PACKRAT_STALE_ROW_S and not self.gates.holds(key)
        }
        for key in old:
            self.gates.set(key, now, GateReason.STARTUP)
        return old

    def _mine_held_gates(self, source: PackratSource) -> list[str]:
        """Returns the gated keys whose stored rows still predate their gates."""
        keys = self.gates.keys()
        if not keys:
            return []
        ages = source.fetched_ages(keys)
        now = self.clock()
        return [k for k in keys if not self.gates.accepts(k, ages.get(k), now)]

    def _mine_startup_wait(self, source: PackratSource, once: bool) -> None:
        """Waits up to ``PACKRAT_STARTUP_WAIT_S`` for packrat to refetch the gated rows.

        The wait follows the change feed from a local position, so the loop still
        sees every row written since startup. A daemon carries a gate that is
        still held into its loop. ``--once`` has no loop, so it uses the stored
        row of a held startup gate with a warning. It keeps the row of a held
        set-exit gate, and the next run gates that row again.
        """
        deadline = self.clock() + PACKRAT_STARTUP_WAIT_S
        seq = self._packrat_seq
        held = self._mine_held_gates(source)
        while held and self.clock() < deadline:
            seq = source.wait_changes(seq, 2, watch_viewer_sets=True).seq
            held = self._mine_held_gates(source)
        if not once:
            return
        for key in held:
            if self.gates.reason(key) != GateReason.STARTUP:
                continue
            sys.stderr.write(
                f"warning: packrat did not refetch {key} within "
                f"{PACKRAT_STARTUP_WAIT_S}s; using the stored row\n"
            )
            self.gates.clear(key)

    def _init_mine_targets(self, args: argparse.Namespace) -> list[Event]:
        """Starts ``--mine``: discovers the set, leases it, gates old rows, and reads them.

        The feed position is taken before discovery, so a set change that lands
        during startup comes back on the first wait.

        Raises:
            PackratError: packrat could not be read, or has never synced the
                ``authored_open`` set. ``run()`` exits 1 on it.
        """
        source = self._require_packrat()
        once = bool(getattr(args, "once", False))
        self.rows = MineRows(cap=MAX_WATCHED_PRS, once=once)
        self.watched_keys = self._prune_startup_mine_snapshots()
        self._mine_announce = bool(self.state.snapshots)
        self.viewer = _packrat_viewer(source)
        now = self.clock()
        self._next_renew = now + PACKRAT_RENEW_S
        self._next_health = now + PACKRAT_HEALTH_S
        head = source.wait_changes(_FEED_HEAD, 0, watch_viewer_sets=True)
        self._packrat_seq = head.seq
        plan = self._mine_discover(now, lease_all=True)
        source.refresh(set(plan.gate) | self._mine_gate_old_rows(source, now))
        self._mine_startup_wait(source, once)
        events = self.refresh() if self.watched_keys else []
        self._mine_announce = True
        return events

    def _loop_heartbeat_and_control(
        self, accumulated: list[Event], args: argparse.Namespace
    ) -> int | None:
        if self.liveness is not None:
            self.liveness.beat()
        self._sync_board_if_overrides_changed(args)
        self._sync_board_if_source_state_changed(args)
        if getattr(args, "daemon", False) and self._process_control(args.repo):
            return self._finish_with("stopped", 0, accumulated, args)
        return None

    def _prune_startup_reviewer_snapshots(self) -> list[str]:
        self._pruned_recent_retired()
        drop = {
            key
            for key, snap in self.state.snapshots.items()
            if snap.state in ("MERGED", "CLOSED")
            or snap.mine
            or (self.repo is not None and snap.repo != self.repo)
        }
        for key in drop:
            self.state.snapshots.pop(key, None)
        if drop:
            self.state.prune(drop)
        return list(self.state.snapshots.keys())

    def _start_reviewer_packrat(self, prior_open: list[str]) -> list[Event]:
        source = self.packrat
        assert source is not None
        all_open_keys = source.discover_review_keys(repo=self.repo)
        open_keys = all_open_keys[:MAX_WATCHED_PRS]
        all_open_set = set(all_open_keys)
        self._missing_review_keys.update(k for k in prior_open if k not in all_open_set)
        self.viewer = _packrat_viewer(source)
        if open_keys:
            source.lease(open_keys)
        self._next_renew = self.clock() + PACKRAT_RENEW_S
        self._next_health = self.clock() + PACKRAT_HEALTH_S
        self._packrat_seq = source.wait_changes(
            _FEED_HEAD, 0, watch_viewer_sets=True
        ).seq
        self.watched_keys = _merge_key_lists(open_keys, prior_open)
        return self.refresh(self.watched_keys) if self.watched_keys else []

    def _start_reviewer_github(self, prior_open: list[str]) -> list[Event]:
        viewer, keys = search_review_keys(
            self.runner, repo=self.repo, backoff=self.backoff
        )
        self.viewer = viewer
        all_open_set = set(keys)
        self._missing_review_keys.update(k for k in prior_open if k not in all_open_set)
        open_keys = keys[:MAX_WATCHED_PRS]
        self.watched_keys = _merge_key_lists(open_keys, prior_open)
        return self.refresh(self.watched_keys) if self.watched_keys else []

    def _init_reviewer_targets(self) -> list[Event]:
        prior_open = self._prune_startup_reviewer_snapshots()
        if self.packrat is not None:
            try:
                return self._start_reviewer_packrat(prior_open)
            except PackratError as exc:
                timers = _Timers(next_poll=0.0, next_sweep=0.0, next_notify=0.0)
                self._fallback_to_github(self.clock(), timers, str(exc))
        return self._start_reviewer_github(prior_open)

    def _find_snapshot_case_insensitive(self, norm_key: str) -> PrSnapshot | None:
        for key, snap in self.state.snapshots.items():
            if key.lower() == norm_key:
                return snap
        return None

    def _resolve_snapshot_for_override(
        self,
        raw_ref: str,
        default_repo: str | None,
    ) -> tuple[str, PrSnapshot | None]:
        repo, number = parse_pr_ref(raw_ref, default_repo)
        canon_hint = pr_key(repo, number)
        norm_key = canon_hint.lower()
        snap = self._find_snapshot_case_insensitive(norm_key)
        if snap is not None:
            return norm_key, snap
        target_key = canon_hint
        if self.packrat is not None:
            known = self.packrat.known_keys([canon_hint, norm_key])
            target_key = known.get(canon_hint) or known.get(norm_key) or norm_key
            try:
                self.packrat.lease([target_key])
            except PackratError:
                pass
        self.refresh([target_key])
        return norm_key, self._find_snapshot_case_insensitive(norm_key)

    def _parse_triage_summaries(self, args: argparse.Namespace) -> dict[str, str]:
        summaries: dict[str, str] = {}
        for item in getattr(args, "triage_summary", None) or []:
            key, text = _parse_kv(item, args.repo)
            summaries[key.lower()] = text
        return summaries

    def _record_cli_triage_items(
        self,
        overrides: ReviewOverrides,
        triage_items: list[str],
        summaries: dict[str, str],
        default_repo: str | None,
        now_ts: str,
    ) -> None:
        for item in triage_items:
            key, verdict_str = _parse_kv(item, default_repo)
            norm_key, snap = self._resolve_snapshot_for_override(key, default_repo)
            if snap is not None and snap.discussion_watermark:
                watermark = snap.discussion_watermark
            elif snap is not None:
                watermark = f"manual:{snap.updated_at}"
            else:
                watermark = "manual"
            record_triage_entry(
                overrides,
                norm_key,
                watermark=watermark,
                verdict=TriageVerdict(verdict_str),
                summary=summaries.get(norm_key, ""),
                evaluated_at=now_ts,
            )

    def _filter_unmuted_events(
        self,
        raw_events: list[Event],
        overrides: ReviewOverrides,
    ) -> list[Event]:
        kept: list[Event] = []
        for ev in raw_events:
            snap = self.state.snapshots.get(ev.pr)
            if snap is None or not is_mute_active(
                snap, overrides.get_mute(ev.pr), self.viewer
            ):
                kept.append(ev)
        return kept

    def _apply_cli_review_overrides(
        self,
        args: argparse.Namespace,
        raw_events: list[Event],
    ) -> list[Event]:
        if self.overrides_watcher is None:
            return raw_events
        mutes = list(getattr(args, "mute", None) or [])
        unmutes = list(getattr(args, "unmute", None) or [])
        triage_items = list(getattr(args, "record_triage", None) or [])
        if not (mutes or unmutes or triage_items):
            return raw_events
        ov_path = self.overrides_watcher.path
        overrides = load_overrides(ov_path)
        now_ts = self.now()
        for raw_ref in mutes:
            norm_key, snap = self._resolve_snapshot_for_override(raw_ref, args.repo)
            mute_pr(overrides, norm_key, snap, self.viewer, now_iso=now_ts)
        for raw_ref in unmutes:
            norm_key = normalize_override_key(raw_ref, default_repo=args.repo)
            unmute_pr(overrides, norm_key, now_iso=now_ts)
        self._record_cli_triage_items(
            overrides,
            triage_items,
            self._parse_triage_summaries(args),
            args.repo,
            now_ts,
        )
        save_overrides(ov_path, overrides)
        self.overrides_watcher = OverridesWatcher(ov_path)
        return self._filter_unmuted_events(raw_events, overrides)

    def _init_run_state(
        self, args: argparse.Namespace
    ) -> tuple[set[str] | None, list[Event]]:
        self._install_signal_handlers()
        if self.notifier is None:
            self.notifier = resolve_notifier()
        self.role = getattr(
            args,
            "role",
            ReviewRole.REVIEWER
            if getattr(args, "reviews", False)
            else ReviewRole.AUTHOR,
        )
        self.membership = getattr(args, "membership", None) or _membership_for_role(
            self.role
        )
        self.wake_on_review = bool(getattr(args, "wake_on_review", False))
        self.wake = bool(getattr(args, "wake", False))
        self.stale_days = int(getattr(args, "stale_days", DEFAULT_STALE_DAYS))
        self.repo = args.repo
        allowed_kinds = parse_event_list(args.events, role=self.role)
        self.state = load_state(self.state_path)
        apply_cli_overrides(args, self.state)
        self.packrat = self._select_source(args)
        self.source_mode = "packrat" if self.packrat is not None else "github"
        self._source_note = "via packrat" if self.packrat is not None else None
        self.notifications_enabled = not bool(args.no_notifications)
        if self.membership == Membership.MINE:
            self.overrides_watcher = None
            return allowed_kinds, self._init_mine_targets(args)
        if self.membership == Membership.REVIEW_QUEUE:
            raw_ov_path = getattr(args, "overrides_file", None)
            ov_path = (
                Path(raw_ov_path).expanduser()
                if raw_ov_path
                else default_overrides_path()
            )
            self.overrides_watcher = OverridesWatcher(ov_path)
            raw_events = self._init_reviewer_targets()
            return allowed_kinds, self._apply_cli_review_overrides(args, raw_events)
        self.overrides_watcher = None
        self.watched_keys = resolve_targets(args, self.runner, self.state, self.packrat)
        if self.packrat is not None:
            try:
                self._start_packrat()
                return allowed_kinds, self.refresh()
            except PackratError as exc:
                timers = _Timers(next_poll=0.0, next_sweep=0.0, next_notify=0.0)
                self._fallback_to_github(self.clock(), timers, str(exc))
        return allowed_kinds, self.refresh()

    def _setup_daemon_mode(self, args: argparse.Namespace) -> int | None:
        if not self.notifier and not self._silent_daemon:
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
            Path(args.control)
            if args.control
            else (self.state_path.parent / CONTROL_FILE)
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

    def _startup_wake_events(
        self, matched: list[Event], args: argparse.Namespace
    ) -> list[Event]:
        """Returns the startup events that open the first debounce window.

        Events that open no window update the board and ``state.json`` here.
        """
        if self.membership == Membership.MINE:
            return self._mine_startup_wake(matched, args)
        wake_events = _select_wake_events(matched, bool(args.actionable_only))
        if not wake_events:
            self._sync_board(matched, args)
            save_state(self.state_path, self.state)
        return wake_events

    def run(self, args: argparse.Namespace) -> int:
        """Executes ``monitor_prs.py`` from parsed arguments and returns its exit code."""
        try:
            allowed_kinds, raw_events = self._init_run_state(args)
        except (ValueError, StateError, GhError, PackratError) as exc:
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

        wake_events = self._startup_wake_events(matched, args)
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
