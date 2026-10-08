#!/usr/bin/env python3
"""Linter for synthesized code-review artifacts (review_results.md)."""

from dataclasses import dataclass, field
import pathlib
import re
import sys
from typing import Callable

VALID_TIERS = ("Executed", "Read", "Fetched", "Deduction", "Speculation", "Recalled")
CLEAN_TIERS = ("Executed", "Read", "Fetched", "Deduction")
ID_TO_SEVERITY = {"C": "critical", "H": "high", "M": "medium", "L": "low"}
CLASS_ORDER = {"C": 0, "H": 1, "M": 2, "L": 3}

HEADING_RE = re.compile(r"^###\s+([CHMLQ]\d+)\.\s+\S")
SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")
FIELD_RE = re.compile(r"^-\s+\*\*([^*]+)\*\*:\s*(.*)$")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
EMPTY_SECTION_TOKENS = {"", "none", "none.", "no findings", "no findings.", "no issues", "no issues."}


@dataclass
class Finding:
    """A parsed review finding or question block."""

    id: str
    severity: str
    fields: dict[str, str]
    line: int


@dataclass
class CleanLine:
    """A bullet line under Checked and Found Clean."""

    text: str
    line: int


@dataclass
class Report:
    """Parsed structure of a review_results.md artifact."""

    findings: list[Finding] = field(default_factory=list)
    questions: list[Finding] = field(default_factory=list)
    clean_lines: list[CleanLine] = field(default_factory=list)
    review_comments_text: str = ""
    review_comments_line: int = 1
    verified_lines: list[int] = field(default_factory=list)


@dataclass
class Violation:
    """A single lint violation with 1-based line number and rule identifier."""

    line: int
    rule: str
    message: str


def _strip_ticks(value: str) -> str:
    return value.strip().strip("`").strip()


def _extract_tier(evidence_value: str) -> str:
    if not evidence_value:
        return ""
    first_part = re.split(r"\s*(?:—|--|-|:)\s*|\s+", evidence_value.strip(), maxsplit=1)[0]
    cleaned = first_part.strip("`*_ ")
    for tier in VALID_TIERS:
        if cleaned.lower() == tier.lower():
            return tier
    return ""


def _classify_section(title: str) -> str:
    lowered = title.lower()
    if lowered.startswith("review comments"):
        return "comments"
    if lowered.startswith("questions"):
        return "questions"
    if lowered.startswith("checked and found clean"):
        return "clean"
    return "other"


def _update_fence(fence_marker: str, line: str) -> tuple[str, bool]:
    match = FENCE_RE.match(line)
    if not match:
        return fence_marker, bool(fence_marker)
    token = match.group(1)
    if not fence_marker:
        return token, True
    if token[0] == fence_marker[0] and len(token) >= len(fence_marker):
        return "", True
    return fence_marker, True


def _record_section_line(
    report: Report,
    section: str,
    current: Finding | None,
    stripped: str,
    lineno: int,
) -> Finding | None:
    HeadingMatch = HEADING_RE.match(stripped)
    if HeadingMatch and section in ("comments", "questions"):
        finding = Finding(id=HeadingMatch.group(1), severity="", fields={}, line=lineno)
        target = report.questions if section == "questions" else report.findings
        target.append(finding)
        return finding

    field_match = FIELD_RE.match(stripped)
    if field_match and current is not None:
        key = field_match.group(1).strip()
        val = field_match.group(2).strip()
        current.fields[key] = val
        if key.lower() == "severity":
            current.severity = _strip_ticks(val).lower()
        return current

    if section == "clean" and stripped.startswith("- "):
        report.clean_lines.append(CleanLine(text=stripped[2:].strip(), line=lineno))
    elif section == "comments" and current is None and stripped:
        report.review_comments_text += stripped + "\n"
    return current


def parse_report(text: str) -> Report:
    """Parse a markdown review report into findings, questions, and clean lines."""
    report = Report()
    section = "other"
    current: Finding | None = None
    fence_marker = ""

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        fence_marker, in_fence = _update_fence(fence_marker, raw_line)
        if in_fence:
            continue

        stripped = raw_line.strip()
        field_match = FIELD_RE.match(stripped)
        if field_match and field_match.group(1).strip().lower() == "verified":
            report.verified_lines.append(lineno)

        section_match = SECTION_RE.match(stripped)
        if section_match:
            section = _classify_section(section_match.group(1))
            if section == "comments":
                report.review_comments_line = lineno
            current = None
            continue

        current = _record_section_line(report, section, current, stripped, lineno)

    return report


def _check_unparsed_comments(report: Report) -> list[Violation]:
    norm = report.review_comments_text.strip().lower()
    if not report.findings and norm not in EMPTY_SECTION_TOKENS:
        return [
            Violation(
                report.review_comments_line,
                "review-comments-unparsed",
                "Review Comments has text but no '### <ID>. <claim>' findings were parsed.",
            )
        ]
    return []


def _check_sequence_for_group(items: list[Finding], expected_prefix: str | None = None) -> list[Violation]:
    violations: list[Violation] = []
    counts: dict[str, int] = {}
    last_order = -1

    for item in items:
        prefix = item.id[0]
        number = int(item.id[1:])
        if expected_prefix and prefix != expected_prefix:
            violations.append(
                Violation(item.line, "ids-sequential", f"Expected '{expected_prefix}' ID, got '{item.id}'.")
            )
            continue
        if not expected_prefix:
            order = CLASS_ORDER.get(prefix, 99)
            if order < last_order:
                violations.append(
                    Violation(
                        item.line,
                        "ids-sequential",
                        f"Finding '{item.id}' appears out of severity class order (C, H, M, L).",
                    )
                )
            last_order = max(last_order, order)

        expected_num = counts.get(prefix, 0) + 1
        counts[prefix] = expected_num
        if number != expected_num:
            violations.append(
                Violation(
                    item.line,
                    "ids-sequential",
                    f"Expected '{prefix}{expected_num}', got '{item.id}'.",
                )
            )
    return violations


def _check_ids_sequential(report: Report) -> list[Violation]:
    return _check_sequence_for_group(report.findings) + _check_sequence_for_group(report.questions, "Q")


def _check_severity_matches_id(report: Report) -> list[Violation]:
    violations: list[Violation] = []
    for item in report.findings:
        expected = ID_TO_SEVERITY.get(item.id[0])
        if expected and item.severity != expected:
            violations.append(
                Violation(
                    item.line,
                    "severity-matches-id",
                    f"Finding '{item.id}' has severity '{item.severity or 'missing'}', expected '{expected}'.",
                )
            )
    return violations


def _check_evidence_and_caps(report: Report) -> list[Violation]:
    violations: list[Violation] = []
    for item in report.findings + report.questions:
        ev = item.fields.get("Evidence", "")
        tier = _extract_tier(ev)
        if not tier:
            violations.append(
                Violation(
                    item.line,
                    "evidence-required",
                    f"'{item.id}' is missing a valid Evidence tier ({', '.join(VALID_TIERS[:-1])}).",
                )
            )
            continue
        if tier == "Recalled":
            violations.append(
                Violation(item.line, "evidence-recalled", f"'{item.id}' uses forbidden tier 'Recalled'.")
            )
        if item in report.findings and item.severity in ("critical", "high") and tier in ("Speculation", "Recalled"):
            violations.append(
                Violation(
                    item.line,
                    "severity-cap",
                    f"'{item.id}' ({item.severity}) cannot rest on '{tier}' evidence.",
                )
            )
    return violations


def _check_why_required(report: Report) -> list[Violation]:
    violations: list[Violation] = []
    for item in report.findings + report.questions:
        why = _strip_ticks(item.fields.get("Why", ""))
        if not why:
            violations.append(
                Violation(item.line, "why-required", f"'{item.id}' is missing a non-empty 'Why' field.")
            )
    return violations


def _check_questions(report: Report) -> list[Violation]:
    violations: list[Violation] = []
    if len(report.questions) > 3:
        violations.append(
            Violation(
                report.questions[3].line,
                "question-fields",
                f"Questions section has {len(report.questions)} entries (maximum is 3).",
            )
        )
    for q in report.questions:
        if not _strip_ticks(q.fields.get("To settle", "")):
            violations.append(
                Violation(q.line, "question-fields", f"Question '{q.id}' is missing a 'To settle' field.")
            )
    return violations


def _check_clean_section(report: Report) -> list[Violation]:
    violations: list[Violation] = []
    if len(report.clean_lines) > 10:
        violations.append(
            Violation(
                report.clean_lines[10].line,
                "clean-max-10",
                f"Checked and Found Clean has {len(report.clean_lines)} lines (maximum is 10).",
            )
        )
    for cl in report.clean_lines:
        has_tier = any(re.search(rf"\b{tier}\b", cl.text) for tier in CLEAN_TIERS)
        if not has_tier:
            violations.append(
                Violation(
                    cl.line,
                    "clean-line-tier",
                    f"Checked and Found Clean line lacks a valid tier ({', '.join(CLEAN_TIERS)}).",
                )
            )
    return violations


def _check_verified_removed(report: Report) -> list[Violation]:
    return [
        Violation(line, "verified-removed", "Deprecated '**Verified**:' field found; use '**Evidence**: `Executed`'.")
        for line in report.verified_lines
    ]


RULES: list[Callable[[Report], list[Violation]]] = [
    _check_unparsed_comments,
    _check_ids_sequential,
    _check_severity_matches_id,
    _check_evidence_and_caps,
    _check_why_required,
    _check_questions,
    _check_clean_section,
    _check_verified_removed,
]


def check_report(report: Report) -> list[Violation]:
    """Run all lint rules against a parsed Report."""
    violations: list[Violation] = []
    for rule_fn in RULES:
        violations.extend(rule_fn(report))
    return sorted(violations, key=lambda v: (v.line, v.rule))


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: lint_review.py <review_results.md>."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("Usage: lint_review.py <review_results.md>", file=sys.stderr)
        return 2
    path = pathlib.Path(args[0])
    if not path.is_file():
        print(f"Error: file not found: {path}", file=sys.stderr)
        return 2
    report = parse_report(path.read_text(encoding="utf-8"))
    violations = check_report(report)
    for v in violations:
        print(f"{v.line}: {v.rule}: {v.message}")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
