"""Builds A2UI stack_board data models, Express DSL files, and live sidecar store updates."""

from __future__ import annotations

import json
import os
import re
import secrets
import statistics
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from models import (
    DrawerReason,
    Event,
    PrSnapshot,
    ReviewSortTier,
    ReviewStep,
    Thread,
    TriageEntry,
    TriageVerdict,
    classify_review_turn,
    dormant_age_days,
    effective_viewer_review,
    has_unanswered_direct_request,
    is_dormant_review,
    is_mute_active,
    viewer_thread_progress,
)
from review_overrides import ReviewOverrides

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

REVIEW_BOARD_STEPS: list[dict[str, str]] = [
    {"label": "Requested", "detail": "Awaiting your initial review"},
    {"label": "Waiting on Author", "detail": "Reviewed; waiting on author response"},
    {"label": "Re-Review Needed", "detail": "Author pushed changes or replied"},
    {"label": "Approved", "detail": "Approved by you"},
    {"label": "Merged", "detail": "Landed on base branch"},
]

REVIEW_BOARD_COMPONENTS: list[dict[str, Any]] = [
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
            "stack_drawer",
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
        "segments": {"@path": "/segments"},
        "summary": {"@path": "/progressSummary"},
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
            "triage_row_badge",
            "review_row_btn",
            "mute_row_btn",
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
        "id": "triage_row_badge",
        "component": "Badge",
        "catalogId": STATUS_CATALOG,
        "text": {"@path": "triageLabel"},
        "tone": {"@path": "triageTone"},
    },
    {
        "id": "review_row_btn",
        "component": "Button",
        "catalogId": BASIC_CATALOG,
        "child": "review_row_btn_label",
        "variant": "borderless",
        "action": {
            "event": {
                "name": "trigger_pr_review",
                "context": {
                    "pr": {"@path": "id"},
                    "key": {"@path": "key"},
                    "repo": {"@path": "repo"},
                    "number": {"@path": "number"},
                    "url": {"@path": "url"},
                    "headSha": {"@path": "headSha"},
                    "_oneShot": False,
                },
            }
        },
        "checks": [
            {
                "condition": {
                    "@call": "and",
                    "catalogId": BASIC_CATALOG,
                    "args": {
                        "values": [
                            {"@path": "canReview"},
                            True,
                        ]
                    },
                },
                "message": "PR is closed or merged",
            }
        ],
    },
    {
        "id": "review_row_btn_label",
        "component": "Text",
        "catalogId": BASIC_CATALOG,
        "text": {"@path": "actionLabel"},
    },
    {
        "id": "mute_row_btn",
        "component": "Button",
        "catalogId": BASIC_CATALOG,
        "child": "mute_row_btn_label",
        "variant": "borderless",
        "action": {
            "event": {
                "name": "mute_review_pr",
                "context": {
                    "pr": {"@path": "id"},
                    "key": {"@path": "key"},
                    "repo": {"@path": "repo"},
                    "number": {"@path": "number"},
                    "headSha": {"@path": "headSha"},
                    "updatedAt": {"@path": "updatedAt"},
                    "_oneShot": False,
                },
            }
        },
        "checks": [
            {
                "condition": {
                    "@call": "and",
                    "catalogId": BASIC_CATALOG,
                    "args": {
                        "values": [
                            {"@path": "canReview"},
                            True,
                        ]
                    },
                },
                "message": "PR is closed or merged",
            }
        ],
    },
    {
        "id": "mute_row_btn_label",
        "component": "Text",
        "catalogId": BASIC_CATALOG,
        "text": "Mute",
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
        "text": {"@path": "meta"},
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
        "id": "stack_drawer",
        "component": "Collapsible",
        "catalogId": STATUS_CATALOG,
        "title": {"@path": "/drawerTitle"},
        "summary": {"@path": "/drawerSummary"},
        "child": "drawer_rows",
    },
    {
        "id": "drawer_rows",
        "component": "List",
        "catalogId": BASIC_CATALOG,
        "children": {"path": "/drawerItems", "componentId": "drawer_row"},
    },
    {
        "id": "drawer_row",
        "component": "Row",
        "catalogId": BASIC_CATALOG,
        "children": [
            "drawer_row_id",
            "drawer_row_badge",
            "drawer_row_title",
            "unhide_row_btn",
        ],
        "align": "center",
    },
    {
        "id": "drawer_row_id",
        "component": "Text",
        "catalogId": BASIC_CATALOG,
        "text": {
            "@call": "formatString",
            "catalogId": BASIC_CATALOG,
            "args": {"value": "**${id}**"},
        },
    },
    {
        "id": "drawer_row_badge",
        "component": "Badge",
        "catalogId": STATUS_CATALOG,
        "text": {"@path": "drawerBadge"},
        "tone": {"@path": "drawerTone"},
    },
    {
        "id": "drawer_row_title",
        "component": "Text",
        "catalogId": BASIC_CATALOG,
        "text": {
            "@call": "formatString",
            "catalogId": BASIC_CATALOG,
            "args": {"value": "[${title}](${url})"},
        },
        "weight": 1,
    },
    {
        "id": "unhide_row_btn",
        "component": "Button",
        "catalogId": BASIC_CATALOG,
        "child": "unhide_row_btn_label",
        "variant": "borderless",
        "action": {
            "event": {
                "name": "unmute_review_pr",
                "context": {
                    "pr": {"@path": "pr"},
                    "key": {"@path": "key"},
                    "repo": {"@path": "repo"},
                    "number": {"@path": "number"},
                    "_oneShot": False,
                },
            }
        },
    },
    {
        "id": "unhide_row_btn_label",
        "component": "Text",
        "catalogId": BASIC_CATALOG,
        "text": "Unhide",
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


def _parse_iso_ts(raw: str | None) -> float:
    if not raw:
        return 0.0
    cleaned = raw.strip()
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _trace_chain(snap: PrSnapshot, by_key: Mapping[str, PrSnapshot]) -> list[str]:
    chain = [snap.key]
    seen = {snap.key}
    cur = snap
    while cur.stack_parent and cur.stack_parent in by_key and cur.stack_parent not in seen:
        seen.add(cur.stack_parent)
        cur = by_key[cur.stack_parent]
        chain.append(cur.key)
    return chain


def sort_review_snapshots(
    snapshots: Mapping[str, PrSnapshot] | Sequence[PrSnapshot],
    viewer: str,
) -> list[tuple[PrSnapshot, ReviewStep, ReviewSortTier, int]]:
    """Sorts reviewer snapshots by 6-tier priority, stack cluster recency, and wave."""
    snaps = list(snapshots.values()) if isinstance(snapshots, Mapping) else list(snapshots)
    by_key = {s.key: s for s in snaps}
    chains = {s.key: _trace_chain(s, by_key) for s in snaps}
    own_ts = {s.key: _parse_iso_ts(s.updated_at) for s in snaps}
    node_max = dict(own_ts)
    for key, chain in chains.items():
        for anc in chain:
            node_max[anc] = max(node_max[anc], own_ts[key])

    entries: list[
        tuple[
            tuple[int, float, int, float, str, int],
            PrSnapshot,
            ReviewStep,
            ReviewSortTier,
            int,
        ]
    ] = []
    for snap in snaps:
        step, tier = classify_review_turn(snap, viewer)
        chain = chains[snap.key]
        wave = len(chain) - 1
        cluster_max_ts = max(node_max[anc] for anc in chain)
        sort_key = (
            int(tier),
            -cluster_max_ts,
            wave,
            -own_ts[snap.key],
            snap.repo,
            snap.number,
        )
        entries.append((sort_key, snap, step, tier, wave))
    entries.sort(key=lambda item: item[0])
    return [(snap, step, tier, wave) for _, snap, step, tier, wave in entries]


def _is_review_ready(snap: PrSnapshot, step: ReviewStep) -> bool:
    ci_green = snap.required_summary() == "SUCCESS" or (
        snap.required_summary() == "NONE" and snap.rollup_state == "SUCCESS"
    )
    approved = (
        (step == ReviewStep.APPROVED or snap.review_decision == "APPROVED" or bool(snap.approvals))
        and snap.review_decision != "CHANGES_REQUESTED"
        and not snap.changes_requested
    )
    return approved and ci_green


def _review_state_badge(snap: PrSnapshot, step: ReviewStep) -> tuple[str, str]:
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
    if _is_review_ready(snap, step):
        return "Ready", "success"
    return "Open", "active"


def _is_viewer_approved_snap(snap: PrSnapshot, viewer_norm: str) -> bool:
    if snap.viewer_opinionated_state == "APPROVED":
        return True
    if snap.viewer_last_review is not None and snap.viewer_last_review.state == "APPROVED":
        return True
    return bool(viewer_norm) and any(a.lower() == viewer_norm for a in snap.approvals)


def _reviewer_badge(
    snap: PrSnapshot,
    step: ReviewStep,
    tier: ReviewSortTier,
    viewer_norm: str,
) -> tuple[str, str]:
    if tier == ReviewSortTier.TERMINAL:
        if _is_viewer_approved_snap(snap, viewer_norm):
            return "Approved by you", "success"
        if effective_viewer_review(snap, viewer_norm) is None:
            return "Unreviewed", "neutral"
        return "Waiting on author", "info"
    if step == ReviewStep.APPROVED:
        return "Approved by you", "success"
    if step == ReviewStep.RE_REVIEW_NEEDED:
        return "Re-review needed", "warning"
    if step == ReviewStep.WAITING_ON_AUTHOR:
        return "Waiting on author", "info"
    return "Review requested", "warning"


def _estimate_eta_s(
    snap: PrSnapshot,
    durations: Mapping[str, Sequence[int]] | None,
    now: float | None,
) -> int | None:
    if not durations:
        return None
    best: int | None = None
    for check in snap.effective_required_checks():
        if check.is_completed():
            continue
        samples = durations.get(f"{snap.repo}:{check.name}") or durations.get(check.name)
        if not samples:
            continue
        med = round(statistics.median(samples))
        start = _parse_iso_ts(check.started_at)
        elapsed = int(now - start) if start > 0 and now is not None and now > start else 0
        remaining = max(5, med - elapsed)
        best = remaining if best is None else max(best, remaining)
    return best


def _is_unread_reviewer_thread(
    thread: Thread,
    submitted_at: str,
    viewer_norm: str,
    restrict_to_viewer: bool,
) -> bool:
    if thread.resolved or thread.last_author_is_bot:
        return False
    if thread.last_author.lower() == viewer_norm:
        return False
    if restrict_to_viewer and not thread.has_participant(viewer_norm):
        return False
    if not submitted_at:
        return True
    return bool(thread.last_comment_at) and thread.last_comment_at > submitted_at


def _unread_thread_count(snap: PrSnapshot, viewer: str) -> int:
    viewer_norm = viewer.strip().lower()
    last = snap.viewer_last_review
    if last is None:
        return 0
    restrict_to_viewer = any(t.participants for t in snap.threads)
    return sum(
        1
        for t in snap.threads
        if _is_unread_reviewer_thread(t, last.submitted_at, viewer_norm, restrict_to_viewer)
    )


def _triage_badge(snap: PrSnapshot, triage: TriageEntry | None) -> tuple[str, str]:
    if (
        triage is None
        or not snap.discussion_watermark
        or triage.watermark != snap.discussion_watermark
    ):
        return "", "neutral"
    if triage.verdict == TriageVerdict.LIKELY_SHELVED:
        return "Likely shelved", "warning"
    if triage.verdict == TriageVerdict.DEFERRED:
        return "Deferred", "neutral"
    return "", "neutral"


def _thread_progress_bullet(snap: PrSnapshot, viewer: str) -> str:
    step, _ = classify_review_turn(snap, viewer)
    if step != ReviewStep.WAITING_ON_AUTHOR:
        return ""
    addressed, total = viewer_thread_progress(snap, viewer)
    if total <= 0:
        return ""
    if 0 < addressed < total:
        return f"- {addressed}/{total} of your threads updated; {total - addressed} awaiting author"
    last = effective_viewer_review(snap, viewer)
    has_new_head = (
        last is not None
        and bool(last.commit_oid)
        and bool(snap.head_sha)
        and snap.head_sha != last.commit_oid
    )
    if addressed == 0 and has_new_head:
        return f"- {total} of your threads awaiting author"
    return ""


def _triage_bullet(snap: PrSnapshot, triage: TriageEntry | None) -> str:
    label, _tone = _triage_badge(snap, triage)
    if not label or triage is None or not triage.summary.strip():
        return ""
    return f"- {label}: {triage.summary.strip()}"


def _has_answered_direct_request(snap: PrSnapshot, viewer: str) -> bool:
    viewer_norm = viewer.strip().lower()
    if not viewer_norm or not any(r.lower() == viewer_norm for r in snap.requested_reviewers):
        return False
    return not has_unanswered_direct_request(snap, viewer_norm)


def _review_fallback_bullet(snap: PrSnapshot) -> str:
    if snap.state == "MERGED":
        return "Merged"
    if snap.state == "CLOSED":
        return "Closed without merge"
    return f"Head `{snap.head_sha[:7]}`" if snap.head_sha else ""


def _dedup_bullets(candidates: Sequence[str]) -> str:
    bullets: list[str] = []
    seen: set[str] = set()
    for text in candidates:
        cleaned = text.strip().removeprefix("- ").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            bullets.append(f"- {cleaned}")
    return "\n".join(bullets)


def _review_detail_bullets(
    snap: PrSnapshot,
    viewer_norm: str,
    pr_events: Sequence[Event] | None = None,
    triage: TriageEntry | None = None,
) -> str:
    candidates: list[str] = [ev.summary for ev in (pr_events or ())]
    last = snap.viewer_last_review
    if last is not None and last.commit_oid and snap.head_sha and snap.head_sha != last.commit_oid:
        candidates.append(
            f"New commit `{snap.head_sha[:7]}` since your review (`{last.commit_oid[:7]}`)"
        )
    candidates.append(_thread_progress_bullet(snap, viewer_norm))
    if _has_answered_direct_request(snap, viewer_norm):
        candidates.append("- Replied in PR comments after review request")
    candidates.append(_triage_bullet(snap, triage))
    unread = _unread_thread_count(snap, viewer_norm)
    if unread > 0:
        noun = "thread reply" if unread == 1 else "thread replies"
        candidates.append(f"{unread} unread {noun}")
    if snap.draft:
        candidates.append("Draft PR — review requested")
    candidates.extend(
        f"CI failed ({check.name})"
        for check in snap.effective_required_checks()
        if check.is_failed()
    )
    result = _dedup_bullets(candidates)
    return result if result else _dedup_bullets([_review_fallback_bullet(snap)])


def _review_action_label(snap: PrSnapshot) -> str:
    if snap.state == "MERGED":
        return "Merged"
    if snap.state == "CLOSED":
        return "Closed"
    if snap.viewer_last_review is not None:
        return "Re-review"
    return "Review"


def _review_row_id(snap: PrSnapshot, *, multi_repo: bool) -> str:
    short_repo = snap.repo.split("/")[-1] if snap.repo else ""
    return f"{short_repo}#{snap.number}" if multi_repo and short_repo else f"PR {snap.number}"


def review_item_for(
    snap: PrSnapshot,
    viewer: str = "",
    *,
    multi_repo: bool = False,
    durations: Mapping[str, Sequence[int]] | None = None,
    now: float | None = None,
    wave: int = 0,
    events: Sequence[Event] | None = None,
    triage: TriageEntry | None = None,
) -> dict[str, Any]:
    """Builds a single reviewer queue board item dictionary for ``snap``."""
    viewer_norm = viewer.strip().lower()
    step, tier = classify_review_turn(snap, viewer)
    state_text, state_tone = _review_state_badge(snap, step)
    eta_s = _estimate_eta_s(snap, durations, now)
    ci_text, ci_tone = _ci_badge(snap, eta_s)
    rev_text, rev_tone = _reviewer_badge(snap, step, tier, viewer_norm)
    triage_label, triage_tone = _triage_badge(snap, triage)
    item_id = _review_row_id(snap, multi_repo=multi_repo)
    base_label = f"PR {snap.stack_parent.split('#')[-1]}" if snap.stack_parent else snap.base_ref
    return {
        "id": item_id,
        "key": snap.key,
        "number": snap.number,
        "repo": snap.repo,
        "url": snap.url,
        "title": snap.title,
        "author": snap.author,
        "meta": f"by @{snap.author} · {snap.repo} · base {snap.base_ref}",
        "headSha": snap.head_sha,
        "updatedAt": snap.updated_at,
        "wave": str(wave),
        "base": base_label,
        "state": state_text,
        "tone": state_tone,
        "ci": ci_text,
        "ciTone": ci_tone,
        "review": rev_text,
        "reviewTone": rev_tone,
        "triageLabel": triage_label,
        "triageTone": triage_tone,
        "step": int(step),
        "detail": _review_detail_bullets(snap, viewer_norm, events, triage=triage),
        "actionLabel": _review_action_label(snap),
        "canReview": snap.state == "OPEN",
    }


def drawer_item_for(
    snap: PrSnapshot,
    reason: DrawerReason,
    *,
    now_epoch: float,
    multi_repo: bool = False,
) -> dict[str, Any]:
    """Builds a compact collapsible-drawer item dictionary for a muted or dormant PR."""
    badge = (
        "Muted"
        if reason == DrawerReason.MUTED
        else f"Dormant · {dormant_age_days(snap, now_epoch)}d"
    )
    return {
        "id": _review_row_id(snap, multi_repo=multi_repo),
        "pr": snap.key,
        "key": snap.key,
        "repo": snap.repo,
        "number": snap.number,
        "title": snap.title,
        "url": snap.url,
        "drawerReason": reason.value,
        "drawerBadge": badge,
        "drawerTone": "neutral",
    }


_TIER_SEGMENT_LABEL: dict[ReviewSortTier, str] = {
    ReviewSortTier.RE_REVIEW_NEEDED: "Re-review",
    ReviewSortTier.REVIEW_REQUESTED: "Requested",
    ReviewSortTier.WAITING_ON_AUTHOR: "Waiting",
    ReviewSortTier.APPROVED_OPEN: "Approved",
    ReviewSortTier.DRAFT_REQUESTED: "Draft",
}

_SEGMENT_ORDER: tuple[tuple[str, str], ...] = (
    ("Re-review", "warning"),
    ("Requested", "warning"),
    ("Waiting", "info"),
    ("Approved", "success"),
    ("Merged", "success"),
    ("Draft", "neutral"),
    ("Closed", "neutral"),
)


def _segment_bucket_label(snap: PrSnapshot, tier: ReviewSortTier) -> str:
    if tier in _TIER_SEGMENT_LABEL:
        return _TIER_SEGMENT_LABEL[tier]
    return "Merged" if snap.state == "MERGED" else "Closed"


def _review_progress_segments(
    sorted_rows: Sequence[tuple[PrSnapshot, ReviewStep, ReviewSortTier, int]],
    drawer_count: int = 0,
) -> tuple[list[dict[str, Any]], str]:
    counts: dict[str, int] = {}
    for snap, _, tier, _ in sorted_rows:
        label = _segment_bucket_label(snap, tier)
        counts[label] = counts.get(label, 0) + 1
    segments = [
        {"label": label, "count": counts[label], "value": counts[label], "tone": tone}
        for label, tone in _SEGMENT_ORDER
        if counts.get(label, 0) > 0
    ]
    if drawer_count > 0:
        segments.append(
            {
                "label": "Muted / Dormant",
                "count": drawer_count,
                "value": drawer_count,
                "tone": "neutral",
            }
        )
    summary = (
        " · ".join(f"{seg['count']} {seg['label'].lower()}" for seg in segments)
        if segments
        else "0 PRs"
    )
    return segments, summary


def _review_status_badge(
    sorted_rows: Sequence[tuple[PrSnapshot, ReviewStep, ReviewSortTier, int]],
    observed_at: str,
    drawer_count: int = 0,
) -> tuple[str, str, bool]:
    short_ts = _format_utc_short(observed_at)
    suffix = f" · {short_ts}" if short_ts else ""
    if not sorted_rows:
        if drawer_count > 0:
            return f"Watching{suffix}", "active", True
        return f"Queue empty{suffix}", "success", False
    action_count = sum(
        1
        for _, _, tier, _ in sorted_rows
        if tier in (ReviewSortTier.RE_REVIEW_NEEDED, ReviewSortTier.REVIEW_REQUESTED)
    )
    if action_count > 0:
        return f"{action_count} awaiting review{suffix}", "warning", True
    if drawer_count == 0 and all(tier == ReviewSortTier.TERMINAL for _, _, tier, _ in sorted_rows):
        return f"Queue complete{suffix}", "success", False
    return f"Watching{suffix}", "active", True


def _review_subtitle(
    sorted_rows: Sequence[tuple[PrSnapshot, ReviewStep, ReviewSortTier, int]],
    viewer: str,
    events: Sequence[Event] | None,
) -> str:
    if not sorted_rows:
        return f"Review queue for @{viewer}" if viewer else ""
    snaps = [s for s, _, _, _ in sorted_rows]
    repos = sorted({s.repo for s in snaps if s.repo})
    noun = "PR" if len(snaps) == 1 else "PRs"
    if len(repos) == 1:
        scope = f"{len(snaps)} {noun} in `{repos[0]}`"
    else:
        scope = f"{len(snaps)} {noun} across {len(repos)} repos"
    if events:
        changed = len({ev.pr for ev in events})
        ev_noun = "PR" if changed == 1 else "PRs"
        return f"{scope} · {changed} {ev_noun} updated"
    return scope


def _resolve_now_epoch(
    now_epoch: float | None,
    now: float | None,
    observed_at: str,
) -> float:
    if now_epoch is not None:
        return now_epoch
    if now is not None:
        return now
    parsed = _parse_iso_ts(observed_at)
    return parsed if parsed > 0 else time.time()


def _drawer_reason_for(
    snap: PrSnapshot,
    viewer: str,
    overrides: ReviewOverrides,
    stale_days: int,
    now_epoch: float,
) -> DrawerReason | None:
    if snap.state != "OPEN":
        return None
    if is_mute_active(snap, overrides.get_mute(snap.key), viewer):
        return DrawerReason.MUTED
    if is_dormant_review(
        snap,
        viewer,
        stale_days,
        now_epoch,
        pinned_at=overrides.get_pin(snap.key),
    ):
        return DrawerReason.DORMANT
    return None


def _build_drawer_section(
    drawer_entries: Sequence[tuple[PrSnapshot, DrawerReason]],
    *,
    now_epoch: float,
    multi_repo: bool,
) -> tuple[str, str, list[dict[str, Any]]]:
    if not drawer_entries:
        return "", "", []
    ordered = sorted(
        drawer_entries,
        key=lambda pair: (
            0 if pair[1] == DrawerReason.MUTED else 1,
            -_parse_iso_ts(pair[0].updated_at),
            pair[0].repo,
            pair[0].number,
        ),
    )
    items = [
        drawer_item_for(snap, reason, now_epoch=now_epoch, multi_repo=multi_repo)
        for snap, reason in ordered
    ]
    muted_count = sum(1 for _, reason in ordered if reason == DrawerReason.MUTED)
    dormant_count = len(ordered) - muted_count
    parts: list[str] = []
    if muted_count > 0:
        parts.append(f"{muted_count} muted")
    if dormant_count > 0:
        parts.append(f"{dormant_count} dormant")
    return f"Muted & Dormant ({len(items)})", " · ".join(parts), items


def _partition_review_rows(
    all_rows: Sequence[tuple[PrSnapshot, ReviewStep, ReviewSortTier, int]],
    viewer: str,
    overrides: ReviewOverrides,
    stale_days: int,
    now_epoch: float,
) -> tuple[
    list[tuple[PrSnapshot, ReviewStep, ReviewSortTier, int]],
    list[tuple[PrSnapshot, DrawerReason]],
]:
    active_rows: list[tuple[PrSnapshot, ReviewStep, ReviewSortTier, int]] = []
    drawer_entries: list[tuple[PrSnapshot, DrawerReason]] = []
    for row in all_rows:
        reason = _drawer_reason_for(row[0], viewer, overrides, stale_days, now_epoch)
        if reason is not None:
            drawer_entries.append((row[0], reason))
        else:
            active_rows.append(row)
    return active_rows, drawer_entries


def review_board_data_model(
    snapshots: Mapping[str, PrSnapshot] | Sequence[PrSnapshot],
    viewer: str = "",
    *,
    durations: Mapping[str, Sequence[int]] | None = None,
    subtitle: str | None = None,
    events: Sequence[Event] | None = None,
    now: float | None = None,
    observed_at: str = "",
    overrides: ReviewOverrides | None = None,
    stale_days: int = 30,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """Builds the reviewer queue board ``dataModel`` dictionary."""
    all_rows = sort_review_snapshots(snapshots, viewer)
    repos = {snap.repo for snap, _, _, _ in all_rows if snap.repo}
    multi_repo = len(repos) > 1
    ov = overrides if overrides is not None else ReviewOverrides()
    ts_now = _resolve_now_epoch(now_epoch, now, observed_at)
    eta_now = now if now is not None else now_epoch
    active_rows, drawer_entries = _partition_review_rows(all_rows, viewer, ov, stale_days, ts_now)
    events_by_pr: dict[str, list[Event]] = {}
    for ev in events or ():
        events_by_pr.setdefault(ev.pr, []).append(ev)
    items = [
        review_item_for(
            snap,
            viewer=viewer,
            multi_repo=multi_repo,
            durations=durations,
            now=eta_now,
            wave=wave,
            events=events_by_pr.get(snap.key),
            triage=ov.get_triage(snap.key),
        )
        for snap, _, _, wave in active_rows
    ]
    drawer_title, drawer_summary, drawer_items = _build_drawer_section(
        drawer_entries,
        now_epoch=ts_now,
        multi_repo=multi_repo,
    )
    segments, progress_summary = _review_progress_segments(
        active_rows, drawer_count=len(drawer_items)
    )
    status_text, status_tone, status_pulse = _review_status_badge(
        active_rows, observed_at, drawer_count=len(drawer_items)
    )
    resolved_subtitle = (
        subtitle if subtitle is not None else _review_subtitle(active_rows, viewer, events)
    )
    title = f"Review Queue ({len(items)})" if items else "Review Queue"
    return {
        "title": title,
        "subtitle": resolved_subtitle,
        "status": status_text,
        "statusTone": status_tone,
        "statusPulse": status_pulse,
        "steps": REVIEW_BOARD_STEPS,
        "segments": segments,
        "progressSummary": progress_summary,
        "items": items,
        "drawerTitle": drawer_title,
        "drawerSummary": drawer_summary,
        "drawerItems": drawer_items,
        "ackLabel": "Acknowledge",
    }


def review_board_dsl(
    data_model: Mapping[str, Any] | Sequence[PrSnapshot],
    *,
    surface_id: str = "monitor",
    viewer: str = "",
    durations: Mapping[str, Sequence[int]] | None = None,
    subtitle: str | None = None,
    events: Sequence[Event] | None = None,
    now: float | None = None,
    observed_at: str = "",
    overrides: ReviewOverrides | None = None,
    stale_days: int = 30,
    now_epoch: float | None = None,
) -> str:
    """Renders A2UI Express data statements for the reviewer queue board."""
    _ = surface_id
    if isinstance(data_model, Mapping) and "items" in data_model and "steps" in data_model:
        dm: Mapping[str, Any] = data_model
    else:
        dm = review_board_data_model(
            data_model,
            viewer=viewer,
            durations=durations,
            subtitle=subtitle,
            events=events,
            now=now,
            observed_at=observed_at,
            overrides=overrides,
            stale_days=stale_days,
            now_epoch=now_epoch,
        )
    keys = (
        "title",
        "subtitle",
        "status",
        "statusTone",
        "statusPulse",
        "steps",
        "segments",
        "progressSummary",
        "items",
        "drawerTitle",
        "drawerSummary",
        "drawerItems",
        "ackLabel",
    )
    return "\n".join(f"$/{key} = {json.dumps(dm[key])}" for key in keys if key in dm)


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
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
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


def _stored_components(line: dict[str, Any]) -> Any:
    create = _stored_create(line)
    if "components" in create:
        return create.get("components")
    msg = line.get("message")
    update_comp = msg.get("updateComponents") if isinstance(msg, dict) else None
    return update_comp.get("components") if isinstance(update_comp, dict) else None


def _is_unchanged(
    line: dict[str, Any],
    data_model: dict[str, Any],
    components: list[dict[str, Any]] | None = None,
) -> bool:
    """Compares components, ``items``, and ``drawerItems`` (``status`` carries a timestamp)."""
    target_components = STACK_BOARD_COMPONENTS if components is None else components
    create = _stored_create(line)
    stored_dm = create.get("dataModel")
    return (
        _stored_components(line) == target_components
        and isinstance(stored_dm, dict)
        and stored_dm.get("items") == data_model.get("items")
        and stored_dm.get("drawerItems") == data_model.get("drawerItems")
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
    components: list[dict[str, Any]] | None = None,
) -> StoreStatus:
    """Replaces the board's surface file; must run under ``_conversation_lock``."""
    target_components = STACK_BOARD_COMPONENTS if components is None else components
    surface_path = conv_dir / f"{surface_id}.jsonl"
    current = _read_surface_line(surface_path)
    if current is None and not allow_create:
        return "dismissed"
    if current is not None and not force and _is_unchanged(current, data_model, target_components):
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
                "components": target_components,
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
    components: list[dict[str, Any]] | None = None,
) -> StoreStatus:
    """Replaces ``conversations/<convId>/<surface_id>.jsonl`` with the board's full snapshot.

    Returns ``"written"`` after a write, ``"unchanged"`` when the stored components,
    ``items``, and ``drawerItems`` already match (unless ``force``), ``"dismissed"`` when
    the surface file is missing and ``allow_create`` is false, and ``"unavailable"`` when
    there is no store, the ids are invalid, a legacy flat file is still waiting for
    migration, or the lock times out. Callers fall back to ``board.a2ui`` and
    ``render_a2ui`` on ``"unavailable"``.
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
    ts = observed_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with _conversation_lock(conv_dir):
            return _commit_board(
                conv_dir,
                surface_id,
                data_model,
                ts,
                force,
                allow_create,
                components=components,
            )
    except StoreLockTimeout:
        return "unavailable"
