import unittest
from unittest.mock import patch, MagicMock, mock_open
import sys
import os
import importlib
import json
from datetime import datetime

import recommend_reviewers
from recommend_reviewers import (
    run_cmd,
    get_repo_info,
    get_default_branch,
    get_changed_files_from_range,
    get_pr_details,
    get_collaborators,
    get_file_contributors,
    map_git_to_github,
    main
)

class TestRecommendReviewers(unittest.TestCase):
    def setUp(self):
        importlib.reload(recommend_reviewers)

    @patch("subprocess.run")
    def test_run_cmd_success(self, mock_run):
        mock_res = MagicMock()
        mock_res.stdout = "  some output  \n"
        mock_run.return_value = mock_res
        
        res = run_cmd(["git", "status"])
        self.assertEqual(res, "some output")
        mock_run.assert_called_once_with(["git", "status"], capture_output=True, text=True, check=True, cwd=None)

    @patch("subprocess.run", side_effect=recommend_reviewers.subprocess.CalledProcessError(1, "cmd"))
    def test_run_cmd_failure(self, mock_run):
        res = run_cmd(["git", "status"])
        self.assertEqual(res, "")

    @patch("shutil.which", return_value="/usr/bin/gh")
    @patch("recommend_reviewers.run_cmd")
    def test_get_repo_info_gh_success(self, mock_run_cmd, mock_which):
        mock_run_cmd.return_value = '{"owner": {"login": "google"}, "name": "cheats"}'
        
        owner, name = get_repo_info("/fake/cwd")
        self.assertEqual(owner, "google")
        self.assertEqual(name, "cheats")
        mock_run_cmd.assert_called_with(["gh", "repo", "view", "--json", "owner,name"], cwd="/fake/cwd")

    @patch("shutil.which", return_value="/usr/bin/gh")
    @patch("recommend_reviewers.run_cmd")
    def test_get_repo_info_gh_malformed_json_fallback_ssh(self, mock_run_cmd, mock_which):
        # gh returns malformed json, fallback to git remote origin
        mock_run_cmd.side_effect = [
            "{malformed json",
            "git@github.com:google/cheats.git"
        ]
        
        owner, name = get_repo_info("/fake/cwd")
        self.assertEqual(owner, "google")
        self.assertEqual(name, "cheats")

    @patch("shutil.which", return_value=None)
    @patch("recommend_reviewers.run_cmd")
    def test_get_repo_info_no_gh_fallback_https(self, mock_run_cmd, mock_which):
        mock_run_cmd.return_value = "https://github.com/google/cheats"
        
        owner, name = get_repo_info("/fake/cwd")
        self.assertEqual(owner, "google")
        self.assertEqual(name, "cheats")

    @patch("shutil.which", return_value=None)
    @patch("recommend_reviewers.run_cmd")
    def test_get_repo_info_fallback_no_match(self, mock_run_cmd, mock_which):
        mock_run_cmd.return_value = "invalid-url"
        
        owner, name = get_repo_info("/fake/cwd")
        self.assertIsNone(owner)
        self.assertIsNone(name)

    @patch("shutil.which", return_value=None)
    @patch("recommend_reviewers.run_cmd", return_value="")
    def test_get_repo_info_fallback_empty(self, mock_run_cmd, mock_which):
        owner, name = get_repo_info("/fake/cwd")
        self.assertIsNone(owner)
        self.assertIsNone(name)

    @patch("recommend_reviewers.run_cmd")
    def test_get_default_branch_symbolic(self, mock_run_cmd):
        mock_run_cmd.return_value = "refs/remotes/origin/main"
        
        self.assertEqual(get_default_branch("/fake/cwd"), "main")
        mock_run_cmd.assert_called_once_with(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd="/fake/cwd")

    @patch("recommend_reviewers.run_cmd")
    def test_get_default_branch_list_main(self, mock_run_cmd):
        # first call (symbolic-ref) fails/empty, second returns branch list
        mock_run_cmd.side_effect = ["", "origin/main\norigin/feature"]
        
        self.assertEqual(get_default_branch("/fake/cwd"), "main")

    @patch("recommend_reviewers.run_cmd")
    def test_get_default_branch_list_master(self, mock_run_cmd):
        mock_run_cmd.side_effect = ["", "origin/master\norigin/feature"]
        
        self.assertEqual(get_default_branch("/fake/cwd"), "master")

    @patch("recommend_reviewers.run_cmd")
    def test_get_default_branch_fallback(self, mock_run_cmd):
        mock_run_cmd.side_effect = ["", "origin/feature"]
        
        self.assertEqual(get_default_branch("/fake/cwd"), "main")

    @patch("recommend_reviewers.run_cmd")
    def test_get_changed_files_success(self, mock_run_cmd):
        mock_run_cmd.return_value = "file1.py\nfile2.md\n"
        
        files = get_changed_files_from_range("main...feature", "/fake/cwd")
        self.assertEqual(files, ["file1.py", "file2.md"])
        mock_run_cmd.assert_called_once_with(["git", "diff", "--name-only", "main...feature"], cwd="/fake/cwd")

    @patch("recommend_reviewers.run_cmd", return_value="")
    def test_get_changed_files_empty(self, mock_run_cmd):
        files = get_changed_files_from_range("main...feature", "/fake/cwd")
        self.assertEqual(files, [])

    @patch("shutil.which", return_value=None)
    def test_get_pr_details_no_gh(self, mock_which):
        self.assertIsNone(get_pr_details(123, "owner/repo", "/fake/cwd"))

    @patch("shutil.which", return_value="/usr/bin/gh")
    @patch("recommend_reviewers.run_cmd")
    def test_get_pr_details_success(self, mock_run_cmd, mock_which):
        mock_run_cmd.return_value = '{"title": "Fix bug", "author": {"login": "coder"}}'
        
        details = get_pr_details(123, "owner/repo", "/fake/cwd")
        self.assertEqual(details["title"], "Fix bug")
        self.assertEqual(details["author"]["login"], "coder")
        mock_run_cmd.assert_called_once_with(
            ["gh", "pr", "view", "123", "--json", "author,files,title,baseRefName", "--repo", "owner/repo"],
            cwd="/fake/cwd"
        )

    @patch("shutil.which", return_value="/usr/bin/gh")
    @patch("recommend_reviewers.run_cmd")
    def test_get_pr_details_no_repo(self, mock_run_cmd, mock_which):
        mock_run_cmd.return_value = '{"title": "Fix bug"}'
        
        details = get_pr_details(123, None, "/fake/cwd")
        self.assertEqual(details["title"], "Fix bug")
        mock_run_cmd.assert_called_once_with(
            ["gh", "pr", "view", "123", "--json", "author,files,title,baseRefName"],
            cwd="/fake/cwd"
        )

    @patch("shutil.which", return_value="/usr/bin/gh")
    @patch("recommend_reviewers.run_cmd", return_value="invalid json")
    def test_get_pr_details_invalid_json(self, mock_run_cmd, mock_which):
        self.assertIsNone(get_pr_details(123, "owner/repo", "/fake/cwd"))

    @patch("shutil.which", return_value="/usr/bin/gh")
    @patch("recommend_reviewers.run_cmd", return_value="")
    def test_get_pr_details_empty_output(self, mock_run_cmd, mock_which):
        self.assertIsNone(get_pr_details(123, "owner/repo", "/fake/cwd"))

    @patch("shutil.which", return_value=None)
    def test_get_collaborators_no_gh(self, mock_which):
        self.assertEqual(get_collaborators("owner/repo", "/fake/cwd"), [])

    @patch("shutil.which", return_value="/usr/bin/gh")
    def test_get_collaborators_no_repo(self, mock_which):
        self.assertEqual(get_collaborators(None, "/fake/cwd"), [])

    @patch("shutil.which", return_value="/usr/bin/gh")
    @patch("recommend_reviewers.run_cmd")
    def test_get_collaborators_success_json(self, mock_run_cmd, mock_which):
        mock_run_cmd.return_value = '[{"login": "user1"}, {"login": "user2"}]'
        
        collabs = get_collaborators("owner/repo", "/fake/cwd")
        self.assertEqual(sorted(collabs), ["user1", "user2"])
        mock_run_cmd.assert_called_once_with(
            ["gh", "api", "repos/owner/repo/assignees", "--paginate"],
            cwd="/fake/cwd"
        )

    @patch("shutil.which", return_value="/usr/bin/gh")
    @patch("recommend_reviewers.run_cmd")
    def test_get_collaborators_fallback_regex(self, mock_run_cmd, mock_which):
        # JSON is not a list, but has logins inside it (e.g. malformed or nested)
        mock_run_cmd.return_value = '{"some": "data", "users": [{"login": "regex_user1"}, {"login": "regex_user2"}]}'
        
        collabs = get_collaborators("owner/repo", "/fake/cwd")
        self.assertEqual(sorted(collabs), ["regex_user1", "regex_user2"])

    @patch("shutil.which", return_value="/usr/bin/gh")
    @patch("recommend_reviewers.run_cmd", return_value="")
    def test_get_collaborators_empty(self, mock_run_cmd, mock_which):
        self.assertEqual(get_collaborators("owner/repo", "/fake/cwd"), [])

    @patch("recommend_reviewers.run_cmd")
    def test_get_file_contributors_success(self, mock_run_cmd):
        mock_run_cmd.return_value = (
            "alice@google.com|Alice Smith|1700000000\n"
            "bob@gmail.com|Bob Jones|1600000000\n"
        )
        
        contribs = get_file_contributors("path/to/file.py", "/fake/cwd")
        self.assertEqual(len(contribs), 2)
        self.assertEqual(contribs[0]["email"], "alice@google.com")
        self.assertEqual(contribs[0]["name"], "Alice Smith")
        self.assertEqual(contribs[0]["timestamp"], 1700000000)
        self.assertEqual(contribs[1]["email"], "bob@gmail.com")
        self.assertEqual(contribs[1]["name"], "Bob Jones")
        self.assertEqual(contribs[1]["timestamp"], 1600000000)
        mock_run_cmd.assert_called_once_with(
            ["git", "log", "-n", "50", "--format=%ae|%an|%at", "--", "path/to/file.py"],
            cwd="/fake/cwd"
        )

    @patch("recommend_reviewers.run_cmd", return_value="")
    def test_get_file_contributors_empty(self, mock_run_cmd):
        self.assertEqual(get_file_contributors("path/to/file.py", "/fake/cwd"), [])

    def test_map_git_to_github(self):
        github_logins = ["alicesmith", "bob", "CharlieBrown", "noreply"]
        
        # 1. Direct email prefix match
        self.assertEqual(map_git_to_github("bob@google.com", "Bob J", github_logins), "bob")
        
        # 2. Direct name match (clean)
        self.assertEqual(map_git_to_github("random@google.com", "Charlie Brown", github_logins), "CharlieBrown")
        
        # 3. Substring match (login in email)
        self.assertEqual(map_git_to_github("alice_smith@google.com", "Alice", github_logins), "alicesmith")
        
        # 4. Fallback to email prefix (not noreply/github)
        self.assertEqual(map_git_to_github("dave@google.com", "Dave S", github_logins), "dave")
        
        # 5. Returns None for noreply/github emails
        self.assertIsNone(map_git_to_github("12345+coder@users.noreply.github.com", "Coder", github_logins))
        self.assertIsNone(map_git_to_github("github-actions@github.com", "GitHub Actions", github_logins))

    # --- Main function tests ---

    @patch("os.path.isdir", return_value=False)
    @patch("sys.exit")
    def test_main_not_git_repo(self, mock_exit, mock_isdir):
        def raise_system_exit(code=0):
            raise SystemExit(code)
        mock_exit.side_effect = raise_system_exit
        
        # Pass a custom --dir
        test_args = ["recommend_reviewers.py", "--dir", "/fake/not-git"]
        with patch.object(sys, "argv", test_args):
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 1)

    @patch("os.path.isdir", return_value=True)
    @patch("recommend_reviewers.get_repo_info", return_value=("google", "cheats"))
    @patch("recommend_reviewers.get_default_branch", return_value="main")
    @patch("recommend_reviewers.get_changed_files_from_range", return_value=[])
    @patch("sys.exit")
    def test_main_no_changed_files(self, mock_exit, mock_changed, mock_branch, mock_repo, mock_isdir):
        test_args = ["recommend_reviewers.py", "--compare", "main...empty"]
        with patch.object(sys, "argv", test_args):
            main()
            mock_exit.assert_called_once_with(0)

    @patch("os.path.isdir", return_value=True)
    @patch("recommend_reviewers.get_repo_info", return_value=("google", "cheats"))
    @patch("recommend_reviewers.get_default_branch", return_value="main")
    @patch("recommend_reviewers.get_changed_files_from_range")
    @patch("recommend_reviewers.get_collaborators")
    @patch("recommend_reviewers.get_file_contributors")
    @patch("recommend_reviewers.datetime")
    @patch("shutil.which", return_value="/usr/bin/gh")
    @patch("builtins.print")
    def test_main_compare_range_success(self, mock_print, mock_which, mock_datetime, mock_file_contribs, mock_collabs, mock_changed, mock_branch, mock_repo, mock_isdir):
        # Fix mock datetime for recency scoring
        mock_datetime.now.return_value.timestamp.return_value = 1700000000
        
        mock_changed.return_value = ["dir1/file1.py", "dir2/file2.py"]
        mock_collabs.return_value = ["alice", "bob", "charlie"]
        
        # Mock file contributors
        # For file1.py: alice (direct commit at 1700000000 -> recent)
        # For file2.py: bob (direct commit at 1600000000 -> older)
        # For dir1/ (directory contribution): charlie (commit at 1700000000)
        # For dir2/ (directory contribution): none
        def mock_contrib_side_effect(path, cwd):
            if path == "dir1/file1.py":
                return [{"email": "alice@google.com", "name": "Alice Smith", "timestamp": 1700000000}]
            elif path == "dir2/file2.py":
                return [{"email": "bob@google.com", "name": "Bob Jones", "timestamp": 1600000000}]
            elif path == "dir1":
                return [{"email": "charlie@google.com", "name": "Charlie Brown", "timestamp": 1700000000}]
            return []
            
        mock_file_contribs.side_effect = mock_contrib_side_effect
        
        test_args = ["recommend_reviewers.py", "--compare", "main...feature"]
        with patch.object(sys, "argv", test_args):
            main()
            
            # Verify stdout prints the primary and secondary suggestions
            # We want to make sure print was called. We can check the print outputs.
            printed_calls = [call[0][0] for call in mock_print.call_args_list if call[0]]
            printed_text = "\n".join(printed_calls)
            
            self.assertIn("Reviewer Recommendation Report", printed_text)
            self.assertIn("Changed Files", printed_text)
            self.assertIn("dir1/file1.py", printed_text)
            self.assertIn("dir2/file2.py", printed_text)
            
            # alice and bob should be primary suggestions since they modified files directly
            self.assertIn("@alice", printed_text)
            self.assertIn("@bob", printed_text)
            
            # charlie should be in secondary suggestions since he only modified the directory
            self.assertIn("@charlie", printed_text)
            
            # Suggested command should be printed since shutil.which("gh") returns truthy
            self.assertIn("gh pr edit <pr-number> --add-reviewer", printed_text)

    @patch("os.path.isdir", return_value=True)
    @patch("recommend_reviewers.get_repo_info", return_value=("google", "cheats"))
    @patch("recommend_reviewers.get_default_branch", return_value="main")
    @patch("recommend_reviewers.get_pr_details")
    @patch("recommend_reviewers.get_collaborators")
    @patch("recommend_reviewers.get_file_contributors", return_value=[])
    @patch("builtins.print")
    def test_main_pr_number_success(self, mock_print, mock_file_contribs, mock_collabs, mock_pr_details, mock_branch, mock_repo, mock_isdir):
        mock_pr_details.return_value = {
            "title": "Add feature",
            "author": {"login": "pr_author"},
            "baseRefName": "main",
            "files": [{"path": "file1.py"}]
        }
        mock_collabs.return_value = ["alice", "pr_author"]
        
        test_args = ["recommend_reviewers.py", "--pr", "123"]
        with patch.object(sys, "argv", test_args):
            main()
            
            printed_text = "\n".join([call[0][0] for call in mock_print.call_args_list if call[0]])
            self.assertIn("Add feature (by @pr_author)", printed_text)
            self.assertIn("No primary candidates identified", printed_text)
            self.assertIn("No secondary candidates identified", printed_text)

    @patch("os.path.isdir", return_value=True)
    @patch("recommend_reviewers.get_repo_info", return_value=("google", "cheats"))
    @patch("recommend_reviewers.get_default_branch", return_value="main")
    @patch("recommend_reviewers.get_pr_details", return_value=None) # fails fetching PR details, falls back to local git
    @patch("recommend_reviewers.get_changed_files_from_range")
    @patch("recommend_reviewers.get_collaborators", return_value=[])
    @patch("recommend_reviewers.get_file_contributors", return_value=[])
    @patch("builtins.print")
    def test_main_pr_number_fallback_to_local(self, mock_print, mock_file_contribs, mock_collabs, mock_changed, mock_pr_details, mock_branch, mock_repo, mock_isdir):
        mock_changed.return_value = ["file1.py"]
        
        test_args = ["recommend_reviewers.py", "--pr", "123"]
        with patch.object(sys, "argv", test_args):
            main()
            
            printed_text = "\n".join([call[0][0] for call in mock_print.call_args_list if call[0]])
            self.assertIn("Found 1 changed files", printed_text)

    @patch("os.path.isdir", return_value=True)
    @patch("recommend_reviewers.get_repo_info", return_value=("google", "cheats"))
    @patch("recommend_reviewers.get_default_branch", return_value="main")
    @patch("recommend_reviewers.run_cmd")
    @patch("recommend_reviewers.get_changed_files_from_range")
    @patch("recommend_reviewers.get_collaborators", return_value=[])
    @patch("recommend_reviewers.get_file_contributors", return_value=[])
    @patch("builtins.print")
    def test_main_branch_argument(self, mock_print, mock_file_contribs, mock_collabs, mock_changed, mock_run_cmd, mock_branch, mock_repo, mock_isdir):
        mock_changed.return_value = ["file1.py"]
        
        test_args = ["recommend_reviewers.py", "--branch", "feature-branch"]
        with patch.object(sys, "argv", test_args):
            main()
            
            mock_changed.assert_called_with("origin/main...feature-branch", unittest.mock.ANY)

    @patch("os.path.isdir", return_value=True)
    @patch("recommend_reviewers.get_repo_info", return_value=("google", "cheats"))
    @patch("recommend_reviewers.get_default_branch", return_value="main")
    @patch("recommend_reviewers.run_cmd")
    @patch("recommend_reviewers.get_changed_files_from_range")
    @patch("recommend_reviewers.get_collaborators", return_value=[])
    @patch("recommend_reviewers.get_file_contributors", return_value=[])
    @patch("builtins.print")
    def test_main_auto_detect_branch(self, mock_print, mock_file_contribs, mock_collabs, mock_changed, mock_run_cmd, mock_branch, mock_repo, mock_isdir):
        mock_run_cmd.return_value = "feature-branch" # current branch
        mock_changed.return_value = ["file1.py"]
        
        test_args = ["recommend_reviewers.py"]
        with patch.object(sys, "argv", test_args):
            main()
            
            mock_changed.assert_called_with("origin/main...feature-branch", unittest.mock.ANY)

    @patch("os.path.isdir", return_value=True)
    @patch("recommend_reviewers.get_repo_info", return_value=("google", "cheats"))
    @patch("recommend_reviewers.get_default_branch", return_value="main")
    @patch("recommend_reviewers.run_cmd")
    @patch("recommend_reviewers.get_changed_files_from_range")
    @patch("recommend_reviewers.get_collaborators", return_value=[])
    @patch("recommend_reviewers.get_file_contributors", return_value=[])
    @patch("builtins.print")
    def test_main_auto_detect_branch_same_as_default(self, mock_print, mock_file_contribs, mock_collabs, mock_changed, mock_run_cmd, mock_branch, mock_repo, mock_isdir):
        mock_run_cmd.return_value = "main" # current branch same as default
        mock_changed.return_value = ["file1.py"]
        
        test_args = ["recommend_reviewers.py"]
        with patch.object(sys, "argv", test_args):
            main()
            
            # falls back to HEAD~1
            mock_changed.assert_called_with("HEAD~1", unittest.mock.ANY)

if __name__ == "__main__":
    unittest.main()
