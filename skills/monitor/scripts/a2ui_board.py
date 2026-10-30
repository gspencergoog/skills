"""Builds A2UI stack_board data models, Express DSL files, and live sidecar store updates."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    if summary == "FAILURE" or snap.rollup_state in ("FAILURE", "ERROR"):
        return "CI failed", "danger"
    if summary == "PENDING" or snap.rollup_state == "PENDING":
        return f"CI running{_format_eta(eta_s)}", "active"
    if summary == "SUCCESS" or snap.rollup_state == "SUCCESS":
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
        s.required_summary() == "PENDING" or s.rollup_state == "PENDING" for s in snaps
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
    """Returns the installed ``a2ui-pane`` plugin data directory when present on disk."""
    env_dir = os.environ.get("A2UI_PANE_DATA") or os.environ.get("PLUGIN_DATA")
    if env_dir:
        candidate = Path(env_dir)
        if candidate.exists():
            return candidate
    fallback = Path.home() / ".gemini" / "jetski" / "plugin_data" / "a2ui-pane"
    return fallback if fallback.exists() else None


def _parse_store_line(raw: str) -> dict[str, Any] | None:
    line = raw.strip()
    if not line:
        return None
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _track_other_surface(
    msg: dict[str, Any], surface_id: str, stale_surfaces: dict[str, None]
) -> None:
    create = msg.get("createSurface")
    if isinstance(create, dict):
        sid = create.get("surfaceId")
        if isinstance(sid, str) and sid and sid != surface_id:
            stale_surfaces[sid] = None
        return
    delete = msg.get("deleteSurface")
    if isinstance(delete, dict):
        sid = delete.get("surfaceId")
        if isinstance(sid, str) and sid != surface_id:
            stale_surfaces.pop(sid, None)


def _apply_store_message(
    msg: dict[str, Any], surface_id: str, exists: bool, current_schema: bool, last_items: Any
) -> tuple[bool, bool, Any]:
    create = msg.get("createSurface")
    if isinstance(create, dict) and create.get("surfaceId") == surface_id:
        dm = create.get("dataModel")
        items = dm.get("items") if isinstance(dm, dict) else None
        matches_schema = create.get("components") == STACK_BOARD_COMPONENTS
        return True, matches_schema, items
    delete = msg.get("deleteSurface")
    if isinstance(delete, dict) and delete.get("surfaceId") == surface_id:
        return False, False, None
    update = msg.get("updateDataModel")
    if (
        exists
        and isinstance(update, dict)
        and update.get("surfaceId") == surface_id
        and update.get("path") == "/items"
    ):
        return exists, current_schema, update.get("value")
    return exists, current_schema, last_items


def _inspect_store_file(conv_file: Path, surface_id: str) -> tuple[int, bool, bool, Any, list[str]]:
    if not conv_file.exists():
        return 0, False, False, None, []
    last_seq = 0
    exists = False
    current_schema = False
    last_items: Any = None
    stale_surfaces: dict[str, None] = {}
    for raw in conv_file.read_text(encoding="utf-8", errors="replace").splitlines():
        entry = _parse_store_line(raw)
        if entry is None:
            continue
        seq = entry.get("seq")
        if isinstance(seq, int) and seq > last_seq:
            last_seq = seq
        msg = entry.get("message")
        if isinstance(msg, dict):
            _track_other_surface(msg, surface_id, stale_surfaces)
            exists, current_schema, last_items = _apply_store_message(
                msg, surface_id, exists, current_schema, last_items
            )
    return last_seq, exists, current_schema, last_items, list(stale_surfaces)


def _build_store_messages(
    exists: bool,
    current_schema: bool,
    surface_id: str,
    data_model: dict[str, Any],
    stale_surfaces: list[str],
    should_update_items: bool,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {"version": "v1.0", "deleteSurface": {"surfaceId": sid}} for sid in stale_surfaces
    ]
    if not exists or not current_schema:
        if exists:
            messages.append({"version": "v1.0", "deleteSurface": {"surfaceId": surface_id}})
        messages.append(
            {
                "version": "v1.0",
                "createSurface": {
                    "surfaceId": surface_id,
                    "sendDataModel": True,
                    "components": STACK_BOARD_COMPONENTS,
                    "dataModel": data_model,
                },
            }
        )
        return messages
    if should_update_items:
        for key in ("status", "statusTone", "statusPulse", "subtitle", "items"):
            if key in data_model:
                messages.append(
                    {
                        "version": "v1.0",
                        "updateDataModel": {
                            "surfaceId": surface_id,
                            "path": f"/{key}",
                            "value": data_model[key],
                        },
                    }
                )
    return messages


def sync_pane_store(
    conversation_id: str | None,
    data_model: dict[str, Any],
    store_dir: str | os.PathLike[str] | None = None,
    surface_id: str = "monitor",
    observed_at: str = "",
    force: bool = False,
) -> bool:
    """Appends ``createSurface`` or ``updateDataModel`` lines to the conversation JSONL store."""
    if not conversation_id or not VALID_ID_RE.match(conversation_id):
        return False
    if not VALID_ID_RE.match(surface_id):
        return False
    base_dir = Path(store_dir) if store_dir is not None else default_store_dir()
    if base_dir is None:
        return False

    conv_dir = base_dir / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)
    conv_file = conv_dir / f"{conversation_id}.jsonl"
    last_seq, exists, current_schema, last_items, stale_surfaces = _inspect_store_file(
        conv_file, surface_id
    )
    should_update_items = force or last_items != data_model.get("items")
    messages = _build_store_messages(
        exists,
        current_schema,
        surface_id,
        data_model,
        stale_surfaces,
        should_update_items,
    )
    if not messages:
        return False
    ts = observed_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    call_id = f"call_monitor_{last_seq + 1}"
    lines: list[str] = []
    for idx, msg in enumerate(messages, start=1):
        record = {
            "seq": last_seq + idx,
            "callId": call_id,
            "ts": ts,
            "message": msg,
        }
        lines.append(json.dumps(record, separators=(",", ":")))
    with conv_file.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return True
