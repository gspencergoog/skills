"""Unit tests for `git_cmd` isolated subprocess helpers and import layering."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import git_cmd


class TestGitCmd(unittest.TestCase):
    """Covers `GIT_ISOLATED_ENV`, protocol allowlisting, SSH env, and import layering."""

    def test_build_git_ssh_env_github_and_custom_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "github"
            key.write_text("dummy", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                gh_env = git_cmd.build_git_ssh_env(
                    "git@github.com:octo/repo.git", ssh_key_path=key
                )
                self.assertIn(str(key), gh_env["GIT_SSH_COMMAND"])
                self.assertIn("IdentitiesOnly=yes", gh_env["GIT_SSH_COMMAND"])
                https_env = git_cmd.build_git_ssh_env(
                    "https://github.com/octo/repo.git", ssh_key_path=key
                )
                self.assertIn(str(key), https_env["GIT_SSH_COMMAND"])
                other_env = git_cmd.build_git_ssh_env(
                    "git@gitlab.com:octo/repo.git", ssh_key_path=key
                )
                self.assertNotIn("IdentitiesOnly=yes", other_env["GIT_SSH_COMMAND"])
            with mock.patch.dict(
                os.environ, {"GIT_SSH_COMMAND": "ssh -i /custom/id"}, clear=True
            ):
                preserved = git_cmd.build_git_ssh_env("git@github.com:octo/repo.git")
                self.assertEqual(preserved["GIT_SSH_COMMAND"], "ssh -i /custom/id")

    def test_isolated_env_blocks_file_protocol_unless_fixture_enables_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            origin = Path(tmp) / "origin.git"
            subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
            url = origin.as_uri()
            blocked = git_cmd.run_git_command(None, ["ls-remote", "--", url])
            self.assertNotEqual(blocked.returncode, 0)
            allowed = git_cmd.run_git_command(
                None,
                ["ls-remote", "--", url],
                extra_config=("-c", "protocol.file.allow=always"),
            )
            self.assertEqual(allowed.returncode, 0, allowed.stderr)

    def test_fresh_interpreter_imports_have_no_cycles(self) -> None:
        scripts_dir = str(Path(__file__).resolve().parent)
        for mod in ("git_cmd", "managed_git", "db_store", "git_indexer"):
            proc = subprocess.run(
                [sys.executable, "-c", f"import {mod}"],
                cwd=scripts_dir,
                env={**os.environ, "PYTHONPATH": scripts_dir},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, f"{mod}: {proc.stderr}")


if __name__ == "__main__":
    unittest.main()
