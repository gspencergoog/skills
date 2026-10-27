"""Builds A2UI stack_board data models, Express DSL files, and live sidecar store updates."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from diff_events import summarize
from models import Event, PrSnapshot

BASIC_CATALOG = "https://a2ui.org/specification/v1_0/catalogs/basic/catalog.json"
STATUS_CATALOG = "https://a2ui.org/specification/v1_0/catalogs/status/catalog.json"
VALID_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

BOARD_STEPS: list[dict[str, str]] = [
    {"label": "Draft", "detail": "Initial branch or work in progress"},
    {"label": "Review", "detail": "Open for review"},
    {"label": "CI", "detail": "Required checks passing"},
    {"label": "Approved", "detail": "Approved and CI green"},
    {"label": "Merged", "detail": "Landed on base branch"},
]

STACK_BOARD_COMPONENTS: list[dict[str, Any]] = [
    {"id": "root", "component": "Card", "catalogId": BASIC_CATALOG, "child": "stack_col"},
    {
        "id": "stack_col",
        "component": "Column",
        "catalogId": BASIC_CATALOG,
        "children": [
            "stack_title",
            "stack_progress",
            "stack_divider",
            "stack_rows",
            "stack_footer",
        ],
    },
    {
        "id": "stack_title",
        "component": "Text",
        "catalogId": BASIC_CATALOG,
        "text": {"@path": "/title"},
    },
    {
        "id": "stack_progress",
        "component": "ProgressBar",
        "catalogId": STATUS_CATALOG,
        "items": {"@path": "/items"},
        "summary": {"@path": "/subtitle"},
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
        "children": ["stack_row_id", "stack_row_state", "stack_row_ci", "stack_row_review"],
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
        "component": "Collapsible",
        "catalogId": STATUS_CATALOG,
        "title": "Details",
        "child": "stack_row_detail_text",
    },
    {
        "id": "stack_row_detail_text",
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
    if snap.review_decision == "APPROVED" and ci_green:
        return 3
    if ci_green:
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
    unresolved_human = sum(1 for t in snap.threads if not t.resolved and not t.last_author_is_bot)
    if unresolved_human > 0:
        return f"{unresolved_human} unresolved", "warning"
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


def item_for(
    snap: PrSnapshot,
    eta_s: int | None = None,
    latest_event: Event | None = None,
    wave: int = 1,
) -> dict[str, Any]:
    """Builds a single ``stack_board`` item dictionary for ``snap``."""
    state_text, state_tone = _state_badge(snap)
    ci_text, ci_tone = _ci_badge(snap, eta_s)
    rev_text, rev_tone = _review_badge(snap)
    base_label = f"PR {snap.stack_parent.split('#')[-1]}" if snap.stack_parent else snap.base_ref
    if latest_event is not None:
        detail = latest_event.summary
    elif not snap.mine:
        detail = "Watch only"
    else:
        detail = f"Head {snap.head_sha[:7]}" if snap.head_sha else ""
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


def board_data_model(
    snapshots: dict[str, PrSnapshot],
    events: list[Event],
    eta: dict[str, int | None] | None = None,
    observed_at: str = "",
) -> dict[str, Any]:
    """Builds the ``stack_board`` dataModel dictionary for ``snapshots`` and ``events``."""
    eta_map = eta or {}
    latest_by_pr: dict[str, Event] = {}
    for ev in events:
        latest_by_pr[ev.pr] = ev
    items: list[dict[str, Any]] = []
    for key, snap in snapshots.items():
        wave = _stack_wave(snap, snapshots)
        items.append(
            item_for(
                snap,
                eta_s=eta_map.get(key),
                latest_event=latest_by_pr.get(key),
                wave=wave,
            )
        )
    subtitle_core = summarize(events)
    subtitle = f"{observed_at} • {subtitle_core}" if observed_at else subtitle_core
    return {
        "title": "PR Monitor",
        "subtitle": subtitle,
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


def _apply_store_message(
    msg: dict[str, Any], surface_id: str, live: bool, last_items: Any
) -> tuple[bool, Any]:
    create = msg.get("createSurface")
    if isinstance(create, dict) and create.get("surfaceId") == surface_id:
        dm = create.get("dataModel")
        items = dm.get("items") if isinstance(dm, dict) else None
        return True, items
    delete = msg.get("deleteSurface")
    if isinstance(delete, dict) and delete.get("surfaceId") == surface_id:
        return False, None
    update = msg.get("updateDataModel")
    if (
        live
        and isinstance(update, dict)
        and update.get("surfaceId") == surface_id
        and update.get("path") == "/items"
    ):
        return True, update.get("value")
    return live, last_items


def _inspect_store_file(conv_file: Path, surface_id: str) -> tuple[int, bool, Any]:
    if not conv_file.exists():
        return 0, False, None
    last_seq = 0
    live = False
    last_items: Any = None
    for raw in conv_file.read_text(encoding="utf-8", errors="replace").splitlines():
        entry = _parse_store_line(raw)
        if entry is None:
            continue
        seq = entry.get("seq")
        if isinstance(seq, int) and seq > last_seq:
            last_seq = seq
        msg = entry.get("message")
        if isinstance(msg, dict):
            live, last_items = _apply_store_message(msg, surface_id, live, last_items)
    return last_seq, live, last_items


def _build_store_messages(
    surface_live: bool, surface_id: str, data_model: dict[str, Any]
) -> list[dict[str, Any]]:
    if not surface_live:
        return [
            {
                "version": "v1.0",
                "createSurface": {
                    "surfaceId": surface_id,
                    "sendDataModel": True,
                    "components": STACK_BOARD_COMPONENTS,
                    "dataModel": data_model,
                },
            }
        ]
    return [
        {
            "version": "v1.0",
            "updateDataModel": {
                "surfaceId": surface_id,
                "path": "/subtitle",
                "value": data_model["subtitle"],
            },
        },
        {
            "version": "v1.0",
            "updateDataModel": {
                "surfaceId": surface_id,
                "path": "/items",
                "value": data_model["items"],
            },
        },
    ]


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
    last_seq, surface_live, last_items = _inspect_store_file(conv_file, surface_id)
    if surface_live and not force and last_items == data_model.get("items"):
        return False

    messages = _build_store_messages(surface_live, surface_id, data_model)
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
