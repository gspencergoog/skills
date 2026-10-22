#!/usr/bin/env python3
"""test_validate_plan.py - Unit tests for validate_plan.py"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from validate_plan import PlanValidator, main

VALID_PLAN = """# Feature: Add Cache Invalidation

## User Review Required

> [!IMPORTANT]
> This change introduces a 5-minute TTL default.

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

## User Review Required
None.

## Phased Implementation Plan

### Wave 1: Foundation [Sequential]
- [ ] [MODIFY] [lib/types.py](file:///lib/types.py)
  - Add shared protocol types.

**Wave 1 Verification**: `pytest tests/test_types.py`

### Wave 2: Core Modules [Parallel]

#### Track 2A: Parser Module
- [ ] [MODIFY] [lib/parser.py](file:///lib/parser.py)
  - Update parser implementation.
- [ ] [NEW] [tests/test_parser.py](file:///tests/test_parser.py)
  - Add parser unit tests.

**Track 2A Verification**: `pytest tests/test_parser.py`

#### Track 2B: Lexer Module
- [ ] [MODIFY] [lib/lexer.py](file:///lib/lexer.py)
  - Update lexer implementation.
- [ ] [NEW] [tests/test_lexer.py](file:///tests/test_lexer.py)
  - Add lexer unit tests.

**Track 2B Verification**: `pytest tests/test_lexer.py`

### Wave 3: Integration [Sequential]
- [ ] [MODIFY] [lib/types.py](file:///lib/types.py)
  - Export parser and lexer symbols.

**Wave 3 Verification**: `pytest`

## Verification Plan
- Run `pytest`
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

        plan = f"""# Test Plan

## User Review Required
None.

## Proposed Changes
- [ ] [MODIFY] [{self.workspace / "lib/cache.py"}](file://{self.workspace / "lib/cache.py"})
  - Edit cache logic.

## Verification Plan
Run tests.
"""
        validator = PlanValidator(plan, workspace_dir=self.workspace)
        issues = validator.validate()
        errors = [issue for issue in issues if issue.is_error]
        self.assertEqual(errors, [])

    def test_workspace_file_verification_modify_hallucinated(self) -> None:
        missing_file = self.workspace / "lib/nonexistent.py"
        plan = f"""# Test Plan

## User Review Required
None.

## Proposed Changes
- [ ] [MODIFY] [{missing_file}](file://{missing_file})
  - Edit nonexistent file.

## Verification Plan
Run tests.
"""
        validator = PlanValidator(plan, workspace_dir=self.workspace)
        issues = validator.validate()
        errors = [issue for issue in issues if issue.is_error]
        self.assertTrue(any("does not exist on disk" in e.message for e in errors))

    def test_workspace_file_verification_new_already_exists(self) -> None:
        existing_file = self.workspace / "already_exists.py"
        existing_file.write_text("# exists", encoding="utf-8")

        plan = f"""# Test Plan

## User Review Required
None.

## Proposed Changes
- [ ] [NEW] [{existing_file}](file://{existing_file})
  - Create duplicate.

## Verification Plan
Run tests.
"""
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

## User Review Required
None.

## Phased Implementation Plan

### Phase 2A: First Slice [Parallel: Wave 2]
- [ ] [MODIFY] [src/shared.py](file:///src/shared.py)
**Phase 2A Verification**: `pytest tests/test_a.py`

### Phase 2B: Second Slice [Parallel: Wave 2]
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
        # In VALID_PARALLEL_WAVE_PLAN, Wave 1 [Sequential] and Wave 3 [Sequential]
        # both modify lib/types.py. Even if Track 2A also touched lib/types.py,
        # Sequential Wave 1 and Wave 3 must not collide with Wave 2.
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

    def test_cli_json_output(self) -> None:
        plan_file = self.workspace / "plan.md"
        plan_file.write_text(VALID_PLAN, encoding="utf-8")

        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = main([str(plan_file), "--json"])

        self.assertEqual(exit_code, 0)
        data = json.loads(buf.getvalue())
        self.assertTrue(data["valid"])
        self.assertEqual(data["errors"], [])


if __name__ == "__main__":
    unittest.main()
