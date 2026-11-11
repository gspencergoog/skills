#!/usr/bin/env python3
"""Shared report parsing, structural checks, and prose linting for review skills."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

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
                Violation(seq_rule, f"Expected '{expected_prefix}' ID, got '{item.id}'.", item.line)
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
                Violation(seq_rule, f"Expected '{prefix}{expected_num}', got '{item.id}'.", item.line)
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
        sev = strip_ticks(item.fields.get("Severity", "")).lower() or ID_TO_SEVERITY.get(item.id[0], "")
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
            Violation(rule, f"'{item.id}' is missing a non-empty '{req}' field.", item.line)
        )
    for field_name, pattern in profile.validators.items():
        raw_val = item.fields.get(field_name, "").strip()
        if raw_val and not pattern.match(raw_val):
            rule = _rule_code(profile, f"invalid-{field_name.lower()}")
            violations.append(
                Violation(rule, f"'{item.id}' has invalid '{field_name}' value '{raw_val}'.", item.line)
            )
    return violations


def check_required_fields(items: Sequence[Item], profile: ReportProfile) -> list[Violation]:
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
    metrics = analyze_fn("-", complexity_threshold=MAX_SENTENCE_PCC, engine="stdlib", text=clean)
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
    out.extend(_prose_quality_violations(item_id, line, metrics.get("violations", {}), where))
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
