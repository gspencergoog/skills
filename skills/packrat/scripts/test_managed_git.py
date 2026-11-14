"""Unit tests for `managed_git` lifecycle, remote checks, locks, and safety checks."""

from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from pathlib import Path

import managed_git
from models import RepoConfig

FILE_ALLOW = ("-c", "protocol.file.allow=always")


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _init_origin(base: Path, branch: str = "main") -> tuple[Path, Path]:
    """Creates a bare `origin` and a scratch worktree with an initial commit."""
    origin = base / "origin.git"
    work = base / "work"
    subprocess.run(
        ["git", "init", "--bare", f"--initial-branch={branch}", str(origin)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "clone", str(origin), str(work)],
        check=True,
        capture_output=True,
    )
    _git(work, "config", "user.name", "Test")
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "checkout", "-B", branch)
    (work / "README.md").write_text("v1\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "initial commit")
    _git(work, "push", "-u", "origin", branch)
    return origin, work


class TestManagedGit(unittest.TestCase):
    """Covers `file://` bare clone convergence, validation, locks, and renames."""

    def test_validation_rejects_traversal_option_injection_and_bad_urls(self) -> None:
        with self.assertRaises(ValueError):
            managed_git.validate_slug("..", "owner")
        with self.assertRaises(ValueError):
            managed_git.validate_slug(".", "name")
        with self.assertRaises(ValueError):
            managed_git.validate_slug("a/b", "name")
        with self.assertRaises(ValueError):
            managed_git.validate_slug("-bad", "owner")
        with self.assertRaises(ValueError):
            managed_git.validate_branch(".")
        with self.assertRaises(ValueError):
            managed_git.validate_branch("HEAD")
        with self.assertRaises(ValueError):
            managed_git.validate_url("--upload-pack=x")
        with self.assertRaises(ValueError):
            managed_git.validate_url("https://user:pass@github.com/o/r.git")
        with self.assertRaises(ValueError):
            managed_git.validate_url("file:///etc/passwd")
        self.assertEqual(
            managed_git.validate_url("https://github.com/octo/repo.git"),
            "https://github.com/octo/repo.git",
        )
        self.assertEqual(
            managed_git.validate_url("git@github.com:octo/repo.git"),
            "git@github.com:octo/repo.git",
        )

    def test_containment_refuses_symlink_in_git_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "git"
            root.mkdir()
            outside = base / "outside"
            outside.mkdir()
            (root / "octo").symlink_to(outside)
            with self.assertRaises(ValueError):
                managed_git.git_dir(root, "octo", "widgets")

    def test_ensure_git_clean_retry_converge_and_fetch_advance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            origin, work = _init_origin(base, branch="main")
            root = base / "git"
            repo = RepoConfig(
                repo_id=1,
                owner="octo",
                name="widgets",
                full_name="octo/widgets",
                default_branch="main",
                git_source="managed",
                git_url=origin.as_uri(),
            )

            # First attempt ensures bare layout + fetches tip.
            first = managed_git.ensure_git(root, repo, extra_git_config=FILE_ALLOW)
            self.assertEqual(first.error, "")
            self.assertTrue(first.new_tip)
            clone_dir = managed_git.git_dir(root, "octo", "widgets")
            self.assertTrue((clone_dir / "HEAD").exists())
            self.assertFalse((clone_dir / ".packrat-cloning").exists())
            self.assertEqual(
                _git(clone_dir, "config", "--get", "remote.origin.fetch"),
                "+refs/heads/main:refs/heads/main",
            )
            self.assertEqual(
                _git(clone_dir, "config", "--get", "maintenance.auto"), "false"
            )

            # Simulated interrupted clone leaves `.packrat-cloning` + stale `tmp_pack_*`.
            (clone_dir / ".packrat-cloning").write_text(
                "interrupted\n", encoding="utf-8"
            )
            stale_tmp = clone_dir / "objects" / "pack" / "tmp_pack_stale"
            stale_tmp.parent.mkdir(parents=True, exist_ok=True)
            stale_tmp.write_bytes(b"partial")

            # Push a new commit upstream, check_remote, and clean-retry + fetch.
            (work / "README.md").write_text("v2\n", encoding="utf-8")
            _git(work, "commit", "-am", "second")
            _git(work, "push", "origin", "main")
            expected_sha = _git(work, "rev-parse", "HEAD")

            check = managed_git.check_remote(repo, extra_git_config=FILE_ALLOW)
            self.assertEqual(check.error, "")
            self.assertEqual(check.remote_default_branch, "main")
            self.assertEqual(check.new_tip, expected_sha)

            retried = managed_git.ensure_git(root, repo, extra_git_config=FILE_ALLOW)
            self.assertEqual(retried.error, "")
            self.assertFalse(stale_tmp.exists())
            self.assertFalse((clone_dir / ".packrat-cloning").exists())
            self.assertEqual(retried.new_tip, expected_sha)

            # Subsequent fetch after another push advances refs/heads/main.
            (work / "README.md").write_text("v3\n", encoding="utf-8")
            _git(work, "commit", "-am", "third")
            _git(work, "push", "origin", "main")
            v3_sha = _git(work, "rev-parse", "HEAD")
            fetched = managed_git.fetch_git(
                root, repo, maintenance="skip", extra_git_config=FILE_ALLOW
            )
            self.assertEqual(fetched.error, "")
            self.assertEqual(fetched.old_tip, expected_sha)
            self.assertEqual(fetched.new_tip, v3_sha)

    def test_q2_branch_rename_discovery_and_refspec_convergence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            origin, work = _init_origin(base, branch="main")
            root = base / "git"
            repo = RepoConfig(
                repo_id=2,
                owner="octo",
                name="widgets",
                full_name="octo/widgets",
                default_branch="main",
                git_source="managed",
                git_url=origin.as_uri(),
            )
            managed_git.ensure_git(root, repo, extra_git_config=FILE_ALLOW)

            # Rename branch in bare origin and advance tip on the new default branch.
            _git(work, "checkout", "-b", "trunk")
            (work / "README.md").write_text("trunk\n", encoding="utf-8")
            _git(work, "commit", "-am", "rename branch")
            _git(work, "push", "-u", "origin", "trunk")
            _git(origin, "symbolic-ref", "HEAD", "refs/heads/trunk")
            _git(origin, "branch", "-D", "main")
            trunk_sha = _git(work, "rev-parse", "HEAD")

            check = managed_git.check_remote(repo, extra_git_config=FILE_ALLOW)
            self.assertEqual(check.error, "")
            self.assertEqual(check.remote_default_branch, "trunk")

            updated = RepoConfig(
                repo_id=repo.repo_id,
                owner=repo.owner,
                name=repo.name,
                full_name=repo.full_name,
                default_branch=check.remote_default_branch,
                git_source="managed",
                git_url=origin.as_uri(),
            )
            fetched = managed_git.fetch_git(
                root, updated, maintenance="skip", extra_git_config=FILE_ALLOW
            )
            self.assertEqual(fetched.error, "")
            self.assertEqual(fetched.new_tip, trunk_sha)
            clone_dir = managed_git.git_dir(root, "octo", "widgets")
            self.assertEqual(
                _git(
                    clone_dir, "for-each-ref", "--format=%(refname)", "refs/heads/trunk"
                ),
                "refs/heads/trunk",
            )

    def test_lock_skip_case_only_move_and_prune_orphans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            origin, _ = _init_origin(base, branch="main")
            root = base / "git"
            repo = RepoConfig(
                repo_id=3,
                owner="octo",
                name="widgets",
                full_name="octo/widgets",
                default_branch="main",
                git_source="managed",
                git_url=origin.as_uri(),
            )
            managed_git.ensure_git(root, repo, extra_git_config=FILE_ALLOW)

            # Non-blocking lock skips cleanly when lock is held.
            with managed_git._repo_lock(root, repo.repo_id, blocking=True):
                skipped = managed_git.fetch_git(
                    root, repo, blocking_lock=False, extra_git_config=FILE_ALLOW
                )
                self.assertIn("lock", skipped.error)

            # Case-only rename moves directory and updates remote URL.
            moved = managed_git.move_git(
                root,
                repo,
                "octo",
                "Widgets",
                clone_url=origin.as_uri(),
                extra_git_config=FILE_ALLOW,
            )
            self.assertIsNotNone(moved)
            self.assertEqual(moved.name, "Widgets.git")  # type: ignore[union-attr]

            orphan = root / "stale-org" / "old.git"
            orphan.mkdir(parents=True)
            renamed_repo = RepoConfig(
                repo_id=3,
                owner="octo",
                name="Widgets",
                full_name="octo/Widgets",
                default_branch="main",
                git_source="managed",
                git_url=origin.as_uri(),
            )
            dry = managed_git.prune_orphans(root, [renamed_repo], dry_run=True)
            self.assertEqual(dry, [orphan])
            self.assertTrue(orphan.exists())
            live = managed_git.prune_orphans(root, [renamed_repo], dry_run=False)
            self.assertEqual(live, [orphan])
            self.assertFalse(orphan.exists())
            self.assertTrue(managed_git.remove_git(root, renamed_repo))
            self.assertFalse(moved.exists())  # type: ignore[union-attr]

    def test_initial_clone_discovers_non_main_default_branch_and_fetch_recovers_rename(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            origin, work = _init_origin(base, branch="master")
            root = base / "git"
            repo = RepoConfig(
                repo_id=4,
                owner="octo",
                name="legacy",
                full_name="octo/legacy",
                default_branch="main",
                git_source="managed",
                git_url=origin.as_uri(),
            )
            cloned = managed_git.ensure_git(root, repo, extra_git_config=FILE_ALLOW)
            self.assertEqual(cloned.error, "")
            self.assertEqual(cloned.remote_default_branch, "master")
            self.assertTrue(cloned.new_tip)

            # Rename master -> trunk and delete master; fetch_git recovers without prior poll.
            _git(work, "checkout", "-b", "trunk")
            (work / "README.md").write_text("trunk\n", encoding="utf-8")
            _git(work, "commit", "-am", "switch to trunk")
            _git(work, "push", "-u", "origin", "trunk")
            _git(origin, "symbolic-ref", "HEAD", "refs/heads/trunk")
            _git(origin, "branch", "-D", "master")
            trunk_sha = _git(work, "rev-parse", "HEAD")

            stale_repo = RepoConfig(
                repo_id=4,
                owner="octo",
                name="legacy",
                full_name="octo/legacy",
                default_branch="master",
                git_source="managed",
                git_url=origin.as_uri(),
            )
            fetched = managed_git.fetch_git(
                root, stale_repo, maintenance="skip", extra_git_config=FILE_ALLOW
            )
            self.assertEqual(fetched.error, "")
            self.assertEqual(fetched.remote_default_branch, "trunk")
            self.assertEqual(fetched.new_tip, trunk_sha)

    def test_ensure_git_accepts_real_stderr_progress_reporter(self) -> None:
        from progress import StderrProgressReporter

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            origin, _ = _init_origin(base, branch="main")
            root = base / "git"
            repo = RepoConfig(
                repo_id=5,
                owner="octo",
                name="reported",
                full_name="octo/reported",
                default_branch="main",
                git_source="managed",
                git_url=origin.as_uri(),
            )
            buf = io.StringIO()
            reporter = StderrProgressReporter(mode="plain", stream=buf, grace_sec=0.0)
            res = managed_git.ensure_git(
                root, repo, reporter=reporter, extra_git_config=FILE_ALLOW
            )
            self.assertEqual(res.error, "")
            out = buf.getvalue()
            self.assertIn("[octo/reported] git clone:", out)
            self.assertIn("[octo/reported] cloned main (", out)


if __name__ == "__main__":
    unittest.main()
