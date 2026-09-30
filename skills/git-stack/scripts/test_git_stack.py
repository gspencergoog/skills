#!/usr/bin/env python3
"""test_git_stack.py - Unit tests for git-stack skill library and CLI."""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import git_stack_lib as lib


class TestGitStackLib(unittest.TestCase):

    def test_parse_worktree_porcelain(self):
        sample_output = (
            "worktree /home/user/code/repo/main\n"
            "HEAD 1111111111111111111111111111111111111111\n"
            "branch refs/heads/main\n"
            "\n"
            "worktree /home/user/code/repo/feature-pr1\n"
            "HEAD 2222222222222222222222222222222222222222\n"
            "branch refs/heads/feature-pr1\n"
            "\n"
            "worktree /home/user/code/repo/detached-test\n"
            "HEAD 3333333333333333333333333333333333333333\n"
            "detached\n"
        )
        with patch.object(lib, "run_cmd", return_value=(0, sample_output, "")):
            wt_map = lib.get_worktree_map(Path("/home/user/code/repo/main"))

        self.assertEqual(len(wt_map), 2)
        self.assertIn("main", wt_map)
        self.assertIn("feature-pr1", wt_map)
        self.assertEqual(wt_map["main"], Path("/home/user/code/repo/main").resolve())
        self.assertEqual(wt_map["feature-pr1"], Path("/home/user/code/repo/feature-pr1").resolve())

    def test_parse_repo_slug(self):
        test_cases = [
            ("git@github.com:a2ui-project/a2ui.git", ("a2ui-project", "a2ui")),
            ("https://github.com/a2ui-project/a2ui.git", ("a2ui-project", "a2ui")),
            ("https://github.com/user/my-repo", ("user", "my-repo")),
        ]
        for url, expected in test_cases:
            with patch.object(lib, "run_cmd", return_value=(0, url, "")):
                slug = lib.parse_repo_slug(Path("/dummy"))
                self.assertEqual(slug, expected)

    def test_stack_model_ancestry_detection(self):
        sample_graphql_response = {
            "number": 2860,
            "title": "PR 2 in stack",
            "state": "OPEN",
            "isDraft": False,
            "headRefName": "feat/pr-2",
            "baseRefName": "feat/pr-1",
            "url": "https://github.com/org/repo/pull/2860",
            "stack": {
                "number": 2859,
                "baseRefName": "main",
                "entries": {
                    "nodes": [
                        {
                            "pullRequest": {
                                "number": 2859,
                                "title": "PR 1 in stack",
                                "state": "OPEN",
                                "isDraft": False,
                                "headRefName": "feat/pr-1",
                                "baseRefName": "main",
                                "url": "https://github.com/org/repo/pull/2859",
                            }
                        },
                        {
                            "pullRequest": {
                                "number": 2860,
                                "title": "PR 2 in stack",
                                "state": "OPEN",
                                "isDraft": False,
                                "headRefName": "feat/pr-2",
                                "baseRefName": "feat/pr-1",
                                "url": "https://github.com/org/repo/pull/2860",
                            }
                        },
                    ]
                },
            },
        }

        # Mock git commands:
        # PR 1 is ancestor of main: True
        # PR 2 is ancestor of PR 1: False (needs rebase)
        def mock_run_cmd(cmd, cwd=None, env_override=None):
            cmd_str = " ".join(cmd)
            if "merge-base --is-ancestor" in cmd_str:
                if "feat/pr-2" in cmd_str:
                    return 1, "", "not ancestor"
                return 0, "", ""
            if "rev-parse --verify" in cmd_str:
                return 0, "mock_sha_123456", ""
            if "worktree list" in cmd_str:
                return 0, "", ""
            if "status --porcelain" in cmd_str:
                return 0, "", ""
            return 0, "", ""

        with patch.object(lib, "parse_repo_slug", return_value=("org", "repo")), \
             patch.object(lib, "fetch_stack_graphql", return_value=sample_graphql_response), \
             patch.object(lib, "run_cmd", side_effect=mock_run_cmd):
            model = lib.discover_stack(Path("/dummy"), target_branch_or_pr="2860")

        self.assertIsNotNone(model)
        self.assertEqual(len(model.entries), 2)
        self.assertEqual(model.entries[0].branch, "feat/pr-1")
        self.assertFalse(model.entries[0].needs_rebase)

        self.assertEqual(model.entries[1].branch, "feat/pr-2")
        self.assertTrue(model.entries[1].needs_rebase)

    def test_cascade_rebase_dry_run(self):
        model = lib.StackModel(
            root_base="main",
            remote="upstream",
            entries=[
                lib.StackEntry(
                    branch="feat/pr-1",
                    base_ref="main",
                    pr_number=2859,
                    needs_rebase=False,
                ),
                lib.StackEntry(
                    branch="feat/pr-2",
                    base_ref="feat/pr-1",
                    pr_number=2860,
                    needs_rebase=True,
                    worktree_path=Path("/dummy/wt-pr2"),
                ),
            ],
        )

        with patch.object(lib, "get_commit_sha", return_value="mock_sha"), \
             patch.object(lib, "compute_merge_base", return_value="mock_old_base_sha"), \
             patch.object(lib, "is_worktree_dirty", return_value=False):
            results = lib.cascade_rebase(Path("/dummy"), model, dry_run=True)

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["status"], "SKIPPED")
        self.assertEqual(results[1]["status"], "DRY_RUN")
        self.assertIn("git rebase --onto feat/pr-1 mock_old_base_sha", results[1]["command"])

    def test_sync_stack_remotes_dry_run(self):
        model = lib.StackModel(
            root_base="main",
            remote="upstream",
            entries=[
                lib.StackEntry(
                    branch="feat/pr-1",
                    base_ref="main",
                    pr_number=2859,
                ),
            ],
        )

        results = lib.sync_stack_remotes(Path("/dummy"), model, dry_run=True)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["action"], "PUSH")
        self.assertEqual(results[0]["command"], "git push --force-with-lease upstream feat/pr-1")
        self.assertEqual(results[1]["action"], "EDIT_BASE")
        self.assertEqual(results[1]["command"], "gh pr edit 2859 --base main")


if __name__ == "__main__":
    unittest.main()
