"""Unit tests for packrat git diff-tree commit, AST symbol, and Markdown section indexer."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from db_store import PackratStore
from git_indexer import (
    _build_git_ssh_env,
    _extract_remote_host,
    resolve_canonical_ref,
    run_git_command,
    sync_git_codebase,
)
from sync_runtime import BoundedCmdResult


def _git(repo_dir: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


class TestGitIndexer(unittest.TestCase):
    """Tests canonical upstream/main resolution, in-memory blob indexing, and diff-tree updates."""

    def test_prefers_upstream_main_and_indexes_blobs_incrementally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            upstream_dir = tmp_path / "upstream.git"
            fork_dir = tmp_path / "fork.git"
            work_dir = tmp_path / "work"

            subprocess.run(["git", "init", "--bare", "-b", "main", str(upstream_dir)], check=True)
            subprocess.run(["git", "init", "--bare", "-b", "main", str(fork_dir)], check=True)
            subprocess.run(["git", "init", "-b", "main", str(work_dir)], check=True)
            _git(work_dir, "config", "user.name", "Test Author")
            _git(work_dir, "config", "user.email", "author@example.com")
            _git(work_dir, "remote", "add", "origin", str(fork_dir))
            _git(work_dir, "remote", "add", "upstream", str(upstream_dir))

            (work_dir / "src").mkdir()
            (work_dir / "src" / "surface.ts").write_text(
                "export class SurfaceController {\n"
                "  renderSurface(id: string): void {\n"
                "    console.log(id);\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            (work_dir / "src" / "legacy.py").write_text(
                "def old_helper(value: int) -> int:\n    return value + 1\n",
                encoding="utf-8",
            )
            (work_dir / "README.md").write_text(
                "# Widgets Architecture\n\n"
                "Overview of the surface protocol.\n\n"
                "## Transport Layer\n\n"
                "Details on JSON-RPC streaming.\n",
                encoding="utf-8",
            )
            _git(work_dir, "add", ".")
            _git(work_dir, "commit", "-m", "feat: initial surface implementation (#12)")
            sha_1 = _git(work_dir, "rev-parse", "HEAD")
            _git(work_dir, "push", "upstream", "main")
            _git(work_dir, "push", "origin", "main")

            # Dirty the working tree with uncommitted text that MUST NOT be indexed
            (work_dir / "src" / "surface.ts").write_text(
                "export class DirtyUncommittedClass {}\n",
                encoding="utf-8",
            )

            store = PackratStore(tmp_path / "repos.sqlite")
            try:
                repo = store.register_repo("octo-org", "widgets", local_path=str(work_dir))
                resolved = resolve_canonical_ref(work_dir, repo)
                self.assertEqual(resolved, ("upstream/main", sha_1))

                stats_1 = sync_git_codebase(store, repo)
                self.assertEqual(stats_1.indexed_ref, "upstream/main")
                self.assertEqual(stats_1.indexed_sha, sha_1)
                self.assertEqual(stats_1.commits_upserted, 1)
                self.assertGreaterEqual(stats_1.symbols_upserted, 2)
                self.assertEqual(stats_1.doc_sections_upserted, 2)

                # Verify dirty worktree class was NOT indexed
                dirty_rows = store.conn.execute(
                    "SELECT * FROM code_symbols WHERE symbol_name = 'DirtyUncommittedClass'"
                ).fetchall()
                self.assertEqual(len(dirty_rows), 0)

                # Verify second run at same SHA is a 0-work no-op
                repo_after_1 = store.get_repo("octo-org/widgets")
                assert repo_after_1 is not None
                stats_noop = sync_git_codebase(store, repo_after_1)
                self.assertEqual(stats_noop.commits_upserted, 0)
                self.assertEqual(stats_noop.symbols_upserted, 0)

                # Now restore worktree, delete src/legacy.py, update src/surface.ts, and push to upstream/main
                _git(work_dir, "checkout", "--", "src/surface.ts")
                _git(work_dir, "rm", "src/legacy.py")
                (work_dir / "src" / "surface.ts").write_text(
                    "export class SurfaceController {\n"
                    "  renderSurface(id: string): void {\n"
                    "    console.log(id);\n"
                    "  }\n"
                    "}\n\n"
                    "export function createSurfaceBus(): void {}\n",
                    encoding="utf-8",
                )
                _git(work_dir, "add", "-A")
                _git(
                    work_dir,
                    "commit",
                    "-m",
                    "refactor: remove legacy helper and add bus (#15)",
                )
                sha_2 = _git(work_dir, "rev-parse", "HEAD")
                _git(work_dir, "push", "upstream", "main")

                stats_2 = sync_git_codebase(store, repo_after_1)
                self.assertEqual(stats_2.indexed_sha, sha_2)
                self.assertEqual(stats_2.commits_upserted, 1)

                # Verify old_helper symbol from deleted src/legacy.py was purged
                legacy_rows = store.conn.execute(
                    "SELECT * FROM code_symbols WHERE file_path = 'src/legacy.py'"
                ).fetchall()
                self.assertEqual(len(legacy_rows), 0)

                # Verify new createSurfaceBus symbol was indexed
                bus_rows = store.conn.execute(
                    "SELECT * FROM code_symbols WHERE symbol_name = 'createSurfaceBus'"
                ).fetchall()
                self.assertEqual(len(bus_rows), 1)

                # Verify force=True full re-index purges stale symbols from deleted files (M2)
                store.conn.execute(
                    """
                    INSERT INTO code_symbols (repo_id, file_path, kind, symbol_name, line_start, line_end, signature, docstring, sha)
                    VALUES (?, 'src/ghost.py', 'function', 'ghost_fn', 1, 2, 'def ghost_fn():', '', 'deadbeef')
                    """,
                    (repo.repo_id,),
                )
                store.conn.commit()
                sync_git_codebase(store, repo_after_1, force=True)
                ghost_rows = store.conn.execute(
                    "SELECT * FROM code_symbols WHERE file_path = 'src/ghost.py'"
                ).fetchall()
                self.assertEqual(len(ghost_rows), 0)
            finally:
                store.close()

    def test_normalize_numstat_rename_paths(self) -> None:
        from git_indexer import _parse_numstat_lines

        records = _parse_numstat_lines(
            1,
            "abc1234",
            [
                "10\t2\tsrc/{old_name.ts => new_name.ts}",
                "5\t0\told_root.py => new_root.py",
                "3\t1\tsrc/regular.ts",
            ],
        )
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0].file_path, "src/new_name.ts")
        self.assertEqual(records[0].old_path, "src/old_name.ts")
        self.assertEqual(records[0].change_type, "R")
        self.assertEqual(records[1].file_path, "new_root.py")
        self.assertEqual(records[1].old_path, "old_root.py")
        self.assertEqual(records[1].change_type, "R")
        self.assertEqual(records[2].file_path, "src/regular.ts")
        self.assertIsNone(records[2].old_path)
        self.assertEqual(records[2].change_type, "M")

    def test_failed_git_fetch_warns_and_file_scan_reports_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_dir = tmp_path / "work"
            subprocess.run(["git", "init", "-b", "main", str(work_dir)], check=True)
            _git(work_dir, "config", "user.name", "Test Author")
            _git(work_dir, "config", "user.email", "author@example.com")
            _git(
                work_dir,
                "remote",
                "add",
                "upstream",
                str(tmp_path / "nonexistent-remote.git"),
            )

            (work_dir / "src").mkdir()
            (work_dir / "src" / "widget.py").write_text(
                "def build_widget() -> int:\n    return 1\n", encoding="utf-8"
            )
            (work_dir / "README.md").write_text("# Widget\n\nDoc text.\n", encoding="utf-8")
            _git(work_dir, "add", ".")
            _git(work_dir, "commit", "-m", "feat: add widget")

            warnings_seen: list[tuple[str, str]] = []
            advances_seen: list[tuple[int, int | None, str]] = []

            class SpyReporter:
                def start_phase(self, repo: str, phase: str, total: int | None = None) -> None:
                    pass

                def advance(
                    self, completed: int, total: int | None = None, *, detail: str = ""
                ) -> None:
                    advances_seen.append((completed, total, detail))

                def finish_phase(self, summary: str = "") -> None:
                    pass

                def warn(self, repo: str, message: str) -> None:
                    warnings_seen.append((repo, message))

                def error(self, repo: str, message: str) -> None:
                    pass

                def clear_active_line(self) -> None:
                    pass

            store = PackratStore(tmp_path / "repos.sqlite")
            try:
                repo = store.register_repo("octo-org", "widgets", local_path=str(work_dir))
                stats = sync_git_codebase(store, repo, fetch=True, reporter=SpyReporter())
                self.assertEqual(len(stats.warnings), 1)
                self.assertIn("git fetch upstream/main failed", stats.warnings[0])
                self.assertEqual(len(warnings_seen), 1)
                self.assertEqual(stats.commits_upserted, 1)
                self.assertEqual(len(advances_seen), 2)
                self.assertEqual(advances_seen[-1][0], 2)
                self.assertEqual(advances_seen[-1][1], 2)
            finally:
                store.close()

    def test_extract_remote_host_variants(self) -> None:
        self.assertEqual(
            _extract_remote_host("git@github.com:octo-org/widgets.git"),
            "github.com",
        )
        self.assertEqual(
            _extract_remote_host("ssh://git@GitHub.COM:22/octo-org/widgets.git"),
            "github.com",
        )
        self.assertEqual(
            _extract_remote_host("https://github.com/octo-org/widgets.git"),
            "github.com",
        )
        self.assertEqual(
            _extract_remote_host("git@github.com-work:octo-org/widgets.git"),
            "github.com-work",
        )
        self.assertEqual(
            _extract_remote_host("git@gitlab.example.com:octo-org/widgets.git"),
            "gitlab.example.com",
        )
        self.assertIsNone(_extract_remote_host("/tmp/local/upstream.git"))
        self.assertIsNone(_extract_remote_host("file:///tmp/local/upstream.git"))
        self.assertIsNone(_extract_remote_host(""))

    def test_build_git_ssh_env_github_ssh_and_https_with_and_without_key(self) -> None:
        base_ssh = "ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            key_path = tmp_path / ".ssh" / "github"
            key_path.parent.mkdir(parents=True)
            key_path.write_text("dummy-private-key\n", encoding="utf-8")
            missing_key = tmp_path / ".ssh" / "missing_github"

            clean_env = {k: v for k, v in os.environ.items() if k != "GIT_SSH_COMMAND"}
            with mock.patch.dict(os.environ, clean_env, clear=True):
                ssh_with_key = _build_git_ssh_env(
                    "git@github.com:octo-org/widgets.git",
                    ssh_key_path=key_path,
                )
                self.assertEqual(ssh_with_key["GIT_TERMINAL_PROMPT"], "0")
                self.assertEqual(ssh_with_key["GCM_INTERACTIVE"], "never")
                self.assertEqual(ssh_with_key["PAGER"], "cat")
                self.assertEqual(
                    ssh_with_key["GIT_SSH_COMMAND"],
                    f"{base_ssh} -i {key_path} -o IdentitiesOnly=yes",
                )

                https_with_key = _build_git_ssh_env(
                    "https://github.com/octo-org/widgets.git",
                    ssh_key_path=key_path,
                )
                self.assertEqual(
                    https_with_key["GIT_SSH_COMMAND"],
                    f"{base_ssh} -i {key_path} -o IdentitiesOnly=yes",
                )

                ssh_without_key = _build_git_ssh_env(
                    "git@github.com:octo-org/widgets.git",
                    ssh_key_path=missing_key,
                )
                self.assertEqual(ssh_without_key["GIT_SSH_COMMAND"], base_ssh)

                https_without_key = _build_git_ssh_env(
                    "https://github.com/octo-org/widgets.git",
                    ssh_key_path=missing_key,
                )
                self.assertEqual(https_without_key["GIT_SSH_COMMAND"], base_ssh)

    def test_build_git_ssh_env_custom_alias_non_github_and_user_override(self) -> None:
        base_ssh = "ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new"
        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "github"
            key_path.write_text("dummy-private-key\n", encoding="utf-8")

            clean_env = {k: v for k, v in os.environ.items() if k != "GIT_SSH_COMMAND"}
            with mock.patch.dict(os.environ, clean_env, clear=True):
                alias_env = _build_git_ssh_env(
                    "git@github.com-work:octo-org/widgets.git",
                    ssh_key_path=key_path,
                )
                self.assertEqual(alias_env["GIT_SSH_COMMAND"], base_ssh)

                gitlab_env = _build_git_ssh_env(
                    "git@gitlab.example.com:octo-org/widgets.git",
                    ssh_key_path=key_path,
                )
                self.assertEqual(gitlab_env["GIT_SSH_COMMAND"], base_ssh)

            custom_cmd = "ssh -F /tmp/custom_ssh_config -o BatchMode=yes"
            with mock.patch.dict(os.environ, {"GIT_SSH_COMMAND": custom_cmd}, clear=False):
                preserved = _build_git_ssh_env(
                    "git@github.com:octo-org/widgets.git",
                    ssh_key_path=key_path,
                )
                self.assertEqual(preserved["GIT_SSH_COMMAND"], custom_cmd)
                self.assertEqual(preserved["GIT_TERMINAL_PROMPT"], "0")
                self.assertEqual(preserved["GCM_INTERACTIVE"], "never")
                self.assertEqual(preserved["PAGER"], "cat")

    def test_resolve_canonical_ref_passes_remote_url_into_fetch_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_home = tmp_path / "home"
            (fake_home / ".ssh").mkdir(parents=True)
            key_path = fake_home / ".ssh" / "github"
            key_path.write_text("dummy-private-key\n", encoding="utf-8")

            work_dir = tmp_path / "work"
            subprocess.run(["git", "init", "-b", "main", str(work_dir)], check=True)
            _git(work_dir, "config", "user.name", "Test Author")
            _git(work_dir, "config", "user.email", "author@example.com")
            _git(
                work_dir,
                "remote",
                "add",
                "upstream",
                "git@github.com-work:octo-org/widgets.git",
            )
            (work_dir / "README.md").write_text("# Widgets\n", encoding="utf-8")
            _git(work_dir, "add", ".")
            _git(work_dir, "commit", "-m", "feat: init")
            expected_sha = _git(work_dir, "rev-parse", "HEAD")

            captured_fetch_envs: list[dict[str, str] | None] = []
            real_run_git = run_git_command

            def spy_run_git(
                repo_dir: Path,
                args: list[str],
                timeout: int = 20,
                extra_env: dict[str, str] | None = None,
            ) -> subprocess.CompletedProcess[str]:
                if args and args[0] == "fetch":
                    captured_fetch_envs.append(extra_env)
                    return subprocess.CompletedProcess(
                        args=args, returncode=0, stdout="", stderr=""
                    )
                return real_run_git(repo_dir, args, timeout=timeout, extra_env=extra_env)

            clean_env = {k: v for k, v in os.environ.items() if k != "GIT_SSH_COMMAND"}
            store = PackratStore(tmp_path / "repos.sqlite")
            try:
                repo = store.register_repo("octo-org", "widgets", local_path=str(work_dir))
                with (
                    mock.patch.dict(os.environ, clean_env, clear=True),
                    mock.patch("git_indexer.Path.home", return_value=fake_home),
                    mock.patch("git_indexer.run_git_command", side_effect=spy_run_git),
                ):
                    resolved = resolve_canonical_ref(work_dir, repo, fetch=True)
                self.assertEqual(resolved, ("main", expected_sha))
                self.assertEqual(len(captured_fetch_envs), 1)
                fetch_env = captured_fetch_envs[0]
                assert fetch_env is not None
                self.assertNotIn("-i ", fetch_env["GIT_SSH_COMMAND"])
                self.assertNotIn("IdentitiesOnly=yes", fetch_env["GIT_SSH_COMMAND"])
            finally:
                store.close()

    def test_fetch_remote_ref_timeout_warns_and_falls_back_to_local_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            upstream_dir = tmp_path / "upstream.git"
            work_dir = tmp_path / "work"
            subprocess.run(["git", "init", "--bare", "-b", "main", str(upstream_dir)], check=True)
            subprocess.run(["git", "init", "-b", "main", str(work_dir)], check=True)
            _git(work_dir, "config", "user.name", "Test Author")
            _git(work_dir, "config", "user.email", "author@example.com")
            _git(work_dir, "remote", "add", "upstream", str(upstream_dir))

            (work_dir / "src").mkdir()
            (work_dir / "src" / "widget.py").write_text(
                "def build_widget() -> int:\n    return 42\n",
                encoding="utf-8",
            )
            _git(work_dir, "add", ".")
            _git(work_dir, "commit", "-m", "feat: initial widget")
            local_sha = _git(work_dir, "rev-parse", "HEAD")
            _git(work_dir, "push", "upstream", "main")

            warnings_seen: list[tuple[str, str]] = []

            class TimeoutSpyReporter:
                def start_phase(self, repo: str, phase: str, total: int | None = None) -> None:
                    pass

                def advance(
                    self, completed: int, total: int | None = None, *, detail: str = ""
                ) -> None:
                    pass

                def finish_phase(self, summary: str = "") -> None:
                    pass

                def warn(self, repo: str, message: str) -> None:
                    warnings_seen.append((repo, message))

                def error(self, repo: str, message: str) -> None:
                    pass

                def clear_active_line(self) -> None:
                    pass

            from sync_runtime import run_bounded_subprocess as real_bounded

            def fake_bounded(
                cmd: list[str],
                *,
                cwd: Path | None = None,
                env: dict[str, str] | None = None,
                timeout: int = 20,
            ) -> BoundedCmdResult:
                if "fetch" in cmd:
                    return BoundedCmdResult(
                        returncode=124,
                        stdout="",
                        stderr=f"git timed out after {timeout}s",
                        timed_out=True,
                    )
                return real_bounded(cmd, cwd=cwd, env=env, timeout=timeout)

            store = PackratStore(tmp_path / "repos.sqlite")
            try:
                repo = store.register_repo("octo-org", "widgets", local_path=str(work_dir))
                with mock.patch("git_indexer.run_bounded_subprocess", side_effect=fake_bounded):
                    stats = sync_git_codebase(
                        store,
                        repo,
                        fetch=True,
                        reporter=TimeoutSpyReporter(),
                    )
                self.assertEqual(
                    stats.warnings,
                    ["git fetch upstream/main timed out; indexing local ref"],
                )
                self.assertEqual(
                    warnings_seen,
                    [
                        (
                            "octo-org/widgets",
                            "git fetch upstream/main timed out; indexing local ref",
                        )
                    ],
                )
                self.assertEqual(stats.indexed_ref, "upstream/main")
                self.assertEqual(stats.indexed_sha, local_sha)
                self.assertEqual(stats.commits_upserted, 1)
            finally:
                store.close()

    def test_run_git_command_reaps_hung_ssh_process_group_on_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_dir = tmp_path / "work"
            subprocess.run(["git", "init", "-b", "main", str(work_dir)], check=True)
            _git(
                work_dir,
                "remote",
                "add",
                "upstream",
                "git@github.com:octo-org/widgets.git",
            )

            hung_ssh = tmp_path / "hung_ssh.sh"
            hung_ssh.write_text(
                "#!/bin/sh\nsleep 30 &\nsleep 30\n",
                encoding="utf-8",
            )
            hung_ssh.chmod(0o755)

            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                run_git_command(
                    work_dir,
                    ["fetch", "--quiet", "upstream", "main"],
                    timeout=1,
                    extra_env={"GIT_SSH_COMMAND": str(hung_ssh)},
                )
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 4.0)


if __name__ == "__main__":
    unittest.main()
