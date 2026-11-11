#!/usr/bin/env python3
"""Linter and JSON exporter for /design-review Markdown artifacts.

Validates the 7-section structure, principle scorecard math, finding/question
IDs, evidence-tier severity caps, single-fence suggestions, and write-prose
budgets, and exports genui-ready JSON payloads for scorecard and triage_matrix.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SKILLS_ROOT = Path(__file__).resolve().parents[2]
for _rel_scripts in (("review-core", "scripts"), ("write-prose", "scripts")):
    _p = _SKILLS_ROOT.joinpath(*_rel_scripts)
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from analyze_prose import analyze_prose
from review_lint_common import (
    CLASS_ORDER,
    CLEAN_TIERS,
    ID_TO_SEVERITY,
    MAX_SENTENCE_PCC,
    MAX_SENTENCE_WORDS,
    VALID_TIERS,
    VALID_VERDICTS,
    FenceMeta,
    Item,
    ParsedReport,
    ReportProfile,
    Violation,
    check_clean_lines,
    check_evidence_caps,
    check_id_sequence,
    check_prose,
    check_questions,
    check_required_fields,
    check_sections,
    check_severity_matches_id,
    check_single_fence_per_field,
    extract_evidence_detail,
    extract_tier,
    strip_ticks,
    update_fence,
)

PRINCIPLE_IDS = (
    "problem-scope",
    "module-depth",
    "boundaries-state",
    "tradeoffs-simplicity",
    "resilience-ops",
    "evolution-fitness",
    "ergonomics-coherence",
)
PRINCIPLE_LABELS: dict[str, str] = {
    "problem-scope": "Problem Isolation & Falsifiable Scope",
    "module-depth": "Module Depth & Information Hiding",
    "boundaries-state": "Dependency & State Boundaries",
    "tradeoffs-simplicity": "Decisive Tradeoffs & Simpler Baseline",
    "resilience-ops": "Failure Modes & Operational Resilience",
    "evolution-fitness": "Evolutionary Design & Reversibility",
    "ergonomics-coherence": "Cognitive Ergonomics & View Coherence",
}

EXPECTED_SECTIONS = (
    "Summary",
    "Principle Scorecard",
    "View & Boundary Assessment",
    "Findings (Ordered by Severity)",
    "Open Design Questions",
    "Checked and Found Clean",
    "Recommended Next Steps",
)

VALID_DOORS = ("one-way", "two-way")
SEVERITY_ORDER = CLASS_ORDER

MAX_VISIBLE_WORDS = 120
MAX_VISIBLE_SENTENCES = 6
MAX_DETAILS_WORDS = 150

_H2_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$")
_H3_ITEM_RE = re.compile(r"^###\s+(?P<fid>[CHMLQ]\d+)\.\s+(?P<claim>.+?)\s*$")
_FIELD_RE = re.compile(r"^-\s+\*\*(?P<name>[A-Za-z][A-Za-z ]*)\*\*:\s*(?P<val>.*)$")
_SCORE_CELL_RE = re.compile(
    r"^(?:(?P<num>10|[0-9])(?:\s*/\s*10)?|(?P<na>N/A))$", re.IGNORECASE
)
_FOCUS_RE = re.compile(r"^`?(?:adhoc:)?[a-z0-9]+(?:-[a-z0-9]+)*`?$")
_DOOR_RE = re.compile(r"^`?(?:one-way|two-way)`?$", re.IGNORECASE)
_PRINCIPLE_RE = re.compile(rf"^`?(?:{'|'.join(PRINCIPLE_IDS)})`?$")
_VERDICT_RE = re.compile(
    r"Verdict:\s*`?(?P<verdict>Ready|Minor Revisions Needed|Critical Blockers)`?",
    re.IGNORECASE,
)
_OVERALL_SCORE_RE = re.compile(
    r"Overall\s+Score:\s*`?(?P<num>\d+)\s*/\s*(?P<den>\d+)`?", re.IGNORECASE
)
_CITATION_RE = re.compile(
    r"Citation\s+audit:\s*(?P<checked>\d+)\s+checked,\s*(?P<dropped>\d+)\s+dropped",
    re.IGNORECASE,
)
_FILE_LINK_RE = re.compile(r"\[(?P<label>[^\]]+)\]\(file://[^\)]+\)")

DESIGN_PROFILE = ReportProfile(
    name="design",
    sections=EXPECTED_SECTIONS,
    findings_section="Findings (Ordered by Severity)",
    questions_section="Open Design Questions",
    required_fields=(
        "Target",
        "Door",
        "Severity",
        "Principle",
        "Evidence",
        "Body",
        "Why",
        "Suggestion",
    ),
    optional_fields=("Focus", "Details"),
    max_words=MAX_VISIBLE_WORDS,
    max_sentences=MAX_VISIBLE_SENTENCES,
    details_words=MAX_DETAILS_WORDS,
    validators={
        "Door": _DOOR_RE,
        "Principle": _PRINCIPLE_RE,
        "Focus": _FOCUS_RE,
    },
    rule_names={
        "evidence-required": "invalid-evidence-tier",
        "evidence-recalled": "recalled-forbidden",
        "invalid-focus": "focus-id-format",
    },
)


@dataclass
class PrincipleRow:
    """One row parsed from the ## Principle Scorecard pipe table.

    Attributes:
        principle_id: Canonical principle slug (e.g. 'module-depth').
        label: Display label from the table cell.
        score: Integer score 0..10, or None when marked N/A.
        rationale: Assessment rationale text from the third column.
        lineno: 1-based source line number.
    """

    principle_id: str
    label: str
    score: int | None
    rationale: str
    lineno: int


@dataclass
class DesignFinding:
    """A parsed finding (C/H/M/L) or open question (Q) block.

    Attributes:
        finding_id: Heading identifier such as 'H1' or 'Q1'.
        claim: Short claim or question text from the ### heading.
        lineno: 1-based line number of the ### heading.
        fields: Mapping of field name to raw multi-line value.
        fence_counts: Count of fenced code blocks inside each field.
    """

    finding_id: str
    claim: str
    lineno: int
    fields: dict[str, str] = field(default_factory=dict)
    fence_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class DesignReviewReport:
    """Structured AST of a parsed design_review_results.md document."""

    sections_seen: list[tuple[int, str]] = field(default_factory=list)
    summary_lines: list[str] = field(default_factory=list)
    scorecard_rows: list[PrincipleRow] = field(default_factory=list)
    view_lines: list[str] = field(default_factory=list)
    findings: list[DesignFinding] = field(default_factory=list)
    questions: list[DesignFinding] = field(default_factory=list)
    clean_lines: list[tuple[int, str]] = field(default_factory=list)
    next_steps_lines: list[str] = field(default_factory=list)
    unparsed_item_lines: list[tuple[int, str]] = field(default_factory=list)


def _extract_principle_id(cell_text: str) -> tuple[str, str]:
    """Extracts (principle_id, display_label) from the first scorecard cell."""
    for pid in PRINCIPLE_IDS:
        if pid in cell_text:
            cleaned = (
                cell_text.replace(f"`{pid}`", "")
                .replace(f"({pid})", "")
                .strip(" -·:()")
            )
            label = cleaned.strip("* ") or PRINCIPLE_LABELS[pid]
            return pid, label
    return strip_ticks(cell_text), cell_text.strip()


def _parse_scorecard_line(stripped: str, lineno: int) -> PrincipleRow | None:
    """Parses one Markdown pipe-table data row in ## Principle Scorecard."""
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return None
    cells = [c.strip() for c in stripped.strip("|").split("|")]
    if len(cells) < 3:
        return None
    if all(set(c) <= {"-", ":", " "} for c in cells) or cells[0].lower() in {
        "principle",
        "dimension",
    }:
        return None
    pid, label = _extract_principle_id(cells[0])
    score_raw = strip_ticks(cells[1])
    match = _SCORE_CELL_RE.match(score_raw)
    if not match:
        return PrincipleRow(pid, label, -1, cells[2], lineno)
    score = None if match.group("na") else int(match.group("num"))
    return PrincipleRow(pid, label, score, cells[2], lineno)


@dataclass
class _ParseCursor:
    """Mutable cursor state while walking lines of design_review_results.md."""

    report: DesignReviewReport
    section: str = ""
    current_item: DesignFinding | None = None
    current_field: str = ""
    active_fence: str = ""


def _append_item_line(
    cursor: _ParseCursor,
    raw_line: str,
    stripped: str,
    in_fence_or_delim: bool,
    is_open_fence: bool,
) -> None:
    """Appends a field line or continuation line to the active finding/question."""
    item = cursor.current_item
    if item is None:
        return
    if not in_fence_or_delim:
        fmatch = _FIELD_RE.match(stripped)
        if fmatch:
            fname = fmatch.group("name").strip()
            cursor.current_field = fname
            item.fields[fname] = fmatch.group("val").strip()
            item.fence_counts.setdefault(fname, 0)
            return
    if cursor.current_field:
        if is_open_fence:
            item.fence_counts[cursor.current_field] = (
                item.fence_counts.get(cursor.current_field, 0) + 1
            )
        prev = item.fields.get(cursor.current_field, "")
        item.fields[cursor.current_field] = f"{prev}\n{raw_line}".strip()


def _handle_section_body_line(
    cursor: _ParseCursor,
    lineno: int,
    raw_line: str,
    stripped: str,
    in_fence_or_delim: bool,
    is_open_fence: bool,
) -> None:
    """Routes a single line inside the current ## section."""
    sec = cursor.section
    if sec == "Summary":
        cursor.report.summary_lines.append(raw_line)
    elif sec == "Principle Scorecard" and not in_fence_or_delim:
        row = _parse_scorecard_line(stripped, lineno)
        if row is not None:
            cursor.report.scorecard_rows.append(row)
    elif sec == "View & Boundary Assessment":
        cursor.report.view_lines.append(raw_line)
    elif sec in {"Findings (Ordered by Severity)", "Open Design Questions"}:
        _handle_item_section_line(
            cursor, lineno, raw_line, stripped, in_fence_or_delim, is_open_fence
        )
    elif sec == "Checked and Found Clean" and stripped and stripped != "None.":
        cursor.report.clean_lines.append((lineno, stripped))
    elif sec == "Recommended Next Steps":
        cursor.report.next_steps_lines.append(raw_line)


def _handle_item_section_line(
    cursor: _ParseCursor,
    lineno: int,
    raw_line: str,
    stripped: str,
    in_fence_or_delim: bool,
    is_open_fence: bool,
) -> None:
    """Parses ### headings and field bullets in Findings or Open Design Questions."""
    if not in_fence_or_delim:
        h3 = _H3_ITEM_RE.match(stripped)
        if h3:
            item = DesignFinding(
                finding_id=h3.group("fid"), claim=h3.group("claim"), lineno=lineno
            )
            target_list = (
                cursor.report.questions
                if cursor.section == "Open Design Questions"
                else cursor.report.findings
            )
            target_list.append(item)
            cursor.current_item = item
            cursor.current_field = ""
            return
    if cursor.current_item is not None:
        _append_item_line(cursor, raw_line, stripped, in_fence_or_delim, is_open_fence)
    elif stripped and stripped != "None.":
        cursor.report.unparsed_item_lines.append((lineno, stripped))


def parse_design_review(text: str) -> DesignReviewReport:
    """Parses a design_review_results.md file into a DesignReviewReport AST."""
    report = DesignReviewReport()
    cursor = _ParseCursor(report=report)
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        was_fenced = bool(cursor.active_fence)
        cursor.active_fence, active = update_fence(cursor.active_fence, stripped)
        in_fence_or_delim = was_fenced or active
        is_open_fence = not was_fenced and bool(cursor.active_fence)
        if not in_fence_or_delim:
            h2 = _H2_RE.match(stripped)
            if h2:
                title = h2.group("title").strip()
                report.sections_seen.append((lineno, title))
                cursor.section = title
                cursor.current_item = None
                cursor.current_field = ""
                continue
        _handle_section_body_line(
            cursor, lineno, raw_line, stripped, in_fence_or_delim, is_open_fence
        )
    return report


def _to_common_item(df: DesignFinding) -> Item:
    """Adapts a DesignFinding into a review_lint_common.Item."""
    fence_meta = {
        fname: FenceMeta(fenced=count > 0, extra_fences=max(0, count - 1))
        for fname, count in df.fence_counts.items()
    }
    return Item(
        id=df.finding_id,
        title=df.claim,
        fields=df.fields,
        line=df.lineno,
        fence_meta=fence_meta,
    )


def _check_verdict_line(
    summary_lines: Sequence[str], findings: Sequence[DesignFinding]
) -> list[Violation]:
    """Validates Summary Verdict presence and consistency with C/H findings."""
    vmatch = _VERDICT_RE.search("\n".join(summary_lines))
    if not vmatch:
        return [
            Violation(
                rule="missing-verdict",
                message="Summary lacks a valid 'Verdict:' line.",
                line=1,
                fix_hint=f"End ## Summary with 'Verdict: <{' | '.join(VALID_VERDICTS)}>'.",
            )
        ]
    has_blocker = any(f.finding_id.startswith(("C", "H")) for f in findings)
    if vmatch.group("verdict").lower() == "ready" and has_blocker:
        return [
            Violation(
                rule="verdict-contradicts-findings",
                message="Verdict 'Ready' contradicts critical or high findings.",
                line=1,
                fix_hint="Change Verdict to 'Minor Revisions Needed' or 'Critical Blockers'.",
            )
        ]
    return []


def _check_sections(report: DesignReviewReport) -> list[Violation]:
    """Checks required section presence, ordering, and absence of Playbook Feedback."""
    section_map: dict[str, list[tuple[int, str]]] = {}
    for lineno, title in report.sections_seen:
        section_map.setdefault(title, []).append((lineno, title))
    common_parsed = ParsedReport(
        sections=section_map, findings=[], questions=[], clean_lines=report.clean_lines
    )
    violations = check_sections(common_parsed, DESIGN_PROFILE)
    violations.extend(_check_verdict_line(report.summary_lines, report.findings))
    return violations


def _check_scorecard_row(row: PrincipleRow) -> list[Violation]:
    """Validates a single Principle Scorecard row."""
    out: list[Violation] = []
    if row.principle_id not in PRINCIPLE_IDS:
        out.append(
            Violation(
                rule="scorecard-unknown-principle",
                message=f"Unknown principle '{row.principle_id}'.",
                line=row.lineno,
                fix_hint=f"Use one of: {', '.join(PRINCIPLE_IDS)}.",
            )
        )
    elif row.score == -1:
        out.append(
            Violation(
                rule="scorecard-invalid-score",
                message=f"Invalid score cell for '{row.principle_id}'.",
                line=row.lineno,
                fix_hint="Write an integer 0..10 (e.g. 8/10) or N/A.",
            )
        )
    if not row.rationale.strip():
        out.append(
            Violation(
                rule="scorecard-empty-rationale",
                message=f"Empty rationale for '{row.principle_id}'.",
                line=row.lineno,
                fix_hint="Provide a concise rationale in the Assessment column.",
            )
        )
    return out


def _check_scorecard_math(
    summary_lines: Sequence[str], applicable: Sequence[PrincipleRow]
) -> list[Violation]:
    """Validates Overall Score math in Summary when present."""
    omatch = _OVERALL_SCORE_RE.search("\n".join(summary_lines))
    if not omatch or not applicable:
        return []
    exp_num = sum(int(r.score) for r in applicable if r.score is not None)
    exp_den = 10 * len(applicable)
    if int(omatch.group("num")) == exp_num and int(omatch.group("den")) == exp_den:
        return []
    return [
        Violation(
            rule="scorecard-math-mismatch",
            message=f"Overall Score {omatch.group('num')}/{omatch.group('den')} does not match scorecard sum {exp_num}/{exp_den}.",
            line=1,
            fix_hint=f"Set Overall Score to {exp_num}/{exp_den}.",
        )
    ]


def _check_scorecard(report: DesignReviewReport) -> list[Violation]:
    """Validates Principle Scorecard completeness, duplicates, scores, and math."""
    seen_list = [r.principle_id for r in report.scorecard_rows]
    seen_ids = set(seen_list)
    violations = [
        Violation(
            rule="scorecard-missing-principle",
            message=f"Scorecard missing principle '{pid}'.",
            line=1,
            fix_hint=f"Add a row for `{pid}` scored 0..10 or N/A.",
        )
        for pid in PRINCIPLE_IDS
        if pid not in seen_ids
    ]
    if len(seen_list) != len(seen_ids):
        violations.append(
            Violation(
                rule="scorecard-duplicate-principle",
                message="Scorecard contains duplicate principle row(s).",
                line=1,
                fix_hint="Include each of the 7 principles at most once.",
            )
        )
    for row in report.scorecard_rows:
        violations.extend(_check_scorecard_row(row))
    applicable = [
        r
        for r in report.scorecard_rows
        if r.principle_id in PRINCIPLE_IDS and r.score is not None and r.score >= 0
    ]
    if report.scorecard_rows and not applicable:
        violations.append(
            Violation(
                rule="scorecard-all-na",
                message="All principles are marked N/A.",
                line=report.scorecard_rows[0].lineno,
                fix_hint="Score at least one applicable principle 0..10.",
            )
        )
    violations.extend(_check_scorecard_math(report.summary_lines, applicable))
    return violations


def _check_questions_and_clean(report: DesignReviewReport) -> list[Violation]:
    """Validates ## Open Design Questions and ## Checked and Found Clean."""
    q_items = [_to_common_item(q) for q in report.questions]
    violations = check_questions(
        q_items,
        max_count=3,
        cap_rule="question-cap",
        field_rule="missing-question-field",
    )
    violations.extend(
        check_id_sequence(
            q_items, expected_prefix="Q", seq_rule="question-id-sequence"
        )
    )
    for q in q_items:
        for req in ("Target", "Evidence", "Why"):
            if not strip_ticks(q.fields.get(req, "")):
                violations.append(
                    Violation(
                        rule="missing-question-field",
                        message=f"{q.id} lacks '{req}'.",
                        line=q.line,
                        fix_hint=f"Add '- **{req}**: ...' to {q.id}.",
                    )
                )
        ev = q.fields.get("Evidence", "")
        tier = extract_tier(ev)
        detail = extract_evidence_detail(ev)
        if ev.strip() and (tier not in {"Speculation", "Deduction"} or not detail):
            violations.append(
                Violation(
                    rule="question-evidence",
                    message=f"{q.id} Evidence must be Speculation or Deduction with detail.",
                    line=q.line,
                    fix_hint="Format as 'Speculation: <what was checked>'.",
                )
            )
    violations.extend(
        check_prose(
            q_items,
            DESIGN_PROFILE,
            analyze_prose,
            prose_builder=lambda item: f"{item.title} {item.fields.get('Why', '')}",
            check_details=False,
        )
    )
    violations.extend(
        check_clean_lines(
            report.clean_lines,
            max_count=10,
            cap_rule="clean-cap",
            tier_rule="clean-format",
        )
    )
    return violations


def check_design_review(
    report: DesignReviewReport, post_design: bool = False
) -> list[Violation]:
    """Runs all structural, scorecard, evidence, and prose checks on report."""
    _ = post_design  # Reserved for caller-specific policy extensions.
    violations = _check_sections(report)
    violations.extend(_check_scorecard(report))
    for lineno, text in report.unparsed_item_lines:
        violations.append(
            Violation(
                rule="unparsed-item-line",
                message=f"Unanchored text in item section: '{text[:50]}'.",
                line=lineno,
                fix_hint="Place every comment under a '### C1/H1/M1/L1/Q1.' heading or write 'None.'.",
            )
        )
    finding_items = [_to_common_item(f) for f in report.findings]
    violations.extend(
        check_id_sequence(
            finding_items,
            order_rule="finding-order",
            seq_rule="finding-id-sequence",
        )
    )
    violations.extend(check_required_fields(finding_items, DESIGN_PROFILE))
    violations.extend(
        check_severity_matches_id(
            finding_items, rule="severity-mismatch", require_present=False
        )
    )
    violations.extend(
        check_evidence_caps(
            finding_items,
            is_findings=True,
            require_detail=True,
            profile=DESIGN_PROFILE,
        )
    )
    violations.extend(check_single_fence_per_field(finding_items))
    violations.extend(check_prose(finding_items, DESIGN_PROFILE, analyze_prose))
    violations.extend(_check_questions_and_clean(report))
    return violations


def _score_tone(score: int) -> str:
    """Maps a 0..10 principle score to a genui status badge tone."""
    if score >= 8:
        return "success"
    if score >= 6:
        return "info"
    if score >= 4:
        return "warning"
    return "danger"


def _strip_file_links(text: str) -> str:
    """Replaces [label](file://...) Markdown links with bare label text."""
    return _FILE_LINK_RE.sub(r"\g<label>", text)


def _clean_summary_overview(summary_text: str) -> str:
    """Strips internal orchestration lines and file:// links from Summary."""
    kept: list[str] = []
    skip_prefixes = (
        "verdict:",
        "citation audit:",
        "flags:",
        "overall score:",
        "door classification:",
        "target mode:",
    )
    for line in summary_text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.lower().startswith(skip_prefixes):
            kept.append(_strip_file_links(stripped))
    return " ".join(kept).strip()


def _export_finding(item: DesignFinding) -> dict[str, Any]:
    """Serializes one DesignFinding with triage_matrix helper fields."""
    sev = strip_ticks(item.fields.get("Severity", "")).lower()
    ev_raw = item.fields.get("Evidence", "")
    tier = extract_tier(ev_raw)
    detail = extract_evidence_detail(ev_raw)
    icon = (
        "error"
        if sev in {"critical", "high"}
        else ("warning" if sev == "medium" else "info")
    )
    bucket = (
        ["Revise design"]
        if sev in {"critical", "high", "medium"}
        else ["Defer to /plan"]
    )
    return {
        "id": item.finding_id,
        "claim": item.claim,
        "severity": sev,
        "target": strip_ticks(item.fields.get("Target", "")),
        "door": strip_ticks(item.fields.get("Door", "")).lower(),
        "principle": strip_ticks(item.fields.get("Principle", "")),
        "focus": strip_ticks(item.fields.get("Focus", "")) or None,
        "evidence_tier": tier,
        "evidence": detail,
        "body": item.fields.get("Body", ""),
        "why": item.fields.get("Why", ""),
        "suggestion": item.fields.get("Suggestion", ""),
        "details": item.fields.get("Details", "") or None,
        "triage_item": {
            "icon": icon,
            "title": f"{item.finding_id}: {item.claim}",
            "summary": f"{item.fields.get('Body', '')} {item.fields.get('Why', '')}".strip(),
            "trace": strip_ticks(item.fields.get("Target", "")),
            "bucket": bucket,
            "note": "",
        },
    }


def _build_scorecard_payload(rows: Sequence[PrincipleRow]) -> dict[str, Any]:
    """Builds the numeric-only genui scorecard block from parsed rows."""
    applicable = [r for r in rows if r.score is not None and r.score >= 0]
    na_ids = [r.principle_id for r in rows if r.score is None]
    scores = [int(r.score) for r in applicable if r.score is not None]
    intro = f"Evaluated {len(applicable)} applicable design principles."
    if na_ids:
        intro += f" Marked N/A (excluded from total): {', '.join(na_ids)}."
    out_rows = [
        {
            "id": r.principle_id,
            "label": r.label,
            "score": int(r.score),
            "max": 10,
            "tone": _score_tone(int(r.score)),
            "rationale": _strip_file_links(r.rationale),
        }
        for r in applicable
        if r.score is not None
    ]
    return {
        "intro": intro,
        "overall": {"score": sum(scores), "max": 10 * len(applicable)},
        "na_principles": na_ids,
        "rows": out_rows,
    }


def _export_question(q: DesignFinding) -> dict[str, Any]:
    """Serializes one Open Design Question entry."""
    ev_raw = q.fields.get("Evidence", "")
    return {
        "id": q.finding_id,
        "question": q.claim,
        "target": strip_ticks(q.fields.get("Target", "")),
        "evidence_tier": extract_tier(ev_raw),
        "evidence": extract_evidence_detail(ev_raw),
        "why": q.fields.get("Why", ""),
        "to_settle": q.fields.get("To settle", ""),
    }


def export_design_review_json(
    report: DesignReviewReport,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Exports genui-compatible scorecard and triage JSON from a validated report.

    Excludes N/A rows from scorecard.rows[] so a2ui-pane's numeric ProgressBar
    contract validates cleanly, recording excluded IDs in scorecard.na_principles[].
    """
    summary_raw = "\n".join(report.summary_lines)
    vmatch = _VERDICT_RE.search(summary_raw)
    cmatch = _CITATION_RE.search(summary_raw)
    audit = (
        {
            "checked": int(cmatch.group("checked")),
            "dropped": int(cmatch.group("dropped")),
        }
        if cmatch
        else {"checked": 0, "dropped": 0}
    )
    payload: dict[str, Any] = {
        "summary": {
            "overview": _clean_summary_overview(summary_raw),
            "verdict": vmatch.group("verdict") if vmatch else "Minor Revisions Needed",
            "citation_audit": audit,
        },
        "scorecard": _build_scorecard_payload(report.scorecard_rows),
        "view_assessment": [ln.strip() for ln in report.view_lines if ln.strip()],
        "findings": [_export_finding(f) for f in report.findings],
        "questions": [_export_question(q) for q in report.questions],
        "checked_clean": [line for _, line in report.clean_lines],
    }
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parses CLI arguments for lint_design_review.py."""
    parser = argparse.ArgumentParser(
        description="Validate and export /design-review Markdown artifacts."
    )
    parser.add_argument("report", help="Path to design_review_results.md.")
    parser.add_argument(
        "--export-json",
        default=None,
        help="Optional path to write design_review_results.json.",
    )
    parser.add_argument(
        "--post-design",
        action="store_true",
        help="Validate in --post-design caller mode.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point returning 0 (clean), 1 (lint violations), or 2 (IO/usage error)."""
    try:
        args = _parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    report_path = Path(args.report)
    if not report_path.is_file():
        print(f"error: report file not found: {report_path}", file=sys.stderr)
        return 2
    text = report_path.read_text(encoding="utf-8")
    parsed = parse_design_review(text)
    violations = check_design_review(parsed, post_design=args.post_design)
    if violations:
        for v in violations:
            print(
                f"{report_path}:{v.lineno}: [{v.rule}] {v.message}\n  Fix: {v.fix_hint}",
                file=sys.stderr,
            )
        return 1
    if args.export_json:
        export_design_review_json(parsed, Path(args.export_json))
    print(
        f"OK: {report_path} passed design-review lint ({len(parsed.findings)} finding(s), {len(parsed.questions)} question(s))."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
