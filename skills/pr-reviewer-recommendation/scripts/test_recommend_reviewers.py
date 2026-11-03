# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit and integration tests for ``recommend_reviewers.py``."""

from __future__ import annotations

import importlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import recommend_reviewers
from recommend_reviewers import (
    get_changed_files_from_range,
    get_collaborators,
    get_default_branch,
    get_file_contributors,
    get_pr_details,
    get_repo_info,
    main,
    map_git_to_github,
    resolve_username,
    run_cmd,
)
from test_packrat_signals import _init_packrat_db


class TestRecommendReviewersHelpers(unittest.TestCase):
    """Tests subprocess wrappers, remote/branch discovery, and identity helpers."""

    def setUp(self) -> None:
        importlib.reload(recommend_reviewers)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        recommend_reviewers.DEFAULT_PACKRAT_DB = self.tmp_dir / "isolated_absent" / "repos.sqlite"

    @patch("subprocess.run")
    def test_run_cmd_success(self, mock_run: MagicMock) -> None:
        mock_res = MagicMock()
        mock_res.stdout = "  some output  \n"
        mock_run.return_value = mock_res

        res = run_cmd(["git", "status"])
        self.assertEqual(res, "some output")
        mock_run.assert_called_once_with(
            ["git", "status"],
            capture_output=True,
            text=True,
            check=True,
            cwd=None,
        )

    @patch(
        "subprocess.run",
        side_effect=recommend_reviewers.subprocess.CalledProcessError(1, "cmd"),
    )
    def test_run_cmd_failure(self, _mock_run: MagicMock) -> None:
        self.assertEqual(run_cmd(["git", "status"]), "")

    @patch("shutil.which", return_value="/usr/bin/gh")
    @patch("recommend_reviewers.run_cmd")
    def test_get_repo_info_upstream_remote_skips_gh(
        self, mock_run_cmd: MagicMock, _mock_which: MagicMock
    ) -> None:
        mock_run_cmd.return_value = (
            "origin\tgit@github.com:fork-user/widgets.git (fetch)\n"
            "upstream\tgit@github.com:octo-org/widgets.git (fetch)\n"
        )
        owner, name = get_repo_info("/fake/cwd")
        self.assertEqual((owner, name), ("octo-org", "widgets"))
        mock_run_cmd.assert_called_once_with(["git", "remote", "-v"], cwd="/fake/cwd")

    @patch("shutil.which", return_value="/usr/bin/gh")
    @patch("recommend_reviewers.run_cmd")
    def test_get_repo_info_fork_parent_lookup(
        self, mock_run_cmd: MagicMock, _mock_which: MagicMock
    ) -> None:
        mock_run_cmd.side_effect = [
            "origin\thttps://github.com/fork-user/widgets.git (fetch)\n",
            json.dumps(
                {
                    "owner": {"login": "fork-user"},
                    "name": "widgets",
                    "parent": {"owner": {"login": "octo-org"}, "name": "widgets"},
                }
            ),
        ]
        owner, name = get_repo_info("/fake/cwd")
        self.assertEqual((owner, name), ("octo-org", "widgets"))
        mock_run_cmd.assert_called_with(
            ["gh", "repo", "view", "--json", "owner,name,parent"],
            cwd="/fake/cwd",
        )

    @patch("shutil.which", return_value="/usr/bin/gh")
    @patch("recommend_reviewers.run_cmd")
    def test_get_repo_info_gh_direct_owner(
        self, mock_run_cmd: MagicMock, _mock_which: MagicMock
    ) -> None:
        mock_run_cmd.side_effect = [
            "origin\thttps://github.com/octo-org/widgets.git (fetch)\n",
            '{"owner": {"login": "octo-org"}, "name": "widgets", "parent": null}',
        ]
        self.assertEqual(get_repo_info("/fake/cwd"), ("octo-org", "widgets"))

    @patch("shutil.which", return_value="/usr/bin/gh")
    @patch("recommend_reviewers.run_cmd")
    def test_get_repo_info_gh_malformed_json_fallback_ssh(
        self, mock_run_cmd: MagicMock, _mock_which: MagicMock
    ) -> None:
        mock_run_cmd.side_effect = [
            "origin\tgit@github.com:octo-org/widgets.git (fetch)\n",
            "{malformed json",
        ]
        self.assertEqual(get_repo_info("/fake/cwd"), ("octo-org", "widgets"))

    @patch("shutil.which", return_value=None)
    @patch("recommend_reviewers.run_cmd")
    def test_get_repo_info_no_gh_fallback_https(
        self, mock_run_cmd: MagicMock, _mock_which: MagicMock
    ) -> None:
        mock_run_cmd.return_value = "origin\thttps://github.com/octo-org/widgets (fetch)\n"
        self.assertEqual(get_repo_info("/fake/cwd"), ("octo-org", "widgets"))

    @patch("shutil.which", return_value="/usr/bin/gh")
    @patch("recommend_reviewers.run_cmd")
    def test_get_repo_info_non_github_skips_gh(
        self, mock_run_cmd: MagicMock, _mock_which: MagicMock
    ) -> None:
        mock_run_cmd.return_value = "origin\thttps://gitlab.example.com/team/widgets.git (fetch)\n"
        self.assertEqual(get_repo_info("/fake/cwd"), (None, None))
        mock_run_cmd.assert_called_once_with(["git", "remote", "-v"], cwd="/fake/cwd")

    @patch("shutil.which", return_value=None)
    @patch("recommend_reviewers.run_cmd", return_value="")
    def test_get_repo_info_fallback_empty(
        self, _mock_run_cmd: MagicMock, _mock_which: MagicMock
    ) -> None:
        self.assertEqual(get_repo_info("/fake/cwd"), (None, None))

    @patch("recommend_reviewers.run_cmd")
    def test_get_default_branch_symbolic_upstream_and_origin(self, mock_run_cmd: MagicMock) -> None:
        mock_run_cmd.return_value = "refs/remotes/upstream/main"
        self.assertEqual(get_default_branch("/fake/cwd"), "main")
        mock_run_cmd.assert_called_once_with(
            ["git", "symbolic-ref", "refs/remotes/upstream/HEAD"],
            cwd="/fake/cwd",
        )

        mock_run_cmd.reset_mock()
        mock_run_cmd.side_effect = ["", "refs/remotes/origin/develop"]
        self.assertEqual(get_default_branch("/fake/cwd"), "develop")

    @patch("recommend_reviewers.run_cmd")
    def test_get_default_branch_for_each_ref_and_fallback(self, mock_run_cmd: MagicMock) -> None:
        mock_run_cmd.side_effect = ["", "", "refs/remotes/origin/main\nrefs/heads/feat"]
        self.assertEqual(get_default_branch("/fake/cwd"), "main")

        mock_run_cmd.side_effect = ["", "", "refs/remotes/origin/master\nrefs/heads/feat"]
        self.assertEqual(get_default_branch("/fake/cwd"), "master")

        mock_run_cmd.side_effect = ["", "", "refs/remotes/origin/feature"]
        self.assertEqual(get_default_branch("/fake/cwd"), "main")

    @patch("recommend_reviewers.run_cmd")
    def test_get_changed_files(self, mock_run_cmd: MagicMock) -> None:
        mock_run_cmd.return_value = "file1.py\nfile2.md\n"
        self.assertEqual(
            get_changed_files_from_range("main...feature", "/fake/cwd"),
            ["file1.py", "file2.md"],
        )
        mock_run_cmd.return_value = ""
        self.assertEqual(get_changed_files_from_range("main...feature", "/fake/cwd"), [])

    @patch("shutil.which", return_value=None)
    def test_get_pr_details_no_gh(self, _mock_which: MagicMock) -> None:
        self.assertIsNone(get_pr_details(123, "octo-org/widgets", "/fake/cwd"))

    @patch("shutil.which", return_value="/usr/bin/gh")
    @patch("recommend_reviewers.run_cmd")
    def test_get_pr_details_variants(self, mock_run_cmd: MagicMock, _mock_which: MagicMock) -> None:
        mock_run_cmd.return_value = (
            '{"title": "Fix bug", "author": {"login": "coder"}, "headRefOid": "abc1234"}'
        )
        details = get_pr_details(123, "octo-org/widgets", "/fake/cwd")
        assert details is not None
        self.assertEqual(details["title"], "Fix bug")
        self.assertEqual(details["headRefOid"], "abc1234")
        mock_run_cmd.assert_called_once_with(
            [
                "gh",
                "pr",
                "view",
                "123",
                "--json",
                "author,files,title,baseRefName,headRefOid",
                "--repo",
                "octo-org/widgets",
            ],
            cwd="/fake/cwd",
        )

        mock_run_cmd.reset_mock()
        mock_run_cmd.return_value = '{"title": "No repo flag"}'
        details_no_repo = get_pr_details(123, None, "/fake/cwd")
        assert details_no_repo is not None
        mock_run_cmd.assert_called_once_with(
            ["gh", "pr", "view", "123", "--json", "author,files,title,baseRefName,headRefOid"],
            cwd="/fake/cwd",
        )

        mock_run_cmd.return_value = "invalid json"
        self.assertIsNone(get_pr_details(123, "octo-org/widgets", "/fake/cwd"))

        mock_run_cmd.return_value = ""
        self.assertIsNone(get_pr_details(123, "octo-org/widgets", "/fake/cwd"))

    @patch("shutil.which")
    @patch("recommend_reviewers.run_cmd")
    def test_get_collaborators_variants(
        self, mock_run_cmd: MagicMock, mock_which: MagicMock
    ) -> None:
        mock_which.return_value = None
        self.assertEqual(get_collaborators("octo-org/widgets", "/fake/cwd"), [])

        mock_which.return_value = "/usr/bin/gh"
        self.assertEqual(get_collaborators(None, "/fake/cwd"), [])

        mock_run_cmd.return_value = '[{"login": "user1"}, {"login": "user2"}]'
        self.assertEqual(
            sorted(get_collaborators("octo-org/widgets", "/fake/cwd")),
            ["user1", "user2"],
        )

        mock_run_cmd.return_value = (
            '{"users": [{"login": "regex_user1"}, {"login": "regex_user2"}]}'
        )
        self.assertEqual(
            sorted(get_collaborators("octo-org/widgets", "/fake/cwd")),
            ["regex_user1", "regex_user2"],
        )

        mock_run_cmd.return_value = ""
        self.assertEqual(get_collaborators("octo-org/widgets", "/fake/cwd"), [])

    @patch("recommend_reviewers.run_cmd")
    def test_get_file_contributors_with_and_without_base_ref(self, mock_run_cmd: MagicMock) -> None:
        mock_run_cmd.return_value = (
            "sha123|alice@widgets.example|Alice Smith|1700000000\n"
            "sha456|bob@widgets.example|Bob Jones|1600000000\n"
        )
        contribs = get_file_contributors("path/to/file.py", "/fake/cwd", base_ref="origin/main")
        self.assertEqual(len(contribs), 2)
        self.assertEqual(contribs[0]["email"], "alice@widgets.example")
        mock_run_cmd.assert_called_once_with(
            [
                "git",
                "log",
                "-n",
                "50",
                "--format=%H|%ae|%an|%at",
                "origin/main",
                "--",
                "path/to/file.py",
            ],
            cwd="/fake/cwd",
        )

        mock_run_cmd.return_value = ""
        self.assertEqual(get_file_contributors("path/to/file.py", "/fake/cwd"), [])

    def test_map_git_to_github(self) -> None:
        github_logins = ["alicesmith", "bob", "CharlieBrown", "dan", "alex"]

        self.assertEqual(map_git_to_github("bob@widgets.example", "Bob J", github_logins), "bob")
        self.assertEqual(
            map_git_to_github("random@widgets.example", "Charlie Brown", github_logins),
            "CharlieBrown",
        )
        self.assertEqual(
            map_git_to_github("alice_smith@widgets.example", "Alice", github_logins),
            "alicesmith",
        )
        self.assertEqual(
            map_git_to_github("12345+coder@users.noreply.github.com", "Coder", github_logins),
            "coder",
        )
        self.assertIsNone(map_git_to_github("dave@widgets.example", "Dave S", github_logins))
        self.assertIsNone(map_git_to_github("jordan@widgets.example", "Jordan", github_logins))
        self.assertIsNone(map_git_to_github("unlinked@widgets.example", "Alex", github_logins))
        self.assertIsNone(map_git_to_github("noreply@github.com", "GitHub", github_logins))
        self.assertIsNone(
            map_git_to_github("github-actions@github.com", "GitHub Actions", github_logins)
        )

    @patch("shutil.which")
    @patch("recommend_reviewers.run_cmd")
    def test_resolve_username(self, mock_run_cmd: MagicMock, mock_which: MagicMock) -> None:
        github_logins = ["alice", "bob"]
        cache: dict[str, str | None] = {"alice@widgets.example": "alice"}
        self.assertEqual(
            resolve_username(
                "sha123",
                "alice@widgets.example",
                "Alice Smith",
                "octo-org/widgets",
                "/fake/cwd",
                github_logins,
                cache,
            ),
            "alice",
        )
        mock_run_cmd.assert_not_called()

        cache = {}
        mock_which.return_value = "/usr/bin/gh"
        mock_run_cmd.return_value = "bob"
        self.assertEqual(
            resolve_username(
                "sha456",
                "bob@widgets.example",
                "Bob Jones",
                "octo-org/widgets",
                "/fake/cwd",
                github_logins,
                cache,
            ),
            "bob",
        )
        self.assertEqual(cache["bob@widgets.example"], "bob")

        mock_run_cmd.reset_mock()
        cache = {}
        mock_run_cmd.return_value = ""
        self.assertEqual(
            resolve_username(
                "sha456",
                "bob@widgets.example",
                "Bob Jones",
                "octo-org/widgets",
                "/fake/cwd",
                github_logins,
                cache,
            ),
            "bob",
        )

        mock_which.return_value = None
        cache = {}
        self.assertIsNone(
            resolve_username(
                "sha789",
                "dave@widgets.example",
                "Dave S",
                "octo-org/widgets",
                "/fake/cwd",
                github_logins,
                cache,
            )
        )


class TestRecommendReviewersMainMocked(unittest.TestCase):
    """Tests CLI ``main()`` control flow with mocked git/gh calls and isolated DB."""

    def setUp(self) -> None:
        importlib.reload(recommend_reviewers)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        recommend_reviewers.DEFAULT_PACKRAT_DB = self.tmp_dir / "isolated_absent" / "repos.sqlite"

    @patch("recommend_reviewers.run_cmd", return_value="false")
    def test_main_not_git_repo(self, mock_run_cmd: MagicMock) -> None:
        test_args = ["recommend_reviewers.py", "--dir", "/fake/not-git"]
        with (
            patch.object(sys, "argv", test_args),
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as cm,
        ):
            main()
        self.assertEqual(cm.exception.code, 1)
        mock_run_cmd.assert_any_call(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd="/fake/not-git",
        )

    @patch("recommend_reviewers.run_cmd", return_value="true")
    @patch("recommend_reviewers.get_repo_info", return_value=("octo-org", "widgets"))
    @patch("recommend_reviewers.get_default_branch", return_value="main")
    @patch("recommend_reviewers.get_changed_files_from_range", return_value=[])
    def test_main_no_changed_files(
        self,
        _mock_changed: MagicMock,
        _mock_branch: MagicMock,
        _mock_repo: MagicMock,
        _mock_run_cmd: MagicMock,
    ) -> None:
        test_args = ["recommend_reviewers.py", "--compare", "main...empty"]
        with (
            patch.object(sys, "argv", test_args),
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as cm,
        ):
            main()
        self.assertEqual(cm.exception.code, 0)

    @patch("recommend_reviewers.run_cmd")
    @patch("recommend_reviewers.get_repo_info", return_value=("octo-org", "widgets"))
    @patch("recommend_reviewers.get_default_branch", return_value="main")
    @patch("recommend_reviewers.get_changed_files_from_range")
    @patch("recommend_reviewers.get_collaborators")
    @patch("recommend_reviewers.get_file_contributors")
    @patch("recommend_reviewers.datetime")
    @patch("shutil.which", return_value="/usr/bin/gh")
    def test_main_compare_range_git_only_fallback(
        self,
        _mock_which: MagicMock,
        mock_datetime: MagicMock,
        mock_file_contribs: MagicMock,
        mock_collabs: MagicMock,
        mock_changed: MagicMock,
        _mock_branch: MagicMock,
        _mock_repo: MagicMock,
        mock_run_cmd: MagicMock,
    ) -> None:
        mock_datetime.now.return_value.timestamp.return_value = 1700000000
        mock_run_cmd.side_effect = lambda args, cwd=None: "true" if "rev-parse" in args else ""
        mock_changed.return_value = ["dir1/file1.py", "dir2/file2.py"]
        mock_collabs.return_value = ["alice", "bob", "charlie"]

        contribs_by_path = {
            "dir1/file1.py": [
                {
                    "sha": "sha1",
                    "email": "alice@widgets.example",
                    "name": "Alice Smith",
                    "timestamp": 1700000000,
                }
            ],
            "dir2/file2.py": [
                {
                    "sha": "sha2",
                    "email": "bob@widgets.example",
                    "name": "Bob Jones",
                    "timestamp": 1600000000,
                }
            ],
            "dir1": [
                {
                    "sha": "sha3",
                    "email": "charlie@widgets.example",
                    "name": "Charlie Brown",
                    "timestamp": 1700000000,
                }
            ],
        }
        mock_file_contribs.side_effect = lambda path, cwd: contribs_by_path.get(path, [])

        stdout_buf = io.StringIO()
        test_args = ["recommend_reviewers.py", "--compare", "main...feature"]
        with (
            patch.object(sys, "argv", test_args),
            redirect_stdout(stdout_buf),
            redirect_stderr(io.StringIO()),
        ):
            main()

        printed = stdout_buf.getvalue()
        self.assertIn("## Reviewer Recommendation Report", printed)
        self.assertIn("packrat sync --repo octo-org/widgets", printed)
        self.assertIn("dir1/file1.py", printed)
        self.assertIn("@alice", printed)
        self.assertIn("@bob", printed)
        self.assertIn("@charlie", printed)
        self.assertIn(
            "gh pr edit <pr-number> --add-reviewer alice,bob,charlie -R octo-org/widgets",
            printed,
        )

    @patch("recommend_reviewers.run_cmd", return_value="true")
    @patch("recommend_reviewers.get_repo_info", return_value=("octo-org", "widgets"))
    @patch("recommend_reviewers.get_default_branch", return_value="main")
    @patch("recommend_reviewers.get_pr_details")
    @patch("recommend_reviewers.get_collaborators", return_value=["alice", "pr_author"])
    @patch("recommend_reviewers.get_file_contributors", return_value=[])
    def test_main_pr_number_and_local_fallback(
        self,
        _mock_contribs: MagicMock,
        _mock_collabs: MagicMock,
        mock_pr_details: MagicMock,
        _mock_branch: MagicMock,
        _mock_repo: MagicMock,
        _mock_run_cmd: MagicMock,
    ) -> None:
        mock_pr_details.return_value = {
            "title": "Add widget",
            "author": {"login": "pr_author"},
            "baseRefName": "main",
            "headRefOid": "abcdef1234567",
            "files": [{"path": "file1.py"}],
        }
        out_buf = io.StringIO()
        with (
            patch.object(sys, "argv", ["recommend_reviewers.py", "--pr", "123"]),
            redirect_stdout(out_buf),
            redirect_stderr(io.StringIO()),
        ):
            main()
        printed = out_buf.getvalue()
        self.assertIn("Add widget (by @pr_author)", printed)
        self.assertIn("No primary candidates identified.", printed)
        self.assertIn("No secondary candidates identified.", printed)

        mock_pr_details.return_value = None
        err_buf = io.StringIO()
        with (
            patch("recommend_reviewers.get_changed_files_from_range", return_value=["file1.py"]),
            patch.object(sys, "argv", ["recommend_reviewers.py", "--pr", "123"]),
            redirect_stdout(io.StringIO()),
            redirect_stderr(err_buf),
        ):
            main()
        self.assertIn("Found 1 changed files.", err_buf.getvalue())

    @patch("recommend_reviewers.get_repo_info", return_value=("octo-org", "widgets"))
    @patch("recommend_reviewers.get_default_branch", return_value="main")
    @patch("recommend_reviewers.run_cmd")
    @patch("recommend_reviewers.get_changed_files_from_range", return_value=["file1.py"])
    @patch("recommend_reviewers.get_collaborators", return_value=[])
    @patch("recommend_reviewers.get_file_contributors", return_value=[])
    def test_main_branch_and_auto_detect_modes(
        self,
        _mock_contribs: MagicMock,
        _mock_collabs: MagicMock,
        mock_changed: MagicMock,
        mock_run_cmd: MagicMock,
        _mock_branch: MagicMock,
        _mock_repo: MagicMock,
    ) -> None:
        mock_run_cmd.side_effect = lambda args, cwd=None: (
            "true" if "--is-inside-work-tree" in args else ""
        )
        with (
            patch.object(sys, "argv", ["recommend_reviewers.py", "--branch", "feature-branch"]),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            main()
        mock_changed.assert_called_with("origin/main...feature-branch", unittest.mock.ANY)

        def auto_feature(args: list[str], cwd: str | None = None) -> str:
            if "--is-inside-work-tree" in args:
                return "true"
            return "feature-branch" if "--show-current" in args else ""

        mock_run_cmd.side_effect = auto_feature
        with (
            patch.object(sys, "argv", ["recommend_reviewers.py"]),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            main()
        mock_changed.assert_called_with("origin/main...feature-branch", unittest.mock.ANY)

        def auto_default(args: list[str], cwd: str | None = None) -> str:
            if "--is-inside-work-tree" in args:
                return "true"
            return "main" if "--show-current" in args else ""

        mock_run_cmd.side_effect = auto_default
        with (
            patch.object(sys, "argv", ["recommend_reviewers.py"]),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            main()
        mock_changed.assert_called_with("HEAD~1", unittest.mock.ANY)


def _seed_end_to_end_db(
    db_path: Path, *, backfill_done_at: str | None = "2026-04-01T00:00:00Z"
) -> None:
    """Seeds ``octo-org/widgets`` PRs, reviews, threads, and commits into ``db_path``."""
    repo_id = _init_packrat_db(db_path, backfill_done_at=backfill_done_at)
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            """
            INSERT INTO pull_requests (
              repo_id, number, state, author, title, base_ref, head_sha,
              changed_files, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'main', ?, 2, '2026-03-15T00:00:00Z', '2026-03-16T00:00:00Z')
            """,
            [
                (repo_id, 10, "MERGED", "alice-dev", "Add button widget", "aaaa1111"),
                (repo_id, 11, "MERGED", "other-author", "Add slider sibling", "bbbb2222"),
                (repo_id, 99, "OPEN", "pr-owner", "Offline Widget Fix", "cccc3333"),
            ],
        )
        conn.executemany(
            "INSERT INTO pr_files (repo_id, pr_number, file_path) VALUES (?, ?, ?)",
            [
                (repo_id, 10, "src/widgets/button.py"),
                (repo_id, 11, "src/widgets/slider.py"),
                (repo_id, 99, "src/widgets/button.py"),
            ],
        )
        conn.executemany(
            """
            INSERT INTO reviews (repo_id, review_id, pr_number, author, state, submitted_at)
            VALUES (?, ?, ?, ?, ?, '2026-03-16T02:00:00Z')
            """,
            [
                (repo_id, "R_pro", 10, "reviewer-pro", "APPROVED"),
                (repo_id, "R_dir", 11, "dir-reviewer", "APPROVED"),
            ],
        )
        conn.execute(
            """
            INSERT INTO review_threads (
              thread_id, repo_id, pr_number, file_path, reviewer, created_at, updated_at, comments_json
            ) VALUES ('T_pro', ?, 10, 'src/widgets/button.py', 'reviewer-pro',
                      '2026-03-16T01:00:00Z', '2026-03-16T02:00:00Z', ?)
            """,
            (
                repo_id,
                json.dumps(
                    [
                        {"author": {"login": "reviewer-pro"}, "body": "Check focus state"},
                        {"author": {"login": "alice-dev"}, "body": "Done"},
                    ]
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO commits (repo_id, sha, author_name, author_email, committed_at, title, pr_number)
            VALUES (?, 'aaaa1111', 'Alice Dev', 'alice@widgets.example',
                    '2026-03-15T12:00:00Z', 'feat: add button', 10)
            """,
            (repo_id,),
        )
        conn.commit()
    finally:
        conn.close()


class TestRecommendReviewersPackratEndToEnd(unittest.TestCase):
    """End-to-end ``main()`` integration tests with a seeded SQLite ``packrat`` DB."""

    def setUp(self) -> None:
        importlib.reload(recommend_reviewers)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.db_path = self.tmp_dir / "repos.sqlite"
        recommend_reviewers.DEFAULT_PACKRAT_DB = self.tmp_dir / "isolated_absent" / "repos.sqlite"
        _seed_end_to_end_db(self.db_path)

    @patch("shutil.which", return_value="/usr/bin/gh")
    @patch("recommend_reviewers.datetime")
    @patch("recommend_reviewers.get_collaborators", return_value=[])
    @patch("recommend_reviewers.get_file_contributors")
    @patch(
        "recommend_reviewers.get_changed_files_from_range", return_value=["src/widgets/button.py"]
    )
    @patch("recommend_reviewers.get_default_branch", return_value="main")
    @patch("recommend_reviewers.get_repo_info", return_value=("octo-org", "widgets"))
    @patch("recommend_reviewers.run_cmd")
    def test_combined_packrat_and_git_ranking_and_unlinked_email(
        self,
        mock_run_cmd: MagicMock,
        _mock_repo: MagicMock,
        _mock_branch: MagicMock,
        _mock_changed: MagicMock,
        mock_contribs: MagicMock,
        _mock_collabs: MagicMock,
        mock_datetime: MagicMock,
        _mock_which: MagicMock,
    ) -> None:
        mock_datetime.now.return_value.timestamp.return_value = 1774000000
        mock_run_cmd.side_effect = lambda args, cwd=None: (
            "true" if "--is-inside-work-tree" in args else ""
        )
        mock_contribs.side_effect = lambda path, cwd, base_ref=None: (
            [
                {
                    "sha": "aaaa1111",
                    "email": "alice@widgets.example",
                    "name": "Alice Dev",
                    "timestamp": 1773600000,
                },
                {
                    "sha": "eeee9999",
                    "email": "ext@partner.example",
                    "name": "External Contributor",
                    "timestamp": 1773500000,
                },
            ]
            if path == "src/widgets/button.py"
            else []
        )

        out_buf = io.StringIO()
        argv = [
            "recommend_reviewers.py",
            "--compare",
            "main...feat",
            "--packrat-db",
            str(self.db_path),
        ]
        with (
            patch.object(sys, "argv", argv),
            redirect_stdout(out_buf),
            redirect_stderr(io.StringIO()),
        ):
            main()

        report = out_buf.getvalue()
        self.assertIn("**@reviewer-pro**", report)
        self.assertIn("**@alice-dev** (Alice Dev)", report)
        self.assertIn("**@dir-reviewer**", report)
        self.assertIn("External Contributor (git:ext@partner.example)", report)
        self.assertNotIn("@ext", report)
        self.assertIn(
            "gh pr edit <pr-number> --add-reviewer reviewer-pro,alice-dev,dir-reviewer -R octo-org/widgets",
            report,
        )

    @patch("shutil.which", return_value="/usr/bin/gh")
    @patch("recommend_reviewers.datetime")
    @patch("recommend_reviewers.get_collaborators", return_value=["pr-owner"])
    @patch("recommend_reviewers.get_file_contributors")
    @patch("recommend_reviewers.get_pr_details", return_value=None)
    @patch("recommend_reviewers.get_default_branch", return_value="main")
    @patch("recommend_reviewers.get_repo_info", return_value=("octo-org", "widgets"))
    @patch("recommend_reviewers.run_cmd")
    def test_offline_pr_resolution_and_pr_author_exclusion(
        self,
        mock_run_cmd: MagicMock,
        _mock_repo: MagicMock,
        _mock_branch: MagicMock,
        _mock_pr_details: MagicMock,
        mock_contribs: MagicMock,
        _mock_collabs: MagicMock,
        mock_datetime: MagicMock,
        _mock_which: MagicMock,
    ) -> None:
        mock_datetime.now.return_value.timestamp.return_value = 1774000000
        mock_run_cmd.side_effect = lambda args, cwd=None: (
            "true" if "--is-inside-work-tree" in args else ""
        )
        mock_contribs.side_effect = lambda path, cwd, base_ref=None: (
            [
                {
                    "sha": "dddd4444",
                    "email": "pr-owner@widgets.example",
                    "name": "PR Owner",
                    "timestamp": 1773900000,
                }
            ]
            if path == "src/widgets/button.py"
            else []
        )

        out_buf = io.StringIO()
        argv = ["recommend_reviewers.py", "--pr", "99", "--packrat-db", str(self.db_path)]
        with (
            patch.object(sys, "argv", argv),
            redirect_stdout(out_buf),
            redirect_stderr(io.StringIO()),
        ):
            main()

        report = out_buf.getvalue()
        self.assertIn("**PR Target**: #99 Offline Widget Fix (by @pr-owner)", report)
        self.assertIn("**@reviewer-pro**", report)
        self.assertNotIn("| **@pr-owner**", report)
        self.assertIn(
            "gh pr edit 99 --add-reviewer reviewer-pro,dir-reviewer -R octo-org/widgets",
            report,
        )

    @patch("shutil.which", return_value="/usr/bin/gh")
    @patch("recommend_reviewers.datetime")
    @patch("recommend_reviewers.get_collaborators", return_value=[])
    @patch("recommend_reviewers.get_file_contributors")
    @patch(
        "recommend_reviewers.get_changed_files_from_range", return_value=["src/widgets/button.py"]
    )
    @patch("recommend_reviewers.get_default_branch", return_value="main")
    @patch("recommend_reviewers.get_repo_info", return_value=("octo-org", "widgets"))
    @patch("recommend_reviewers.run_cmd")
    def test_exclude_author_flag_and_inferred_branch_author(
        self,
        mock_run_cmd: MagicMock,
        _mock_repo: MagicMock,
        _mock_branch: MagicMock,
        _mock_changed: MagicMock,
        mock_contribs: MagicMock,
        _mock_collabs: MagicMock,
        mock_datetime: MagicMock,
        _mock_which: MagicMock,
    ) -> None:
        mock_datetime.now.return_value.timestamp.return_value = 1774000000
        mock_contribs.side_effect = lambda path, cwd, base_ref=None: (
            [
                {
                    "sha": "aaaa1111",
                    "email": "alice@widgets.example",
                    "name": "Alice Dev",
                    "timestamp": 1773600000,
                }
            ]
            if path == "src/widgets/button.py"
            else []
        )

        # Case 1: explicit --exclude-author excludes reviewer-pro and Alice Dev.
        mock_run_cmd.side_effect = lambda args, cwd=None: (
            "true" if "--is-inside-work-tree" in args else ""
        )
        out_explicit = io.StringIO()
        argv_explicit = [
            "recommend_reviewers.py",
            "--compare",
            "main...feat",
            "--exclude-author",
            "reviewer-pro, alice@widgets.example",
            "--packrat-db",
            str(self.db_path),
        ]
        with (
            patch.object(sys, "argv", argv_explicit),
            redirect_stdout(out_explicit),
            redirect_stderr(io.StringIO()),
        ):
            main()
        text_explicit = out_explicit.getvalue()
        self.assertNotIn("@reviewer-pro", text_explicit)
        self.assertNotIn("@alice-dev", text_explicit)
        self.assertIn("@dir-reviewer", text_explicit)

        # Case 2: inferred single-author local branch excludes Alice Dev automatically.
        def branch_cmd(args: list[str], cwd: str | None = None) -> str:
            if "--is-inside-work-tree" in args:
                return "true"
            if "merge-base" in args:
                return "1111111111111111"
            if args[:3] == ["git", "log", "--format=%ae|%an"]:
                return "alice@widgets.example|Alice Dev"
            return ""

        mock_run_cmd.side_effect = branch_cmd
        out_inferred = io.StringIO()
        argv_inferred = [
            "recommend_reviewers.py",
            "--branch",
            "feat-button",
            "--packrat-db",
            str(self.db_path),
        ]
        with (
            patch.object(sys, "argv", argv_inferred),
            redirect_stdout(out_inferred),
            redirect_stderr(io.StringIO()),
        ):
            main()
        text_inferred = out_inferred.getvalue()
        self.assertNotIn("@alice-dev", text_inferred)
        self.assertIn("@reviewer-pro", text_inferred)

    @patch("shutil.which", return_value="/usr/bin/gh")
    @patch("recommend_reviewers.datetime")
    @patch("recommend_reviewers.get_collaborators", return_value=[])
    @patch("recommend_reviewers.get_file_contributors", return_value=[])
    @patch(
        "recommend_reviewers.get_changed_files_from_range", return_value=["src/widgets/button.py"]
    )
    @patch("recommend_reviewers.get_default_branch", return_value="main")
    @patch("recommend_reviewers.get_repo_info", return_value=("octo-org", "widgets"))
    @patch("recommend_reviewers.run_cmd", return_value="true")
    def test_incomplete_backfill_warning_and_json_output(
        self,
        _mock_run_cmd: MagicMock,
        _mock_repo: MagicMock,
        _mock_branch: MagicMock,
        _mock_changed: MagicMock,
        _mock_contribs: MagicMock,
        _mock_collabs: MagicMock,
        mock_datetime: MagicMock,
        _mock_which: MagicMock,
    ) -> None:
        mock_datetime.now.return_value.timestamp.return_value = 1774000000
        partial_db = self.tmp_dir / "partial.sqlite"
        _seed_end_to_end_db(partial_db, backfill_done_at=None)

        md_buf = io.StringIO()
        argv_md = [
            "recommend_reviewers.py",
            "--compare",
            "main...feat",
            "--packrat-db",
            str(partial_db),
        ]
        with (
            patch.object(sys, "argv", argv_md),
            redirect_stdout(md_buf),
            redirect_stderr(io.StringIO()),
        ):
            main()
        self.assertIn("packrat PR backfill for octo-org/widgets is incomplete", md_buf.getvalue())
        self.assertIn("packrat sync --repo octo-org/widgets", md_buf.getvalue())

        non_gh_buf = io.StringIO()
        with (
            patch("recommend_reviewers.get_repo_info", return_value=(None, None)),
            patch.object(sys, "argv", ["recommend_reviewers.py", "--compare", "main...feat"]),
            redirect_stdout(non_gh_buf),
            redirect_stderr(io.StringIO()),
        ):
            main()
        self.assertNotIn("packrat", non_gh_buf.getvalue())

        json_buf = io.StringIO()
        argv_json = [
            "recommend_reviewers.py",
            "--compare",
            "main...feat",
            "--packrat-db",
            str(self.db_path),
            "--json",
        ]
        with (
            patch.object(sys, "argv", argv_json),
            redirect_stdout(json_buf),
            redirect_stderr(io.StringIO()),
        ):
            main()
        payload = json.loads(json_buf.getvalue())
        self.assertEqual(payload["repo"], "octo-org/widgets")
        self.assertEqual(payload["packrat"]["status"], "ok")
        self.assertTrue(payload["packrat"]["prs_backfill_complete"])
        self.assertEqual(payload["primary"][0]["username"], "reviewer-pro")
        self.assertEqual(payload["primary"][0]["direct_reviews"], 1)
        self.assertEqual(payload["primary"][0]["direct_threads"], 1)
        self.assertEqual(payload["secondary"][0]["username"], "dir-reviewer")
        self.assertEqual(payload["suggested_reviewers"], ["reviewer-pro", "dir-reviewer"])


if __name__ == "__main__":
    unittest.main()
