#!/usr/bin/env python3
"""Shared report parsing, structural checks, and prose linting for review skills."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

VALID_TIERS = ("Executed", "Read", "Fetched", "Deduction", "Speculation", "Recalled")
CLEAN_TIERS = ("Executed", "Read", "Fetched", "Deduction")
ID_TO_SEVERITY = {"C": "critical", "H": "high", "M": "medium", "L": "low"}
CLASS_ORDER = {"C": 0, "H": 1, "M": 2, "L": 3}
VALID_VERDICTS = ("Ready", "Minor Revisions Needed", "Critical Blockers")

VERDICT_LINE_RE = re.compile(
    r"\bverdict\b\W*(critical blockers?|minor revisions? needed|ready)\b",
    re.IGNORECASE,
)

MAX_SENTENCE_WORDS = 30
MAX_SENTENCE_PCC = 5

_SECTION_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$")
_ITEM_HEADING_RE = re.compile(r"^###\s+(?P<fid>[A-Z]+\d+)\.\s*(?P<title>.*?)\s*$")
_FIELD_RE = re.compile(r"^-\s+\*\*([^*]+)\*\*:\s*(.*)$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")


@dataclass(frozen=True)
class ReportProfile:
    """Configuration defining one review report format and its caps."""

    name: str
    sections: tuple[str, ...]
    findings_section: str
    questions_section: str
    required_fields: tuple[str, ...]
    optional_fields: tuple[str, ...] = ()
    max_words: int = 120
    max_sentences: int = 6
    details_words: int = 150
    validators: dict[str, re.Pattern[str]] = field(default_factory=dict)
    rule_names: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Violation:
    """One lint violation shared across code-review and design-review."""

    rule: str
    message: str
    line: int = 1
    fix_hint: str = ""

    @property
    def lineno(self) -> int:
        """Alias used by design-review callers."""
        return self.line

    @property
    def fix(self) -> str:
        """Alias used by code-review callers."""
        return self.fix_hint


@dataclass
class FenceMeta:
    """Metadata describing fenced code blocks inside one finding field."""

    fenced: bool = False
    info: str = ""
    lead_in: str = ""
    extra_fences: int = 0


@dataclass
class Item:
    """One parsed `### <ID>. <title>` finding or question block."""

    id: str
    title: str
    fields: dict[str, str]
    line: int
    fence_meta: dict[str, FenceMeta] = field(default_factory=dict)

    def fence(self, field_name: str) -> FenceMeta:
        """Returns the FenceMeta entry for `field_name`, creating it if absent."""
        return self.fence_meta.setdefault(field_name, FenceMeta())


@dataclass
class ParsedReport:
    """Structured sections, findings, questions, and clean bullets of a report."""

    sections: dict[str, list[tuple[int, str]]]
    findings: list[Item]
    questions: list[Item]
    clean_lines: list[tuple[int, str]]
    section_lines: list[tuple[int, str]] = field(default_factory=list)


def strip_ticks(value: str) -> str:
    """Strips surrounding whitespace and single backtick wrappers."""
    val = value.strip()
    if len(val) >= 2 and val.startswith("`") and val.endswith("`"):
        return val[1:-1].strip()
    return val.strip("`").strip()


def update_fence(fence_marker: str, line: str) -> tuple[str, bool]:
    """Tracks fenced-code state; returns `(new_fence_marker, is_in_or_delimiter)`."""
    match = _FENCE_RE.match(line)
    if not match:
        return fence_marker, bool(fence_marker)
    token = match.group(1)
    if not fence_marker:
        return token, True
    if token[0] == fence_marker[0] and len(token) >= len(fence_marker):
        return "", True
    return fence_marker, True


def extract_tier(evidence_value: str) -> str:
    """Extracts the leading evidence tier token from an Evidence field value."""
    if not evidence_value:
        return ""
    first_part = re.split(
        r"\s*(?:—|--|-|:)\s*|\s+", evidence_value.strip(), maxsplit=1
    )[0]
    cleaned = first_part.strip("`*_ ")
    for tier in VALID_TIERS:
        if cleaned.lower() == tier.lower():
            return tier
    return ""


def extract_evidence_detail(evidence_value: str) -> str:
    """Returns the proof text following the leading evidence tier token."""
    if not evidence_value:
        return ""
    parts = re.split(r"\s*(?:—|--|-|:)\s*", evidence_value.strip(), maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def strip_fenced_blocks(text: str) -> str:
    """Removes fenced code blocks before counting prose words and sentences."""
    out: list[str] = []
    fence = ""
    for line in text.splitlines():
        was_fenced = bool(fence)
        fence, active = update_fence(fence, line)
        if not was_fenced and not active:
            out.append(line)
    return "\n".join(out).strip()


def parse_sections(text: str) -> dict[str, list[tuple[int, str]]]:
    """Splits a Markdown report into `## <heading>` buckets outside code fences."""
    sections: dict[str, list[tuple[int, str]]] = {}
    current = ""
    fence = ""
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        was_fenced = bool(fence)
        fence, active = update_fence(fence, raw_line)
        if was_fenced or active:
            if current:
                sections[current].append((lineno, raw_line))
            continue
        match = _SECTION_RE.match(raw_line.strip())
        if match:
            current = match.group("title").strip()
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append((lineno, raw_line))
    return sections


def _append_item_continuation(
    item: Item,
    current_field: str,
    raw_line: str,
    was_fenced: bool,
    fence_marker: str,
    fenced_fields: tuple[str, ...],
) -> None:
    """Appends a continuation or fenced-block line to the active field."""
    if not current_field:
        return
    if not was_fenced and fence_marker and current_field in fenced_fields:
        meta = item.fence(current_field)
        if meta.fenced:
            meta.extra_fences += 1
        else:
            meta.fenced = True
            meta.info = raw_line.strip().lstrip("`~").strip()
            meta.lead_in = item.fields.get(current_field, "")
        return
    prev = item.fields.get(current_field, "")
    item.fields[current_field] = f"{prev}\n{raw_line}".rstrip() if prev else raw_line


def _parse_unquoted_item_line(
    raw_line: str,
    lineno: int,
    id_prefixes: tuple[str, ...],
    items: list[Item],
    current: Item | None,
    current_field: str,
) -> tuple[Item | None, str]:
    stripped = raw_line.strip()
    h3 = _ITEM_HEADING_RE.match(stripped)
    if h3 and h3.group("fid")[0] in id_prefixes:
        new_item = Item(
            id=h3.group("fid"),
            title=h3.group("title").strip(),
            fields={},
            line=lineno,
        )
        items.append(new_item)
        return new_item, ""
    if current is None:
        return None, current_field
    fmatch = _FIELD_RE.match(stripped)
    if fmatch:
        fname = fmatch.group(1).strip()
        current.fields[fname] = fmatch.group(2).strip()
        return current, fname
    if current_field and stripped:
        prev = current.fields.get(current_field, "")
        current.fields[current_field] = f"{prev}\n{stripped}".strip()
    return current, current_field


def parse_items(
    section_lines: Sequence[tuple[int, str]],
    id_prefixes: tuple[str, ...] = ("C", "H", "M", "L", "Q"),
    fenced_fields: tuple[str, ...] = ("Suggestion", "Repro test"),
) -> list[Item]:
    """Parses `### <ID>. <title>` blocks from a section's `(lineno, line)` pairs."""
    items: list[Item] = []
    current: Item | None = None
    current_field = ""
    fence = ""
    for lineno, raw_line in section_lines:
        was_fenced = bool(fence)
        fence, active = update_fence(fence, raw_line)
        if was_fenced or active:
            if current is not None:
                _append_item_continuation(
                    current, current_field, raw_line, was_fenced, fence, fenced_fields
                )
            continue
        current, current_field = _parse_unquoted_item_line(
            raw_line, lineno, id_prefixes, items, current, current_field
        )
    return items


def _rule_code(profile: ReportProfile | None, default: str) -> str:
    if profile is None:
        return default
    return profile.rule_names.get(default, default)


def check_sections(report: ParsedReport, profile: ReportProfile) -> list[Violation]:
    """Checks section ordering, required headings, and Playbook Feedback leaks."""
    violations: list[Violation] = []
    for title in report.sections:
        if title.lower() == "playbook feedback":
            line = report.sections[title][0][0] if report.sections[title] else 1
            violations.append(
                Violation(
                    rule=_rule_code(profile, "playbook-feedback-leaked"),
                    message="Playbook Feedback section must be stripped before writing the final report.",
                    line=line,
                )
            )
    if not profile.sections:
        return violations
    seen_titles = list(report.sections.keys())
    filtered = [t for t in seen_titles if t in profile.sections]
    if tuple(filtered) != profile.sections:
        missing = [s for s in profile.sections if s not in seen_titles]
        msg = (
            f"Missing section(s): {', '.join(missing)}."
            if missing
            else f"Sections out of canonical order: {filtered}."
        )
        violations.append(
            Violation(
                rule=_rule_code(profile, "section-order"),
                message=msg,
                line=1,
                fix_hint=f"Include exact ## headings in order: {', '.join(profile.sections)}.",
            )
        )
    return violations


def check_id_sequence(
    items: Sequence[Item],
    groups: tuple[str, ...] = ("C", "H", "M", "L"),
    expected_prefix: str | None = None,
    order_rule: str = "ids-sequential",
    seq_rule: str = "ids-sequential",
) -> list[Violation]:
    """Checks severity class order and 1-based sequential numbering."""
    violations: list[Violation] = []
    counts: dict[str, int] = {}
    last_order = -1
    order_map = {g: idx for idx, g in enumerate(groups)}
    for item in items:
        prefix = item.id[0]
        number = int(item.id[1:])
        if expected_prefix and prefix != expected_prefix:
            violations.append(
                Violation(
                    seq_rule,
                    f"Expected '{expected_prefix}' ID, got '{item.id}'.",
                    item.line,
                )
            )
            continue
        if not expected_prefix:
            order = order_map.get(prefix, 99)
            if order < last_order:
                violations.append(
                    Violation(
                        order_rule,
                        f"Finding '{item.id}' appears out of severity class order ({', '.join(groups)}).",
                        item.line,
                    )
                )
            last_order = max(last_order, order)
        expected_num = counts.get(prefix, 0) + 1
        counts[prefix] = expected_num
        if number != expected_num:
            violations.append(
                Violation(
                    seq_rule,
                    f"Expected '{prefix}{expected_num}', got '{item.id}'.",
                    item.line,
                )
            )
    return violations


def check_severity_matches_id(
    items: Sequence[Item],
    rule: str = "severity-matches-id",
    require_present: bool = True,
) -> list[Violation]:
    """Checks that each finding's Severity field matches its C/H/M/L heading prefix."""
    violations: list[Violation] = []
    for item in items:
        expected = ID_TO_SEVERITY.get(item.id[0])
        sev = strip_ticks(item.fields.get("Severity", "")).lower()
        if not sev and not require_present:
            continue
        if expected and sev != expected:
            violations.append(
                Violation(
                    rule,
                    f"Finding '{item.id}' has severity '{sev or 'missing'}', expected '{expected}'.",
                    item.line,
                )
            )
    return violations


def check_evidence_caps(
    items: Sequence[Item],
    is_findings: bool = True,
    require_detail: bool = False,
    profile: ReportProfile | None = None,
) -> list[Violation]:
    """Enforces evidence tier validity, forbids Recalled, and caps C/H severities."""
    violations: list[Violation] = []
    for item in items:
        ev = item.fields.get("Evidence", "").strip()
        tier = extract_tier(ev)
        if not tier:
            violations.append(
                Violation(
                    _rule_code(profile, "evidence-required"),
                    f"'{item.id}' is missing a valid Evidence tier ({', '.join(VALID_TIERS[:-1])}).",
                    item.line,
                )
            )
            continue
        if tier == "Recalled":
            violations.append(
                Violation(
                    _rule_code(profile, "evidence-recalled"),
                    f"'{item.id}' uses forbidden tier 'Recalled'.",
                    item.line,
                )
            )
            continue
        if require_detail and not extract_evidence_detail(ev):
            violations.append(
                Violation(
                    _rule_code(profile, "missing-evidence-detail"),
                    f"'{item.id}' Evidence lacks proof detail.",
                    item.line,
                )
            )
            continue
        sev = strip_ticks(
            item.fields.get("Severity", "")
        ).lower() or ID_TO_SEVERITY.get(item.id[0], "")
        if is_findings and sev in ("critical", "high") and tier == "Speculation":
            violations.append(
                Violation(
                    _rule_code(profile, "severity-cap"),
                    f"'{item.id}' ({sev}) cannot rest on 'Speculation' evidence.",
                    item.line,
                )
            )
    return violations


def _check_item_fields(item: Item, profile: ReportProfile) -> list[Violation]:
    violations: list[Violation] = []
    for req in profile.required_fields:
        if strip_ticks(item.fields.get(req, "")):
            continue
        rule = (
            "why-required"
            if req == "Why" and "why-required" in profile.rule_names
            else _rule_code(profile, "missing-field")
        )
        violations.append(
            Violation(
                rule, f"'{item.id}' is missing a non-empty '{req}' field.", item.line
            )
        )
    for field_name, pattern in profile.validators.items():
        raw_val = item.fields.get(field_name, "").strip()
        if raw_val and not pattern.match(raw_val):
            rule = _rule_code(profile, f"invalid-{field_name.lower()}")
            violations.append(
                Violation(
                    rule,
                    f"'{item.id}' has invalid '{field_name}' value '{raw_val}'.",
                    item.line,
                )
            )
    return violations


def check_required_fields(
    items: Sequence[Item], profile: ReportProfile
) -> list[Violation]:
    """Checks presence of profile.required_fields and runs profile.validators."""
    violations: list[Violation] = []
    for item in items:
        violations.extend(_check_item_fields(item, profile))
    return violations


def check_questions(
    items: Sequence[Item],
    max_count: int = 3,
    cap_rule: str = "question-fields",
    field_rule: str = "question-fields",
) -> list[Violation]:
    """Checks maximum question count and non-empty To settle field."""
    violations: list[Violation] = []
    if len(items) > max_count:
        violations.append(
            Violation(
                cap_rule,
                f"Questions section has {len(items)} entries (maximum is {max_count}).",
                items[max_count].line,
            )
        )
    for q in items:
        if not strip_ticks(q.fields.get("To settle", "")):
            violations.append(
                Violation(
                    field_rule,
                    f"Question '{q.id}' is missing a 'To settle' field.",
                    q.line,
                )
            )
    return violations


def check_clean_lines(
    lines: Sequence[tuple[int, str]],
    max_count: int = 10,
    cap_rule: str = "clean-max-10",
    tier_rule: str = "clean-line-tier",
) -> list[Violation]:
    """Checks the 10-line cap and proof-tier presence in Checked and Found Clean."""
    violations: list[Violation] = []
    if len(lines) > max_count:
        violations.append(
            Violation(
                cap_rule,
                f"Checked and Found Clean has {len(lines)} lines (maximum is {max_count}).",
                lines[max_count][0],
            )
        )
    for lineno, text in lines:
        if not any(re.search(rf"\b{tier}\b", text) for tier in CLEAN_TIERS):
            violations.append(
                Violation(
                    tier_rule,
                    f"Checked and Found Clean line lacks a valid tier ({', '.join(CLEAN_TIERS)}).",
                    lineno,
                )
            )
    return violations


def check_single_fence_per_field(items: Sequence[Item]) -> list[Violation]:
    """Ensures each fenced field contains at most one fenced code block."""
    violations: list[Violation] = []
    for item in items:
        for field_name, meta in item.fence_meta.items():
            if meta.extra_fences:
                violations.append(
                    Violation(
                        "suggestion-single-fence",
                        f"'{item.id}' {field_name} has more than one fenced block.",
                        item.line,
                    )
                )
    return violations


def check_verdict(
    summary_lines: Sequence[str],
    findings: Sequence[Item],
    required: bool = True,
) -> list[Violation]:
    """Validates Summary Verdict label and checks consistency with C/H findings."""
    labels = VERDICT_LINE_RE.findall("\n".join(summary_lines))
    if not labels:
        if not required:
            return []
        return [
            Violation(
                "missing-verdict",
                "Summary lacks a valid 'Verdict:' line.",
                1,
                f"End ## Summary with 'Verdict: <{' | '.join(VALID_VERDICTS)}>'.",
            )
        ]
    verdict = labels[-1].lower()
    has_blocker = any(f.id.startswith(("C", "H")) for f in findings)
    if verdict == "ready" and has_blocker:
        return [
            Violation(
                "verdict-contradicts-findings",
                "Verdict 'Ready' contradicts critical or high findings.",
                1,
                "Change Verdict to 'Minor Revisions Needed' or 'Critical Blockers'.",
            )
        ]
    return []


def _collect_ai_labels(vdict: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for item in vdict.get("banned_ai_words_found", []):
        labels.append(f"banned word '{item['banned_word']}'")
    for item in vdict.get("banned_ai_phrases_found", []):
        labels.append(f"banned phrase '{item['banned_phrase']}'")
    if vdict.get("em_dashes_found"):
        labels.append("em-dash '—'")
    for item in vdict.get("noun_clusters_found", []):
        labels.append(f"noun cluster '{item['cluster']}'")
    return labels


def _prose_quality_violations(
    item_id: str, line: int, vdict: dict[str, Any], where: str
) -> list[Violation]:
    out: list[Violation] = []
    long_sents = [
        s
        for s in vdict.get("sentences_exceeding_25_words", [])
        if int(s.get("word_count", 0)) > MAX_SENTENCE_WORDS
    ]
    if long_sents:
        longest = max(int(s["word_count"]) for s in long_sents)
        out.append(
            Violation(
                "comment-sentence-length",
                f"'{item_id}' {where} has {len(long_sents)} sentence(s) exceeding "
                f"{MAX_SENTENCE_WORDS} words (longest: {longest} words).",
                line,
            )
        )
    complex_sents = vdict.get("complex_sentences_found", [])
    if complex_sents:
        worst = max(int(s["cognitive_complexity"]) for s in complex_sents)
        out.append(
            Violation(
                "comment-sentence-complexity",
                f"'{item_id}' {where} has {len(complex_sents)} sentence(s) with cognitive "
                f"complexity PCC > {MAX_SENTENCE_PCC} (max PCC: {worst}).",
                line,
            )
        )
    ai_labels = _collect_ai_labels(vdict)
    if ai_labels:
        out.append(
            Violation(
                "comment-ai-ism",
                f"'{item_id}' {where} contains prose violation(s): {', '.join(ai_labels)}.",
                line,
            )
        )
    return out


def _lint_text_prose(
    item_id: str,
    line: int,
    prose: str,
    profile: ReportProfile,
    analyze_fn: Callable[..., dict[str, Any]],
    where: str = "comment",
    is_details: bool = False,
) -> list[Violation]:
    clean = strip_fenced_blocks(prose)
    if not clean:
        return []
    metrics = analyze_fn(
        "-", complexity_threshold=MAX_SENTENCE_PCC, engine="stdlib", text=clean
    )
    if "error" in metrics:
        return []
    summary = metrics.get("summary", {})
    words = int(summary.get("total_words", 0))
    sents = int(summary.get("total_sentences", 0))
    out: list[Violation] = []
    if is_details:
        if words > profile.details_words:
            out.append(
                Violation(
                    "comment-details-length",
                    f"'{item_id}' Details is {words} words (max {profile.details_words} words).",
                    line,
                )
            )
    elif words > profile.max_words or sents > profile.max_sentences:
        out.append(
            Violation(
                "comment-prose-length",
                f"'{item_id}' comment prose is {words} words / {sents} sentences "
                f"(max {profile.max_words} words, {profile.max_sentences} sentences).",
                line,
            )
        )
    out.extend(
        _prose_quality_violations(item_id, line, metrics.get("violations", {}), where)
    )
    return out


def _default_prose_builder(item: Item) -> str:
    return "\n".join(
        part
        for part in (
            item.fields.get("Body", "").strip(),
            item.fields.get("Why", "").strip(),
            item.fields.get("Suggestion", "").strip(),
        )
        if part
    )


def check_prose(
    items: Sequence[Item],
    profile: ReportProfile,
    analyze_fn: Callable[..., dict[str, Any]],
    prose_builder: Callable[[Item], str] | None = None,
    check_details: bool = True,
) -> list[Violation]:
    """Runs write-prose checks and word/sentence caps on findings or questions."""
    builder = prose_builder or _default_prose_builder
    violations: list[Violation] = []
    for item in items:
        violations.extend(
            _lint_text_prose(item.id, item.line, builder(item), profile, analyze_fn)
        )
        if check_details:
            details = item.fields.get("Details", "").strip()
            if details:
                violations.extend(
                    _lint_text_prose(
                        item.id,
                        item.line,
                        details,
                        profile,
                        analyze_fn,
                        where="Details",
                        is_details=True,
                    )
                )
    return violations


# Fix hints for the grounding and worker-mode rules below. Each value completes
# the sentence "Fix: ...". `check_grounding` appends the nearest changed lines
# or the place where a quote does appear to the matching hint.
GROUNDING_FIX_HINTS: dict[str, str] = {
    "anchor-file-missing": (
        "Set '- **File**:' to a repository-relative path that exists in the reviewed "
        "checkout, or to the path of a file that the diff deletes."
    ),
    "anchor-line-out-of-bounds": (
        "Set '- **Line**:' to a line number inside the file as it exists in the "
        "reviewed checkout, or to the old line number of a line that the diff deletes."
    ),
    "anchor-line-unchanged": (
        "Anchor the finding to a line that the diff adds, to a line next to a deletion, "
        "or to the old line number of a deleted line, and keep the unchanged location "
        "in Evidence or Details."
    ),
    "evidence-quote-mismatch": (
        "Quote the cited lines exactly (mark omissions with '...'), correct the "
        "`path:line` citation next to the quote, or remove the quotation marks from "
        "text that is not a literal quote of the file."
    ),
    "playbook-feedback-missing": (
        "End the report with '## Playbook Feedback' and open that section with "
        "'Failure modes checked: <numbers from the playbook>'."
    ),
    "playbook-modes-invalid": (
        "List only failure mode numbers that the assigned playbook defines, as positive "
        "integers separated by commas (for example 'Failure modes checked: 1, 2, 4')."
    ),
}

# A quotation must appear within this many lines of a place its field cites.
_QUOTE_WINDOW_LINES = 15
# Shorter quoted text is a term or a name, not a quotation worth checking.
_MIN_QUOTE_CHARS = 12
# Bounds the work done on one Evidence field and on one failure mode range.
_MAX_EVIDENCE_CHARS = 8000
_MAX_MODE_RANGE = 200

_DELETED_FILE_MARKERS = ("deleted file mode", "+++ /dev/null")
_HEADER_PATH_PREFIXES = ("+++ ", "rename to ", "copy to ", "diff --git ", "--- ")
_HEADER_SIDE_PREFIXES = {"+++ ": "b/", "--- ": "a/"}
# The quoted new-side path that ends a `diff --git` line.
_GIT_QUOTED_NEW_PATH_RE = re.compile(r' "b/((?:[^"\\]|\\.)+)"$')
_GIT_OCTAL_RUN_RE = re.compile(r"(?:\\[0-3][0-7]{2})+")
_GIT_SIMPLE_ESCAPE_RE = re.compile(r'\\(["\\tn])')

_FIRST_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
# One line number or range with an optional `L` prefix: `42`, `L10-L20`, `3..9`.
_LINE_SPAN_RE = re.compile(
    r"(?<!\d)L?(\d{1,7})(?!\d)(?:\s*(?:-|\u2013|\u2014|\.\.|to)\s*L?(\d{1,7})(?!\d))?"
)

# A Markdown code span, closed by a backtick run as long as the one that opens it.
_CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.DOTALL)
# The content of a code span that holds nothing but one double-quoted string.
_QUOTED_CODE_SPAN_RE = re.compile(r'\s*"((?:\\.|[^"\\])+)"\s*')
# Quoted text in prose, in straight or curly marks. A straight opening mark must
# start a word and a closing mark must end one, so inch marks and stray marks
# do not pair up across unrelated text.
_PLAIN_QUOTE_RE = re.compile(
    r'(?<![\w"\\])"(?=\S)(?:\\.|[^"\\])+?(?<=\S)"(?![\w"])|\u201c[^\u201d]+\u201d',
    re.DOTALL,
)
# A backticked evidence tier that marks where text about files ends.
_OTHER_TIER_RE = re.compile(r"`(?:Executed|Fetched|Deduction|Speculation)`")
# An omission mark inside a quotation, with or without square brackets.
_ELLIPSIS_RE = re.compile(r"\[\s*(?:\.{3,}|\u2026)\s*\]|\.{3,}|\u2026")

# A path-like token with an optional line list, such as `pkg/a.py:12, 30-34`.
_LINE_SPEC = r"L?\d{1,7}(?!\d)(?:\s?(?:-|\u2013|\.\.)\s?L?\d{1,7}(?!\d))?"
_LINE_LIST = rf"{_LINE_SPEC}(?:\s*(?:,\s*(?:and\s+)?|and\s+){_LINE_SPEC})*"
_PATH_TOKEN_RE = re.compile(
    rf"(?<![\w.@~+/:-])(?P<path>[\w.@~+/-]+)(?::(?P<lines>{_LINE_LIST}))?"
)
# A line reference without a path: `line 42`, `lines 3-9`, `L42`, or `:42`.
_BARE_LINES_RE = re.compile(
    rf"(?<![\w.@~+/:-])(?:lines?\s+|L(?=\d)|:(?=L?\d))(?P<lines>{_LINE_LIST})",
    re.IGNORECASE,
)
_FILE_EXTENSION_RE = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,9}$")
# A Markdown link `[label](target)`. Reviewers link `File` values and citations
# to `file://` addresses that end with a line fragment such as `#L10-L20`. A
# label may end with its own line list, as in `[store.py:12, 30](...)`.
_MARKDOWN_LINK_RE = re.compile(r"\[(?P<label>[^\]\n]*)\]\((?P<target>[^()\s]+)\)")
_LINK_FRAGMENT_RE = re.compile(r"L\d{1,7}(?:-L?\d{1,7})?")
_LABEL_LINES_RE = re.compile(rf":(?P<lines>{_LINE_LIST})\s*$")

# Written escapes that stand for whitespace or for one punctuation mark.
_MATCH_ESCAPED_SPACE_RE = re.compile(r"\\[nrt]")
_MATCH_ESCAPED_PUNCT_RE = re.compile(r"\\([!-/:-@\[-`{-~])")
# Characters that never count when a quotation is compared with file text:
# whitespace, the backticks of code formatting, and the asterisks of Markdown
# emphasis and of comment-block margins.
_MATCH_DROPPED_RE = re.compile(r"[\s`*]+")
# The marker that starts a list item, a block quote, a heading, or a comment line.
_LINE_MARKER_RE = re.compile(
    r"^[ \t]*(?:[-*+>]|#{1,6}|/{2,3}!?|\d{1,3}[.)])(?:[ \t]+|$)", re.MULTILINE
)
_MATCH_QUOTE_MARKS = str.maketrans(
    {"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'"}
)

_MODES_LINE_RE = re.compile(r"failure modes checked\s*:\s*(.*)", re.IGNORECASE)
_MODE_TOKEN_RE = re.compile(
    r"(?<!\d)(\d{1,6})(?!\d)(?:\s*(?:-|\u2013|\.\.|to)\s*(\d{1,6})(?!\d))?"
)
_PARENTHETICAL_RE = re.compile(r"\([^()]*\)")
_LIST_MARKER_RE = re.compile(r"^\s*[-*+]\s+")
_PLAYBOOK_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")
_PLAYBOOK_MODE_RE = re.compile(r"^\s{0,3}(\d{1,4})[.)]\s+\S")

_CLUSTER_TOKEN_RE = re.compile(r"[a-z0-9_]{3,}")
# Common English words that say nothing about which defect a finding describes.
_CLUSTER_STOPWORDS = frozenset(
    {
        "after",
        "all",
        "also",
        "and",
        "any",
        "are",
        "because",
        "been",
        "before",
        "being",
        "but",
        "can",
        "could",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "into",
        "its",
        "more",
        "most",
        "not",
        "only",
        "should",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "was",
        "were",
        "when",
        "where",
        "which",
        "while",
        "will",
        "with",
        "without",
        "would",
        "you",
    }
)


@dataclass(frozen=True)
class StagedDiffIndex:
    """Changed-line lookup tables for one staged diff directory.

    `load_staged_diff_index` builds an index from `stage_diffs.py --grouped`
    output. Every key is a repository-relative path on the new side of the
    diff. Line numbers are 1-based.

    Attributes:
        added_lines_by_file: New-side line numbers of added lines. Every file
            that the diff changes and keeps has an entry, so a file changed
            without line-level hunks (a pure rename, a mode change, a binary
            change, or an empty new file) maps to an empty set.
        deletion_seam_lines_by_file: New-side line numbers directly before and
            after each run of deleted lines in a file that survives. A deletion
            at the top of a file contributes line 1 only, and a deletion at the
            end contributes the line number one past the new end.
        deleted_old_lines_by_file: Old-side line numbers of deleted lines,
            including the lines of deleted files.
        deleted_line_texts_by_file: Text of deleted lines in diff order,
            including the lines of deleted files.
        deleted_files: Paths of files that the diff deletes.
    """

    added_lines_by_file: dict[str, frozenset[int]]
    deletion_seam_lines_by_file: dict[str, frozenset[int]]
    deleted_old_lines_by_file: dict[str, frozenset[int]]
    deleted_line_texts_by_file: dict[str, tuple[str, ...]]
    deleted_files: frozenset[str]


@dataclass
class _LineTables:
    """Per-file line facts gathered from `diff_lines.jsonl`."""

    added: dict[str, set[int]] = field(default_factory=dict)
    deleted_old: dict[str, set[int]] = field(default_factory=dict)
    deleted_texts: dict[str, list[str]] = field(default_factory=dict)
    by_hunk: dict[tuple[str, Any], list[Mapping[str, Any]]] = field(
        default_factory=dict
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Reads the JSON objects of a JSON Lines file, skipping blank lines."""
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError as exc:
                raise ValueError(f"{path}:{lineno}: not a JSON record ({exc})") from exc
            if isinstance(record, dict):
                records.append(record)
    return records


def _collect_line_tables(records: Iterable[Mapping[str, Any]]) -> _LineTables:
    """Sorts line records into added, deleted, and per-hunk tables."""
    tables = _LineTables()
    for rec in records:
        path, op = rec.get("file_path"), rec.get("op")
        if not isinstance(path, str):
            continue
        tables.by_hunk.setdefault((path, rec.get("hunk_id")), []).append(rec)
        if op == "+" and isinstance(rec.get("new_line"), int):
            tables.added.setdefault(path, set()).add(rec["new_line"])
        elif op == "-":
            tables.deleted_texts.setdefault(path, []).append(
                str(rec.get("content", ""))
            )
            if isinstance(rec.get("old_line"), int):
                tables.deleted_old.setdefault(path, set()).add(rec["old_line"])
    return tables


def _hunk_start_cursor(
    meta: Mapping[str, Any] | None, records: Sequence[Mapping[str, Any]]
) -> int | None:
    """Returns the new-side line number that the next surviving line of a hunk takes.

    A hunk header with a new-side count of zero names the line before the
    deletion, so the next surviving line is one past it. Without a header the
    first surviving record gives the position.
    """
    if meta is not None and isinstance(meta.get("new_start"), int):
        return meta["new_start"] + (1 if meta.get("new_count") == 0 else 0)
    for rec in records:
        if isinstance(rec.get("new_line"), int):
            return rec["new_line"]
    return None


def _hunk_seam_lines(
    records: Sequence[Mapping[str, Any]], cursor: int | None
) -> set[int]:
    """Returns the new-side lines on both sides of every deleted run in a hunk."""
    seams: set[int] = set()
    for rec in records:
        new_line = rec.get("new_line")
        if rec.get("op") == "-":
            if cursor is not None:
                seams.update((cursor - 1, cursor))
        elif isinstance(new_line, int):
            cursor = new_line + 1
    return {line for line in seams if line >= 1}


def _collect_seams(
    tables: _LineTables,
    hunk_meta: Mapping[Any, Mapping[str, Any]],
    deleted_files: set[str],
) -> dict[str, set[int]]:
    """Maps each surviving file to the lines next to its deletions."""
    seams: dict[str, set[int]] = {}
    for (path, hunk_id), records in tables.by_hunk.items():
        if path in deleted_files:
            continue
        cursor = _hunk_start_cursor(hunk_meta.get(hunk_id), records)
        lines = _hunk_seam_lines(records, cursor)
        if lines:
            seams.setdefault(path, set()).update(lines)
    return seams


def _hunk_deleted_files(hunk_records: Iterable[Mapping[str, Any]]) -> set[str]:
    """Infers deleted files from hunks that leave no new-side lines."""
    emptied: dict[str, bool] = {}
    for hunk in hunk_records:
        path = hunk.get("file_path")
        if isinstance(path, str):
            gone = hunk.get("new_start") == 0 and hunk.get("new_count") == 0
            emptied[path] = emptied.get(path, True) and gone
    return {path for path, gone in emptied.items() if gone}


def _unquote_git_path(raw: str) -> str:
    """Decodes a path that git wrapped in double quotes with C-style escapes."""
    if len(raw) < 2 or not (raw.startswith('"') and raw.endswith('"')):
        return raw

    def _octal_run(match: re.Match[str]) -> str:
        octets = bytes(int(part, 8) for part in match.group(0).split("\\")[1:])
        return octets.decode("utf-8", errors="replace")

    def _simple(match: re.Match[str]) -> str:
        return {"t": "\t", "n": "\n"}.get(match.group(1), match.group(1))

    return _GIT_SIMPLE_ESCAPE_RE.sub(
        _simple, _GIT_OCTAL_RUN_RE.sub(_octal_run, raw[1:-1])
    )


def _git_header_new_path(rest: str) -> str | None:
    """Extracts the new-side path from the text after `diff --git `.

    Unquoted paths may contain spaces, which makes the line ambiguous. A file
    that keeps its name gives a line of two equal halves, so the path is read
    from the half. A renamed or copied file is named by its `rename to` or
    `copy to` line instead, and yields None here.
    """
    quoted = _GIT_QUOTED_NEW_PATH_RE.search(rest)
    if quoted:
        return _unquote_git_path(f'"{quoted.group(1)}"')
    half, odd = divmod(len(rest) - 5, 2)
    if half > 0 and not odd and rest == f"a/{rest[-half:]} b/{rest[-half:]}":
        return rest[-half:]
    return None


def _header_line_path(prefix: str, rest: str) -> str | None:
    """Extracts the repository path from the text after a file header prefix."""
    if prefix == "diff --git ":
        return _git_header_new_path(rest)
    value = _unquote_git_path(rest.split("\t", 1)[0])
    side = _HEADER_SIDE_PREFIXES.get(prefix)
    if side is None:
        return value or None
    return (
        value[len(side) :]
        if value.startswith(side) and len(value) > len(side)
        else None
    )


def _header_block_path(block: Sequence[str]) -> str | None:
    """Returns the path that a file header names, preferring the new side."""
    for prefix in _HEADER_PATH_PREFIXES:
        for line in block:
            if line.startswith(prefix):
                path = _header_line_path(prefix, line[len(prefix) :])
                if path:
                    return path
    return None


def _starts_file_header(lines: Sequence[str], idx: int, git_style: bool) -> bool:
    """Reports whether `lines[idx]` opens the header of a file diff."""
    if git_style:
        return lines[idx].startswith("diff --git ")
    follows = idx + 1 < len(lines) and lines[idx + 1].startswith("+++ ")
    return lines[idx].startswith("--- ") and follows


def _file_header_blocks(text: str) -> list[list[str]]:
    """Splits chunk text into the header lines of each file diff in it.

    A header ends at its `+++` line, so a hunk body line that looks like a
    header line is never read as one. That matters for a chunk that continues
    a long hunk, where body lines follow the header with no `@@` line between.
    A header without a `+++` line belongs to a file with no hunks and runs to
    the next file.
    """
    lines = [line.rstrip("\r") for line in text.split("\n")]
    git_style = any(line.startswith("diff --git ") for line in lines)
    blocks: list[list[str]] = []
    collecting = False
    for idx, line in enumerate(lines):
        if _starts_file_header(lines, idx, git_style):
            blocks.append([line])
            collecting = True
        elif collecting:
            blocks[-1].append(line)
            collecting = not line.startswith("+++ ")
    return blocks


def _manifest_chunks(diff_dir: Path) -> list[Mapping[str, Any]]:
    """Returns the chunk entries of `manifest.json`, or none when it is absent."""
    manifest_path = diff_dir / "manifest.json"
    if not manifest_path.is_file():
        return []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"{manifest_path}: not a JSON document ({exc})") from exc
    chunks = manifest.get("chunks") if isinstance(manifest, dict) else None
    return [chunk for chunk in chunks or [] if isinstance(chunk, dict)]


def _chunk_text(diff_dir: Path, chunk: Mapping[str, Any]) -> str:
    """Reads a chunk file from `diff_dir`, or returns an empty string without one."""
    name = Path(str(chunk.get("chunk_name") or "")).name
    chunk_path = diff_dir / name
    if not name or not chunk_path.is_file():
        return ""
    return chunk_path.read_text(encoding="utf-8", errors="replace")


def _staged_header_files(chunk_texts: Iterable[str]) -> tuple[set[str], set[str]]:
    """Returns the files that chunk headers name and the subset marked deleted."""
    files: set[str] = set()
    deleted: set[str] = set()
    for text in chunk_texts:
        for block in _file_header_blocks(text):
            path = _header_block_path(block)
            if path is None:
                continue
            files.add(path)
            if any(line.startswith(_DELETED_FILE_MARKERS) for line in block):
                deleted.add(path)
    return files, deleted


def load_staged_diff_index(diff_dir: Path) -> StagedDiffIndex:
    """Builds changed-line lookup tables from a staged diff directory.

    Reads the files that `stage_diffs.py --grouped` writes: `diff_lines.jsonl`
    for added and deleted lines, `diff_hunks.jsonl` for hunk positions, and the
    chunk files listed in `manifest.json` for file headers. The headers reveal
    files that have no line records, such as an empty or binary file that was
    deleted. When `diff_hunks.jsonl`, `manifest.json`, or a chunk file is
    missing, the index is built from what is there, and a file whose hunks all
    leave no new-side lines counts as deleted.

    Args:
        diff_dir: The staged diff directory.

    Returns:
        The index for the staged diff. An empty diff gives empty tables.

    Raises:
        FileNotFoundError: `diff_lines.jsonl` is not in `diff_dir`, which means
            the directory is not a staged diff.
        ValueError: A JSON Lines record or `manifest.json` is not valid JSON.
    """
    diff_dir = Path(diff_dir)
    tables = _collect_line_tables(_read_jsonl(diff_dir / "diff_lines.jsonl"))
    hunks_path = diff_dir / "diff_hunks.jsonl"
    hunk_records = _read_jsonl(hunks_path) if hunks_path.is_file() else []
    chunks = _manifest_chunks(diff_dir)
    header_files, header_deleted = _staged_header_files(
        _chunk_text(diff_dir, chunk) for chunk in chunks
    )

    deleted_files = header_deleted | (_hunk_deleted_files(hunk_records) - header_files)
    hunk_meta = {hunk.get("hunk_id"): hunk for hunk in hunk_records}
    seams = _collect_seams(tables, hunk_meta, deleted_files)

    listed = {str(name) for chunk in chunks for name in chunk.get("files") or []}
    hunk_files = {
        h["file_path"] for h in hunk_records if isinstance(h.get("file_path"), str)
    }
    changed = (
        header_files | listed | hunk_files | set(tables.added) | set(tables.deleted_old)
    )
    added = {path: tables.added.get(path, set()) for path in changed - deleted_files}
    return StagedDiffIndex(
        added_lines_by_file={path: frozenset(lines) for path, lines in added.items()},
        deletion_seam_lines_by_file={
            path: frozenset(lines) for path, lines in seams.items()
        },
        deleted_old_lines_by_file={
            path: frozenset(lines) for path, lines in tables.deleted_old.items()
        },
        deleted_line_texts_by_file={
            path: tuple(texts) for path, texts in tables.deleted_texts.items()
        },
        deleted_files=frozenset(deleted_files),
    )


def _normalize_anchor_path(value: str) -> str:
    """Strips backticks, backslashes, and leading `./` from a `File` or cited path."""
    path = strip_ticks(value).replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _parse_line_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Parses line numbers and ranges such as `42`, `L10-L20`, or `3, 8..9`."""
    spans: list[tuple[int, int]] = []
    for match in _LINE_SPAN_RE.finditer(text):
        first = int(match.group(1))
        last = int(match.group(2)) if match.group(2) else first
        spans.append((min(first, last), max(first, last)))
    return tuple(spans)


def _link_target(target: str) -> tuple[str | None, str]:
    """Splits a Markdown link target into a file path and a fragment.

    The path is None when the target is a web address or names no file.
    """
    try:
        parts = urlsplit(target)
    except ValueError:
        return None, ""
    is_file = parts.scheme == "file" or not (parts.scheme or parts.netloc)
    if not is_file or not parts.path:
        return None, ""
    return unquote(parts.path), parts.fragment


def _anchor_file(item: Any) -> str:
    """Returns the path in an item's `File` field without Markdown markup.

    The field holds a backticked path, a bare path, or a Markdown link. A link
    gives the path of its target, or its label when the target is a web address.
    """
    raw = str(item.fields.get("File", "")).strip()
    link = _MARKDOWN_LINK_RE.search(raw)
    ticked = _FIRST_CODE_SPAN_RE.search(raw)
    if link and (ticked is None or link.start() < ticked.start()):
        path, _ = _link_target(link.group("target"))
        if path:
            return _normalize_anchor_path(path)
        raw = link.group("label")
        ticked = _FIRST_CODE_SPAN_RE.search(raw)
    return _normalize_anchor_path(ticked.group(1) if ticked else raw)


def _anchor_span(item: Any) -> tuple[int, int] | None:
    """Returns the inclusive line range in an item's `Line` field, if it has one."""
    spans = _parse_line_spans(str(item.fields.get("Line", "")))
    return spans[0] if spans else None


def _span_text(span: tuple[int, int]) -> str:
    return str(span[0]) if span[0] == span[1] else f"{span[0]}-{span[1]}"


def _span_touches(span: tuple[int, int], lines: Iterable[int]) -> bool:
    """Reports whether any of `lines` falls inside the inclusive `span`."""
    return any(span[0] <= line <= span[1] for line in lines)


def _line_runs(lines: Iterable[int]) -> list[tuple[int, int]]:
    """Groups line numbers into sorted inclusive runs."""
    runs: list[tuple[int, int]] = []
    for line in sorted(lines):
        if runs and line == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], line)
        else:
            runs.append((line, line))
    return runs


def _nearest_runs(
    span: tuple[int, int], lines: Iterable[int], limit: int = 2
) -> list[tuple[int, int]]:
    """Returns the `limit` runs of `lines` closest to `span`, in file order."""

    def _gap(run: tuple[int, int]) -> int:
        return max(run[0] - span[1], span[0] - run[1], 0)

    return sorted(sorted(_line_runs(lines), key=_gap)[:limit])


def _normalize_for_match(text: str) -> str:
    """Reduces text to the characters a quote and its source must share.

    Drops whitespace, backticks, and asterisks, turns written escapes such as
    `\\n` and `\\"` into what they stand for, and straightens curly quotation
    marks. The same reduction is applied to both sides, so a quote still has
    to appear character for character in the file.
    """
    text = _MATCH_ESCAPED_SPACE_RE.sub(" ", text)
    text = _MATCH_ESCAPED_PUNCT_RE.sub(r"\1", text)
    return _MATCH_DROPPED_RE.sub("", text.translate(_MATCH_QUOTE_MARKS))


def _split_lines(text: str) -> list[str]:
    """Splits file text on newlines only, the way git counts lines."""
    lines = text.split("\n")
    if lines and not lines[-1]:
        lines.pop()
    return lines


def _workspace_relative(root: Path, raw_path: str) -> str | None:
    """Returns `raw_path` as a POSIX path under `root`, or None when it leaves `root`."""
    joined = Path(os.path.normpath(root / raw_path))
    if joined == root or not joined.is_relative_to(root):
        return None
    return joined.relative_to(root).as_posix()


def _is_workspace_file(path: Path, real_root: Path) -> bool:
    """Reports whether `path` is a readable regular file that resolves inside the workspace."""
    try:
        return (
            path.is_file()
            and os.access(path, os.R_OK)
            and path.resolve().is_relative_to(real_root)
        )
    except OSError:
        return False


def _diff_files(index: StagedDiffIndex) -> frozenset[str]:
    """Returns every path that a diff index knows about."""
    return frozenset(
        {
            *index.added_lines_by_file,
            *index.deletion_seam_lines_by_file,
            *index.deleted_old_lines_by_file,
            *index.deleted_files,
        }
    )


class _Grounding:
    """Resolves paths and caches file text for one `check_grounding` run.

    Paths from a report are untrusted, so only regular files that resolve to a
    location inside the workspace are ever read. A file is read when its lines
    are first needed, not when a report merely names it.
    """

    def __init__(
        self, workspace: Path | None, diff_index: StagedDiffIndex | None
    ) -> None:
        self.root = Path(os.path.abspath(workspace)) if workspace is not None else None
        self.real_root = self.root.resolve() if self.root is not None else None
        self.has_diff = diff_index is not None
        self.index = (
            diff_index
            if diff_index is not None
            else StagedDiffIndex({}, {}, {}, {}, frozenset())
        )
        self.diff_files = _diff_files(self.index)
        self._exists: dict[str, bool] = {}
        self._lines: dict[str, list[str]] = {}

    def relative(self, raw_path: str) -> str | None:
        """Returns a report path relative to the workspace, or None when it is outside.

        An absolute path may reach the workspace through a symbolic link, or
        the workspace may have been given through one. Such a path is compared
        again after both sides are resolved.
        """
        cleaned = _normalize_anchor_path(raw_path)
        if self.root is None or self.real_root is None:
            return cleaned or None
        rel = _workspace_relative(self.root, cleaned)
        if rel is None and os.path.isabs(cleaned):
            rel = _workspace_relative(self.real_root, os.path.realpath(cleaned))
        return rel

    def exists(self, rel: str | None) -> bool:
        """Reports whether `rel` is a readable file inside the workspace."""
        if rel is None or self.root is None or self.real_root is None:
            return False
        if rel not in self._exists:
            self._exists[rel] = _is_workspace_file(self.root / rel, self.real_root)
        return self._exists[rel]

    def lines(self, rel: str | None) -> list[str] | None:
        """Returns the lines of a workspace file, or None when it is not one."""
        if rel is None or self.root is None or not self.exists(rel):
            return None
        if rel not in self._lines:
            text = (self.root / rel).read_text(encoding="utf-8", errors="replace")
            self._lines[rel] = _split_lines(text)
        return self._lines[rel]

    def deleted_lines(self, rel: str) -> frozenset[int]:
        """Returns the old-side line numbers that the diff deletes from `rel`."""
        return self.index.deleted_old_lines_by_file.get(rel, frozenset())

    def changed_lines(self, rel: str) -> frozenset[int]:
        """Returns the new-side lines of `rel` that are added or next to a deletion."""
        added = self.index.added_lines_by_file.get(rel, frozenset())
        return added | self.index.deletion_seam_lines_by_file.get(rel, frozenset())

    def deleted_text(self, rel: str) -> str:
        """Returns the lines that the diff deletes from `rel`, one per line."""
        return "\n".join(self.index.deleted_line_texts_by_file.get(rel, ()))

    def near_anchor(self, cited: str, anchor_rel: str | None) -> str | None:
        """Finds `cited` as a path relative to a directory that holds the anchor file."""
        if anchor_rel is None or self.root is None:
            return None
        for parent in Path(anchor_rel).parents:
            candidate = _workspace_relative(self.root, (parent / cited).as_posix())
            if self.exists(candidate):
                return candidate
        return None

    def resolve_citation(self, raw_path: str, anchor_rel: str | None) -> str | None:
        """Maps a cited path to a file on disk or in the diff, or None.

        Reviewers often cite a file by its base name or by a path relative to
        a package directory. A path that is not found as written is matched as
        a trailing part of the item's `File`, then of exactly one file in the
        diff, and then as a path relative to each directory that holds the
        item's `File`, nearest first.
        """
        rel = self.relative(raw_path)
        if rel is not None and (self.exists(rel) or rel in self.diff_files):
            return rel
        cited = _normalize_anchor_path(raw_path)
        if anchor_rel is not None and ("/" + anchor_rel).endswith("/" + cited):
            return anchor_rel
        matches = [
            name for name in self.diff_files if ("/" + name).endswith("/" + cited)
        ]
        if len(matches) == 1:
            return matches[0]
        return self.near_anchor(cited, anchor_rel)


def _grounding_violation(
    rule: str, item: Any, message: str, detail: str = ""
) -> Violation:
    """Builds a grounding violation whose hint ends with an optional `detail`."""
    hint = GROUNDING_FIX_HINTS[rule]
    return Violation(
        rule=rule,
        message=message,
        line=getattr(item, "line", 1),
        fix_hint=f"{hint} {detail}" if detail else hint,
    )


def _check_line_bounds(
    item: Any, rel: str, span: tuple[int, int], ground: _Grounding
) -> Violation | None:
    """Checks that an anchor range lies inside the file on disk."""
    lines = ground.lines(rel)
    if lines is None:
        return None
    last = max(1, len(lines))
    if 1 <= span[0] and span[1] <= last:
        return None
    if _span_touches(span, ground.deleted_lines(rel)):
        return None
    return _grounding_violation(
        "anchor-line-out-of-bounds",
        item,
        f"'{item.id}' Line {_span_text(span)} is outside `{rel}`, which has lines 1-{last}.",
    )


def _check_line_changed(
    item: Any, rel: str, span: tuple[int, int], ground: _Grounding
) -> Violation | None:
    """Checks that an anchor range touches a line the diff changed."""
    if not ground.has_diff:
        return None
    if rel not in ground.diff_files:
        return _grounding_violation(
            "anchor-line-unchanged",
            item,
            f"'{item.id}' is anchored to `{rel}`, which the diff does not change.",
            "Use the changed line that causes or exposes the problem.",
        )
    changed = ground.changed_lines(rel)
    deleted = ground.deleted_lines(rel)
    if not changed and not deleted:
        return None
    if _span_touches(span, changed) or _span_touches(span, deleted):
        return None
    runs = ", ".join(_span_text(run) for run in _nearest_runs(span, changed or deleted))
    return _grounding_violation(
        "anchor-line-unchanged",
        item,
        f"'{item.id}' Line {_span_text(span)} of `{rel}` is not a line the diff changed.",
        f"Nearest changed lines in `{rel}`: {runs}.",
    )


def _check_item_anchor(
    item: Any, rel: str | None, span: tuple[int, int] | None, ground: _Grounding
) -> Violation | None:
    """Checks one item's `File` and `Line` anchor against the workspace and diff."""
    deleted_file = rel is not None and rel in ground.index.deleted_files
    if ground.root is not None and not ground.exists(rel) and not deleted_file:
        return _grounding_violation(
            "anchor-file-missing",
            item,
            f"'{item.id}' File `{_anchor_file(item)}` is not a file in the reviewed "
            "checkout and is not deleted by the diff.",
        )
    if rel is None or span is None or deleted_file:
        return None
    return _check_line_bounds(item, rel, span, ground) or _check_line_changed(
        item, rel, span, ground
    )


@dataclass(frozen=True)
class _Quote:
    """A quoted snippet and its character range in an Evidence field."""

    start: int
    end: int
    text: str


@dataclass(frozen=True)
class _Citation:
    """A path, a line reference, or both, found in an Evidence field.

    `path` is None for a bare reference such as `line 42`, and `spans` is empty
    for a path that is named without a line.
    """

    start: int
    end: int
    path: str | None
    spans: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class _Target:
    """A file and line ranges where a quote may legitimately come from."""

    start: int
    end: int
    rel: str
    spans: tuple[tuple[int, int], ...]


def _scan_evidence(text: str) -> tuple[list[_Quote], str]:
    """Finds quoted snippets and returns them with a view for citation matching.

    A snippet is text in double quotation marks, or a code span that holds
    nothing but such text. Other code spans are never read as quotes, because
    reviewers use them for symbol names and paraphrased code. The view is
    `text` with every quote blanked and code-span backticks turned into spaces,
    so a citation pattern cannot match inside a quote.
    """
    quotes: list[_Quote] = []
    masked = list(text)
    view = list(text)
    for span in _CODE_SPAN_RE.finditer(text):
        start, end = span.span()
        quoted = _QUOTED_CODE_SPAN_RE.fullmatch(span.group(2))
        if quoted:
            quotes.append(_Quote(start, end, quoted.group(1)))
            view[start:end] = " " * (end - start)
        else:
            ticks = len(span.group(1))
            view[start : start + ticks] = " " * ticks
            view[end - ticks : end] = " " * ticks
        masked[start:end] = "\x00" * (end - start)
    for match in _PLAIN_QUOTE_RE.finditer("".join(masked)):
        start, end = match.span()
        quotes.append(_Quote(start, end, text[start + 1 : end - 1]))
        view[start:end] = " " * (end - start)
    return sorted(quotes, key=lambda quote: quote.start), "".join(view)


def _looks_like_path(token: str) -> bool:
    """Reports whether a token has a directory separator or a file extension."""
    return "/" in token or bool(_FILE_EXTENSION_RE.search(token))


def _link_citations(view: str) -> tuple[list[_Citation], str]:
    """Finds Markdown links to files and returns them with those links blanked.

    The link target names the file. Its `#L10-L20` fragment and a `name:line`
    label give the lines. Blanking keeps the label and the fragment from being
    read a second time as separate citations.
    """
    found: list[_Citation] = []
    chars = list(view)
    for match in _MARKDOWN_LINK_RE.finditer(view):
        path, fragment = _link_target(match.group("target"))
        if path is None:
            continue
        label_lines = _LABEL_LINES_RE.search(match.group("label"))
        lines = (
            fragment if _LINK_FRAGMENT_RE.fullmatch(fragment) else "",
            label_lines.group("lines") if label_lines else "",
        )
        start, end = match.span()
        found.append(_Citation(start, end, path, _parse_line_spans(" ".join(lines))))
        chars[start:end] = " " * (end - start)
    return found, "".join(chars)


def _find_citations(view: str) -> list[_Citation]:
    """Finds linked files, `path:line` citations, bare paths, and bare line references."""
    found, view = _link_citations(view)
    for match in _PATH_TOKEN_RE.finditer(view):
        path = match.group("path").rstrip(".,")
        spans = _parse_line_spans(match.group("lines") or "")
        has_letter = any(char.isalpha() for char in path)
        if has_letter and (spans or _looks_like_path(path)):
            found.append(_Citation(match.start(), match.end(), path, spans))
    for match in _BARE_LINES_RE.finditer(view):
        spans = _parse_line_spans(match.group("lines"))
        found.append(_Citation(match.start(), match.end(), None, spans))
    return sorted(found, key=lambda cite: cite.start)


def _named_targets(
    citations: Sequence[_Citation], anchor_rel: str | None, ground: _Grounding
) -> tuple[list[_Target], bool]:
    """Resolves the citations that name a path and reports an unknown cited file.

    A path named without a line gives a target with no spans. It only tells
    which files the field is about.
    """
    targets: list[_Target] = []
    unknown_file = False
    for cite in citations:
        if cite.path is None:
            continue
        resolved = ground.resolve_citation(cite.path, anchor_rel)
        if resolved is not None:
            targets.append(_Target(cite.start, cite.end, resolved, cite.spans))
        elif cite.spans:
            unknown_file = True
    return targets, unknown_file


def _bind_citations(
    citations: Sequence[_Citation], anchor_rel: str | None, ground: _Grounding
) -> tuple[list[_Target], bool]:
    """Resolves citations to files and reports whether any could not be checked.

    A bare line reference does not say which file it means, so it becomes a
    target in every file the field names and in the item's `File`. A citation
    whose file cannot be found makes the result unverifiable, because a quote
    may come from that file.
    """
    named, unknown_file = _named_targets(citations, anchor_rel, ground)
    paths = dict.fromkeys([anchor_rel] if anchor_rel else [])
    paths.update(dict.fromkeys(target.rel for target in named))
    bare = [cite for cite in citations if cite.path is None]
    targets = [target for target in named if target.spans]
    targets.extend(
        _Target(cite.start, cite.end, rel, cite.spans) for cite in bare for rel in paths
    )
    return targets, unknown_file or (bool(bare) and not paths)


def _match_forms(text: str) -> tuple[str, str]:
    """Returns the two reductions under which a quotation may match file text.

    The first keeps the text as written. The second is the text as a reader of
    Markdown or of a comment block sees it: without the marker that starts a
    line, and with each Markdown link replaced by its label.
    """
    unmarked = _LINE_MARKER_RE.sub("", text)
    rendered = _MARKDOWN_LINK_RE.sub(lambda link: link.group("label"), unmarked)
    return _normalize_for_match(text), _normalize_for_match(rendered)


def _target_haystacks(
    targets: Sequence[_Target], ground: _Grounding
) -> list[tuple[str, str]]:
    """Returns the match forms of the text near each target and of its deleted lines."""
    haystacks: list[tuple[str, str]] = []
    for rel in dict.fromkeys(target.rel for target in targets):
        deleted = ground.deleted_text(rel)
        if deleted:
            haystacks.append(_match_forms(deleted))
    for target in targets:
        lines = ground.lines(target.rel) or []
        for first, last in target.spans:
            window = lines[
                max(0, first - 1 - _QUOTE_WINDOW_LINES) : last + _QUOTE_WINDOW_LINES
            ]
            haystacks.append(_match_forms("\n".join(window)))
    return haystacks


def _trim_segment(segment: str) -> str:
    """Drops punctuation that a reviewer may have added around a quotation part.

    A reviewer who trims a quotation often closes a bracket or ends the
    sentence. Dropping that punctuation only shortens the text to find.
    """
    return segment.strip(" ,.;:)]}").rstrip("!?")


def _segment_needle(segment: str) -> str:
    """Returns the reduced text of a quotation part that must appear in a file."""
    return _normalize_for_match(_trim_segment(segment))


def _quote_segments(text: str) -> list[str]:
    """Splits a quotation at its ellipses into the parts long enough to check.

    A quoted single token is a name or a literal value, not a quotation, so it
    yields no parts.
    """
    if not re.search(r"\S\s+\S", text):
        return []
    segments: list[str] = []
    for part in _ELLIPSIS_RE.split(text):
        segment = re.sub(r"\s+", " ", part).strip()
        if len(_trim_segment(segment)) >= _MIN_QUOTE_CHARS:
            segments.append(segment)
    return segments


def _segment_found(segment: str, haystacks: Sequence[tuple[str, str]]) -> bool:
    """Reports whether a quotation part appears in file text in either match form."""
    written, rendered = _match_forms(_trim_segment(segment))
    if any(written in as_written for as_written, _ in haystacks):
        return True
    # A rendered form that lost most of its text would match almost anywhere.
    if len(rendered) < _MIN_QUOTE_CHARS:
        return False
    return any(rendered in as_rendered for _, as_rendered in haystacks)


def _first_missing_segment(
    quote: _Quote, haystacks: Sequence[tuple[str, str]]
) -> str | None:
    """Returns the first checked part of a quote that no haystack contains."""
    for segment in _quote_segments(quote.text):
        if not _segment_found(segment, haystacks):
            return segment
    return None


def _locate_text(segment: str, targets: Sequence[_Target], ground: _Grounding) -> str:
    """Returns `path:line` for the place a segment appears in a target file, if any."""
    needle = _segment_needle(segment)
    for rel in dict.fromkeys(target.rel for target in targets):
        reduced = [_normalize_for_match(line) for line in ground.lines(rel) or []]
        position = "".join(reduced).find(needle)
        if position < 0:
            continue
        for number, part in enumerate(reduced, start=1):
            position -= len(part)
            if position < 0:
                return f"{rel}:{number}"
    return ""


def _nearest_target(quote: _Quote, targets: Sequence[_Target]) -> _Target:
    """Returns the target cited closest to a quote, or the anchor target."""

    def _gap(target: _Target) -> tuple[bool, int]:
        distance = max(target.start - quote.end, quote.start - target.end, 0)
        return target.start < 0, distance

    return min(targets, key=_gap)


def _quote_violation(
    item: Any,
    quote: _Quote,
    segment: str,
    targets: Sequence[_Target],
    ground: _Grounding,
) -> Violation:
    """Builds the violation for a quote whose text is not near any citation."""
    target = _nearest_target(quote, targets)
    where = f"{target.rel}:{_span_text(target.spans[0])}"
    shown = segment if len(segment) <= 60 else segment[:57] + "..."
    found = _locate_text(segment, targets, ground)
    return _grounding_violation(
        "evidence-quote-mismatch",
        item,
        f"'{item.id}' Evidence quotes \"{shown}\", but that text is not within "
        f"{_QUOTE_WINDOW_LINES} lines of `{where}`.",
        f"The text appears at `{found}`." if found else "",
    )


def _read_evidence(item: Any) -> str:
    """Returns the part of a `Read` Evidence field that describes file text."""
    evidence = str(item.fields.get("Evidence", ""))[:_MAX_EVIDENCE_CHARS]
    if extract_tier(evidence) != "Read":
        return ""
    other_tier = _OTHER_TIER_RE.search(evidence)
    return evidence[: other_tier.start()] if other_tier else evidence


def _check_item_quotes(
    item: Any, rel: str | None, span: tuple[int, int] | None, ground: _Grounding
) -> list[Violation]:
    """Checks that each quote in a `Read` Evidence field appears where it is cited."""
    if ground.root is None:
        return []
    quotes, view = _scan_evidence(_read_evidence(item))
    quotes = [quote for quote in quotes if _quote_segments(quote.text)]
    if not quotes:
        return []
    known_rel = ground.resolve_citation(rel, None) if rel is not None else None
    targets, unverifiable = _bind_citations(_find_citations(view), known_rel, ground)
    if known_rel is not None and span is not None:
        targets.append(_Target(-1, -1, known_rel, (span,)))
    if unverifiable or not targets:
        return []
    haystacks = _target_haystacks(targets, ground)
    violations: list[Violation] = []
    for quote in quotes:
        missing = _first_missing_segment(quote, haystacks)
        if missing is not None:
            violations.append(_quote_violation(item, quote, missing, targets, ground))
    return violations


def check_grounding(
    items: Sequence[Any],
    *,
    workspace: Path | None = None,
    diff_index: StagedDiffIndex | None = None,
) -> list[Violation]:
    """Checks that findings and questions point at real, changed, quoted code.

    Each item needs `id`, `line`, and `fields` attributes, as `Item` has. Items
    without a `File` field are skipped. `File` may be a backticked path, a bare
    path, or a Markdown link to a file. The rules, by what they need:

    - `anchor-file-missing` (needs `workspace`): `File` is not a file in the
      checkout and the diff does not delete it.
    - `anchor-line-out-of-bounds` (needs `workspace`): `Line` is outside the
      file, unless it names the old line number of a deleted line.
    - `anchor-line-unchanged` (needs `diff_index`): `Line` touches no added
      line, no line next to a deletion, and no deleted old line. The hint
      names the nearest changed lines. A file that changed without line-level
      hunks accepts any line.
    - `evidence-quote-mismatch` (needs `workspace`): a quotation in a `Read`
      Evidence field is not found near any place the field cites.

    A quotation is text in double quotation marks with at least 12 characters
    and more than one word. A quoted single token is read as a name or a
    value. The places a field cites are the 15 lines around each `path:line`
    citation, each Markdown link to a file with a `#L42` fragment, each bare
    line reference such as `line 42`, and `File:Line`, plus the lines that the
    diff deletes from those files.

    Text is compared without whitespace, backticks, or asterisks. A quotation
    also matches the text as a reader sees it: without the list or comment
    marker that starts a line, and with each Markdown link shown as its label.
    `...` splits a quotation into parts that are checked separately. Text
    after a second tier label such as `Executed` is not checked. The rule
    stays silent when a citation names a file that cannot be found, because
    the quotation may come from there.

    Nothing is rewritten. Every violation carries a fix hint that starts with
    the `GROUNDING_FIX_HINTS` text for its rule.

    Args:
        items: Parsed findings and questions.
        workspace: Root of the reviewed checkout. Only files inside it are read.
        diff_index: Index of the staged diff under review.

    Returns:
        The violations in item order. Empty when both `workspace` and
        `diff_index` are None.

    Raises:
        OSError: A readable file in the workspace fails while it is read.
    """
    ground = _Grounding(workspace, diff_index)
    violations: list[Violation] = []
    for item in items:
        raw_file = _anchor_file(item)
        if not raw_file:
            continue
        rel = ground.relative(raw_file)
        span = _anchor_span(item)
        anchor = _check_item_anchor(item, rel, span, ground)
        if anchor is not None:
            violations.append(anchor)
        violations.extend(_check_item_quotes(item, rel, span, ground))
    return violations


def _first_content_line(
    lines: Sequence[tuple[int, str]] | None,
) -> tuple[int, str] | None:
    """Returns the first line with text, or None when there is none."""
    for lineno, text in lines or ():
        if text.strip():
            return lineno, text
    return None


def _parse_mode_numbers(spec: str) -> list[int]:
    """Parses a failure mode list such as `1, 2, 4-6` into sorted unique numbers."""
    numbers: set[int] = set()
    for match in _MODE_TOKEN_RE.finditer(_PARENTHETICAL_RE.sub(" ", spec)):
        first = int(match.group(1))
        last = int(match.group(2)) if match.group(2) else first
        if first <= last <= first + _MAX_MODE_RANGE:
            numbers.update(range(first, last + 1))
        else:
            numbers.update((first, last))
    return sorted(numbers)


def _playbook_failure_modes(playbook_path: Path) -> set[int]:
    """Returns the failure mode numbers that a focus playbook defines.

    The numbers are the top-level numbered items under the `Failure modes`
    heading. A playbook without that heading contributes every top-level
    numbered item outside code fences.
    """
    in_section: set[int] = set()
    anywhere: set[int] = set()
    inside = False
    fence = ""
    for line in playbook_path.read_text(encoding="utf-8").splitlines():
        was_fenced = bool(fence)
        fence, active = update_fence(fence, line)
        if was_fenced or active:
            continue
        heading = _PLAYBOOK_HEADING_RE.match(line)
        if heading:
            inside = heading.group(1).strip().lower().startswith("failure modes")
            continue
        mode = _PLAYBOOK_MODE_RE.match(line)
        if mode:
            anywhere.add(int(mode.group(1)))
            if inside:
                in_section.add(int(mode.group(1)))
    return in_section or anywhere


def _check_mode_numbers(
    modes: Sequence[int], lineno: int, playbook_path: Path | None
) -> list[Violation]:
    """Checks listed failure mode numbers against the playbook, when one is given."""
    rule = "playbook-modes-invalid"
    hint = GROUNDING_FIX_HINTS[rule]
    if not modes:
        message = "'Failure modes checked:' lists no failure mode numbers."
        return [Violation(rule, message, lineno, hint)]
    if modes[0] < 1:
        message = (
            "'Failure modes checked:' lists 0, which is not a failure mode number."
        )
        return [Violation(rule, message, lineno, hint)]
    if playbook_path is None:
        return []
    defined = _playbook_failure_modes(playbook_path)
    unknown = [mode for mode in modes if mode not in defined]
    if not defined or not unknown:
        return []
    listed = ", ".join(str(mode) for mode in unknown)
    runs = ", ".join(_span_text(run) for run in _line_runs(defined))
    message = (
        f"'Failure modes checked:' lists {listed}, which `{playbook_path.name}` does not "
        f"define (it defines {runs})."
    )
    return [Violation(rule, message, lineno, hint)]


def check_worker_playbook_feedback(
    feedback_lines: Sequence[tuple[int, str]] | None,
    *,
    worker_mode: bool = False,
    playbook_path: Path | None = None,
    section_line: int = 1,
) -> list[Violation]:
    """Checks the `## Playbook Feedback` section of a reviewer draft or final report.

    A final report must not carry the section. A reviewer draft may, and a
    focus reviewer's draft must open it with the playbook-read confirmation
    `Failure modes checked: <numbers>`.

    The rules:

    - `playbook-feedback-leaked`: the section is present and `worker_mode` is
      false. The violation has no fix hint of its own.
    - `playbook-feedback-missing`: `worker_mode` is true and either the
      section is absent or empty while `playbook_path` is given, or the section
      has text that does not open with `Failure modes checked:`.
    - `playbook-modes-invalid`: the opening line lists no numbers, lists 0, or
      lists a number that the playbook at `playbook_path` does not define.
      Ranges such as `2-4` count as each number in the range.

    Without `playbook_path`, any positive numbers are accepted. That covers an
    ad hoc theme (`adhoc:<slug>`), whose playbook is inline in the reviewer
    brief and cannot be read here.

    Args:
        feedback_lines: `(line number, text)` pairs under the section heading,
            or None when the report has no such section.
        worker_mode: Whether the report is a reviewer draft, not a final report.
        playbook_path: The catalog playbook assigned to a focus reviewer.
        section_line: Line number of the section heading, used for violations
            that concern the section as a whole.

    Returns:
        The violations found, at most one.

    Raises:
        OSError: `playbook_path` cannot be read.
    """
    if not worker_mode:
        if feedback_lines is None:
            return []
        message = "Playbook Feedback section must be stripped before writing the final report."
        return [Violation("playbook-feedback-leaked", message, section_line)]
    missing_hint = GROUNDING_FIX_HINTS["playbook-feedback-missing"]
    first = _first_content_line(feedback_lines)
    if first is None:
        if playbook_path is None:
            return []
        message = (
            "Focus reviewer report has no Playbook Feedback for its assigned playbook."
        )
        return [
            Violation("playbook-feedback-missing", message, section_line, missing_hint)
        ]
    lineno, text = first
    plain = re.sub(r"[*_`]", "", _LIST_MARKER_RE.sub("", text)).strip()
    modes_line = _MODES_LINE_RE.fullmatch(plain)
    if modes_line is None:
        message = (
            "Playbook Feedback does not open with 'Failure modes checked: <numbers>'."
        )
        return [Violation("playbook-feedback-missing", message, lineno, missing_hint)]
    return _check_mode_numbers(
        _parse_mode_numbers(modes_line.group(1)), lineno, playbook_path
    )


@dataclass(frozen=True)
class _ClusterMember:
    """One finding prepared for comparison with findings from other reports."""

    report: int
    reviewer: str
    item_id: str
    title: str
    severity: str
    file: str
    span: tuple[int, int] | None
    tokens: frozenset[str]
    report_line: int


def _cluster_member(report: int, reviewer: str, item: Any) -> _ClusterMember:
    """Extracts the location and comparison tokens of one finding."""
    fields = item.fields
    title = str(getattr(item, "title", ""))
    wording = " ".join((title, str(fields.get("Body", "")), str(fields.get("Why", ""))))
    tokens = set(_CLUSTER_TOKEN_RE.findall(wording.lower())) - _CLUSTER_STOPWORDS
    item_id = str(item.id)
    severity = strip_ticks(str(fields.get("Severity", ""))).lower()
    fallback = (
        "question" if item_id.startswith("Q") else ID_TO_SEVERITY.get(item_id[:1], "")
    )
    return _ClusterMember(
        report=report,
        reviewer=reviewer,
        item_id=item_id,
        title=title,
        severity=severity or fallback,
        file=_anchor_file(item),
        span=_anchor_span(item),
        tokens=frozenset(tokens),
        report_line=getattr(item, "line", 1),
    )


def _same_file(a: str, b: str) -> bool:
    """Reports whether two `File` values name one file.

    An absolute path matches a relative path that it ends with, so a reviewer
    who links into a checkout and one who writes the repository path agree.
    """
    if a == b:
        return True
    if a.startswith("/") == b.startswith("/"):
        return False
    absolute, relative = (a, b) if a.startswith("/") else (b, a)
    return absolute.endswith("/" + relative)


def _same_defect(
    a: _ClusterMember, b: _ClusterMember, line_window: int, min_jaccard: float
) -> bool:
    """Reports whether two findings from different reports likely describe one defect."""
    if a.report == b.report or not a.file or not _same_file(a.file, b.file):
        return False
    if a.span is None or b.span is None:
        return False
    if max(a.span[0], b.span[0]) - min(a.span[1], b.span[1]) > line_window:
        return False
    union = a.tokens | b.tokens
    return bool(union) and len(a.tokens & b.tokens) / len(union) >= min_jaccard


def _cluster_root(parents: list[int], index: int) -> int:
    """Returns the representative of `index` in a union-find forest."""
    while parents[index] != index:
        parents[index] = parents[parents[index]]
        index = parents[index]
    return index


def _cluster_record(
    cluster_id: int, members: Sequence[_ClusterMember]
) -> dict[str, Any]:
    """Builds the JSON-serializable record of one cluster."""
    spans = [member.span for member in members if member.span is not None]
    files = [member.file for member in members]
    return {
        "cluster_id": cluster_id,
        "file": next((name for name in files if not name.startswith("/")), files[0]),
        "line_start": min((span[0] for span in spans), default=None),
        "line_end": max((span[1] for span in spans), default=None),
        "size": len(members),
        "reviewers": list(dict.fromkeys(member.reviewer for member in members)),
        "members": [
            {
                "reviewer": member.reviewer,
                "id": member.item_id,
                "title": member.title,
                "severity": member.severity,
                "line_start": member.span[0] if member.span else None,
                "line_end": member.span[1] if member.span else None,
                "report_line": member.report_line,
            }
            for member in members
        ],
    }


def cluster_findings(
    reports: Sequence[tuple[str, Sequence[Any]]],
    *,
    line_window: int = 15,
    min_jaccard: float = 0.25,
) -> list[dict[str, Any]]:
    """Groups findings from several reviewer reports that likely describe one defect.

    Two findings are linked when they come from different reports, name the
    same `File`, have `Line` ranges at most `line_window` lines apart, and
    share enough wording. `File` values are compared without Markdown markup,
    and an absolute path matches a relative path that it ends with. Wording is compared as the Jaccard similarity of the
    lowercase tokens of three or more characters in the title, `Body`, and
    `Why`, with common English words removed. Linked findings form a cluster,
    and links chain: if A matches B and B matches C, all three share a cluster.

    Findings in different files are never grouped, however similar their
    wording. A finding without a `File` or a `Line` stays alone.

    Args:
        reports: `(reviewer name, items)` pairs, one per report. Each item
            needs `id`, `title`, `line`, and `fields` attributes, as `Item` has.
        line_window: The largest gap in lines between two linked findings.
        min_jaccard: The smallest wording similarity of two linked findings.

    Returns:
        One JSON-serializable dict per cluster, ordered by each cluster's
        first finding in `reports`. A finding that matched nothing is a
        cluster of `size` 1. Each dict has `cluster_id` (from 1), `file` (a
        relative path when any member has one), `line_start` and `line_end` (the range covering all members, or None),
        `size`, `reviewers` (distinct, in order), and `members`. Each member
        has `reviewer`, `id`, `title`, `severity`, `line_start`, `line_end`,
        and `report_line` (the line of its heading in its report).
    """
    members = [
        _cluster_member(report, reviewer, item)
        for report, (reviewer, items) in enumerate(reports)
        for item in items
    ]
    parents = list(range(len(members)))
    for right, member in enumerate(members):
        for left in range(right):
            if _same_defect(members[left], member, line_window, min_jaccard):
                parents[_cluster_root(parents, right)] = _cluster_root(parents, left)
    # Members are visited in report order, so each group is created at its
    # first finding and the groups come out in that order.
    grouped: dict[int, list[_ClusterMember]] = {}
    for index, member in enumerate(members):
        grouped.setdefault(_cluster_root(parents, index), []).append(member)
    return [
        _cluster_record(number, group)
        for number, group in enumerate(grouped.values(), start=1)
    ]
