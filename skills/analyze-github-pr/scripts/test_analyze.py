import unittest
from unittest.mock import patch, mock_open, MagicMock
import sys
import os
import importlib
import json

import analyze_comments
from analyze_comments import truncate_log, parse_suggestion, check_if_addressed

class TestAnalyzeComments(unittest.TestCase):
    def setUp(self):
        importlib.reload(analyze_comments)

    def test_truncate_log_empty(self):
        self.assertEqual(truncate_log(None), "")
        self.assertEqual(truncate_log(""), "")

    def test_truncate_log_short(self):
        log = "line1\nline2\nline3"
        self.assertEqual(truncate_log(log), log)

    def test_truncate_log_long_no_keywords(self):
        lines = [f"line {i}" for i in range(200)]
        log = "\n".join(lines)
        res = truncate_log(log)
        
        res_lines = res.splitlines()
        self.assertEqual(res_lines[0], "line 0")
        self.assertEqual(res_lines[14], "line 14")
        self.assertEqual(res_lines[15], "... [ELIDED 100 LINES] ...")
        self.assertEqual(res_lines[16], "line 115")
        self.assertEqual(res_lines[-1], "line 199")

    def test_truncate_log_long_with_keywords(self):
        lines = [f"line {i}" for i in range(200)]
        lines[50] = "error occurred on line 50"
        log = "\n".join(lines)
        res = truncate_log(log)
        
        res_lines = res.splitlines()
        self.assertIn("error occurred on line 50", res_lines)
        self.assertIn("line 20", res_lines)
        self.assertIn("line 60", res_lines)
        self.assertIn("... [ELIDED 5 LINES] ...", res_lines)
        self.assertIn("... [ELIDED 54 LINES] ...", res_lines)

    def test_parse_suggestion(self):
        body = "Here is my suggestion:\n```suggestion\nnew code block\n```\nHope it works."
        self.assertEqual(parse_suggestion(body), "new code block")
        
        body_no_suggest = "Some comments."
        self.assertIsNone(parse_suggestion(body_no_suggest))

    @patch('os.path.exists')
    def test_check_if_addressed_not_exists(self, mock_exists):
        mock_exists.return_value = False
        res = check_if_addressed("fake_path.dart", 10, None)
        self.assertEqual(res, "File not found")

    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open, read_data="line1\nline2\nline3\nline4\nline5\n")
    def test_check_if_addressed_addressed_suggestion(self, mock_file, mock_exists):
        mock_exists.return_value = True
        
        res = check_if_addressed("fake_path.dart", 2, "line2\nline3")
        self.assertEqual(res, "Addressed (matches suggestion)")

        res_unaddressed = check_if_addressed("fake_path.dart", 2, "line99\nline100")
        self.assertEqual(res_unaddressed, "Unaddressed (does not match suggestion)")

    @patch('os.path.exists', return_value=True)
    @patch('builtins.open', side_effect=IOError("Permission denied"))
    def test_check_if_addressed_unreadable(self, mock_file, mock_exists):
        res = check_if_addressed("fake_path.dart", 2, "line2")
        self.assertEqual(res, "Unreadable file")

    @patch('os.path.exists', return_value=True)
    @patch('builtins.open', new_callable=mock_open, read_data="line1\nline2\n")
    def test_check_if_addressed_out_of_bounds(self, mock_file, mock_exists):
        res = check_if_addressed("fake_path.dart", 999, "line2")
        self.assertEqual(res, "Line index out of bounds")

    @patch('os.path.exists', return_value=True)
    def test_check_if_addressed_no_line(self, mock_exists):
        res = check_if_addressed("fake_path.dart", None, "line2")
        self.assertEqual(res, "Pending review")

    @patch('os.path.exists', return_value=True)
    @patch('builtins.open', new_callable=mock_open, read_data="line1\nline2\n")
    def test_check_if_addressed_no_suggestion(self, mock_file, mock_exists):
        res = check_if_addressed("fake_path.dart", 2, None)
        self.assertEqual(res, "Pending review")

    # --- Expanded Coverage Tests ---

    @patch('analyze_comments.run_cmd')
    def test_get_repo_info_success(self, mock_run):
        mock_run.side_effect = ["origin", "git@github.com:owner/repo.git"]
        from analyze_comments import get_repo_info
        o, r = get_repo_info()
        self.assertEqual(o, "owner")
        self.assertEqual(r, "repo")

    def test_get_repo_info_with_pr_url(self):
        from analyze_comments import get_repo_info
        o, r = get_repo_info(pr_input="https://github.com/my-org/my-project/pull/456")
        self.assertEqual(o, "my-org")
        self.assertEqual(r, "my-project")

    @patch('analyze_comments.run_cmd')
    def test_get_repo_info_with_dots(self, mock_run):
        mock_run.side_effect = ["origin", "https://github.com/google/google.github.io.git"]
        from analyze_comments import get_repo_info
        o, r = get_repo_info()
        self.assertEqual(o, "google")
        self.assertEqual(r, "google.github.io")
        
        # Test SSH format too
        mock_run.side_effect = ["origin", "git@github.com:google/google.github.io"]
        o, r = get_repo_info()
        self.assertEqual(o, "google")
        self.assertEqual(r, "google.github.io")

    @patch('analyze_comments.run_cmd')
    def test_get_repo_info_gh_repo_view_fallback(self, mock_run):
        mock_run.side_effect = [
            Exception("no remotes"),
            Exception("no upstream"),
            Exception("no origin"),
            json.dumps({"owner": {"login": "gh-owner"}, "name": "gh-repo"})
        ]
        from analyze_comments import get_repo_info
        o, r = get_repo_info()
        self.assertEqual(o, "gh-owner")
        self.assertEqual(r, "gh-repo")

    @patch('analyze_comments.run_cmd')
    def test_get_pr_number_success(self, mock_run):
        mock_run.side_effect = [
            "feature-branch",
            json.dumps([{"number": 123, "url": "https://github.com/o/r/pull/123"}])
        ]
        from analyze_comments import get_pr_number
        self.assertEqual(get_pr_number(), 123)

    def test_get_pr_number_with_pr_url_and_digits(self):
        from analyze_comments import get_pr_number
        self.assertEqual(get_pr_number(pr_input="https://github.com/owner/repo/pull/789"), 789)
        self.assertEqual(get_pr_number(pr_input="789"), 789)
        self.assertEqual(get_pr_number(pr_input=789), 789)

    def test_get_pr_number_invalid_arg(self):
        from analyze_comments import get_pr_number
        with self.assertRaises(Exception) as ctx:
            get_pr_number(pr_input="not-a-valid-pr")
        self.assertIn("Invalid PR argument", str(ctx.exception))

    @patch('analyze_comments.run_cmd')
    def test_get_pr_number_detached_head_or_main(self, mock_run):
        mock_run.return_value = "main"
        from analyze_comments import get_pr_number
        with self.assertRaises(Exception) as ctx:
            get_pr_number()
        self.assertIn("Active branch is 'main'", str(ctx.exception))

    @patch('analyze_comments.run_cmd')
    def test_get_pr_number_multiple_prs(self, mock_run):
        mock_run.side_effect = [
            "feature-branch",
            json.dumps([{"number": 101}, {"number": 102}])
        ]
        from analyze_comments import get_pr_number
        with self.assertRaises(Exception) as ctx:
            get_pr_number()
        self.assertIn("Multiple open PRs found", str(ctx.exception))

    @patch('analyze_comments.run_cmd')
    def test_fetch_pr_sync_status_in_sync(self, mock_run):
        mock_run.side_effect = ["feature", "sha123"]
        from analyze_comments import fetch_pr_sync_status
        res = fetch_pr_sync_status("owner", "repo", 123, "feature", "sha123")
        self.assertTrue(res["isSynced"])
        self.assertEqual(res["syncState"], "in_sync")
        self.assertIsNone(res["warning"])

    @patch('analyze_comments.run_cmd')
    def test_fetch_pr_sync_status_branch_mismatch(self, mock_run):
        mock_run.side_effect = ["local-branch", "sha123"]
        from analyze_comments import fetch_pr_sync_status
        res = fetch_pr_sync_status("owner", "repo", 123, "remote-branch", "sha123")
        self.assertFalse(res["isSynced"])
        self.assertEqual(res["syncState"], "branch_mismatch")
        self.assertIn("Active local branch", res["warning"])

    @patch('analyze_comments.run_cmd')
    def test_fetch_pr_sync_status_not_fetched(self, mock_run):
        mock_run.side_effect = ["feature", "sha_local", Exception("commit not found")]
        from analyze_comments import fetch_pr_sync_status
        res = fetch_pr_sync_status("owner", "repo", 123, "feature", "sha_remote")
        self.assertFalse(res["isSynced"])
        self.assertEqual(res["syncState"], "not_fetched")

    @patch('analyze_comments.run_cmd')
    def test_fetch_pr_sync_status_behind_and_ahead_remote(self, mock_run):
        # Behind remote
        mock_run.side_effect = ["feature", "sha_local", "", ""]  # cat-file ok, merge-base ancestor ok
        from analyze_comments import fetch_pr_sync_status
        res = fetch_pr_sync_status("owner", "repo", 123, "feature", "sha_remote")
        self.assertFalse(res["isSynced"])
        self.assertEqual(res["syncState"], "behind_remote")

        # Ahead of remote
        mock_run.side_effect = ["feature", "sha_local", "", Exception("not local ancestor"), ""]  # remote ancestor ok
        res2 = fetch_pr_sync_status("owner", "repo", 123, "feature", "sha_remote")
        self.assertFalse(res2["isSynced"])
        self.assertEqual(res2["syncState"], "ahead_of_remote")

        # Diverged
        mock_run.side_effect = ["feature", "sha_local", "", Exception("not local"), Exception("not remote")]
        res3 = fetch_pr_sync_status("owner", "repo", 123, "feature", "sha_remote")
        self.assertFalse(res3["isSynced"])
        self.assertEqual(res3["syncState"], "diverged")

    @patch('analyze_comments.run_cmd')
    def test_get_modified_lines_success(self, mock_run):
        mock_run.side_effect = [
            "base_commit_hash",
            "diff --git a/lib/foo.dart b/lib/foo.dart\n@@ -10,1 +10,2 @@\n+line1\n"
        ]
        from analyze_comments import get_modified_lines
        lines = get_modified_lines()
        self.assertIn("lib/foo.dart", lines)
        self.assertIn(10, lines["lib/foo.dart"])

    @patch('analyze_comments.run_cmd', side_effect=Exception("git failed"))
    def test_get_modified_lines_all_failed(self, mock_run):
        from analyze_comments import get_modified_lines
        lines = get_modified_lines()
        self.assertEqual(lines, {})

    @patch('analyze_comments.get_repo_info', return_value=("owner", "repo"))
    @patch('analyze_comments.get_pr_number', return_value=123)
    @patch('analyze_comments.fetch_pr_data')
    @patch('analyze_comments.fetch_pr_sync_status', return_value={"isSynced": True, "syncState": "in_sync", "warning": None})
    @patch('analyze_comments.get_modified_lines', return_value={})
    @patch('analyze_comments.fetch_failed_checks_logs', return_value=([], []))
    def test_analyze_success(self, mock_failed_checks, mock_modified, mock_sync, mock_fetch_pr, mock_pr_num, mock_repo_info):
        mock_fetch_pr.return_value = (
            "PR description",
            "feature-branch",
            "oid123",
            "main",
            "APPROVED",
            "https://github.com/owner/repo/pull/123",
            [
                {
                    "id": "thread_1",
                    "path": "lib/foo.dart",
                    "line": 10,
                    "originalLine": 10,
                    "isOutdated": False,
                    "isResolved": False,
                    "comments": {
                        "nodes": [
                            {"id": "c1", "body": "nit", "author": {"login": "rev"}, "createdAt": "2026-06-15", "isMinimized": False}
                        ]
                    }
                }
            ],
            [{"id": "r1", "author": "rev", "body": "LGTM", "state": "APPROVED"}],
            [{"databaseId": "1", "author": "dev", "body": "Thanks"}]
        )
        
        from analyze_comments import analyze
        res = analyze()
        self.assertEqual(res["repo"], "owner/repo")
        self.assertEqual(res["pr"], 123)
        self.assertEqual(len(res["threads"]), 1)
        self.assertEqual(res["threads"][0]["id"], "thread_1")
        self.assertEqual(len(res["reviews"]), 1)
        self.assertEqual(len(res["comments"]), 1)

    @patch('analyze_comments.run_cmd')
    def test_fetch_pr_data_graphql_error(self, mock_run):
        mock_run.return_value = '{"errors": [{"message": "Invalid query"}]}'
        from analyze_comments import fetch_pr_data
        with self.assertRaises(RuntimeError) as ctx:
            fetch_pr_data("owner", "repo", 123)
        self.assertIn("GraphQL errors", str(ctx.exception))

    @patch('analyze_comments.run_cmd')
    def test_fetch_failed_checks_logs_success(self, mock_run):
        mock_run.side_effect = [
            json.dumps([
                {
                    "name": "build_and_test",
                    "state": "FAILURE",
                    "bucket": "fail",
                    "link": "https://github.com/owner/repo/actions/runs/12345",
                    "workflow": "CI Workflow"
                },
                {
                    "name": "lint",
                    "state": "PENDING",
                    "bucket": "pending",
                    "link": "https://github.com/owner/repo/actions/runs/12346",
                    "workflow": "CI Workflow"
                }
            ]),
            json.dumps({"jobs": [{"id": 999, "name": "test-job", "conclusion": "failure"}]}),
            "error: compilation failed"
        ]
        from analyze_comments import fetch_failed_checks_logs
        failed, pending = fetch_failed_checks_logs("owner", "repo", 123)
        self.assertEqual(len(failed), 1)
        self.assertEqual(len(pending), 1)
        self.assertEqual(failed[0]["name"], "build_and_test")
        self.assertIn("compilation failed", failed[0]["logs"])
        self.assertEqual(pending[0]["name"], "lint")

    @patch('analyze_comments.run_cmd')
    def test_fetch_check_annotations_and_job_logs(self, mock_run):
        from analyze_comments import parse_run_id_from_link, parse_check_run_id_from_link, fetch_check_annotations, fetch_failed_job_logs
        self.assertEqual(parse_run_id_from_link("https://github.com/o/r/actions/runs/555"), "555")
        self.assertEqual(parse_check_run_id_from_link("https://github.com/o/r/runs/1/job/666"), "666")
        self.assertEqual(parse_check_run_id_from_link("https://github.com/o/r/check-runs/777"), "777")

        mock_run.return_value = json.dumps([
            {"path": "lib/a.dart", "start_line": 15, "message": "Syntax error", "annotation_level": "failure", "title": "Compiler"}
        ])
        annotations = fetch_check_annotations("owner", "repo", "777")
        self.assertEqual(len(annotations), 1)
        self.assertIn("lib/a.dart:15", annotations[0])

    @patch('analyze_comments.run_cmd', side_effect=Exception("git error"))
    def test_get_repo_info_failure(self, mock_run):
        from analyze_comments import get_repo_info
        with self.assertRaises(Exception) as ctx:
            get_repo_info()
        self.assertIn("Could not determine repository owner", str(ctx.exception))

    @patch('analyze_comments.run_cmd')
    def test_get_pr_number_failure(self, mock_run):
        mock_run.side_effect = ["feature-x", Exception("gh list error"), Exception("gh view error")]
        from analyze_comments import get_pr_number
        with self.assertRaises(Exception) as ctx:
            get_pr_number()
        self.assertIn("No open PR found", str(ctx.exception))

    @patch('analyze_comments.run_cmd')
    def test_get_modified_lines_with_slash(self, mock_run):
        mock_run.side_effect = [
            "merge_base_commit",
            "diff --git a/lib/foo.dart b/lib/foo.dart\n@@ -10,1 +10,1 @@\n"
        ]
        from analyze_comments import get_modified_lines
        lines = get_modified_lines("origin/feature")
        self.assertIn("lib/foo.dart", lines)

    def test_truncate_log_keep_all(self):
        lines = ["error line" for _ in range(101)]
        log = "\n".join(lines)
        from analyze_comments import truncate_log
        self.assertEqual(truncate_log(log), log)

    def test_parse_diff_hunk_right_ref_basic(self):
        from analyze_comments import parse_diff_hunk_right_ref
        hunk = "@@ -10,4 +10,5 @@\n line10\n-line11\n+line11 new\n line12"
        res = parse_diff_hunk_right_ref(hunk)
        expected = [
            {"line": 10, "content": "line10"},
            {"line": 11, "content": "line11 new"},
            {"line": 12, "content": "line12"}
        ]
        self.assertEqual(res, expected)

    def test_parse_diff_hunk_right_ref_empty(self):
        from analyze_comments import parse_diff_hunk_right_ref
        self.assertEqual(parse_diff_hunk_right_ref(None), [])
        self.assertEqual(parse_diff_hunk_right_ref(""), [])

    @patch('analyze_comments.analyze')
    @patch('os.path.exists', return_value=True)
    @patch('os.chdir')
    @patch('argparse.ArgumentParser.parse_args')
    def test_main_output_file(self, mock_parse_args, mock_chdir, mock_exists, mock_analyze):
        mock_analyze.return_value = {
            "repo": "owner/repo",
            "pr": 123,
            "headRefName": "feature",
            "headRefOid": "sha123",
            "threads": [],
            "reviews": [{"author": "r", "state": "APPROVED", "body": "ok"}],
            "comments": [{"author": "c", "body": "hello"}],
            "checks": [{"name": "ci", "workflow": "CI", "link": "http://", "logs": "error"}],
            "pendingChecks": [{"name": "ci2", "state": "PENDING", "link": "http://"}],
            "syncStatus": {"warning": "sync warning"}
        }
        mock_parse_args.return_value = MagicMock(dir=".", output="/tmp/test_out/comments.json", json=False, all=False, pr=None)
        with patch('builtins.open', mock_open()) as mock_file, patch('os.makedirs') as mock_makedirs, patch('builtins.print') as mock_print:
            from analyze_comments import main
            main()
            mock_makedirs.assert_called_with("/tmp/test_out", exist_ok=True)
            mock_file.assert_called_with("/tmp/test_out/comments.json", "w", encoding="utf-8")
            mock_print.assert_called_with("Saved PR analysis to /tmp/test_out/comments.json")

    @patch('analyze_comments.analyze')
    @patch('os.path.exists', return_value=True)
    @patch('os.chdir')
    @patch('argparse.ArgumentParser.parse_args')
    def test_main_human_output(self, mock_parse_args, mock_chdir, mock_exists, mock_analyze):
        mock_parse_args.return_value = MagicMock(json=False, all=True, dir=".", output=None, pr=None)
        mock_analyze.return_value = {
            "repo": "owner/repo",
            "pr": 123,
            "prDescription": "desc",
            "syncStatus": {"warning": "Branch mismatch warning"},
            "reviews": [{"author": "reviewer1", "state": "APPROVED", "body": "Looks great"}],
            "comments": [{"author": "dev1", "body": "General comment"}],
            "threads": [
                {
                    "id": "t1",
                    "path": "lib/foo.dart",
                    "line": 10,
                    "originalLine": 10,
                    "isOutdated": False,
                    "isResolved": False,
                    "isHidden": False,
                    "localStatus": "unresolved",
                    "suggestion": "new code",
                    "comments": [
                        {
                            "body": "nit comment",
                            "author": "rev",
                            "createdAt": "2026-06-15",
                            "isMinimized": False,
                            "minimizedReason": ""
                        }
                    ]
                }
            ],
            "checks": [
                {
                    "name": "build",
                    "state": "FAILURE",
                    "link": "url",
                    "workflow": "CI",
                    "logs": "error logs"
                }
            ],
            "pendingChecks": [
                {
                    "name": "test_matrix",
                    "state": "IN_PROGRESS",
                    "link": "url2"
                }
            ]
        }
        
        with patch('builtins.print') as mock_print:
            from analyze_comments import main
            main()
            mock_print.assert_called()

if __name__ == '__main__':
    unittest.main()

