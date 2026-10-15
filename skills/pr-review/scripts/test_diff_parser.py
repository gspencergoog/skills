#!/usr/bin/env python3
"""
test_diff_parser.py - Unit tests for diff_parser.py.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from diff_parser import (
    DiffHunk,
    extract_source_lines,
    is_line_in_diff,
    is_range_in_diff,
    normalize_findings_payload,
    parse_diff_hunks,
    tag_findings_with_hunk_status,
)

SAMPLE_DIFF = """diff --git a/lib/parser.dart b/lib/parser.dart
index abc1234..def5678 100644
--- a/lib/parser.dart
+++ b/lib/parser.dart
@@ -10,6 +10,8 @@ class Parser {
   final String source;
+  final int version;
+  bool isClosed = false;
 
   void parse() {
@@ -35,5 +37,4 @@ class Parser {
   void reset() {
-    source = "";
     isClosed = false;
   }
diff --git a/test/parser_test.dart b/test/parser_test.dart
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/test/parser_test.dart
@@ -0,0 +1,5 @@
+void main() {
+  test('parses', () {
+    expect(true, isTrue);
+  });
+}
"""


class TestDiffParser(unittest.TestCase):

    def test_parse_diff_hunks(self):
        hunks = parse_diff_hunks(SAMPLE_DIFF)
        self.assertIn("lib/parser.dart", hunks)
        self.assertIn("test/parser_test.dart", hunks)

        parser_hunks = hunks["lib/parser.dart"]
        self.assertEqual(len(parser_hunks), 2)

        # First hunk: -10,6 +10,8
        self.assertEqual(parser_hunks[0].old_start, 10)
        self.assertEqual(parser_hunks[0].old_count, 6)
        self.assertEqual(parser_hunks[0].new_start, 10)
        self.assertEqual(parser_hunks[0].new_count, 8)

        # Second hunk: -35,5 +37,4
        self.assertEqual(parser_hunks[1].old_start, 35)
        self.assertEqual(parser_hunks[1].old_count, 5)
        self.assertEqual(parser_hunks[1].new_start, 37)
        self.assertEqual(parser_hunks[1].new_count, 4)

        # New file hunk: -0,0 +1,5
        test_hunks = hunks["test/parser_test.dart"]
        self.assertEqual(len(test_hunks), 1)
        self.assertEqual(test_hunks[0].new_start, 1)
        self.assertEqual(test_hunks[0].new_count, 5)

    def test_is_line_in_diff(self):
        hunks = parse_diff_hunks(SAMPLE_DIFF)

        # Line 12 in lib/parser.dart is within [10, 18)
        self.assertTrue(is_line_in_diff(hunks, "lib/parser.dart", 10))
        self.assertTrue(is_line_in_diff(hunks, "lib/parser.dart", 17))
        self.assertFalse(is_line_in_diff(hunks, "lib/parser.dart", 18))
        self.assertFalse(is_line_in_diff(hunks, "lib/parser.dart", 5))

        # Check outside hunk
        self.assertFalse(is_line_in_diff(hunks, "lib/parser.dart", 25))

        # Check line in second hunk [37, 41)
        self.assertTrue(is_line_in_diff(hunks, "lib/parser.dart", 37))
        self.assertTrue(is_line_in_diff(hunks, "lib/parser.dart", 40))
        self.assertFalse(is_line_in_diff(hunks, "lib/parser.dart", 41))

    def test_is_range_in_diff(self):
        hunks = parse_diff_hunks(SAMPLE_DIFF)

        # Range completely inside first hunk
        self.assertTrue(is_range_in_diff(hunks, "lib/parser.dart", 10, 15))
        # Range spanning outside hunk
        self.assertFalse(is_range_in_diff(hunks, "lib/parser.dart", 10, 20))
        # Inverted range
        self.assertTrue(is_range_in_diff(hunks, "lib/parser.dart", 15, 10))

    def test_extract_source_lines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "test.txt"
            content = "line 1\nline 2\nline 3\nline 4\nline 5\n"
            file_path.write_text(content, encoding="utf-8")

            # Extract lines 2 to 4
            extracted = extract_source_lines("test.txt", 2, 4, repo_dir=tmpdir)
            self.assertEqual(extracted, "line 2\nline 3\nline 4")

            # Extract single line
            single = extract_source_lines("test.txt", 1, 1, repo_dir=tmpdir)
            self.assertEqual(single, "line 1")

            # Out of bounds lines
            empty = extract_source_lines("test.txt", 10, 15, repo_dir=tmpdir)
            self.assertEqual(empty, "")

    def test_tag_findings_with_hunk_status(self):
        hunks = parse_diff_hunks(SAMPLE_DIFF)
        findings = [
            {
                "id": "1",
                "file_path": "lib/parser.dart",
                "line_start": 12,
                "line_end": 14,
                "draft_comment": "Check this",
            },
            {
                "id": "2",
                "file_path": "lib/parser.dart",
                "line_start": 25,
                "line_end": 25,
                "draft_comment": "Outside diff",
            },
        ]

        tagged = tag_findings_with_hunk_status(findings, hunks)
        self.assertTrue(tagged[0]["is_diff_hunk"])
        self.assertFalse(tagged[1]["is_diff_hunk"])

    def test_dotfile_paths_preserved(self):
        dot_diff = (
            "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
            "--- a/.github/workflows/ci.yml\n"
            "+++ b/.github/workflows/ci.yml\n"
            "@@ -3,0 +3,2 @@\n"
            "+      - run: make test\n"
            "+      - run: make lint\n"
        )
        hunks = parse_diff_hunks(dot_diff)
        self.assertTrue(is_line_in_diff(hunks, ".github/workflows/ci.yml", 3))
        self.assertTrue(is_line_in_diff(hunks, "./.github/workflows/ci.yml", 3))

        with tempfile.TemporaryDirectory() as tmpdir:
            wf_dir = Path(tmpdir) / ".github" / "workflows"
            wf_dir.mkdir(parents=True)
            (wf_dir / "ci.yml").write_text("line1\nline2\nmake test\n", encoding="utf-8")
            extracted = extract_source_lines(".github/workflows/ci.yml", 3, 3, repo_dir=tmpdir)
            self.assertEqual(extracted, "make test")

    def test_quoted_diff_paths_not_attributed_to_previous_file(self):
        quoted_diff = (
            "diff --git a/src/a.py b/src/a.py\n"
            "--- a/src/a.py\n"
            "+++ b/src/a.py\n"
            "@@ -1,0 +2 @@\n"
            "+x = 1\n"
            'diff --git "a/docs/caf\\303\\251.md" "b/docs/caf\\303\\251.md"\n'
            '--- "a/docs/caf\\303\\251.md"\n'
            '+++ "b/docs/caf\\303\\251.md"\n'
            "@@ -50,0 +51,3 @@\n"
            "+a\n+b\n+c\n"
        )
        hunks = parse_diff_hunks(quoted_diff)
        self.assertFalse(is_line_in_diff(hunks, "src/a.py", 52))
        self.assertTrue(is_line_in_diff(hunks, "docs/café.md", 52))

    def test_normalize_alternate_findings_schema(self):
        raw_payload = {
            "summary": "Replaces static common_types table with generated schema.",
            "verdict": "COMMENT",
            "general_findings": [
                {
                    "title": "Top-level observation",
                    "body": "General note outside any specific hunk.",
                }
            ],
            "inline_findings": [
                {
                    "file": "lib/parser.dart",
                    "start_line": 12,
                    "line": 15,
                    "severity": "MEDIUM",
                    "category": "Correctness",
                    "title": "Handleleading bracket in character class",
                    "body": "Unescaped `]` right after `[` is literal.",
                }
            ],
        }
        normalized = normalize_findings_payload(raw_payload)
        self.assertEqual(
            normalized["review_summary"]["overview"],
            "Replaces static common_types table with generated schema.",
        )
        self.assertEqual(normalized["review_summary"]["verdict"], "COMMENT")
        self.assertEqual(len(normalized["findings"]), 2)

        hunks = parse_diff_hunks(SAMPLE_DIFF)
        tagged = tag_findings_with_hunk_status(normalized["findings"], hunks)
        inline_item = tagged[0]
        self.assertEqual(inline_item["id"], "f1")
        self.assertEqual(inline_item["file_path"], "lib/parser.dart")
        self.assertEqual(inline_item["line_start"], 12)
        self.assertEqual(inline_item["line_end"], 15)
        self.assertEqual(inline_item["severity"], "medium")
        self.assertEqual(inline_item["description"], "Handleleading bracket in character class")
        self.assertEqual(inline_item["draft_comment"], "Unescaped `]` right after `[` is literal.")
        self.assertTrue(inline_item["is_diff_hunk"])

        general_item = tagged[1]
        self.assertEqual(general_item["id"], "f2")
        self.assertFalse(general_item["is_diff_hunk"])

    def test_repo_dir_accepts_workspace_file_and_coords_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir) / "checkout"
            (repo_root / "lib").mkdir(parents=True)
            (repo_root / "lib" / "parser.dart").write_text(
                "\n".join(f"line {i}" for i in range(1, 20)) + "\n",
                encoding="utf-8",
            )

            ws_txt = Path(tmpdir) / "workspace_dir.txt"
            ws_txt.write_text(f"{repo_root}\n", encoding="utf-8")
            self.assertEqual(
                extract_source_lines("lib/parser.dart", 12, 13, repo_dir=str(ws_txt)),
                "line 12\nline 13",
            )

            coords_json = Path(tmpdir) / "pr_coords.json"
            coords_json.write_text(
                json.dumps({"workspace_dir": str(repo_root)}),
                encoding="utf-8",
            )
            hunks = parse_diff_hunks(SAMPLE_DIFF)
            findings = [
                {
                    "id": "1",
                    "file_path": "lib/parser.dart",
                    "line_start": 12,
                    "line_end": 13,
                    "draft_comment": "Check this",
                }
            ]
            tagged = tag_findings_with_hunk_status(findings, hunks, repo_dir=str(coords_json))
            self.assertEqual(tagged[0]["original_code"], "line 12\nline 13")

    def test_normalize_findings_preserves_enriched_metadata(self):
        raw_payload = {
            "review_summary": {
                "overview": "Adds async handling.",
                "verdict": "REQUEST_CHANGES",
                "checked_clean": [
                    "No memory leak: Executed - valgrind clean",
                ],
                "structural_assessment": {
                    "implementer": "Clean interface",
                },
            },
            "findings": [
                {
                    "id": "H1",
                    "file_path": "lib/parser.dart",
                    "line_start": 12,
                    "line_end": 15,
                    "severity": "high",
                    "evidence_tier": "Read",
                    "evidence_detail": "lib/parser.dart:12 unawaited future",
                    "why": "Unawaited future causes silent failures.",
                    "description": "Unawaited future",
                    "draft_comment": "Await the future.",
                }
            ],
            "questions": [
                {
                    "id": "Q1",
                    "file_path": "lib/config.dart",
                    "line": 40,
                    "severity": "question",
                    "evidence_tier": "Speculation",
                    "evidence_detail": "Unclear default timeout",
                    "why": "May terminate early",
                    "to_settle": "Check timeout in client.dart",
                    "description": "Does timeout apply to streams?",
                    "draft_comment": "**Question**: Does timeout apply to streams?",
                }
            ],
        }
        normalized = normalize_findings_payload(raw_payload)
        summary = normalized["review_summary"]
        self.assertEqual(summary["overview"], "Adds async handling.")
        self.assertEqual(summary["verdict"], "REQUEST_CHANGES")
        self.assertEqual(len(summary["checked_clean"]), 1)
        self.assertEqual(summary["structural_assessment"]["implementer"], "Clean interface")

        findings = normalized["findings"]
        self.assertEqual(len(findings), 2)
        h1 = findings[0]
        self.assertEqual(h1["evidence_tier"], "Read")
        self.assertEqual(h1["why"], "Unawaited future causes silent failures.")

        q1 = findings[1]
        self.assertEqual(q1["id"], "Q1")
        self.assertEqual(q1["severity"], "question")
        self.assertEqual(q1["line_start"], 40)
        self.assertEqual(q1["line_end"], 40)
        self.assertEqual(q1["to_settle"], "Check timeout in client.dart")

    def test_normalize_findings_double_pass_deduplicates_questions_and_preserves_summary(self):
        raw_payload = {
            "review_summary": {
                "overview": "Adds async handling.",
                "verdict": "REQUEST_CHANGES",
            },
            "findings": [
                {
                    "id": "H1",
                    "file_path": "lib/parser.dart",
                    "line_start": 12,
                    "line_end": 15,
                    "severity": "high",
                    "draft_comment": "Fix this.",
                }
            ],
            "questions": [
                {
                    "id": "Q1",
                    "file_path": "lib/config.dart",
                    "line": 40,
                    "severity": "question",
                    "draft_comment": "**Question**: Does timeout apply to streams?",
                }
            ],
        }
        pass1 = normalize_findings_payload(raw_payload)
        self.assertEqual(len(pass1["findings"]), 2)
        self.assertNotIn("questions", pass1)

        pass2 = normalize_findings_payload(pass1)
        self.assertEqual(len(pass2["findings"]), 2)
        self.assertNotIn("questions", pass2)
        self.assertEqual(pass2["review_summary"]["overview"], "Adds async handling.")
        self.assertEqual(pass2["review_summary"]["verdict"], "REQUEST_CHANGES")

    def test_normalize_findings_questions_default_to_question_severity(self):
        raw_payload = {
            "findings": [],
            "questions": [
                {"id": "Q1", "file_path": "a.py", "line_start": 3, "draft_comment": "Needed?"},
                {"id": "Q2", "file_path": "a.py", "line_start": 9, "severity": "LOW"},
            ],
        }
        normalized = normalize_findings_payload(raw_payload)
        severities = {f["id"]: f["severity"] for f in normalized["findings"]}
        # Missing severity defaults to "question"; an explicit one is kept.
        self.assertEqual(severities, {"Q1": "question", "Q2": "low"})

    def test_normalize_findings_legacy_dict_summary_fallback(self):
        raw_payload = {
            "summary": {
                "overview": "Legacy summary dict.",
                "verdict": "APPROVE",
                "checked_clean": ["All clean"],
            },
            "findings": [],
        }
        normalized = normalize_findings_payload(raw_payload)
        self.assertEqual(normalized["review_summary"]["overview"], "Legacy summary dict.")
        self.assertEqual(normalized["review_summary"]["verdict"], "APPROVE")
        self.assertEqual(normalized["review_summary"]["checked_clean"], ["All clean"])
        self.assertNotIn("summary", normalized)

    def test_end_to_end_export_to_normalize_pipeline(self):
        import pathlib
        import sys
        import tempfile

        code_review_scripts = (
            pathlib.Path(__file__).resolve().parents[2]
            / "code-review"
            / "scripts"
        )
        if str(code_review_scripts) not in sys.path:
            sys.path.insert(0, str(code_review_scripts))

        from lint_review import export_report_to_json, parse_report

        report_text = """\
## Summary
Reviewed 2 files. Overall structure is solid.
Citation audit: 2 checked, 0 dropped. Flags: `--max`.

## Structural Assessment
- **Implementer**: Clean design
- **Tester / Verifier**: Full test coverage

## Review Comments (Ordered by Severity)

### H1. Missing await
- **File**: `lib/client.dart`
- **Line**: `22`
- **Severity**: `high`
- **Evidence**: `Read` - line inspection
- **Body**: Future is not awaited.
- **Why**: Requests finish prematurely.
- **Suggestion**:
```dart
await client.send();
```

## Questions

### Q1. Is timeout configurable?
- **File**: `lib/client.dart`
- **Line**: `10`
- **Evidence**: `Speculation` - default used
- **Why**: May hang on slow network.
- **To settle**: Inspect options class.

## Checked and Found Clean
- Memory leak in buffer: Executed - valgrind clean
"""
        report = parse_report(report_text)
        with tempfile.TemporaryDirectory() as tmp:
            json_path = pathlib.Path(tmp) / "review_results.json"
            exported = export_report_to_json(report, json_path)

            # Pass 1 through diff_parser normalization
            pass1 = normalize_findings_payload(exported)
            self.assertEqual(
                pass1["review_summary"]["overview"],
                "Reviewed 2 files. Overall structure is solid.",
            )
            self.assertEqual(pass1["review_summary"]["verdict"], "REQUEST_CHANGES")
            self.assertEqual(len(pass1["review_summary"]["checked_clean"]), 1)
            self.assertEqual(
                pass1["review_summary"]["structural_assessment"]["implementer"],
                "Clean design",
            )
            self.assertEqual(len(pass1["findings"]), 2)
            self.assertNotIn("questions", pass1)

            # Pass 2 (as happens in launch_dashboard)
            pass2 = normalize_findings_payload(pass1)
            self.assertEqual(len(pass2["findings"]), 2)
            self.assertNotIn("questions", pass2)
            self.assertEqual(
                pass2["review_summary"]["overview"],
                "Reviewed 2 files. Overall structure is solid.",
            )


if __name__ == "__main__":
    unittest.main()

