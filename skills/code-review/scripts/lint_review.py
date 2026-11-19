#!/usr/bin/env python3
"""Linter for synthesized code-review artifacts (review_results.md)."""

import argparse
import json
import pathlib
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

_SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2]
for _rel_scripts in (("write-prose", "scripts"), ("review-core", "scripts")):
    _script_dir = str(_SKILLS_DIR.joinpath(*_rel_scripts))
    if _script_dir not in sys.path:
        sys.path.insert(0, _script_dir)

from analyze_prose import analyze_prose
from review_lint_common import (
    GROUNDING_FIX_HINTS,
    VERDICT_LINE_RE,
    FenceMeta,
    ReportProfile,
    StagedDiffIndex,
    Violation,
    check_clean_lines,
    check_evidence_caps,
    check_grounding,
    check_id_sequence,
    check_prose,
    check_questions,
    check_required_fields,
    check_severity_matches_id,
    check_single_fence_per_field,
    check_worker_playbook_feedback,
    cluster_findings,
    extract_evidence_detail,
    extract_tier,
    load_staged_diff_index,
    strip_ticks,
    update_fence,
)


@dataclass(frozen=True)
class CommentCaps:
    """Prose caps for one comment profile.

    `words` and `sentences` bound the visible comment prose (Body, Why, and a
    prose Suggestion). `details_words` bounds the optional `Details` field,
    which is exported inside a collapsed `<details>` block.
    """

    words: int
    sentences: int
    details_words: int


# `default` applies to standalone code-review reports. `pr` applies when
# pr-review posts the comments to GitHub, where the extra detail is folded.
COMMENT_PROFILES: dict[str, CommentCaps] = {
    "default": CommentCaps(words=120, sentences=6, details_words=150),
    "pr": CommentCaps(words=120, sentences=6, details_words=150),
}


def _prose_length_hint(caps: CommentCaps) -> str:
    return (
        "Shorten the combined Body, Why, and prose Suggestion to at most "
        f"{caps.words} words and {caps.sentences} sentences. Move supporting detail "
        "into '- **Details**:'."
    )


def _details_length_hint(caps: CommentCaps) -> str:
    return (
        f"Shorten the Details field to at most {caps.details_words} words; keep only "
        "the root-cause trace, affected callers, or rejected alternatives."
    )


MAX_COMMENT_WORDS = COMMENT_PROFILES["default"].words
MAX_COMMENT_SENTENCES = COMMENT_PROFILES["default"].sentences

_STOPWORDS = frozenset(
    {
        "that",
        "this",
        "with",
        "from",
        "into",
        "when",
        "where",
        "which",
        "while",
        "will",
        "would",
        "could",
        "should",
        "have",
        "does",
        "were",
        "been",
        "being",
        "than",
        "then",
        "them",
        "they",
        "their",
        "there",
        "these",
        "those",
        "only",
        "also",
        "more",
        "most",
        "some",
        "such",
    }
)
_TRAILING_SEVERITY_TAG_RE = re.compile(
    r"\s*\(\s*`?(?:critical|high|medium|low)`?(?:\s+severity)?\s*\)\s*([.!?]?)\s*$",
    re.IGNORECASE,
)

HEADING_RE = re.compile(r"^###\s+([CHMLQ]\d+)\.\s*(.*)$")
SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")
FIELD_RE = re.compile(r"^-\s+\*\*([^*]+)\*\*:\s*(.*)$")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
FOCUS_ID_RE = re.compile(r"^`[a-z0-9][a-z0-9-]*(?::[a-z0-9-]+)?`$")
EMPTY_SECTION_TOKENS = {
    "",
    "none",
    "none.",
    "no findings",
    "no findings.",
    "no issues",
    "no issues.",
}


# Finding fields whose value may be a fenced code block.
FENCED_FIELDS = ("Suggestion", "Repro test")

# Finding fields whose prose may continue on the lines after the field bullet.
CONTINUED_FIELDS = ("Details",)


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
    playbook_feedback_line: int = 0
    playbook_feedback_lines: list[tuple[int, str]] | None = None


_FIX_HINTS: dict[str, str] = {
    "review-comments-unparsed": (
        "Format each finding as '### <ID>. <title>' (for example '### H1. Missing null check') "
        "or write 'None.' when there are no findings."
    ),
    "ids-sequential": (
        "Order findings by severity class (C, H, M, L) and number each class sequentially "
        "starting at 1 (C1, C2, H1, M1, L1; questions Q1, Q2)."
    ),
    "severity-matches-id": (
        "Set '- **Severity**:' to match the heading prefix (C -> critical, H -> high, "
        "M -> medium, L -> low)."
    ),
    "evidence-required": (
        "Add '- **Evidence**: `<Tier>` - <detail>' with a valid tier (Executed, Read, "
        "Fetched, Deduction, or Speculation)."
    ),
    "evidence-recalled": (
        "Verify the claim in the repository or docs and replace 'Recalled' with 'Read', "
        "'Executed', 'Fetched', or 'Deduction', or drop the finding."
    ),
    "severity-cap": (
        "Verify the claim to upgrade Evidence to 'Read', 'Executed', 'Fetched', or "
        "'Deduction', or downgrade the finding to M/L (medium/low)."
    ),
    "why-required": (
        "Add a non-empty '- **Why**: <concrete consequence>' field explaining the failure "
        "mode or impact."
    ),
    "question-fields": (
        "Keep at most 3 questions (Q1-Q3) and include a non-empty '- **To settle**: <check>' "
        "field on each."
    ),
    "clean-max-10": "Trim '## Checked and Found Clean' to at most 10 bullet lines.",
    "clean-line-tier": (
        "Include a valid verification tier (Executed, Read, Fetched, or Deduction) on every "
        "'## Checked and Found Clean' bullet."
    ),
    "verified-removed": (
        "Remove '- **Verified**:' and record verification under "
        "'- **Evidence**: `Executed` - <command and result>'."
    ),
    "focus-id-format": (
        "Format '- **Focus**:' as a single backticked slug such as `sync-to-async` or "
        "`adhoc:<slug>`."
    ),
    "playbook-feedback-leaked": (
        "Remove the '## Playbook Feedback' section from review_results.md before linting."
    ),
    "suggestion-single-fence": (
        "Keep only one fenced replacement block under '- **Suggestion**:' and move any test "
        "block to '- **Repro test**:'."
    ),
    "comment-prose-length": _prose_length_hint(COMMENT_PROFILES["default"]),
    "comment-details-length": _details_length_hint(COMMENT_PROFILES["default"]),
    "comment-sentence-length": (
        "Split or trim sentences in Body, Why, or Suggestion so every sentence has at most "
        "30 words."
    ),
    "comment-sentence-complexity": (
        "Split subordinate clauses, remove passive voice, or shorten subject-verb distance "
        "so sentence PCC is at most 5."
    ),
    "comment-ai-ism": (
        "Remove banned AI words/phrases, replace em-dashes with commas or parentheses, and "
        "break up 4+ noun stacks with prepositions."
    ),
    **GROUNDING_FIX_HINTS,
}


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
    if lowered.startswith("playbook feedback"):
        return "playbook_feedback"
    return "other"


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
    elif section == "playbook_feedback" and stripped:
        if report.playbook_feedback_lines is None:
            report.playbook_feedback_lines = []
        report.playbook_feedback_lines.append((lineno, stripped))
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
    state.fence_marker, in_fence = update_fence(state.fence_marker, raw_line)

    if state.capture_field is not None:
        if old_fence and not state.fence_marker:
            if state.current is not None:
                state.current.fields[state.capture_field] = "\n".join(
                    state.captured_lines
                )
            state.capture_field = None
            return True
        leading = len(raw_line) - len(raw_line.lstrip(" "))
        state.captured_lines.append(raw_line[min(state.fence_indent, leading) :])
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


def _enter_section(state: _ParserState, title: str, lineno: int) -> None:
    state.section = _classify_section(title)
    if state.section == "comments":
        state.report.review_comments_line = lineno
    elif state.section == "playbook_feedback":
        state.report.playbook_feedback_line = (
            state.report.playbook_feedback_line or lineno
        )
        if state.report.playbook_feedback_lines is None:
            state.report.playbook_feedback_lines = []
    state.current = None
    state.current_field = None


def _handle_unquoted_line(state: _ParserState, stripped: str, lineno: int) -> None:
    """Processes a single non-fenced markdown line in the review report."""
    field_match = FIELD_RE.match(stripped)
    if field_match and field_match.group(1).strip().lower() == "verified":
        state.report.verified_lines.append(lineno)

    section_match = SECTION_RE.match(stripped)
    if section_match:
        _enter_section(state, section_match.group(1), lineno)
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
        target = (
            state.report.questions
            if state.section == "questions"
            else state.report.findings
        )
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
            state.current.severity = strip_ticks(val).lower()
        return

    if state.current is not None and state.current_field in CONTINUED_FIELDS:
        prev = state.current.fields.get(state.current_field, "")
        state.current.fields[state.current_field] = f"{prev}\n{stripped}"
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


_WHY_PROFILE = ReportProfile(
    name="code",
    sections=(),
    findings_section="",
    questions_section="",
    required_fields=("Why",),
    rule_names={"why-required": "why-required"},
)
_FOCUS_PROFILE = ReportProfile(
    name="code",
    sections=(),
    findings_section="",
    questions_section="",
    required_fields=(),
    validators={"Focus": FOCUS_ID_RE},
    rule_names={"invalid-focus": "focus-id-format"},
)


def _check_unparsed_comments(report: Report) -> list[Violation]:
    norm = report.review_comments_text.strip().lower()
    if not report.findings and norm not in EMPTY_SECTION_TOKENS:
        return [
            Violation(
                "review-comments-unparsed",
                "Review Comments has text but no '### <ID>. <claim>' findings were parsed.",
                report.review_comments_line,
            )
        ]
    return []


def _check_ids_sequential(report: Report) -> list[Violation]:
    return check_id_sequence(report.findings) + check_id_sequence(
        report.questions, expected_prefix="Q"
    )


def _check_severity_matches_id(report: Report) -> list[Violation]:
    return check_severity_matches_id(report.findings)


def _check_evidence_and_caps(report: Report) -> list[Violation]:
    return check_evidence_caps(report.findings, is_findings=True) + check_evidence_caps(
        report.questions, is_findings=False
    )


def _check_why_required(report: Report) -> list[Violation]:
    return check_required_fields(report.findings + report.questions, _WHY_PROFILE)


def _check_questions(report: Report) -> list[Violation]:
    return check_questions(report.questions)


def _check_clean_section(report: Report) -> list[Violation]:
    return check_clean_lines([(cl.line, cl.text) for cl in report.clean_lines])


def _check_verified_removed(report: Report) -> list[Violation]:
    return [
        Violation(
            "verified-removed",
            "Deprecated '**Verified**:' field found; use '**Evidence**: `Executed`'.",
            line,
        )
        for line in report.verified_lines
    ]


def _check_focus_field(report: Report) -> list[Violation]:
    return check_required_fields(report.findings + report.questions, _FOCUS_PROFILE)


def _check_single_fence_per_field(report: Report) -> list[Violation]:
    return check_single_fence_per_field(report.findings)


def _content_words(text: str) -> set[str]:
    return {
        w for w in re.findall(r"[a-z0-9_]{4,}", text.lower()) if w not in _STOPWORDS
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


def _check_comment_prose(report: Report, caps: CommentCaps) -> list[Violation]:
    """Runs write-prose checks on each finding and question comment, plus Details."""
    prose_profile = ReportProfile(
        name="code",
        sections=(),
        findings_section="",
        questions_section="",
        required_fields=(),
        max_words=caps.words,
        max_sentences=caps.sentences,
        details_words=caps.details_words,
    )
    return check_prose(
        report.findings,
        prose_profile,
        analyze_prose,
        prose_builder=_extract_finding_comment_prose,
        check_details=True,
    ) + check_prose(
        report.questions,
        prose_profile,
        analyze_prose,
        prose_builder=_build_question_prose,
        check_details=False,
    )


RULES: list[Callable[[Report], list[Violation]]] = [
    _check_unparsed_comments,
    _check_ids_sequential,
    _check_severity_matches_id,
    _check_evidence_and_caps,
    _check_why_required,
    _check_questions,
    _check_clean_section,
    _check_verified_removed,
    _check_focus_field,
    _check_single_fence_per_field,
]


def _attach_fix_hint(v: Violation, caps: CommentCaps) -> Violation:
    if v.fix_hint:
        return v
    if v.rule == "comment-prose-length":
        hint = _prose_length_hint(caps)
    elif v.rule == "comment-details-length":
        hint = _details_length_hint(caps)
    else:
        hint = _FIX_HINTS.get(v.rule, "")
    return replace(v, fix_hint=hint) if hint else v


def _resolve_grounding_index(
    diff_dir: pathlib.Path | str | None,
    diff_index: StagedDiffIndex | None,
) -> StagedDiffIndex | None:
    """Returns `diff_index`, or loads one from `diff_dir` when provided."""
    if diff_index is not None and diff_dir is not None:
        raise ValueError("Pass at most one of diff_dir and diff_index.")
    if diff_index is not None:
        return diff_index
    if diff_dir is not None:
        return load_staged_diff_index(pathlib.Path(diff_dir))
    return None


def _check_playbook_feedback(
    report: Report,
    worker_mode: bool,
    playbook_path: pathlib.Path | None,
) -> list[Violation]:
    """Validates `## Playbook Feedback` rules for final reports or worker drafts."""
    # Preserve detection if a caller constructed `Report(playbook_feedback_line=N)`.
    feedback_lines = (
        []
        if report.playbook_feedback_lines is None and report.playbook_feedback_line
        else report.playbook_feedback_lines
    )
    return check_worker_playbook_feedback(
        feedback_lines,
        worker_mode=worker_mode,
        playbook_path=playbook_path,
        section_line=report.playbook_feedback_line or 1,
    )


def check_report(
    report: Report,
    profile: str = "default",
    *,
    workspace: pathlib.Path | str | None = None,
    diff_dir: pathlib.Path | str | None = None,
    diff_index: StagedDiffIndex | None = None,
    worker_mode: bool = False,
    playbook_path: pathlib.Path | str | None = None,
) -> list[Violation]:
    """Run all lint rules against a parsed Report.

    `profile` names an entry in COMMENT_PROFILES and sets the comment prose caps.
    When `workspace`, `diff_dir`, or `diff_index` is given, validates file
    existence, line bounds, changed lines, and `Evidence: Read` quotes across
    findings and questions. When `worker_mode` is True, validates
    `## Playbook Feedback` against `playbook_path` (when given) instead of
    rejecting the section. Raises ValueError for an unknown profile or if both
    `diff_dir` and `diff_index` are passed.
    """
    if profile not in COMMENT_PROFILES:
        raise ValueError(
            f"Unknown comment profile '{profile}' "
            f"(expected one of: {', '.join(COMMENT_PROFILES)})."
        )
    resolved_workspace = pathlib.Path(workspace) if workspace is not None else None
    resolved_index = _resolve_grounding_index(diff_dir, diff_index)
    resolved_playbook = (
        pathlib.Path(playbook_path) if playbook_path is not None else None
    )

    caps = COMMENT_PROFILES[profile]
    violations: list[Violation] = []
    for rule_fn in RULES:
        violations.extend(rule_fn(report))
    violations.extend(_check_playbook_feedback(report, worker_mode, resolved_playbook))
    if resolved_workspace is not None or resolved_index is not None:
        violations.extend(
            check_grounding(
                report.findings + report.questions,
                workspace=resolved_workspace,
                diff_index=resolved_index,
            )
        )
    violations.extend(_check_comment_prose(report, caps))
    with_hints = [_attach_fix_hint(v, caps) for v in violations]
    return sorted(with_hints, key=lambda v: (v.line, v.rule))


def _parse_line_range(line_val: str) -> tuple[int, int]:
    cleaned = strip_ticks(line_val)
    m = re.search(r"(\d+)(?:\s*(?:-|–|—|\.\.)\s*(\d+))?", cleaned)
    if not m:
        return 1, 1
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else start
    return start, end


# Process sentences that code-review's Summary carries for the orchestrator
# (ring classification banner, Pre-Execution Security Gate status, citation
# audit tally, Packrat context status, flags in effect, trust/tier limit,
# clean check, challenge tally, and the closing Verdict label). They are parsed
# into structured fields and removed from the public overview. Each alternative
# is anchored to a sentence start and bounded at a sentence end, so prose such
# as "New Flags: `--dry-run` are wired" or text after the trust-limit sentence
# is left alone.
_NESTED_PARENS = r"\((?:[^()\n]|\([^()\n]*\))*\)"
_FLAG_TOKEN = (
    r"(?:`[^`\n]*`|--[\w-]+|\*\(none\)\*|\(none\)|\b(?:none|default|standard)\b)"
)
_SENTENCE_END = r"(?:\.(?=\s|$)|[ \t]*$)"
INTERNAL_SUMMARY_RE = re.compile(
    r"(?:^|(?<=[.!?;]))[ \t]*(?:"
    r"(?:🛡️\s*)?(?:Ring Classification|Review Level):[^\n]*?"
    + _SENTENCE_END
    + r"|(?i:citation audit):[^\n.]*?(?:dropped|verified|none required)(?:\s*"
    + _NESTED_PARENS
    + r")?\.?"
    + r"|(?i:packrat context):[^\n().]*(?:\s*"
    + _NESTED_PARENS
    + r")?"
    + _SENTENCE_END
    + r"|Flags(?:\s+in\s+effect)?:\s*"
    + _FLAG_TOKEN
    + r"(?:(?:\s*,\s*|\s+)(?:and\s+)?"
    + _FLAG_TOKEN
    + r")*"
    + r"(?:\s*"
    + _NESTED_PARENS
    + r")?(?:\*\*)?"
    + _SENTENCE_END
    + r"|Pre-Execution Security Gate:[^\n.]*?"
    + _SENTENCE_END
    + r"|Local execution blocked by Pre-Execution Security Gate[^\n.]*?"
    + _SENTENCE_END
    + r"|`--verify`(?: and (?i:tier) 1b)? (?:limited to|skipped)[^\n]*?"
    + _SENTENCE_END
    + r"|(?:Both reviewer checkouts|Reviewer checkout) passed the post-verification working-tree clean check(?:\s*"
    + _NESTED_PARENS
    + r")?\.?"
    + r"|\d+ disputed \([^)\n]*\)\.?"
    + r"|(?:\*\*)?(?i:(?:overall\s+)?verdict)(?:\*\*)?\s*:\s*(?:\*\*)?(?i:critical blockers?|minor revisions? needed|ready|approved?)(?:\*\*)?(?:\s*"
    + _NESTED_PARENS
    + r")?(?:\*\*)?\.?"
    + r")",
    re.MULTILINE,
)

FILE_LINK_RE = re.compile(r"\[([^\]]+)\]\(file://[^)\s]+\)")


def _strip_file_links(text: str) -> str:
    """Replaces local [label](file://...) links with peer-safe text or backticks."""

    def _repl(match: re.Match[str]) -> str:
        label = match.group(1)
        if label.startswith("`") and label.endswith("`"):
            return label
        if "`" not in label and re.match(r"^[\w./:#-]+$", label):
            return f"`{label}`"
        return label

    return FILE_LINK_RE.sub(_repl, text)


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
    m = re.search(
        r"[Cc]itation audit:\s*(\d+)\s+checked,\s*(\d+)\s+dropped", summary_text
    )
    if m:
        return {"checked": int(m.group(1)), "dropped": int(m.group(2))}
    return {"checked": 0, "dropped": 0}


def _parse_flags(summary_text: str) -> list[str]:
    m = re.search(r"[Ff]lags:\s*([^\n\.]+)", summary_text)
    if m:
        raw_flags = m.group(1)
        return list(dict.fromkeys(re.findall(r"--[a-z-]+", raw_flags)))
    return []


def _parse_focus_themes(summary_text: str) -> list[str]:
    matches = re.findall(r"`--focus\s+([a-z0-9][a-z0-9:-]*)[^`\n]*`", summary_text)
    return list(dict.fromkeys(matches))


def _parse_structural_assessment(structural_text: str) -> dict[str, str]:
    result = {}
    for line in structural_text.splitlines():
        m = re.match(r"^-\s+\*\*([^*]+)\*\*:\s*(.*)$", line.strip())
        if m:
            result[m.group(1).strip().lower()] = m.group(2).strip()
    return result


def _clean_overview(summary_text: str) -> str:
    """Strip process sentences and local file:// links paragraph by paragraph."""
    paragraphs = re.split(r"\n[ \t]*\n", summary_text)
    cleaned = (
        _strip_file_links(INTERNAL_SUMMARY_RE.sub("", p)).strip() for p in paragraphs
    )
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


def _build_details_block(
    evidence_tier: str,
    evidence_detail: str,
    details: str,
    repro_md: str,
    to_settle: str = "",
) -> str:
    """Returns a collapsed `<details>` block holding a comment's supporting detail.

    The block is omitted (returns "") when `evidence_detail`, `details`,
    `repro_md`, and `to_settle` are all empty. `evidence_tier` alone never
    produces a block, because every finding carries a tier. Parts are separated
    by blank lines so GitHub renders Markdown and code fences inside the block.
    """
    parts: list[str] = []
    if evidence_detail.strip():
        label = f"**Evidence** (`{evidence_tier}`)" if evidence_tier else "**Evidence**"
        parts.append(f"{label}: {evidence_detail.strip()}")
    if details.strip():
        parts.append(details.strip())
    if to_settle.strip():
        parts.append(f"**To settle**: {to_settle.strip()}")
    if repro_md.strip():
        parts.append(repro_md.strip())
    if not parts:
        return ""
    body = "\n\n".join(parts)
    return f"<details>\n<summary>Details</summary>\n\n{body}\n\n</details>"


def _build_finding_draft_comment(
    f: Finding, body: str, why: str, sugg: str, repro: str
) -> str:
    """Assembles the peer-facing draft_comment for a finding.

    Visible parts come first (prose, suggestion lead-in, suggestion block). The
    evidence detail, Details field, and repro test go in a trailing fold.
    """
    sugg_meta = f.fence("Suggestion")
    lead_in = _strip_file_links(sugg_meta.lead_in.strip())
    repro_meta = f.fence("Repro test")
    repro_lead_in = _strip_file_links(repro_meta.lead_in.strip())

    draft_parts: list[str] = []
    main_prose = _strip_file_links(_build_finding_prose(body, why))
    if main_prose:
        draft_parts.append(main_prose)
    if lead_in:
        draft_parts.append(lead_in)
    if sugg:
        draft_parts.append(_format_suggestion(sugg_meta, sugg))

    repro_md = ""
    if repro:
        heading = (
            f"**Repro test**: {repro_lead_in}" if repro_lead_in else "**Repro test**"
        )
        repro_md = f"{heading}\n\n{_format_repro_test(repro_meta, repro)}"
    ev = f.fields.get("Evidence", "")
    fold = _build_details_block(
        extract_tier(ev),
        _strip_file_links(extract_evidence_detail(ev)),
        _strip_file_links(f.fields.get("Details", "").strip()),
        repro_md,
    )
    if fold:
        draft_parts.append(fold)
    return "\n\n".join(draft_parts)


def _build_finding_dict(f: Finding) -> dict[str, Any]:
    """Converts a parsed Finding into its exported JSON dictionary."""
    line_start, line_end = _parse_line_range(f.fields.get("Line", "1"))
    ev = f.fields.get("Evidence", "")
    body = _strip_file_links(f.fields.get("Body", "").strip())
    why = _strip_file_links(f.fields.get("Why", "").strip())
    details = _strip_file_links(f.fields.get("Details", "").strip())
    sugg = f.fields.get("Suggestion", "").rstrip().lstrip("\n")
    repro = f.fields.get("Repro test", "").rstrip().lstrip("\n")
    desc = _strip_file_links(
        f.title or (body.split(".")[0].strip() + "." if body else f.id)
    )

    finding_dict: dict[str, Any] = {
        "id": f.id,
        "severity": f.severity or "medium",
        "file_path": strip_ticks(f.fields.get("File", "")),
        "line_start": line_start,
        "line_end": line_end,
        "side": "RIGHT",
        "evidence_tier": extract_tier(ev),
        "evidence_detail": _strip_file_links(extract_evidence_detail(ev)),
        "description": desc,
        "body": body,
        "why": why,
        "draft_comment": _build_finding_draft_comment(f, body, why, sugg, repro),
    }
    if details:
        finding_dict["details"] = details
    if "Category" in f.fields:
        finding_dict["category"] = f.fields["Category"].strip().lower()
    if "Focus" in f.fields:
        finding_dict["focus"] = strip_ticks(f.fields["Focus"]).strip()
    if sugg:
        finding_dict["suggestion"] = sugg
    if repro:
        finding_dict["repro_test"] = repro
    return finding_dict


def _build_question_dict(q: Finding) -> dict[str, Any]:
    """Converts a parsed Question Finding into its exported JSON dictionary."""
    line_start, line_end = _parse_line_range(q.fields.get("Line", "1"))
    ev = q.fields.get("Evidence", "")
    why = _strip_file_links(q.fields.get("Why", "").strip())
    to_settle = _strip_file_links(q.fields.get("To settle", "").strip())
    desc = _strip_file_links(q.title or q.fields.get("Body", "").strip())
    evidence_tier = extract_tier(ev)
    evidence_detail = _strip_file_links(extract_evidence_detail(ev))
    prose = _strip_file_links(_build_question_prose(q))
    fold = _build_details_block(evidence_tier, evidence_detail, "", "", to_settle)
    draft = f"{prose}\n\n{fold}" if fold and prose else (prose or fold)
    return {
        "id": q.id,
        "severity": "question",
        "file_path": strip_ticks(q.fields.get("File", "")),
        "line_start": line_start,
        "line_end": line_end,
        "side": "RIGHT",
        "evidence_tier": extract_tier(ev),
        "evidence_detail": _strip_file_links(extract_evidence_detail(ev)),
        "description": desc,
        "why": why,
        "to_settle": to_settle,
        "draft_comment": draft,
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
            "focus_themes": _parse_focus_themes(report.summary_text),
            "checked_clean": [_strip_file_links(cl.text) for cl in report.clean_lines],
        },
        "findings": findings_list,
        "questions": questions_list,
    }
    structural = {
        k: _strip_file_links(v)
        for k, v in _parse_structural_assessment(report.structural_text).items()
    }
    if structural:
        payload["review_summary"]["structural_assessment"] = structural
    if report.recommendations:
        payload["recommendations"] = [
            _strip_file_links(rec) for rec in report.recommendations
        ]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


class _CliUsageError(Exception):
    """Raised when CLI arguments or input paths fail validation."""


def _one_line_error(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def _build_parser() -> argparse.ArgumentParser:
    epilog = (
        "Grounding and mode combinations:\n"
        "  (no flags)                Structural, ID, evidence-tier, and prose checks; rejects ## Playbook Feedback.\n"
        "  --workspace DIR           Adds file existence, line bounds, and Evidence: Read quote checks against DIR.\n"
        "  --diff-dir DIR            Adds changed-line (+, deletion seam, or deleted old line) anchor checks.\n"
        "  --workspace + --diff-dir  Runs all file, bounds, changed-line, and quote checks (including deleted files/lines).\n"
        "  --worker [--playbook MD]  Allows ## Playbook Feedback; with --playbook, validates checked mode numbers.\n"
        "  --cluster A.md B.md ...   Clusters nearby same-file findings across 2+ reports and prints JSON to stdout."
    )
    parser = argparse.ArgumentParser(
        prog="lint_review.py",
        description="Lint a code-review review_results.md or cluster findings across worker drafts.",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "report",
        nargs="?",
        help="Path to review_results.md (required unless --cluster is set).",
    )
    parser.add_argument(
        "--comment-profile",
        choices=sorted(COMMENT_PROFILES),
        default="default",
        help="Comment prose caps: default or pr (both 120 words / 6 sentences).",
    )
    parser.add_argument(
        "--export-json", help="Write review_results.json here on success."
    )
    parser.add_argument(
        "--workspace",
        help="Path to the reviewed checkout directory (checks file existence, line bounds, and Read quotes).",
    )
    parser.add_argument(
        "--diff-dir",
        help="Path to staged diff directory containing diff_lines.jsonl (checks changed-line anchors).",
    )
    parser.add_argument(
        "--worker",
        action="store_true",
        help="Lint a worker draft (allows ## Playbook Feedback; other rules match final reports).",
    )
    parser.add_argument(
        "--playbook",
        help="Path to the assigned focus playbook markdown file (validates mode numbers under --worker).",
    )
    parser.add_argument(
        "--cluster",
        nargs="*",
        metavar="REPORT_MD",
        help="Cluster same-file findings across two or more report files and print JSON to stdout.",
    )
    return parser


def _read_utf8_file(raw_path: str, label: str = "file") -> tuple[pathlib.Path, str]:
    if not raw_path:
        raise _CliUsageError(f"{label} path must not be empty.")
    path = pathlib.Path(raw_path)
    if not path.is_file():
        raise _CliUsageError(f"file not found: {path}")
    try:
        return path, path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _CliUsageError(f"cannot read {path}: {_one_line_error(exc)}") from exc


def _validate_workspace(raw: str | None) -> pathlib.Path | None:
    if raw is None:
        return None
    if not raw:
        raise _CliUsageError("--workspace path must not be empty.")
    path = pathlib.Path(raw)
    if not path.is_dir():
        raise _CliUsageError(f"--workspace is not a directory: {path}")
    return path


def _validate_diff_index(raw: str | None) -> StagedDiffIndex | None:
    if raw is None:
        return None
    if not raw:
        raise _CliUsageError("--diff-dir path must not be empty.")
    path = pathlib.Path(raw)
    if not path.is_dir():
        raise _CliUsageError(f"--diff-dir is not a directory: {path}")
    try:
        return load_staged_diff_index(path)
    except (OSError, UnicodeError, ValueError) as exc:
        raise _CliUsageError(
            f"invalid --diff-dir '{path}': {_one_line_error(exc)}"
        ) from exc


def _validate_playbook(raw: str | None) -> pathlib.Path | None:
    if raw is None:
        return None
    if not raw:
        raise _CliUsageError("--playbook path must not be empty.")
    path = pathlib.Path(raw)
    if not path.is_file():
        raise _CliUsageError(f"--playbook is not a readable file: {path}")
    try:
        path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _CliUsageError(
            f"cannot read --playbook '{path}': {_one_line_error(exc)}"
        ) from exc
    return path


def _cluster_reviewer_labels(paths: Sequence[pathlib.Path]) -> list[str]:
    stems = [path.stem for path in paths]
    if len(set(stems)) == len(stems):
        return stems
    return [str(path) for path in paths]


def _run_cluster(args: argparse.Namespace) -> int:
    if args.report is not None:
        raise _CliUsageError(
            "--cluster does not accept a positional report; pass all files after --cluster."
        )
    if args.worker or any(
        val is not None
        for val in (args.export_json, args.workspace, args.diff_dir, args.playbook)
    ):
        raise _CliUsageError(
            "--cluster prints JSON to stdout and cannot be combined with "
            "--workspace, --diff-dir, --worker, --playbook, or --export-json."
        )
    raw_paths = args.cluster or []
    if len(raw_paths) < 2:
        raise _CliUsageError("--cluster requires at least 2 report files.")

    parsed_reports: list[tuple[pathlib.Path, Report]] = []
    for raw_path in raw_paths:
        path, raw_text = _read_utf8_file(raw_path)
        parsed_reports.append((path, parse_report(raw_text)))

    labels = _cluster_reviewer_labels([path for path, _ in parsed_reports])
    reports = [
        (label, report.findings + report.questions)
        for label, (_, report) in zip(labels, parsed_reports, strict=True)
    ]
    print(json.dumps(cluster_findings(reports), indent=2))
    return 0


def _run_lint(args: argparse.Namespace) -> int:
    if args.report is None:
        raise _CliUsageError(
            "missing required report path (or pass --cluster with 2+ report files)."
        )
    _, raw_text = _read_utf8_file(args.report)
    workspace_path = _validate_workspace(args.workspace)
    diff_index = _validate_diff_index(args.diff_dir)
    playbook_path = _validate_playbook(args.playbook)

    report = parse_report(raw_text)
    try:
        violations = check_report(
            report,
            profile=args.comment_profile,
            workspace=workspace_path,
            diff_index=diff_index,
            worker_mode=args.worker,
            playbook_path=playbook_path,
        )
    except (OSError, UnicodeError) as exc:
        raise _CliUsageError(_one_line_error(exc)) from exc

    for v in violations:
        print(f"{v.line}: {v.rule}: {v.message}")
        if v.fix:
            print(f"Fix: {v.fix}")

    if violations:
        return 1

    if args.export_json:
        try:
            export_report_to_json(report, pathlib.Path(args.export_json))
        except OSError as exc:
            raise _CliUsageError(
                f"cannot write --export-json '{args.export_json}': {_one_line_error(exc)}"
            ) from exc

    return 0


def main(argv: list[str] | None = None) -> int:
    """Runs report linting or multi-report finding clustering from CLI arguments."""
    parser = _build_parser()
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return 2 if exc.code else 0

    try:
        if args.cluster is not None:
            return _run_cluster(args)
        return _run_lint(args)
    except _CliUsageError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
