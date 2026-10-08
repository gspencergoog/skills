#!/usr/bin/env python3
"""Unit tests for scaffold_proposals.py."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

scripts_dir = os.path.dirname(os.path.abspath(__file__))
if scripts_dir not in sys.path:
    sys.path.insert(0, scripts_dir)

from scaffold_proposals import extract_proposals_scaffold, main, summarize_text


class TestScaffoldProposals(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_scaffold_")
        self.comments_path = os.path.join(self.test_dir, "pr_comments.json")
        self.proposals_path = os.path.join(self.test_dir, "proposals.json")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_summarize_text(self):
        self.assertEqual(summarize_text(""), "")
        self.assertEqual(summarize_text("   \n\n  "), "")
        self.assertEqual(summarize_text("Short line"), "Short line")
        long_line = "a" * 150
        summary = summarize_text(long_line, max_len=50)
        self.assertEqual(len(summary), 50)
        self.assertTrue(summary.endswith("..."))

    def test_extract_proposals_basic(self):
        report_data = {
            "threads": [
                {
                    "id": "PRRT_1",
                    "path": "lib/foo.dart",
                    "line": 42,
                    "isResolved": False,
                    "isHidden": False,
                    "suggestion": "void foo() {}",
                    "comments": [
                        {
                            "author": "alice",
                            "body": "Please add a null check here to prevent crashes.",
                        }
                    ],
                }
            ]
        }
        scaffold, summaries = extract_proposals_scaffold(report_data)
        self.assertIn("PRRT_1", scaffold)
        self.assertEqual(scaffold["PRRT_1"]["path"], "lib/foo.dart")
        self.assertEqual(scaffold["PRRT_1"]["line"], 42)
        self.assertEqual(scaffold["PRRT_1"]["author"], "alice")
        self.assertEqual(scaffold["PRRT_1"]["summary"], "Please add a null check here to prevent crashes.")
        self.assertEqual(scaffold["PRRT_1"]["suggestion"], "void foo() {}")
        self.assertEqual(scaffold["PRRT_1"]["proposedFix"], "")
        self.assertEqual(scaffold["PRRT_1"]["draftReply"], "")
        self.assertEqual(scaffold["PRRT_1"]["action"], "accept")
        self.assertEqual(scaffold["PRRT_1"]["assessment"], "solid")
        self.assertEqual(len(summaries), 1)

    def test_skip_resolved_and_hidden_by_default(self):
        report_data = {
            "threads": [
                {
                    "id": "PRRT_open",
                    "path": "lib/open.dart",
                    "line": 10,
                    "isResolved": False,
                    "isHidden": False,
                    "comments": [{"author": "bob", "body": "Open issue"}],
                },
                {
                    "id": "PRRT_resolved",
                    "path": "lib/resolved.dart",
                    "line": 20,
                    "isResolved": True,
                    "isHidden": False,
                    "comments": [{"author": "carol", "body": "Resolved issue"}],
                },
                {
                    "id": "PRRT_hidden",
                    "path": "lib/hidden.dart",
                    "line": 30,
                    "isResolved": False,
                    "isHidden": True,
                    "comments": [{"author": "dave", "body": "Hidden issue"}],
                },
            ]
        }
        scaffold, summaries = extract_proposals_scaffold(report_data, include_resolved=False)
        self.assertIn("PRRT_open", scaffold)
        self.assertNotIn("PRRT_resolved", scaffold)
        self.assertNotIn("PRRT_hidden", scaffold)
        self.assertEqual(len(summaries), 1)

        # Include resolved
        scaffold_all, summaries_all = extract_proposals_scaffold(report_data, include_resolved=True)
        self.assertIn("PRRT_open", scaffold_all)
        self.assertIn("PRRT_resolved", scaffold_all)
        self.assertIn("PRRT_hidden", scaffold_all)
        self.assertEqual(len(summaries_all), 3)

    def test_cli_execution_with_data_dir(self):
        sample_report = {
            "threads": [
                {
                    "id": "PRRT_abc",
                    "path": "src/main.py",
                    "line": 100,
                    "isResolved": False,
                    "comments": [{"author": "reviewer", "body": "Fix this bug"}],
                }
            ]
        }
        with open(self.comments_path, "w", encoding="utf-8") as f:
            json.dump(sample_report, f)

        test_args = ["scaffold_proposals.py", "--data-dir", self.test_dir]
        with patch.object(sys, "argv", test_args):
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 0)

        self.assertTrue(os.path.exists(self.proposals_path))
        with open(self.proposals_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("PRRT_abc", data)
        self.assertEqual(data["PRRT_abc"]["path"], "src/main.py")

    def test_cli_overwrite_guard(self):
        sample_report = {"threads": []}
        with open(self.comments_path, "w", encoding="utf-8") as f:
            json.dump(sample_report, f)
        with open(self.proposals_path, "w", encoding="utf-8") as f:
            f.write("{}")

        # Fails without --force
        test_args = ["scaffold_proposals.py", "--data-dir", self.test_dir]
        with patch.object(sys, "argv", test_args):
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 1)

        # Succeeds with --force
        test_args_force = ["scaffold_proposals.py", "--data-dir", self.test_dir, "--force"]
        with patch.object(sys, "argv", test_args_force):
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 0)

    def test_cli_missing_comments_file(self):
        non_existent_dir = os.path.join(self.test_dir, "non_existent")
        test_args = ["scaffold_proposals.py", "--data-dir", non_existent_dir]
        with patch.object(sys, "argv", test_args):
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
