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

            subprocess.run(
                ["git", "init", "--bare", "-b", "main", str(upstream_dir)], check=True
            )
            subprocess.run(
                ["git", "init", "--bare", "-b", "main", str(fork_dir)], check=True
            )
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
                repo = store.register_repo(
                    "octo-org", "widgets", local_path=str(work_dir)
                )
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

    def test_commit_message_mentions_queue_timeline_refreshes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_dir = tmp_path / "work"
            subprocess.run(["git", "init", "-b", "main", str(work_dir)], check=True)
            _git(work_dir, "config", "user.name", "Test Author")
            _git(work_dir, "config", "user.email", "author@example.com")
            (work_dir / "README.md").write_text("# Widget\n", encoding="utf-8")
            _git(work_dir, "add", ".")
            _git(
                work_dir,
                "commit",
                "-m",
                "fix: handle empty input (#30)",
                "-m",
                "Fixes #12\n\nSee also octo-org/gadgets#4 and #30.",
            )

            store = PackratStore(tmp_path / "repos.sqlite")
            try:
                repo = store.register_repo(
                    "octo-org", "widgets", local_path=str(work_dir)
                )
                store.register_repo("octo-org", "gadgets")
                stats = sync_git_codebase(store, repo)
                self.assertEqual(stats.commits_upserted, 1)
                rows = store.conn.execute(
                    """
                    SELECT r.full_name, d.item_number, d.reason
                    FROM timeline_dirty d JOIN repos r USING (repo_id)
                    ORDER BY r.full_name, d.item_number
                    """
                ).fetchall()
                # The squash-merge PR (#30) is the commit's own source, so it is skipped.
                self.assertEqual(
                    [tuple(r) for r in rows],
                    [
                        ("octo-org/gadgets", 4, "commit"),
                        ("octo-org/widgets", 12, "commit"),
                    ],
                )
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
            (work_dir / "README.md").write_text(
                "# Widget\n\nDoc text.\n", encoding="utf-8"
            )
            _git(work_dir, "add", ".")
            _git(work_dir, "commit", "-m", "feat: add widget")

            warnings_seen: list[tuple[str, str]] = []
            advances_seen: list[tuple[int, int | None, str]] = []

            class SpyReporter:
                def start_phase(
                    self, repo: str, phase: str, total: int | None = None
                ) -> None:
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
                repo = store.register_repo(
                    "octo-org", "widgets", local_path=str(work_dir)
                )
                stats = sync_git_codebase(
                    store, repo, fetch=True, reporter=SpyReporter()
                )
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
            with mock.patch.dict(
                os.environ, {"GIT_SSH_COMMAND": custom_cmd}, clear=False
            ):
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
                return real_run_git(
                    repo_dir, args, timeout=timeout, extra_env=extra_env
                )

            clean_env = {k: v for k, v in os.environ.items() if k != "GIT_SSH_COMMAND"}
            store = PackratStore(tmp_path / "repos.sqlite")
            try:
                repo = store.register_repo(
                    "octo-org", "widgets", local_path=str(work_dir)
                )
                with (
                    mock.patch.dict(os.environ, clean_env, clear=True),
                    mock.patch("git_cmd.Path.home", return_value=fake_home),
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
            subprocess.run(
                ["git", "init", "--bare", "-b", "main", str(upstream_dir)], check=True
            )
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
                def start_phase(
                    self, repo: str, phase: str, total: int | None = None
                ) -> None:
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
                repo = store.register_repo(
                    "octo-org", "widgets", local_path=str(work_dir)
                )
                with mock.patch(
                    "git_cmd.run_bounded_subprocess", side_effect=fake_bounded
                ):
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

    def test_force_reindex_cleans_up_stale_code_symbol_chunks_from_deleted_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_dir = tmp_path / "work"
            subprocess.run(["git", "init", "-b", "main", str(work_dir)], check=True)
            _git(work_dir, "config", "user.name", "Test Author")
            _git(work_dir, "config", "user.email", "author@example.com")

            (work_dir / "keep.py").write_text(
                "def keep_fn() -> int:\n    return 1\n", encoding="utf-8"
            )
            (work_dir / "gone.py").write_text(
                "def gone_fn() -> int:\n    return 2\n", encoding="utf-8"
            )
            _git(work_dir, "add", ".")
            _git(work_dir, "commit", "-m", "feat: add keep and gone")

            store = PackratStore(tmp_path / "repos.sqlite")
            try:
                repo = store.register_repo(
                    "octo-org", "widgets", local_path=str(work_dir)
                )
                sync_git_codebase(store, repo)

                # Simulate an orphaned code_symbol chunk whose code_symbols row was removed
                # while its file was deleted from the tree before a force re-index.
                store.conn.execute(
                    "DELETE FROM code_symbols WHERE repo_id = ? AND file_path = 'gone.py'",
                    (repo.repo_id,),
                )
                store.conn.commit()
                (work_dir / "gone.py").unlink()
                _git(work_dir, "add", "-A")
                _git(work_dir, "commit", "-m", "chore: remove gone.py")

                repo = store.get_repo("octo-org/widgets")
                assert repo is not None
                sync_git_codebase(store, repo, force=True)

                remaining_chunks = store.conn.execute(
                    "SELECT file_path FROM chunks WHERE repo_id = ? AND source_kind = 'code_symbol'",
                    (repo.repo_id,),
                ).fetchall()
                self.assertEqual(
                    [r["file_path"] for r in remaining_chunks], ["keep.py"]
                )
            finally:
                store.close()

    def test_python_blob_with_invalid_escape_does_not_emit_syntax_warning(self) -> None:
        import warnings

        from blob_parsers import extract_content_entities

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", SyntaxWarning)
            entities = extract_content_entities(
                "legacy.py", 'def f():\n    return "\\ "\n'
            )
        self.assertEqual([e["name"] for e in entities], ["f"])
        self.assertEqual(caught, [])

    def test_commit_timestamps_are_normalized_to_utc_z(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_dir = tmp_path / "work"
            subprocess.run(["git", "init", "-b", "main", str(work_dir)], check=True)
            _git(work_dir, "config", "user.name", "Test Author")
            _git(work_dir, "config", "user.email", "author@example.com")
            (work_dir / "README.md").write_text("# UTC Check\n", encoding="utf-8")
            _git(work_dir, "add", ".")
            env = {
                **os.environ,
                "GIT_AUTHOR_DATE": "2026-05-10T16:30:00-07:00",
                "GIT_COMMITTER_DATE": "2026-05-10T16:30:00-07:00",
            }
            subprocess.run(
                ["git", "-C", str(work_dir), "commit", "-m", "feat: non-utc offset"],
                env=env,
                check=True,
                capture_output=True,
            )

            store = PackratStore(tmp_path / "repos.sqlite")
            try:
                repo = store.register_repo(
                    "octo-org", "widgets", local_path=str(work_dir)
                )
                stats = sync_git_codebase(store, repo)
                self.assertEqual(stats.commits_upserted, 1)
                commit_row = store.conn.execute(
                    "SELECT committed_at, substr(committed_at, 20) AS z_suffix FROM commits"
                ).fetchone()
                assert commit_row is not None
                self.assertEqual(commit_row["committed_at"], "2026-05-10T23:30:00Z")
                self.assertEqual(commit_row["z_suffix"], "Z")

                chunk_row = store.conn.execute(
                    "SELECT updated_at FROM chunks WHERE source_kind = 'commit'"
                ).fetchone()
                assert chunk_row is not None
                self.assertEqual(chunk_row["updated_at"], "2026-05-10T23:30:00Z")
            finally:
                store.close()

    def test_read_blobs_at_sha_batches_cat_file_and_skips_binary_missing_oversized(
        self,
    ) -> None:
        from git_indexer import _read_blobs_at_sha

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_dir = tmp_path / "work"
            subprocess.run(["git", "init", "-b", "main", str(work_dir)], check=True)
            _git(work_dir, "config", "user.name", "Test Author")
            _git(work_dir, "config", "user.email", "author@example.com")

            (work_dir / "a.py").write_text(
                "def a() -> int:\n    return 1\n", encoding="utf-8"
            )
            (work_dir / "b.md").write_text("# Doc B\n\nBody.\n", encoding="utf-8")
            (work_dir / "bin.py").write_bytes(b"def bad():\x00\x01\x02\n")
            (work_dir / "big.py").write_text("x = 1\n" * 200, encoding="utf-8")
            _git(work_dir, "add", ".")
            _git(work_dir, "commit", "-m", "feat: add mixed blobs")
            sha = _git(work_dir, "rev-parse", "HEAD")

            cat_file_calls = 0
            real_run = subprocess.run

            def spy_subproc_run(*args, **kwargs):  # type: ignore[no-untyped-def]
                nonlocal cat_file_calls
                cmd = args[0] if args else kwargs.get("args", [])
                if isinstance(cmd, list) and "cat-file" in cmd and "--batch" in cmd:
                    cat_file_calls += 1
                return real_run(*args, **kwargs)

            with mock.patch("git_indexer.subprocess.run", side_effect=spy_subproc_run):
                blobs = _read_blobs_at_sha(
                    work_dir,
                    sha,
                    ("a.py", "b.md", "bin.py", "big.py", "nonexistent.py"),
                    max_blob_bytes=500,
                )

            self.assertEqual(cat_file_calls, 1)
            self.assertEqual(sorted(blobs.keys()), ["a.py", "b.md"])
            self.assertIn("def a()", blobs["a.py"])
            self.assertIn("# Doc B", blobs["b.md"])

    def test_commits_backfill_cursor_drains_across_passes_when_head_unchanged(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_dir = tmp_path / "work"
            subprocess.run(["git", "init", "-b", "main", str(work_dir)], check=True)
            _git(work_dir, "config", "user.name", "Test Author")
            _git(work_dir, "config", "user.email", "author@example.com")

            shas: list[str] = []
            for idx in range(1, 6):
                (work_dir / "app.py").write_text(
                    f"def fn_{idx}() -> int:\n    return {idx}\n", encoding="utf-8"
                )
                _git(work_dir, "add", ".")
                _git(work_dir, "commit", "-m", f"feat: commit {idx}")
                shas.append(_git(work_dir, "rev-parse", "HEAD"))

            store = PackratStore(tmp_path / "repos.sqlite")
            try:
                repo = store.register_repo(
                    "octo-org", "widgets", local_path=str(work_dir)
                )
                # Pass 1: initial sync with max_commits=2 indexes c5 and c4
                stats_1 = sync_git_codebase(store, repo, max_commits=2)
                self.assertEqual(stats_1.commits_upserted, 2)
                self.assertIn("commits", stats_1.backfill_incomplete)
                repo_1 = store.get_repo("octo-org/widgets")
                assert repo_1 is not None
                self.assertEqual(repo_1.last_indexed_sha, shas[4])
                self.assertEqual(repo_1.commits_backfill_cursor, shas[2])
                self.assertIsNone(repo_1.commits_backfill_done_at)

                # Pass 2: HEAD is unchanged (shas[4]), drains c3 and c2 without re-indexing files
                stats_2 = sync_git_codebase(store, repo_1, max_commits=2)
                self.assertEqual(stats_2.commits_upserted, 2)
                self.assertEqual(stats_2.symbols_upserted, 0)
                self.assertIn("commits", stats_2.backfill_incomplete)
                repo_2 = store.get_repo("octo-org/widgets")
                assert repo_2 is not None
                self.assertEqual(repo_2.commits_backfill_cursor, shas[0])
                self.assertIsNone(repo_2.commits_backfill_done_at)

                # Pass 3: HEAD is unchanged, drains final commit c1 and marks backfill done
                stats_3 = sync_git_codebase(store, repo_2, max_commits=2)
                self.assertEqual(stats_3.commits_upserted, 1)
                self.assertEqual(stats_3.backfill_incomplete, [])
                repo_3 = store.get_repo("octo-org/widgets")
                assert repo_3 is not None
                self.assertIsNone(repo_3.commits_backfill_cursor)
                self.assertIsNotNone(repo_3.commits_backfill_done_at)

                stored_shas = [
                    r["sha"]
                    for r in store.conn.execute("SELECT sha FROM commits").fetchall()
                ]
                self.assertEqual(set(stored_shas), set(shas))
            finally:
                store.close()

    def test_commits_backfill_cursor_drains_burst_above_prior_old_sha_floor(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_dir = tmp_path / "work"
            subprocess.run(["git", "init", "-b", "main", str(work_dir)], check=True)
            _git(work_dir, "config", "user.name", "Test Author")
            _git(work_dir, "config", "user.email", "author@example.com")

            (work_dir / "app.py").write_text(
                "def base() -> int:\n    return 0\n", encoding="utf-8"
            )
            _git(work_dir, "add", ".")
            _git(work_dir, "commit", "-m", "feat: base commit")
            base_sha = _git(work_dir, "rev-parse", "HEAD")

            store = PackratStore(tmp_path / "repos.sqlite")
            try:
                repo = store.register_repo(
                    "octo-org", "widgets", local_path=str(work_dir)
                )
                sync_git_codebase(store, repo, max_commits=2)

                burst_shas: list[str] = []
                for idx in range(1, 5):
                    (work_dir / "app.py").write_text(
                        f"def burst_{idx}() -> int:\n    return {idx}\n",
                        encoding="utf-8",
                    )
                    _git(work_dir, "add", ".")
                    _git(work_dir, "commit", "-m", f"feat: burst {idx}")
                    burst_shas.append(_git(work_dir, "rev-parse", "HEAD"))

                repo_before = store.get_repo("octo-org/widgets")
                assert repo_before is not None
                stats_1 = sync_git_codebase(store, repo_before, max_commits=2)
                self.assertEqual(stats_1.commits_upserted, 2)
                self.assertIn("commits", stats_1.backfill_incomplete)
                repo_mid = store.get_repo("octo-org/widgets")
                assert repo_mid is not None
                self.assertEqual(
                    repo_mid.commits_backfill_cursor,
                    f"{base_sha}..{burst_shas[1]}",
                )

                stats_2 = sync_git_codebase(store, repo_mid, max_commits=2)
                self.assertEqual(stats_2.commits_upserted, 2)
                self.assertEqual(stats_2.backfill_incomplete, [])
                repo_done = store.get_repo("octo-org/widgets")
                assert repo_done is not None
                self.assertIsNone(repo_done.commits_backfill_cursor)
                self.assertIsNotNone(repo_done.commits_backfill_done_at)

                stored_shas = {
                    r["sha"]
                    for r in store.conn.execute("SELECT sha FROM commits").fetchall()
                }
                self.assertEqual(stored_shas, {base_sha, *burst_shas})
            finally:
                store.close()

    def test_incremental_sync_checks_commit_exists_once_and_batches_mentions(
        self,
    ) -> None:
        import git_indexer

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_dir = tmp_path / "work"
            subprocess.run(["git", "init", "-b", "main", str(work_dir)], check=True)
            _git(work_dir, "config", "user.name", "Test Author")
            _git(work_dir, "config", "user.email", "author@example.com")

            (work_dir / "mod.py").write_text(
                "def v0() -> int:\n    return 0\n", encoding="utf-8"
            )
            _git(work_dir, "add", ".")
            _git(work_dir, "commit", "-m", "feat: v0")

            store = PackratStore(tmp_path / "repos.sqlite")
            try:
                repo = store.register_repo(
                    "octo-org", "widgets", local_path=str(work_dir)
                )
                sync_git_codebase(store, repo)

                for idx in (1, 2, 3):
                    (work_dir / "mod.py").write_text(
                        f"def v{idx}() -> int:\n    return {idx}\n", encoding="utf-8"
                    )
                    _git(work_dir, "add", ".")
                    _git(
                        work_dir,
                        "commit",
                        "-m",
                        f"fix: update {idx} (#{100 + idx})",
                        "-m",
                        f"Refs #{idx} and #{100 + idx}",
                    )

                repo_inc = store.get_repo("octo-org/widgets")
                assert repo_inc is not None
                with (
                    mock.patch(
                        "git_indexer._commit_exists",
                        wraps=git_indexer._commit_exists,
                    ) as spy_exists,
                    mock.patch.object(
                        store,
                        "enqueue_timeline_dirty",
                        wraps=store.enqueue_timeline_dirty,
                    ) as spy_enqueue,
                ):
                    stats = sync_git_codebase(store, repo_inc)

                self.assertEqual(stats.commits_upserted, 3)
                self.assertEqual(spy_exists.call_count, 1)
                self.assertEqual(spy_enqueue.call_count, 1)
                queued_numbers = [
                    r["item_number"]
                    for r in store.conn.execute(
                        "SELECT item_number FROM timeline_dirty ORDER BY item_number"
                    ).fetchall()
                ]
                self.assertEqual(queued_numbers, [1, 2, 3])
            finally:
                store.close()

    def test_missing_ancestor_rebase_skips_unchanged_blob_oids_and_deletes_removed(
        self,
    ) -> None:
        import git_indexer

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_dir = tmp_path / "work"
            subprocess.run(["git", "init", "-b", "main", str(work_dir)], check=True)
            _git(work_dir, "config", "user.name", "Test Author")
            _git(work_dir, "config", "user.email", "author@example.com")

            (work_dir / "unchanged.py").write_text(
                "def stay_same() -> int:\n    return 1\n", encoding="utf-8"
            )
            (work_dir / "changed.py").write_text(
                "def before_change() -> int:\n    return 2\n", encoding="utf-8"
            )
            (work_dir / "removed.py").write_text(
                "def will_vanish() -> int:\n    return 3\n", encoding="utf-8"
            )
            _git(work_dir, "add", ".")
            _git(work_dir, "commit", "-m", "feat: initial tree")

            store = PackratStore(tmp_path / "repos.sqlite")
            try:
                repo = store.register_repo(
                    "octo-org", "widgets", local_path=str(work_dir)
                )
                sync_git_codebase(store, repo)
                indexed_files = store.get_indexed_files(repo.repo_id)
                self.assertEqual(
                    set(indexed_files.keys()),
                    {"unchanged.py", "changed.py", "removed.py"},
                )

                # Simulate a force-push / missing ancestor where unchanged.py has the same blob_oid,
                # changed.py is updated, and removed.py is deleted.
                (work_dir / "changed.py").write_text(
                    "def after_change() -> int:\n    return 22\n", encoding="utf-8"
                )
                (work_dir / "removed.py").unlink()
                _git(work_dir, "add", "-A")
                _git(work_dir, "commit", "-m", "feat: rebased tree")

                store.update_repo_sync_state(
                    repo.repo_id, last_indexed_sha="deadbeef" * 5
                )
                repo_rebased = store.get_repo("octo-org/widgets")
                assert repo_rebased is not None

                with mock.patch(
                    "git_indexer._read_blobs_at_sha",
                    wraps=git_indexer._read_blobs_at_sha,
                ) as spy_read_blobs:
                    stats = sync_git_codebase(store, repo_rebased, force=False)

                self.assertEqual(spy_read_blobs.call_count, 1)
                read_paths = tuple(spy_read_blobs.call_args.args[2])
                self.assertEqual(read_paths, ("changed.py",))
                self.assertEqual(stats.symbols_upserted, 1)

                symbols = {
                    (r["file_path"], r["symbol_name"])
                    for r in store.conn.execute(
                        "SELECT file_path, symbol_name FROM code_symbols WHERE repo_id = ?",
                        (repo.repo_id,),
                    ).fetchall()
                }
                self.assertEqual(
                    symbols,
                    {
                        ("unchanged.py", "stay_same"),
                        ("changed.py", "after_change"),
                    },
                )
            finally:
                store.close()

    def test_v14_reindex_rules_ratchet_batches_caps_and_chunks(self) -> None:
        import stat

        from parsers.stamps import PlatformStamp, Stamp, binary_name, host_platform, write_stamp
        from symbol_parsers import SymbolParsers

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_dir = tmp_path / "work"
            subprocess.run(["git", "init", "-b", "main", str(work_dir)], check=True)
            _git(work_dir, "config", "user.name", "Test Author")
            _git(work_dir, "config", "user.email", "author@example.com")

            (work_dir / "a.py").write_text(
                'def alpha() -> int:\n    """Alpha docstring."""\n    return 1\n',
                encoding="utf-8",
            )
            (work_dir / "b.py").write_text(
                "def beta() -> int:\n    return 2\n",
                encoding="utf-8",
            )
            (work_dir / "w.dart").write_text(
                "class WidgetBox {}\n",
                encoding="utf-8",
            )
            (work_dir / "README.md").write_text(
                "# Title\n\nSome markdown docs.\n",
                encoding="utf-8",
            )
            _git(work_dir, "add", ".")
            _git(work_dir, "commit", "-m", "feat: initial")
            sha_1 = _git(work_dir, "rev-parse", "HEAD")

            scripts_dir = tmp_path / "scripts"
            py_dir = scripts_dir / "parsers" / "python"
            py_dir.mkdir(parents=True)
            (py_dir / "symbol_parser.py").write_text(
                (Path(__file__).resolve().parent / "parsers" / "python" / "symbol_parser.py").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            write_stamp(
                py_dir,
                Stamp(parser="py-ast", protocol=1, parser_ids={".py": "py-ast@1"}, platforms={}),
            )

            dart_dir = scripts_dir / "parsers" / "dart"
            dart_dir.mkdir(parents=True)
            host = host_platform()
            dart_bin = dart_dir / binary_name("dart-analyzer", host)
            dart_bin.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                'print(json.dumps({"ready": True, "parser": "dart-analyzer", "protocol": 1, "parser_ids": {".dart": "dart-analyzer@1"}}), flush=True)\n'
                "for line in sys.stdin:\n"
                "    req = json.loads(line)\n"
                '    print(json.dumps({"id": req["id"], "symbols": [{"kind": "class", "name": "WidgetBox", "qualified_name": "WidgetBox", "parent": "", "signature": "class WidgetBox", "docstring": "Dart widget docstring", "line_start": 1, "line_end": 1, "visibility": "public"}], "syntax_error_count": 0}), flush=True)\n',
                encoding="utf-8",
            )
            dart_bin.chmod(dart_bin.stat().st_mode | stat.S_IXUSR)
            write_stamp(
                dart_dir,
                Stamp(
                    parser="dart-analyzer",
                    protocol=1,
                    parser_ids={".dart": "dart-analyzer@1"},
                    platforms={host: PlatformStamp(source_sha256="x", built_at="now", built_on=host)},
                ),
            )

            store = PackratStore(tmp_path / "repos.sqlite")
            try:
                repo = store.register_repo("octo-org", "widgets", local_path=str(work_dir))

                # 1. Capped sync (`max_reparse_files=2`): commits batch cleanly and leaves parser_fingerprint NULL.
                with SymbolParsers(scripts_dir=scripts_dir, overrides={}) as p_cap:
                    capped_stats = sync_git_codebase(
                        store, repo, parsers=p_cap, max_reparse_files=2
                    )
                self.assertTrue(capped_stats.reparse_capped)
                repo_after_cap = store.get_repo("octo-org/widgets")
                assert repo_after_cap is not None
                self.assertIsNone(repo_after_cap.parser_fingerprint)
                self.assertEqual(len(store.get_indexed_files(repo.repo_id)), 2)

                # 2. Uncapped sync finishes remaining files via ls-tree even though HEAD SHA matches, writes fingerprint.
                with SymbolParsers(scripts_dir=scripts_dir, overrides={}) as p_full:
                    full_stats = sync_git_codebase(store, repo_after_cap, parsers=p_full)
                    expected_fp = p_full.fingerprint()
                self.assertFalse(full_stats.reparse_capped)
                repo_done = store.get_repo("octo-org/widgets")
                assert repo_done is not None
                self.assertEqual(repo_done.last_indexed_sha, sha_1)
                self.assertEqual(repo_done.parser_fingerprint, expected_fp)

                idx_map = store.get_indexed_files(repo.repo_id)
                self.assertEqual(idx_map["README.md"][1], "md-sections@1")
                self.assertEqual(idx_map["a.py"][1], "py-ast@1")
                self.assertEqual(idx_map["w.dart"][1], "dart-analyzer@1")

                # Check chunk composition and `is_untrusted`: True for docstring & doc_section, False for empty docstring
                chunk_rows = {
                    str(r["chunk_id"]): (int(r["is_untrusted"]), str(r["text"]), str(r["title"]))
                    for r in store.conn.execute(
                        "SELECT chunk_id, is_untrusted, text, title FROM chunks WHERE repo_id = ?",
                        (repo.repo_id,),
                    ).fetchall()
                }
                alpha_row = chunk_rows["octo-org/widgets@symbol:a.py:function:alpha:1"]
                beta_row = chunk_rows["octo-org/widgets@symbol:b.py:function:beta:1"]
                doc_row = chunk_rows["octo-org/widgets@doc:README.md:1"]
                self.assertEqual(alpha_row[0], 1)
                self.assertIn("Alpha docstring.", alpha_row[1])
                self.assertEqual(alpha_row[2], "function alpha in a.py")
                self.assertEqual(beta_row[0], 0)
                self.assertEqual(doc_row[0], 1)

                # 3. Ratchet: disable dart-analyzer WITHOUT --reindex -> fingerprint mismatches, ls-tree runs, w.dart is NOT downgraded!
                with SymbolParsers(
                    scripts_dir=scripts_dir, overrides={"dart-analyzer": "disabled"}
                ) as p_dis:
                    dis_stats = sync_git_codebase(store, repo_done, parsers=p_dis)
                self.assertEqual(dis_stats.symbols_upserted, 0)
                self.assertEqual(
                    store.get_indexed_files(repo.repo_id)["w.dart"][1], "dart-analyzer@1"
                )

                # 4. disable --reindex (`downgrade_parser_index`) clears w.dart indexed_files row -> next sync downgrades to regex@1.
                store.downgrade_parser_index(["dart-analyzer@1"], [".dart"])
                repo_downgrade = store.get_repo("octo-org/widgets")
                assert repo_downgrade is not None
                self.assertIsNone(repo_downgrade.parser_fingerprint)
                with SymbolParsers(
                    scripts_dir=scripts_dir, overrides={"dart-analyzer": "disabled"}
                ) as p_dis2:
                    down_stats = sync_git_codebase(store, repo_downgrade, parsers=p_dis2)
                self.assertGreaterEqual(down_stats.symbols_upserted, 1)
                self.assertEqual(
                    store.get_indexed_files(repo.repo_id)["w.dart"][1], "regex@1"
                )
            finally:
                store.close()

    def test_managed_bare_clone_end_to_end_and_skip_fetch(self) -> None:
        from dataclasses import replace
        from managed_git import ensure_git, fetch_git

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_dir = tmp_path / "source"
            subprocess.run(["git", "init", "-b", "main", str(source_dir)], check=True)
            _git(source_dir, "config", "user.name", "Test Author")
            _git(source_dir, "config", "user.email", "author@example.com")
            (source_dir / "lib.py").write_text(
                "def greet(name: str) -> str:\n    return f'hi {name}'\n",
                encoding="utf-8",
            )
            (source_dir / "README.md").write_text(
                "# Managed Repo\n\nBare clone docs.\n",
                encoding="utf-8",
            )
            _git(source_dir, "add", ".")
            _git(source_dir, "commit", "-m", "feat: initial managed commit (#7)")
            sha_1 = _git(source_dir, "rev-parse", "HEAD")

            store = PackratStore(tmp_path / "state" / "repos.sqlite")
            try:
                repo = store.register_repo(
                    "octo-org",
                    "managed-widgets",
                    git_source="managed",
                )
                local_repo = replace(repo, git_url=source_dir.as_uri())
                managed_dir = store.resolve_git_dir(repo)
                assert managed_dir is not None
                self.assertFalse(managed_dir.exists())

                pending_stats = sync_git_codebase(store, repo, fetch=True)
                self.assertEqual(pending_stats.skipped_reason, "pending_clone")
                self.assertIsNone(pending_stats.indexed_sha)

                assert store.git_root is not None
                clone_res = ensure_git(
                    store.git_root,
                    local_repo,
                    extra_git_config=("-c", "protocol.file.allow=always"),
                )
                self.assertEqual(clone_res.error, "")
                self.assertTrue(managed_dir.is_dir())
                self.assertFalse((managed_dir / ".git").exists())

                with mock.patch("git_indexer._fetch_remote_ref") as mock_fetch:
                    resolved = resolve_canonical_ref(managed_dir, repo, fetch=True)
                    stats_1 = sync_git_codebase(store, repo, fetch=True)

                mock_fetch.assert_not_called()
                self.assertEqual(resolved, ("refs/heads/main", sha_1))
                self.assertIsNone(stats_1.skipped_reason)
                self.assertEqual(stats_1.indexed_ref, "refs/heads/main")
                self.assertEqual(stats_1.indexed_sha, sha_1)
                self.assertEqual(stats_1.commits_upserted, 1)
                self.assertGreaterEqual(stats_1.symbols_upserted, 1)
                self.assertEqual(stats_1.doc_sections_upserted, 1)

                (source_dir / "lib.py").write_text(
                    "def greet(name: str) -> str:\n    return f'hi {name}'\n\n"
                    "def farewell() -> str:\n    return 'bye'\n",
                    encoding="utf-8",
                )
                _git(source_dir, "add", "lib.py")
                _git(source_dir, "commit", "-m", "feat: add farewell (#8)")
                sha_2 = _git(source_dir, "rev-parse", "HEAD")

                fetch_res = fetch_git(
                    store.git_root,
                    local_repo,
                    maintenance="skip",
                    extra_git_config=("-c", "protocol.file.allow=always"),
                )
                self.assertEqual(fetch_res.error, "")
                repo_after_1 = store.get_repo("octo-org/managed-widgets")
                assert repo_after_1 is not None

                with mock.patch("git_indexer._fetch_remote_ref") as mock_fetch_2:
                    stats_2 = sync_git_codebase(store, repo_after_1, fetch=True)

                mock_fetch_2.assert_not_called()
                self.assertEqual(stats_2.indexed_ref, "refs/heads/main")
                self.assertEqual(stats_2.indexed_sha, sha_2)
                self.assertEqual(stats_2.commits_upserted, 1)
                sym_names = {
                    r["symbol_name"]
                    for r in store.conn.execute(
                        "SELECT symbol_name FROM code_symbols WHERE repo_id = ?",
                        (repo.repo_id,),
                    ).fetchall()
                }
                self.assertEqual(sym_names, {"greet", "farewell"})
            finally:
                store.close()

    def test_sync_skipped_reasons_no_local_path_and_missing_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            not_a_repo = tmp_path / "not-a-repo"
            not_a_repo.mkdir()
            absent_path = tmp_path / "absent-repo"

            store = PackratStore(tmp_path / "state" / "repos.sqlite")
            try:
                repo_none = store.register_repo("octo-org", "no-git")
                self.assertEqual(repo_none.git_source, "none")
                stats_none = sync_git_codebase(store, repo_none)
                self.assertEqual(stats_none.skipped_reason, "no_local_path")

                repo_non_git = store.register_repo(
                    "octo-org", "plain-dir", local_path=str(not_a_repo)
                )
                self.assertIsNone(resolve_canonical_ref(not_a_repo, repo_non_git))
                stats_non_git = sync_git_codebase(store, repo_non_git)
                self.assertEqual(
                    stats_non_git.skipped_reason, f"missing_git_repo:{not_a_repo}"
                )

                repo_absent = store.register_repo(
                    "octo-org", "absent-dir", local_path=str(absent_path)
                )
                stats_absent = sync_git_codebase(store, repo_absent)
                self.assertEqual(
                    stats_absent.skipped_reason, f"missing_git_repo:{absent_path}"
                )

                repo_corrupt_mgr = store.register_repo(
                    "octo-org",
                    "corrupt-managed",
                    git_source="managed",
                    git_url="https://github.com/octo-org/corrupt-managed.git",
                )
                managed_dir = store.resolve_git_dir(repo_corrupt_mgr)
                assert managed_dir is not None
                managed_dir.mkdir(parents=True)
                stats_corrupt = sync_git_codebase(store, repo_corrupt_mgr)
                self.assertEqual(
                    stats_corrupt.skipped_reason, f"missing_git_repo:{managed_dir}"
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
