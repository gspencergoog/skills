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

_WRITE_PROSE_SCRIPTS = Path(__file__).resolve().parents[2] / "write-prose" / "scripts"
if str(_WRITE_PROSE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_WRITE_PROSE_SCRIPTS))

from analyze_prose import analyze_prose

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

VALID_TIERS = ("Executed", "Read", "Fetched", "Deduction", "Speculation", "Recalled")
CLEAN_TIERS = ("Executed", "Read", "Fetched", "Deduction")
VALID_VERDICTS = ("Ready for /plan", "Revisions Recommended", "Architectural Blockers")
VALID_DOORS = ("one-way", "two-way")
ID_TO_SEVERITY = {"C": "critical", "H": "high", "M": "medium", "L": "low"}
SEVERITY_ORDER = {"C": 0, "H": 1, "M": 2, "L": 3}

MAX_VISIBLE_WORDS = 120
MAX_VISIBLE_SENTENCES = 6
MAX_DETAILS_WORDS = 150
MAX_SENTENCE_WORDS = 30
MAX_SENTENCE_PCC = 5

_H2_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$")
_H3_ITEM_RE = re.compile(r"^###\s+(?P<fid>[CHMLQ]\d+)\.\s+(?P<claim>.+?)\s*$")
_FIELD_RE = re.compile(r"^-\s+\*\*(?P<name>[A-Za-z][A-Za-z ]*)\*\*:\s*(?P<val>.*)$")
_SCORE_CELL_RE = re.compile(
    r"^(?:(?P<num>10|[0-9])(?:\s*/\s*10)?|(?P<na>N/A))$", re.IGNORECASE
)
_FOCUS_RE = re.compile(r"^(?:adhoc:)?[a-z0-9]+(?:-[a-z0-9]+)*$")
_VERDICT_RE = re.compile(
    r"Verdict:\s*`?(?P<verdict>Ready for /plan|Revisions Recommended|Architectural Blockers)`?",
    re.IGNORECASE,
)
_OVERALL_SCORE_RE = re.compile(
    r"Overall\s+Score:\s*`?(?P<num>\d+)\s*/\s*(?P<den>\d+)`?", re.IGNORECASE
)
_CITATION_RE = re.compile(
    r"Citation\s+audit:\s*(?P<checked>\d+)\s+checked,\s*(?P<dropped>\d+)\s+dropped",
    re.IGNORECASE,
)
_CLEAN_BULLET_RE = re.compile(
    r"^-\s+(?P<hyp>.+?):\s*`?(?P<tier>Executed|Read|Fetched|Deduction|Speculation|Recalled)`?\s*(?:[:\-—])\s*(?P<proof>.+)$"
)
_FILE_LINK_RE = re.compile(r"\[(?P<label>[^\]]+)\]\(file://[^\)]+\)")


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


@dataclass(frozen=True)
class Violation:
    """One lint violation with line number, rule code, message, and fix hint."""

    lineno: int
    rule: str
    message: str
    fix_hint: str


def _strip_ticks(value: str) -> str:
    """Strips surrounding whitespace and single backticks."""
    val = value.strip()
    if len(val) >= 2 and val.startswith("`") and val.endswith("`"):
        return val[1:-1].strip()
    return val


def _fence_marker_update(active_fence: str, stripped: str) -> tuple[str, bool]:
    """Updates fence tracking state and returns (new_fence, is_fence_delimiter)."""
    if not stripped.startswith(("```", "~~~")):
        return active_fence, False
    char = stripped[0]
    length = len(stripped) - len(stripped.lstrip(char))
    if not active_fence:
        return char * length, True
    if char == active_fence[0] and length >= len(active_fence):
        return "", True
    return active_fence, False


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
    return _strip_ticks(cell_text), cell_text.strip()


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
    score_raw = _strip_ticks(cells[1])
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
    cursor: _ParseCursor, raw_line: str, stripped: str, is_open_fence: bool
) -> None:
    """Appends a field line or continuation line to the active finding/question."""
    item = cursor.current_item
    if item is None:
        return
    if not cursor.active_fence and not is_open_fence:
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
    cursor: _ParseCursor, lineno: int, raw_line: str, stripped: str, is_open_fence: bool
) -> None:
    """Routes a single line inside the current ## section."""
    sec = cursor.section
    if sec == "Summary":
        cursor.report.summary_lines.append(raw_line)
    elif sec == "Principle Scorecard" and not cursor.active_fence:
        row = _parse_scorecard_line(stripped, lineno)
        if row is not None:
            cursor.report.scorecard_rows.append(row)
    elif sec == "View & Boundary Assessment":
        cursor.report.view_lines.append(raw_line)
    elif sec in {"Findings (Ordered by Severity)", "Open Design Questions"}:
        _handle_item_section_line(cursor, lineno, raw_line, stripped, is_open_fence)
    elif sec == "Checked and Found Clean" and stripped and stripped != "None.":
        cursor.report.clean_lines.append((lineno, stripped))
    elif sec == "Recommended Next Steps":
        cursor.report.next_steps_lines.append(raw_line)


def _handle_item_section_line(
    cursor: _ParseCursor, lineno: int, raw_line: str, stripped: str, is_open_fence: bool
) -> None:
    """Parses ### headings and field bullets in Findings or Open Design Questions."""
    if not cursor.active_fence and not is_open_fence:
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
        _append_item_line(cursor, raw_line, stripped, is_open_fence)
    elif stripped and stripped != "None.":
        cursor.report.unparsed_item_lines.append((lineno, stripped))


def parse_design_review(text: str) -> DesignReviewReport:
    """Parses a design_review_results.md file into a DesignReviewReport AST."""
    report = DesignReviewReport()
    cursor = _ParseCursor(report=report)
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        was_fenced = bool(cursor.active_fence)
        cursor.active_fence, is_delim = _fence_marker_update(
            cursor.active_fence, stripped
        )
        is_open_fence = is_delim and not was_fenced
        if not was_fenced and not is_delim:
            h2 = _H2_RE.match(stripped)
            if h2:
                title = h2.group("title").strip()
                report.sections_seen.append((lineno, title))
                cursor.section = title
                cursor.current_item = None
                cursor.current_field = ""
                continue
        _handle_section_body_line(cursor, lineno, raw_line, stripped, is_open_fence)
    return report


def _check_verdict_line(
    summary_lines: Sequence[str], findings: Sequence[DesignFinding]
) -> list[Violation]:
    """Validates Summary Verdict presence and consistency with C/H findings."""
    vmatch = _VERDICT_RE.search("\n".join(summary_lines))
    if not vmatch:
        return [
            Violation(
                1,
                "missing-verdict",
                "Summary lacks a valid 'Verdict:' line.",
                f"End ## Summary with 'Verdict: <{' | '.join(VALID_VERDICTS)}>'.",
            )
        ]
    has_blocker = any(f.finding_id.startswith(("C", "H")) for f in findings)
    if vmatch.group("verdict").lower() == "ready for /plan" and has_blocker:
        return [
            Violation(
                1,
                "verdict-contradicts-findings",
                "Verdict 'Ready for /plan' contradicts critical or high findings.",
                "Change Verdict to 'Revisions Recommended' or 'Architectural Blockers'.",
            )
        ]
    return []


def _check_sections(report: DesignReviewReport) -> list[Violation]:
    """Checks required section presence, ordering, and absence of Playbook Feedback."""
    violations: list[Violation] = []
    titles = [t for _, t in report.sections_seen]
    for lineno, title in report.sections_seen:
        if title.lower() == "playbook feedback":
            violations.append(
                Violation(
                    lineno,
                    "playbook-feedback-leaked",
                    "Leaked '## Playbook Feedback' section.",
                    "Remove '## Playbook Feedback' from the synthesized report.",
                )
            )
    filtered = [t for t in titles if t in EXPECTED_SECTIONS]
    if tuple(filtered) != EXPECTED_SECTIONS:
        missing = [s for s in EXPECTED_SECTIONS if s not in titles]
        msg = (
            f"Missing section(s): {', '.join(missing)}."
            if missing
            else f"Sections out of canonical order: {filtered}."
        )
        violations.append(
            Violation(
                1,
                "section-order",
                msg,
                f"Include exact ## headings in order: {', '.join(EXPECTED_SECTIONS)}.",
            )
        )
    violations.extend(_check_verdict_line(report.summary_lines, report.findings))
    return violations


def _check_scorecard_row(row: PrincipleRow) -> list[Violation]:
    """Validates a single Principle Scorecard row."""
    out: list[Violation] = []
    if row.principle_id not in PRINCIPLE_IDS:
        out.append(
            Violation(
                row.lineno,
                "scorecard-unknown-principle",
                f"Unknown principle '{row.principle_id}'.",
                f"Use one of: {', '.join(PRINCIPLE_IDS)}.",
            )
        )
    elif row.score == -1:
        out.append(
            Violation(
                row.lineno,
                "scorecard-invalid-score",
                f"Invalid score cell for '{row.principle_id}'.",
                "Write an integer 0..10 (e.g. 8/10) or N/A.",
            )
        )
    if not row.rationale.strip():
        out.append(
            Violation(
                row.lineno,
                "scorecard-empty-rationale",
                f"Empty rationale for '{row.principle_id}'.",
                "Provide a concise rationale in the Assessment column.",
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
            1,
            "scorecard-math-mismatch",
            f"Overall Score {omatch.group('num')}/{omatch.group('den')} does not match scorecard sum {exp_num}/{exp_den}.",
            f"Set Overall Score to {exp_num}/{exp_den}.",
        )
    ]


def _check_scorecard(report: DesignReviewReport) -> list[Violation]:
    """Validates Principle Scorecard completeness, duplicates, scores, and math."""
    seen_list = [r.principle_id for r in report.scorecard_rows]
    seen_ids = set(seen_list)
    violations = [
        Violation(
            1,
            "scorecard-missing-principle",
            f"Scorecard missing principle '{pid}'.",
            f"Add a row for `{pid}` scored 0..10 or N/A.",
        )
        for pid in PRINCIPLE_IDS
        if pid not in seen_ids
    ]
    if len(seen_list) != len(seen_ids):
        violations.append(
            Violation(
                1,
                "scorecard-duplicate-principle",
                "Scorecard contains duplicate principle row(s).",
                "Include each of the 7 principles at most once.",
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
                report.scorecard_rows[0].lineno,
                "scorecard-all-na",
                "All principles are marked N/A.",
                "Score at least one applicable principle 0..10.",
            )
        )
    violations.extend(_check_scorecard_math(report.summary_lines, applicable))
    return violations


def _extract_tier(evidence_val: str) -> tuple[str, str]:
    """Splits an Evidence field into (tier, detail)."""
    raw = evidence_val.strip()
    for sep in (":", "—", " - "):
        if sep in raw:
            head, tail = raw.split(sep, 1)
            return _strip_ticks(head), tail.strip()
    return _strip_ticks(raw), ""


def _check_finding_sequence(findings: Sequence[DesignFinding]) -> list[Violation]:
    """Checks severity ordering (C, H, M, L) and 1-based sequential numbering."""
    violations: list[Violation] = []
    last_rank = -1
    counts: dict[str, int] = {"C": 0, "H": 0, "M": 0, "L": 0}
    for item in findings:
        prefix = item.finding_id[0]
        num = int(item.finding_id[1:])
        rank = SEVERITY_ORDER.get(prefix, 99)
        if rank < last_rank:
            violations.append(
                Violation(
                    item.lineno,
                    "finding-order",
                    f"Finding {item.finding_id} appears out of severity order.",
                    "Order findings C1.., H1.., M1.., L1..",
                )
            )
        last_rank = max(last_rank, rank)
        counts[prefix] = counts.get(prefix, 0) + 1
        if num != counts[prefix]:
            violations.append(
                Violation(
                    item.lineno,
                    "finding-id-sequence",
                    f"Expected {prefix}{counts[prefix]}, found {item.finding_id}.",
                    f"Renumber {prefix} findings sequentially starting at {prefix}1.",
                )
            )
    return violations


def _check_single_finding(item: DesignFinding) -> list[Violation]:
    """Checks metadata, severity caps, and fence rules on one finding."""
    violations: list[Violation] = []
    required = (
        "Target",
        "Door",
        "Severity",
        "Principle",
        "Evidence",
        "Body",
        "Why",
        "Suggestion",
    )
    for req in required:
        if not item.fields.get(req, "").strip():
            violations.append(
                Violation(
                    item.lineno,
                    "missing-field",
                    f"{item.finding_id} is missing required field '{req}'.",
                    f"Add '- **{req}**: ...' to {item.finding_id}.",
                )
            )
    sev = _strip_ticks(item.fields.get("Severity", "")).lower()
    exp_sev = ID_TO_SEVERITY.get(item.finding_id[0], "")
    if sev and sev != exp_sev:
        violations.append(
            Violation(
                item.lineno,
                "severity-mismatch",
                f"{item.finding_id} has Severity '{sev}' (expected '{exp_sev}').",
                f"Set Severity to `{exp_sev}` or rename the heading.",
            )
        )
    door = _strip_ticks(item.fields.get("Door", "")).lower()
    if door and door not in VALID_DOORS:
        violations.append(
            Violation(
                item.lineno,
                "invalid-door",
                f"{item.finding_id} has invalid Door '{door}'.",
                "Use `one-way` or `two-way`.",
            )
        )
    principle = _strip_ticks(item.fields.get("Principle", ""))
    if principle and principle not in PRINCIPLE_IDS:
        violations.append(
            Violation(
                item.lineno,
                "invalid-principle",
                f"{item.finding_id} names unknown Principle '{principle}'.",
                f"Use one of: {', '.join(PRINCIPLE_IDS)}.",
            )
        )
    if "Focus" in item.fields and not _FOCUS_RE.match(
        _strip_ticks(item.fields["Focus"])
    ):
        violations.append(
            Violation(
                item.lineno,
                "focus-id-format",
                f"{item.finding_id} has invalid Focus id.",
                "Use kebab-case '<playbook-id>' or 'adhoc:<slug>'.",
            )
        )
    violations.extend(_check_evidence_cap(item, exp_sev))
    if item.fence_counts.get("Suggestion", 0) > 1:
        violations.append(
            Violation(
                item.lineno,
                "suggestion-single-fence",
                f"{item.finding_id} Suggestion contains multiple code fences.",
                "Keep at most one fenced block in Suggestion; move extras to Details.",
            )
        )
    return violations


def _check_evidence_cap(item: DesignFinding, severity: str) -> list[Violation]:
    """Enforces evidence tier validity and severity caps."""
    ev_raw = item.fields.get("Evidence", "").strip()
    if not ev_raw:
        return []
    tier, detail = _extract_tier(ev_raw)
    if tier not in VALID_TIERS:
        return [
            Violation(
                item.lineno,
                "invalid-evidence-tier",
                f"{item.finding_id} uses unknown tier '{tier}'.",
                "Use Executed, Read, Fetched, Deduction, or Speculation.",
            )
        ]
    if tier == "Recalled":
        return [
            Violation(
                item.lineno,
                "recalled-forbidden",
                f"{item.finding_id} uses forbidden tier 'Recalled'.",
                "Read the file on disk or downgrade to an Open Design Question.",
            )
        ]
    if not detail:
        return [
            Violation(
                item.lineno,
                "missing-evidence-detail",
                f"{item.finding_id} Evidence lacks proof detail.",
                "Follow the tier with ': <exact quote, path:line, or command output>'.",
            )
        ]
    if severity in {"critical", "high"} and tier == "Speculation":
        return [
            Violation(
                item.lineno,
                "severity-cap",
                f"{item.finding_id} ({severity}) cannot rely on Speculation.",
                "Supply Read/Executed/Deduction proof, cap at medium, or move to Questions.",
            )
        ]
    return []


def _strip_fenced_blocks(text: str) -> str:
    """Removes fenced code blocks before counting prose words and sentences."""
    out: list[str] = []
    fence = ""
    for line in text.splitlines():
        stripped = line.strip()
        was_fenced = bool(fence)
        fence, is_delim = _fence_marker_update(fence, stripped)
        if not was_fenced and not is_delim:
            out.append(line)
    return "\n".join(out).strip()


def _check_prose_block(
    lineno: int, label: str, visible_text: str, details_text: str = ""
) -> list[Violation]:
    """Validates prose length and write-prose lexical/syntactic rules."""
    violations: list[Violation] = []
    clean_visible = _strip_fenced_blocks(visible_text)
    if clean_visible:
        res = analyze_prose(
            "-",
            complexity_threshold=MAX_SENTENCE_PCC,
            engine="stdlib",
            text=clean_visible,
        )
        summary = res.get("summary", {})
        vdict = res.get("violations", {})
        words = int(summary.get("total_words", 0))
        sents = int(summary.get("total_sentences", 0))
        if words > MAX_VISIBLE_WORDS or sents > MAX_VISIBLE_SENTENCES:
            violations.append(
                Violation(
                    lineno,
                    "comment-prose-length",
                    f"{label} visible prose is {words} words / {sents} sentences (max {MAX_VISIBLE_WORDS} words, {MAX_VISIBLE_SENTENCES} sentences).",
                    "Shorten Body/Why/Suggestion and move deep traces to '- **Details**:'.",
                )
            )
        violations.extend(_collect_prose_violations(lineno, label, vdict))
    clean_details = _strip_fenced_blocks(details_text)
    if clean_details and len(clean_details.split()) > MAX_DETAILS_WORDS:
        violations.append(
            Violation(
                lineno,
                "comment-details-length",
                f"{label} Details exceeds {MAX_DETAILS_WORDS} words.",
                f"Trim Details to <= {MAX_DETAILS_WORDS} words.",
            )
        )
    return violations


def _collect_prose_violations(
    lineno: int, label: str, vdict: dict[str, Any]
) -> list[Violation]:
    """Extracts sentence length, complexity, and AI-ism violations from analyze_prose."""
    out: list[Violation] = []
    long_sents = [
        s
        for s in vdict.get("sentences_exceeding_25_words", [])
        if int(s.get("word_count", 0)) > MAX_SENTENCE_WORDS
    ]
    if long_sents:
        out.append(
            Violation(
                lineno,
                "comment-sentence-length",
                f"{label} has sentence(s) over {MAX_SENTENCE_WORDS} words.",
                f"Split sentences so each is <= {MAX_SENTENCE_WORDS} words.",
            )
        )
    if vdict.get("complex_sentences_found"):
        out.append(
            Violation(
                lineno,
                "comment-sentence-complexity",
                f"{label} exceeds sentence complexity PCC {MAX_SENTENCE_PCC}.",
                "Split nested clauses into shorter direct sentences.",
            )
        )
    ai_flags: list[str] = []
    for key in (
        "banned_ai_words_found",
        "banned_ai_phrases_found",
        "em_dashes_found",
        "noun_clusters_found",
    ):
        if vdict.get(key):
            ai_flags.append(key)
    if ai_flags:
        out.append(
            Violation(
                lineno,
                "comment-ai-ism",
                f"{label} triggered write-prose rule(s): {', '.join(ai_flags)}.",
                "Remove banned AI words, em-dashes, and 4+ noun stacks.",
            )
        )
    return out


def _check_single_question(idx: int, q: DesignFinding) -> list[Violation]:
    """Validates one ### Q<n> block in ## Open Design Questions."""
    violations: list[Violation] = []
    if q.finding_id != f"Q{idx}":
        violations.append(
            Violation(
                q.lineno,
                "question-id-sequence",
                f"Expected Q{idx}, found {q.finding_id}.",
                "Number questions sequentially Q1..Q3.",
            )
        )
    for req in ("Target", "Evidence", "Why", "To settle"):
        if not q.fields.get(req, "").strip():
            violations.append(
                Violation(
                    q.lineno,
                    "missing-question-field",
                    f"{q.finding_id} lacks '{req}'.",
                    f"Add '- **{req}**: ...' to {q.finding_id}.",
                )
            )
    tier, detail = _extract_tier(q.fields.get("Evidence", ""))
    if tier and (tier not in {"Speculation", "Deduction"} or not detail):
        violations.append(
            Violation(
                q.lineno,
                "question-evidence",
                f"{q.finding_id} Evidence must be Speculation or Deduction with detail.",
                "Format as 'Speculation: <what was checked>'.",
            )
        )
    violations.extend(
        _check_prose_block(
            q.lineno, q.finding_id, f"{q.claim} {q.fields.get('Why', '')}"
        )
    )
    return violations


def _check_clean_bullets(clean_lines: Sequence[tuple[int, str]]) -> list[Violation]:
    """Validates ## Checked and Found Clean bullets and cap."""
    violations: list[Violation] = []
    if len(clean_lines) > 10:
        violations.append(
            Violation(
                clean_lines[10][0],
                "clean-cap",
                f"Checked and Found Clean has {len(clean_lines)} items (max 10).",
                "Cap disproved hypotheses at 10 bullets.",
            )
        )
    for lineno, line in clean_lines:
        cmatch = _CLEAN_BULLET_RE.match(line)
        if not cmatch or cmatch.group("tier") not in CLEAN_TIERS:
            violations.append(
                Violation(
                    lineno,
                    "clean-format",
                    "Checked and Found Clean bullet lacks a valid proof tier.",
                    "Format as '- <hypothesis>: `Read`: <exact disproof>'.",
                )
            )
    return violations


def _check_questions_and_clean(report: DesignReviewReport) -> list[Violation]:
    """Validates ## Open Design Questions and ## Checked and Found Clean."""
    violations: list[Violation] = []
    if len(report.questions) > 3:
        violations.append(
            Violation(
                report.questions[3].lineno,
                "question-cap",
                f"Found {len(report.questions)} questions (max 3).",
                "Keep at most 3 highest-stakes questions.",
            )
        )
    for idx, q in enumerate(report.questions, start=1):
        violations.extend(_check_single_question(idx, q))
    violations.extend(_check_clean_bullets(report.clean_lines))
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
                lineno,
                "unparsed-item-line",
                f"Unanchored text in item section: '{text[:50]}'.",
                "Place every comment under a '### C1/H1/M1/L1/Q1.' heading or write 'None.'.",
            )
        )
    violations.extend(_check_finding_sequence(report.findings))
    for item in report.findings:
        violations.extend(_check_single_finding(item))
        visible = "\n".join(
            [
                item.fields.get("Body", ""),
                item.fields.get("Why", ""),
                item.fields.get("Suggestion", ""),
            ]
        )
        violations.extend(
            _check_prose_block(
                item.lineno, item.finding_id, visible, item.fields.get("Details", "")
            )
        )
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
    sev = _strip_ticks(item.fields.get("Severity", "")).lower()
    tier, detail = _extract_tier(item.fields.get("Evidence", ""))
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
        "target": _strip_ticks(item.fields.get("Target", "")),
        "door": _strip_ticks(item.fields.get("Door", "")).lower(),
        "principle": _strip_ticks(item.fields.get("Principle", "")),
        "focus": _strip_ticks(item.fields.get("Focus", "")) or None,
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
            "trace": _strip_ticks(item.fields.get("Target", "")),
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
    tier, detail = _extract_tier(q.fields.get("Evidence", ""))
    return {
        "id": q.finding_id,
        "question": q.claim,
        "target": _strip_ticks(q.fields.get("Target", "")),
        "evidence_tier": tier,
        "evidence": detail,
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
            "verdict": vmatch.group("verdict") if vmatch else "Revisions Recommended",
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
