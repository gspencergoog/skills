#!/usr/bin/env python3
"""plan_model.py - Parses implementation plan Markdown into a structured model.

The model is shared by `validate_plan.py` (brief and question checks) and
`plan_surfaces.py` (post-plan A2UI worksheets and the `plan_overview` pane).
It understands these plan conventions:

- `## At a glance` with `- **Field:** value` bullets.
- A pipe table under a `PRs`, `Commits`, or `PR plan` heading.
- `### Q<n>: <title>` questions under `## Open Questions`, each followed by
  option bullets such as `- (Recommended) A: Text. Consequence: ...`.
- Bullets under `## Decisions`.
- `### Wave <n>: <title> [Sequential|Parallel]` headings, `#### Track <id>`
  subheadings, and inline `[Parallel: Wave N]` tags on Phase or Track headings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator

HEADING_RE = re.compile(r"^\s*(?P<hashes>#{1,6})\s+(?P<text>.+?)\s*$")
WAVE_HEADING_RE = re.compile(
    r"^Wave\s+(?P<wave_id>[\w.-]+)\b\s*:?\s*(?P<rest>.*)$", re.IGNORECASE
)
TRACK_HEADING_RE = re.compile(r"^(?P<label>Track\s+[\w.-]+)\b", re.IGNORECASE)
INLINE_PARALLEL_WAVE_RE = re.compile(
    r"^(?P<label>(?:Phase|Track)\s+[\w.-]+)\b.*?\[Parallel:\s*(?:Wave\s*)?(?P<wave_id>[\w.-]+)\]",
    re.IGNORECASE,
)
FIELD_BULLET_RE = re.compile(
    r"^\s*[-*+]\s+\*\*(?P<name>[^*]+?)\s*:?\s*\*\*\s*:?\s*(?P<value>.*)$"
)
QUESTION_HEADING_RE = re.compile(r"^(?P<qid>Q\d+)\s*[:.)-]\s*(?P<title>.+)$", re.IGNORECASE)
OPTION_RE = re.compile(
    r"^\s*[-*+]\s+(?P<rec>\(Recommended\)\s+)?(?P<letter>[A-Z])\s*:\s*(?P<body>.+?)\s*$"
)
CONSEQUENCE_SPLIT_RE = re.compile(r"\s+Consequences?:\s*", re.IGNORECASE)
BULLET_RE = re.compile(r"^\s*[-*+]\s+(?P<text>.+?)\s*$")
MODEL_TIER_RE = re.compile(
    r"^\s*[-*+]?\s*\*\*Model tier\*\*\s*:?\s*(?P<tier>.+?)\s*$", re.IGNORECASE
)
TRACK_VERIFICATION_RE = re.compile(
    r"\*\*(?:track|phase)\b[^*\n]*verification\b[^*\n]*\*\*\s*:?\s*(?P<cmd>.*)$",
    re.IGNORECASE,
)
FILE_ACTION_RE = re.compile(
    r"\[(?:NEW|MODIFY|DELETE|RENAME)\]\s+(?:\[(?P<label>[^\]]+)\]\((?P<link>[^\)]+)\)|`?(?P<path>[^\s`\)]+)`?)",
    re.IGNORECASE,
)
TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
PR_HEADING_RE = re.compile(r"\b(PRs?|Commits|PR plan)\b", re.IGNORECASE)
NO_QUESTIONS_RE = re.compile(r"none currently blocking", re.IGNORECASE)

FULL_GLANCE_FIELDS = (
    "goal",
    "shape",
    "decisions needed from you",
    "biggest risk",
    "not doing",
)
LIGHTWEIGHT_GLANCE_FIELDS = ("goal", "decisions needed from you", "not doing")


@dataclass(frozen=True)
class Section:
    """A Markdown heading and the 1-based line span it covers (inclusive)."""

    level: int
    heading: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class QuestionOption:
    letter: str
    text: str
    consequence: str
    recommended: bool


@dataclass(frozen=True)
class OpenQuestion:
    qid: str
    title: str
    options: tuple[QuestionOption, ...]
    line: int


@dataclass(frozen=True)
class WaveInfo:
    wave_id: str
    title: str
    mode: str  # "Sequential", "Parallel", or "" when unstated
    model_tier: str | None
    line: int


@dataclass
class TrackInfo:
    wave_id: str
    track_id: str
    model_tier: str | None = None
    files: list[str] = field(default_factory=list)
    verification: str | None = None
    line: int = 0
    title: str = ""

@dataclass(frozen=True)
class PlanDocument:
    title: str
    sections: tuple[Section, ...]
    at_a_glance: dict[str, str] | None
    glance_bullet_text: str
    pr_rows: tuple[dict[str, str], ...]
    questions: tuple[OpenQuestion, ...]
    free_question_bullets: tuple[str, ...]
    no_questions_marker: bool
    decisions: tuple[str, ...]
    waves: tuple[WaveInfo, ...]
    tracks: tuple[TrackInfo, ...]
    file_action_count: int

    @property
    def is_lightweight(self) -> bool:
        """Whether the plan is a lightweight plan (no waves, at most 2 file actions)."""
        return not self.waves and self.file_action_count <= 2

    @property
    def parallel_waves(self) -> tuple[WaveInfo, ...]:
        return tuple(w for w in self.waves if w.mode == "Parallel")


def iter_non_fenced_lines(lines: list[str]) -> Iterator[tuple[int, str]]:
    """Yields (1-based line number, line) for lines outside fenced code blocks."""
    in_block = False
    fence_char = ""
    fence_len = 0
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            char = stripped[0]
            length = len(stripped) - len(stripped.lstrip(char))
            if not in_block:
                in_block, fence_char, fence_len = True, char, length
            elif char == fence_char and length >= fence_len:
                in_block = False
            continue
        if not in_block:
            yield i, line


def _parse_sections(lines: list[str]) -> tuple[str, tuple[Section, ...]]:
    headings: list[tuple[int, int, str]] = []
    for i, line in iter_non_fenced_lines(lines):
        m = HEADING_RE.match(line)
        if m:
            headings.append((i, len(m.group("hashes")), m.group("text")))

    title = next((text for _, level, text in headings if level == 1), "")
    sections: list[Section] = []
    for idx, (start, level, text) in enumerate(headings):
        end = len(lines)
        for later_start, later_level, _ in headings[idx + 1 :]:
            if later_level <= level:
                end = later_start - 1
                break
        sections.append(Section(level, text, start, end))
    return title, tuple(sections)


def find_section(
    sections: tuple[Section, ...], heading_regex: str, level: int | None = None
) -> Section | None:
    """Returns the first section whose heading matches `heading_regex`."""
    regex = re.compile(heading_regex, re.IGNORECASE)
    for section in sections:
        if level is not None and section.level != level:
            continue
        if regex.search(section.heading):
            return section
    return None


def _section_lines(
    lines: list[str], section: Section | None, stop_at_subheading: bool = False
) -> list[tuple[int, str]]:
    """Returns non-fenced body lines of a section (excluding its heading)."""
    if section is None:
        return []
    body: list[tuple[int, str]] = []
    for i, line in iter_non_fenced_lines(lines):
        if i <= section.start_line or i > section.end_line:
            continue
        if stop_at_subheading and HEADING_RE.match(line):
            break
        body.append((i, line))
    return body


def section_text(text: str, doc: PlanDocument, heading_regex: str) -> str:
    """Returns the raw text of the first matching section, including fenced blocks."""
    section = find_section(doc.sections, heading_regex)
    if section is None:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[section.start_line : section.end_line])


def _parse_glance(
    lines: list[str], sections: tuple[Section, ...]
) -> tuple[dict[str, str] | None, str]:
    section = find_section(sections, r"^at a glance\b", level=2)
    if section is None:
        return None, ""
    fields: dict[str, str] = {}
    bullet_texts: list[str] = []
    for _, line in _section_lines(lines, section, stop_at_subheading=True):
        m = FIELD_BULLET_RE.match(line)
        if m:
            name = m.group("name").strip().rstrip(":").lower()
            fields[name] = m.group("value").strip()
            bullet_texts.append(f"{m.group('name')} {m.group('value')}")
    return fields, "\n".join(bullet_texts)


def _split_row(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [cell.strip() for cell in stripped.split("|")]


def _parse_pr_rows(
    lines: list[str], sections: tuple[Section, ...]
) -> tuple[dict[str, str], ...]:
    for section in sections:
        if section.level < 2 or not PR_HEADING_RE.search(section.heading):
            continue
        body = [line for _, line in _section_lines(lines, section, stop_at_subheading=True)]
        rows = _first_table(body)
        if rows:
            return rows
    return ()


def _first_table(body: list[str]) -> tuple[dict[str, str], ...]:
    for idx in range(len(body) - 1):
        header, separator = body[idx], body[idx + 1]
        if "|" not in header or not TABLE_SEPARATOR_RE.match(separator):
            continue
        keys = _split_row(header)
        rows: list[dict[str, str]] = []
        for line in body[idx + 2 :]:
            if "|" not in line:
                break
            rows.append(dict(zip(keys, _split_row(line))))
        return tuple(rows)
    return ()


def _parse_option(line: str) -> QuestionOption | None:
    m = OPTION_RE.match(line)
    if not m:
        return None
    parts = CONSEQUENCE_SPLIT_RE.split(m.group("body"), maxsplit=1)
    consequence = parts[1].strip() if len(parts) > 1 else ""
    return QuestionOption(
        letter=m.group("letter"),
        text=parts[0].strip(),
        consequence=consequence,
        recommended=bool(m.group("rec")),
    )


def _parse_questions(
    lines: list[str], sections: tuple[Section, ...]
) -> tuple[tuple[OpenQuestion, ...], tuple[str, ...], bool]:
    section = find_section(sections, r"^open questions\b", level=2)
    if section is None:
        return (), (), False

    questions: list[OpenQuestion] = []
    free_bullets: list[str] = []
    marker = False
    current: tuple[str, str, int] | None = None
    options: list[QuestionOption] = []

    def flush() -> None:
        if current is not None:
            qid, title, line_num = current
            questions.append(OpenQuestion(qid, title, tuple(options), line_num))

    for i, line in _section_lines(lines, section):
        if NO_QUESTIONS_RE.search(line):
            marker = True
        heading = HEADING_RE.match(line)
        if heading:
            flush()
            options = []
            qm = QUESTION_HEADING_RE.match(heading.group("text"))
            current = (qm.group("qid").upper(), qm.group("title").strip(), i) if qm else None
            continue
        if current is not None:
            option = _parse_option(line)
            if option:
                options.append(option)
            continue
        bullet = BULLET_RE.match(line)
        if bullet:
            free_bullets.append(bullet.group("text"))
    flush()
    return tuple(questions), tuple(free_bullets), marker


def _parse_decisions(
    lines: list[str], sections: tuple[Section, ...]
) -> tuple[str, ...]:
    section = find_section(sections, r"^decisions\b", level=2)
    out: list[str] = []
    for _, line in _section_lines(lines, section):
        bullet = BULLET_RE.match(line)
        if bullet:
            out.append(bullet.group("text"))
    return tuple(out)


@dataclass
class _WaveState:
    waves: list[WaveInfo] = field(default_factory=list)
    tracks: list[TrackInfo] = field(default_factory=list)
    wave_id: str | None = None
    wave_level: int = 0
    wave_index: int = -1
    track: TrackInfo | None = None
    file_actions: int = 0


def _wave_mode(rest: str) -> str:
    lowered = rest.lower()
    if "[parallel" in lowered:
        return "Parallel"
    if "[sequential" in lowered:
        return "Sequential"
    return ""


def _strip_mode_tag(rest: str) -> str:
    return re.sub(r"\s*\[(?:Parallel|Sequential)[^\]]*\]\s*$", "", rest, flags=re.IGNORECASE).strip()


def _heading_title(text: str) -> str:
    """Returns the part of a Track or Phase heading after the first colon."""
    _, sep, rest = text.partition(":")
    return _strip_mode_tag(rest) if sep else ""


def _handle_heading(state: _WaveState, line_num: int, level: int, text: str) -> None:
    if state.wave_id is not None and level <= state.wave_level:
        state.wave_id, state.track = None, None

    inline = INLINE_PARALLEL_WAVE_RE.match(text)
    if inline:
        state.track = TrackInfo(
            inline.group("wave_id"), inline.group("label"), line=line_num, title=_heading_title(text)
        )
        state.tracks.append(state.track)
        return

    wave = WAVE_HEADING_RE.match(text)
    if wave:
        rest = wave.group("rest") or ""
        info = WaveInfo(wave.group("wave_id"), _strip_mode_tag(rest), _wave_mode(rest), None, line_num)
        state.waves.append(info)
        state.wave_index = len(state.waves) - 1
        state.wave_id, state.wave_level, state.track = info.wave_id, level, None
        return

    track = TRACK_HEADING_RE.match(text)
    if track and state.wave_id is not None:
        state.track = TrackInfo(
            state.wave_id, track.group("label"), line=line_num, title=_heading_title(text)
        )
        state.tracks.append(state.track)
        return

    if state.track is not None and level <= 4:
        state.track = None


def _set_model_tier(state: _WaveState, tier: str) -> None:
    if state.track is not None:
        state.track.model_tier = tier
    elif state.wave_id is not None and state.wave_index >= 0:
        wave = state.waves[state.wave_index]
        state.waves[state.wave_index] = WaveInfo(
            wave.wave_id, wave.title, wave.mode, tier, wave.line
        )


def _handle_body_line(state: _WaveState, line: str) -> None:
    actions = list(FILE_ACTION_RE.finditer(line))
    state.file_actions += len(actions)

    tier = MODEL_TIER_RE.match(line)
    if tier:
        _set_model_tier(state, tier.group("tier"))
        return

    if state.track is None:
        return
    verification = TRACK_VERIFICATION_RE.search(line)
    if verification:
        state.track.verification = verification.group("cmd").strip().strip("`") or None
    for m in actions:
        path = m.group("link") or m.group("path") or m.group("label") or ""
        path = path.removeprefix("file://").split("#")[0].strip()
        if path and not path.startswith(("http://", "https://")):
            state.track.files.append(path)


def _parse_waves(lines: list[str]) -> _WaveState:
    state = _WaveState()
    for i, line in iter_non_fenced_lines(lines):
        heading = HEADING_RE.match(line)
        if heading:
            _handle_heading(state, i, len(heading.group("hashes")), heading.group("text"))
        else:
            _handle_body_line(state, line)
    return state


def parse_plan(text: str) -> PlanDocument:
    """Parses implementation plan Markdown into a `PlanDocument`."""
    lines = text.splitlines()
    title, sections = _parse_sections(lines)
    glance, glance_text = _parse_glance(lines, sections)
    questions, free_bullets, marker = _parse_questions(lines, sections)
    wave_state = _parse_waves(lines)
    return PlanDocument(
        title=title,
        sections=sections,
        at_a_glance=glance,
        glance_bullet_text=glance_text,
        pr_rows=_parse_pr_rows(lines, sections),
        questions=questions,
        free_question_bullets=free_bullets,
        no_questions_marker=marker,
        decisions=_parse_decisions(lines, sections),
        waves=tuple(wave_state.waves),
        tracks=tuple(wave_state.tracks),
        file_action_count=wave_state.file_actions,
    )


_STRIP_PATTERNS = (
    (re.compile(r"```.*?```", re.DOTALL), " "),
    (re.compile(r"~~~.*?~~~", re.DOTALL), " "),
    (re.compile(r"^\s*\|.*$", re.MULTILINE), " "),
    (re.compile(r"`[^`\n]*`"), "code"),
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),
    (re.compile(r"^\s*#{1,6}\s+.*$", re.MULTILINE), " "),
    (re.compile(r"^\s*>\s*\[![A-Z]+\]\s*$", re.MULTILINE), " "),
    (re.compile(r"[*_>#]"), " "),
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n\s*\n|\n\s*[-+]\s+|\n\s*\d+\.\s+")


def sentence_stats(markdown: str) -> tuple[float, int]:
    """Returns (mean words per sentence, max words) for prose in `markdown`.

    Fenced blocks, tables, and headings are removed; inline code counts as one
    word; fragments of 3 or fewer words are ignored. Returns (0.0, 0) when no
    sentence remains.
    """
    text = "\n" + markdown
    for pattern, replacement in _STRIP_PATTERNS:
        text = pattern.sub(replacement, text)
    counts = [
        len(chunk.split())
        for chunk in _SENTENCE_SPLIT_RE.split(text)
        if len(chunk.split()) > 3
    ]
    if not counts:
        return 0.0, 0
    return sum(counts) / len(counts), max(counts)
