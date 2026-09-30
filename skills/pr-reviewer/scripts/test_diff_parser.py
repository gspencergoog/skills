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


if __name__ == "__main__":
    unittest.main()
