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
from plan_model import parse_plan

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
| 1 | cheats | `feat: add cache` | 1 |
| 2 | a2ui-pane | `feat: wire cache` | 2 |

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

    def test_a2ui_review_block_first_and_prefilled_choices(self):
        dsl = plan_surfaces.render_questions(self.doc, surface_id="custom-surface")
        self.assertIn('surface("custom-surface", sendDataModel=true)', dsl)
        self.assertIn('$/review/choice = ["yes"]', dsl)
        self.assertIn('$/q1/choice = ["a"]', dsl)
        self.assertIn('$/q2/choice = ["b"]', dsl)
        self.assertLess(dsl.index("review_block"), dsl.index("q1_block"))
        self.assertLess(dsl.index("q1_block"), dsl.index("q2_block"))
        self.assertIn(
            'Event("submit_plan_decisions", {review: $/review/choice, q1: $/q1/choice, q2: $/q2/choice, notes: $/notes})',
            dsl,
        )

    def test_a2ui_zero_questions_has_only_review_block(self):
        dsl = plan_surfaces.render_questions(self.zero_doc)
        self.assertIn("review_block", dsl)
        self.assertNotIn("q1_block", dsl)
        self.assertIn(
            'Event("submit_plan_decisions", {review: $/review/choice, notes: $/notes})',
            dsl,
        )

    def test_chip_truncation_and_escaping(self):
        truncated = plan_surfaces.truncate_chip_label("A" * 80)
        self.assertEqual(len(truncated), 60)
        self.assertTrue(truncated.endswith("..."))

        escaped = plan_surfaces.escape_dsl_str('line1 "quote" \\slash\\\nline2')
        self.assertEqual(escaped, 'line1 \\"quote\\" \\\\slash\\\\\\nline2')

        dsl = plan_surfaces.render_questions(self.doc)
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
        self.assertIn("flowchart LR", dsl)
        self.assertIn("Track 2A: Reader", dsl)
        self.assertNotIn("modeTone", dsl)
        self.assertIn('repo: "cheats"', dsl)
        self.assertIn('repo: "a2ui-pane"', dsl)

    def test_overview_without_pr_table(self):
        doc = parse_plan(PLAN_ZERO_QUESTIONS)
        dsl = plan_surfaces.render_overview(doc)
        self.assertIn('$/prsHeading = ""', dsl)
        self.assertIn("$/prs = []", dsl)
        self.assertIn("$/openQuestions = 0", dsl)
        self.assertIn('$/status = "Ready"', dsl)
        self.assertIn('$/statusTone = "success"', dsl)
        self.assertIn('label: "Decisions needed"', dsl)

    def test_overview_reuses_plan_flowchart(self):
        text = (
            "# T\n\n## At a glance\n\n- **Goal:** G.\n\n```mermaid\nflowchart LR\n"
            '    W1["Wave 1: Parser"] --> W2["Wave 2: Checks"]\n```\n\n'
            "## Phases\n\n### Wave 1: Parser [Sequential]\n\n### Wave 2: Checks [Sequential]\n"
        )
        dsl = plan_surfaces.render_overview(parse_plan(text), text)
        self.assertIn('W1[\\"Wave 1: Parser\\"] --> W2', dsl)

    def test_overview_generates_flowchart_without_plan_diagram(self):
        text = (
            "# T\n\n## Phases\n\n### Wave 1: Base [Sequential]\n\n### Wave 2: Core [Parallel]\n\n"
            "#### Track 2A: Reader\n\n#### Track 2B: Writer\n\n### Wave 3: Wire [Sequential]\n"
        )
        dsl = plan_surfaces.render_overview(parse_plan(text), text)
        self.assertIn('W1[\\"Wave 1: Base\\"]', dsl)
        self.assertIn('W2T0[\\"Track 2A: Reader\\"]', dsl)
        self.assertIn("W1 --> W2T1", dsl)
        self.assertIn("W2T0 --> W3", dsl)

    def test_overview_template_pr_column_backticks_and_wave_split(self):
        doc = parse_plan(
            "# T\n\n## At a glance\n\n"
            "- **Goal:** make briefs easier to scan.\n"
            "- **Biggest risk:** `plan_model.py` rejects legacy files.\n\n"
            "### PRs\n\n"
            "| # | Repo | PR / Commit | Wave |\n| :--- | :--- | :--- | :--- |\n"
            "| 1 | `cheats` | `feat: add parser` | 2 (2A to 2C) |\n"
            "| 2 | | Add `foo` and `bar` | Wave 3 |\n"
        )
        dsl = plan_surfaces.render_overview(doc)
        self.assertIn('value: "Make briefs easier to scan."', dsl)
        self.assertIn('value: "`plan_model.py` rejects legacy files."', dsl)
        self.assertIn(
            '{id: "1", title: "feat: add parser", repo: "cheats · 2A to 2C", wave: "Wave 2"}',
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
                rc = plan_surfaces.main(["overview", str(plan_path)])
            self.assertEqual(rc, 0)
            self.assertIn("$/waveDiagram =", buf.getvalue())

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
        caller_dsl = plan_surfaces.render_overview(
            parse_plan(PLAN_WITH_QUESTIONS), PLAN_WITH_QUESTIONS
        )
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
            responses[1]["messages"][0]["createSurface"]["surfaceId"], "plan-overview"
        )


if __name__ == "__main__":
    unittest.main()
