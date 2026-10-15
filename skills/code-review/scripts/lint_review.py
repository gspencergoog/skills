#!/usr/bin/env python3
"""Linter for synthesized code-review artifacts (review_results.md)."""

from dataclasses import dataclass, field
import json
import pathlib
import re
import sys
from typing import Any, Callable

_WRITE_PROSE_SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "write-prose" / "scripts"
if str(_WRITE_PROSE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_WRITE_PROSE_SCRIPTS))

from analyze_prose import analyze_prose

VALID_TIERS = ("Executed", "Read", "Fetched", "Deduction", "Speculation", "Recalled")
CLEAN_TIERS = ("Executed", "Read", "Fetched", "Deduction")
ID_TO_SEVERITY = {"C": "critical", "H": "high", "M": "medium", "L": "low"}
CLASS_ORDER = {"C": 0, "H": 1, "M": 2, "L": 3}

MAX_COMMENT_WORDS = 80
MAX_COMMENT_SENTENCES = 4
MAX_SENTENCE_WORDS = 25
MAX_SENTENCE_PCC = 5

_STOPWORDS = frozenset({
    "that", "this", "with", "from", "into", "when", "where", "which",
    "while", "will", "would", "could", "should", "have", "does", "were",
    "been", "being", "than", "then", "them", "they", "their", "there",
    "these", "those", "only", "also", "more", "most", "some", "such",
})
_TRAILING_SEVERITY_TAG_RE = re.compile(
    r"\s*\(\s*`?(?:critical|high|medium|low)`?(?:\s+severity)?\s*\)\s*([.!?]?)\s*$",
    re.IGNORECASE,
)

HEADING_RE = re.compile(r"^###\s+([CHMLQ]\d+)\.\s*(.*)$")
SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")
FIELD_RE = re.compile(r"^-\s+\*\*([^*]+)\*\*:\s*(.*)$")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
EMPTY_SECTION_TOKENS = {"", "none", "none.", "no findings", "no findings.", "no issues", "no issues."}


@dataclass
class FenceMeta:
    """How a fenced block under a finding field was written in the report."""

    fenced: bool = False
    info: str = ""
    lead_in: str = ""
    extra_fences: int = 0


# Finding fields whose value may be a fenced code block.
FENCED_FIELDS = ("Suggestion", "Repro test")


@dataclass
class Finding:
    """A parsed review finding or question block."""

    id: str
    severity: str
    fields: dict[str, str]
    line: int
    title: str = ""
    fence_meta: dict[str, FenceMeta] = field(default_factory=dict)

    def fence(self, field_name: str) -> FenceMeta:
        return self.fence_meta.setdefault(field_name, FenceMeta())


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
    summary_text: str = ""
    structural_text: str = ""
    review_comments_text: str = ""
    review_comments_line: int = 1
    verified_lines: list[int] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)


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


def _extract_evidence_detail(evidence_value: str) -> str:
    if not evidence_value:
        return ""
    parts = re.split(r"\s*(?:—|--|-|:)\s*", evidence_value.strip(), maxsplit=1)
    if len(parts) > 1:
        return parts[1].strip()
    return ""


def _classify_section(title: str) -> str:
    lowered = title.lower()
    if lowered.startswith("summary"):
        return "summary"
    if lowered.startswith("structural assessment"):
        return "structural"
    if lowered.startswith("review comments"):
        return "comments"
    if lowered.startswith("questions"):
        return "questions"
    if lowered.startswith("checked and found clean"):
        return "clean"
    if lowered.startswith("recommendations"):
        return "recommendations"
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
    if section == "clean" and stripped.startswith("- "):
        report.clean_lines.append(CleanLine(text=stripped[2:].strip(), line=lineno))
    elif section == "recommendations" and stripped.startswith("- "):
        report.recommendations.append(stripped[2:].strip())
    elif section == "summary":
        report.summary_text += stripped + "\n"
    elif section == "structural" and stripped:
        report.structural_text += stripped + "\n"
    elif section == "comments" and current is None and stripped:
        report.review_comments_text += stripped + "\n"
    return current


@dataclass
class _ParserState:
    report: Report = field(default_factory=Report)
    section: str = "other"
    current: Finding | None = None
    current_field: str | None = None
    fence_marker: str = ""
    capture_field: str | None = None
    fence_indent: int = 0
    captured_lines: list[str] = field(default_factory=list)


def _handle_fence_transition(state: _ParserState, raw_line: str) -> bool:
    """Updates fence state and captures fenced field bodies; returns True if line is consumed."""
    old_fence = state.fence_marker
    state.fence_marker, in_fence = _update_fence(state.fence_marker, raw_line)

    if state.capture_field is not None:
        if old_fence and not state.fence_marker:
            if state.current is not None:
                state.current.fields[state.capture_field] = "\n".join(state.captured_lines)
            state.capture_field = None
            return True
        leading = len(raw_line) - len(raw_line.lstrip(" "))
        state.captured_lines.append(raw_line[min(state.fence_indent, leading):])
        return True

    if (
        not old_fence
        and state.fence_marker
        and state.current is not None
        and state.current_field in FENCED_FIELDS
    ):
        meta = state.current.fence(state.current_field)
        if meta.fenced:
            meta.extra_fences += 1
        else:
            state.capture_field = state.current_field
            meta.fenced = True
            meta.info = raw_line.strip().lstrip("`~").strip()
            meta.lead_in = state.current.fields.get(state.current_field, "")
            state.fence_indent = len(raw_line) - len(raw_line.lstrip(" "))
            state.captured_lines = []
        return True

    return in_fence


def _handle_unquoted_line(state: _ParserState, stripped: str, lineno: int) -> None:
    """Processes a single non-fenced markdown line in the review report."""
    field_match = FIELD_RE.match(stripped)
    if field_match and field_match.group(1).strip().lower() == "verified":
        state.report.verified_lines.append(lineno)

    section_match = SECTION_RE.match(stripped)
    if section_match:
        state.section = _classify_section(section_match.group(1))
        if state.section == "comments":
            state.report.review_comments_line = lineno
        state.current = None
        state.current_field = None
        return

    heading_match = HEADING_RE.match(stripped)
    if heading_match and state.section in ("comments", "questions"):
        finding = Finding(
            id=heading_match.group(1),
            severity="",
            fields={},
            line=lineno,
            title=heading_match.group(2).strip(),
        )
        target = state.report.questions if state.section == "questions" else state.report.findings
        target.append(finding)
        state.current = finding
        state.current_field = None
        return

    if field_match and state.current is not None:
        key = field_match.group(1).strip()
        val = field_match.group(2).strip()
        state.current.fields[key] = val
        state.current_field = key
        if key.lower() == "severity":
            state.current.severity = _strip_ticks(val).lower()
        return

    state.current = _record_section_line(
        state.report, state.section, state.current, stripped, lineno
    )


def parse_report(text: str) -> Report:
    """Parse a markdown review report into findings, questions, and clean lines."""
    state = _ParserState()
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        if _handle_fence_transition(state, raw_line):
            continue
        _handle_unquoted_line(state, raw_line.strip(), lineno)
    return state.report


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


def _check_single_fence_per_field(report: Report) -> list[Violation]:
    """Each fenced field holds one block. A second fence usually means
    "replace X with Y", and the exporter cannot tell X from Y."""
    violations: list[Violation] = []
    for f in report.findings:
        for field_name, meta in f.fence_meta.items():
            if meta.extra_fences:
                violations.append(
                    Violation(
                        f.line,
                        "suggestion-single-fence",
                        f"'{f.id}' {field_name} has more than one fenced block; "
                        "keep only the drop-in replacement (move a repro test to "
                        "'**Repro test**:').",
                    )
                )
    return violations


def _content_words(text: str) -> set[str]:
    return {
        w for w in re.findall(r"[a-z0-9_]{4,}", text.lower())
        if w not in _STOPWORDS
    }


def _why_already_covered(body: str, why: str) -> bool:
    """Returns True when `body` already states the consequence in `why`."""
    if not body or not why:
        return True
    norm_body = re.sub(r"\s+", " ", body.strip().rstrip(".")).lower()
    norm_why = re.sub(r"\s+", " ", why.strip().rstrip(".")).lower()
    if norm_why in norm_body:
        return True
    why_words = _content_words(norm_why)
    if not why_words:
        return False
    body_words = _content_words(norm_body)
    return (len(why_words & body_words) / len(why_words)) >= 0.5


def _build_finding_prose(body: str, why: str) -> str:
    """Combines `body` with `why` only when `body` does not already state `why`."""
    clean_body = body.strip()
    clean_why = why.strip()
    if not clean_body:
        return clean_why
    if not clean_why or _why_already_covered(clean_body, clean_why):
        return clean_body
    sep = " " if clean_body.endswith((".", "!", "?")) else ". "
    return f"{clean_body}{sep}{clean_why}"


def _strip_trailing_severity_tag(text: str) -> str:
    """Strips trailing severity tags like (`medium`) or (`medium` severity) from question Why text."""
    cleaned = _TRAILING_SEVERITY_TAG_RE.sub(r"\1", text.strip()).strip()
    if cleaned and not cleaned.endswith((".", "!", "?")):
        cleaned += "."
    return cleaned


def _build_question_prose(q: Finding) -> str:
    """Builds peer-facing question prose from `q.title`, optional `Body`, and cleaned `Why`."""
    title = q.title.strip()
    body = q.fields.get("Body", "").strip()
    why = _strip_trailing_severity_tag(q.fields.get("Why", ""))
    question_text = title
    if body and body.lower() not in title.lower():
        sep = " " if question_text.endswith((".", "!", "?")) else "? "
        question_text = f"{question_text}{sep}{body}" if question_text else body
    if question_text and not question_text.endswith((".", "!", "?")):
        question_text += "?"
    return _build_finding_prose(question_text, why)


def _extract_finding_comment_prose(f: Finding) -> str:
    """Returns the prose portion of a finding's assembled comment (excluding code fences)."""
    body = f.fields.get("Body", "").strip()
    why = f.fields.get("Why", "").strip()
    sugg = f.fields.get("Suggestion", "").rstrip().lstrip("\n")
    sugg_meta = f.fence("Suggestion")
    lead_in = sugg_meta.lead_in.strip()
    parts: list[str] = []
    main_prose = _build_finding_prose(body, why)
    if main_prose:
        parts.append(main_prose)
    if lead_in:
        parts.append(lead_in)
    if sugg:
        formatted_sugg = _format_suggestion(sugg_meta, sugg)
        if not formatted_sugg.startswith("```"):
            parts.append(formatted_sugg)
    return "\n\n".join(parts)


def _collect_ai_ism_labels(violations_dict: dict[str, Any]) -> list[str]:
    """Collects human-readable labels for AI-ism, em-dash, and noun-cluster violations."""
    labels: list[str] = []
    for item in violations_dict.get("banned_ai_words_found", []):
        labels.append(f"banned word '{item['banned_word']}'")
    for item in violations_dict.get("banned_ai_phrases_found", []):
        labels.append(f"banned phrase '{item['banned_phrase']}'")
    if violations_dict.get("em_dashes_found"):
        labels.append("em-dash '—'")
    for item in violations_dict.get("noun_clusters_found", []):
        labels.append(f"noun cluster '{item['cluster']}'")
    return labels


def _lint_single_comment_prose(item_id: str, line: int, prose: str) -> list[Violation]:
    """Runs write-prose checks on a single finding or question's assembled prose."""
    if not prose.strip():
        return []
    metrics = analyze_prose(
        "-",
        complexity_threshold=MAX_SENTENCE_PCC,
        engine="stdlib",
        text=prose,
    )
    if "error" in metrics:
        return []
    summary = metrics["summary"]
    v_dict = metrics["violations"]
    out: list[Violation] = []

    words = summary["total_words"]
    sentences = summary["total_sentences"]
    if words > MAX_COMMENT_WORDS or sentences > MAX_COMMENT_SENTENCES:
        out.append(
            Violation(
                line,
                "comment-prose-length",
                f"'{item_id}' comment prose is {words} words / {sentences} sentences "
                f"(max {MAX_COMMENT_WORDS} words, {MAX_COMMENT_SENTENCES} sentences).",
            )
        )

    long_sents = v_dict.get("sentences_exceeding_25_words", [])
    if long_sents:
        longest = max(s["word_count"] for s in long_sents)
        out.append(
            Violation(
                line,
                "comment-sentence-length",
                f"'{item_id}' has {len(long_sents)} sentence(s) exceeding "
                f"{MAX_SENTENCE_WORDS} words (longest: {longest} words).",
            )
        )

    complex_sents = v_dict.get("complex_sentences_found", [])
    if complex_sents:
        worst = max(s["cognitive_complexity"] for s in complex_sents)
        out.append(
            Violation(
                line,
                "comment-sentence-complexity",
                f"'{item_id}' has {len(complex_sents)} sentence(s) with cognitive complexity "
                f"PCC > {MAX_SENTENCE_PCC} (max PCC: {worst}).",
            )
        )

    ai_labels = _collect_ai_ism_labels(v_dict)
    if ai_labels:
        out.append(
            Violation(
                line,
                "comment-ai-ism",
                f"'{item_id}' contains prose violation(s): {', '.join(ai_labels)}.",
            )
        )
    return out


def _check_comment_prose(report: Report) -> list[Violation]:
    """Runs write-prose checks on each finding and question comment prose."""
    violations: list[Violation] = []
    for f in report.findings:
        prose = _extract_finding_comment_prose(f)
        violations.extend(_lint_single_comment_prose(f.id, f.line, prose))
    for q in report.questions:
        prose = _build_question_prose(q)
        violations.extend(_lint_single_comment_prose(q.id, q.line, prose))
    return violations


RULES: list[Callable[[Report], list[Violation]]] = [
    _check_unparsed_comments,
    _check_ids_sequential,
    _check_severity_matches_id,
    _check_evidence_and_caps,
    _check_why_required,
    _check_questions,
    _check_clean_section,
    _check_verified_removed,
    _check_single_fence_per_field,
    _check_comment_prose,
]


def check_report(report: Report) -> list[Violation]:
    """Run all lint rules against a parsed Report."""
    violations: list[Violation] = []
    for rule_fn in RULES:
        violations.extend(rule_fn(report))
    return sorted(violations, key=lambda v: (v.line, v.rule))


def _parse_line_range(line_val: str) -> tuple[int, int]:
    cleaned = _strip_ticks(line_val)
    m = re.search(r"(\d+)(?:\s*(?:-|–|—|\.\.)\s*(\d+))?", cleaned)
    if not m:
        return 1, 1
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else start
    return start, end


VERDICT_LINE_RE = re.compile(
    r"\bverdict\b\W*(critical blockers?|minor revisions? needed|ready)\b", re.IGNORECASE
)

# Process sentences that code-review's Summary carries for the orchestrator
# (citation audit tally, flags in effect, trust limit, challenge tally, and
# the closing Verdict label). They are parsed into structured fields and
# removed from the public overview. Each alternative is anchored to a
# sentence start and bounded at a sentence end, so prose such as "New Flags:
# `--dry-run` are wired" or text after the trust-limit sentence is left alone.
_FLAG_TOKEN = r"(?:`[^`\n]*`|--[\w-]+)"
_SENTENCE_END = r"(?:\.(?=\s|$)|[ \t]*$)"
INTERNAL_SUMMARY_RE = re.compile(
    r"(?:^|(?<=[.!?]))[ \t]*(?:"
    r"Citation audit:[^\n]*?dropped(?: \((?:[^()\n]|\([^()\n]*\))*\))?\.?"
    r"|Citation audit: none required(?: \([^()\n]*\))?\.?"
    r"|Flags:\s*" + _FLAG_TOKEN
    + r"(?:(?:\s*,\s*|\s+)(?:and\s+)?" + _FLAG_TOKEN + r")*"
    r"(?:\s*\([^()\n]*\))?" + _SENTENCE_END
    + r"|`--verify` limited to tier 1:[^\n]*?" + _SENTENCE_END
    + r"|\d+ disputed \([^)\n]*\)\.?"
    r"|(?i:verdict)\W*(?i:critical blockers?|minor revisions? needed|ready)\b\.?"
    r")",
    re.MULTILINE,
)

# Fence info strings whose content is a patch, not a drop-in replacement.
NON_SUGGESTION_FENCES = frozenset({"diff", "patch"})

# Lowercase keywords that mark an unfenced single line as code.
CODE_LINE_PREFIXES = (
    "return ",
    "await ",
    "def ",
    "const ",
    "let ",
    "var ",
    "import ",
    "class ",
)


def _parse_verdict(summary_text: str, findings: list[Finding]) -> str:
    """Read the closing `Verdict:` label; fall back to finding severities.

    The last match wins, so a Summary that mentions an earlier pass's label
    in prose still exports the label on its closing line.
    """
    labels = VERDICT_LINE_RE.findall(summary_text)
    if labels:
        label = labels[-1].lower()
        if label.startswith("critical"):
            return "REQUEST_CHANGES"
        return "COMMENT" if label.startswith("minor") else "APPROVE"
    if any(f.severity in ("critical", "high") for f in findings):
        return "REQUEST_CHANGES"
    if any(f.severity in ("medium", "low") for f in findings):
        return "COMMENT"
    return "APPROVE"


def _parse_citation_audit(summary_text: str) -> dict[str, int]:
    m = re.search(r"[Cc]itation audit:\s*(\d+)\s+checked,\s*(\d+)\s+dropped", summary_text)
    if m:
        return {"checked": int(m.group(1)), "dropped": int(m.group(2))}
    return {"checked": 0, "dropped": 0}


def _parse_flags(summary_text: str) -> list[str]:
    m = re.search(r"[Ff]lags:\s*([^\n\.]+)", summary_text)
    if m:
        raw_flags = m.group(1)
        return re.findall(r"--[a-z-]+", raw_flags)
    return []


def _parse_structural_assessment(structural_text: str) -> dict[str, str]:
    result = {}
    for line in structural_text.splitlines():
        m = re.match(r"^-\s+\*\*([^*]+)\*\*:\s*(.*)$", line.strip())
        if m:
            result[m.group(1).strip().lower()] = m.group(2).strip()
    return result


def _clean_overview(summary_text: str) -> str:
    """Strip process sentences paragraph by paragraph, keeping paragraph breaks."""
    paragraphs = re.split(r"\n[ \t]*\n", summary_text)
    cleaned = (INTERNAL_SUMMARY_RE.sub("", p).strip() for p in paragraphs)
    return "\n\n".join(p for p in cleaned if p)


def _format_suggestion(meta: FenceMeta, sugg: str) -> str:
    """Return the markdown for a finding's Suggestion in the draft comment.

    Routing rules (lead-in prose before the fence never changes the result):
    - ```diff and ```patch fences are re-emitted as-is: their +/- markers
      are not drop-in replacement lines.
    - Any other fenced block (```suggestion, a language tag, or a bare
      fence) becomes a committable ```suggestion block.
    - Unfenced text is wrapped as a suggestion only when it looks like code
      (multi-line, or starts with a code keyword); otherwise it is prose.
    """
    fence_info = meta.info.strip().lower()
    if sugg.startswith("```"):
        return sugg
    if fence_info in NON_SUGGESTION_FENCES:
        return f"```{fence_info}\n{sugg}\n```"
    if meta.fenced:
        return f"```suggestion\n{sugg}\n```"
    if "\n" in sugg or sugg.startswith(CODE_LINE_PREFIXES):
        return f"```suggestion\n{sugg}\n```"
    return sugg


def _format_repro_test(meta: FenceMeta, test: str) -> str:
    """Return the markdown for a finding's Repro test: never committable."""
    if test.startswith("```"):
        return test
    info = meta.info.strip().lower()
    if info in ("", "suggestion"):
        info = "python" if meta.fenced else ""
    if meta.fenced or "\n" in test:
        return f"```{info}\n{test}\n```"
    return test


def _build_finding_draft_comment(f: Finding, body: str, why: str, sugg: str, repro: str) -> str:
    """Assembles the peer-facing draft_comment for a finding."""
    sugg_meta = f.fence("Suggestion")
    lead_in = sugg_meta.lead_in.strip()
    repro_meta = f.fence("Repro test")
    repro_lead_in = repro_meta.lead_in.strip()

    draft_parts: list[str] = []
    main_prose = _build_finding_prose(body, why)
    if main_prose:
        draft_parts.append(main_prose)
    if lead_in:
        draft_parts.append(lead_in)
    if sugg:
        draft_parts.append(_format_suggestion(sugg_meta, sugg))
    if repro:
        heading = f"**Repro test**: {repro_lead_in}" if repro_lead_in else "**Repro test**"
        draft_parts.append(heading)
        draft_parts.append(_format_repro_test(repro_meta, repro))
    return "\n\n".join(draft_parts)


def _build_finding_dict(f: Finding) -> dict[str, Any]:
    """Converts a parsed Finding into its exported JSON dictionary."""
    line_start, line_end = _parse_line_range(f.fields.get("Line", "1"))
    ev = f.fields.get("Evidence", "")
    body = f.fields.get("Body", "").strip()
    why = f.fields.get("Why", "").strip()
    sugg = f.fields.get("Suggestion", "").rstrip().lstrip("\n")
    repro = f.fields.get("Repro test", "").rstrip().lstrip("\n")
    desc = f.title or (body.split(".")[0].strip() + "." if body else f.id)

    finding_dict: dict[str, Any] = {
        "id": f.id,
        "severity": f.severity or "medium",
        "file_path": _strip_ticks(f.fields.get("File", "")),
        "line_start": line_start,
        "line_end": line_end,
        "side": "RIGHT",
        "evidence_tier": _extract_tier(ev),
        "evidence_detail": _extract_evidence_detail(ev),
        "description": desc,
        "body": body,
        "why": why,
        "draft_comment": _build_finding_draft_comment(f, body, why, sugg, repro),
    }
    if "Category" in f.fields:
        finding_dict["category"] = f.fields["Category"].strip().lower()
    if sugg:
        finding_dict["suggestion"] = sugg
    if repro:
        finding_dict["repro_test"] = repro
    return finding_dict


def _build_question_dict(q: Finding) -> dict[str, Any]:
    """Converts a parsed Question Finding into its exported JSON dictionary."""
    line_start, line_end = _parse_line_range(q.fields.get("Line", "1"))
    ev = q.fields.get("Evidence", "")
    why = q.fields.get("Why", "").strip()
    to_settle = q.fields.get("To settle", "").strip()
    desc = q.title or q.fields.get("Body", "").strip()
    return {
        "id": q.id,
        "severity": "question",
        "file_path": _strip_ticks(q.fields.get("File", "")),
        "line_start": line_start,
        "line_end": line_end,
        "side": "RIGHT",
        "evidence_tier": _extract_tier(ev),
        "evidence_detail": _extract_evidence_detail(ev),
        "description": desc,
        "why": why,
        "to_settle": to_settle,
        "draft_comment": _build_question_prose(q),
    }


def export_report_to_json(report: Report, output_path: pathlib.Path) -> dict[str, Any]:
    """Exports a validated Report to canonical review_results.json."""
    findings_list = [_build_finding_dict(f) for f in report.findings]
    questions_list = [_build_question_dict(q) for q in report.questions]
    cleaned_overview = _clean_overview(report.summary_text)

    payload: dict[str, Any] = {
        "review_summary": {
            "overview": cleaned_overview,
            "verdict": _parse_verdict(report.summary_text, report.findings),
            "citation_audit": _parse_citation_audit(report.summary_text),
            "flags": _parse_flags(report.summary_text),
            "checked_clean": [cl.text for cl in report.clean_lines],
        },
        "findings": findings_list,
        "questions": questions_list,
    }
    structural = _parse_structural_assessment(report.structural_text)
    if structural:
        payload["review_summary"]["structural_assessment"] = structural
    if report.recommendations:
        payload["recommendations"] = report.recommendations

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: lint_review.py <review_results.md> [--export-json <output.json>]."""
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print(
            "Usage: lint_review.py <review_results.md> [--export-json <path>]",
            file=sys.stderr,
        )
        return 2

    report_path = pathlib.Path(args[0])
    export_json_path: pathlib.Path | None = None

    if len(args) == 3 and args[1] == "--export-json":
        export_json_path = pathlib.Path(args[2])
    elif len(args) != 1:
        print(
            "Usage: lint_review.py <review_results.md> [--export-json <path>]",
            file=sys.stderr,
        )
        return 2

    if not report_path.is_file():
        print(f"Error: file not found: {report_path}", file=sys.stderr)
        return 2

    raw_text = report_path.read_text(encoding="utf-8")
    report = parse_report(raw_text)
    violations = check_report(report)
    for v in violations:
        print(f"{v.line}: {v.rule}: {v.message}")

    if violations:
        return 1

    if export_json_path is not None:
        export_report_to_json(report, export_json_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
