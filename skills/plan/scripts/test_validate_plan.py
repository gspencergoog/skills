#!/usr/bin/env python3
"""test_validate_plan.py - Unit tests for validate_plan.py"""

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from validate_plan import NEXT_STEP_MESSAGE, PlanValidator, main

VALID_PLAN = """# Feature: Add Cache Invalidation

## At a glance

- **Goal:** Add cache key invalidation with a 5-minute TTL.
- **Shape:** Single sequential change across two library files.
- **Decisions needed from you:** none.
- **Biggest risk:** Stale entries if callers skip the TTL helper.
- **Not doing:** Distributed cache eviction.

## User Review Required

> [!IMPORTANT]
> This change introduces a 5-minute TTL default.

## Open Questions

None currently blocking.

## Decisions

- **Q1 (2026-11-04):** A. Keep the 5-minute TTL default.

## Proposed Changes

### Cache Subsystem
- [ ] [MODIFY] [lib/cache.py](file:///lib/cache.py)
  - Add `invalidate_key()` method.
- [ ] [NEW] [lib/ttl.py](file:///lib/ttl.py)
  - Implement time-to-live helper.

## Verification Plan

### Automated Tests
- Run `pytest tests/test_cache.py`
"""


VALID_PARALLEL_WAVE_PLAN = """# Feature: Parallel Subsystem Upgrade

## At a glance

- **Goal:** Upgrade the parser and lexer in parallel waves.
- **Shape:** 3 waves with 2 parallel tracks in Wave 2.
- **Decisions needed from you:** none.
- **Biggest risk:** Shared protocol type drift between parser and lexer.
- **Not doing:** Runtime AST optimization.

```mermaid
flowchart LR
    W1["Wave 1"] --> W2A["Track 2A"]
    W1 --> W2B["Track 2B"]
    W2A --> W3["Wave 3"]
    W2B --> W3
```

### Commits

| # | Commit | Wave |
| ---: | :--- | :--- |
| 1 | `feat: add protocol types` | 1 |
| 2 | `feat: upgrade parser and lexer` | 2 |
| 3 | `feat: wire exports` | 3 |

## User Review Required

None.

## Open Questions

None currently blocking.

## Decisions

- **Q1 (2026-11-04):** A. Land in three commits.

## Execution Strategy & Parallelization

| Setting | Value |
| :--- | :--- |
| Orchestration | `/swarm` with 2 author workers (`workspace: "share"`) |

## Phased Implementation Plan

### Wave 1: Foundation [Sequential]
- **Model tier**: Claude Fable 5.1 High
- [ ] [MODIFY] [lib/types.py](file:///lib/types.py)
  - Add shared protocol types.

**Wave 1 Verification**: `pytest tests/test_types.py`

### Wave 2: Core Modules [Parallel]

#### Track 2A: Parser Module
- **Model tier**: Gemini Pro Next
- [ ] [MODIFY] [lib/parser.py](file:///lib/parser.py)
  - Update parser implementation.
- [ ] [NEW] [tests/test_parser.py](file:///tests/test_parser.py)
  - Add parser unit tests.

**Track 2A Verification**: `pytest tests/test_parser.py`

#### Track 2B: Lexer Module
- **Model tier**: Gemini Pro Next
- [ ] [MODIFY] [lib/lexer.py](file:///lib/lexer.py)
  - Update lexer implementation.
- [ ] [NEW] [tests/test_lexer.py](file:///tests/test_lexer.py)
  - Add lexer unit tests.

**Track 2B Verification**: `pytest tests/test_lexer.py`

### Wave 3: Integration [Sequential]
- **Model tier**: Claude Fable 5.1 High
- [ ] [MODIFY] [lib/types.py](file:///lib/types.py)
  - Export parser and lexer symbols.

**Wave 3 Verification**: `pytest`

## Verification Plan
- Run `pytest`
"""


LIGHTWEIGHT_PLAN = """# Fix Off-by-One in Retry Counter

## At a glance

- **Goal:** Fix the retry loop so it stops after 3 attempts.
- **Decisions needed from you:** none.
- **Not doing:** Changing backoff timing.

## User Review Required

None.

## Open Questions

None currently blocking.

## Proposed Changes

- [ ] [MODIFY] [lib/retry.py](file:///lib/retry.py)
  - Use `< max_retries` instead of `<= max_retries`.

## Verification Plan

- Run `pytest tests/test_retry.py`
"""


class TestPlanValidator(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.temp_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_valid_plan_without_workspace(self) -> None:
        validator = PlanValidator(VALID_PLAN)
        issues = validator.validate()
        errors = [issue for issue in issues if issue.is_error]
        self.assertEqual(errors, [])

    def test_missing_required_sections(self) -> None:
        invalid_plan = "# Title Only\n\nSome body text without sections."
        validator = PlanValidator(invalid_plan)
        issues = validator.validate()
        errors = [issue for issue in issues if issue.is_error]
        self.assertTrue(len(errors) >= 3)
        messages = [e.message for e in errors]
        self.assertTrue(any("User Review" in m for m in messages))
        self.assertTrue(any("Proposed Changes" in m for m in messages))
        self.assertTrue(any("Verification Plan" in m for m in messages))

    def test_unclosed_code_block(self) -> None:
        broken_code_block = VALID_PLAN + "\n\n```python\ndef test():\n    pass\n"
        validator = PlanValidator(broken_code_block)
        issues = validator.validate()
        errors = [issue for issue in issues if issue.is_error]
        self.assertTrue(any("Unclosed code fence" in e.message for e in errors))

    def test_placeholder_detection(self) -> None:
        plan_with_todo = VALID_PLAN.replace(
            "Add `invalidate_key()` method.",
            "TODO: implement cache method later",
        )
        validator = PlanValidator(plan_with_todo, strict=False)
        issues = validator.validate()
        warnings = [issue for issue in issues if not issue.is_error]
        self.assertTrue(any("Unresolved placeholder" in w.message for w in warnings))

        strict_validator = PlanValidator(plan_with_todo, strict=True)
        strict_issues = strict_validator.validate()
        strict_errors = [issue for issue in strict_issues if issue.is_error]
        self.assertTrue(
            any("Unresolved placeholder" in e.message for e in strict_errors)
        )

    def test_checkbox_heading_is_rejected(self) -> None:
        plan_with_checkbox_heading = VALID_PLAN.replace(
            "- [ ] [MODIFY] [lib/cache.py](file:///lib/cache.py)",
            "- [ ] #### [MODIFY] [lib/cache.py](file:///lib/cache.py)",
        )
        validator = PlanValidator(plan_with_checkbox_heading)
        issues = validator.validate()
        errors = [issue for issue in issues if issue.is_error]
        self.assertTrue(
            any("task list item contains heading hashes" in e.message for e in errors)
        )

    def test_workspace_file_verification_modify_exists(self) -> None:
        lib_dir = self.workspace / "lib"
        lib_dir.mkdir(parents=True)
        (lib_dir / "cache.py").write_text("# cache", encoding="utf-8")

        plan = LIGHTWEIGHT_PLAN.replace(
            "[lib/retry.py](file:///lib/retry.py)",
            f"[{self.workspace / 'lib/cache.py'}](file://{self.workspace / 'lib/cache.py'})",
        )
        validator = PlanValidator(plan, workspace_dir=self.workspace)
        issues = validator.validate()
        errors = [issue for issue in issues if issue.is_error]
        self.assertEqual(errors, [])

    def test_workspace_file_verification_modify_hallucinated(self) -> None:
        missing_file = self.workspace / "lib/nonexistent.py"
        plan = LIGHTWEIGHT_PLAN.replace(
            "[lib/retry.py](file:///lib/retry.py)",
            f"[{missing_file}](file://{missing_file})",
        )
        validator = PlanValidator(plan, workspace_dir=self.workspace)
        issues = validator.validate()
        errors = [issue for issue in issues if issue.is_error]
        self.assertTrue(any("does not exist on disk" in e.message for e in errors))

    def test_workspace_file_verification_new_already_exists(self) -> None:
        existing_file = self.workspace / "already_exists.py"
        existing_file.write_text("# exists", encoding="utf-8")

        plan = LIGHTWEIGHT_PLAN.replace(
            "[MODIFY] [lib/retry.py](file:///lib/retry.py)",
            f"[NEW] [{existing_file}](file://{existing_file})",
        )
        validator = PlanValidator(plan, workspace_dir=self.workspace)
        issues = validator.validate()
        warnings = [issue for issue in issues if not issue.is_error]
        self.assertTrue(any("already exists on disk" in w.message for w in warnings))

    def test_parallel_wave_disjoint_tracks_valid(self) -> None:
        validator = PlanValidator(VALID_PARALLEL_WAVE_PLAN, strict=True)
        issues = validator.validate()
        self.assertEqual(issues, [])

    def test_parallel_wave_file_collision_is_error(self) -> None:
        colliding_plan = VALID_PARALLEL_WAVE_PLAN.replace(
            "- [ ] [MODIFY] [lib/lexer.py](file:///lib/lexer.py)",
            "- [ ] [MODIFY] `lib/parser.py`",
        )
        validator = PlanValidator(colliding_plan)
        issues = validator.validate()
        errors = [issue for issue in issues if issue.is_error]
        self.assertEqual(len(errors), 1)
        self.assertIn("Parallel file collision in Wave 2", errors[0].message)
        self.assertIn("Track 2A", errors[0].message)
        self.assertIn("Track 2B", errors[0].message)
        self.assertIn("lib/parser.py", errors[0].message)

    def test_inline_parallel_wave_tag_collision_is_error(self) -> None:
        inline_plan = """# Inline Wave Plan

## At a glance

- **Goal:** Split phase work across two parallel slices.
- **Decisions needed from you:** none.
- **Not doing:** Sequential fallback.

## User Review Required
None.

## Phased Implementation Plan

### Phase 2A: First Slice [Parallel: Wave 2]
- **Model tier**: Gemini Pro Next
- [ ] [MODIFY] [src/shared.py](file:///src/shared.py)
**Phase 2A Verification**: `pytest tests/test_a.py`

### Phase 2B: Second Slice [Parallel: Wave 2]
- **Model tier**: Gemini Pro Next
- [ ] [MODIFY] [src/shared.py](file:///src/shared.py)
**Phase 2B Verification**: `pytest tests/test_b.py`

## Verification Plan
- `pytest`
"""
        validator = PlanValidator(inline_plan)
        issues = validator.validate()
        errors = [issue for issue in issues if issue.is_error]
        self.assertEqual(len(errors), 1)
        self.assertIn("Parallel file collision in Wave 2", errors[0].message)
        self.assertIn("Phase 2A", errors[0].message)
        self.assertIn("Phase 2B", errors[0].message)

    def test_sequential_waves_touching_same_file_is_valid(self) -> None:
        plan = VALID_PARALLEL_WAVE_PLAN.replace(
            "- [ ] [MODIFY] [lib/parser.py](file:///lib/parser.py)",
            "- [ ] [MODIFY] [lib/types.py](file:///lib/types.py)",
        )
        validator = PlanValidator(plan, strict=True)
        issues = validator.validate()
        self.assertEqual(issues, [])

    def test_parallel_track_missing_verification_warning_and_strict_error(self) -> None:
        missing_verif_plan = VALID_PARALLEL_WAVE_PLAN.replace(
            "**Track 2B Verification**: `pytest tests/test_lexer.py`\n",
            "",
        )
        validator = PlanValidator(missing_verif_plan, strict=False)
        issues = validator.validate()
        errors = [issue for issue in issues if issue.is_error]
        warnings = [issue for issue in issues if not issue.is_error]
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("Track 2B", warnings[0].message)
        self.assertIn("missing a track verification command", warnings[0].message)

        strict_validator = PlanValidator(missing_verif_plan, strict=True)
        strict_issues = strict_validator.validate()
        strict_errors = [issue for issue in strict_issues if issue.is_error]
        self.assertEqual(len(strict_errors), 1)

    def test_at_a_glance_valid_with_diagram_and_commits_table(self) -> None:
        extra_table_rows = "\n".join(
            f"| {i} | repo | `feat: commit {i} with extra words in the table cell` | 1 |"
            for i in range(1, 35)
        )
        plan = VALID_PARALLEL_WAVE_PLAN.replace(
            "| 3 | `feat: wire exports` | 3 |",
            "| 3 | `feat: wire exports` | 3 |\n" + extra_table_rows,
        )
        validator = PlanValidator(plan)
        errors = [issue for issue in validator.validate() if issue.is_error]
        self.assertEqual(errors, [])

    def test_at_a_glance_missing_or_not_first_or_over_budget_is_error(self) -> None:
        no_glance = VALID_PLAN.replace("## At a glance\n", "## Overview\n")
        errors = [i for i in PlanValidator(no_glance).validate() if i.is_error]
        self.assertTrue(any("At a glance" in e.message for e in errors))

        reordered = (
            "# Feature\n\n## User Review Required\n\nNone.\n\n"
            + VALID_PLAN.split("# Feature: Add Cache Invalidation\n\n", 1)[1]
        )
        errors = [i for i in PlanValidator(reordered).validate() if i.is_error]
        self.assertTrue(any("first '##' section" in e.message for e in errors))

        missing_field = VALID_PARALLEL_WAVE_PLAN.replace(
            "- **Biggest risk:** Shared protocol type drift between parser and lexer.\n",
            "",
        )
        errors = [i for i in PlanValidator(missing_field).validate() if i.is_error]
        self.assertTrue(any("missing required field" in e.message for e in errors))
        self.assertTrue(any("biggest risk" in e.message for e in errors))

        long_goal = " ".join(["short words here."] * 65)
        over_budget = VALID_PLAN.replace(
            "Add cache key invalidation with a 5-minute TTL.",
            long_goal,
        )
        errors = [i for i in PlanValidator(over_budget).validate() if i.is_error]
        self.assertTrue(any("field bullets total" in e.message for e in errors))

    def test_brief_readability_positive_and_negative(self) -> None:
        long_exec_sentence = (
            "Update parser implementation with a deliberately verbose execution "
            "note that contains far more than twenty words in a single sentence "
            "so we confirm that execution sections are exempt from the brief "
            "sentence length check."
        )
        exec_plan = VALID_PARALLEL_WAVE_PLAN.replace(
            "Update parser implementation.",
            long_exec_sentence,
        )
        errors = [i for i in PlanValidator(exec_plan).validate() if i.is_error]
        self.assertEqual(errors, [])

        long_brief_sentence = (
            "This user review note is intentionally written as a single very "
            "long sentence with more than seventy words in it so that even when "
            "averaged together with the three short bullets in the At a glance "
            "section above the overall mean sentence length across all brief "
            "sections exceeds the twenty word readability threshold enforced by "
            "the implementation plan validator during automated checks."
        )
        bad_brief = LIGHTWEIGHT_PLAN.replace(
            "## User Review Required\n\nNone.",
            f"## User Review Required\n\n{long_brief_sentence}",
        )
        errors = [i for i in PlanValidator(bad_brief).validate() if i.is_error]
        self.assertTrue(
            any("Brief sections average" in e.message for e in errors),
            f"Expected readability error, got: {errors}",
        )

    def test_questions_positive_and_negative(self) -> None:
        with_question = VALID_PLAN.replace(
            "None currently blocking.",
            "### Q1: Should TTL be configurable?\n"
            "- (Recommended) A: Yes. Consequence: callers can override TTL.\n"
            "- B: No. Consequence: simpler API.\n",
        )
        errors = [i for i in PlanValidator(with_question).validate() if i.is_error]
        self.assertEqual(errors, [])

        one_option = VALID_PLAN.replace(
            "None currently blocking.",
            "### Q1: Should TTL be configurable?\n- (Recommended) A: Yes.\n",
        )
        errors = [i for i in PlanValidator(one_option).validate() if i.is_error]
        self.assertTrue(any("at least 2 options" in e.message for e in errors))

        no_rec = VALID_PLAN.replace(
            "None currently blocking.",
            "### Q1: Should TTL be configurable?\n- A: Yes.\n- B: No.\n",
        )
        errors = [i for i in PlanValidator(no_rec).validate() if i.is_error]
        self.assertTrue(any("'(Recommended)' option" in e.message for e in errors))

        two_rec = VALID_PLAN.replace(
            "None currently blocking.",
            "### Q1: Should TTL be configurable?\n"
            "- (Recommended) A: Yes.\n"
            "- (Recommended) B: No.\n",
        )
        errors = [i for i in PlanValidator(two_rec).validate() if i.is_error]
        self.assertTrue(any("'(Recommended)' option" in e.message for e in errors))

        free_bullet = VALID_PLAN.replace(
            "None currently blocking.",
            "- Should we make the TTL configurable?",
        )
        errors = [i for i in PlanValidator(free_bullet).validate() if i.is_error]
        self.assertTrue(any("free-form bullets" in e.message for e in errors))

    def test_pr_table_positive_and_negative(self) -> None:
        no_table = VALID_PARALLEL_WAVE_PLAN.replace(
            "### Commits\n\n"
            "| # | Commit | Wave |\n"
            "| ---: | :--- | :--- |\n"
            "| 1 | `feat: add protocol types` | 1 |\n"
            "| 2 | `feat: upgrade parser and lexer` | 2 |\n"
            "| 3 | `feat: wire exports` | 3 |\n\n",
            "",
        )
        errors = [i for i in PlanValidator(no_table).validate() if i.is_error]
        self.assertEqual(len(errors), 1)
        self.assertIn("no PR or Commits table", errors[0].message)

    def test_lightweight_plan_exemptions(self) -> None:
        validator = PlanValidator(LIGHTWEIGHT_PLAN, strict=True)
        self.assertTrue(validator.doc.is_lightweight)
        self.assertEqual(validator.validate(), [])

    def test_model_tiers_positive_and_negative_including_inline_track(self) -> None:
        missing_tier = VALID_PARALLEL_WAVE_PLAN.replace(
            "#### Track 2B: Lexer Module\n- **Model tier**: Gemini Pro Next\n",
            "#### Track 2B: Lexer Module\n",
        )
        errors = [i for i in PlanValidator(missing_tier).validate() if i.is_error]
        self.assertEqual(len(errors), 1)
        self.assertIn("Track 2B", errors[0].message)
        self.assertIn("Model tier", errors[0].message)

        inline_missing_tier = """# Inline Wave Plan

## At a glance

- **Goal:** Split phase work across two parallel slices.
- **Decisions needed from you:** none.
- **Not doing:** Sequential fallback.

## User Review Required
None.

## Phased Implementation Plan

### Phase 2A: First Slice [Parallel: Wave 2]
- **Model tier**: Gemini Pro Next
- [ ] [MODIFY] [src/a.py](file:///src/a.py)
**Phase 2A Verification**: `pytest tests/test_a.py`

### Phase 2B: Second Slice [Parallel: Wave 2]
- [ ] [MODIFY] [src/b.py](file:///src/b.py)
**Phase 2B Verification**: `pytest tests/test_b.py`

## Verification Plan
- `pytest`
"""
        errors = [
            i for i in PlanValidator(inline_missing_tier).validate() if i.is_error
        ]
        self.assertEqual(len(errors), 1)
        self.assertIn("Phase 2B", errors[0].message)
        self.assertIn("Model tier", errors[0].message)

    def test_parallel_orchestration_with_swarm_or_workflow_has_no_warning(self) -> None:
        validator = PlanValidator(VALID_PARALLEL_WAVE_PLAN)
        issues = validator.validate()
        self.assertEqual(issues, [])

        alt_heading_plan = VALID_PARALLEL_WAVE_PLAN.replace(
            "## Execution Strategy & Parallelization\n\n"
            "| Setting | Value |\n"
            "| :--- | :--- |\n"
            '| Orchestration | `/swarm` with 2 author workers (`workspace: "share"`) |',
            "### Execution & Verification Strategy\n\n"
            "| Setting | Value |\n"
            "| :--- | :--- |\n"
            "| Orchestration | `/run-workflow` (`parallel()` script) |",
        )
        alt_issues = PlanValidator(alt_heading_plan).validate()
        self.assertEqual(alt_issues, [])

    def test_parallel_orchestration_missing_swarm_emits_warning(self) -> None:
        no_swarm_plan = VALID_PARALLEL_WAVE_PLAN.replace(
            '| Orchestration | `/swarm` with 2 author workers (`workspace: "share"`) |',
            "| Orchestration | Sequential single agent |",
        )
        issues = PlanValidator(no_swarm_plan).validate()
        errors = [i for i in issues if i.is_error]
        warnings = [i for i in issues if not i.is_error]
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertEqual(
            warnings[0].message,
            "Plan contains parallel waves but Execution Strategy does not specify /swarm or /run-workflow orchestration.",
        )

    def test_parallel_orchestration_single_wave_sequential_plan_no_warning(
        self,
    ) -> None:
        single_wave_plan = VALID_PLAN.replace(
            "### Cache Subsystem\n",
            "### Wave 1: Cache Subsystem [Sequential]\n- **Model tier**: Claude Fable 5.1 High\n",
        )
        issues = PlanValidator(single_wave_plan).validate()
        self.assertEqual(issues, [])

    def test_cli_human_output_next_line_and_quiet(self) -> None:
        plan_file = self.workspace / "plan.md"
        plan_file.write_text(VALID_PLAN, encoding="utf-8")

        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = main([str(plan_file)])
        self.assertEqual(exit_code, 0)
        self.assertIn(NEXT_STEP_MESSAGE, buf.getvalue())

        quiet_buf = io.StringIO()
        with redirect_stdout(quiet_buf):
            quiet_code = main([str(plan_file), "--quiet"])
        self.assertEqual(quiet_code, 0)
        self.assertEqual(quiet_buf.getvalue(), "")

        bad_file = self.workspace / "bad.md"
        bad_file.write_text("# Broken\n", encoding="utf-8")
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            bad_code = main([str(bad_file)])
        self.assertEqual(bad_code, 1)
        self.assertNotIn(NEXT_STEP_MESSAGE, out_buf.getvalue())

    def test_cli_json_output(self) -> None:
        plan_file = self.workspace / "plan.md"
        plan_file.write_text(VALID_PLAN, encoding="utf-8")

        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = main([str(plan_file), "--json"])

        self.assertEqual(exit_code, 0)
        data = json.loads(buf.getvalue())
        self.assertTrue(data["valid"])
        self.assertEqual(data["errors"], [])
        self.assertEqual(data["file_action_count"], 2)
        self.assertEqual(data["next_step"], NEXT_STEP_MESSAGE)

        quiet_buf = io.StringIO()
        with redirect_stdout(quiet_buf):
            quiet_code = main([str(plan_file), "--json", "--quiet"])
        self.assertEqual(quiet_code, 0)
        quiet_data = json.loads(quiet_buf.getvalue())
        self.assertNotIn("next_step", quiet_data)

    def test_pr_table_with_narrow_gfm_separators_passes(self) -> None:
        narrow_table_plan = VALID_PARALLEL_WAVE_PLAN.replace(
            "| ---: | :--- | :--- |",
            "| --: | - | :-- |",
        )
        issues = PlanValidator(narrow_table_plan, strict=True).validate()
        self.assertEqual(issues, [])

    def test_track_verification_subheading_and_sequential_track_exempt(self) -> None:
        plan = VALID_PARALLEL_WAVE_PLAN.replace(
            "### Wave 1: Foundation [Sequential]\n",
            "### Wave 1: Foundation [Sequential]\n#### Track 1A: Substep\n",
        )
        plan = plan.replace(
            "- **Model tier**: Gemini Pro Next\n"
            "- [ ] [MODIFY] [lib/parser.py](file:///lib/parser.py)",
            "- **Model tier:** Gemini Pro Next\n"
            "- [ ] [MODIFY] [lib/parser.py](file:///lib/parser.py)",
        )
        plan = plan.replace(
            "**Track 2A Verification**: `pytest tests/test_parser.py`",
            "##### Track Verification\n- Run `pytest tests/test_parser.py`",
        )
        plan = plan.replace(
            "**Track 2B Verification**: `pytest tests/test_lexer.py`",
            "#### Track 2B Verification\n- Run `pytest tests/test_lexer.py`",
        )
        issues = PlanValidator(plan, strict=True).validate()
        self.assertEqual(issues, [])

    def test_cross_wave_new_then_modify_delete_and_rename_pass(self) -> None:
        lib_dir = self.workspace / "lib"
        tests_dir = self.workspace / "tests"
        lib_dir.mkdir(parents=True)
        tests_dir.mkdir(parents=True)
        for name in ("types.py", "parser.py", "lexer.py"):
            (lib_dir / name).write_text("# ok", encoding="utf-8")

        created_file = lib_dir / "protocol_v2.py"
        plan = VALID_PARALLEL_WAVE_PLAN
        for rel in (
            "lib/types.py",
            "lib/parser.py",
            "lib/lexer.py",
            "tests/test_parser.py",
            "tests/test_lexer.py",
        ):
            full = self.workspace / rel
            plan = plan.replace(f"[{rel}](file:///{rel})", f"[{rel}](file://{full})")

        cross_wave_plan = plan.replace(
            f"- [ ] [MODIFY] [lib/types.py](file://{lib_dir / 'types.py'})\n"
            "  - Add shared protocol types.",
            f"- [ ] [NEW] [protocol_v2.py](file://{created_file})\n"
            "  - Create shared protocol v2 types.",
        ).replace(
            f"- [ ] [MODIFY] [lib/types.py](file://{lib_dir / 'types.py'})\n"
            "  - Export parser and lexer symbols.",
            f"- [ ] [MODIFY] [protocol_v2.py](file://{created_file})\n"
            "  - Export parser and lexer symbols.\n"
            f"- [ ] [RENAME] [protocol_v2.py](file://{created_file}) -> lib/protocol.py",
        )
        issues = PlanValidator(
            cross_wave_plan, workspace_dir=self.workspace, strict=True
        ).validate()
        self.assertEqual(issues, [])

        wrong_order_plan = plan.replace(
            f"- [ ] [MODIFY] [lib/types.py](file://{lib_dir / 'types.py'})\n"
            "  - Add shared protocol types.",
            f"- [ ] [MODIFY] [protocol_v2.py](file://{created_file})\n"
            "  - Update before creation.",
        ).replace(
            f"- [ ] [MODIFY] [lib/types.py](file://{lib_dir / 'types.py'})\n"
            "  - Export parser and lexer symbols.",
            f"- [ ] [NEW] [protocol_v2.py](file://{created_file})\n"
            "  - Create too late.",
        )
        wrong_errors = [
            i
            for i in PlanValidator(
                wrong_order_plan, workspace_dir=self.workspace
            ).validate()
            if i.is_error
        ]
        self.assertEqual(len(wrong_errors), 1)
        self.assertIn(
            "File marked [MODIFY] does not exist on disk", wrong_errors[0].message
        )

    def test_rename_source_validation(self) -> None:
        lib_dir = self.workspace / "lib"
        lib_dir.mkdir(parents=True)
        existing_src = lib_dir / "old_retry.py"
        existing_src.write_text("# retry", encoding="utf-8")
        missing_src = lib_dir / "missing_retry.py"

        ok_plan = LIGHTWEIGHT_PLAN.replace(
            "- [ ] [MODIFY] [lib/retry.py](file:///lib/retry.py)",
            f"- [ ] [RENAME] [{existing_src}](file://{existing_src}) -> lib/retry.py",
        )
        self.assertEqual(
            PlanValidator(
                ok_plan, workspace_dir=self.workspace, strict=True
            ).validate(),
            [],
        )

        bad_plan = LIGHTWEIGHT_PLAN.replace(
            "- [ ] [MODIFY] [lib/retry.py](file:///lib/retry.py)",
            f"- [ ] [RENAME] [{missing_src}](file://{missing_src}) -> lib/retry.py",
        )
        errors = [
            i
            for i in PlanValidator(bad_plan, workspace_dir=self.workspace).validate()
            if i.is_error
        ]
        self.assertEqual(len(errors), 1)
        self.assertIn("File marked [RENAME] does not exist on disk", errors[0].message)

    def test_prose_action_tokens_ignored(self) -> None:
        lib_dir = self.workspace / "lib"
        lib_dir.mkdir(parents=True)
        existing = lib_dir / "retry.py"
        existing.write_text("# retry", encoding="utf-8")

        plan = LIGHTWEIGHT_PLAN.replace(
            "[lib/retry.py](file:///lib/retry.py)",
            f"[lib/retry.py](file://{existing})",
        ).replace(
            "- Use `< max_retries` instead of `<= max_retries`.",
            "- Do not [MODIFY] decision_1_catalogs or [DELETE] legacy_table in this step.\n"
            "In prose, [NEW] phantom_file.py and [RENAME] ghost.py are descriptive text.",
        )
        validator = PlanValidator(plan, workspace_dir=self.workspace, strict=True)
        self.assertEqual(validator.doc.file_action_count, 1)
        self.assertEqual(validator.validate(), [])

    def test_glance_question_consistency_positive_negative_and_post_decision(
        self,
    ) -> None:
        consistent_open = VALID_PLAN.replace(
            "- **Decisions needed from you:** none.",
            "- **Decisions needed from you:** 1 (Q1).",
        ).replace(
            "None currently blocking.",
            "### Q1: Should TTL be configurable?\n"
            "- (Recommended) A: Yes. Consequence: flexible.\n"
            "- B: No. Consequence: simpler.\n",
        )
        self.assertEqual(PlanValidator(consistent_open, strict=True).validate(), [])

        post_decision = VALID_PLAN.replace(
            "- **Decisions needed from you:** none.",
            "- **Decisions needed from you:** none. Recorded (Q1).",
        )
        self.assertEqual(PlanValidator(post_decision, strict=True).validate(), [])

        zero_prefix = VALID_PLAN.replace(
            "- **Decisions needed from you:** none.",
            "- **Decisions needed from you:** 0 (Q1 resolved under Decisions).",
        )
        self.assertEqual(PlanValidator(zero_prefix, strict=True).validate(), [])

        stale_none = VALID_PLAN.replace(
            "None currently blocking.",
            "### Q1: Should TTL be configurable?\n"
            "- (Recommended) A: Yes. Consequence: flexible.\n"
            "- B: No. Consequence: simpler.\n",
        )
        issues = PlanValidator(stale_none, strict=False).validate()
        self.assertEqual([i for i in issues if i.is_error], [])
        self.assertEqual(len(issues), 1)
        self.assertIn("Decisions needed from you", issues[0].message)
        strict_errors = [
            i for i in PlanValidator(stale_none, strict=True).validate() if i.is_error
        ]
        self.assertEqual(len(strict_errors), 1)

        stale_qref = VALID_PLAN.replace(
            "- **Decisions needed from you:** none.",
            "- **Decisions needed from you:** 2 (Q1, Q2).",
        )
        qref_issues = PlanValidator(stale_qref, strict=False).validate()
        self.assertEqual([i for i in qref_issues if i.is_error], [])
        self.assertEqual(len(qref_issues), 1)
        self.assertIn("0 open questions", qref_issues[0].message)
        self.assertEqual(
            len(
                [
                    i
                    for i in PlanValidator(stale_qref, strict=True).validate()
                    if i.is_error
                ]
            ),
            1,
        )

    def test_prose_rules_em_dash_prose_vs_code(self) -> None:
        backticked = VALID_PLAN.replace(
            "Add `invalidate_key()` method.",
            "Strip inline em-dashes (`\u2014`) and double-backticked `` `\u2014` `` tokens.\n\n"
            "```python\nX = '\u2014'\n```",
        )
        self.assertEqual(PlanValidator(backticked, strict=True).validate(), [])

        prose_em_dash = VALID_PLAN.replace(
            "Add `invalidate_key()` method.",
            "Add `invalidate_key()` \u2014 invalidating `stale` entries immediately.",
        )
        default_issues = PlanValidator(prose_em_dash, strict=False).validate()
        self.assertEqual([i for i in default_issues if i.is_error], [])
        self.assertEqual(len(default_issues), 1)
        self.assertIn("Em-dash", default_issues[0].message)

        strict_issues = PlanValidator(prose_em_dash, strict=True).validate()
        self.assertEqual(len([i for i in strict_issues if i.is_error]), 1)

    def test_prose_rules_default_option_prefix_vs_legitimate_prose(self) -> None:
        legit_default = VALID_PLAN.replace(
            "- **Decisions needed from you:** none.",
            "- **Decisions needed from you:** 1 (Q1).",
        ).replace(
            "None currently blocking.",
            "### Q1: Should the cache be on by default?\n"
            "- (Recommended) A: Keep the 5-minute TTL default.\n"
            "- B: Disable by default.\n",
        )
        self.assertEqual(PlanValidator(legit_default, strict=True).validate(), [])

        bad_default_prefix = legit_default.replace(
            "- B: Disable by default.\n",
            "- B: Disable by default.\n- (default) C: Use a 1-minute TTL.\n",
        )
        warnings = PlanValidator(bad_default_prefix, strict=False).validate()
        self.assertEqual([i for i in warnings if i.is_error], [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("'(default)' prefix", warnings[0].message)

        strict_errors = [
            i
            for i in PlanValidator(bad_default_prefix, strict=True).validate()
            if i.is_error
        ]
        self.assertEqual(len(strict_errors), 1)

    def test_repeatable_workspace_cli_and_extra_workspaces(self) -> None:
        ws2_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, ws2_dir, True)

        (self.workspace / "lib").mkdir(parents=True)
        (self.workspace / "lib/cache.py").write_text("# ws1", encoding="utf-8")
        (ws2_dir / "pkg").mkdir(parents=True)
        ws2_file = ws2_dir / "pkg/remote.py"
        ws2_file.write_text("# ws2", encoding="utf-8")

        multi_ws_plan = VALID_PLAN.replace(
            "- [ ] [MODIFY] [lib/cache.py](file:///lib/cache.py)",
            "- [ ] [MODIFY] `lib/cache.py`",
        ).replace(
            "- [ ] [NEW] [lib/ttl.py](file:///lib/ttl.py)",
            f"- [ ] [MODIFY] [pkg/remote.py](file://{ws2_file})",
        )
        plan_file = self.workspace / "multi_plan.md"
        plan_file.write_text(multi_ws_plan, encoding="utf-8")

        out_buf = io.StringIO()
        with redirect_stdout(out_buf):
            code = main(
                [
                    str(plan_file),
                    "--workspace",
                    str(self.workspace),
                    "--workspace",
                    str(ws2_dir),
                    "--strict",
                ]
            )
        self.assertEqual(code, 0)

        err_buf = io.StringIO()
        with redirect_stderr(err_buf):
            bad_ws_code = main(
                [
                    str(plan_file),
                    "--workspace",
                    str(self.workspace),
                    "--workspace",
                    str(self.workspace / "does_not_exist"),
                ]
            )
        self.assertEqual(bad_ws_code, 2)

    def test_out_of_workspace_parallel_track_warning_and_strict_error(self) -> None:
        ws2_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, ws2_dir, True)

        (self.workspace / "lib").mkdir(parents=True)
        (self.workspace / "tests").mkdir(parents=True)
        (self.workspace / "lib/types.py").write_text("# types", encoding="utf-8")
        (self.workspace / "lib/parser.py").write_text("# parser", encoding="utf-8")
        (ws2_dir / "lib").mkdir(parents=True)
        (ws2_dir / "tests").mkdir(parents=True)
        (ws2_dir / "lib/lexer.py").write_text("# lexer", encoding="utf-8")

        plan = (
            VALID_PARALLEL_WAVE_PLAN.replace(
                "[lib/types.py](file:///lib/types.py)",
                f"[lib/types.py](file://{self.workspace / 'lib/types.py'})",
            )
            .replace(
                "[lib/parser.py](file:///lib/parser.py)",
                f"[lib/parser.py](file://{self.workspace / 'lib/parser.py'})",
            )
            .replace(
                "[tests/test_parser.py](file:///tests/test_parser.py)",
                f"[tests/test_parser.py](file://{self.workspace / 'tests/test_parser.py'})",
            )
            .replace(
                "[lib/lexer.py](file:///lib/lexer.py)",
                f"[lib/lexer.py](file://{ws2_dir / 'lib/lexer.py'})",
            )
            .replace(
                "[tests/test_lexer.py](file:///tests/test_lexer.py)",
                f"[tests/test_lexer.py](file://{self.workspace / 'tests/test_lexer.py'})",
            )
        )

        issues = PlanValidator(
            plan,
            workspace_dir=self.workspace,
            strict=False,
            extra_workspaces=(ws2_dir,),
        ).validate()
        self.assertEqual([i for i in issues if i.is_error], [])
        self.assertEqual(len(issues), 1)
        self.assertIn("Track 2B", issues[0].message)
        self.assertIn("outside primary workspace", issues[0].message)

        strict_issues = PlanValidator(
            plan,
            workspace_dir=self.workspace,
            strict=True,
            extra_workspaces=(ws2_dir,),
        ).validate()
        self.assertEqual(len([i for i in strict_issues if i.is_error]), 1)

    def test_relative_path_resolves_against_extra_workspaces(self) -> None:
        ws2_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, ws2_dir, ignore_errors=True)
        (ws2_dir / "pkg").mkdir(parents=True)
        (ws2_dir / "pkg/remote.py").write_text("# remote", encoding="utf-8")

        plan = LIGHTWEIGHT_PLAN.replace(
            "[lib/retry.py](file:///lib/retry.py)", "`pkg/remote.py`"
        )
        issues = PlanValidator(
            plan,
            workspace_dir=self.workspace,
            strict=True,
            extra_workspaces=(ws2_dir,),
        ).validate()
        self.assertEqual(issues, [])

    def test_validate_does_not_reparse_unchanged_content(self) -> None:
        from plan_model import parse_plan as real_parse

        with mock.patch("validate_plan.parse_plan", wraps=real_parse) as mock_parse:
            validator = PlanValidator(VALID_PLAN)
            self.assertEqual(mock_parse.call_count, 1)
            validator.validate()
            self.assertEqual(mock_parse.call_count, 1)
            validator.content = LIGHTWEIGHT_PLAN
            validator.validate()
            self.assertEqual(mock_parse.call_count, 2)


if __name__ == "__main__":
    unittest.main()
