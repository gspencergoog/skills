#!/usr/bin/env python3
"""test_collab_search.py - Unit tests for github-collab-search skill."""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import collab_search_lib as lib


class TestCollabSearchLib(unittest.TestCase):

    def test_detect_repo_slug(self):
        with patch.object(lib, "run_gh_cmd", return_value=(0, "git@github.com:a2ui-project/a2ui.git", "")):
            owner, repo = lib.detect_repo_slug()
            self.assertEqual(owner, "a2ui-project")
            self.assertEqual(repo, "a2ui")

        with patch.object(lib, "run_gh_cmd", return_value=(0, "https://github.com/flutter/genui.git", "")):
            owner, repo = lib.detect_repo_slug()
            self.assertEqual(owner, "flutter")
            self.assertEqual(repo, "genui")

    def test_build_search_query(self):
        q1 = lib.build_search_query("owner/repo", "alice")
        self.assertEqual(q1, "repo:owner/repo is:issue,pr involves:alice")

        q2 = lib.build_search_query("org:my-org", "alice", start_date="2026-01-01", end_date="2026-06-30")
        self.assertEqual(q2, "org:my-org is:issue,pr involves:alice updated:2026-01-01..2026-06-30")

    def test_parse_thread_node_filtering(self):
        # Thread with User A as author and User B as reviewer
        pr_node = {
            "__typename": "PullRequest",
            "number": 101,
            "title": "Add feature",
            "url": "https://github.com/org/repo/pull/101",
            "state": "MERGED",
            "mergedAt": "2026-09-10T12:00:00Z",
            "createdAt": "2026-09-08T10:00:00Z",
            "updatedAt": "2026-09-10T12:00:00Z",
            "author": {"login": "alice"},
            "comments": {
                "nodes": [
                    {
                        "author": {"login": "bob"},
                        "body": "Looks great to me!",
                        "createdAt": "2026-09-09T14:00:00Z",
                        "url": "https://github.com/org/repo/pull/101#comment-1",
                    }
                ]
            },
            "reviews": {
                "nodes": [
                    {
                        "author": {"login": "bob"},
                        "state": "APPROVED",
                        "submittedAt": "2026-09-09T14:05:00Z",
                        "comments": {"nodes": []},
                    }
                ]
            },
        }

        # Case 1: Search for alice & bob -> retained
        t1 = lib.parse_thread_node(pr_node, user_a="alice", user_b="bob")
        self.assertIsNotNone(t1)
        self.assertEqual(t1.number, 101)
        self.assertEqual(t1.type, "PR")
        self.assertEqual(t1.state, "MERGED")
        self.assertTrue(t1.user_a_activity.is_author)
        self.assertEqual(len(t1.user_b_activity.reviews), 1)
        self.assertEqual(len(t1.user_b_activity.comments), 1)

        # Case 2: Search for alice & charlie -> excluded
        t2 = lib.parse_thread_node(pr_node, user_a="alice", user_b="charlie")
        self.assertIsNone(t2)

        # Case 3: Search for alice without user_b -> retained
        t3 = lib.parse_thread_node(pr_node, user_a="alice", user_b=None)
        self.assertIsNotNone(t3)

    def test_generate_markdown_report(self):
        thread = lib.CollaborativeThread(
            number=202,
            title="Fix async bug",
            type="PR",
            state="MERGED",
            url="https://github.com/org/repo/pull/202",
            created_at="2026-09-01T10:00:00Z",
            updated_at="2026-09-05T12:00:00Z",
            author="alice",
            participants={"alice", "bob"},
            user_a_activity=lib.UserActivity(user="alice", is_author=True),
            user_b_activity=lib.UserActivity(
                user="bob",
                reviews=[{"state": "APPROVED"}],
                comments=[{"body": "Nice fix"}],
            ),
        )

        md = lib.generate_markdown_report([thread], target="repo:org/repo", user_a="alice", user_b="bob")
        self.assertIn("# GitHub Collaboration Report: alice & bob", md)
        self.assertIn("### PR #202: Fix async bug [MERGED]", md)
        self.assertIn("- **@alice**: Author", md)
        self.assertIn("- **@bob**: 1 review(s) (APPROVED), 1 comment(s)", md)

    def test_partition_into_chunks(self):
        # Create a report with multiple sections
        header = "# Title\nOverview\n"
        pr1 = "\n### PR #1: Title 1 [MERGED]\nURL: http://pr1\nDetails 1\n"
        pr2 = "\n### PR #2: Title 2 [OPEN]\nURL: http://pr2\nDetails 2\n"

        full_content = header + pr1 + pr2
        # Partition with very small chunk size to force split
        chunks = lib.partition_into_chunks(full_content, max_chunk_kb=1)
        self.assertGreaterEqual(len(chunks), 1)


if __name__ == "__main__":
    unittest.main()
