#!/usr/bin/env python3
"""Unit tests for packrat_probe.py."""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import packrat_probe
from packrat_probe import (
    discover_packrat_cmd,
    main,
    probe_packrat,
    resolve_github_repo,
)

SHA_INDEXED = "1111111111111111111111111111111111111111"
SHA_DRIFTED = "2222222222222222222222222222222222222222"

FULL_SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
CREATE TABLE repos (
  repo_id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner TEXT NOT NULL,
  name TEXT NOT NULL,
  full_name TEXT NOT NULL UNIQUE,
  local_path TEXT,
  default_branch TEXT NOT NULL DEFAULT 'main',
  last_indexed_sha TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  issues_backfill_cursor TEXT,
  issues_backfill_done_at TEXT,
  prs_backfill_cursor TEXT,
  prs_backfill_done_at TEXT,
  discussions_backfill_cursor TEXT,
  discussions_backfill_done_at TEXT
);
CREATE TABLE chunks (
  chunk_id TEXT PRIMARY KEY,
  repo_id INTEGER NOT NULL
);
CREATE TABLE commits (
  repo_id INTEGER NOT NULL,
  sha TEXT NOT NULL
);
CREATE TABLE pull_requests (
  repo_id INTEGER NOT NULL,
  number INTEGER NOT NULL
);
CREATE TABLE issues (
  repo_id INTEGER NOT NULL,
  number INTEGER NOT NULL
);
CREATE TABLE review_threads (
  thread_id TEXT PRIMARY KEY,
  repo_id INTEGER NOT NULL
);
"""


def _init_git_config_repo(
    repo_dir: Path,
    remotes: dict[str, str],
    *,
    loose_refs: dict[str, str] | None = None,
    packed_refs_text: str | None = None,
) -> Path:
    """Creates a synthetic `.git` directory with `.git/config` and optional refs."""
    git_dir = repo_dir / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    lines = ["[core]", "\trepositoryformatversion = 0"]
    for name, url in remotes.items():
        lines.append(f'[remote "{name}"]')
        lines.append(f"\turl = {url}")
    (git_dir / "config").write_text("\n".join(lines) + "\n", encoding="utf-8")

    for ref_path, sha in (loose_refs or {}).items():
        target = git_dir / ref_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{sha}\n", encoding="utf-8")

    if packed_refs_text is not None:
        (git_dir / "packed-refs").write_text(packed_refs_text, encoding="utf-8")

    return git_dir


def _create_full_db(
    db_path: Path,
    *,
    full_name: str = "octo-org/widgets",
    default_branch: str = "main",
    last_indexed_sha: str | None = SHA_INDEXED,
    chunk_count: int = 3,
    backfill_done: bool = True,
    prs_cursor: str | None = None,
) -> None:
    """Creates a populated SQLite WAL fixture mirroring packrat's schema."""
    owner, name = full_name.split("/", 1)
    done_ts = "2026-11-01T00:00:00Z" if backfill_done else None
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(FULL_SCHEMA_SQL)
        conn.execute(
            """
            INSERT INTO repos (
              owner, name, full_name, default_branch, last_indexed_sha,
              created_at, updated_at,
              issues_backfill_done_at, prs_backfill_cursor, prs_backfill_done_at,
              discussions_backfill_done_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                owner,
                name,
                full_name,
                default_branch,
                last_indexed_sha,
                "2026-11-01T00:00:00Z",
                "2026-11-01T00:00:00Z",
                done_ts,
                prs_cursor,
                None if prs_cursor else done_ts,
                done_ts,
            ),
        )
        for idx in range(chunk_count):
            conn.execute(
                "INSERT INTO chunks (chunk_id, repo_id) VALUES (?, 1)",
                (f"c-{idx}",),
            )
        conn.execute("INSERT INTO commits (repo_id, sha) VALUES (1, ?)", (SHA_INDEXED,))
        conn.execute("INSERT INTO pull_requests (repo_id, number) VALUES (1, 10)")
        conn.execute("INSERT INTO pull_requests (repo_id, number) VALUES (1, 11)")
        conn.execute("INSERT INTO issues (repo_id, number) VALUES (1, 5)")
        conn.execute(
            "INSERT INTO review_threads (thread_id, repo_id) VALUES ('t-1', 1)"
        )
        conn.commit()
    finally:
        conn.close()


class TestPackratProbe(unittest.TestCase):
    """Tests remote resolution, read-only SQLite probing, and CLI formatting."""

    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp()).resolve()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_ready_state_and_counts(self) -> None:
        repo_dir = self.temp_dir / "widgets"
        _init_git_config_repo(
            repo_dir,
            {"upstream": "https://github.com/octo-org/widgets.git"},
            loose_refs={"refs/remotes/upstream/main": SHA_INDEXED},
        )
        db_path = self.temp_dir / "repos.sqlite"
        _create_full_db(db_path, full_name="octo-org/widgets", chunk_count=4)

        res = probe_packrat(repo_dir, db_path=db_path, packrat_cmd=["packrat"])
        self.assertEqual(res.status, "ready")
        self.assertTrue(res.available)
        self.assertTrue(res.synced)
        self.assertEqual(res.repo, "octo-org/widgets")
        self.assertEqual(res.checkout_root, str(repo_dir))
        self.assertEqual(res.default_branch, "main")
        self.assertEqual(res.last_indexed_sha, SHA_INDEXED)
        self.assertEqual(res.local_canonical_sha, SHA_INDEXED)
        self.assertEqual(
            res.counts,
            {
                "chunks": 4,
                "commits": 1,
                "pull_requests": 2,
                "issues": 1,
                "review_threads": 1,
            },
        )
        self.assertEqual(res.warnings, [])
        self.assertEqual(res.user_note, "")

    def test_case_insensitive_repo_lookup_and_ready_without_cli(self) -> None:
        repo_dir = self.temp_dir / "widgets"
        _init_git_config_repo(
            repo_dir,
            {"origin": "https://github.com/Octo-Org/Widgets.git"},
        )
        db_path = self.temp_dir / "repos.sqlite"
        _create_full_db(db_path, full_name="octo-org/widgets", chunk_count=2)

        res = probe_packrat(repo_dir, db_path=db_path, packrat_cmd=[])
        self.assertEqual(res.status, "ready")
        self.assertTrue(res.available)
        self.assertTrue(res.synced)
        self.assertIsNone(res.packrat_cmd)
        self.assertEqual(res.repo, "octo-org/widgets")
        self.assertEqual(res.user_note, "")

    def test_missing_db_creates_zero_files_not_synced_and_unavailable(self) -> None:
        repo_dir = self.temp_dir / "widgets"
        _init_git_config_repo(
            repo_dir,
            {"origin": "https://github.com/octo-org/widgets.git"},
        )
        empty_db_dir = self.temp_dir / "no_such_packrat_dir"
        empty_db_dir.mkdir()
        missing_db = empty_db_dir / "repos.sqlite"

        not_synced = probe_packrat(
            repo_dir,
            db_path=missing_db,
            packrat_cmd=["packrat"],
        )
        self.assertFalse(missing_db.exists())
        self.assertEqual(list(empty_db_dir.iterdir()), [])
        self.assertEqual(not_synced.status, "not_synced")
        self.assertTrue(not_synced.available)
        self.assertFalse(not_synced.synced)
        self.assertEqual(
            not_synced.user_note,
            "Note: Codebase discovery could have been faster with `packrat` "
            f"(`packrat repo add octo-org/widgets --path {repo_dir} && packrat sync`).",
        )

        unavailable = probe_packrat(
            repo_dir,
            db_path=missing_db,
            packrat_cmd=[],
        )
        self.assertFalse(missing_db.exists())
        self.assertEqual(list(empty_db_dir.iterdir()), [])
        self.assertEqual(unavailable.status, "unavailable")
        self.assertFalse(unavailable.available)
        self.assertFalse(unavailable.synced)
        self.assertEqual(
            unavailable.user_note,
            "Note: Codebase discovery could have been faster with `packrat` "
            "installed and synced for `octo-org/widgets`.",
        )

    def test_unregistered_repo_and_zero_chunks_notes(self) -> None:
        repo_dir = self.temp_dir / "widgets"
        _init_git_config_repo(
            repo_dir,
            {"origin": "https://github.com/octo-org/widgets.git"},
        )
        db_other = self.temp_dir / "other.sqlite"
        _create_full_db(db_other, full_name="octo-org/packrat", chunk_count=5)

        unreg = probe_packrat(repo_dir, db_path=db_other, packrat_cmd=["packrat"])
        self.assertEqual(unreg.status, "not_synced")
        self.assertEqual(
            unreg.user_note,
            "Note: Codebase discovery could have been faster with `packrat` "
            f"(`packrat repo add octo-org/widgets --path {repo_dir} && packrat sync`).",
        )

        db_zero = self.temp_dir / "zero.sqlite"
        _create_full_db(db_zero, full_name="octo-org/widgets", chunk_count=0)

        zero_synced = probe_packrat(repo_dir, db_path=db_zero, packrat_cmd=["packrat"])
        self.assertEqual(zero_synced.status, "not_synced")
        self.assertFalse(zero_synced.synced)
        self.assertEqual(zero_synced.counts["chunks"], 0)
        self.assertEqual(
            zero_synced.user_note,
            "Note: Codebase discovery could have been faster with `packrat` "
            "(`packrat sync --repo octo-org/widgets`).",
        )

        zero_unavail = probe_packrat(repo_dir, db_path=db_zero, packrat_cmd=[])
        self.assertEqual(zero_unavail.status, "unavailable")
        self.assertEqual(
            zero_unavail.user_note,
            "Note: Codebase discovery could have been faster with `packrat` "
            "installed and synced for `octo-org/widgets`.",
        )

    def test_not_github_plain_dir_and_non_github_remotes(self) -> None:
        plain_dir = self.temp_dir / "plain"
        plain_dir.mkdir()
        res_plain = probe_packrat(
            plain_dir,
            db_path=self.temp_dir / "missing.sqlite",
            packrat_cmd=["packrat"],
        )
        self.assertEqual(res_plain.status, "not_github")
        self.assertIsNone(res_plain.repo)
        self.assertIsNone(res_plain.checkout_root)
        self.assertEqual(res_plain.user_note, "")

        gitlab_dir = self.temp_dir / "gitlab_only"
        _init_git_config_repo(
            gitlab_dir,
            {
                "upstream": "https://gitlab.example.com/octo-org/widgets.git",
                "origin": "https://github.example.com/octo-org/widgets.git",
            },
        )
        res_gitlab = probe_packrat(
            gitlab_dir,
            db_path=self.temp_dir / "missing.sqlite",
            packrat_cmd=["packrat"],
        )
        self.assertEqual(res_gitlab.status, "not_github")
        self.assertIsNone(res_gitlab.repo)
        self.assertEqual(res_gitlab.checkout_root, str(gitlab_dir))
        self.assertEqual(res_gitlab.user_note, "")

    def test_multi_remote_ordering_ssh_aliases_and_removesuffix_git(self) -> None:
        # Non-GitHub upstream + GitHub SSH alias origin ending in 't' ('packrat')
        mirror_dir = self.temp_dir / "packrat_mirror"
        _init_git_config_repo(
            mirror_dir,
            {
                "upstream": "https://gitlab.example.com/mirror/packrat.git",
                "origin": "git@github.com-work:octo-org/packrat.git",
            },
        )
        slug, root = resolve_github_repo(mirror_dir)
        self.assertEqual(slug, "octo-org/packrat")
        self.assertEqual(root, mirror_dir)

        # Repo URL without .git suffix preserves trailing 't' in 'packrat'
        bare_url_dir = self.temp_dir / "packrat_no_suffix"
        _init_git_config_repo(
            bare_url_dir,
            {"origin": "ssh://git@github.com-corp:2222/octo-org/packrat"},
        )
        slug_no_ext, _ = resolve_github_repo(bare_url_dir)
        self.assertEqual(slug_no_ext, "octo-org/packrat")

        # Canonical GitHub upstream wins over fork GitHub origin
        fork_dir = self.temp_dir / "fork_checkout"
        _init_git_config_repo(
            fork_dir,
            {
                "origin": "https://github.com/fork-user/widgets.git",
                "upstream": "https://github.com/octo-org/widgets.git",
            },
        )
        slug_fork, _ = resolve_github_repo(fork_dir)
        self.assertEqual(slug_fork, "octo-org/widgets")

        # Remaining remotes checked in sorted order when upstream/origin absent
        other_dir = self.temp_dir / "sorted_remotes"
        _init_git_config_repo(
            other_dir,
            {
                "zeta": "https://github.com/octo-org/packrat.git",
                "alpha": "https://github.com/octo-org/widgets.git",
            },
        )
        slug_sorted, _ = resolve_github_repo(other_dir)
        self.assertEqual(slug_sorted, "octo-org/widgets")

    def test_git_worktree_commondir_and_subdirectory_resolution(self) -> None:
        main_repo = self.temp_dir / "main_repo"
        main_git = _init_git_config_repo(
            main_repo,
            {"upstream": "https://github.com/octo-org/packrat.git"},
            loose_refs={"refs/remotes/upstream/main": SHA_INDEXED},
        )
        wt_git_dir = main_git / "worktrees" / "feature_wt"
        wt_git_dir.mkdir(parents=True)
        (wt_git_dir / "HEAD").write_text("ref: refs/heads/feat\n", encoding="utf-8")
        (wt_git_dir / "commondir").write_text("../..\n", encoding="utf-8")

        worktree_dir = self.temp_dir / "worktrees" / "feature_wt"
        nested_dir = worktree_dir / "src" / "module"
        nested_dir.mkdir(parents=True)
        (worktree_dir / ".git").write_text(
            f"gitdir: {wt_git_dir}\n",
            encoding="utf-8",
        )

        db_path = self.temp_dir / "repos.sqlite"
        _create_full_db(db_path, full_name="octo-org/packrat", chunk_count=3)

        res = probe_packrat(nested_dir, db_path=db_path, packrat_cmd=["packrat"])
        self.assertEqual(res.status, "ready")
        self.assertEqual(res.repo, "octo-org/packrat")
        self.assertEqual(res.checkout_root, str(worktree_dir))
        self.assertEqual(res.local_canonical_sha, SHA_INDEXED)
        self.assertEqual(res.warnings, [])

    def test_git_remote_v_fallback_when_config_missing(self) -> None:
        repo_dir = self.temp_dir / "broken_config_repo"
        git_dir = repo_dir / ".git"
        git_dir.mkdir(parents=True)
        # Intentionally omit .git/config to trigger `git remote -v` fallback.
        fake_proc = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "upstream\thttps://gitlab.example.com/mirror/widgets.git (fetch)\n"
                "origin\tgit@github.com:octo-org/widgets.git (fetch)\n"
                "origin\tgit@github.com:octo-org/widgets.git (push)\n"
            ),
            stderr="",
        )
        with mock.patch(
            "packrat_probe.subprocess.run", return_value=fake_proc
        ) as m_run:
            slug, checkout = resolve_github_repo(repo_dir)

        self.assertEqual(slug, "octo-org/widgets")
        self.assertEqual(checkout, repo_dir)
        called_argv = m_run.call_args[0][0]
        self.assertIn("core.hooksPath=/dev/null", called_argv)
        self.assertIn("core.fsmonitor=false", called_argv)

    def test_pre_migration_schema_without_backfill_columns(self) -> None:
        repo_dir = self.temp_dir / "legacy_widgets"
        _init_git_config_repo(
            repo_dir,
            {"origin": "https://github.com/octo-org/widgets.git"},
            loose_refs={"refs/remotes/origin/main": SHA_INDEXED},
        )
        db_path = self.temp_dir / "legacy.sqlite"
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE repos (
                  repo_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  owner TEXT NOT NULL,
                  name TEXT NOT NULL,
                  full_name TEXT NOT NULL UNIQUE,
                  default_branch TEXT NOT NULL DEFAULT 'main',
                  last_indexed_sha TEXT
                );
                CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY, repo_id INTEGER);
                INSERT INTO repos (owner, name, full_name, default_branch, last_indexed_sha)
                VALUES ('octo-org', 'widgets', 'octo-org/widgets', 'main',
                        '1111111111111111111111111111111111111111');
                INSERT INTO chunks (chunk_id, repo_id) VALUES ('c-1', 1);
                """
            )
            conn.commit()
        finally:
            conn.close()

        res = probe_packrat(repo_dir, db_path=db_path, packrat_cmd=["packrat"])
        self.assertEqual(res.status, "ready")
        self.assertEqual(res.counts["chunks"], 1)
        self.assertEqual(res.counts["review_threads"], 0)
        self.assertEqual(res.warnings, [])

    def test_sha_drift_and_incomplete_backfill_warnings(self) -> None:
        repo_dir = self.temp_dir / "drifted_widgets"
        packed_refs = (
            "# pack-refs with: peeled fully-peeled sorted\n"
            f"{SHA_DRIFTED} refs/remotes/origin/main\n"
            "^3333333333333333333333333333333333333333\n"
        )
        _init_git_config_repo(
            repo_dir,
            {"origin": "https://github.com/octo-org/widgets.git"},
            packed_refs_text=packed_refs,
        )
        db_path = self.temp_dir / "drift.sqlite"
        _create_full_db(
            db_path,
            full_name="octo-org/widgets",
            last_indexed_sha=SHA_INDEXED,
            chunk_count=2,
            prs_cursor="Y3Vyc29yLTE=",
        )

        res = probe_packrat(repo_dir, db_path=db_path, packrat_cmd=["packrat"])
        self.assertEqual(res.status, "ready")
        self.assertEqual(res.local_canonical_sha, SHA_DRIFTED)
        self.assertTrue(any("prs backfill incomplete" in w for w in res.warnings))
        self.assertTrue(any("SHA drift" in w for w in res.warnings))

    def test_discover_packrat_cmd_precedence(self) -> None:
        self.assertEqual(
            discover_packrat_cmd(["/opt/packrat", "--flag"]),
            ["/opt/packrat", "--flag"],
        )
        self.assertEqual(discover_packrat_cmd([]), None)

        with mock.patch("packrat_probe.shutil.which", return_value="/usr/bin/packrat"):
            self.assertEqual(discover_packrat_cmd(None), ["/usr/bin/packrat"])

        fake_home = self.temp_dir / "home"
        local_bin = fake_home / ".local" / "bin" / "packrat"
        local_bin.parent.mkdir(parents=True)
        local_bin.write_text("#!/bin/sh\n", encoding="utf-8")
        local_bin.chmod(0o755)
        with (
            mock.patch("packrat_probe.shutil.which", return_value=None),
            mock.patch("packrat_probe.Path.home", return_value=fake_home),
        ):
            self.assertEqual(discover_packrat_cmd(None), [str(local_bin)])

        local_bin.unlink()
        expected_candidates = packrat_probe._candidate_skill_scripts(fake_home)
        self.assertEqual(
            expected_candidates,
            [
                Path(packrat_probe.__file__).resolve().parents[2]
                / "packrat"
                / "scripts"
                / "packrat.py",
                fake_home
                / ".gemini"
                / "config"
                / "skills"
                / "packrat"
                / "scripts"
                / "packrat.py",
                fake_home / ".gemini" / "skills" / "packrat" / "scripts" / "packrat.py",
                fake_home / ".claude" / "skills" / "packrat" / "scripts" / "packrat.py",
                fake_home / ".agents" / "skills" / "packrat" / "scripts" / "packrat.py",
            ],
        )

        skill_script = (
            fake_home / ".claude" / "skills" / "packrat" / "scripts" / "packrat.py"
        )
        skill_script.parent.mkdir(parents=True)
        skill_script.write_text("# stub\n", encoding="utf-8")
        with (
            mock.patch("packrat_probe.shutil.which", return_value=None),
            mock.patch("packrat_probe.Path.home", return_value=fake_home),
            mock.patch.object(
                packrat_probe,
                "_candidate_skill_scripts",
                return_value=[skill_script],
            ),
        ):
            self.assertEqual(
                discover_packrat_cmd(None),
                ["python3", str(skill_script)],
            )

    def test_cli_json_text_and_invalid_arguments(self) -> None:
        repo_dir = self.temp_dir / "cli_widgets"
        _init_git_config_repo(
            repo_dir,
            {"origin": "https://github.com/octo-org/widgets.git"},
            loose_refs={"refs/remotes/origin/main": SHA_INDEXED},
        )
        db_path = self.temp_dir / "cli.sqlite"
        _create_full_db(db_path, full_name="octo-org/widgets", chunk_count=3)

        out_json = io.StringIO()
        with redirect_stdout(out_json):
            rc = main(
                [
                    "--root",
                    str(repo_dir),
                    "--packrat-db",
                    str(db_path),
                    "--packrat-cmd",
                    "packrat",
                    "--json",
                ]
            )
        self.assertEqual(rc, 0)
        payload = json.loads(out_json.getvalue())
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["repo"], "octo-org/widgets")

        out_text = io.StringIO()
        with redirect_stdout(out_text):
            rc_text = main(
                [
                    "--root",
                    str(repo_dir),
                    "--repo",
                    "octo-org/packrat",
                    "--packrat-db",
                    str(db_path),
                    "--packrat-cmd",
                    "",
                    "--text",
                ]
            )
        self.assertEqual(rc_text, 0)
        lines = out_text.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("status=unavailable", lines[0])
        self.assertIn("octo-org/packrat", lines[0])

        err_buf = io.StringIO()
        with redirect_stderr(err_buf):
            rc_bad = main(["--root", str(repo_dir), "--repo", "invalid-slug"])
        self.assertEqual(rc_bad, 2)

    def test_warm_probe_under_50ms(self) -> None:
        repo_dir = self.temp_dir / "perf_widgets"
        _init_git_config_repo(
            repo_dir,
            {"upstream": "https://github.com/octo-org/widgets.git"},
            loose_refs={"refs/remotes/upstream/main": SHA_INDEXED},
        )
        db_path = self.temp_dir / "perf.sqlite"
        _create_full_db(db_path, full_name="octo-org/widgets", chunk_count=5)

        # Warm SQLite page cache and Python path caches once.
        probe_packrat(repo_dir, db_path=db_path, packrat_cmd=["packrat"])

        start = time.perf_counter()
        res = probe_packrat(repo_dir, db_path=db_path, packrat_cmd=["packrat"])
        elapsed_sec = time.perf_counter() - start

        self.assertEqual(res.status, "ready")
        self.assertLess(
            elapsed_sec,
            0.050,
            f"Expected warm probe < 50 ms, got {elapsed_sec * 1000:.2f} ms",
        )

    def test_symbolic_ref_cycle_returns_none_without_false_drift_warning(self) -> None:
        repo_dir = self.temp_dir / "symref_cycle"
        _init_git_config_repo(
            repo_dir,
            {"upstream": "https://github.com/octo-org/widgets.git"},
            loose_refs={
                "refs/remotes/upstream/main": "ref: refs/remotes/upstream/main"
            },
        )
        db_path = self.temp_dir / "cycle.sqlite"
        _create_full_db(db_path, full_name="octo-org/widgets", chunk_count=5)
        res = probe_packrat(repo_dir, db_path=db_path, packrat_cmd=["packrat"])
        self.assertEqual(res.status, "ready")
        self.assertEqual(res.warnings, [])


if __name__ == "__main__":
    unittest.main()
