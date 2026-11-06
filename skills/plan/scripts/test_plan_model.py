#!/usr/bin/env python3
"""test_plan_model.py - Unit tests for plan_model.py"""

import unittest

from plan_model import (
    FULL_GLANCE_FIELDS,
    find_section,
    parse_plan,
    section_text,
    sentence_stats,
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
