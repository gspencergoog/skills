"""Unit tests for packrat git diff-tree commit, AST symbol, and Markdown section indexer."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from db_store import PackratStore
from git_indexer import resolve_canonical_ref, sync_git_codebase


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
                "def old_helper(value: int) -> int:\n"
                "    return value + 1\n",
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
                _git(work_dir, "commit", "-m", "refactor: remove legacy helper and add bus (#15)")
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


if __name__ == "__main__":
    unittest.main()
