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


if __name__ == "__main__":
    unittest.main()

