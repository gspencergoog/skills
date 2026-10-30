"""Unit tests for lint_review.py."""

import importlib.util
import json
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
            bad_path.write_text(
                "## Review Comments\nSome unparsed text\n", encoding="utf-8"
            )
            self.assertEqual(main([str(bad_path)]), 1)

    def test_export_json_valid_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            clean_path = pathlib.Path(tmp) / "clean.md"
            clean_path.write_text(CLEAN_REPORT, encoding="utf-8")
            json_path = pathlib.Path(tmp) / "output.json"

            exit_code = main([str(clean_path), "--export-json", str(json_path)])
            self.assertEqual(exit_code, 0)
            self.assertTrue(json_path.is_file())

            data = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(data["review_summary"]["verdict"], "REQUEST_CHANGES")
            self.assertEqual(len(data["findings"]), 2)

            h1 = data["findings"][0]
            self.assertEqual(h1["id"], "H1")
            self.assertEqual(h1["severity"], "high")
            self.assertEqual(h1["file_path"], "split_diff.py")
            self.assertEqual(h1["line_start"], 50)
            self.assertEqual(h1["line_end"], 50)
            self.assertEqual(h1["evidence_tier"], "Executed")
            self.assertIn("48,120 bytes", h1["evidence_detail"])
            self.assertEqual(
                h1["description"],
                "`_fits_limits` measures code points instead of UTF-8 bytes",
            )
            self.assertIn("Multibyte diffs exceed", h1["why"])
            self.assertIn("return _byte_len(text) <= max_bytes", h1["suggestion"])
            self.assertIn(
                "`len(text)` counts characters rather than UTF-8 bytes. "
                "Multibyte diffs exceed `view_file`'s 46,080-byte read cap and truncate.",
                h1["draft_comment"],
            )
            self.assertNotIn("**Impact**:", h1["draft_comment"])
            self.assertIn("```suggestion", h1["draft_comment"])

            self.assertEqual(len(data["questions"]), 1)
            q1 = data["questions"][0]
            self.assertEqual(q1["id"], "Q1")
            self.assertEqual(q1["severity"], "question")
            self.assertEqual(q1["file_path"], "split_diff.py")
            self.assertEqual(q1["line_start"], 41)
            self.assertEqual(q1["line_end"], 41)
            self.assertEqual(q1["evidence_tier"], "Speculation")
            self.assertIn("Check how `git diff`", q1["to_settle"])
            self.assertEqual(
                q1["draft_comment"],
                "Can `split_diff_grouped` receive surrogate-escaped text on stdin? "
                'If binary diffs contain non-UTF-8 bytes, `encode("utf-8")` raises `UnicodeEncodeError`.',
            )
            self.assertNotIn("**Question**", q1["draft_comment"])
            self.assertNotIn("**Evidence**", q1["draft_comment"])
            self.assertNotIn("**To Settle**", q1["draft_comment"])

            self.assertEqual(len(data["review_summary"]["checked_clean"]), 2)

    def test_export_json_invalid_report_does_not_write_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_path = pathlib.Path(tmp) / "bad.md"
            bad_path.write_text(
                "## Review Comments\nSome unparsed text\n", encoding="utf-8"
            )
            json_path = pathlib.Path(tmp) / "output.json"

            exit_code = main([str(bad_path), "--export-json", str(json_path)])
            self.assertEqual(exit_code, 1)
            self.assertFalse(json_path.exists())

    def test_export_json_preserves_indentation(self):
        report_text = """\
## Summary
Verdict: Minor revisions needed.

## Review Comments (Ordered by Severity)

### M1. Indented suggestion
- **File**: `example.py`
- **Line**: `10`
- **Severity**: `medium`
- **Evidence**: `Read` - file inspection
- **Body**: Nested block needs indentation.
- **Why**: Broken syntax.
- **Suggestion**:
```suggestion
        if not items:
            return []
```
"""
        with tempfile.TemporaryDirectory() as tmp:
            doc_path = pathlib.Path(tmp) / "report.md"
            doc_path.write_text(report_text, encoding="utf-8")
            json_path = pathlib.Path(tmp) / "output.json"
            exit_code = main([str(doc_path), "--export-json", str(json_path)])
            self.assertEqual(exit_code, 0)
            data = json.loads(json_path.read_text(encoding="utf-8"))
            sugg = data["findings"][0]["draft_comment"]
            self.assertIn(
                "```suggestion\n        if not items:\n            return []\n```", sugg
            )

    def test_export_json_overview_strips_internal_process_lines(self):
        report_text = """\
## Summary
Changes look solid.
Citation audit: 4 checked, 0 dropped. Flags: `--max --claude`.
`--verify` limited to tier 1: PR author is not a repo collaborator.
1 disputed (1 withdrawn, 0 upheld).

## Review Comments (Ordered by Severity)

### L1. Minor typo
- **File**: `doc.md`
- **Line**: `1`
- **Severity**: `low`
- **Evidence**: `Read` - typo in title
- **Body**: Fix typo.
- **Why**: Better readability.
- **Suggestion**: Fix word.
"""
        with tempfile.TemporaryDirectory() as tmp:
            doc_path = pathlib.Path(tmp) / "report.md"
            doc_path.write_text(report_text, encoding="utf-8")
            json_path = pathlib.Path(tmp) / "output.json"
            exit_code = main([str(doc_path), "--export-json", str(json_path)])
            self.assertEqual(exit_code, 0)
            data = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(data["review_summary"]["overview"], "Changes look solid.")
            self.assertEqual(data["review_summary"]["citation_audit"]["checked"], 4)
            self.assertEqual(data["review_summary"]["flags"], ["--max", "--claude"])

    def test_parse_verdict_word_boundaries(self):
        from lint_review import Finding, _parse_verdict

        # Contains 'already' (should not match 'ready') and 'no critical blockers'
        text = "These changes are already tested. There are no critical blockers in the implementation."
        # No high/critical findings -> should fall back to COMMENT because there is a medium finding
        findings = [Finding(id="M1", severity="medium", fields={}, line=10)]
        self.assertEqual(_parse_verdict(text, findings), "COMMENT")

        # Explicit verdict label
        text_ready = "Review complete. Verdict: Ready for merge."
        self.assertEqual(_parse_verdict(text_ready, findings), "APPROVE")

        text_blocker = "Review complete. Verdict: Critical blockers present."
        self.assertEqual(_parse_verdict(text_blocker, []), "REQUEST_CHANGES")

    def test_export_json_preserves_lead_in_text_before_fence(self):
        report_text = """\
## Summary
Verdict: Ready.

## Review Comments (Ordered by Severity)

### M1. Helper function
- **File**: `helper.py`
- **Line**: `15`
- **Severity**: `medium`
- **Evidence**: `Read` - missing helper
- **Body**: Extract helper.
- **Why**: Redundant code.
- **Suggestion**: Replace with the following helper:
```python
def helper():
    return 42
```
"""
        data = self._export(report_text)
        comment = data["findings"][0]["draft_comment"]
        # Lead-in text precedes the block and does not remove committability.
        self.assertIn(
            "Replace with the following helper:\n\n```suggestion\ndef helper():\n    return 42\n```",
            comment,
        )

    def _export(self, report_text: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            doc_path = pathlib.Path(tmp) / "report.md"
            doc_path.write_text(report_text, encoding="utf-8")
            json_path = pathlib.Path(tmp) / "output.json"
            exit_code = main([str(doc_path), "--export-json", str(json_path)])
            self.assertEqual(exit_code, 0)
            return json.loads(json_path.read_text(encoding="utf-8"))

    @staticmethod
    def _finding_report(suggestion_block: str) -> str:
        return f"""\
## Summary
Verdict: Minor revisions needed.

## Review Comments (Ordered by Severity)

### M1. Example finding
- **File**: `example.py`
- **Line**: `10`
- **Severity**: `medium`
- **Evidence**: `Read` - file inspection
- **Body**: Something is off.
- **Why**: It breaks.
- **Suggestion**:{suggestion_block}
"""

    def test_export_json_indented_fence_keeps_relative_indentation(self):
        # Fence opener at 4 spaces; body has a line shallower than the fence
        # and a line deeper than it. Relative nesting must survive.
        report_text = self._finding_report(
            "\n    ```suggestion\n  if x:\n          y()\n    ```"
        )
        data = self._export(report_text)
        self.assertEqual(data["findings"][0]["suggestion"], "if x:\n      y()")

    def test_export_json_bare_fence_becomes_suggestion(self):
        report_text = self._finding_report("\n```\nx = compute()\n```")
        data = self._export(report_text)
        self.assertIn(
            "```suggestion\nx = compute()\n```", data["findings"][0]["draft_comment"]
        )

    def test_export_json_bare_fence_with_lead_in_becomes_suggestion(self):
        report_text = self._finding_report(
            " Use this instead:\n```\nif not items:\n    return []\n```"
        )
        data = self._export(report_text)
        self.assertIn(
            "Use this instead:\n\n```suggestion\nif not items:\n    return []\n```",
            data["findings"][0]["draft_comment"],
        )

    def test_export_json_diff_fence_is_not_committable(self):
        report_text = self._finding_report(
            "\n```diff\n-    send()\n+    await send()\n```"
        )
        data = self._export(report_text)
        comment = data["findings"][0]["draft_comment"]
        self.assertIn("```diff\n-    send()\n+    await send()\n```", comment)
        self.assertNotIn("```suggestion", comment)

    def test_second_fence_under_suggestion_fails_lint(self):
        # "Replace X with Y": the exporter cannot tell X from Y, so lint
        # fails and the orchestrator rewrites the Suggestion as one block.
        report_text = self._finding_report(
            "\n```python\nold_call()\n```\nwith:\n```python\nnew_call()\n```"
        )
        with tempfile.TemporaryDirectory() as tmp:
            doc_path = pathlib.Path(tmp) / "report.md"
            doc_path.write_text(report_text, encoding="utf-8")
            json_path = pathlib.Path(tmp) / "output.json"
            exit_code = main([str(doc_path), "--export-json", str(json_path)])
            self.assertEqual(exit_code, 1)
            self.assertFalse(json_path.exists())
        report = parse_report(report_text)
        rules = {v.rule for v in check_report(report)}
        self.assertIn("suggestion-single-fence", rules)

    def test_export_json_prose_suggestion_is_not_fenced(self):
        report_text = self._finding_report(
            " Return the error to the caller instead of logging it."
        )
        data = self._export(report_text)
        comment = data["findings"][0]["draft_comment"]
        self.assertNotIn("```suggestion", comment)
        self.assertIn("Return the error to the caller instead of logging it.", comment)

    def test_clean_overview_keeps_prose_and_strips_process_sentences(self):
        from lint_review import _clean_overview

        cases = {
            # "Flags:" inside prose is not a process sentence; the closing
            # Verdict label is a process label and is stripped.
            "Adds CLI parsing. New Flags: `--dry-run` and `--json` are wired into argparse. "
            "Verdict: Minor Revisions Needed.": "Adds CLI parsing. New Flags: `--dry-run` and `--json` are wired into argparse.",
            # Nested parentheses inside the citation audit parenthetical.
            "Refactors the parser. Citation audit: 3 checked, 1 dropped (H2 cited `parse()` "
            "which does not exist). Flags: `--max`.": "Refactors the parser.",
            # Prose after the flags sentence survives; Verdict is stripped.
            "Verdict: Ready. Flags: `--claude`. The remaining risk is the untested retry path.": "The remaining risk is the untested retry path.",
            # "none required" variant is stripped.
            "Looks good. Citation audit: none required (no critical or high findings).": "Looks good.",
            # Two paragraphs are preserved as two paragraphs.
            "First paragraph.\n\nSecond paragraph. Flags: `--fast`.": "First paragraph.\n\nSecond paragraph.",
            # One backtick span per flag, comma separated.
            "Refactors X. Flags: `--json`, `--verify`.": "Refactors X.",
            # One backtick span per flag, space separated.
            "Refactors X. Flags: `--json` `--verify`.": "Refactors X.",
            # Trailing parenthetical after the flag list.
            "Refactors X. Flags: `--fast` (`--claude` ignored).": "Refactors X.",
            # Prose after the trust-limit sentence survives.
            "Refactors X. `--verify` limited to tier 1: PR author is not a repo collaborator. "
            "The retry path remains untested.": "Refactors X. The retry path remains untested.",
            # A paragraph that starts with a process sentence keeps its prose
            # as its own paragraph.
            "First paragraph.\n\nCitation audit: 3 checked, 0 dropped. Second paragraph prose.": "First paragraph.\n\nSecond paragraph prose.",
            # A paragraph that is only process text disappears entirely.
            "First paragraph.\n\nFlags: `--max`.\n\nThird paragraph.": "First paragraph.\n\nThird paragraph.",
            # Ring Classification and Review Level banners are stripped.
            "🛡️ Ring Classification: Ring 3 (Shared Libraries & Public Contracts) -> Review Level: Panel (`--panel`)\n\n"
            "Updates the generator.\n\n"
            "Pre-Execution Security Gate: `SAFE_FOR_LOCAL_EXECUTION`.": "Updates the generator.",
            "🛡️ Review Level: Fast (`--fast` specified, ring evaluation skipped)\n\n"
            "Minor doc fix.": "Minor doc fix.",
            # Exact user repro with Ring 3, file:// links, Citation audit, Flags, and Pre-Execution Security Gate.
            "🛡️ Ring Classification: Ring 3 (Shared Libraries & Public Contracts) -> Review Level: Panel (`--panel`)\n\n"
            "This change fixes [#3485](https://github.com/a2ui-project/a2ui/issues/3485) by updating "
            "[`prepareRef`](file:///tmp/pr-review-bfp9czs1/typescript/core/web_core/scripts/zod-generator-core.mjs#L245-L295) "
            "in [zod-generator-core.mjs](file:///tmp/pr-review-bfp9czs1/typescript/core/web_core/scripts/zod-generator-core.mjs#L185-L234).\n\n"
            "Citation audit: 2 checked, 0 dropped (`M2` from Lane A was filtered out). "
            "Flags: `--panel` (Ring 3 auto-scaled). Pre-Execution Security Gate: `SAFE_FOR_LOCAL_EXECUTION`.\n\n"
            "Verdict: Ready": "This change fixes [#3485](https://github.com/a2ui-project/a2ui/issues/3485) by updating "
            "`prepareRef` in `zod-generator-core.mjs`.",
            # Tier 1a/1b limit, skipped security gate, clean check, and Flags: none / *(none)* / Flags in effect.
            "Refactors X. `--verify` limited to tier 1a/1b: PR author is not a repo collaborator. "
            "`--verify` and tier 1b skipped: blocked by Pre-Execution Security Gate. "
            "Both reviewer checkouts passed the post-verification working-tree clean check (`0` tracked modifications). "
            "Flags: none (one Gemini reviewer, tier 1 verification).": "Refactors X.",
            "Refactors Y. Flags in effect: `(none)` (baseline 1-reviewer standard mode). "
            "Overall verdict: **Ready**.": "Refactors Y.",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(_clean_overview(raw), expected)

    def test_parse_verdict_uses_last_label(self):
        from lint_review import _parse_verdict

        text = (
            "Earlier passes reached verdict Ready, but H1 regresses the export path.\n"
            "Verdict: Critical Blockers\n"
        )
        self.assertEqual(_parse_verdict(text, []), "REQUEST_CHANGES")

    def test_export_json_repro_test_is_not_committable(self):
        report_text = self._finding_report(
            "\n```suggestion\n    await send()\n```\n"
            "- **Repro test**: `tests/review_scratch_test_retry.py`\n"
            "```python\ndef test_retry_increments_once():\n    assert retry(fail_once) == 1\n```"
        )
        data = self._export(report_text)
        finding = data["findings"][0]
        comment = finding["draft_comment"]
        self.assertIn("```suggestion\n    await send()\n```", comment)
        self.assertIn(
            "**Repro test**: `tests/review_scratch_test_retry.py`\n\n"
            "```python\ndef test_retry_increments_once():\n    assert retry(fail_once) == 1\n```",
            comment,
        )
        self.assertEqual(comment.count("```suggestion"), 1)
        self.assertEqual(
            finding["repro_test"],
            "def test_retry_increments_once():\n    assert retry(fail_once) == 1",
        )
        self.assertEqual(finding["suggestion"], "    await send()")

    def test_export_json_repro_test_bare_fence_defaults_to_python(self):
        report_text = self._finding_report(
            " Fix it.\n- **Repro test**:\n```\nassert retry(fail_once) == 1\n```"
        )
        data = self._export(report_text)
        self.assertIn(
            "**Repro test**\n\n```python\nassert retry(fail_once) == 1\n```",
            data["findings"][0]["draft_comment"],
        )

    def test_summary_keeps_paragraph_breaks(self):
        report_text = """\
## Summary
First paragraph.

Second paragraph.

## Review Comments (Ordered by Severity)
None.
"""
        data = self._export(report_text)
        self.assertEqual(
            data["review_summary"]["overview"], "First paragraph.\n\nSecond paragraph."
        )

    def test_why_deduplication_in_draft_comment(self):
        report_text = """\
## Summary
Verdict: Minor revisions needed.

## Review Comments (Ordered by Severity)

### M1. Overlapping why is not repeated
- **File**: `split_diff.py`
- **Line**: `50`
- **Severity**: `medium`
- **Evidence**: `Read` - line 50
- **Body**: `len(text)` counts characters instead of UTF-8 bytes, so multibyte diffs exceed the 46,080-byte read cap.
- **Why**: Multibyte diffs exceed the 46,080-byte read cap.
- **Suggestion**:
```python
return _byte_len(text) <= max_bytes
```
"""
        data = self._export(report_text)
        comment = data["findings"][0]["draft_comment"]
        self.assertEqual(comment.count("46,080-byte read cap"), 1)
        self.assertNotIn("**Impact**:", comment)

    def test_comment_prose_length_fails(self):
        five_sentences = (
            "First short sentence here. "
            "Second short sentence here. "
            "Third short sentence here. "
            "Fourth short sentence here. "
            "Fifth short sentence here."
        )
        bad = f"""\
## Review Comments (Ordered by Severity)
### M1. Too many sentences
- **File**: `foo.py`
- **Line**: `10`
- **Severity**: `medium`
- **Evidence**: `Read` - `foo.py:10`
- **Body**: {five_sentences}
- **Why**: Causes a bug.
"""
        rules = [v.rule for v in check_report(parse_report(bad))]
        self.assertIn("comment-prose-length", rules)

    def test_comment_sentence_length_fails(self):
        long_sentence = (
            "When the caller passes a dictionary with more than thirty entries "
            "and the configuration flag for strict validation is enabled in "
            "the runtime environment, the validator drops every remaining key "
            "without logging a warning."
        )
        bad = f"""\
## Review Comments (Ordered by Severity)
### M1. Long sentence
- **File**: `foo.py`
- **Line**: `10`
- **Severity**: `medium`
- **Evidence**: `Read` - `foo.py:10`
- **Body**: {long_sentence}
- **Why**: Drops keys silently.
"""
        rules = [v.rule for v in check_report(parse_report(bad))]
        self.assertIn("comment-sentence-length", rules)

    def test_comment_sentence_complexity_fails(self):
        complex_sentence = (
            "The parser, which when tokens that are not yet validated arrive "
            "while the buffer is still locked, fails unless the caller retries."
        )
        bad = f"""\
## Review Comments (Ordered by Severity)
### M1. Complex sentence
- **File**: `foo.py`
- **Line**: `10`
- **Severity**: `medium`
- **Evidence**: `Read` - `foo.py:10`
- **Body**: {complex_sentence}
- **Why**: Causes a failure.
"""
        rules = [v.rule for v in check_report(parse_report(bad))]
        self.assertIn("comment-sentence-complexity", rules)

    def test_comment_ai_ism_fails(self):
        for body_text in (
            "We should delve into this parser branch.",
            "The parser fails here — and drops the token.",
            "The system configuration validation pipeline rejects valid input.",
        ):
            with self.subTest(body_text=body_text):
                bad = f"""\
## Review Comments (Ordered by Severity)
### M1. AI-ism check
- **File**: `foo.py`
- **Line**: `10`
- **Severity**: `medium`
- **Evidence**: `Read` - `foo.py:10`
- **Body**: {body_text}
- **Why**: Breaks callers.
"""
                rules = [v.rule for v in check_report(parse_report(bad))]
                self.assertIn("comment-ai-ism", rules)

    def test_long_suggestion_code_block_ignored_by_prose_length(self):
        long_code = "\n".join(f"    value_{i} = compute_step({i})" for i in range(30))
        report_text = f"""\
## Summary
Verdict: Minor revisions needed.

## Review Comments (Ordered by Severity)

### M1. Long code block in suggestion
- **File**: `foo.py`
- **Line**: `10`
- **Severity**: `medium`
- **Evidence**: `Read` - `foo.py:10`
- **Body**: `compute_all()` skips the initialization steps.
- **Why**: Downstream callers read uninitialized state.
- **Suggestion**:
```suggestion
{long_code}
```
"""
        self.assertEqual(check_report(parse_report(report_text)), [])

    @staticmethod
    def _focus_report(focus_line: str, summary: str = "Verdict: Ready.") -> str:
        return f"""\
## Summary
{summary}

## Review Comments (Ordered by Severity)

### M1. Blocking call left inside async handler
- **File**: `handler.py`
- **Line**: `10`
- **Severity**: `medium`
- **Category**: `correctness`
{focus_line}
- **Evidence**: `Read` - `handler.py:10`
- **Body**: `fetch()` is called without `await`.
- **Why**: The handler returns before the fetch completes.
"""

    def test_focus_field_valid_passes_and_exports(self):
        cases = {
            "- **Focus**: `sync-to-async`": "sync-to-async",
            "- **Focus**: `adhoc:retry-storm-on-new-client`": "adhoc:retry-storm-on-new-client",
        }
        for focus_line, expected in cases.items():
            with self.subTest(focus_line=focus_line):
                report_text = self._focus_report(focus_line)
                self.assertEqual(check_report(parse_report(report_text)), [])
                data = self._export(report_text)
                self.assertEqual(data["findings"][0]["focus"], expected)
                self.assertIn("category", data["findings"][0])

    def test_focus_field_absent_is_not_exported(self):
        data = self._export(self._finding_report(" Fix it."))
        self.assertNotIn("focus", data["findings"][0])
        self.assertEqual(data["review_summary"]["focus_themes"], [])

    def test_focus_field_invalid_fails(self):
        for focus_line in (
            "- **Focus**: sync-to-async",
            "- **Focus**: `sync to async`",
            "- **Focus**: `sync-to-async` `adhoc:other`",
        ):
            with self.subTest(focus_line=focus_line):
                report = parse_report(self._focus_report(focus_line))
                violations = [
                    v for v in check_report(report) if v.rule == "focus-id-format"
                ]
                self.assertEqual(len(violations), 1)
                self.assertEqual(violations[0].line, report.findings[0].line)

    def test_focus_field_invalid_on_question_fails(self):
        bad = """\
## Questions
### Q1. Is the retry bounded?
- **Focus**: adhoc:retry-storm
- **Evidence**: `Speculation` - unchecked
- **Why**: Could loop.
- **To settle**: Read `retry.py`.
"""
        rules = [v.rule for v in check_report(parse_report(bad))]
        self.assertIn("focus-id-format", rules)

    def test_focus_themes_parsed_from_flags_line(self):
        from lint_review import _parse_focus_themes

        cases = {
            "Flags: `--panel`, `--focus sync-to-async (auto)`.": ["sync-to-async"],
            "Flags: `--focus adhoc:retry-storm`, `--focus sync-to-async (auto)`.": [
                "adhoc:retry-storm",
                "sync-to-async",
            ],
            "Flags: `--focus sync-to-async`, `--focus sync-to-async (auto)`.": [
                "sync-to-async"
            ],
            "Flags: `--panel`.": [],
            "Looks good.": [],
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(_parse_focus_themes(raw), expected)

    def test_focus_themes_exported_in_review_summary(self):
        report_text = self._focus_report(
            "- **Focus**: `sync-to-async`",
            summary="Flags: `--panel`, `--focus sync-to-async (auto)`. Verdict: Ready.",
        )
        data = self._export(report_text)
        self.assertEqual(data["review_summary"]["focus_themes"], ["sync-to-async"])
        self.assertEqual(data["review_summary"]["flags"], ["--panel", "--focus"])

    def test_flags_deduplicated_for_two_themes(self):
        from lint_review import _parse_flags

        self.assertEqual(
            _parse_flags(
                "Flags: `--panel`, `--focus sync-to-async (auto)`, "
                "`--focus adhoc:retry-storm`."
            ),
            ["--panel", "--focus"],
        )

    def test_overview_strips_focus_flag_token(self):
        from lint_review import _clean_overview

        cases = {
            "Refactors X. Flags: `--panel`, `--focus sync-to-async (auto)`.": "Refactors X.",
            "Refactors X. Flags: `--focus adhoc:retry-storm`, "
            "`--focus sync-to-async (auto)`. Verdict: Ready.": "Refactors X.",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(_clean_overview(raw), expected)

    def test_playbook_feedback_section_fails_lint(self):
        leaked = self._focus_report("- **Focus**: `sync-to-async`") + (
            "\n## Playbook Feedback\n"
            "Failure modes checked: 1, 2, 4\n"
            "- Missing failure mode: cancellation during shutdown.\n"
            "\n## Recommendations\n"
            "- Await `fetch()`.\n"
        )
        report = parse_report(leaked)
        violations = [
            v for v in check_report(report) if v.rule == "playbook-feedback-leaked"
        ]
        self.assertEqual(len(violations), 1)
        heading_line = leaked.splitlines().index("## Playbook Feedback") + 1
        self.assertEqual(violations[0].line, heading_line)
        self.assertEqual(len(report.findings), 1)
        self.assertEqual(report.recommendations, ["Await `fetch()`."])

    def test_playbook_feedback_lines_are_not_parsed(self):
        leaked = """\
## Review Comments (Ordered by Severity)
None.

## Playbook Feedback
Failure modes checked: 1, 2
### H1. Not a finding
- **Severity**: `high`
- Not a recommendation.
"""
        report = parse_report(leaked)
        self.assertEqual(report.findings, [])
        self.assertEqual(report.recommendations, [])
        rules = [v.rule for v in check_report(report)]
        self.assertEqual(rules, ["playbook-feedback-leaked"])

    def test_focus_fields_survive_pr_review_normalization(self):
        diff_parser_path = (
            pathlib.Path(__file__).resolve().parents[2]
            / "pr-review"
            / "scripts"
            / "diff_parser.py"
        )
        spec = importlib.util.spec_from_file_location("diff_parser", diff_parser_path)
        if spec is None or spec.loader is None:
            self.skipTest(f"diff_parser.py not found at {diff_parser_path}")
        try:
            diff_parser = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(diff_parser)
        except Exception as exc:  # noqa: BLE001 - skip on any import failure
            self.skipTest(f"diff_parser.py import failed: {exc}")

        report_text = self._focus_report(
            "- **Focus**: `adhoc:retry-storm`",
            summary="Flags: `--focus adhoc:retry-storm`. Verdict: Ready.",
        )
        payload = self._export(report_text)
        normalized = diff_parser.normalize_findings_payload(payload)
        self.assertEqual(normalized["findings"][0]["focus"], "adhoc:retry-storm")
        self.assertEqual(
            normalized["review_summary"]["focus_themes"], ["adhoc:retry-storm"]
        )


if __name__ == "__main__":
    unittest.main()
