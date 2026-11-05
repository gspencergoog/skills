"""Tests for packrat's versioned schema migrations and reader views."""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db_migrations
import packrat
from auto_sync import CmdResult, get_auto_sync_status
from db_migrations import (
    LATEST_SCHEMA_VERSION,
    REFETCH_OPEN_PRS_KEY_PREFIX,
    SchemaTooNewError,
    apply_migrations,
    read_schema_version,
    views_sha256,
)
from db_schema import SCHEMA_SQL
from db_store import PackratStore
from search_engine import hybrid_rrf_search

READER_VIEWS = (
    "v_api_calls",
    "v_changes",
    "v_issue_basic",
    "v_item_comments",
    "v_notifications",
    "v_pr_basic",
    "v_pr_checks",
    "v_pr_latest_reviews",
    "v_pr_live",
    "v_pr_review_requests",
    "v_pr_threads",
    "v_service_health",
    "v_viewer_items",
    "v_watches",
)

OLD_PR_INSERT = """
INSERT INTO pull_requests (
  repo_id, number, node_id, state, is_draft, author, author_association,
  title, body, labels_json, base_ref, head_ref, head_sha, merge_commit_sha,
  review_decision, mergeable, additions, deletions, changed_files,
  created_at, updated_at, merged_at, closed_at, url
) VALUES (?, ?, 'PR_x', 'OPEN', 0, 'alice', 'MEMBER', ?, 'body', '[]', 'main', ?, 'abc',
  NULL, NULL, NULL, 1, 1, 1, '2026-10-01T00:00:00Z', '2026-10-02T00:00:00Z', NULL, NULL, NULL)
"""
"""The PR insert used by packrat before schema versioning (no live columns)."""


def _build_pre_migration_db(path: Path) -> None:
    """Creates a database the way packrat did before `schema_version`, with sample rows."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript("PRAGMA journal_mode = WAL;\n" + SCHEMA_SQL)
        conn.execute(
            "INSERT INTO repos (owner, name, full_name, created_at, updated_at) "
            "VALUES ('octo-org', 'widgets', 'octo-org/widgets', 'now', 'now')"
        )
        conn.execute(OLD_PR_INSERT, (1, 7, "Add focus ring", "fix-focus"))
        conn.execute(
            "INSERT INTO issues (repo_id, number, state, title, assignees_json, updated_at) "
            "VALUES (1, 3, 'OPEN', 'Broken focus', '[\"bob\"]', '2026-10-02T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO chunks (chunk_id, repo_id, source_kind, source_ref, title, "
            "content_sha256, text, updated_at) VALUES ('c1', 1, 'pr', 'octo-org/widgets#7', "
            "'PR #7: Add focus ring', 'sha', 'keyboard focus ring', '2026-10-02T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO chunks_fts (chunk_id, source_kind, source_ref, title, text) "
            "VALUES ('c1', 'pr', 'octo-org/widgets#7', 'PR #7: Add focus ring', "
            "'keyboard focus ring')"
        )
        conn.commit()
    finally:
        conn.close()


def _schema_cookie(path: Path) -> int:
    """Returns SQLite's schema cookie, which changes on every DDL statement."""
    conn = sqlite3.connect(str(path))
    try:
        return int(conn.execute("PRAGMA schema_version").fetchone()[0])
    finally:
        conn.close()


class TestMigrations(unittest.TestCase):
    """Tests fresh, in-place, repeated, and refused migrations."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "repos.sqlite"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _view_names(self, store: PackratStore) -> set[str]:
        rows = store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'")
        return {str(r[0]) for r in rows}

    def test_fresh_db_reaches_latest_version_with_views(self) -> None:
        store = PackratStore(self.db_path)
        try:
            self.assertEqual(store.applied_migrations, [1, 2, 3, 4, 5, 6, 7])
            self.assertEqual(read_schema_version(store.conn), LATEST_SCHEMA_VERSION)
            self.assertEqual(self._view_names(store), set(READER_VIEWS))
            stored = store.conn.execute(
                "SELECT views_sha256 FROM views_meta"
            ).fetchone()[0]
            self.assertEqual(stored, views_sha256())
            for view in READER_VIEWS:
                store.conn.execute(f"SELECT * FROM {view}").fetchall()
            health = store.conn.execute(
                "SELECT beat_age_s, pid FROM v_service_health"
            ).fetchall()
            self.assertEqual([tuple(r) for r in health], [(None, None)])
        finally:
            store.close()

    def test_pre_migration_db_upgrades_in_place_and_keeps_rows(self) -> None:
        _build_pre_migration_db(self.db_path)
        store = PackratStore(self.db_path)
        try:
            self.assertEqual(store.applied_migrations, [1, 2, 3, 4, 5, 6, 7])
            repo = store.get_repo("octo-org/widgets")
            assert repo is not None
            self.assertEqual(repo.mode, "mirror")
            self.assertEqual(store.table_counts()["pull_requests"], 1)
            self.assertEqual(
                store.get_state(f"{REFETCH_OPEN_PRS_KEY_PREFIX}{repo.repo_id}"), "1"
            )
            row = store.conn.execute(
                "SELECT item_key, title, is_cross_repository, repo_mode FROM v_pr_basic"
            ).fetchone()
            self.assertEqual(
                tuple(row), ("octo-org/widgets#7", "Add focus ring", None, "mirror")
            )
            self.assertEqual(
                store.conn.execute("SELECT COUNT(*) FROM v_pr_live").fetchone()[0], 0
            )
            issue = store.conn.execute(
                "SELECT item_key, title, state, assignees_json FROM v_issue_basic"
            ).fetchone()
            self.assertEqual(
                tuple(issue), ("octo-org/widgets#3", "Broken focus", "OPEN", '["bob"]')
            )
        finally:
            store.close()

    def test_reopen_is_a_no_op_without_ddl(self) -> None:
        PackratStore(self.db_path).close()
        cookie = _schema_cookie(self.db_path)
        with mock.patch.object(db_migrations, "refresh_views") as refresh:
            store = PackratStore(self.db_path)
            store.close()
        refresh.assert_not_called()
        self.assertEqual(store.applied_migrations, [])
        self.assertEqual(_schema_cookie(self.db_path), cookie)

    def test_changed_views_file_recreates_views_only(self) -> None:
        PackratStore(self.db_path).close()
        edited = Path(self.tmp.name) / "views.sql"
        edited.write_text(
            db_migrations.VIEWS_SQL_PATH.read_text(encoding="utf-8") + "\n-- edited\n",
            encoding="utf-8",
        )
        conn = sqlite3.connect(str(self.db_path))
        try:
            self.assertEqual(apply_migrations(conn, edited), [])
            stored = conn.execute("SELECT views_sha256 FROM views_meta").fetchone()[0]
            self.assertEqual(stored, views_sha256(edited))
            self.assertEqual(read_schema_version(conn), LATEST_SCHEMA_VERSION)
        finally:
            conn.close()

    def test_migration_already_applied_by_another_process_is_skipped(self) -> None:
        PackratStore(self.db_path).close()
        conn = sqlite3.connect(str(self.db_path))
        try:
            calls: list[int] = []
            self.assertFalse(
                db_migrations._run_migration(conn, 2, lambda _c: calls.append(2))
            )
            self.assertEqual(calls, [])
            self.assertFalse(conn.in_transaction)
        finally:
            conn.close()

    def test_failed_migration_rolls_back(self) -> None:
        def boom(conn: sqlite3.Connection) -> None:
            conn.execute("CREATE TABLE half_done (x INTEGER)")
            raise RuntimeError("boom")

        PackratStore(self.db_path).close()
        conn = sqlite3.connect(str(self.db_path))
        try:
            with self.assertRaises(RuntimeError):
                db_migrations._run_migration(conn, LATEST_SCHEMA_VERSION + 1, boom)
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            self.assertNotIn("half_done", names)
            self.assertEqual(read_schema_version(conn), LATEST_SCHEMA_VERSION)
        finally:
            conn.close()

    def test_v2_check_runs_are_rekeyed_by_0003(self) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript(db_migrations.CONNECTION_PRAGMAS)
            for version, migrate in db_migrations.MIGRATIONS[:2]:
                db_migrations._run_migration(conn, version, migrate)
            conn.execute(
                "INSERT INTO repos (owner, name, full_name, created_at, updated_at) "
                "VALUES ('o', 'r', 'o/r', 'now', 'now')"
            )
            conn.execute(OLD_PR_INSERT, (1, 7, "PR", "feature"))
            conn.execute(
                "INSERT INTO check_runs (repo_id, pr_number, head_oid, name, kind, conclusion) "
                "VALUES (1, 7, 'abc', 'build', 'CheckRun', 'FAILURE')"
            )
            conn.commit()
            self.assertEqual(apply_migrations(conn), [3, 4, 5, 6, 7])
            rows = conn.execute(
                "SELECT item_key, run_key, name, conclusion FROM v_pr_checks"
            ).fetchall()
            self.assertEqual(rows, [("o/r#7", "CheckRun:build", "build", "FAILURE")])
            pk = [r[1] for r in conn.execute("PRAGMA table_info(check_runs)") if r[5]]
            self.assertEqual(pk, ["repo_id", "pr_number", "head_oid", "run_key"])
        finally:
            conn.close()

    def test_migration_0005_converts_f32_embeddings_in_place(self) -> None:
        import struct

        from models import EMBEDDING_DIM, l2_normalize

        def _pack_f32_vector(vec: tuple[float, ...]) -> bytes:
            return struct.pack(f"<{EMBEDDING_DIM}f", *vec)

        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript(db_migrations.CONNECTION_PRAGMAS)
            for version, migrate in db_migrations.MIGRATIONS[:4]:
                db_migrations._run_migration(conn, version, migrate)
            conn.execute("DROP TABLE chunk_embeddings")
            conn.execute(
                """
                CREATE TABLE chunk_embeddings (
                  chunk_id TEXT PRIMARY KEY REFERENCES chunks(chunk_id) ON DELETE CASCADE,
                  model_id TEXT NOT NULL,
                  dim INTEGER NOT NULL DEFAULT 384,
                  vector_f32 BLOB NOT NULL,
                  vector_json TEXT NOT NULL,
                  content_sha256 TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO repos (owner, name, full_name, created_at, updated_at) "
                "VALUES ('o', 'r', 'o/r', 'now', 'now')"
            )
            conn.execute(
                "INSERT INTO chunks (chunk_id, repo_id, source_kind, source_ref, title, "
                "content_sha256, text, updated_at) VALUES ('c1', 1, 'pr', 'o/r#1', "
                "'PR #1', 'sha1', 'text', 'now'), ('c2', 1, 'pr', 'o/r#2', "
                "'PR #2', 'sha2', 'text', 'now')"
            )
            orig_vec = l2_normalize(
                [
                    0.8 if i == 0 else -0.4 if i == 1 else 0.1
                    for i in range(EMBEDDING_DIM)
                ]
            )
            conn.execute(
                """
                INSERT INTO chunk_embeddings (
                  chunk_id, model_id, dim, vector_f32, vector_json, content_sha256, updated_at
                ) VALUES
                  ('c1', 'legacy-model', 384, ?, '[]', 'sha1', 'now'),
                  ('c2', 'legacy-model', 384, ?, '[]', 'sha2', 'now')
                """,
                (_pack_f32_vector(orig_vec), b"corrupt-truncated-blob"),
            )
            conn.commit()
        finally:
            conn.close()

        stale_init = self.db_path.parent / "init.sql"
        stale_init.write_text("-- stale v4 vector_json init.sql\n", encoding="utf-8")

        store = PackratStore(self.db_path)
        try:
            self.assertEqual(store.applied_migrations, [5, 6, 7])
            self.assertNotIn("vector_json", stale_init.read_text(encoding="utf-8"))
            self.assertIn("vector_u8", stale_init.read_text(encoding="utf-8"))
            seq_row = store.conn.execute(
                "SELECT value FROM service_state WHERE key = 'vector_cache_seq'"
            ).fetchone()
            self.assertIsNotNone(seq_row)
            emb = store.get_embedding("c1")
            self.assertIsNotNone(emb)
            assert emb is not None
            self.assertIsNone(store.get_embedding("c2"))
            reconstructed_norm = l2_normalize(emb.vector)
            cosine = sum(a * b for a, b in zip(orig_vec, reconstructed_norm))
            self.assertGreater(cosine, 0.999)
        finally:
            store.close()

    def test_migration_0006_adds_repo_aliases_with_nocase_and_cascade(self) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript(db_migrations.CONNECTION_PRAGMAS)
            for version, migrate in db_migrations.MIGRATIONS[:5]:
                db_migrations._run_migration(conn, version, migrate)
            self.assertEqual(apply_migrations(conn), [6, 7])
            conn.execute(
                "INSERT INTO repos (owner, name, full_name, created_at, updated_at) "
                "VALUES ('octo-org', 'widgets', 'octo-org/widgets', 'now', 'now')"
            )
            conn.execute(
                "INSERT INTO repo_aliases (alias_full_name, repo_id, created_at) "
                "VALUES ('Old-Org/Old-Widgets', 1, 'now')"
            )
            hit = conn.execute(
                "SELECT repo_id FROM repo_aliases WHERE alias_full_name = 'old-org/old-widgets'"
            ).fetchone()
            self.assertEqual(hit, (1,))
            conn.execute("DELETE FROM repos WHERE repo_id = 1")
            remaining = conn.execute("SELECT COUNT(*) FROM repo_aliases").fetchone()[0]
            self.assertEqual(remaining, 0)
        finally:
            conn.close()

    def test_migration_0007_adds_timeline_tables_columns_and_indexes(self) -> None:
        self.assertEqual(LATEST_SCHEMA_VERSION, 7)
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript(db_migrations.CONNECTION_PRAGMAS)
            for version, migrate in db_migrations.MIGRATIONS[:6]:
                db_migrations._run_migration(conn, version, migrate)
            conn.execute(
                "INSERT INTO repos (owner, name, full_name, created_at, updated_at) "
                "VALUES ('octo-org', 'widgets', 'octo-org/widgets', 'now', 'now')"
            )
            conn.execute(OLD_PR_INSERT, (1, 7, "Add focus ring", "fix-focus"))
            conn.commit()
            self.assertEqual(apply_migrations(conn), [7])
            self.assertEqual(read_schema_version(conn), 7)
            tables = {
                str(r[0])
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            self.assertLessEqual({"timeline_events", "timeline_dirty"}, tables)
            for table in ("pull_requests", "issues"):
                self.assertLessEqual(
                    {"timeline_synced_at", "xref_synced_at"},
                    db_migrations._table_columns(conn, table),
                )
            indexes = {
                str(r[0])
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
            }
            self.assertLessEqual(
                {
                    "idx_timeline_item",
                    "idx_timeline_type",
                    "idx_timeline_subject",
                    "idx_prs_timeline_pending",
                    "idx_issues_timeline_pending",
                },
                indexes,
            )
            pending = conn.execute(
                "SELECT number, timeline_synced_at FROM pull_requests"
            ).fetchall()
            self.assertEqual(pending, [(7, None)])
            plan = " ".join(
                str(r[3])
                for r in conn.execute(
                    "EXPLAIN QUERY PLAN SELECT number FROM pull_requests "
                    "WHERE repo_id = 1 AND timeline_synced_at IS NULL"
                )
            )
            self.assertIn("idx_prs_timeline_pending", plan)

            # Re-running is a no-op, and migration 7 itself is idempotent.
            self.assertEqual(apply_migrations(conn), [])
            conn.execute("BEGIN IMMEDIATE")
            db_migrations.migrate_0007_timeline_events(conn)
            conn.execute("COMMIT")
            self.assertEqual(read_schema_version(conn), 7)

            conn.execute(
                "INSERT INTO timeline_events (event_id, repo_id, item_kind, item_number, "
                "event_type, created_at) VALUES ('E1', 1, 'pr', 7, 'MergedEvent', 'now')"
            )
            conn.execute(
                "INSERT INTO timeline_dirty (repo_id, item_number, reason, queued_at) "
                "VALUES (1, 7, 'mention', 'now')"
            )
            conn.execute("DELETE FROM repos WHERE repo_id = 1")
            for table in ("timeline_events", "timeline_dirty"):
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                self.assertEqual(count, 0)
        finally:
            conn.close()

    def test_reader_indexes_exist_and_comment_lookups_use_them(self) -> None:
        # L8
        store = PackratStore(self.db_path)
        try:
            names = {
                r[0]
                for r in store.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            self.assertTrue({"idx_prs_live", "idx_changes_at"} <= names)
            plan = " ".join(
                str(r[3])
                for r in store.conn.execute(
                    "EXPLAIN QUERY PLAN SELECT * FROM v_pr_live WHERE item_key = 'a/b#1'"
                )
            )
            self.assertIn("idx_prs_live", plan)
        finally:
            store.close()

    def test_newer_schema_version_is_refused(self) -> None:
        PackratStore(self.db_path).close()
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (99, 'later')"
        )
        conn.commit()
        conn.close()
        with self.assertRaises(SchemaTooNewError):
            PackratStore(self.db_path)
        err = io.StringIO()
        with mock.patch("sys.stderr", err), mock.patch("sys.stdout", io.StringIO()):
            rc = packrat.main(["--db", str(self.db_path), "status", "--json"])
        self.assertEqual(rc, 2)
        self.assertIn("schema version 99 is newer", err.getvalue())


class TestBackwardCompatibility(unittest.TestCase):
    """Shows that pre-migration readers and writers still work on a migrated database."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "repos.sqlite"
        _build_pre_migration_db(self.db_path)
        PackratStore(self.db_path).close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_old_open_and_old_pr_insert_still_work(self) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            # What packrat's PackratStore.__init__ ran on every open before versioning.
            conn.executescript("PRAGMA journal_mode = WAL;\n" + SCHEMA_SQL)
            conn.execute(OLD_PR_INSERT, (1, 8, "Second PR", "second"))
            conn.commit()
            count = conn.execute("SELECT COUNT(*) FROM v_pr_basic").fetchone()[0]
            self.assertEqual(count, 2)
        finally:
            conn.close()

    def test_search_and_status_read_paths(self) -> None:
        store = PackratStore(self.db_path)
        try:
            hits = hybrid_rrf_search(store, query="focus ring", mode="fts", limit=5)
            self.assertTrue(any(h.chunk_id == "c1" for h in hits))
        finally:
            store.close()
        status = get_auto_sync_status(
            db_path=self.db_path,
            systemd_dir=Path(self.tmp.name) / "no_units",
            backend="systemd-user",
            runner=lambda _a: CmdResult(1, "disabled", ""),
        )
        out = io.StringIO()
        with (
            mock.patch("packrat.get_auto_sync_status", return_value=status),
            mock.patch("sys.stdout", out),
        ):
            rc = packrat.main(["--db", str(self.db_path), "status", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["repos"][0]["counts"]["pull_requests"], 1)
        self.assertEqual(payload["repos"][0]["mode"], "mirror")

    @unittest.skipUnless(shutil.which("duckdb"), "duckdb CLI not installed")
    def test_init_sql_old_and_new_duckdb_views(self) -> None:
        from models import EMBEDDING_DIM, StoredEmbeddingRecord, l2_normalize

        store = PackratStore(self.db_path)
        try:
            vec = l2_normalize([1.0 if i == 0 else 0.2 for i in range(EMBEDDING_DIM)])
            store.upsert_embeddings(
                [
                    StoredEmbeddingRecord(
                        chunk_id="c1",
                        model_id="test-model",
                        content_sha256="sha",
                        vector=vec,
                    )
                ]
            )
            init_path = store.write_init_sql()
        finally:
            store.close()
        query = (
            "SELECT (SELECT count(*) FROM v_pull_requests) AS old_prs, "
            "(SELECT count(*) FROM v_item_participants) AS participants, "
            "(SELECT count(*) FROM v_pr_basic) AS basic, "
            "(SELECT count(*) FROM v_service_health) AS health, "
            "(SELECT count(*) FROM kb.api_calls) AS calls, "
            "(SELECT ROUND(kb_cosine(vector_u8, scale, kb_vec(vector_u8, scale)), 2) "
            "FROM v_chunk_vectors WHERE chunk_id = 'c1') AS self_cos"
        )
        res = subprocess.run(
            [
                "duckdb",
                "-batch",
                "-no-agent",
                "-json",
                "-init",
                str(init_path),
                "-c",
                query,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        rows = json.loads(res.stdout[res.stdout.index("[") :])
        self.assertEqual(
            rows,
            [
                {
                    "old_prs": 1,
                    "participants": 1,
                    "basic": 1,
                    "health": 1,
                    "calls": 0,
                    "self_cos": 1.0,
                }
            ],
        )


class TestDuckDbParity(unittest.TestCase):
    """L9: the DuckDB and SQLite health views agree on the latest budget row."""

    @unittest.skipUnless(shutil.which("duckdb"), "duckdb CLI not installed")
    def test_same_second_api_calls_pick_the_same_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PackratStore(Path(tmp) / "repos.sqlite")
            try:
                for remaining in (4963, 4962):
                    store.record_api_call(
                        worker="w",
                        resource="core",
                        cost=1,
                        remaining=remaining,
                        reset_at=1,
                        status=200,
                        now=1_800_000_000.0,
                    )
                sqlite_value = store.conn.execute(
                    "SELECT core_remaining FROM v_service_health"
                ).fetchone()[0]
                init_path = store.write_init_sql()
            finally:
                store.close()
            res = subprocess.run(
                [
                    "duckdb",
                    "-batch",
                    "-no-agent",
                    "-json",
                    "-init",
                    str(init_path),
                    "-c",
                    "SELECT core_remaining FROM v_service_health",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
            rows = json.loads(res.stdout[res.stdout.index("[") :])
            self.assertEqual((sqlite_value, rows[0]["core_remaining"]), (4962, 4962))


if __name__ == "__main__":
    unittest.main()
