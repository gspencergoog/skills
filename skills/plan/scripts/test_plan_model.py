#!/usr/bin/env python3
"""test_plan_model.py - Unit tests for plan_model.py"""

import unittest

from plan_model import (
    FULL_GLANCE_FIELDS,
    ShellSegment,
    extract_command_text,
    find_section,
    parse_plan,
    section_text,
    sentence_stats,
    split_shell_segments,
)

FULL_PLAN = """# Improve the widget

## At a glance

- **Goal:** Make widgets faster.
- **Shape:** 3 waves, 2 parallel tracks, 2 commits.
- **Decisions needed from you:** 1 (Q1).
- **Biggest risk:** Cache invalidation.
- **Not doing:** No UI changes.

```mermaid
flowchart LR
    A --> B
```

### Commits

| # | Commit | Wave |
| ---: | :--- | :--- |
| 1 | `feat: add cache` | 1 |
| 2 | `feat: wire cache` | 2 |

## User Review Required

> [!IMPORTANT]
> The cache is on by default.

## Open Questions

### Q1: Should the cache be on by default?
- (Recommended) A: Yes. Consequence: faster first load.
- B: No. Consequence: users opt in.

```markdown
### Q9: This question is inside a fence.
- (Recommended) A: Ignored.
```

## Decisions

- **Q0 (2026-11-04):** A. Use the existing store.

## Phased Implementation Plan

### Wave 1: Foundation [Sequential]

- **Model tier**: Claude Fable 5.1 High

- [ ] [NEW] [cache.py](file:///repo/cache.py)

### Wave 2: Core [Parallel]

#### Track 2A: Reader

- **Model tier**: Gemini Pro Next
- [ ] [MODIFY] [reader.py](file:///repo/reader.py)

**Track 2A Verification**: `pytest tests/test_reader.py`

#### Track 2B: Writer

- [ ] [MODIFY] [writer.py](file:///repo/writer.py)

### Wave 3: Wire up [Sequential]

- [ ] [MODIFY] [index.py](file:///repo/index.py)

## Verification Plan

- `pytest`
"""


class ParsePlanTest(unittest.TestCase):
    def setUp(self):
        self.doc = parse_plan(FULL_PLAN)

    def test_title_and_sections(self):
        self.assertEqual(self.doc.title, "Improve the widget")
        self.assertIsNotNone(find_section(self.doc.sections, r"^at a glance", level=2))
        self.assertEqual(self.doc.sections[1].heading, "At a glance")

    def test_at_a_glance_fields(self):
        glance = self.doc.at_a_glance
        self.assertIsNotNone(glance)
        for name in FULL_GLANCE_FIELDS:
            self.assertIn(name, glance)
        self.assertEqual(glance["goal"], "Make widgets faster.")
        self.assertNotIn("flowchart", self.doc.glance_bullet_text)
        self.assertNotIn("feat: add cache", self.doc.glance_bullet_text)

    def test_pr_rows_from_commits_table(self):
        self.assertEqual(len(self.doc.pr_rows), 2)
        self.assertEqual(self.doc.pr_rows[0]["Commit"], "`feat: add cache`")

    def test_questions_skip_fenced(self):
        self.assertEqual([q.qid for q in self.doc.questions], ["Q1"])
        q1 = self.doc.questions[0]
        self.assertEqual(q1.title, "Should the cache be on by default?")
        self.assertEqual([o.letter for o in q1.options], ["A", "B"])
        self.assertTrue(q1.options[0].recommended)
        self.assertFalse(q1.options[1].recommended)
        self.assertEqual(q1.options[0].text, "Yes.")
        self.assertEqual(q1.options[0].consequence, "faster first load.")

    def test_decisions(self):
        self.assertEqual(len(self.doc.decisions), 1)
        self.assertIn("Q0", self.doc.decisions[0])

    def test_waves_and_tracks(self):
        self.assertEqual([w.wave_id for w in self.doc.waves], ["1", "2", "3"])
        self.assertEqual(
            [w.mode for w in self.doc.waves], ["Sequential", "Parallel", "Sequential"]
        )
        self.assertEqual(self.doc.waves[0].title, "Foundation")
        self.assertEqual(self.doc.waves[0].model_tier, "Claude Fable 5.1 High")
        self.assertEqual(
            [t.track_id for t in self.doc.tracks], ["Track 2A", "Track 2B"]
        )
        track_a, track_b = self.doc.tracks
        self.assertEqual(track_a.model_tier, "Gemini Pro Next")
        self.assertEqual(track_a.files, ["/repo/reader.py"])
        self.assertEqual(track_a.verification, "pytest tests/test_reader.py")
        self.assertIsNone(track_b.model_tier)
        self.assertIsNone(track_b.verification)
        self.assertEqual(len(self.doc.parallel_waves), 1)
        self.assertFalse(self.doc.is_lightweight)

    def test_section_text(self):
        text = section_text(FULL_PLAN, self.doc, r"^user review required")
        self.assertIn("The cache is on by default.", text)
        self.assertNotIn("Open Questions", text)


class EdgeCaseTest(unittest.TestCase):
    def test_missing_glance(self):
        doc = parse_plan("# T\n\n## User Review Required\nNone.\n")
        self.assertIsNone(doc.at_a_glance)
        self.assertEqual(doc.glance_bullet_text, "")

    def test_lightweight_plan(self):
        doc = parse_plan(
            "# Fix\n\n## At a glance\n\n- **Goal:** Fix it.\n\n## Proposed Changes\n\n"
            "- [ ] [MODIFY] [a.py](file:///a.py)\n"
        )
        self.assertTrue(doc.is_lightweight)
        self.assertEqual(doc.file_action_count, 1)

    def test_free_form_questions_and_marker(self):
        doc = parse_plan("# T\n\n## Open Questions\n\n- Should we do X?\n")
        self.assertEqual(doc.free_question_bullets, ("Should we do X?",))
        self.assertFalse(doc.no_questions_marker)
        doc = parse_plan("# T\n\n## Open Questions\n\nNone currently blocking.\n")
        self.assertTrue(doc.no_questions_marker)
        self.assertEqual(doc.questions, ())

    def test_question_without_recommendation(self):
        doc = parse_plan(
            "# T\n\n## Open Questions\n\n### Q3: Pick one\n- A: This.\n- B: That.\n"
        )
        self.assertEqual(len(doc.questions), 1)
        self.assertFalse(any(o.recommended for o in doc.questions[0].options))

    def test_inline_parallel_tracks(self):
        doc = parse_plan(
            "# T\n\n## Phases\n\n### Phase 2A: Core [Parallel: Wave 2]\n\n"
            "- **Model tier**: Gemini Pro Next\n- [ ] [MODIFY] [a.py](file:///a.py)\n\n"
            "### Phase 2B: Other [Parallel: Wave 2]\n\n- [ ] [MODIFY] [b.py](file:///b.py)\n"
        )
        self.assertEqual([t.track_id for t in doc.tracks], ["Phase 2A", "Phase 2B"])
        self.assertEqual(doc.tracks[0].model_tier, "Gemini Pro Next")
        self.assertIsNone(doc.tracks[1].model_tier)
        self.assertEqual(doc.tracks[0].wave_id, "2")

    def test_pr_table_under_prs_heading(self):
        doc = parse_plan("# T\n\n### PRs\n\n| PR | Title |\n| --- | --- |\n| 1 | a |\n")
        self.assertEqual(doc.pr_rows, ({"PR": "1", "Title": "a"},))

    def test_wave_has_verifier_preserves_mode_and_model_tier(self):
        doc = parse_plan(
            "# T\n\n## Phases\n\n"
            "### Wave 1: Foundation [Sequential]\n\n"
            "- **Model tier**: Gemini Pro Next\n"
            "- [ ] [NEW] [a.py](file:///a.py)\n\n"
            "### Wave 2: Core [Parallel]\n\n"
            "#### Track 2A: Reader\n\n"
            "- **Model tier**: Gemini Pro Next\n"
            "- [ ] [MODIFY] [b.py](file:///b.py)\n\n"
            "**Wave 2 Verifier & Barrier Gate**:\n"
            "- Peer `verifier` runs Track 2A tests.\n\n"
            "### Wave 3: Lite [Parallel]\n\n"
            "#### Track 3A: Docs\n\n"
            "- **Model tier**: Gemini 3.8 Flash High\n"
            "- [ ] [MODIFY] [c.md](file:///c.md)\n"
        )
        self.assertEqual(
            [(w.wave_id, w.mode, w.model_tier, w.has_verifier) for w in doc.waves],
            [
                ("1", "Sequential", "Gemini Pro Next", False),
                ("2", "Parallel", None, True),
                ("3", "Parallel", None, False),
            ],
        )

    def test_pr_table_accepts_gfm_narrow_separators(self):
        doc = parse_plan(
            "# T\n\n### PRs\n\n| # | Title | Wave |\n| --: | - | :-- |\n| 1 | a | 1 |\n"
        )
        self.assertEqual(doc.pr_rows, ({"#": "1", "Title": "a", "Wave": "1"},))

    def test_track_verification_heading_not_parsed_as_parallel_track(self):
        doc = parse_plan(
            "# T\n\n## Phased Implementation Plan\n\n"
            "### Wave 1: Core [Parallel]\n\n"
            "#### Track 1A: Parser\n\n"
            "- **Model tier:** top-tier\n"
            "- [ ] [MODIFY] [a.py](file:///a.py)\n\n"
            "##### Track Verification\n\n"
            "- Run `pytest tests/test_a.py`\n\n"
            "#### Track 1B: CLI\n\n"
            "- **Model tier**: parallel author pool\n"
            "- [ ] [MODIFY] [b.py](file:///b.py)\n\n"
            "#### Track 1B Verification\n\n"
            "- Run `pytest tests/test_b.py`\n"
        )
        self.assertEqual([t.track_id for t in doc.tracks], ["Track 1A", "Track 1B"])
        self.assertEqual(doc.tracks[0].model_tier, "top-tier")
        self.assertTrue(doc.tracks[0].has_verification)
        self.assertTrue(doc.tracks[1].has_verification)
        self.assertEqual(doc.tracks[0].heading_level, 4)

    def test_verification_variants_and_multiline(self):
        doc = parse_plan(
            "# T\n\n## Phased Implementation Plan\n\n"
            "### Wave 2: Core [Parallel]\n\n"
            "#### Track A: Alpha\n"
            "- **Model tier**: top-tier\n"
            "- [ ] [NEW] [a.py](file:///a.py)\n"
            "- **Verification**: `pytest tests/test_a.py`\n\n"
            "#### Track B: Beta\n"
            "- **Model tier**: top-tier\n"
            "- [ ] [NEW] [b.py](file:///b.py)\n"
            "**Track B Verification**:\n"
            "- Run `pytest tests/test_b.py`\n"
        )
        self.assertEqual([t.track_id for t in doc.tracks], ["Track A", "Track B"])
        self.assertTrue(doc.tracks[0].has_verification)
        self.assertEqual(doc.tracks[0].verification, "pytest tests/test_a.py")
        self.assertTrue(doc.tracks[1].has_verification)
        self.assertIsNone(doc.tracks[1].verification)

    def test_file_actions_list_scoped_and_wave_tracked(self):
        doc = parse_plan(
            "# T\n\n## Phased Implementation Plan\n\n"
            "### Wave 1: Base [Sequential]\n\n"
            "- [ ] [NEW] [lib/base.py](file:///repo/lib/base.py)\n"
            "- Mentioning [MODIFY] foo in prose is ignored.\n\n"
            "### Phase 2A: Reader [Parallel: Wave 2]\n\n"
            "- **Model tier**: top-tier\n"
            "- [ ] [MODIFY] [lib/base.py](file:///repo/lib/base.py)\n"
        )
        self.assertEqual(doc.file_action_count, 2)
        self.assertEqual(
            [(a.action, a.raw_path, a.wave_id, a.track_id) for a in doc.file_actions],
            [
                ("NEW", "/repo/lib/base.py", "1", None),
                ("MODIFY", "/repo/lib/base.py", "2", "Phase 2A"),
            ],
        )
        self.assertEqual(len(doc.parallel_tracks), 1)
        self.assertEqual(doc.parallel_tracks[0].track_id, "Phase 2A")

    def test_sequential_wave_tracks_excluded_from_parallel_tracks(self):
        doc = parse_plan(
            "# T\n\n## Phased Implementation Plan\n\n"
            "### Wave 1: Base [Sequential]\n\n"
            "#### Track 1A: Substep\n"
            "- [ ] [NEW] [a.py](file:///a.py)\n"
        )
        self.assertEqual(len(doc.tracks), 1)
        self.assertEqual(doc.parallel_tracks, ())

    def test_verification_heading_does_not_swallow_sibling_or_top_section(self):
        doc = parse_plan(
            "# T\n\n## Phased Implementation Plan\n\n"
            "### Wave 2: Core [Parallel]\n\n"
            "#### Track 2A: Parser\n"
            "- **Model tier**: top-tier\n"
            "- [ ] [MODIFY] [a.py](file:///a.py)\n\n"
            "#### Track 2B: Plan Verification\n"
            "- **Model tier**: top-tier\n"
            "- [ ] [MODIFY] [b.py](file:///b.py)\n\n"
            "## Verification Plan\n\n"
            "- Run `pytest`\n"
        )
        self.assertEqual([t.track_id for t in doc.tracks], ["Track 2A", "Track 2B"])
        self.assertEqual(doc.tracks[0].files, ["/a.py"])
        self.assertEqual(doc.tracks[1].files, ["/b.py"])
        self.assertFalse(doc.tracks[0].has_verification)
        self.assertFalse(doc.tracks[1].has_verification)

    def test_wave_verification_barrier_commit_handoff_and_track_interfaces(self):
        doc = parse_plan(
            "# T\n\n## Phased Implementation Plan\n\n"
            "### Wave 1: Base [Sequential]\n\n"
            "Commit Wave 1 before spawning Wave 2 share workers.\n\n"
            "- [ ] [NEW] [base.py](file:///base.py)\n\n"
            "**Wave 1 Verification**: Run `pytest tests/test_base.py` to check.\n\n"
            "### Wave 2: Core [Parallel]\n\n"
            "#### Track 2A: Reader\n"
            "- **Model tier**: parallel author pool\n"
            "- **Consumes**: `Wave 1: BaseModel`\n"
            "- **Produces**: `parse_reader(text: str) -> BaseModel`\n"
            "- [ ] [NEW] [reader.py](file:///reader.py)\n"
            "**Track 2A Verification**: `pytest tests/test_reader.py`\n\n"
            "**Wave 2 Verifier & Barrier Gate**:\n"
            "- Merge Track 2A back before Wave 3.\n"
        )
        self.assertEqual(doc.waves[0].verification, "pytest tests/test_base.py")
        self.assertTrue(doc.waves[0].mentions_commit_handoff)
        self.assertFalse(doc.waves[0].has_barrier)
        self.assertTrue(doc.waves[1].has_barrier)
        self.assertTrue(doc.waves[1].has_verifier)
        self.assertEqual(doc.tracks[0].consumes, "`Wave 1: BaseModel`")
        self.assertEqual(
            doc.tracks[0].produces, "`parse_reader(text: str) -> BaseModel`"
        )

    def test_wave_footer_heading_and_body_clear_active_track_and_multiline_interfaces(
        self,
    ):
        doc = parse_plan(
            "# T\n\n## Phased Implementation Plan\n\n"
            "### Wave 2: Core [Parallel]\n\n"
            "#### Track 2A: Reader\n"
            "- **Model tier**: parallel author pool\n"
            "- **Consumes**:\n"
            "  - `Wave 1: BaseModel`\n"
            "- **Produces**:\n"
            "  - `parse_reader()`\n"
            "- [ ] [NEW] [reader.py](file:///reader.py)\n"
            "**Track 2A Verification**: `pytest tests/test_reader.py`\n\n"
            "#### Wave 2 Verifier & Barrier Gate\n"
            "- **Verification**: `ruff check .`\n\n"
            "### Wave 3: Followup [Parallel]\n\n"
            "#### Track 3A: Writer\n"
            "- **Model tier**: parallel author pool\n"
            "- [ ] [NEW] [writer.py](file:///writer.py)\n"
            "**Track 3A Verification**: `pytest tests/test_writer.py`\n\n"
            "**Wave 3 Verification**: `pytest`\n"
            "- **Verification**: `ruff check .`\n"
        )
        self.assertEqual([w.wave_id for w in doc.waves], ["2", "3"])
        self.assertTrue(doc.waves[0].has_barrier)
        self.assertTrue(doc.waves[0].has_verifier)
        self.assertEqual(doc.tracks[0].consumes, "(multiline)")
        self.assertEqual(doc.tracks[0].produces, "(multiline)")
        # Wave footer heading and body cleared state.track so trailing Verification
        # bullets did not overwrite Track 2A or Track 3A verification.
        self.assertEqual(doc.tracks[0].verification, "pytest tests/test_reader.py")
        self.assertEqual(doc.tracks[1].verification, "pytest tests/test_writer.py")
        self.assertEqual(doc.waves[1].verification, "pytest")


class ShellSegmentTest(unittest.TestCase):
    def test_extract_command_text_prefers_first_backtick_span_and_double_backticks(
        self,
    ):
        self.assertEqual(
            extract_command_text("Run `pytest tests/a.py` to verify."),
            "pytest tests/a.py",
        )
        self.assertEqual(
            extract_command_text("`pytest a.py` (plus Dart MCP `run_tests`)"),
            "pytest a.py",
        )
        self.assertEqual(
            extract_command_text("``python3 -c 'print(\"`x`\")'`` and prose"),
            "python3 -c 'print(\"`x`\")'",
        )
        self.assertEqual(extract_command_text("pytest tests/a.py"), "pytest tests/a.py")

    def test_split_shell_segments_subshells_braces_negation_and_command_subst(self):
        self.assertEqual(
            split_shell_segments("(cd sub && python3 local.py) && pytest tests/a.py"),
            [
                ShellSegment(argv=("python3", "local.py"), cwd="sub"),
                ShellSegment(argv=("pytest", "tests/a.py"), cwd=None),
            ],
        )
        self.assertEqual(
            split_shell_segments("out=$(python3 scripts/lint.py)"),
            [ShellSegment(argv=("python3", "scripts/lint.py"), cwd=None)],
        )
        self.assertEqual(
            split_shell_segments("! rg TODO src/ && { pytest tests/a.py; }"),
            [
                ShellSegment(argv=("rg", "TODO", "src/"), cwd=None),
                ShellSegment(argv=("pytest", "tests/a.py"), cwd=None),
            ],
        )

    def test_split_shell_segments_preserves_cd_and_quoted_pipe(self):
        cmd = (
            "cd /tmp/worktree && env FOO=1 BAR=2 python3 scripts/run.py --flag "
            "| jq -e '(.items | length) == 0'"
        )
        segments = split_shell_segments(cmd)
        self.assertEqual(
            segments,
            [
                ShellSegment(
                    argv=(
                        "env",
                        "FOO=1",
                        "BAR=2",
                        "python3",
                        "scripts/run.py",
                        "--flag",
                    ),
                    cwd="/tmp/worktree",
                ),
                ShellSegment(
                    argv=("jq", "-e", "(.items | length) == 0"),
                    cwd="/tmp/worktree",
                ),
            ],
        )

    def test_split_shell_segments_strips_env_prefix_and_handles_nested_cd(self):
        cmd = "cd sub && cd nested && A2UI_PANE_PYTHON=~/venv/bin/python3 python3 -m unittest"
        segments = split_shell_segments(cmd)
        self.assertEqual(
            segments,
            [ShellSegment(argv=("python3", "-m", "unittest"), cwd="sub/nested")],
        )

    def test_split_shell_segments_malformed_quotes_returns_empty(self):
        self.assertEqual(split_shell_segments("pytest 'unclosed"), [])


class SentenceStatsTest(unittest.TestCase):
    def test_counts_prose_and_ignores_code(self):
        mean, longest = sentence_stats(
            "This has five words here. And `some_code(x, y)` is one word only.\n\n"
            "```\nignored code block with many many many words in it\n```\n"
        )
        self.assertEqual(longest, 6)
        self.assertAlmostEqual(mean, 5.5)

    def test_tables_and_short_fragments_ignored(self):
        mean, longest = sentence_stats("| a | b c d e f g |\n\nOk.\n")
        self.assertEqual((mean, longest), (0.0, 0))

    def test_bullets_split(self):
        _mean, longest = sentence_stats(
            "- one two three four\n- five six seven eight nine"
        )
        self.assertEqual(longest, 5)


if __name__ == "__main__":
    unittest.main()
