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

"""Unit and integration tests for git_signals collectors and remote parsers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import MagicMock, patch

try:
    from git_signals import (
        collect_blame_touches,
        collect_file_commits,
        get_canonical_repo_info,
        get_default_branch,
        infer_local_change_author,
        parse_github_remote_url,
        resolve_comparison_targets,
        run_bounded_cmd,
    )
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from git_signals import (
        collect_blame_touches,
        collect_file_commits,
        get_canonical_repo_info,
        get_default_branch,
        infer_local_change_author,
        parse_github_remote_url,
        resolve_comparison_targets,
        run_bounded_cmd,
    )

SHA_ALICE = "a" * 40
SHA_BOB = "b" * 40
SHA_CAROL = "c" * 40
SHA_ZERO = "0" * 40
SHA_BASE = "1234567890abcdef1234567890abcdef12345678"


def _porcelain_block(
    sha: str,
    lineno: int,
    name: str,
    email: str,
    timestamp: int,
    filename: str = "src/widgets/button.py",
) -> str:
    """Builds a synthetic ``git blame --line-porcelain`` record for one line."""
    return "\n".join(
        [
            f"{sha} {lineno} {lineno} 1",
            f"author {name}",
            f"author-mail <{email}>",
            f"author-time {timestamp}",
            "author-tz +0000",
            f"filename {filename}",
            "\treturn True",
        ]
    )


def _git(cwd: str, *args: str, env: dict[str, str] | None = None) -> str:
    """Runs a real git command inside a temporary test repository."""
    merged_env = {**os.environ, **(env or {})}
    res = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=merged_env,
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


class TestBoundedCommandAndRemoteParsing(unittest.TestCase):
    """Tests bounded subprocess execution and GitHub remote URL parsing."""

    @patch("git_signals.subprocess.run")
    def test_run_bounded_cmd_merges_non_interactive_env(self, mock_run: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.stdout = "  ok-output \n"
        mock_run.return_value = mock_proc

        result = run_bounded_cmd(["git", "status"], cwd="/tmp/widgets", timeout=9)
        self.assertEqual(result, "ok-output")

        args, kwargs = mock_run.call_args
        self.assertEqual(args[0], ["git", "status"])
        self.assertEqual(kwargs["cwd"], "/tmp/widgets")
        self.assertEqual(kwargs["timeout"], 9)
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])
        self.assertTrue(kwargs["check"])
        self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(kwargs["env"]["GH_PROMPT_DISABLED"], "1")

    @patch("git_signals.subprocess.run")
    def test_run_bounded_cmd_returns_empty_on_errors(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.CalledProcessError(128, ["git", "status"])
        self.assertEqual(run_bounded_cmd(["git", "status"]), "")

        mock_run.side_effect = subprocess.TimeoutExpired(["git", "fetch"], timeout=5)
        self.assertEqual(run_bounded_cmd(["git", "fetch"], timeout=5), "")

        mock_run.side_effect = FileNotFoundError("git not found")
        self.assertEqual(run_bounded_cmd(["git", "status"]), "")

    def test_parse_github_remote_url_preserves_suffix_characters(self) -> None:
        self.assertEqual(
            parse_github_remote_url("https://github.com/octo-org/packrat.git"),
            ("octo-org", "packrat"),
        )
        self.assertEqual(
            parse_github_remote_url("git@github.com:octo-org/octo.js.git"),
            ("octo-org", "octo.js"),
        )
        self.assertEqual(
            parse_github_remote_url("git@github.com-work:octo-org/widgets.git"),
            ("octo-org", "widgets"),
        )
        self.assertEqual(
            parse_github_remote_url("ssh://git@github.com:22/octo-org/widgets.git/"),
            ("octo-org", "widgets"),
        )
        self.assertEqual(
            parse_github_remote_url("https://token@github.com/octo-org/widgets"),
            ("octo-org", "widgets"),
        )

    def test_parse_github_remote_url_rejects_non_github_urls(self) -> None:
        self.assertIsNone(parse_github_remote_url(""))
        self.assertIsNone(parse_github_remote_url("https://gitlab.com/octo-org/widgets.git"))
        self.assertIsNone(parse_github_remote_url("https://mygithub.com/octo-org/widgets.git"))
        self.assertIsNone(parse_github_remote_url("git@bitbucket.org:octo-org/widgets.git"))
        self.assertIsNone(parse_github_remote_url("/var/repos/widgets.git"))
        self.assertIsNone(parse_github_remote_url("https://github.com/octo-org"))
        self.assertIsNone(parse_github_remote_url("https://github.com/octo-org/widgets/sub"))


class TestCanonicalRepoInfoAndTargets(unittest.TestCase):
    """Tests canonical remote selection, fork parent lookup, and comparison targets."""

    def test_upstream_github_remote_wins_without_calling_gh(self) -> None:
        calls: list[list[str]] = []

        def fake_runner(args: Sequence[str], _cwd: str | None) -> str:
            calls.append(list(args))
            return (
                "origin\tgit@github.com:fork-dev/widgets.git (fetch)\n"
                "upstream\thttps://github.com/octo-org/widgets.git (fetch)\n"
            )

        self.assertEqual(
            get_canonical_repo_info("/repo", runner=fake_runner),
            ("octo-org", "widgets"),
        )
        self.assertEqual(calls, [["git", "remote", "-v"]])

    def test_non_github_remotes_skip_gh_immediately(self) -> None:
        calls: list[list[str]] = []

        def fake_runner(args: Sequence[str], _cwd: str | None) -> str:
            calls.append(list(args))
            return (
                "origin\thttps://gitlab.com/octo-org/widgets.git (fetch)\n"
                "upstream\tssh://git@gitlab.com/octo-org/widgets.git (fetch)\n"
            )

        self.assertEqual(get_canonical_repo_info("/repo", runner=fake_runner), (None, None))
        self.assertEqual(calls, [["git", "remote", "-v"]])

    @patch("shutil.which", return_value="/usr/bin/gh")
    def test_origin_fork_resolves_parent_via_gh(self, _mock_which: MagicMock) -> None:
        calls: list[list[str]] = []
        gh_payload = json.dumps(
            {
                "name": "widgets",
                "owner": {"login": "fork-dev"},
                "parent": {"name": "widgets", "owner": {"login": "octo-org"}},
            }
        )

        def fake_runner(args: Sequence[str], _cwd: str | None) -> str:
            calls.append(list(args))
            if args[:2] == ["git", "remote"]:
                return "origin\tgit@github.com-dev:fork-dev/widgets.git (fetch)\n"
            if args[:3] == ["gh", "repo", "view"]:
                return gh_payload
            return ""

        self.assertEqual(
            get_canonical_repo_info("/repo", runner=fake_runner),
            ("octo-org", "widgets"),
        )
        self.assertIn(["gh", "repo", "view", "--json", "owner,name,parent"], calls)

    @patch("shutil.which", return_value="/usr/bin/gh")
    def test_origin_non_fork_and_gh_failure_fallbacks(self, _mock_which: MagicMock) -> None:
        non_fork_json = json.dumps(
            {"name": "packrat", "owner": {"login": "octo-org"}, "parent": None}
        )

        def non_fork_runner(args: Sequence[str], _cwd: str | None) -> str:
            if args[:2] == ["git", "remote"]:
                return (
                    "upstream\thttps://gitlab.com/mirror/packrat.git (fetch)\n"
                    "origin\thttps://github.com/octo-org/packrat.git (fetch)\n"
                )
            return non_fork_json

        self.assertEqual(
            get_canonical_repo_info("/repo", runner=non_fork_runner),
            ("octo-org", "packrat"),
        )

        def broken_gh_runner(args: Sequence[str], _cwd: str | None) -> str:
            if args[:2] == ["git", "remote"]:
                return "mirror\thttps://github.com/octo-org/octo.js.git (fetch)\n"
            return "{not-valid-json"

        self.assertEqual(
            get_canonical_repo_info("/repo", runner=broken_gh_runner),
            ("octo-org", "octo.js"),
        )

    def test_get_default_branch_prefers_upstream_then_origin_then_refs(self) -> None:
        self.assertEqual(
            get_default_branch(
                "/repo",
                runner=lambda args, _cwd: (
                    "refs/remotes/upstream/develop" if "upstream/HEAD" in args[-1] else ""
                ),
            ),
            "develop",
        )

        def master_ref_runner(args: Sequence[str], _cwd: str | None) -> str:
            if args[1] == "for-each-ref":
                return "refs/remotes/origin/master\nrefs/heads/feature\n"
            return ""

        self.assertEqual(get_default_branch("/repo", runner=master_ref_runner), "master")

    def test_resolve_comparison_targets_prefers_upstream_over_origin(self) -> None:
        def fake_runner(args: Sequence[str], _cwd: str | None) -> str:
            if args[1] == "for-each-ref":
                return "refs/remotes/upstream/main\nrefs/remotes/origin/main\n"
            if args[1] == "merge-base":
                self.assertEqual(args[2:], ["upstream/main", "feat/ui"])
                return SHA_BASE
            return ""

        diff_range, merge_base, head_ref = resolve_comparison_targets(
            "/repo", "main", branch="feat/ui", runner=fake_runner
        )
        self.assertEqual(diff_range, "upstream/main...feat/ui")
        self.assertEqual(merge_base, SHA_BASE)
        self.assertEqual(head_ref, "feat/ui")

    def test_resolve_comparison_targets_explicit_compare_variants(self) -> None:
        def fake_runner(args: Sequence[str], _cwd: str | None) -> str:
            if args[1] in {"merge-base", "rev-parse"}:
                return SHA_BASE
            return ""

        self.assertEqual(
            resolve_comparison_targets(
                "/repo", "main", compare="origin/main...feat/a", runner=fake_runner
            ),
            ("origin/main...feat/a", SHA_BASE, "feat/a"),
        )
        self.assertEqual(
            resolve_comparison_targets("/repo", "main", compare="HEAD~2..HEAD", runner=fake_runner),
            ("HEAD~2..HEAD", SHA_BASE, "HEAD"),
        )
        self.assertEqual(
            resolve_comparison_targets("/repo", "main", compare="HEAD~1", runner=fake_runner),
            ("HEAD~1", SHA_BASE, None),
        )


class TestCommitHistoryAndAuthorInference(unittest.TestCase):
    """Tests base-branch git log collection and local author inference."""

    def test_collect_file_commits_uses_base_ref_and_deduplicates_dir_shas(self) -> None:
        calls: list[list[str]] = []
        logs_by_target = {
            "src/widgets/button.py": f"{SHA_ALICE}|alice@widgets.example|Alice Dev|1770000100\n",
            "src/widgets/slider.py": f"{SHA_BOB}|bob@widgets.example|Bob Maintainer|1770000200\n",
            "README.md": f"{SHA_ALICE}|alice@widgets.example|Alice Dev|1770000050\n",
            "src/widgets": (
                f"{SHA_ALICE}|alice@widgets.example|Alice Dev|1770000100\n"
                f"{SHA_BOB}|bob@widgets.example|Bob Maintainer|1770000200\n"
                f"{SHA_CAROL}|carol@widgets.example|Carol Reviewer|1770000300\n"
            ),
        }

        def fake_runner(args: Sequence[str], _cwd: str | None) -> str:
            calls.append(list(args))
            return logs_by_target.get(args[-1], "")

        touches = collect_file_commits(
            ["./src/widgets/button.py", "src/widgets/slider.py", "README.md"],
            "/repo",
            base_ref="upstream/main",
            runner=fake_runner,
        )

        for call in calls:
            self.assertEqual(
                call[:-1],
                ["git", "log", "-n", "50", "--format=%H|%ae|%an|%at", "upstream/main", "--"],
            )
        queried_targets = [call[-1] for call in calls]
        self.assertEqual(
            queried_targets,
            ["src/widgets/button.py", "src/widgets/slider.py", "README.md", "src/widgets"],
        )

        direct = [t for t in touches if not t.is_directory]
        directory = [t for t in touches if t.is_directory]
        self.assertEqual(len(direct), 3)
        self.assertEqual(len(directory), 1)
        self.assertEqual(directory[0].sha, SHA_CAROL)
        self.assertEqual(directory[0].scope_path, "src/widgets")

    def test_infer_local_change_author_single_and_multi_author_branches(self) -> None:
        def single_runner(args: Sequence[str], _cwd: str | None) -> str:
            self.assertEqual(args, ["git", "log", "--format=%ae|%an", f"{SHA_BASE}..feat/x"])
            return "alice@widgets.example|Alice Dev\nALICE@widgets.example|Alice Dev\n"

        self.assertEqual(
            infer_local_change_author("/repo", SHA_BASE, "feat/x", runner=single_runner),
            ("alice@widgets.example", "Alice Dev"),
        )

        def multi_runner(_args: Sequence[str], _cwd: str | None) -> str:
            return "alice@widgets.example|Alice Dev\nbob@widgets.example|Bob Maintainer\n"

        self.assertEqual(
            infer_local_change_author("/repo", SHA_BASE, "feat/x", runner=multi_runner),
            (),
        )

    def test_infer_local_change_author_uncommitted_diff_and_empty_mock(self) -> None:
        def uncommitted_runner(args: Sequence[str], _cwd: str | None) -> str:
            if args[:2] == ["git", "log"]:
                return ""
            if args == ["git", "config", "user.email"]:
                return "carol@widgets.example"
            if args == ["git", "config", "user.name"]:
                return "Carol Dev"
            return ""

        self.assertEqual(
            infer_local_change_author("/repo", SHA_BASE, None, runner=uncommitted_runner),
            ("carol@widgets.example", "Carol Dev"),
        )

        self.assertEqual(
            infer_local_change_author("/repo", SHA_BASE, "feat/x", runner=lambda _a, _c: ""),
            (),
        )
        self.assertEqual(
            infer_local_change_author("/repo", None, "feat/x", runner=lambda _a, _c: "ignored"),
            (),
        )
        self.assertEqual(
            infer_local_change_author("/repo", SHA_BASE, None, runner=lambda _a, _c: ""),
            (),
        )


class TestBlameCollectorAndRealGitIntegration(unittest.TestCase):
    """Tests diff hunk blame extraction, filtering, and real git integration."""

    def test_collect_blame_touches_noop_when_merge_base_missing(self) -> None:
        called = False

        def fail_runner(_args: Sequence[str], _cwd: str | None) -> str:
            nonlocal called
            called = True
            return ""

        self.assertEqual(
            collect_blame_touches(["src/widgets/button.py"], "/repo", None, runner=fail_runner),
            (),
        )
        self.assertFalse(called)

    def test_collect_blame_touches_hunks_insertions_zeros_and_bots(self) -> None:
        blame_ranges: list[str] = []
        diffs_by_file = {
            "src/widgets/added.py": "@@ -0,0 +1,15 @@\n+new file\n",
            "assets/logo.png": "Binary files a/assets/logo.png and b/assets/logo.png differ\n",
            "src/widgets/button.py": (
                "@@ -10,0 +11,4 @@\n@@ -42 +46 @@ def render():\n@@ -60,3 +64,2 @@ def click():"
            ),
        }
        blames_by_range = {
            "42,+1": _porcelain_block(
                SHA_ALICE, 42, "Alice Dev", "alice@widgets.example", 1770000100
            ),
            "60,+3": "\n".join(
                [
                    _porcelain_block(
                        SHA_ALICE, 60, "Alice Dev", "alice@widgets.example", 1770000100
                    ),
                    _porcelain_block(
                        SHA_ZERO, 61, "Not Committed Yet", "not.committed.yet", 1770000200
                    ),
                    _porcelain_block(
                        SHA_BOB,
                        62,
                        "dependabot[bot]",
                        "49699333+dependabot[bot]@users.noreply.github.com",
                        1770000150,
                    ),
                ]
            ),
        }

        def fake_runner(args: Sequence[str], _cwd: str | None) -> str:
            if args[1] == "diff":
                return diffs_by_file.get(args[-1], "")
            if args[1] == "blame":
                blame_ranges.append(args[4])
                return blames_by_range.get(args[4], "")
            return ""

        touches = collect_blame_touches(
            ["src/widgets/added.py", "assets/logo.png", "src/widgets/button.py"],
            "/repo",
            SHA_BASE,
            head_ref="feat/ui",
            runner=fake_runner,
        )

        self.assertEqual(blame_ranges, ["42,+1", "60,+3"])
        self.assertEqual(len(touches), 1)
        self.assertEqual(touches[0].sha, SHA_ALICE)
        self.assertEqual(touches[0].file_path, "src/widgets/button.py")
        self.assertEqual(touches[0].line_count, 2)

    def test_collect_blame_touches_enforces_hunk_and_line_caps(self) -> None:
        blame_ranges: list[str] = []
        diff_lines = [
            "@@ -1,90 +1,90 @@",
            "@@ -100,90 +100,90 @@",
            "@@ -200,50 +200,50 @@",
            "@@ -300,10 +300,10 @@",
        ]

        def fake_runner(args: Sequence[str], _cwd: str | None) -> str:
            if args[1] == "diff":
                return "\n".join(diff_lines)
            if args[1] == "blame":
                blame_ranges.append(args[4])
            return ""

        collect_blame_touches(["src/widgets/large.py"], "/repo", SHA_BASE, runner=fake_runner)
        self.assertEqual(blame_ranges, ["1,+90", "100,+90", "200,+20"])

    def test_real_git_repository_end_to_end_signals(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _git(tmpdir, "init", "-b", "main")
            _git(tmpdir, "config", "user.name", "Carol Author")
            _git(tmpdir, "config", "user.email", "carol@widgets.example")
            _git(tmpdir, "remote", "add", "upstream", "https://github.com/octo-org/widgets.git")
            _git(tmpdir, "remote", "add", "origin", "git@github.com:carol/widgets.git")

            widgets_dir = Path(tmpdir) / "src" / "widgets"
            widgets_dir.mkdir(parents=True)
            button_path = widgets_dir / "button.py"
            sibling_path = widgets_dir / "theme.py"

            button_path.write_text("line1\nline2\nline3\nline4\n", encoding="utf-8")
            _git(tmpdir, "add", "src/widgets/button.py")
            _git(
                tmpdir,
                "commit",
                "-m",
                "Add button",
                env={
                    "GIT_AUTHOR_NAME": "Alice Maintainer",
                    "GIT_AUTHOR_EMAIL": "alice@widgets.example",
                    "GIT_COMMITTER_NAME": "Alice Maintainer",
                    "GIT_COMMITTER_EMAIL": "alice@widgets.example",
                },
            )

            sibling_path.write_text("THEME = 'light'\n", encoding="utf-8")
            button_path.write_text("line1\nline2_bob\nline3_bob\nline4\n", encoding="utf-8")
            _git(tmpdir, "add", "src/widgets/theme.py", "src/widgets/button.py")
            _git(
                tmpdir,
                "commit",
                "-m",
                "Update button and add theme",
                env={
                    "GIT_AUTHOR_NAME": "Bob Reviewer",
                    "GIT_AUTHOR_EMAIL": "bob@widgets.example",
                    "GIT_COMMITTER_NAME": "Bob Reviewer",
                    "GIT_COMMITTER_EMAIL": "bob@widgets.example",
                },
            )

            _git(tmpdir, "checkout", "-b", "feat/button-polish")
            button_path.write_text("line1_carol\nline2_bob\nline3_carol\nline4\n", encoding="utf-8")
            (widgets_dir / "new_widget.py").write_text("NEW = True\n", encoding="utf-8")
            _git(tmpdir, "add", ".")
            _git(tmpdir, "commit", "-m", "Polish button")

            self.assertEqual(get_canonical_repo_info(tmpdir), ("octo-org", "widgets"))
            self.assertEqual(get_default_branch(tmpdir), "main")

            diff_range, merge_base, head_ref = resolve_comparison_targets(tmpdir, "main")
            self.assertEqual(diff_range, "main...feat/button-polish")
            self.assertIsNotNone(merge_base)
            self.assertEqual(head_ref, "feat/button-polish")

            self.assertEqual(
                infer_local_change_author(tmpdir, merge_base, head_ref),
                ("carol@widgets.example", "Carol Author"),
            )

            commits = collect_file_commits(
                ["src/widgets/button.py", "src/widgets/new_widget.py"],
                tmpdir,
                base_ref=merge_base,
            )
            commit_emails = {c.email for c in commits}
            self.assertEqual(commit_emails, {"alice@widgets.example", "bob@widgets.example"})
            self.assertNotIn("carol@widgets.example", commit_emails)

            blame = collect_blame_touches(
                ["src/widgets/button.py", "src/widgets/new_widget.py"],
                tmpdir,
                merge_base,
                head_ref=head_ref,
            )
            by_email = {b.email: b.line_count for b in blame}
            self.assertEqual(
                by_email,
                {"alice@widgets.example": 1, "bob@widgets.example": 1},
            )

    def test_real_git_deleted_file_and_uncommitted_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _git(tmpdir, "init", "-b", "main")
            _git(tmpdir, "config", "user.name", "Carol Author")
            _git(tmpdir, "config", "user.email", "carol@widgets.example")

            legacy_file = Path(tmpdir) / "src" / "legacy.py"
            legacy_file.parent.mkdir(parents=True)
            legacy_file.write_text("OLD_A = 1\nOLD_B = 2\n", encoding="utf-8")
            _git(tmpdir, "add", "src/legacy.py")
            _git(
                tmpdir,
                "commit",
                "-m",
                "Add legacy module",
                env={
                    "GIT_AUTHOR_NAME": "Alice Maintainer",
                    "GIT_AUTHOR_EMAIL": "alice@widgets.example",
                    "GIT_COMMITTER_NAME": "Alice Maintainer",
                    "GIT_COMMITTER_EMAIL": "alice@widgets.example",
                },
            )

            legacy_file.unlink()
            diff_range, merge_base, head_ref = resolve_comparison_targets(tmpdir, "main")
            self.assertEqual(diff_range, "HEAD")
            self.assertIsNotNone(merge_base)
            self.assertIsNone(head_ref)

            self.assertEqual(
                infer_local_change_author(tmpdir, merge_base, head_ref),
                ("carol@widgets.example", "Carol Author"),
            )
            blame = collect_blame_touches(["src/legacy.py"], tmpdir, merge_base, head_ref=head_ref)
            self.assertEqual(len(blame), 1)
            self.assertEqual(blame[0].email, "alice@widgets.example")
            self.assertEqual(blame[0].line_count, 2)


if __name__ == "__main__":
    unittest.main()
