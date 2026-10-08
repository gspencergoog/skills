#!/usr/bin/env python3
"""Unit tests for lint_review.py."""

import pathlib
import tempfile
import unittest

from lint_review import check_report, main, parse_report

CLEAN_REPORT = """\
## Summary
Reviewed 2 files. Citation audit: 1 checked, 0 dropped. Flags: `--verify`.

## Changed Files Summary
- `split_diff.py`: Updated `_fits_limits` to measure UTF-8 bytes.

## Review Comments (Ordered by Severity)

### H1. `_fits_limits` measures code points instead of UTF-8 bytes
- **File**: `split_diff.py`
- **Line**: `50`
- **Severity**: `high`
- **Evidence**: `Executed` — `python3 -m unittest` — chunk reached 48,120 bytes
- **Body**: `len(text)` counts characters rather than UTF-8 bytes.
- **Why**: Multibyte diffs exceed `view_file`'s 46,080-byte read cap and truncate.
- **Suggestion**:
```python
return _byte_len(text) <= max_bytes and _line_count(text) <= max_lines
```

### M1. Weak assertion in multibyte boundary test
- **File**: `test_split_diff.py`
- **Line**: `354`
- **Severity**: `medium`
- **Evidence**: Read — `self.assertIsInstance(chunks, list)` (`test_split_diff.py:354`)
- **Body**: The test checks only the return type.
- **Why**: Regressions in byte-limit splitting pass the test suite unnoticed.
- **Suggestion**: Assert `len(chunks) == 2` and check each chunk's byte size.

## Questions

### Q1. Can `split_diff_grouped` receive surrogate-escaped text on stdin?
- **File**: `split_diff.py`
- **Line**: `41`
- **Evidence**: `Speculation` — `sys.stdin.read()` uses the locale encoding
- **Why**: If binary diffs contain non-UTF-8 bytes, `encode("utf-8")` raises `UnicodeEncodeError` (`medium`).
- **To settle**: Check how `git diff` binary paths are piped into `split_diff.py` in `SKILL.md`.

## Checked and Found Clean
- Line-count splitting boundary in `_fits_limits`: `Read` — `_line_count(text) <= max_lines` unchanged at `split_diff.py:50`
- Manifest total byte count: `Read` — `split_diff.py:161` still calls `_byte_len(diff_content)`

## Recommendations
- Restore `_byte_len(text)` in `_fits_limits`.
"""


class TestLintReview(unittest.TestCase):
    """Tests for parse_report, check_report, and CLI exit codes."""

    def test_clean_report_passes(self):
        report = parse_report(CLEAN_REPORT)
        self.assertEqual(len(report.findings), 2)
        self.assertEqual(len(report.questions), 1)
        self.assertEqual(len(report.clean_lines), 2)
        self.assertEqual(check_report(report), [])

    def test_document_finding_with_category_passes(self):
        doc_report = """\
## Review Comments (Ordered by Severity)
### C1. Contradictory retry limits
- **File**: `spec.md`
- **Line**: `42`
- **Severity**: `critical`
- **Category**: `inconsistency`
- **Evidence**: `Read` — "MUST retry 3 times" (`spec.md:42`) vs "MUST NOT retry" (`spec.md:108`)
- **Body**: Section 2 and Section 5 contradict on retry behavior.
- **Why**: Two compliant implementations will not interoperate on transient errors.
- **Suggestion**: Remove the prohibition in Section 5.
"""
        self.assertEqual(check_report(parse_report(doc_report)), [])

    def test_empty_findings_with_clean_section_passes(self):
        empty_report = """\
## Summary
No defects found.

## Review Comments (Ordered by Severity)
None.

## Checked and Found Clean
- Off-by-one in chunk splitter: `Executed` — `python3 -m unittest` passed 53 tests
"""
        self.assertEqual(check_report(parse_report(empty_report)), [])

    def test_unparsed_review_comments_fails(self):
        bad = """\
## Review Comments (Ordered by Severity)
- **File**: `foo.py`
- **Severity**: `high`
"""
        rules = [v.rule for v in check_report(parse_report(bad))]
        self.assertIn("review-comments-unparsed", rules)

    def test_ids_sequential_and_order_fails(self):
        bad = """\
## Review Comments (Ordered by Severity)
### M1. First medium
- **Severity**: `medium`
- **Evidence**: `Read` — `foo.py:1`
- **Why**: Breaks callers.

### H2. High after medium and skipping H1
- **Severity**: `high`
- **Evidence**: `Read` — `foo.py:2`
- **Why**: Corrupts state.
"""
        rules = [v.rule for v in check_report(parse_report(bad))]
        self.assertEqual(rules.count("ids-sequential"), 2)

    def test_severity_matches_id_fails(self):
        bad = """\
## Review Comments (Ordered by Severity)
### H1. Mismatched severity
- **Severity**: `low`
- **Evidence**: `Read` — `foo.py:1`
- **Why**: Minor issue.
"""
        rules = [v.rule for v in check_report(parse_report(bad))]
        self.assertIn("severity-matches-id", rules)

    def test_evidence_required_and_recalled_and_cap(self):
        bad = """\
## Review Comments (Ordered by Severity)
### C1. Speculative critical
- **Severity**: `critical`
- **Evidence**: `Speculation` — might fail under load
- **Why**: Service outage.

### H1. Recalled high
- **Severity**: `high`
- **Evidence**: `Recalled` — docs say this is unsafe
- **Why**: Crash on startup.

### M1. Missing tier
- **Severity**: `medium`
- **Evidence**: some quote without a tier
- **Why**: Wrong return value.
"""
        rules = [v.rule for v in check_report(parse_report(bad))]
        self.assertIn("severity-cap", rules)
        self.assertIn("evidence-recalled", rules)
        self.assertIn("evidence-required", rules)

    def test_why_required_fails(self):
        bad = """\
## Review Comments (Ordered by Severity)
### L1. No why field
- **Severity**: `low`
- **Evidence**: `Read` — `foo.py:1`
- **Body**: Unused variable.
"""
        rules = [v.rule for v in check_report(parse_report(bad))]
        self.assertIn("why-required", rules)

    def test_question_fields_and_max_3(self):
        questions = "\n".join(
            f"""\
### Q{i}. Question {i}?
- **Evidence**: `Speculation` — unchecked
- **Why**: Could fail.
- **To settle**: Read `bar.py`."""
            for i in range(1, 5)
        )
        bad = f"## Questions\n{questions}\n"
        rules = [v.rule for v in check_report(parse_report(bad))]
        self.assertIn("question-fields", rules)

        missing_settle = """\
## Questions
### Q1. Missing settle?
- **Evidence**: `Speculation` — unchecked
- **Why**: Could fail.
"""
        rules2 = [v.rule for v in check_report(parse_report(missing_settle))]
        self.assertIn("question-fields", rules2)

    def test_clean_line_tier_and_max_10(self):
        lines = "\n".join(f"- Hypothesis {i}: `Read` — `foo.py:{i}`" for i in range(11))
        bad = f"## Checked and Found Clean\n{lines}\n- Untiered line without proof\n"
        rules = [v.rule for v in check_report(parse_report(bad))]
        self.assertIn("clean-max-10", rules)
        self.assertIn("clean-line-tier", rules)

    def test_verified_removed_fails(self):
        bad = CLEAN_REPORT + "\n- **Verified**: `pytest` — passed\n"
        rules = [v.rule for v in check_report(parse_report(bad))]
        self.assertIn("verified-removed", rules)

    def test_main_exit_codes(self):
        self.assertEqual(main([]), 2)
        self.assertEqual(main(["/nonexistent/review_results.md"]), 2)
        with tempfile.TemporaryDirectory() as tmp:
            clean_path = pathlib.Path(tmp) / "clean.md"
            clean_path.write_text(CLEAN_REPORT, encoding="utf-8")
            self.assertEqual(main([str(clean_path)]), 0)

            bad_path = pathlib.Path(tmp) / "bad.md"
            bad_path.write_text("## Review Comments\nSome unparsed text\n", encoding="utf-8")
            self.assertEqual(main([str(bad_path)]), 1)


if __name__ == "__main__":
    unittest.main()
