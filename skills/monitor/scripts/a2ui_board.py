"""Builds A2UI stack_board data models, Express DSL files, and live sidecar store updates."""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from models import Event, PrSnapshot

BASIC_CATALOG = "https://a2ui.org/specification/v1_0/catalogs/basic/catalog.json"
STATUS_CATALOG = "https://a2ui.org/specification/v1_0/catalogs/status/catalog.json"
VALID_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

BOARD_STEPS: list[dict[str, str]] = [
    {"label": "Draft", "detail": "Initial branch or work in progress"},
    {"label": "Open", "detail": "Open for review and CI checks"},
    {"label": "Partially Ready", "detail": "Review approved or CI checks passing"},
    {"label": "Ready", "detail": "Approved and CI green"},
    {"label": "Merged", "detail": "Landed on base branch"},
]

STACK_BOARD_COMPONENTS: list[dict[str, Any]] = [
    {
        "id": "root",
        "component": "Card",
        "catalogId": BASIC_CATALOG,
        "child": "stack_col",
    },
    {
        "id": "stack_col",
        "component": "Column",
        "catalogId": BASIC_CATALOG,
        "children": [
            "stack_header",
            "stack_subtitle",
            "stack_progress",
            "stack_divider",
            "stack_rows",
            "stack_footer",
        ],
    },
    {
        "id": "stack_header",
        "component": "Row",
        "catalogId": BASIC_CATALOG,
        "children": ["stack_title", "stack_status"],
        "justify": "spaceBetween",
        "align": "center",
    },
    {
        "id": "stack_title",
        "component": "Text",
        "catalogId": BASIC_CATALOG,
        "text": {"@path": "/title"},
        "variant": "body",
        "weight": 1,
    },
    {
        "id": "stack_status",
        "component": "Badge",
        "catalogId": STATUS_CATALOG,
        "text": {"@path": "/status"},
        "tone": {"@path": "/statusTone"},
        "pulse": {"@path": "/statusPulse"},
    },
    {
        "id": "stack_subtitle",
        "component": "Text",
        "catalogId": BASIC_CATALOG,
        "text": {"@path": "/subtitle"},
        "variant": "caption",
    },
    {
        "id": "stack_progress",
        "component": "ProgressBar",
        "catalogId": STATUS_CATALOG,
        "items": {"@path": "/items"},
    },
    {"id": "stack_divider", "component": "Divider", "catalogId": BASIC_CATALOG},
    {
        "id": "stack_rows",
        "component": "List",
        "catalogId": BASIC_CATALOG,
        "children": {"path": "/items", "componentId": "stack_row"},
    },
    {
        "id": "stack_row",
        "component": "Column",
        "catalogId": BASIC_CATALOG,
        "children": [
            "stack_row_head",
            "stack_row_link",
            "stack_row_meta",
            "stack_row_details",
            "stack_row_steps",
        ],
    },
    {
        "id": "stack_row_head",
        "component": "Row",
        "catalogId": BASIC_CATALOG,
        "children": [
            "stack_row_id",
            "stack_row_state",
            "stack_row_ci",
            "stack_row_review",
        ],
        "align": "center",
    },
    {
        "id": "stack_row_id",
        "component": "Text",
        "catalogId": BASIC_CATALOG,
        "text": {
            "@call": "formatString",
            "catalogId": BASIC_CATALOG,
            "args": {"value": "**${id}**"},
        },
    },
    {
        "id": "stack_row_state",
        "component": "Badge",
        "catalogId": STATUS_CATALOG,
        "text": {"@path": "state"},
        "tone": {"@path": "tone"},
    },
    {
        "id": "stack_row_ci",
        "component": "Badge",
        "catalogId": STATUS_CATALOG,
        "text": {"@path": "ci"},
        "tone": {"@path": "ciTone"},
    },
    {
        "id": "stack_row_review",
        "component": "Badge",
        "catalogId": STATUS_CATALOG,
        "text": {"@path": "review"},
        "tone": {"@path": "reviewTone"},
    },
    {
        "id": "stack_row_link",
        "component": "Text",
        "catalogId": BASIC_CATALOG,
        "text": {
            "@call": "formatString",
            "catalogId": BASIC_CATALOG,
            "args": {"value": "[${title}](${url})"},
        },
    },
    {
        "id": "stack_row_meta",
        "component": "Text",
        "catalogId": BASIC_CATALOG,
        "text": {
            "@call": "formatString",
            "catalogId": BASIC_CATALOG,
            "args": {"value": "wave ${wave} · base ${base}"},
        },
        "variant": "caption",
    },
    {
        "id": "stack_row_details",
        "component": "Text",
        "catalogId": BASIC_CATALOG,
        "text": {"@path": "detail"},
        "variant": "caption",
    },
    {
        "id": "stack_row_steps",
        "component": "Stepper",
        "catalogId": STATUS_CATALOG,
        "steps": {"@path": "/steps"},
        "current": {"@path": "step"},
        "tone": {"@path": "tone"},
    },
    {
        "id": "stack_footer",
        "component": "Row",
        "catalogId": BASIC_CATALOG,
        "children": ["ack_btn"],
        "justify": "end",
        "align": "center",
    },
    {
        "id": "ack_btn",
        "component": "Button",
        "catalogId": BASIC_CATALOG,
        "child": "ack_btn_child",
        "action": {"event": {"name": "acknowledge_stack_board", "context": {"_oneShot": True}}},
    },
    {
        "id": "ack_btn_child",
        "component": "Text",
        "catalogId": BASIC_CATALOG,
        "text": {"@path": "/ackLabel"},
    },
]


def lifecycle_step(snap: PrSnapshot) -> int:
    """Maps a ``PrSnapshot`` to its 0..4 lifecycle step index on ``stack_board``."""
    if snap.state == "MERGED":
        return 4
    if snap.state == "CLOSED" or snap.draft:
        return 0
    ci_green = snap.required_summary() == "SUCCESS" or (
        snap.required_summary() == "NONE" and snap.rollup_state == "SUCCESS"
    )
    approved = (
        (snap.review_decision == "APPROVED" or bool(snap.approvals))
        and snap.review_decision != "CHANGES_REQUESTED"
        and not snap.changes_requested
    )
    if approved and ci_green:
        return 3
    if approved or ci_green:
        return 2
    return 1


def _state_badge(snap: PrSnapshot) -> tuple[str, str]:
    if snap.state == "MERGED":
        return "Merged", "success"
    if snap.state == "CLOSED":
        return "Closed", "neutral"
    if snap.draft:
        return "Draft", "neutral"
    if snap.mergeable == "CONFLICTING" or snap.merge_state == "DIRTY":
        return "Conflicts", "danger"
    if snap.merge_state == "BEHIND":
        return "Behind", "warning"
    if not snap.mine:
        return "Watching", "neutral"
    return "Open", "active"


def _format_eta(eta_s: int | None) -> str:
    if eta_s is None or eta_s <= 0:
        return ""
    if eta_s < 60:
        return f", ETA {eta_s}s"
    minutes = max(1, round(eta_s / 60))
    return f", ETA {minutes}m"


def _ci_badge(snap: PrSnapshot, eta_s: int | None) -> tuple[str, str]:
    summary = snap.required_summary()
    if summary != "NONE":
        if summary == "FAILURE":
            return "CI failed", "danger"
        if summary == "PENDING":
            return f"CI running{_format_eta(eta_s)}", "active"
        return "CI passed", "success"
    if snap.rollup_state in ("FAILURE", "ERROR"):
        return "CI failed", "danger"
    if snap.rollup_state == "PENDING":
        return f"CI running{_format_eta(eta_s)}", "active"
    if snap.rollup_state == "SUCCESS":
        return "CI passed", "success"
    return "No CI", "neutral"


def _review_badge(snap: PrSnapshot) -> tuple[str, str]:
    if snap.review_decision == "CHANGES_REQUESTED" or snap.changes_requested:
        return "Changes requested", "danger"
    unresolved = sum(1 for t in snap.threads if not t.resolved)
    if unresolved > 0:
        return f"{unresolved} unresolved", "warning"
    if snap.review_decision == "APPROVED" or snap.approvals:
        return "Approved", "success"
    if snap.review_decision == "REVIEW_REQUIRED" or snap.requested_reviewers:
        return "Review required", "warning"
    return "Unreviewed", "neutral"


def _stack_wave(snap: PrSnapshot, snapshots: dict[str, PrSnapshot]) -> int:
    depth = 1
    seen: set[str] = {snap.key}
    cur = snap
    while cur.stack_parent and cur.stack_parent in snapshots and cur.stack_parent not in seen:
        seen.add(cur.stack_parent)
        cur = snapshots[cur.stack_parent]
        depth += 1
    return depth


def _detail_bullets(snap: PrSnapshot, pr_events: list[Event]) -> str:
    bullets: list[str] = []
    seen: set[str] = set()

    def add(text: str) -> None:
        cleaned = text.strip()
        if cleaned.startswith("- "):
            cleaned = cleaned[2:].strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            bullets.append(f"- {cleaned}")

    for ev in pr_events:
        add(ev.summary)
    for check in snap.effective_required_checks():
        if check.is_failed():
            add(f"CI failed ({check.name})")
    if not snap.mine and not bullets:
        add("Watch only")
    if not bullets and snap.head_sha:
        add(f"Head `{snap.head_sha[:7]}`")
    return "\n".join(bullets)


def item_for(
    snap: PrSnapshot,
    eta_s: int | None = None,
    latest_event: Event | None = None,
    wave: int = 1,
    events: list[Event] | None = None,
) -> dict[str, Any]:
    """Builds a single ``stack_board`` item dictionary for ``snap``."""
    state_text, state_tone = _state_badge(snap)
    ci_text, ci_tone = _ci_badge(snap, eta_s)
    rev_text, rev_tone = _review_badge(snap)
    base_label = f"PR {snap.stack_parent.split('#')[-1]}" if snap.stack_parent else snap.base_ref
    pr_events = (
        list(events) if events is not None else ([latest_event] if latest_event is not None else [])
    )
    detail = _detail_bullets(snap, pr_events)
    return {
        "id": f"PR {snap.number}",
        "title": snap.title,
        "url": snap.url,
        "wave": str(wave),
        "base": base_label,
        "state": state_text,
        "tone": state_tone,
        "ci": ci_text,
        "ciTone": ci_tone,
        "review": rev_text,
        "reviewTone": rev_tone,
        "detail": detail,
        "step": lifecycle_step(snap),
    }


def _format_utc_short(observed_at: str) -> str:
    raw = observed_at.strip()
    if "T" in raw:
        time_part = raw.split("T", 1)[1]
        hh_mm = time_part[:5]
        if len(hh_mm) == 5 and hh_mm[2] == ":":
            return f"{hh_mm} UTC"
    return raw


def _board_status_badge(
    snapshots: dict[str, PrSnapshot],
    eta_map: dict[str, int | None],
    observed_at: str,
) -> tuple[str, str, bool]:
    short_ts = _format_utc_short(observed_at)
    suffix = f" · {short_ts}" if short_ts else ""
    snaps = list(snapshots.values())
    if snaps and all(s.state in ("MERGED", "CLOSED") for s in snaps):
        return f"All merged{suffix}", "success", False
    any_pending = any(
        s.required_summary() == "PENDING"
        or (s.required_summary() == "NONE" and s.rollup_state == "PENDING")
        for s in snaps
    )
    if any_pending:
        etas = [v for v in eta_map.values() if isinstance(v, int) and v > 0]
        max_eta = max(etas) if etas else None
        return f"CI running{_format_eta(max_eta)}{suffix}", "active", True
    return f"Watching{suffix}", "active", True


def _board_subtitle(snapshots: dict[str, PrSnapshot], events: list[Event]) -> str:
    snaps = list(snapshots.values())
    if not snaps:
        return ""
    repos = sorted({s.repo for s in snaps if s.repo})
    if len(snaps) == 1:
        only = snaps[0]
        scope = (
            f"Watching `{only.repo}#{only.number}`" if only.repo else f"Watching PR {only.number}"
        )
    elif len(repos) == 1:
        first_num = snaps[0].number
        last_num = snaps[-1].number
        scope = f"{len(snaps)} PRs in `{repos[0]}` (`#{first_num}` -> `#{last_num}`)"
    else:
        scope = f"{len(snaps)} PRs across {len(repos)} repos"
    if events:
        changed_prs = len({ev.pr for ev in events})
        noun = "PR" if changed_prs == 1 else "PRs"
        return f"{scope} · {changed_prs} {noun} updated"
    return scope


def board_data_model(
    snapshots: dict[str, PrSnapshot],
    events: list[Event],
    eta: dict[str, int | None] | None = None,
    observed_at: str = "",
) -> dict[str, Any]:
    """Builds the ``stack_board`` dataModel dictionary for ``snapshots`` and ``events``."""
    eta_map = eta or {}
    events_by_pr: dict[str, list[Event]] = {}
    for ev in events:
        events_by_pr.setdefault(ev.pr, []).append(ev)
    items: list[dict[str, Any]] = []
    for key, snap in snapshots.items():
        wave = _stack_wave(snap, snapshots)
        items.append(
            item_for(
                snap,
                eta_s=eta_map.get(key),
                wave=wave,
                events=events_by_pr.get(key),
            )
        )
    status_text, status_tone, status_pulse = _board_status_badge(snapshots, eta_map, observed_at)
    subtitle = _board_subtitle(snapshots, events)
    return {
        "title": "### PR Monitor",
        "subtitle": subtitle,
        "status": status_text,
        "statusTone": status_tone,
        "statusPulse": status_pulse,
        "steps": BOARD_STEPS,
        "items": items,
        "ackLabel": "Acknowledge",
    }


def board_dsl(
    snapshots: dict[str, PrSnapshot],
    events: list[Event],
    eta: dict[str, int | None] | None = None,
    observed_at: str = "",
) -> str:
    """Renders A2UI Express data statements for the ``stack_board`` template."""
    dm = board_data_model(snapshots, events, eta=eta, observed_at=observed_at)
    lines = [
        f"$/title = {json.dumps(dm['title'])}",
        f"$/subtitle = {json.dumps(dm['subtitle'])}",
        f"$/status = {json.dumps(dm['status'])}",
        f"$/statusTone = {json.dumps(dm['statusTone'])}",
        f"$/statusPulse = {json.dumps(dm['statusPulse'])}",
        f"$/steps = {json.dumps(dm['steps'])}",
        f"$/items = {json.dumps(dm['items'])}",
        f"$/ackLabel = {json.dumps(dm['ackLabel'])}",
    ]
    return "\n".join(lines)


def write_board_file(path: str | os.PathLike[str], dsl: str) -> Path:
    """Atomically writes ``dsl`` to ``path`` and returns the resolved ``Path``."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp.{os.getpid()}")
    tmp.write_text(dsl + "\n", encoding="utf-8")
    os.replace(tmp, dest)
    return dest


def default_store_dir() -> Path | None:
    """Returns the installed ``a2ui-pane`` plugin data directory when present on disk.

    Checks, in order: ``$A2UI_PANE_DATA``, ``$PLUGIN_DATA``,
    ``$ANTIGRAVITY_APP_DATA_DIR/plugin_data/a2ui-pane`` (exported to agent shells by
    Jetski and Antigravity hosts, so the store resolves under whichever host is
    running), and finally ``~/.gemini/jetski/plugin_data/a2ui-pane`` for runs
    outside an agent shell.
    """
    candidates: list[Path] = []
    for name in ("A2UI_PANE_DATA", "PLUGIN_DATA"):
        env_dir = os.environ.get(name)
        if env_dir:
            candidates.append(Path(env_dir))
    app_data_dir = os.environ.get("ANTIGRAVITY_APP_DATA_DIR")
    if app_data_dir:
        candidates.append(Path(app_data_dir) / "plugin_data" / "a2ui-pane")
    candidates.append(Path.home() / ".gemini" / "jetski" / "plugin_data" / "a2ui-pane")
    return next((candidate for candidate in candidates if candidate.exists()), None)


def _parse_store_line(raw: str) -> dict[str, Any] | None:
    line = raw.strip()
    if not line:
        return None
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


# Pane store contract: a2ui-pane docs/store-format.md. Each surface is one file holding one
# StoreLine; writers take `<convId>/.lock` (stale locks are removed only under the
# `.lock.steal` guard), replace files through tmp + rename, and write `_seq` last.
STORE_VERSION = "1.0"
LOCK_TIMEOUT_S = 2.0
LOCK_RETRY_S = 0.005
STALE_LOCK_S = 10.0
LEGACY_WAIT_S = 2.0
LEGACY_POLL_S = 0.05
SURFACE_FILE_RE = re.compile(r"^[A-Za-z0-9-][A-Za-z0-9_-]*\.jsonl$")
CALL_ID_PREFIX = "call_monitor"

StoreStatus = Literal["written", "unchanged", "dismissed", "unavailable"]


class StoreLockTimeout(Exception):
    """Raised when the conversation lock cannot be taken within ``LOCK_TIMEOUT_S``."""


def _lock_is_stale(path: Path) -> bool:
    try:
        return time.time() - path.stat().st_mtime > STALE_LOCK_S
    except FileNotFoundError:
        return False


def _try_create_lock(lock_path: Path, token: str) -> bool:
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    try:
        try:
            os.write(fd, f"{os.getpid()} {stamp} {token}\n".encode())
        finally:
            os.close(fd)
    except BaseException:
        # A tokenless lock would block every writer until it goes stale.
        _unlink_quietly(lock_path)
        raise
    return True


def _unlink_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _steal_stale_lock(lock_path: Path) -> bool:
    """Removes a stale lock while holding the ``.lock.steal`` guard.

    Returns True only when this call removed ``.lock``, so the caller may retry at once.
    The re-check under the guard keeps a waiter with an outdated staleness check from
    removing a fresh lock.
    """
    guard = lock_path.with_name(".lock.steal")
    try:
        os.close(os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        # Another waiter is stealing; a guard older than the stale limit lost its owner.
        if _lock_is_stale(guard):
            _unlink_quietly(guard)
        return False
    try:
        if not _lock_is_stale(lock_path):
            return False
        try:
            os.unlink(lock_path)
        except FileNotFoundError:
            return False
        return True
    finally:
        _unlink_quietly(guard)


def _release_lock(lock_path: Path, token: str) -> None:
    try:
        fields = lock_path.read_text(encoding="utf-8").split()
    except FileNotFoundError:
        return
    if fields[2:3] == [token]:
        _unlink_quietly(lock_path)


@contextmanager
def _conversation_lock(conv_dir: Path) -> Iterator[None]:
    """Holds ``conv_dir/.lock`` for the body; raises ``StoreLockTimeout`` after 2 s."""
    conv_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = conv_dir / ".lock"
    token = secrets.token_hex(8)
    deadline = time.monotonic() + LOCK_TIMEOUT_S
    while not _try_create_lock(lock_path, token):
        if _lock_is_stale(lock_path) and _steal_stale_lock(lock_path):
            continue
        if time.monotonic() >= deadline:
            raise StoreLockTimeout(f"timed out waiting for {lock_path}")
        time.sleep(LOCK_RETRY_S)
    try:
        yield
    finally:
        _release_lock(lock_path, token)


def _write_atomic(dest: Path, tmp_name: str, text: str) -> None:
    tmp = dest.with_name(tmp_name)
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, dest)


def _read_seq(conv_dir: Path) -> int:
    try:
        return max(int((conv_dir / "_seq").read_text(encoding="utf-8").strip() or 0), 0)
    except (FileNotFoundError, ValueError):
        return 0


def _read_surface_line(path: Path) -> dict[str, Any] | None:
    """Returns the surface's StoreLine, ``{}`` for an unreadable file, or None when missing."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    lines = raw.splitlines()
    return (_parse_store_line(lines[0]) if lines else None) or {}


def _line_seq(line: dict[str, Any] | None) -> int:
    seq = (line or {}).get("seq")
    return seq if isinstance(seq, int) and seq > 0 else 0


def _stored_create(line: dict[str, Any]) -> dict[str, Any]:
    msg = line.get("message")
    create = msg.get("createSurface") if isinstance(msg, dict) else None
    return create if isinstance(create, dict) else {}


def _is_unchanged(line: dict[str, Any], data_model: dict[str, Any]) -> bool:
    """Compares only components and ``items``: ``status`` carries a minute timestamp."""
    create = _stored_create(line)
    stored_dm = create.get("dataModel")
    return (
        create.get("components") == STACK_BOARD_COMPONENTS
        and isinstance(stored_dm, dict)
        and stored_dm.get("items") == data_model.get("items")
    )


def _foreign_surface_files(conv_dir: Path, surface_id: str) -> list[Path]:
    own = f"{surface_id}.jsonl"
    return sorted(
        entry
        for entry in conv_dir.iterdir()
        if entry.name != own and SURFACE_FILE_RE.match(entry.name) and entry.is_file()
    )


def _await_migration(conv_root: Path, conversation_id: str) -> bool:
    """Waits, without the lock, for the TS migrator to fold a legacy flat file.

    Returns False when ``<convId>.jsonl`` still has no ``<convId>/_seq`` after
    ``LEGACY_WAIT_S``. Python never migrates legacy files itself.
    """
    legacy = conv_root / f"{conversation_id}.jsonl"
    seq_file = conv_root / conversation_id / "_seq"
    deadline = time.monotonic() + LEGACY_WAIT_S
    while legacy.exists() and not seq_file.exists():
        if time.monotonic() >= deadline:
            return False
        time.sleep(LEGACY_POLL_S)
    return True


def _commit_board(
    conv_dir: Path,
    surface_id: str,
    data_model: dict[str, Any],
    ts: str,
    force: bool,
    allow_create: bool,
) -> StoreStatus:
    """Replaces the board's surface file; must run under ``_conversation_lock``."""
    surface_path = conv_dir / f"{surface_id}.jsonl"
    current = _read_surface_line(surface_path)
    if current is None and not allow_create:
        return "dismissed"
    if current is not None and not force and _is_unchanged(current, data_model):
        return "unchanged"
    # Creating the board keeps it the sole surface, as the append-only store did.
    removed = _foreign_surface_files(conv_dir, surface_id) if current is None else []
    seq = max(_read_seq(conv_dir), _line_seq(current)) + len(removed) + 1
    record = {
        "seq": seq,
        "callId": f"{CALL_ID_PREFIX}_{seq}",
        "ts": ts,
        "message": {
            "version": STORE_VERSION,
            "createSurface": {
                "surfaceId": surface_id,
                "sendDataModel": True,
                "components": STACK_BOARD_COMPONENTS,
                "dataModel": data_model,
            },
        },
    }
    pid = os.getpid()
    line = json.dumps(record, separators=(",", ":")) + "\n"
    _write_atomic(surface_path, f".{surface_id}.tmp-{pid}", line)
    for path in removed:
        _unlink_quietly(path)
    _write_atomic(conv_dir / "_seq", f"._seq.tmp-{pid}", f"{seq}\n")
    return "written"


def sync_pane_store(
    conversation_id: str | None,
    data_model: dict[str, Any],
    store_dir: str | os.PathLike[str] | None = None,
    surface_id: str = "monitor",
    observed_at: str = "",
    force: bool = False,
    allow_create: bool = True,
) -> StoreStatus:
    """Replaces ``conversations/<convId>/<surface_id>.jsonl`` with the board's full snapshot.

    Returns ``"written"`` after a write, ``"unchanged"`` when the stored components and
    ``items`` already match (unless ``force``), ``"dismissed"`` when the surface file is
    missing and ``allow_create`` is false, and ``"unavailable"`` when there is no store,
    the ids are invalid, a legacy flat file is still waiting for migration, or the lock
    times out. Callers fall back to ``board.a2ui`` and ``render_a2ui`` on ``"unavailable"``.
    """
    if not conversation_id or not VALID_ID_RE.match(conversation_id):
        return "unavailable"
    if not VALID_ID_RE.match(surface_id) or surface_id[0] in "_.":
        return "unavailable"
    base_dir = Path(store_dir) if store_dir is not None else default_store_dir()
    if base_dir is None:
        return "unavailable"
    conv_root = base_dir / "conversations"
    if not _await_migration(conv_root, conversation_id):
        return "unavailable"
    conv_dir = conv_root / conversation_id
    if not allow_create and not conv_dir.is_dir():
        return "dismissed"
    ts = observed_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with _conversation_lock(conv_dir):
            return _commit_board(conv_dir, surface_id, data_model, ts, force, allow_create)
    except StoreLockTimeout:
        return "unavailable"
