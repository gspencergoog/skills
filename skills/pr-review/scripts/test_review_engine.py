#!/usr/bin/env python3
"""
test_review_engine.py - Unit tests for review_engine.py.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from review_engine import (
    annotate_stack_status,
    blind_stack_summary,
    build_arg_parser,
    capture_stack_view,
    cleanup_workspace,
    fetch_pr_context,
    fetch_pr_stack,
    get_pr_diff,
    main,
    merge_stack_status,
    parse_repo_from_url,
    prepare_workspace,
    resolve_pr_coordinates,
    resolve_pr_metadata,
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
        args_ref = parser.parse_args([
            "--pr", "42",
            "--prepare-workspace",
            "--reference", "/tmp/local-repo",
            "--output-coords", "pr_coords.json",
            "--output-diff", "pr.diff",
            "--output-workspace", "workspace_dir.txt",
        ])
        self.assertEqual(args_ref.repo_dir, "/tmp/local-repo")

        args_dir = parser.parse_args([
            "--pr", "42",
            "--prepare-workspace",
            "--repo-dir", "/tmp/other-repo",
        ])
        self.assertEqual(args_dir.repo_dir, "/tmp/other-repo")

    @patch("review_engine.run_command")
    def test_resolve_pr_coordinates_omits_narrative_fields(self, mock_run):
        mock_run.return_value = (
            0,
            json.dumps({
                "number": 42,
                "baseRefName": "main",
                "headRefOid": "abc1234",
                "url": "https://github.com/octocat/hello-world/pull/42",
                "state": "OPEN",
            }),
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
            json.dumps({
                "number": 42,
                "title": "feat: add widget",
                "author": {"login": "octocat"},
                "baseRefName": "main",
                "headRefName": "feat/widget",
                "headRefOid": "abc1234",
                "url": "https://github.com/octocat/hello-world/pull/42",
                "state": "OPEN",
            }),
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
                json.dumps({
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
                }),
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

            rc = main([
                "--pr", "42",
                "--prepare-workspace",
                "--reference", "/tmp/ref",
                "--output-coords", str(coords_file),
                "--output-diff", str(diff_file),
                "--output-workspace", str(ws_file),
            ])
            self.assertEqual(rc, 0)
            saved_coords = json.loads(coords_file.read_text(encoding="utf-8"))
            self.assertEqual(saved_coords["workspace_dir"], "/tmp/pr-review-abc123")
            self.assertEqual(saved_coords["head_sha"], "actual_checked_out_sha")
            self.assertNotIn("title", saved_coords)
            self.assertEqual(diff_file.read_text(encoding="utf-8"), "diff --git a/a.py b/a.py\n")
            self.assertEqual(ws_file.read_text(encoding="utf-8"), "/tmp/pr-review-abc123")

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
    def test_main_fetch_context_enforces_blind_description_and_pins_sha(self, mock_fetch, _mock_stack):
        mock_fetch.return_value = (
            {"owner": "o", "repo": "r", "pull_number": 1, "title": "T", "head_sha": "new_sha_222"},
            {"title": "T", "body": "B", "comments": [], "reviews": [], "threads": [], "threads_complete": True},
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            coords_file = tmp_path / "pr_coords.json"
            coords_file.write_text(
                json.dumps({"owner": "o", "repo": "r", "pull_number": 1, "head_sha": "stage1_sha_111"}),
                encoding="utf-8",
            )
            blind_file = tmp_path / "blind_description.md"
            meta_file = tmp_path / "pr_meta.json"
            ctx_file = tmp_path / "pr_context.json"

            # Fails when --require-blind-description is omitted altogether (M6)
            rc_no_flag = main([
                "--fetch-context",
                "--input-coords", str(coords_file),
                "--output-meta", str(meta_file),
            ])
            self.assertEqual(rc_no_flag, 1)
            mock_fetch.assert_not_called()

            # Fails when blind_description.md does not exist
            rc_missing = main([
                "--fetch-context",
                "--input-coords", str(coords_file),
                "--require-blind-description", str(blind_file),
                "--output-meta", str(meta_file),
                "--output-context", str(ctx_file),
            ])
            self.assertEqual(rc_missing, 1)
            mock_fetch.assert_not_called()

            # Fails when blind_description.md is empty whitespace
            blind_file.write_text("   \n", encoding="utf-8")
            rc_empty = main([
                "--fetch-context",
                "--input-coords", str(coords_file),
                "--require-blind-description", str(blind_file),
                "--output-meta", str(meta_file),
                "--output-context", str(ctx_file),
            ])
            self.assertEqual(rc_empty, 1)
            mock_fetch.assert_not_called()

            # Succeeds once blind_description.md has content, and pins Stage 1 SHA (H2)
            blind_file.write_text("## Summary\nUpdates parser.", encoding="utf-8")
            rc_ok = main([
                "--fetch-context",
                "--input-coords", str(coords_file),
                "--require-blind-description", str(blind_file),
                "--output-meta", str(meta_file),
                "--output-context", str(ctx_file),
            ])
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

            rc = main([
                "--fetch-context",
                "--input-coords", str(coords_file),
                "--require-blind-description", str(blind_file),
            ])
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
                json.dumps({"owner": "o", "repo": "r", "pull_number": 1, "workspace_dir": safe_dir_json}),
                encoding="utf-8",
            )
            self.assertTrue(cleanup_workspace(str(coords_file)))
            self.assertFalse(Path(safe_dir_json).exists())


def _stack_graphql_payload(target: int = 3045) -> str:
    """A GraphQL `pullRequest.stack` response for a three-PR stack, bottom first."""
    return json.dumps({
        "data": {"repository": {"pullRequest": {"stack": {
            "number": 3046,
            "size": 3,
            "baseRefName": "main",
            "entries": {"totalCount": 3, "nodes": [
                {"pullRequest": {"number": 3027, "title": "fix(python): resolve", "state": "OPEN",
                                 "isDraft": False, "headRefName": "fix/py", "baseRefName": "main",
                                 "url": "https://github.com/o/r/pull/3027"}},
                {"pullRequest": {"number": 3044, "title": "fix(ts): resolve", "state": "OPEN",
                                 "isDraft": True, "headRefName": "fix/ts", "baseRefName": "fix/py",
                                 "url": "https://github.com/o/r/pull/3044"}},
                {"pullRequest": {"number": target, "title": "fix(dart): resolve", "state": "OPEN",
                                 "isDraft": False, "headRefName": "fix/dart", "baseRefName": "fix/ts",
                                 "url": f"https://github.com/o/r/pull/{target}"}},
            ]},
        }}}}
    })


def _stack_view_json() -> str:
    """`gh stack view --json` output matching `_stack_graphql_payload`, with the bottom PR stale."""
    return json.dumps({
        "trunk": "main",
        "currentBranch": "fix/dart",
        "branches": [
            {"name": "fix/py", "needsRebase": True, "isMerged": False, "isQueued": False,
             "pr": {"number": 3027, "state": "OPEN"}},
            {"name": "fix/ts", "needsRebase": False, "isMerged": False, "isQueued": False,
             "pr": {"number": 3044, "state": "OPEN"}},
            {"name": "fix/dart", "needsRebase": False, "isMerged": False, "isQueued": False,
             "pr": {"number": 3045, "state": "OPEN"}},
        ],
    })


class TestStackDetection(unittest.TestCase):

    @patch("review_engine.run_command")
    def test_fetch_pr_stack_orders_entries_bottom_first_and_marks_target(self, mock_run):
        mock_run.return_value = (0, _stack_graphql_payload(), "")
        stack = fetch_pr_stack("o", "r", 3045)
        self.assertEqual(stack["number"], 3046)
        self.assertEqual(stack["size"], 3)
        self.assertEqual(stack["trunk"], "main")
        self.assertEqual(stack["position"], 3)
        self.assertTrue(stack["entries_complete"])
        self.assertEqual([e["number"] for e in stack["entries"]], [3027, 3044, 3045])
        self.assertEqual([e["is_target"] for e in stack["entries"]], [False, False, True])
        self.assertEqual(stack["entries"][1]["base_ref"], "fix/py")
        self.assertTrue(stack["entries"][1]["is_draft"])
        self.assertEqual(mock_run.call_args[0][0][:3], ["gh", "api", "graphql"])

    @patch("review_engine.run_command")
    def test_fetch_pr_stack_returns_none_when_not_stacked_or_on_failure(self, mock_run):
        mock_run.return_value = (0, json.dumps({"data": {"repository": {"pullRequest": {"stack": None}}}}), "")
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
        self.assertEqual(blind["entries"][0], {
            "number": 3027, "state": "OPEN", "is_draft": False, "is_target": False,
            "needs_rebase": True, "is_merged": False, "is_queued": False,
        })

        structural = structural_stack(stack)
        self.assertEqual(structural["entries"][0]["head_ref"], "fix/py")
        self.assertNotIn("title", structural["entries"][0])
        self.assertNotIn("url", structural["entries"][0])
        self.assertIsNone(blind_stack_summary(None))
        self.assertIsNone(structural_stack(None))

    @patch("review_engine.run_command")
    def test_capture_stack_view_restores_detached_head(self, mock_run):
        mock_run.side_effect = [
            (0, "start_sha", ""),                      # git rev-parse HEAD
            (0, "✓ Imported stack", ""),               # gh stack checkout 3045
            (0, _stack_view_json(), ""),               # gh stack view --json
            (0, "", ""),                               # git checkout --detach start_sha
        ]
        view = capture_stack_view("/tmp/ws", 3045)
        self.assertEqual(view["trunk"], "main")
        self.assertEqual(len(view["branches"]), 3)
        self.assertEqual(mock_run.call_args_list[1][0][0], ["gh", "stack", "checkout", "3045"])
        self.assertEqual(mock_run.call_args_list[2][0][0], ["gh", "stack", "view", "--json"])
        self.assertEqual(
            mock_run.call_args_list[3][0][0],
            ["git", "checkout", "--quiet", "--detach", "start_sha"],
        )

    @patch("review_engine.run_command")
    def test_capture_stack_view_returns_none_without_extension(self, mock_run):
        mock_run.side_effect = [
            (0, "start_sha", ""),
            (1, "", "unknown command \"stack\" for \"gh\""),
            (0, "", ""),
        ]
        self.assertIsNone(capture_stack_view("/tmp/ws", 3045))
        self.assertEqual(mock_run.call_args_list[-1][0][0][:3], ["git", "checkout", "--quiet"])

    @patch("review_engine.run_command")
    def test_annotate_stack_status_falls_back_to_git_ancestry(self, mock_run):
        mock_run.side_effect = [
            (0, _stack_graphql_payload(), ""),
            (1, "", ""),   # origin/main not in origin/fix/py -> needs rebase
            (0, "", ""),   # origin/fix/py in origin/fix/ts
            (128, "", "fatal: Not a valid object name"),  # unknown
        ]
        stack = fetch_pr_stack("o", "r", 3045)
        annotate_stack_status(stack, None, workspace_dir="/tmp/ws")
        self.assertEqual(stack["source"], "git ancestry")
        self.assertEqual([e["needs_rebase"] for e in stack["entries"]], [True, False, None])
        self.assertEqual(
            mock_run.call_args_list[1][0][0],
            ["git", "merge-base", "--is-ancestor", "origin/main", "origin/fix/py"],
        )

    def test_merge_stack_status_copies_recorded_flags_by_pr_number(self):
        fresh = {"number": 3046, "size": 3, "position": 3, "entries": [
            {"number": 3027, "title": "a", "needs_rebase": None},
            {"number": 3044, "title": "b", "needs_rebase": None},
            {"number": 3099, "title": "new", "needs_rebase": None},
        ]}
        recorded = {"source": "gh stack view", "entries": [
            {"number": 3027, "needs_rebase": True, "is_merged": False},
            {"number": 3044, "needs_rebase": False},
        ]}
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
            "owner": "o", "repo": "r", "pull_number": 3045, "base_ref": "fix/ts",
            "head_sha": "1111111", "pr_url": "https://github.com/o/r/pull/3045", "state": "OPEN",
        }
        mock_prep.return_value = ("/tmp/pr-review-stack", True)
        with patch("review_engine.run_command", return_value=(0, _stack_graphql_payload(), "")):
            mock_stack.return_value = fetch_pr_stack("o", "r", 3045)
        mock_view.return_value = json.loads(_stack_view_json())
        mock_run.return_value = (0, "checked_out_sha", "")
        mock_diff.return_value = "diff --git a/a.py b/a.py\n"

        with tempfile.TemporaryDirectory() as tmp:
            coords_file = Path(tmp) / "pr_coords.json"
            stack_file = Path(tmp) / "pr_stack.json"
            rc = main([
                "--pr", "3045", "--prepare-workspace",
                "--output-coords", str(coords_file),
                "--output-stack", str(stack_file),
            ])
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
    @patch("review_engine.prepare_workspace", return_value=("/tmp/pr-review-flat", True))
    @patch("review_engine.resolve_pr_coordinates")
    def test_main_prepare_workspace_records_null_stack_for_unstacked_pr(
        self, mock_coords, _prep, _diff, _run, _stack, mock_view
    ):
        mock_coords.return_value = {
            "owner": "o", "repo": "r", "pull_number": 7, "base_ref": "main",
            "head_sha": "1", "pr_url": "https://github.com/o/r/pull/7", "state": "OPEN",
        }
        with tempfile.TemporaryDirectory() as tmp:
            coords_file = Path(tmp) / "pr_coords.json"
            stack_file = Path(tmp) / "pr_stack.json"
            rc = main([
                "--pr", "7", "--prepare-workspace",
                "--output-coords", str(coords_file),
                "--output-stack", str(stack_file),
            ])
            self.assertEqual(rc, 0)
            mock_view.assert_not_called()
            self.assertIsNone(json.loads(coords_file.read_text(encoding="utf-8"))["stack"])
            self.assertFalse(stack_file.exists())

    @patch("review_engine.fetch_pr_stack")
    @patch("review_engine.fetch_pr_context")
    def test_main_fetch_context_merges_recorded_stack_status(self, mock_fetch, mock_stack):
        mock_fetch.return_value = (
            {"owner": "o", "repo": "r", "pull_number": 3045, "title": "T", "head_sha": "s"},
            {"title": "T", "body": "B", "comments": [], "reviews": [], "threads": [], "threads_complete": True},
        )
        with patch("review_engine.run_command", return_value=(0, _stack_graphql_payload(), "")):
            mock_stack.return_value = fetch_pr_stack("o", "r", 3045)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            coords_file = tmp_path / "pr_coords.json"
            coords_file.write_text(
                json.dumps({"owner": "o", "repo": "r", "pull_number": 3045, "head_sha": "s"}),
                encoding="utf-8",
            )
            blind_file = tmp_path / "blind_description.md"
            blind_file.write_text("## Summary\nOk.", encoding="utf-8")
            stack_file = tmp_path / "pr_stack.json"
            stack_file.write_text(json.dumps({"source": "gh stack view", "entries": [
                {"number": 3027, "needs_rebase": True},
            ]}), encoding="utf-8")
            meta_file = tmp_path / "pr_meta.json"
            ctx_file = tmp_path / "pr_context.json"

            rc = main([
                "--fetch-context",
                "--input-coords", str(coords_file),
                "--input-stack", str(stack_file),
                "--require-blind-description", str(blind_file),
                "--output-meta", str(meta_file),
                "--output-context", str(ctx_file),
            ])
            self.assertEqual(rc, 0)
            context = json.loads(ctx_file.read_text(encoding="utf-8"))
            self.assertEqual(context["stack"]["number"], 3046)
            self.assertEqual(context["stack"]["source"], "gh stack view")
            self.assertTrue(context["stack"]["entries"][0]["needs_rebase"])
            self.assertEqual(context["stack"]["entries"][0]["title"], "fix(python): resolve")
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            self.assertEqual(meta["stack"], {"number": 3046, "size": 3, "position": 3})


if __name__ == "__main__":
    unittest.main()

