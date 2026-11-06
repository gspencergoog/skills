"""test_plan_surfaces.py - Unit tests for plan_surfaces.py"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import plan_surfaces
from plan_model import OpenQuestion, QuestionOption, parse_plan

A2UI_PANE_ROOT = Path.home() / "code" / "a2ui-pane"
EXPRESS_WORKER = (
    A2UI_PANE_ROOT / "packages" / "mcp-server" / "python" / "express_worker.py"
)
CATALOG_PATHS = [
    str(A2UI_PANE_ROOT / "catalogs" / "v1_0" / "basic" / "catalog.json"),
    str(A2UI_PANE_ROOT / "catalogs" / "v1_0" / "charts" / "catalog.json"),
    str(A2UI_PANE_ROOT / "catalogs" / "v1_0" / "status" / "catalog.json"),
]

PLAN_WITH_QUESTIONS = """# Improve the widget

## At a glance

- **Goal:** Make widgets faster.
- **Shape:** 2 waves, 2 parallel tracks, 2 commits.
- **Decisions needed from you:** 2 (Q1, Q2).
- **Biggest risk:** Cache invalidation.
- **Not doing:** No UI changes.

### Commits

| # | Repo | Commit | Wave |
| ---: | :--- | :--- | :--- |
| 1 | backend | `feat: add cache` | 1 |
| 2 | frontend | `feat: wire cache` | 2 |

## User Review Required

> [!IMPORTANT]
> The cache is on by default.

## Open Questions

### Q1: Should the cache be on by default?
- (Recommended) A: Yes, enable it immediately for all callers with a very long option description that exceeds sixty characters easily. Consequence: faster first load.
- B: No, keep it off. Consequence: users opt in.

### Q2: How should "quoted" and `backtick` $paths\\nbe handled?
- A: Reject them at the boundary. Consequence: strict input.
- (Recommended) B: Escape "quotes", \\backslashes\\, and $vars. Consequence: safe strings.

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

#### Track 2B: Writer

- **Model tier**: Gemini 4 Argon High
- [ ] [MODIFY] [writer.py](file:///repo/writer.py)
"""

PLAN_ZERO_QUESTIONS = """# Simple cleanup

## At a glance

- **Goal:** Clean up imports.
- **Decisions needed from you:** none.
- **Not doing:** No behavior changes.

## Open Questions

None currently blocking.

## Proposed Changes

- [ ] [MODIFY] [a.py](file:///repo/a.py)
"""

SIX_WAVE_ENVELOPE_PLAN = """# Prefix A2UI v1.0 Component Envelope Fields with `@`

## At a glance

- **Goal:** Prefix v1.0 component protocol fields so property names cannot collide.
- **Shape:** 6 waves, 2 parallel 4-track phases, 4 PRs.
- **Decisions needed from you:** none.
- **Biggest risk:** Breaking pre-1.0 custom catalogs.
- **Not doing:** No `id` or `component` prefixing.

```mermaid
flowchart TD
    subgraph W1["Wave 1: Shared Core Conformance Fixtures [Sequential]"]
        S1["Pre-1.0 property & v1.0 dual-read YAML cases (PR 1 base)"]
    end
    subgraph W2["Wave 2: Core & Renderer Dual-Read [Parallel]"]
        T2A["Track 2A: TS web_core & web adapters"]
        T2B["Track 2B: Python a2ui_core"]
        T2C["Track 2C: Dart a2ui_core & a2ui_flutter"]
        T2D["Track 2D: Swift A2UICore & A2UISwiftUI"]
        V2["Wave 2 Verifier & PR 1 Gate"]
        T2A -.-> V2
        T2B -.-> V2
        T2C -.-> V2
        T2D -.-> V2
    end
    W1 --> W2 --> W3 --> W4 --> W5 --> W6
```

### PRs

| # | Repo | PR / Commit | Wave |
| ---: | :--- | :--- | :--- |
| 1 | `a2ui-project/a2ui` | `feat(core): gate pre-1.0 envelope fields` | 1, 2 |
| 2 | `a2ui-project/a2ui` | `feat(v1.0)!: prefix component envelope` | 3, 4 |
| 3 | `google3` | `Roll //third_party/a2ui` | 5 |
| 4 | `a2ui-project/a2ui` | `refactor(v1.0)!: drop legacy bare fallback` | 6 |

## Open Questions

None currently blocking.

## Migration & Execution Phases

### Wave 1: Shared Core Conformance Fixtures [Sequential]

- **Model tier**: Gemini Pro Next
- [ ] [MODIFY] [message_processor_v1_0.yaml](file:///repo/message_processor_v1_0.yaml)

### Wave 2: Core & Renderer Dual-Read [Parallel]

#### Track 2A: TS web_core & web adapters

- **Model tier**: Gemini Pro Next
- [ ] [MODIFY] [payload-validator.ts](file:///repo/payload-validator.ts)

#### Track 2B: Python a2ui_core

- **Model tier**: Gemini Pro Next
- [ ] [MODIFY] [validator.py](file:///repo/validator.py)

#### Track 2C: Dart a2ui_core & a2ui_flutter

- **Model tier**: Gemini Pro Next
- [ ] [MODIFY] [validator.dart](file:///repo/validator.dart)

#### Track 2D: Swift A2UICore & A2UISwiftUI

- **Model tier**: Gemini Pro Next
- [ ] [MODIFY] [Validator.swift](file:///repo/Validator.swift)

**Wave 2 Verifier & Barrier Gate**:
- Peer `verifier` runs all SDK suites.

### Wave 3: v1.0 Spec, Catalogs & Codegen [Sequential]

- **Model tier**: Gemini Pro Next
- [ ] [MODIFY] [common_types.json](file:///repo/common_types.json)

### Wave 4: Agent SDKs New-Emit & Docs [Parallel]

#### Track 4A: Python a2ui_agent

- **Model tier**: Gemini Pro Next
- [ ] [MODIFY] [models.py](file:///repo/models.py)

#### Track 4B: TS a2ui/agent

- **Model tier**: Gemini Pro Next
- [ ] [MODIFY] [schema.ts](file:///repo/schema.ts)

#### Track 4C: Dart a2ui_agent & a2ui_cli

- **Model tier**: Gemini Pro Next
- [ ] [MODIFY] [agent.dart](file:///repo/agent.dart)

#### Track 4D: Conformance, Blueprints & Docs

- **Model tier**: Gemini 3.8 Flash High
- [ ] [MODIFY] [docs.md](file:///repo/docs.md)

**Wave 4 Verifier & Barrier Gate**:
- Peer `verifier` checks all agent outputs.

### Wave 5: Google3 Roll & Local Sync [Sequential]

- **Model tier**: Gemini Pro Next
- [ ] [MODIFY] [BUILD](file:///repo/BUILD)

### Wave 6: Drop Legacy v1.0 Bare Fallback [Sequential]

- **Model tier**: Gemini Pro Next
- [ ] [MODIFY] [legacy.py](file:///repo/legacy.py)
"""

SAMPLE_FINDINGS = [
    {
        "id": "F1",
        "severity": "blocker",
        "title": "Race condition in writer",
        "summary": "Two threads can interleave writes.",
        "evidence": "writer.py:42",
    },
    {
        "id": "F2",
        "severity": "major",
        "title": "Missing timeout",
        "summary": "Subprocess call has no timeout.",
        "evidence": "runner.py:19",
    },
    {
        "id": "F3",
        "severity": "minor",
        "title": "Unused variable",
        "summary": "Temporary variable is unused.",
        "evidence": "utils.py:8",
    },
    {
        "id": "F4",
        "severity": "nit",
        "title": "Typo in comment",
        "summary": "Extra space in docstring.",
        "evidence": "utils.py:2",
    },
]


class QuestionsSurfaceTest(unittest.TestCase):
    def setUp(self):
        self.doc = parse_plan(PLAN_WITH_QUESTIONS)
        self.zero_doc = parse_plan(PLAN_ZERO_QUESTIONS)

    def test_a2ui_cards_options_and_footer_review(self):
        dsl = plan_surfaces.render_questions(self.doc, surface_id="custom-surface")
        self.assertIn('surface("custom-surface", sendDataModel=true)', dsl)
        self.assertIn('Text("Recommended answers are preselected.", "caption")', dsl)
        self.assertIn("$/review = true", dsl)
        self.assertIn('$/q1/choice = ["a"]', dsl)
        self.assertIn('$/q2/choice = ["b"]', dsl)
        self.assertNotIn("review_block", dsl)
        self.assertIn("q1_cards = ChoiceCards($/q1/options, $/q1/choice)", dsl)
        self.assertIn("q2_cards = ChoiceCards($/q2/options, $/q2/choice)", dsl)
        self.assertLess(dsl.index("q1_block"), dsl.index("q2_block"))
        self.assertIn(
            'review_check = CheckBox("Run an independent review of this plan (recommended)", $/review)',
            dsl,
        )
        self.assertIn(
            'footer = Row([review_check, submit_btn], "spaceBetween", "center")',
            dsl,
        )
        self.assertIn(
            'Event("submit_plan_decisions", {review: $/review, q1: $/q1/choice, q2: $/q2/choice, notes: $/notes})',
            dsl,
        )

    def test_card_options_recommended_badge_quiet_defer_and_no_truncation(self):
        options = plan_surfaces._card_options_for_question(self.doc.questions[0])
        self.assertEqual(len(options), 3)
        self.assertEqual(options[0]["value"], "a")
        self.assertTrue(
            str(options[0]["label"]).startswith("A · Yes, enable it immediately")
        )
        self.assertGreater(len(str(options[0]["label"])), 60)
        self.assertEqual(options[0]["description"], "faster first load.")
        self.assertEqual(options[0]["badge"], "Recommended")
        self.assertEqual(options[0]["tone"], "success")
        self.assertEqual(options[1]["value"], "b")
        self.assertEqual(options[1]["label"], "B · No, keep it off.")
        self.assertEqual(options[1]["description"], "users opt in.")
        self.assertNotIn("badge", options[1])
        self.assertNotIn("tone", options[1])
        self.assertEqual(
            options[2], {"value": "defer", "label": "Defer", "quiet": True}
        )

    def test_a2ui_zero_questions_has_only_footer_review(self):
        dsl = plan_surfaces.render_questions(self.zero_doc)
        self.assertNotIn("review_block", dsl)
        self.assertNotIn("q1_block", dsl)
        self.assertIn("$/review = true", dsl)
        self.assertIn(
            'review_check = CheckBox("Run an independent review of this plan (recommended)", $/review)',
            dsl,
        )
        self.assertIn(
            'Event("submit_plan_decisions", {review: $/review, notes: $/notes})',
            dsl,
        )

    def test_a2ui_list_layout_fallback(self):
        dsl = plan_surfaces.render_questions(self.doc, layout="list")
        self.assertNotIn("ChoiceCards", dsl)
        self.assertIn('Text("Recommended answers are preselected.", "caption")', dsl)
        self.assertIn('Badge("A", "success")', dsl)
        self.assertIn('Badge("B", "neutral")', dsl)
        self.assertIn('Text("faster first load.", "caption")', dsl)
        self.assertIn(
            'q1_picker = ChoicePicker(_, "mutuallyExclusive", '
            '[{label: "A", value: "a"}, {label: "B", value: "b"}, {label: "Defer", value: "defer"}], '
            '$/q1/choice, "chips")',
            dsl,
        )
        self.assertIn(
            'review_check = CheckBox("Run an independent review of this plan (recommended)", $/review)',
            dsl,
        )
        self.assertIn(
            'Event("submit_plan_decisions", {review: $/review, q1: $/q1/choice, q2: $/q2/choice, notes: $/notes})',
            dsl,
        )

    def test_chip_truncation_and_escaping(self):
        truncated = plan_surfaces.truncate_chip_label("A" * 80)
        self.assertEqual(len(truncated), 60)
        self.assertTrue(truncated.endswith("..."))

        long_q = OpenQuestion(
            qid="Q9",
            title="Long label",
            line=1,
            options=[
                QuestionOption(
                    letter="A" * 80,
                    text="opt",
                    consequence="",
                    recommended=True,
                )
            ],
        )
        list_chips = plan_surfaces._list_chips_for_question(long_q)
        self.assertEqual(len(list_chips[0]["label"]), 60)
        self.assertTrue(list_chips[0]["label"].endswith("..."))
        self.assertEqual(list_chips[-1], {"label": "Defer", "value": "defer"})

        escaped = plan_surfaces.escape_dsl_str('line1 "quote" \\slash\\\nline2')
        self.assertEqual(escaped, 'line1 \\"quote\\" \\\\slash\\\\\\nline2')

        for layout in ("cards", "list"):
            dsl = plan_surfaces.render_questions(self.doc, layout=layout)
            self.assertIn('\\"quoted\\"', dsl)
            self.assertIn("\\\\backslashes\\\\", dsl)

    def test_ask_question_format(self):
        raw = plan_surfaces.render_questions(self.doc, fmt="ask-question")
        parsed = json.loads(raw)
        self.assertIsInstance(parsed, list)
        self.assertEqual(len(parsed), 3)
        self.assertEqual(
            parsed[0]["question"], "Run an independent review of this plan?"
        )
        self.assertEqual(parsed[0]["options"][0], "(Recommended) Yes")
        self.assertTrue(parsed[1]["options"][0].startswith("(Recommended) A:"))
        self.assertTrue(parsed[2]["options"][0].startswith("(Recommended) B:"))
        self.assertEqual(parsed[2]["options"][-1], "Defer")

    def test_markdown_format(self):
        md = plan_surfaces.render_questions(self.doc, fmt="markdown")
        lines = [line for line in md.strip().splitlines() if line.strip()]
        self.assertTrue(
            lines[0].startswith("1. **Run an independent review of this plan?**")
        )
        self.assertIn("- (Recommended) Yes", lines[1])
        self.assertIn("2. **Q1:", md)
        self.assertIn("3. **Q2:", md)
        q2_pos = md.index("3. **Q2:")
        rec_b_pos = md.index("- (Recommended) B:", q2_pos)
        opt_a_pos = md.index("- A:", q2_pos)
        self.assertLess(rec_b_pos, opt_a_pos)


class OverviewSurfaceTest(unittest.TestCase):
    def test_overview_contract_keys_and_values(self):
        doc = parse_plan(PLAN_WITH_QUESTIONS)
        dsl = plan_surfaces.render_overview(doc)
        for key in (
            '$/title = "## ',
            "$/subtitle =",
            "$/status =",
            "$/statusTone =",
            "$/glance =",
            "$/waveDiagram =",
            '$/prsHeading = "### Planned changes"',
            "$/prs =",
            "$/openQuestions = 2",
            "$/ackLabel =",
        ):
            self.assertIn(key, dsl)
        self.assertIn("2 waves · 2 parallel tracks · 2 changes · 2 open questions", dsl)
        self.assertIn('label: "Goal"', dsl)
        self.assertIn('label: "Decisions needed"', dsl)
        self.assertIn("```mermaid", dsl)
        self.assertIn("flowchart TD", dsl)
        self.assertIn('W1[\\"Wave 1 · Foundation (PR 1)\\"]', dsl)
        self.assertIn('subgraph W2[\\"Wave 2 · Core (Verifier · PR 2)\\"]', dsl)
        self.assertIn('W2T0[\\"2A · Reader\\"]', dsl)
        self.assertIn('W2T1[\\"2B · Writer\\"]', dsl)
        self.assertIn("W1 --> W2", dsl)
        self.assertNotIn("modeTone", dsl)
        self.assertIn('repo: "backend"', dsl)
        self.assertIn('repo: "frontend"', dsl)

    def test_overview_without_pr_table(self):
        doc = parse_plan(PLAN_ZERO_QUESTIONS)
        dsl = plan_surfaces.render_overview(doc)
        self.assertIn('$/prsHeading = ""', dsl)
        self.assertIn("$/prs = []", dsl)
        self.assertIn('$/waveDiagram = ""', dsl)
        self.assertIn("$/openQuestions = 0", dsl)
        self.assertIn('$/status = "Ready"', dsl)
        self.assertIn('$/statusTone = "success"', dsl)
        self.assertIn('label: "Decisions needed"', dsl)

    def test_diagram_modes_auto_plan_and_compact(self):
        text_with_waves_and_raw = (
            "# T\n\n## At a glance\n\n- **Goal:** G.\n\n```mermaid\nflowchart LR\n"
            '    RawW1["Wave 1: Raw Legacy"] --> RawW2["Wave 2: Raw Legacy"]\n```\n\n'
            "## Phases\n\n### Wave 1: Parser [Sequential]\n\n### Wave 2: Checks [Sequential]\n"
        )
        doc_with_waves = parse_plan(text_with_waves_and_raw)

        auto_dsl = plan_surfaces.render_overview(
            doc_with_waves, text_with_waves_and_raw
        )
        self.assertIn('W1[\\"Wave 1 · Parser\\"]', auto_dsl)
        self.assertIn('W2[\\"Wave 2 · Checks\\"]', auto_dsl)
        self.assertNotIn("RawW1", auto_dsl)

        plan_dsl = plan_surfaces.render_overview(
            doc_with_waves, text_with_waves_and_raw, diagram_mode="plan"
        )
        self.assertIn(
            'RawW1[\\"Wave 1: Raw Legacy\\"] --> RawW2[\\"Wave 2: Raw Legacy\\"]',
            plan_dsl,
        )

        text_no_waves_with_raw = (
            "# T\n\n## At a glance\n\n- **Goal:** G.\n\n```mermaid\nflowchart LR\n"
            '    Custom1["Wave 1: Custom"] --> Custom2["Wave 2: Custom"]\n```\n\n'
            "## Proposed Changes\n\n- [ ] [MODIFY] [a.py](file:///a.py)\n"
        )
        doc_no_waves = parse_plan(text_no_waves_with_raw)

        auto_fallback_dsl = plan_surfaces.render_overview(
            doc_no_waves, text_no_waves_with_raw, diagram_mode="auto"
        )
        self.assertIn(
            'Custom1[\\"Wave 1: Custom\\"] --> Custom2[\\"Wave 2: Custom\\"]',
            auto_fallback_dsl,
        )

        compact_zero_waves_dsl = plan_surfaces.render_overview(
            doc_no_waves, text_no_waves_with_raw, diagram_mode="compact"
        )
        self.assertIn('$/waveDiagram = ""', compact_zero_waves_dsl)

    def test_overview_generates_flowchart_without_plan_diagram(self):
        text = (
            "# T\n\n## Phases\n\n### Wave 1: Base [Sequential]\n\n### Wave 2: Core [Parallel]\n\n"
            "#### Track 2A: Reader\n\n#### Track 2B: Writer\n\n### Wave 3: Wire [Sequential]\n"
        )
        doc = parse_plan(text)
        dsl = plan_surfaces.render_overview(doc, text)
        self.assertIn("flowchart TD", dsl)
        self.assertIn('W1[\\"Wave 1 · Base\\"]', dsl)
        self.assertIn('subgraph W2[\\"Wave 2 · Core (Verifier)\\"]', dsl)
        self.assertIn('W2T0[\\"2A · Reader\\"]', dsl)
        self.assertIn('W2T1[\\"2B · Writer\\"]', dsl)
        self.assertIn('W3[\\"Wave 3 · Wire\\"]', dsl)
        self.assertIn("W1 --> W2", dsl)
        self.assertIn("W2 --> W3", dsl)
        self.assertNotIn("W1 --> W2T", dsl)
        self.assertNotIn("W2T0 --> W3", dsl)

        plan_fallback_dsl = plan_surfaces.render_overview(
            doc, text, diagram_mode="plan"
        )
        self.assertIn('subgraph W2[\\"Wave 2 · Core (Verifier)\\"]', plan_fallback_dsl)

    def test_flowchart_direction_lr_vs_td(self):
        two_seq = parse_plan(
            "# T\n\n## Phases\n\n"
            "### Wave 1: One [Sequential]\n\n"
            "### Wave 2: Two [Sequential]\n"
        )
        self.assertTrue(
            plan_surfaces._generated_flowchart(two_seq).startswith("flowchart LR\n")
        )

        three_seq = parse_plan(
            "# T\n\n## Phases\n\n"
            "### Wave 1: One [Sequential]\n\n"
            "### Wave 2: Two [Sequential]\n\n"
            "### Wave 3: Three [Sequential]\n"
        )
        self.assertTrue(
            plan_surfaces._generated_flowchart(three_seq).startswith("flowchart TD\n")
        )

        two_with_parallel = parse_plan(
            "# T\n\n## Phases\n\n"
            "### Wave 1: One [Sequential]\n\n"
            "### Wave 2: Two [Parallel]\n\n"
            "#### Track 2A: Alpha\n"
        )
        self.assertTrue(
            plan_surfaces._generated_flowchart(two_with_parallel).startswith(
                "flowchart TD\n"
            )
        )

    def test_completing_wave_pr_mapping_and_verifier_caption(self):
        doc = parse_plan(
            "# T\n\n### PRs\n\n"
            "| # | Repo | PR / Commit | Wave |\n"
            "| ---: | :--- | :--- | :--- |\n"
            "| 1 | a2ui | `feat: span w1 and w2` | 1, 2 |\n"
            "| 2 | a2ui | `feat: track 2a only` | 2 (2A) |\n"
            "| 3 | a2ui | `feat: span all three` | 1, 2 (2A), 3 |\n"
            "| 4 | cheats | `feat: also wave 3` | 1, 2 (2B), 3 |\n\n"
            "## Phases\n\n"
            "### Wave 1: Foundation [Sequential]\n\n"
            "### Wave 2: Dual-Read [Parallel]\n\n"
            "#### Track 2A: Reader\n\n"
            "**Wave 2 Verifier**:\n"
            "- Peer `verifier` checks Track 2A.\n\n"
            "### Wave 3: Cutover [Sequential]\n"
        )
        chart = plan_surfaces._generated_flowchart(doc)
        self.assertIn('    W1["Wave 1 · Foundation"]', chart)
        self.assertNotIn("Wave 1 · Foundation (", chart)
        self.assertIn(
            '    subgraph W2["Wave 2 · Dual-Read (Verifier · PR 1 · PR 2)"]', chart
        )
        self.assertIn('        W2T0["2A · Reader"]', chart)
        self.assertIn('    W3["Wave 3 · Cutover (PR 3, PR 4)"]', chart)

    def test_title_normalization_strips_prefixes_tags_and_escapes_quotes(self):
        self.assertEqual(
            plan_surfaces._normalize_diagram_title(
                "Track 2A: Renderer Integration [Parallel]"
            ),
            "2A · Renderer Integration",
        )
        self.assertEqual(
            plan_surfaces._normalize_diagram_title(
                'Wave 1: Handle "quoted" `code`:   extra   spaces [Sequential]',
                strip_wave_prefix=True,
            ),
            "Handle 'quoted' code · extra spaces",
        )

    def test_six_wave_multi_track_synthesis(self):
        doc = parse_plan(SIX_WAVE_ENVELOPE_PLAN)
        chart = plan_surfaces._generated_flowchart(doc)
        expected_lines = [
            "flowchart TD",
            '    W1["Wave 1 · Shared Core Conformance Fixtures"]',
            '    subgraph W2["Wave 2 · Core & Renderer Dual-Read (Verifier · PR 1)"]',
            '        W2T0["2A · TS web_core & web adapters"]',
            '        W2T1["2B · Python a2ui_core"]',
            '        W2T2["2C · Dart a2ui_core & a2ui_flutter"]',
            '        W2T3["2D · Swift A2UICore & A2UISwiftUI"]',
            "    end",
            '    W3["Wave 3 · v1.0 Spec, Catalogs & Codegen"]',
            '    subgraph W4["Wave 4 · Agent SDKs New-Emit & Docs (Verifier · PR 2)"]',
            '        W4T0["4A · Python a2ui_agent"]',
            '        W4T1["4B · TS a2ui/agent"]',
            '        W4T2["4C · Dart a2ui_agent & a2ui_cli"]',
            '        W4T3["4D · Conformance, Blueprints & Docs"]',
            "    end",
            '    W5["Wave 5 · Google3 Roll & Local Sync (PR 3)"]',
            '    W6["Wave 6 · Drop Legacy v1.0 Bare Fallback (PR 4)"]',
            "    W1 --> W2",
            "    W2 --> W3",
            "    W3 --> W4",
            "    W4 --> W5",
            "    W5 --> W6",
        ]
        self.assertEqual(chart.splitlines(), expected_lines)
        self.assertNotIn("-.->", chart)
        self.assertNotIn("V2[", chart)
        self.assertNotIn("V4[", chart)
        edge_lines = [line for line in chart.splitlines() if "-->" in line]
        self.assertEqual(len(edge_lines), 5)

    def test_overview_template_pr_column_backticks_and_wave_split(self):
        doc = parse_plan(
            "# T\n\n## At a glance\n\n"
            "- **Goal:** make briefs easier to scan.\n"
            "- **Biggest risk:** `plan_model.py` rejects legacy files.\n\n"
            "### PRs\n\n"
            "| # | Repo | PR / Commit | Wave |\n| :--- | :--- | :--- | :--- |\n"
            "| 1 | `backend` | `feat: add parser` | 2 (2A to 2C) |\n"
            "| 2 | | Add `foo` and `bar` | Wave 3 |\n"
        )
        dsl = plan_surfaces.render_overview(doc)
        self.assertIn('value: "Make briefs easier to scan."', dsl)
        self.assertIn('value: "`plan_model.py` rejects legacy files."', dsl)
        self.assertIn(
            '{id: "1", title: "feat: add parser", repo: "backend · 2A to 2C", wave: "Wave 2"}',
            dsl,
        )
        self.assertIn(
            '{id: "2", title: "Add `foo` and `bar`", repo: "", wave: "Wave 3"}', dsl
        )


class TriageSurfaceTest(unittest.TestCase):
    def test_triage_a2ui_buckets_and_icons(self):
        dsl = plan_surfaces.render_triage(SAMPLE_FINDINGS, fmt="a2ui")
        self.assertIn("$/title =", dsl)
        self.assertIn("$/intro =", dsl)
        self.assertIn("$/items =", dsl)
        self.assertIn("$/submitLabel =", dsl)
        self.assertIn("bucket_picker = ChoicePicker(", dsl)
        self.assertIn('{label: "Apply", value: "Apply"}', dsl)
        self.assertIn('{label: "Discuss", value: "Discuss"}', dsl)
        self.assertIn('{label: "Reject", value: "Reject"}', dsl)
        self.assertIn('icon: "error", title: "F1: Race condition in writer"', dsl)
        self.assertIn('icon: "warning", title: "F2: Missing timeout"', dsl)
        self.assertIn('icon: "info", title: "F3: Unused variable"', dsl)
        self.assertEqual(dsl.count('bucket: ["Apply"]'), 2)
        self.assertEqual(dsl.count('bucket: ["Discuss"]'), 2)

    def test_triage_ask_question_format(self):
        raw = plan_surfaces.render_triage(SAMPLE_FINDINGS, fmt="ask-question")
        parsed = json.loads(raw)
        self.assertEqual(len(parsed), 4)
        self.assertEqual(parsed[0]["options"][0], "(Recommended) Apply")
        self.assertEqual(parsed[1]["options"][0], "(Recommended) Apply")
        self.assertEqual(parsed[2]["options"][0], "(Recommended) Discuss")
        self.assertEqual(parsed[3]["options"][0], "(Recommended) Discuss")

    def test_triage_markdown_format(self):
        md = plan_surfaces.render_triage(SAMPLE_FINDINGS, fmt="markdown")
        self.assertIn("1. **F1: Race condition in writer (blocker)**", md)
        self.assertIn("- (Recommended) Apply", md)
        self.assertIn("- (Recommended) Discuss", md)


class CliEntrypointTest(unittest.TestCase):
    def test_cli_subcommands(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            plan_path = Path(tmpdir) / "plan.md"
            plan_path.write_text(PLAN_WITH_QUESTIONS, encoding="utf-8")
            findings_path = Path(tmpdir) / "findings.json"
            findings_path.write_text(json.dumps(SAMPLE_FINDINGS), encoding="utf-8")

            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = plan_surfaces.main(
                    ["questions", str(plan_path), "--format", "markdown"]
                )
            self.assertEqual(rc, 0)
            self.assertIn(
                "1. **Run an independent review of this plan?**", buf.getvalue()
            )

            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = plan_surfaces.main(
                    ["questions", str(plan_path), "--layout", "list"]
                )
            self.assertEqual(rc, 0)
            self.assertIn("ChoicePicker(", buf.getvalue())
            self.assertNotIn("ChoiceCards(", buf.getvalue())

            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = plan_surfaces.main(["overview", str(plan_path)])
            self.assertEqual(rc, 0)
            self.assertIn("$/waveDiagram =", buf.getvalue())
            self.assertIn(
                'subgraph W2[\\"Wave 2 · Core (Verifier · PR 2)\\"]',
                buf.getvalue(),
            )

            six_wave_path = Path(tmpdir) / "six_wave.md"
            six_wave_path.write_text(SIX_WAVE_ENVELOPE_PLAN, encoding="utf-8")
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = plan_surfaces.main(
                    ["overview", str(six_wave_path), "--diagram", "plan"]
                )
            self.assertEqual(rc, 0)
            self.assertIn("T2A -.-> V2", buf.getvalue())

            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = plan_surfaces.main(["overview", str(six_wave_path)])
            self.assertEqual(rc, 0)
            self.assertIn(
                'subgraph W2[\\"Wave 2 · Core & Renderer Dual-Read (Verifier · PR 1)\\"]',
                buf.getvalue(),
            )
            self.assertNotIn("T2A -.-> V2", buf.getvalue())

            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = plan_surfaces.main(
                    ["triage", str(findings_path), "--format", "ask-question"]
                )
            self.assertEqual(rc, 0)
            self.assertEqual(len(json.loads(buf.getvalue())), 4)


@unittest.skipUnless(
    os.environ.get("A2UI_PANE_PYTHON") and EXPRESS_WORKER.exists(),
    "A2UI_PANE_PYTHON or express_worker.py not available",
)
class ExpressCompileSmokeTest(unittest.TestCase):
    def test_questions_dsl_compiles_via_express_worker(self):
        python_bin = os.environ["A2UI_PANE_PYTHON"]
        edge_plan = PLAN_WITH_QUESTIONS + (
            '\n### Q3: Multi-line and special chars "$foo" `bar`?\n'
            '- (Recommended) A: Keep `$foo` and "quotes". Consequence: line 1\\nline 2.\n'
            "- B: Drop them.\n"
        )
        doc = parse_plan(edge_plan)
        dsl = plan_surfaces.render_questions(doc, surface_id="smoke-test")
        requests = "\n".join(
            [
                json.dumps(
                    {
                        "id": 1,
                        "op": "init",
                        "catalogPaths": CATALOG_PATHS,
                        "version": "1.0",
                    }
                ),
                json.dumps(
                    {
                        "id": 2,
                        "op": "compile",
                        "surfaceId": "smoke-test",
                        "dsl": dsl,
                        "version": "1.0",
                    }
                ),
            ]
        )
        proc = subprocess.run(
            [python_bin, str(EXPRESS_WORKER)],
            input=requests + "\n",
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )
        responses = [
            json.loads(line)
            for line in proc.stdout.strip().splitlines()
            if line.strip()
        ]
        self.assertEqual(len(responses), 2)
        self.assertTrue(responses[0].get("ok"), responses[0])
        self.assertTrue(responses[1].get("ok"), responses[1])
        messages = responses[1]["messages"]
        self.assertTrue(messages)
        self.assertEqual(messages[0]["createSurface"]["surfaceId"], "smoke-test")

    def test_questions_list_layout_dsl_compiles_via_express_worker(self):
        python_bin = os.environ["A2UI_PANE_PYTHON"]
        edge_plan = PLAN_WITH_QUESTIONS + (
            '\n### Q3: Multi-line and special chars "$foo" `bar`?\n'
            '- (Recommended) A: Keep `$foo` and "quotes". Consequence: line 1\\nline 2.\n'
            "- B: Drop them.\n"
        )
        doc = parse_plan(edge_plan)
        dsl = plan_surfaces.render_questions(
            doc, surface_id="smoke-list-test", layout="list"
        )
        requests = "\n".join(
            [
                json.dumps(
                    {
                        "id": 1,
                        "op": "init",
                        "catalogPaths": CATALOG_PATHS,
                        "version": "1.0",
                    }
                ),
                json.dumps(
                    {
                        "id": 2,
                        "op": "compile",
                        "surfaceId": "smoke-list-test",
                        "dsl": dsl,
                        "version": "1.0",
                    }
                ),
            ]
        )
        proc = subprocess.run(
            [python_bin, str(EXPRESS_WORKER)],
            input=requests + "\n",
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )
        responses = [
            json.loads(line)
            for line in proc.stdout.strip().splitlines()
            if line.strip()
        ]
        self.assertEqual(len(responses), 2)
        self.assertTrue(responses[0].get("ok"), responses[0])
        self.assertTrue(responses[1].get("ok"), responses[1])
        messages = responses[1]["messages"]
        self.assertTrue(messages)
        self.assertEqual(messages[0]["createSurface"]["surfaceId"], "smoke-list-test")

    def test_overview_dsl_compiles_via_express_worker(self):
        python_bin = os.environ["A2UI_PANE_PYTHON"]
        template_path = (
            A2UI_PANE_ROOT
            / "plugin"
            / "skills"
            / "genui"
            / "templates"
            / "plan_overview.a2ui"
        )
        template_body = template_path.read_text(encoding="utf-8").strip()
        for sample_plan in (PLAN_WITH_QUESTIONS, SIX_WAVE_ENVELOPE_PLAN):
            caller_dsl = plan_surfaces.render_overview(
                parse_plan(sample_plan), sample_plan
            )
            self.assertIn("subgraph W2", caller_dsl)
            full_dsl = f'surface("plan-overview", sendDataModel=true)\n{template_body}\n{caller_dsl}'
            requests = "\n".join(
                [
                    json.dumps(
                        {
                            "id": 1,
                            "op": "init",
                            "catalogPaths": CATALOG_PATHS,
                            "version": "1.0",
                        }
                    ),
                    json.dumps(
                        {
                            "id": 2,
                            "op": "compile",
                            "surfaceId": "plan-overview",
                            "dsl": full_dsl,
                            "version": "1.0",
                        }
                    ),
                ]
            )
            proc = subprocess.run(
                [python_bin, str(EXPRESS_WORKER)],
                input=requests + "\n",
                text=True,
                capture_output=True,
                check=True,
                timeout=30,
            )
            responses = [
                json.loads(line)
                for line in proc.stdout.strip().splitlines()
                if line.strip()
            ]
            self.assertEqual(len(responses), 2)
            self.assertTrue(responses[0].get("ok"), responses[0])
            self.assertTrue(responses[1].get("ok"), responses[1])
            self.assertEqual(
                responses[1]["messages"][0]["createSurface"]["surfaceId"],
                "plan-overview",
            )


if __name__ == "__main__":
    unittest.main()
