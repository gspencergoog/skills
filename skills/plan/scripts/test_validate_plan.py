#!/usr/bin/env python3
"""test_validate_plan.py - Unit tests for validate_plan.py"""

import json
import os
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
- [ ] #### [MODIFY] [lib/cache.py](file:///lib/cache.py)
  - Add `invalidate_key()` method.
- [ ] #### [NEW] [lib/ttl.py](file:///lib/ttl.py)
  - Implement time-to-live helper.

## Verification Plan

### Automated Tests
- Run `pytest tests/test_cache.py`
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
        # Should flag missing User Review, Proposed Changes, and Verification Plan
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

        # Under strict mode, placeholders should be errors
        strict_validator = PlanValidator(plan_with_todo, strict=True)
        strict_issues = strict_validator.validate()
        strict_errors = [issue for issue in strict_issues if issue.is_error]
        self.assertTrue(any("Unresolved placeholder" in e.message for e in strict_errors))

    def test_workspace_file_verification_modify_exists(self) -> None:
        # Create lib/cache.py in workspace
        lib_dir = self.workspace / "lib"
        lib_dir.mkdir(parents=True)
        (lib_dir / "cache.py").write_text("# cache", encoding="utf-8")

        plan = f"""# Test Plan

## User Review Required
None.

## Proposed Changes
- [ ] #### [MODIFY] [{self.workspace / "lib/cache.py"}](file://{self.workspace / "lib/cache.py"})
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
- [ ] #### [MODIFY] [{missing_file}](file://{missing_file})
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
- [ ] #### [NEW] [{existing_file}](file://{existing_file})
  - Create duplicate.

## Verification Plan
Run tests.
"""
        validator = PlanValidator(plan, workspace_dir=self.workspace)
        issues = validator.validate()
        warnings = [issue for issue in issues if not issue.is_error]
        self.assertTrue(any("already exists on disk" in w.message for w in warnings))

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

