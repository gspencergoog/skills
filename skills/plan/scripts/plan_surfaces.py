#!/usr/bin/env python3
"""plan_surfaces.py: Emits post-plan A2UI surfaces and fallback prompts.

Provides four subcommands consumed after `validate_plan.py` passes:

- `questions <plan.md> [--surface-id ID] [--format a2ui|ask-question|markdown] [--layout cards|list] [--no-review] [--only QID,...]`
  Emits a Composable Express Question Blocks worksheet (or Tier 2/3 fallbacks)
  with one block per `Q<n>` and an optional independent review checkbox in the footer.
- `overview <plan.md> [--diagram auto|compact|plan]`
  Emits `$/...` data lines for the `plan_overview` A2UI template.
- `progress <plan.md> [--running ID,...] [--done ID,...] [--failed ID,...] [--subtitle TEXT] [--delta]`
  Emits full or `--delta` data lines for the `progress_board` A2UI template.
- `triage <findings.json> [--format a2ui|ask-question|markdown]`
  Emits `triage_matrix` data lines and a custom `bucket_picker` with
  `Apply`, `Discuss`, and `Reject` chips (or Tier 2/3 fallbacks).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from plan_model import (
    OpenQuestion,
    PlanDocument,
    QuestionOption,
    TrackInfo,
    WaveInfo,
    parse_plan,
)

DiagramMode = Literal["auto", "compact", "plan"]

CHIP_LABEL_MAX_CHARS = 60
REVIEW_PROMPT = "Run an independent review of this plan?"
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_GLANCE_LABELS = {
    "goal": "Goal",
    "shape": "Shape",
    "decisions needed from you": "Decisions needed",
    "biggest risk": "Biggest risk",
    "not doing": "Not doing",
}

_PLAIN_LOWER_START_RE = re.compile(r"^[a-z]+(?:[\s.,;:!?)]|$)")
_HEADING_PREFIX_RE = re.compile(r"^#{1,6}\s+")
_WAVE_DETAIL_RE = re.compile(r"^(?P<base>[^(]+?)\s*\((?P<detail>[^)]+)\)\s*$")

_ERROR_SEVERITIES = frozenset({"blocker", "critical", "error", "high"})
_WARNING_SEVERITIES = frozenset({"major", "warning", "medium"})
_APPLY_SEVERITIES = frozenset({"blocker", "major", "critical", "error", "high"})


def escape_dsl_str(value: str) -> str:
    """Escapes backslashes, double quotes, and newlines for an Express string literal."""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return (
        normalized.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


def dsl_str(value: str) -> str:
    """Formats `value` as a double-quoted A2UI Express string literal."""
    return f'"{escape_dsl_str(value)}"'


def truncate_chip_label(label: str, limit: int = CHIP_LABEL_MAX_CHARS) -> str:
    """Collapses internal newlines and truncates a chip label to at most `limit` characters."""
    flat = " ".join(label.replace("\r\n", "\n").replace("\r", "\n").split("\n")).strip()
    if len(flat) <= limit:
        return flat
    if limit <= 3:
        return flat[:limit]
    return flat[: limit - 3] + "..."


def _format_dsl_dict(mapping: dict[Any, Any]) -> str:
    """Serializes a dictionary into an A2UI Express object literal."""
    pairs: list[str] = []
    for k, v in mapping.items():
        key_str = str(k)
        formatted_key = key_str if _IDENT_RE.match(key_str) else dsl_str(key_str)
        pairs.append(f"{formatted_key}: {format_dsl_value(v)}")
    return "{" + ", ".join(pairs) + "}"


def format_dsl_value(value: Any) -> str:
    """Serializes a Python value into A2UI Express literal syntax."""
    if isinstance(value, str):
        return dsl_str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(format_dsl_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return _format_dsl_dict(value)
    return dsl_str(str(value))


def _recommended_option(question: OpenQuestion) -> QuestionOption | None:
    """Returns the recommended option for `question`, or its first option as fallback."""
    for opt in question.options:
        if opt.recommended:
            return opt
    return question.options[0] if question.options else None


def _ordered_options(question: OpenQuestion) -> list[QuestionOption]:
    """Returns `question.options` with the recommended option placed first."""
    return sorted(question.options, key=lambda opt: not opt.recommended)


def _format_option_line(opt: QuestionOption) -> str:
    """Formats an option with its letter, text, consequence, and recommendation prefix."""
    prefix = "(Recommended) " if opt.recommended else ""
    base = f"{prefix}{opt.letter}: {opt.text}"
    if opt.consequence:
        sep = "" if base.endswith((".", "!", "?")) else "."
        return f"{base}{sep} Consequence: {opt.consequence}"
    return base


def _card_options_for_question(
    question: OpenQuestion,
) -> list[dict[str, str | bool]]:
    """Builds the `ChoiceCards` option definitions for `question`."""
    cards: list[dict[str, str | bool]] = []
    for opt in question.options:
        entry: dict[str, str | bool] = {
            "value": opt.letter.lower(),
            "label": f"{opt.letter} · {opt.text}",
        }
        if opt.consequence:
            entry["description"] = opt.consequence
        if opt.recommended:
            entry["badge"] = "Recommended"
            entry["tone"] = "success"
        cards.append(entry)
    cards.append({"value": "defer", "label": "Defer", "quiet": True})
    return cards


def _render_question_block(question: OpenQuestion) -> tuple[str, list[str], list[str]]:
    """Returns (block_id, data_lines, component_lines) for one `OpenQuestion`."""
    key = question.qid.lower()
    rec = _recommended_option(question)
    default_val = rec.letter.lower() if rec is not None else "defer"
    options_dsl = format_dsl_value(_card_options_for_question(question))

    data_lines = [
        f"$/{key}/choice = [{dsl_str(default_val)}]",
        f"$/{key}/options = {options_dsl}",
    ]

    head_title = dsl_str(f"**{question.qid}: {question.title}**")
    badge_text = dsl_str(question.qid)
    block_id = f"{key}_block"
    component_lines = [
        f'{key}_head = Row([Text({head_title}, "body", 1), Badge({badge_text}, "warning")], "spaceBetween", "center")',
        f"{key}_cards = ChoiceCards($/{key}/options, $/{key}/choice)",
        f"{block_id} = Column([{key}_head, {key}_cards])",
    ]
    return block_id, data_lines, component_lines


def _list_chips_for_question(question: OpenQuestion) -> list[dict[str, str]]:
    """Builds short letter `ChoicePicker` chip definitions for the `list` layout."""
    chips = [
        {
            "label": truncate_chip_label(opt.letter),
            "value": opt.letter.lower(),
        }
        for opt in question.options
    ]
    chips.append({"label": truncate_chip_label("Defer"), "value": "defer"})
    return chips


def _render_question_block_list(
    question: OpenQuestion,
) -> tuple[str, list[str], list[str]]:
    """Returns (block_id, data_lines, component_lines) for one `OpenQuestion` in list layout."""
    key = question.qid.lower()
    rec = _recommended_option(question)
    default_val = rec.letter.lower() if rec is not None else "defer"
    data_lines = [f"$/{key}/choice = [{dsl_str(default_val)}]"]

    head_title = dsl_str(f"**{question.qid}: {question.title}**")
    badge_text = dsl_str(question.qid)
    component_lines = [
        f'{key}_head = Row([Text({head_title}, "body", 1), Badge({badge_text}, "warning")], "spaceBetween", "center")'
    ]
    block_children = [f"{key}_head"]

    for opt in question.options:
        opt_id = f"{key}_opt_{opt.letter.lower()}"
        tone = "success" if opt.recommended else "neutral"
        text_children = [f"Text({dsl_str(f'**{opt.text}**')})"]
        if opt.consequence:
            text_children.append(f'Text({dsl_str(opt.consequence)}, "caption")')
        text_col = f"Column([{', '.join(text_children)}], _, _, 1)"
        component_lines.append(
            f'{opt_id} = Row([Badge({dsl_str(opt.letter)}, "{tone}"), {text_col}], "start", "start")'
        )
        block_children.append(opt_id)

    chips_dsl = format_dsl_value(_list_chips_for_question(question))
    picker_id = f"{key}_picker"
    component_lines.append(
        f'{picker_id} = ChoicePicker(_, "mutuallyExclusive", {chips_dsl}, $/{key}/choice, "chips")'
    )
    block_children.append(picker_id)

    block_id = f"{key}_block"
    component_lines.append(f"{block_id} = Column([{', '.join(block_children)}])")
    return block_id, data_lines, component_lines


def _select_questions(
    doc: PlanDocument, only_qids: set[str] | None
) -> tuple[OpenQuestion, ...]:
    """Filters `doc.questions` in document order when `only_qids` is provided.

    Raises `ValueError` when `only_qids` is empty or names any unknown question ID.
    """
    if only_qids is None:
        return doc.questions
    wanted = {qid.strip().upper() for qid in only_qids if qid and qid.strip()}
    if not wanted:
        raise ValueError("Expected at least one question ID in only_qids.")
    known = {q.qid.upper() for q in doc.questions}
    unknown = sorted(wanted - known)
    if unknown:
        available = ", ".join(q.qid for q in doc.questions) or "none"
        raise ValueError(
            f"Unknown question ID(s): {', '.join(unknown)} (available: {available})"
        )
    return tuple(q for q in doc.questions if q.qid.upper() in wanted)


def _render_questions_footer(payload_dsl: str, *, include_review: bool) -> list[str]:
    """Builds the notes field, optional review checkbox, submit button, and footer row."""
    footer_lines = ['shared_note = TextField("Overall notes or overrides", $/notes)']
    if include_review:
        footer_lines.append(
            'review_check = CheckBox("Run an independent review of this plan (recommended)", $/review)'
        )
    footer_lines.append(
        f'submit_btn = Button(Text("Submit Decisions"), "primary", Event("submit_plan_decisions", {payload_dsl}))'
    )
    footer_row = (
        'footer = Row([review_check, submit_btn], "spaceBetween", "center")'
        if include_review
        else 'footer = Row([submit_btn], "end", "center")'
    )
    footer_lines.append(footer_row)
    return footer_lines


def _render_questions_surface(
    doc: PlanDocument,
    surface_id: str,
    block_renderer: Callable[[OpenQuestion], tuple[str, list[str], list[str]]],
    *,
    include_review: bool = True,
) -> str:
    """Renders an A2UI Express worksheet for `doc` using `block_renderer` per question."""
    data_lines = ["$/review = true"] if include_review else []
    comp_lines: list[str] = []
    block_ids: list[str] = []
    payload_entries = ["review: $/review"] if include_review else []

    for question in doc.questions:
        block_id, q_data, q_comp = block_renderer(question)
        data_lines.extend(q_data)
        if comp_lines:
            comp_lines.append("")
        comp_lines.extend(q_comp)
        block_ids.append(block_id)
        key = question.qid.lower()
        payload_entries.append(f"{key}: $/{key}/choice")

    data_lines.append('$/notes = ""')
    payload_entries.append("notes: $/notes")
    payload_dsl = "{" + ", ".join(payload_entries) + "}"
    children = [
        'Text("### Plan Decisions")',
        'Text("Recommended answers are preselected.", "caption")',
    ]
    for block_id in block_ids:
        children.append(block_id)
        children.append("Divider()")
    children.extend(["shared_note", "footer"])
    children_dsl = "[" + ", ".join(children) + "]"

    lines = [
        f"surface({dsl_str(surface_id)}, sendDataModel=true)",
        "",
        *data_lines,
    ]
    if comp_lines:
        lines.extend(["", *comp_lines])
    lines.extend(
        [
            "",
            *_render_questions_footer(payload_dsl, include_review=include_review),
            "",
            f"root = Card(Column({children_dsl}))",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_questions_a2ui(
    doc: PlanDocument, surface_id: str, *, include_review: bool = True
) -> str:
    """Renders the post-plan decision worksheet in A2UI Express DSL using `ChoiceCards`."""
    return _render_questions_surface(
        doc, surface_id, _render_question_block, include_review=include_review
    )


def _render_questions_a2ui_list(
    doc: PlanDocument, surface_id: str, *, include_review: bool = True
) -> str:
    """Renders the fallback post-plan worksheet using option rows and short letter chips."""
    return _render_questions_surface(
        doc, surface_id, _render_question_block_list, include_review=include_review
    )


def _render_questions_ask_question(
    doc: PlanDocument, *, include_review: bool = True
) -> str:
    """Renders the post-plan questions as a JSON array for the host `ask_question` tool."""
    items: list[dict[str, Any]] = []
    if include_review:
        items.append(
            {
                "question": REVIEW_PROMPT,
                "options": ["(Recommended) Yes", "No"],
                "is_multi_select": False,
            }
        )
    for question in doc.questions:
        options = [_format_option_line(opt) for opt in _ordered_options(question)]
        options.append("Defer")
        items.append(
            {
                "question": f"{question.qid}: {question.title}",
                "options": options,
                "is_multi_select": False,
            }
        )
    return json.dumps(items, indent=2, ensure_ascii=False) + "\n"


def _render_questions_markdown(
    doc: PlanDocument, *, include_review: bool = True
) -> str:
    """Renders the post-plan questions as a numbered Markdown list for chat."""
    lines: list[str] = []
    start_idx = 1
    if include_review:
        lines.extend(
            [
                f"1. **{REVIEW_PROMPT}**",
                "   - (Recommended) Yes",
                "   - No",
            ]
        )
        start_idx = 2
    for idx, question in enumerate(doc.questions, start=start_idx):
        lines.append(f"{idx}. **{question.qid}: {question.title}**")
        for opt in _ordered_options(question):
            lines.append(f"   - {_format_option_line(opt)}")
        lines.append("   - Defer")
    return "\n".join(lines) + "\n" if lines else ""


def render_questions(
    doc: PlanDocument,
    surface_id: str = "plan-decisions",
    fmt: str = "a2ui",
    layout: str = "cards",
    *,
    include_review: bool = True,
    only_qids: set[str] | None = None,
) -> str:
    """Renders post-plan questions for `doc` in the requested format and layout.

    Filters questions in document order when `only_qids` is set, raising
    `ValueError` on unknown question IDs. Set `include_review=False` to omit
    the independent plan review prompt or footer checkbox.
    """
    filtered_doc = replace(doc, questions=_select_questions(doc, only_qids))
    if fmt == "ask-question":
        return _render_questions_ask_question(
            filtered_doc, include_review=include_review
        )
    if fmt == "markdown":
        return _render_questions_markdown(filtered_doc, include_review=include_review)
    if layout == "list":
        return _render_questions_a2ui_list(
            filtered_doc, surface_id, include_review=include_review
        )
    return _render_questions_a2ui(
        filtered_doc, surface_id, include_review=include_review
    )


def _capitalize_glance_value(value: str) -> str:
    """Capitalizes a leading plain lowercase word left after a Markdown colon."""
    stripped = value.strip()
    if stripped and _PLAIN_LOWER_START_RE.match(stripped):
        return stripped[0].upper() + stripped[1:]
    return stripped


def _strip_wrapping_backticks(text: str) -> str:
    """Strips a single outer pair of backticks when no inner backticks exist."""
    cleaned = text.strip()
    if len(cleaned) >= 2 and cleaned.startswith("`") and cleaned.endswith("`"):
        inner = cleaned[1:-1]
        if "`" not in inner:
            return inner.strip()
    return cleaned


def _split_wave_cell(wave: str) -> tuple[str, str]:
    """Splits a wave table cell into `(badge_label, track_detail)`."""
    cleaned = _strip_wrapping_backticks(wave)
    if not cleaned:
        return "", ""
    detail = ""
    match = _WAVE_DETAIL_RE.match(cleaned)
    if match:
        cleaned = match.group("base").strip()
        detail = match.group("detail").strip()
    if cleaned and not cleaned.lower().startswith("wave"):
        cleaned = f"Wave {cleaned}"
    return cleaned, detail


def _build_glance_items(doc: PlanDocument) -> list[dict[str, str]]:
    """Converts `doc.at_a_glance` into `[{label, value}]` rows."""
    if not doc.at_a_glance:
        return []
    items: list[dict[str, str]] = []
    for key, value in doc.at_a_glance.items():
        fallback_label = key[:1].upper() + key[1:] if key else "Note"
        label = _GLANCE_LABELS.get(key, fallback_label)
        items.append({"label": label, "value": _capitalize_glance_value(value)})
    return items


def _lookup_cell(
    row: dict[str, str], candidates: tuple[str, ...], default: str = ""
) -> str:
    """Finds the first non-empty column value matching `candidates` (case-insensitive)."""
    lowered = {k.strip().lower(): v.strip() for k, v in row.items()}
    for name in candidates:
        if lowered.get(name):
            return lowered[name]
    return default


def _build_pr_items(doc: PlanDocument) -> list[dict[str, str]]:
    """Converts `doc.pr_rows` into `[{id, title, repo, wave}]` rows."""
    items: list[dict[str, str]] = []
    for idx, row in enumerate(doc.pr_rows, start=1):
        pr_id = _strip_wrapping_backticks(
            _lookup_cell(row, ("#", "pr", "id", "commit #"), str(idx))
        )
        title = _strip_wrapping_backticks(
            _lookup_cell(
                row,
                (
                    "commit",
                    "pr / commit",
                    "pr/commit",
                    "title",
                    "summary",
                    "description",
                ),
                "",
            )
        )
        repo = _strip_wrapping_backticks(
            _lookup_cell(row, ("repo", "repository", "package"), "")
        )
        raw_wave = _lookup_cell(row, ("wave", "phase"), "")
        wave_badge, wave_detail = _split_wave_cell(raw_wave)
        meta = " · ".join(part for part in (repo, wave_detail) if part)
        items.append({"id": pr_id, "title": title, "repo": meta, "wave": wave_badge})
    return items


def _plural_count(count: int, singular: str) -> str:
    """Formats `count` with `singular` or `{singular}s`."""
    suffix = "" if count == 1 else "s"
    return f"{count} {singular}{suffix}"


def _overview_subtitle(doc: PlanDocument, pr_count: int) -> str:
    """Builds a compact subtitle summarizing waves, parallel tracks, changes, and questions."""
    parts: list[str] = []
    if doc.waves:
        parts.append(_plural_count(len(doc.waves), "wave"))
    if doc.tracks:
        parts.append(_plural_count(len(doc.tracks), "parallel track"))
    if pr_count > 0:
        parts.append(_plural_count(pr_count, "change"))
    parts.append(_plural_count(len(doc.questions), "open question"))
    return " · ".join(parts)


_MERMAID_BLOCK_RE = re.compile(
    r"^```mermaid[ \t]*\n(?P<body>.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL
)
_FLOWCHART_RE = re.compile(r"^\s*(flowchart|graph)\b", re.IGNORECASE)
_MODE_TAG_SUFFIX_RE = re.compile(
    r"\s*\[(?:Parallel|Sequential)[^\]]*\]\s*$", re.IGNORECASE
)
_TRACK_PREFIX_RE = re.compile(r"^Track\s+", re.IGNORECASE)
_WAVE_PREFIX_RE = re.compile(r"^Wave\s+[\w.-]+\b\s*(?::|·)?\s*", re.IGNORECASE)
_COLON_SEP_RE = re.compile(r"\s*:\s+")
_PAREN_GROUP_RE = re.compile(r"\([^)]*\)")
_WAVE_TOKEN_RE = re.compile(r"\b(\d[\w.-]*)\b")


def _node_label(text: str) -> str:
    """Makes `text` safe inside a quoted Mermaid node label."""
    return text.replace('"', "'").replace("`", "").strip()


def _normalize_diagram_title(text: str, *, strip_wave_prefix: bool = False) -> str:
    """Normalizes a wave or track heading into a safe, compact Mermaid label."""
    cleaned = _MODE_TAG_SUFFIX_RE.sub("", text.strip())
    if strip_wave_prefix:
        cleaned = _WAVE_PREFIX_RE.sub("", cleaned)
    cleaned = _TRACK_PREFIX_RE.sub("", cleaned)
    cleaned = _COLON_SEP_RE.sub(" · ", cleaned)
    cleaned = _node_label(cleaned)
    return " ".join(cleaned.split())


def _plan_flowchart(text: str) -> str | None:
    """Returns the body of the plan's first Mermaid flowchart that mentions waves."""
    for match in _MERMAID_BLOCK_RE.finditer(text):
        body = match.group("body")
        if _FLOWCHART_RE.match(body) and re.search(r"\bwave\b", body, re.IGNORECASE):
            return body.rstrip("\n")
    return None


def _completing_prs_by_wave(doc: PlanDocument) -> dict[str, list[str]]:
    """Maps each wave ID to the `PR <id>` labels completing in that wave."""
    by_wave: dict[str, list[str]] = {}
    for idx, row in enumerate(doc.pr_rows, start=1):
        raw_id = _strip_wrapping_backticks(
            _lookup_cell(row, ("#", "pr", "id", "commit #"), str(idx))
        )
        pr_label = (
            f"PR {raw_id[3:].strip()}"
            if raw_id.lower().startswith("pr ")
            else f"PR {raw_id}"
        )
        wave_cell = _strip_wrapping_backticks(_lookup_cell(row, ("wave", "phase"), ""))
        stripped = _PAREN_GROUP_RE.sub("", wave_cell)
        wave_ids = _WAVE_TOKEN_RE.findall(stripped)
        if wave_ids:
            by_wave.setdefault(wave_ids[-1], []).append(pr_label)
    return by_wave


def _wave_header_title(wave: WaveInfo) -> str:
    """Formats `Wave <id> · <title>` (or `Wave <id>` when untitled)."""
    clean_title = _normalize_diagram_title(wave.title, strip_wave_prefix=True)
    if clean_title:
        return f"Wave {wave.wave_id} · {clean_title}"
    return f"Wave {wave.wave_id}"


def _track_node_label(track: TrackInfo) -> str:
    """Formats a compact track node label such as `2A · Reader`."""
    raw = f"{track.track_id}: {track.title}" if track.title else track.track_id
    return _normalize_diagram_title(raw)


def _parallel_wave_caption(
    wave: WaveInfo, wave_tracks: list[TrackInfo], pr_list: list[str]
) -> str:
    """Formats the parenthesized header caption for a parallel wave."""
    meta_parts: list[str] = []
    if wave.has_verifier or len(wave_tracks) >= 2:
        meta_parts.append("Verifier")
    meta_parts.extend(pr_list)
    return f" ({' · '.join(meta_parts)})" if meta_parts else ""


def _wave_nodes(
    doc: PlanDocument, wave: WaveInfo, prs: list[str] | None = None
) -> tuple[list[str], str]:
    """Builds `(mermaid_lines, wave_node_id)` for a single `wave`."""
    pr_list = prs or []
    wave_tracks = [t for t in doc.tracks if t.wave_id == wave.wave_id]
    safe_wave = f"W{wave.wave_id.replace('.', '_').replace('-', '_')}"
    base_title = _wave_header_title(wave)

    if wave.mode == "Parallel" and wave_tracks:
        caption = _parallel_wave_caption(wave, wave_tracks, pr_list)
        lines = [f'    subgraph {safe_wave}["{base_title}{caption}"]']
        for idx, track in enumerate(wave_tracks):
            lines.append(f'        {safe_wave}T{idx}["{_track_node_label(track)}"]')
        lines.append("    end")
        return lines, safe_wave

    suffix = f" ({', '.join(pr_list)})" if pr_list else ""
    return [f'    {safe_wave}["{base_title}{suffix}"]'], safe_wave


def _generated_flowchart(doc: PlanDocument) -> str:
    """Builds a compact wave flowchart from the parsed waves, tracks, and PR rows."""
    if not doc.waves:
        return ""
    direction = "LR" if len(doc.waves) <= 2 and not doc.parallel_waves else "TD"
    lines = [f"flowchart {direction}"]
    prs_by_wave = _completing_prs_by_wave(doc)
    wave_ids: list[str] = []
    for wave in doc.waves:
        node_lines, wave_node_id = _wave_nodes(
            doc, wave, prs_by_wave.get(wave.wave_id, [])
        )
        lines.extend(node_lines)
        wave_ids.append(wave_node_id)
    for idx in range(len(wave_ids) - 1):
        lines.append(f"    {wave_ids[idx]} --> {wave_ids[idx + 1]}")
    return "\n".join(lines)


def _select_flowchart_body(
    doc: PlanDocument, text: str, diagram_mode: DiagramMode
) -> str:
    """Selects the inner Mermaid flowchart body for `diagram_mode`."""
    if diagram_mode == "compact":
        return _generated_flowchart(doc) if doc.waves else ""
    if diagram_mode == "plan":
        return _plan_flowchart(text) or (_generated_flowchart(doc) if doc.waves else "")
    if doc.waves:
        return _generated_flowchart(doc)
    return _plan_flowchart(text) or ""


def _wave_diagram_markdown(
    doc: PlanDocument, text: str = "", diagram_mode: DiagramMode = "auto"
) -> str:
    """Returns the Markdown wave diagram for `doc` under `diagram_mode`."""
    body = _select_flowchart_body(doc, text, diagram_mode)
    return f"```mermaid\n{body}\n```" if body else ""


def render_overview(
    doc: PlanDocument,
    text: str = "",
    *,
    diagram_mode: DiagramMode = "auto",
) -> str:
    """Renders `plan_overview` template data lines for `doc`.

    `diagram_mode` controls `$/waveDiagram`: `"auto"` synthesizes a compact wave
    diagram when `doc.waves` is non-empty and falls back to the plan's raw
    Mermaid block otherwise; `"compact"` always synthesizes; `"plan"` prefers
    the plan's raw Mermaid block.
    """
    open_count = len(doc.questions)
    status = "Ready" if open_count == 0 else f"{open_count} open"
    status_tone = "success" if open_count == 0 else "warning"
    clean_title = _HEADING_PREFIX_RE.sub("", doc.title).strip() or "Plan"
    title_heading = f"## {clean_title}"
    pr_items = _build_pr_items(doc)
    prs_heading = "### Planned changes" if pr_items else ""

    lines = [
        f"$/title = {dsl_str(title_heading)}",
        f"$/subtitle = {dsl_str(_overview_subtitle(doc, len(pr_items)))}",
        f"$/status = {dsl_str(status)}",
        f"$/statusTone = {dsl_str(status_tone)}",
        f"$/glance = {format_dsl_value(_build_glance_items(doc))}",
        f"$/waveDiagram = {dsl_str(_wave_diagram_markdown(doc, text, diagram_mode))}",
        f"$/prsHeading = {dsl_str(prs_heading)}",
        f"$/prs = {format_dsl_value(pr_items)}",
        f"$/openQuestions = {open_count}",
        '$/ackLabel = "Proceed"',
    ]
    return "\n".join(lines) + "\n"


_SHORT_TRACK_PREFIX_RE = re.compile(r"^(?:Track|Phase)\s+", re.IGNORECASE)
_LEADING_SEP_RE = re.compile(r"^[:·\-]+\s*")


@dataclass(frozen=True)
class _ProgressEntry:
    label: str
    detail: str
    aliases: frozenset[str]
    wave_aliases: frozenset[str] = frozenset()


def _summarize_basenames(paths: list[str], fallback: str) -> str:
    """Summarizes up to three unique file basenames from `paths`, or returns `fallback`."""
    names = list(
        dict.fromkeys(name for raw in paths if (name := Path(raw.rstrip("/")).name))
    )
    if not names:
        return fallback
    if len(names) <= 3:
        return ", ".join(names)
    return f"{', '.join(names[:3])} (+{len(names) - 3} more)"


def _clean_progress_title(text: str, *, strip_wave_prefix: bool = False) -> str:
    """Strips mode tags, optional `Wave <id>` prefixes, and leading separators."""
    cleaned = _MODE_TAG_SUFFIX_RE.sub("", text.strip())
    if strip_wave_prefix:
        cleaned = _WAVE_PREFIX_RE.sub("", cleaned)
    cleaned = _LEADING_SEP_RE.sub("", cleaned).strip()
    return " ".join(cleaned.split())


def _norm_id_token(raw: str) -> str:
    """Normalizes a wave or track identifier for case-insensitive matching."""
    return " ".join(raw.strip().lower().split())


def _wave_aliases(wave_id: str) -> frozenset[str]:
    """Builds flexible case-insensitive identifier aliases for `wave_id`."""
    wid = _norm_id_token(wave_id)
    return frozenset({wid, f"w{wid}", f"wave {wid}", f"wave-{wid}", f"wave_{wid}"})


def _track_aliases(track_id: str) -> tuple[str, frozenset[str]]:
    """Returns `(short_id, aliases)` for `track_id` (such as `'Track 2A'` -> `'2A'`)."""
    short_id = (
        _SHORT_TRACK_PREFIX_RE.sub("", track_id.strip()).strip() or track_id.strip()
    )
    sid = _norm_id_token(short_id)
    raw = _norm_id_token(track_id)
    aliases = frozenset(
        {sid, f"t{sid}", f"track {sid}", f"track-{sid}", f"track_{sid}", raw}
    )
    return short_id, aliases


def _build_track_entry(track: TrackInfo, w_aliases: frozenset[str]) -> _ProgressEntry:
    """Builds a `_ProgressEntry` for a single parallel track."""
    short_id, t_aliases = _track_aliases(track.track_id)
    clean_title = _clean_progress_title(track.title)
    label = (
        f"**Track {short_id}:** {clean_title}"
        if clean_title
        else f"**Track {short_id}**"
    )
    paths = track.files or [fa.raw_path for fa in track.file_actions]
    fallback = clean_title or f"Wave {track.wave_id}"
    detail = _summarize_basenames(paths, fallback=fallback)
    return _ProgressEntry(
        label=label, detail=detail, aliases=t_aliases, wave_aliases=w_aliases
    )


def _build_wave_entry(doc: PlanDocument, wave: WaveInfo) -> _ProgressEntry:
    """Builds a `_ProgressEntry` for a single non-expanded wave."""
    w_aliases = _wave_aliases(wave.wave_id)
    clean_title = _clean_progress_title(wave.title, strip_wave_prefix=True)
    label = (
        f"**Wave {wave.wave_id}:** {clean_title}"
        if clean_title
        else f"**Wave {wave.wave_id}**"
    )
    wave_paths = [fa.raw_path for fa in doc.file_actions if fa.wave_id == wave.wave_id]
    fallback = f"{wave.mode} wave" if wave.mode else "Wave tasks"
    detail = _summarize_basenames(wave_paths, fallback=fallback)
    return _ProgressEntry(label=label, detail=detail, aliases=w_aliases)


def _build_lightweight_entry(doc: PlanDocument) -> _ProgressEntry:
    """Builds the single progress entry for a wave-less lightweight plan."""
    clean_title = _HEADING_PREFIX_RE.sub("", doc.title).strip() or "Plan"
    paths = [fa.raw_path for fa in doc.file_actions]
    detail = _summarize_basenames(paths, fallback="Single-phase implementation")
    aliases = frozenset({"1", "w1", "wave 1", "implementation", "impl"})
    return _ProgressEntry(
        label=f"**Implementation:** {clean_title}",
        detail=detail,
        aliases=aliases,
    )


def _entries_for_wave(doc: PlanDocument, wave: WaveInfo) -> list[_ProgressEntry]:
    """Builds progress entries for `wave` (one per track when non-sequential, else one)."""
    wave_tracks = [t for t in doc.tracks if t.wave_id == wave.wave_id]
    if wave.mode != "Sequential" and wave_tracks:
        w_aliases = _wave_aliases(wave.wave_id)
        return [_build_track_entry(t, w_aliases) for t in wave_tracks]
    return [_build_wave_entry(doc, wave)]


def _build_progress_entries(doc: PlanDocument) -> list[_ProgressEntry]:
    """Builds ordered progress board entries for `doc`, including inline parallel tracks."""
    if not doc.waves and not doc.tracks:
        return [_build_lightweight_entry(doc)]
    entries: list[_ProgressEntry] = []
    covered_waves: set[str] = set()
    for wave in doc.waves:
        entries.extend(_entries_for_wave(doc, wave))
        covered_waves.add(wave.wave_id)
    for track in doc.tracks:
        if track.wave_id not in covered_waves:
            entries.append(_build_track_entry(track, _wave_aliases(track.wave_id)))
    return entries


def _match_entry_indices(entries: list[_ProgressEntry], key: str) -> list[int]:
    """Returns 0-based indices matching normalized `key` by direct or wave aliases."""
    direct = [i for i, entry in enumerate(entries) if key in entry.aliases]
    if direct:
        return direct
    return [i for i, entry in enumerate(entries) if key in entry.wave_aliases]


def _resolve_progress_ids(
    entries: list[_ProgressEntry], raw_ids: set[str] | None
) -> set[int]:
    """Resolves flexible wave or track IDs to 0-based indices into `entries`.

    Raises `ValueError` when any ID does not match an entry in `entries`.
    """
    if not raw_ids:
        return set()
    resolved: set[int] = set()
    for raw_id in raw_ids:
        matched = _match_entry_indices(entries, _norm_id_token(raw_id))
        if not matched:
            raise ValueError(f"Unknown wave or track ID: {raw_id!r}")
        resolved.update(matched)
    return resolved


def _entry_status_tone(
    idx: int,
    running_idx: set[int],
    done_idx: set[int],
    failed_idx: set[int],
) -> tuple[str, str]:
    """Returns `(status, tone)` for item `idx` using `StatusTone` catalog values."""
    if idx in failed_idx:
        return "Failed", "danger"
    if idx in done_idx:
        return "Done", "success"
    if idx in running_idx:
        return "Running", "active"
    return "Queued", "neutral"


def _default_progress_subtitle(total: int, statuses: list[tuple[str, str]]) -> str:
    """Computes a concise default subtitle from item `(status, tone)` pairs."""
    done_count = sum(1 for status, _ in statuses if status == "Done")
    running_count = sum(1 for status, _ in statuses if status == "Running")
    failed_count = sum(1 for status, _ in statuses if status == "Failed")
    if done_count == 0 and running_count == 0 and failed_count == 0:
        return "Ready to start."
    extras: list[str] = []
    if running_count:
        extras.append(f"{running_count} running")
    if failed_count:
        extras.append(f"{failed_count} failed")
    suffix = f" ({', '.join(extras)})" if extras else ""
    return f"{done_count} of {total} complete{suffix}."


def _render_progress_delta(
    effective_subtitle: str,
    statuses: list[tuple[str, str]],
    referenced: list[int],
) -> str:
    """Renders data-only `--delta` lines for `effective_subtitle` and `referenced` items."""
    lines = [f"$/subtitle = {dsl_str(effective_subtitle)}"]
    for idx in referenced:
        status, tone = statuses[idx]
        lines.append(f"$/items/{idx}/status = {dsl_str(status)}")
        lines.append(f"$/items/{idx}/tone = {dsl_str(tone)}")
    return "\n".join(lines) + "\n"


def render_progress(
    doc: PlanDocument,
    *,
    running: set[str] | None = None,
    done: set[str] | None = None,
    failed: set[str] | None = None,
    subtitle: str | None = None,
    delta: bool = False,
) -> str:
    """Renders `progress_board` full or `--delta` data lines for `doc`.

    Matches IDs in `running`, `done`, and `failed` flexibly and case-insensitively
    (such as `'W1'`, `'1'`, `'Wave 1'`, `'2A'`, `'T2A'`, or `'Track 2A'`), raising
    `ValueError` when an unknown ID is given.
    """
    entries = _build_progress_entries(doc)
    running_idx = _resolve_progress_ids(entries, running)
    done_idx = _resolve_progress_ids(entries, done)
    failed_idx = _resolve_progress_ids(entries, failed)
    statuses = [
        _entry_status_tone(i, running_idx, done_idx, failed_idx)
        for i in range(len(entries))
    ]
    effective_subtitle = (
        subtitle
        if subtitle is not None
        else _default_progress_subtitle(len(entries), statuses)
    )
    if delta:
        referenced = sorted(running_idx | done_idx | failed_idx)
        return _render_progress_delta(effective_subtitle, statuses, referenced)

    clean_title = _HEADING_PREFIX_RE.sub("", doc.title).strip()
    title_heading = f"### {clean_title}" if clean_title else "### Progress"
    items = [
        {
            "label": entry.label,
            "detail": entry.detail,
            "status": statuses[idx][0],
            "tone": statuses[idx][1],
        }
        for idx, entry in enumerate(entries)
    ]
    lines = [
        f"$/title = {dsl_str(title_heading)}",
        f"$/subtitle = {dsl_str(effective_subtitle)}",
        f"$/items = {format_dsl_value(items)}",
        '$/ackLabel = "Acknowledge"',
    ]
    return "\n".join(lines) + "\n"


def _severity_icon(severity: str) -> str:
    """Maps a finding severity string to a basic-catalog icon name."""
    lowered = severity.strip().lower()
    if lowered in _ERROR_SEVERITIES:
        return "error"
    if lowered in _WARNING_SEVERITIES:
        return "warning"
    return "info"


def _default_bucket(severity: str) -> str:
    """Returns `Apply` for blocker/major findings and `Discuss` for all others."""
    return "Apply" if severity.strip().lower() in _APPLY_SEVERITIES else "Discuss"


def _finding_title(finding: dict[str, Any], index: int) -> str:
    """Formats a finding's display title with its ID prefix."""
    fid = str(finding.get("id") or f"F{index}").strip()
    title = str(finding.get("title") or "").strip()
    if not title:
        return fid
    if title.startswith(fid):
        return title
    return f"{fid}: {title}"


def _ordered_triage_options(default_bucket: str) -> list[str]:
    """Returns triage disposition labels with `default_bucket` first and marked `(Recommended)`."""
    all_buckets = ["Apply", "Discuss", "Reject"]
    ordered = [default_bucket] + [b for b in all_buckets if b != default_bucket]
    return [f"(Recommended) {ordered[0]}", *ordered[1:]]


def _render_triage_a2ui(findings: list[dict[str, Any]]) -> str:
    """Renders `triage_matrix` data lines and the custom `bucket_picker` component."""
    items: list[dict[str, Any]] = []
    for idx, finding in enumerate(findings, start=1):
        severity = str(finding.get("severity") or "minor")
        bucket = _default_bucket(severity)
        trace = str(finding.get("evidence") or finding.get("trace") or "")
        items.append(
            {
                "icon": _severity_icon(severity),
                "title": _finding_title(finding, idx),
                "summary": str(finding.get("summary") or ""),
                "trace": trace,
                "bucket": [bucket],
                "note": "",
            }
        )

    picker_chips = format_dsl_value(
        [
            {"label": "Apply", "value": "Apply"},
            {"label": "Discuss", "value": "Discuss"},
            {"label": "Reject", "value": "Reject"},
        ]
    )
    lines = [
        '$/title = "### Plan Review Findings Triage"',
        '$/intro = "Choose a disposition for each finding and inspect its evidence trace when needed."',
        f"$/items = {format_dsl_value(items)}",
        '$/submitLabel = "Apply Triage Decisions"',
        f'bucket_picker = ChoicePicker(_, "mutuallyExclusive", {picker_chips}, $bucket, "chips")',
    ]
    return "\n".join(lines) + "\n"


def _render_triage_ask_question(findings: list[dict[str, Any]]) -> str:
    """Renders review findings as a JSON array for the host `ask_question` tool."""
    questions: list[dict[str, Any]] = []
    for idx, finding in enumerate(findings, start=1):
        severity = str(finding.get("severity") or "minor").strip()
        bucket = _default_bucket(severity)
        title = _finding_title(finding, idx)
        summary = str(finding.get("summary") or "").strip()
        prompt = (
            f"{title} ({severity}) - {summary}" if summary else f"{title} ({severity})"
        )
        questions.append(
            {
                "question": prompt,
                "options": _ordered_triage_options(bucket),
                "is_multi_select": False,
            }
        )
    return json.dumps(questions, indent=2, ensure_ascii=False) + "\n"


def _render_triage_markdown(findings: list[dict[str, Any]]) -> str:
    """Renders review findings as a Markdown table and numbered prompt list."""
    lines = [
        "| # | Finding | Severity | Proposed Disposition |",
        "| ---: | :--- | :--- | :--- |",
    ]
    for idx, finding in enumerate(findings, start=1):
        severity = str(finding.get("severity") or "minor").strip()
        bucket = _default_bucket(severity)
        title = _finding_title(finding, idx)
        lines.append(f"| {idx} | {title} | {severity} | (Recommended) {bucket} |")

    lines.append("")
    for idx, finding in enumerate(findings, start=1):
        severity = str(finding.get("severity") or "minor").strip()
        bucket = _default_bucket(severity)
        title = _finding_title(finding, idx)
        summary = str(finding.get("summary") or "").strip()
        lines.append(f"{idx}. **{title} ({severity})**: {summary}")
        for opt in _ordered_triage_options(bucket):
            lines.append(f"   - {opt}")
    return "\n".join(lines) + "\n"


def render_triage(findings: list[dict[str, Any]], fmt: str = "a2ui") -> str:
    """Renders reviewer findings in the requested format (`a2ui`, `ask-question`, or `markdown`)."""
    if fmt == "ask-question":
        return _render_triage_ask_question(findings)
    if fmt == "markdown":
        return _render_triage_markdown(findings)
    return _render_triage_a2ui(findings)


def _load_findings(path: Path) -> list[dict[str, Any]]:
    """Loads findings JSON from `path`, accepting either a top-level list or `{"findings": [...]}`."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict) and isinstance(raw.get("findings"), list):
        return raw["findings"]
    raise ValueError(f"Expected a JSON array of findings in {path}")


def _parse_csv_arg(values: list[str] | None, flag: str) -> set[str] | None:
    """Splits repeatable comma-separated CLI flag values into a non-empty set."""
    if values is None:
        return None
    items = {part.strip() for raw in values for part in raw.split(",") if part.strip()}
    if not items:
        raise ValueError(f"{flag} requires at least one ID.")
    return items


def build_parser() -> argparse.ArgumentParser:
    """Builds the command-line argument parser for `plan_surfaces.py`."""
    parser = argparse.ArgumentParser(
        description="Emit post-plan A2UI surfaces and fallback prompts."
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    q_parser = subparsers.add_parser(
        "questions", help="Emit the post-plan decision worksheet."
    )
    q_parser.add_argument("plan", type=Path, help="Path to the plan Markdown file.")
    q_parser.add_argument(
        "--surface-id",
        default="plan-decisions",
        help="A2UI surface identifier (default: plan-decisions).",
    )
    q_parser.add_argument(
        "--format",
        choices=("a2ui", "ask-question", "markdown"),
        default="a2ui",
        help="Output format (default: a2ui).",
    )
    q_parser.add_argument(
        "--layout",
        choices=("cards", "list"),
        default="cards",
        help="A2UI worksheet layout (default: cards).",
    )
    q_parser.add_argument(
        "--no-review",
        action="store_true",
        help="Omit the independent plan review prompt or checkbox.",
    )
    q_parser.add_argument(
        "--only",
        action="append",
        default=None,
        metavar="QID,...",
        help="Comma-separated question IDs to include (for example, Q1,Q3).",
    )

    o_parser = subparsers.add_parser(
        "overview", help="Emit data lines for the plan_overview template."
    )
    o_parser.add_argument("plan", type=Path, help="Path to the plan Markdown file.")
    o_parser.add_argument(
        "--diagram",
        choices=("auto", "compact", "plan"),
        default="auto",
        help=(
            "Wave diagram mode: auto synthesizes compact wave diagrams when "
            "Wave sections exist and falls back to raw Mermaid otherwise, "
            "compact always synthesizes, and plan prefers the plan's raw "
            "Mermaid block (default: auto)."
        ),
    )

    p_parser = subparsers.add_parser(
        "progress", help="Emit data lines for the progress_board template."
    )
    p_parser.add_argument("plan", type=Path, help="Path to the plan Markdown file.")
    p_parser.add_argument(
        "--running",
        action="append",
        default=None,
        metavar="ID,...",
        help="Comma-separated wave or track IDs currently running.",
    )
    p_parser.add_argument(
        "--done",
        action="append",
        default=None,
        metavar="ID,...",
        help="Comma-separated wave or track IDs that completed.",
    )
    p_parser.add_argument(
        "--failed",
        action="append",
        default=None,
        metavar="ID,...",
        help="Comma-separated wave or track IDs that failed.",
    )
    p_parser.add_argument(
        "--subtitle",
        default=None,
        metavar="TEXT",
        help="Custom subtitle overriding the computed summary.",
    )
    p_parser.add_argument(
        "--delta",
        action="store_true",
        help="Emit data-only delta updates for referenced items and subtitle.",
    )

    t_parser = subparsers.add_parser(
        "triage", help="Emit triage_matrix data lines from reviewer findings JSON."
    )
    t_parser.add_argument("findings", type=Path, help="Path to the findings JSON file.")
    t_parser.add_argument(
        "--format",
        choices=("a2ui", "ask-question", "markdown"),
        default="a2ui",
        help="Output format (default: a2ui).",
    )
    return parser


_build_parser = build_parser


def _dispatch(args: argparse.Namespace) -> int:
    """Executes the parsed CLI subcommand and writes output to `sys.stdout`."""
    if args.subcommand == "questions":
        doc = parse_plan(args.plan.read_text(encoding="utf-8"))
        sys.stdout.write(
            render_questions(
                doc,
                surface_id=args.surface_id,
                fmt=args.format,
                layout=args.layout,
                include_review=not args.no_review,
                only_qids=_parse_csv_arg(args.only, "--only"),
            )
        )
        return 0

    if args.subcommand == "overview":
        plan_text = args.plan.read_text(encoding="utf-8")
        sys.stdout.write(
            render_overview(
                parse_plan(plan_text),
                plan_text,
                diagram_mode=args.diagram,
            )
        )
        return 0

    if args.subcommand == "progress":
        doc = parse_plan(args.plan.read_text(encoding="utf-8"))
        sys.stdout.write(
            render_progress(
                doc,
                running=_parse_csv_arg(args.running, "--running"),
                done=_parse_csv_arg(args.done, "--done"),
                failed=_parse_csv_arg(args.failed, "--failed"),
                subtitle=args.subtitle,
                delta=args.delta,
            )
        )
        return 0

    if args.subcommand == "triage":
        findings = _load_findings(args.findings)
        sys.stdout.write(render_triage(findings, fmt=args.format))
        return 0

    return 1


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for `plan_surfaces.py`."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
