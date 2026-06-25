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
        mock_run.return_value = "git@github.com:owner/repo.git"
        from analyze_comments import get_repo_info
        o, r = get_repo_info()
        self.assertEqual(o, "owner")
        self.assertEqual(r, "repo")

    @patch('analyze_comments.run_cmd')
    def test_get_repo_info_with_dots(self, mock_run):
        mock_run.return_value = "https://github.com/google/google.github.io.git"
        from analyze_comments import get_repo_info
        o, r = get_repo_info()
        self.assertEqual(o, "google")
        self.assertEqual(r, "google.github.io")
        
        # Test SSH format too
        mock_run.return_value = "git@github.com:google/google.github.io"
        o, r = get_repo_info()
        self.assertEqual(o, "google")
        self.assertEqual(r, "google.github.io")

    @patch('analyze_comments.run_cmd')
    def test_get_pr_number_success(self, mock_run):
        mock_run.return_value = "123"
        from analyze_comments import get_pr_number
        self.assertEqual(get_pr_number(), 123)

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
    @patch('analyze_comments.get_modified_lines', return_value={})
    @patch('analyze_comments.fetch_failed_checks_logs', return_value=[])
    def test_analyze_success(self, mock_failed_checks, mock_modified, mock_fetch_pr, mock_pr_num, mock_repo_info):
        mock_fetch_pr.return_value = (
            "PR description",
            "feature-branch",
            "main",
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
            ]
        )
        
        from analyze_comments import analyze
        res = analyze()
        self.assertEqual(res["repo"], "owner/repo")
        self.assertEqual(res["pr"], 123)
        self.assertEqual(len(res["threads"]), 1)
        self.assertEqual(res["threads"][0]["id"], "thread_1")

    @patch('analyze_comments.analyze')
    @patch('os.path.exists', return_value=True)
    @patch('os.chdir')
    @patch('argparse.ArgumentParser.parse_args')
    def test_main_json_success(self, mock_parse_args, mock_chdir, mock_exists, mock_analyze):
        mock_parse_args.return_value = MagicMock(json=True, all=False, dir=".")
        mock_analyze.return_value = {
            "repo": "owner/repo",
            "pr": 123,
            "prDescription": "desc",
            "threads": [],
            "checks": []
        }
        
        with patch('builtins.print') as mock_print:
            from analyze_comments import main
            main()
            mock_print.assert_called()

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
                    "link": "http://github.com/actions/runs/12345",
                    "workflow": "CI Workflow"
                }
            ]),
            "line1\nline2\nerror: compilation failed\nline4"
        ]
        from analyze_comments import fetch_failed_checks_logs
        checks = fetch_failed_checks_logs(123)
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["name"], "build_and_test")
        self.assertIn("compilation failed", checks[0]["logs"])

    @patch('analyze_comments.run_cmd')
    def test_fetch_failed_checks_logs_error(self, mock_run):
        mock_run.side_effect = Exception("failed run")
        from analyze_comments import fetch_failed_checks_logs
        checks = fetch_failed_checks_logs(123)
        self.assertEqual(checks, [])

    @patch('analyze_comments.get_repo_info', return_value=("owner", "repo"))
    @patch('analyze_comments.get_pr_number', return_value=123)
    @patch('analyze_comments.fetch_pr_data')
    @patch('analyze_comments.get_modified_lines', return_value={})
    @patch('analyze_comments.fetch_failed_checks_logs', return_value=[])
    @patch('analyze_comments.check_if_addressed')
    def test_analyze_unaddressed_and_outdated(self, mock_addressed, mock_failed_checks, mock_modified, mock_fetch_pr, mock_pr_num, mock_repo_info):
        mock_fetch_pr.return_value = (
            "PR description",
            "feature-branch",
            "main",
            [
                {
                    "id": "thread_1",
                    "path": "lib/foo.dart",
                    "line": 10,
                    "originalLine": 10,
                    "isOutdated": True,
                    "isResolved": False,
                    "comments": {
                        "nodes": [
                            {"id": "c1", "body": "nit", "author": {"login": "rev"}, "createdAt": "2026-06-15", "isMinimized": False}
                        ]
                    }
                }
            ]
        )
        mock_addressed.return_value = "Unaddressed (does not match suggestion)"
        
        from analyze_comments import analyze
        res = analyze(include_all=True)
        self.assertEqual(len(res["threads"]), 1)
        self.assertTrue(res["threads"][0]["isOutdated"])

    @patch('analyze_comments.analyze')
    @patch('os.path.exists', return_value=True)
    @patch('os.chdir')
    @patch('argparse.ArgumentParser.parse_args')
    def test_main_human_output(self, mock_parse_args, mock_chdir, mock_exists, mock_analyze):
        mock_parse_args.return_value = MagicMock(json=False, all=True, dir=".")
        mock_analyze.return_value = {
            "repo": "owner/repo",
            "pr": 123,
            "prDescription": "desc",
            "threads": [
                {
                    "id": "t1",
                    "path": "lib/foo.dart",
                    "line": 10,
                    "addressedStatus": "Unaddressed",
                    "isOutdated": False,
                    "isResolved": False,
                    "isHidden": False,
                    "localStatus": "unresolved",
                    "diffHunk": "@@ -10,1 +10,2 @@",
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
            ]
        }
        
        with patch('builtins.print') as mock_print:
            from analyze_comments import main
            main()
            mock_print.assert_called()

    # --- Additional Coverage Tests to cover >90% ---

    @patch('analyze_comments.run_cmd', side_effect=Exception("git error"))
    def test_get_repo_info_failure(self, mock_run):
        from analyze_comments import get_repo_info
        with self.assertRaises(Exception) as ctx:
            get_repo_info()
        self.assertIn("Could not determine repository owner", str(ctx.exception))

    @patch('analyze_comments.run_cmd', side_effect=Exception("gh error"))
    def test_get_pr_number_failure(self, mock_run):
        from analyze_comments import get_pr_number
        with self.assertRaises(Exception) as ctx:
            get_pr_number()
        self.assertIn("Could not find an active PR", str(ctx.exception))

    @patch('analyze_comments.run_cmd')
    def test_fetch_pr_data_success(self, mock_run):
        mock_run.return_value = json.dumps({
            "data": {
                "repository": {
                    "pullRequest": {
                        "body": "PR Body",
                        "headRefName": "feature",
                        "baseRefName": "main",
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "id": "thread_1",
                                    "isResolved": False,
                                    "isOutdated": False,
                                    "path": "lib/foo.dart",
                                    "line": 10,
                                    "originalLine": 10,
                                    "comments": {"nodes": []}
                                }
                            ]
                        }
                    }
                }
            }
        })
        from analyze_comments import fetch_pr_data
        body, head, base, threads = fetch_pr_data("owner", "repo", 123)
        self.assertEqual(body, "PR Body")
        self.assertEqual(head, "feature")
        self.assertEqual(base, "main")
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0]["id"], "thread_1")

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
        # 101 lines, each contains keyword "error" so all should be kept
        lines = ["error line" for _ in range(101)]
        log = "\n".join(lines)
        from analyze_comments import truncate_log
        self.assertEqual(truncate_log(log), log)

    @patch('analyze_comments.run_cmd')
    def test_fetch_failed_checks_logs_no_checks(self, mock_run):
        mock_run.side_effect = Exception("no checks reported")
        from analyze_comments import fetch_failed_checks_logs
        self.assertEqual(fetch_failed_checks_logs(123), [])

    @patch('analyze_comments.run_cmd')
    def test_fetch_failed_checks_logs_generic_exception(self, mock_run):
        mock_run.side_effect = Exception("random error")
        from analyze_comments import fetch_failed_checks_logs
        self.assertEqual(fetch_failed_checks_logs(123), [])

    @patch('analyze_comments.run_cmd')
    def test_fetch_failed_checks_logs_run_view_error_and_non_github(self, mock_run):
        mock_run.side_effect = [
            json.dumps([
                {
                    "name": "check_gha",
                    "state": "FAILURE",
                    "bucket": "fail",
                    "link": "https://github.com/owner/repo/actions/runs/123",
                    "workflow": "CI GHA"
                },
                {
                    "name": "check_other",
                    "state": "FAILURE",
                    "bucket": "fail",
                    "link": "https://other-ci.com/runs/456",
                    "workflow": "CI Other"
                }
            ]),
            Exception("gh run view failed")
        ]
        from analyze_comments import fetch_failed_checks_logs
        checks = fetch_failed_checks_logs(123)
        self.assertEqual(len(checks), 2)
        self.assertEqual(checks[0]["name"], "check_gha")
        self.assertIn("Failed to fetch logs", checks[0]["logs"])
        self.assertEqual(checks[1]["name"], "check_other")
        self.assertIn("Non-GitHub Actions run", checks[1]["logs"])

    @patch('analyze_comments.get_repo_info', return_value=("owner", "repo"))
    @patch('analyze_comments.get_pr_number', return_value=123)
    @patch('analyze_comments.fetch_pr_data')
    @patch('analyze_comments.get_modified_lines')
    @patch('analyze_comments.fetch_failed_checks_logs', return_value=[])
    @patch('analyze_comments.check_if_addressed', return_value="Pending review")
    def test_analyze_resolved_thread_skipped_and_local_status(self, mock_check_addressed, mock_failed, mock_modified, mock_fetch_pr, mock_pr_num, mock_repo_info):
        mock_fetch_pr.return_value = (
            "PR description",
            "feature-branch",
            "main",
            [
                {
                    "id": "thread_resolved",
                    "isResolved": True,
                    "isOutdated": False,
                    "path": "lib/foo.dart",
                    "line": 10,
                    "originalLine": 10,
                    "comments": {"nodes": []}
                },
                {
                    "id": "thread_local_mod",
                    "isResolved": False,
                    "isOutdated": False,
                    "path": "lib/bar.dart",
                    "line": 20,
                    "originalLine": 20,
                    "comments": {"nodes": [{"id": "c1", "body": "nit comment", "author": {"login": "rev"}, "createdAt": "2026-06-15", "isMinimized": False}]}
                }
            ]
        )
        mock_modified.return_value = {"lib/bar.dart": {20}}
        
        from analyze_comments import analyze
        res = analyze(include_all=False)
        self.assertEqual(len(res["threads"]), 1)
        self.assertEqual(res["threads"][0]["id"], "thread_local_mod")
        self.assertEqual(res["threads"][0]["localStatus"], "Modified locally")

    @patch('os.path.exists', return_value=False)
    @patch('sys.exit')
    @patch('argparse.ArgumentParser.parse_args')
    def test_main_directory_not_exists(self, mock_parse_args, mock_exit, mock_exists):
        def raise_system_exit(code=0):
            raise SystemExit(code)
        mock_exit.side_effect = raise_system_exit
        
        mock_parse_args.return_value = MagicMock(dir="/nonexistent/path")
        with patch('builtins.print') as mock_print:
            from analyze_comments import main
            with self.assertRaises(SystemExit) as cm:
                main()
            mock_print.assert_called()
            self.assertEqual(cm.exception.code, 1)

    @patch('analyze_comments.analyze', side_effect=Exception("analyze failed"))
    @patch('os.path.exists', return_value=True)
    @patch('os.chdir')
    @patch('sys.exit')
    @patch('argparse.ArgumentParser.parse_args')
    def test_main_generic_exception(self, mock_parse_args, mock_exit, mock_chdir, mock_exists, mock_analyze):
        def raise_system_exit(code=0):
            raise SystemExit(code)
        mock_exit.side_effect = raise_system_exit
        
        mock_parse_args.return_value = MagicMock(dir=".")
        with patch('builtins.print') as mock_print:
            from analyze_comments import main
            with self.assertRaises(SystemExit) as cm:
                main()
            mock_print.assert_called()
            self.assertEqual(cm.exception.code, 1)

if __name__ == '__main__':
    unittest.main()
