#!/usr/bin/env python3
"""plan_surfaces.py - Emits post-plan A2UI surfaces and fallback prompts.

Provides three subcommands consumed after `validate_plan.py` passes:

- `questions <plan.md> [--surface-id ID] [--format a2ui|ask-question|markdown] [--layout cards|list]`
  Emits a Composable Express Question Blocks worksheet (or Tier 2/3 fallbacks)
  with one block per `Q<n>` and an independent review checkbox in the footer.
- `overview <plan.md>`
  Emits `$/...` data lines for the `plan_overview` A2UI template.
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
from pathlib import Path
from typing import Any

from plan_model import OpenQuestion, PlanDocument, QuestionOption, parse_plan

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


def _render_questions_surface(
    doc: PlanDocument,
    surface_id: str,
    block_renderer: Callable[[OpenQuestion], tuple[str, list[str], list[str]]],
) -> str:
    """Renders an A2UI Express worksheet for `doc` using `block_renderer` per question."""
    data_lines = ["$/review = true"]
    comp_lines: list[str] = []
    block_ids: list[str] = []
    payload_entries = ["review: $/review"]

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
            'shared_note = TextField("Overall notes or overrides", $/notes)',
            'review_check = CheckBox("Run an independent review of this plan (recommended)", $/review)',
            f'submit_btn = Button(Text("Submit Decisions"), "primary", Event("submit_plan_decisions", {payload_dsl}))',
            'footer = Row([review_check, submit_btn], "spaceBetween", "center")',
            "",
            f"root = Card(Column({children_dsl}))",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_questions_a2ui(doc: PlanDocument, surface_id: str) -> str:
    """Renders the post-plan decision worksheet in A2UI Express DSL using `ChoiceCards`."""
    return _render_questions_surface(doc, surface_id, _render_question_block)


def _render_questions_a2ui_list(doc: PlanDocument, surface_id: str) -> str:
    """Renders the fallback post-plan worksheet using option rows and short letter chips."""
    return _render_questions_surface(doc, surface_id, _render_question_block_list)


def _render_questions_ask_question(doc: PlanDocument) -> str:
    """Renders the post-plan questions as a JSON array for the host `ask_question` tool."""
    items: list[dict[str, Any]] = [
        {
            "question": REVIEW_PROMPT,
            "options": ["(Recommended) Yes", "No"],
            "is_multi_select": False,
        }
    ]
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


def _render_questions_markdown(doc: PlanDocument) -> str:
    """Renders the post-plan questions as a numbered Markdown list for chat."""
    lines = [
        f"1. **{REVIEW_PROMPT}**",
        "   - (Recommended) Yes",
        "   - No",
    ]
    for idx, question in enumerate(doc.questions, start=2):
        lines.append(f"{idx}. **{question.qid}: {question.title}**")
        for opt in _ordered_options(question):
            lines.append(f"   - {_format_option_line(opt)}")
        lines.append("   - Defer")
    return "\n".join(lines) + "\n"


def render_questions(
    doc: PlanDocument,
    surface_id: str = "plan-decisions",
    fmt: str = "a2ui",
    layout: str = "cards",
) -> str:
    """Renders post-plan questions for `doc` in the requested format and layout."""
    if fmt == "ask-question":
        return _render_questions_ask_question(doc)
    if fmt == "markdown":
        return _render_questions_markdown(doc)
    if layout == "list":
        return _render_questions_a2ui_list(doc, surface_id)
    return _render_questions_a2ui(doc, surface_id)


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


def _node_label(text: str) -> str:
    """Makes `text` safe inside a quoted Mermaid node label."""
    return text.replace('"', "'").replace("`", "").strip()


def _plan_flowchart(text: str) -> str | None:
    """Returns the body of the plan's first Mermaid flowchart that mentions waves."""
    for match in _MERMAID_BLOCK_RE.finditer(text):
        body = match.group("body")
        if _FLOWCHART_RE.match(body) and re.search(r"\bwave\b", body, re.IGNORECASE):
            return body.rstrip("\n")
    return None


def _wave_nodes(doc: PlanDocument, wave: Any) -> tuple[list[str], list[str]]:
    """Builds `(mermaid_lines, node_ids)` for a single `wave`."""
    tracks = [t for t in doc.tracks if t.wave_id == wave.wave_id]
    safe_wave = wave.wave_id.replace(".", "_").replace("-", "_")
    if wave.mode == "Parallel" and tracks:
        node_lines: list[str] = []
        nodes: list[str] = []
        for idx, track in enumerate(tracks):
            node = f"W{safe_wave}T{idx}"
            label = (
                f"{track.track_id}: {track.title}" if track.title else track.track_id
            )
            node_lines.append(f'    {node}["{_node_label(label)}"]')
            nodes.append(node)
        return node_lines, nodes
    title = (
        f"Wave {wave.wave_id}: {wave.title}" if wave.title else f"Wave {wave.wave_id}"
    )
    node = f"W{safe_wave}"
    return [f'    {node}["{_node_label(title)}"]'], [node]


def _generated_flowchart(doc: PlanDocument) -> str | None:
    """Builds a left-to-right wave flowchart from the parsed waves and tracks."""
    if not doc.waves:
        return None
    lines = ["flowchart LR"]
    previous: list[str] = []
    for wave in doc.waves:
        node_lines, current = _wave_nodes(doc, wave)
        lines.extend(node_lines)
        lines.extend(f"    {src} --> {dst}" for src in previous for dst in current)
        previous = current
    return "\n".join(lines)


def _wave_diagram_markdown(doc: PlanDocument, text: str) -> str:
    """Returns the Markdown wave diagram: the plan's own flowchart, else a generated one."""
    body = _plan_flowchart(text) or _generated_flowchart(doc)
    if body is None:
        return "_No waves: single-step plan._"
    return f"```mermaid\n{body}\n```"


def render_overview(doc: PlanDocument, text: str = "") -> str:
    """Renders `plan_overview` template data lines for `doc`.

    `text` is the raw plan Markdown; when it contains a Mermaid flowchart that
    mentions waves, that diagram is reused so the pane matches the artifact.
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
        f"$/waveDiagram = {dsl_str(_wave_diagram_markdown(doc, text))}",
        f"$/prsHeading = {dsl_str(prs_heading)}",
        f"$/prs = {format_dsl_value(pr_items)}",
        f"$/openQuestions = {open_count}",
        '$/ackLabel = "Proceed"',
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


def _build_parser() -> argparse.ArgumentParser:
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

    o_parser = subparsers.add_parser(
        "overview", help="Emit data lines for the plan_overview template."
    )
    o_parser.add_argument("plan", type=Path, help="Path to the plan Markdown file.")

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


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for `plan_surfaces.py`."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "questions":
        doc = parse_plan(args.plan.read_text(encoding="utf-8"))
        sys.stdout.write(
            render_questions(
                doc,
                surface_id=args.surface_id,
                fmt=args.format,
                layout=args.layout,
            )
        )
        return 0

    if args.subcommand == "overview":
        plan_text = args.plan.read_text(encoding="utf-8")
        sys.stdout.write(render_overview(parse_plan(plan_text), plan_text))
        return 0

    if args.subcommand == "triage":
        findings = _load_findings(args.findings)
        sys.stdout.write(render_triage(findings, fmt=args.format))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
