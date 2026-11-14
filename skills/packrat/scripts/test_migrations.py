"""Tests for packrat's versioned schema migrations and reader views."""

from __future__ import annotations

import io
import json
import re
import shutil
import sqlite3
import struct
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
    read_views_version,
    views_sha256,
)
from db_schema import SCHEMA_SQL
from db_store import PackratStore
from models import EMBEDDING_DIM, l2_normalize
from search_engine import hybrid_rrf_search

SCHEMA_V1_PATH = Path(__file__).resolve().parent / "fixtures" / "schema_v1.sql"
SCHEMA_V1_SQL = SCHEMA_V1_PATH.read_text(encoding="utf-8")

READER_VIEWS = (
    "v_api_calls",
    "v_changes",
    "v_issue_basic",
    "v_item_comments",
    "v_notifications",
    "v_pr_basic",
    "v_pr_checks",
    "v_pr_latest_author_reviews",
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
    """Creates a database using the frozen v1 DDL from before `schema_version`."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript("PRAGMA journal_mode = WAL;\n" + SCHEMA_V1_SQL)
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
        vec = l2_normalize([0.9 if i == 0 else 0.05 for i in range(EMBEDDING_DIM)])
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, model_id, dim, vector_f32, vector_json, "
            "content_sha256, updated_at) VALUES ('c1', 'legacy', 384, ?, '[]', 'sha', 'now')",
            (struct.pack(f"<{EMBEDDING_DIM}f", *vec),),
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
            self.assertEqual(
                store.applied_migrations, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
            )
            self.assertEqual(read_schema_version(store.conn), LATEST_SCHEMA_VERSION)
            self.assertEqual(self._view_names(store), set(READER_VIEWS))
            stored, version = store.conn.execute(
                "SELECT views_sha256, views_version FROM views_meta"
            ).fetchone()
            self.assertEqual(stored, views_sha256())
            self.assertEqual(version, read_views_version())
            for view in READER_VIEWS:
                store.conn.execute(f"SELECT * FROM {view}").fetchall()
            health = store.conn.execute(
                "SELECT beat_age_s, pid, last_ok_at, consecutive_failed_ticks FROM v_service_health"
            ).fetchall()
            self.assertEqual([tuple(r) for r in health], [(None, None, None, 0)])
        finally:
            store.close()

    def test_pre_migration_db_upgrades_in_place_and_keeps_rows(self) -> None:
        _build_pre_migration_db(self.db_path)
        store = PackratStore(self.db_path)
        try:
            self.assertEqual(
                store.applied_migrations, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
            )
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
            self.assertEqual(apply_migrations(conn), [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15])
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
            self.assertEqual(store.applied_migrations, [5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15])
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
            self.assertEqual(apply_migrations(conn), [6, 7, 8, 9, 10, 11, 12, 13, 14, 15])
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
        self.assertEqual(LATEST_SCHEMA_VERSION, 15)
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
            self.assertEqual(apply_migrations(conn), [7, 8, 9, 10, 11, 12, 13, 14, 15])
            self.assertEqual(read_schema_version(conn), 15)
            tables = {
                str(r[0])
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertLessEqual({"timeline_events", "timeline_dirty"}, tables)
            for table in ("pull_requests", "issues"):
                self.assertLessEqual(
                    {"timeline_synced_at", "xref_synced_at"},
                    db_migrations._table_columns(conn, table),
                )
            indexes = {
                str(r[0])
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
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
            self.assertEqual(read_schema_version(conn), 15)

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

    def test_migration_0008_backfills_latest_reviews_from_opinionated_reviews(
        self,
    ) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript(db_migrations.CONNECTION_PRAGMAS)
            for version, migrate in db_migrations.MIGRATIONS[:7]:
                db_migrations._run_migration(conn, version, migrate)
            conn.execute(
                "INSERT INTO repos (owner, name, full_name, created_at, updated_at) "
                "VALUES ('octo-org', 'widgets', 'octo-org/widgets', 'now', 'now')"
            )
            conn.execute(OLD_PR_INSERT, (1, 7, "Add focus ring", "fix-focus"))
            conn.execute(
                "INSERT INTO latest_opinionated_reviews "
                "(repo_id, pr_number, author, author_type, state, submitted_at) "
                "VALUES (1, 7, 'bob', 'User', 'CHANGES_REQUESTED', '2026-10-27T20:00:00Z')"
            )
            conn.commit()
            self.assertEqual(apply_migrations(conn), [8, 9, 10, 11, 12, 13, 14, 15])
            rows = conn.execute(
                "SELECT item_key, author, author_type, state, submitted_at, commit_oid "
                "FROM v_pr_latest_author_reviews"
            ).fetchall()
            self.assertEqual(
                rows,
                [
                    (
                        "octo-org/widgets#7",
                        "bob",
                        "User",
                        "CHANGES_REQUESTED",
                        "2026-10-27T20:00:00Z",
                        None,
                    )
                ],
            )
            conn.execute("BEGIN IMMEDIATE")
            db_migrations.migrate_0008_latest_reviews(conn)
            conn.execute("COMMIT")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM latest_reviews").fetchone()[0], 1
            )
        finally:
            conn.close()

    def test_migration_0009_adds_participants_and_requested_at(self) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript(db_migrations.CONNECTION_PRAGMAS)
            for version, migrate in db_migrations.MIGRATIONS[:8]:
                db_migrations._run_migration(conn, version, migrate)
            conn.execute(
                "INSERT INTO repos (owner, name, full_name, created_at, updated_at) "
                "VALUES ('acme', 'widget', 'acme/widget', 'now', 'now')"
            )
            conn.execute(OLD_PR_INSERT, (1, 7, "Add focus ring", "fix-focus"))
            conn.execute(
                "INSERT INTO thread_activity "
                "(thread_id, repo_id, pr_number, is_resolved, is_outdated, last_author, last_at) "
                "VALUES ('T1', 1, 7, 0, 0, 'bob', '2026-10-27T20:00:00Z')"
            )
            conn.execute(
                "INSERT INTO review_requests (repo_id, pr_number, reviewer, reviewer_kind) "
                "VALUES (1, 7, 'bob', 'User')"
            )
            conn.commit()

            self.assertEqual(apply_migrations(conn), [9, 10, 11, 12, 13, 14, 15])
            self.assertEqual(read_schema_version(conn), 15)
            thread_rows = conn.execute(
                "SELECT item_key, thread_id, participants_json FROM v_pr_threads"
            ).fetchall()
            self.assertEqual(thread_rows, [("acme/widget#7", "T1", "[]")])
            req_rows = conn.execute(
                "SELECT item_key, reviewer, reviewer_kind, requested_at FROM v_pr_review_requests"
            ).fetchall()
            self.assertEqual(req_rows, [("acme/widget#7", "bob", "User", None)])

            conn.execute("BEGIN IMMEDIATE")
            db_migrations.migrate_0009_review_turn_signals(conn)
            conn.execute("COMMIT")
            self.assertEqual(read_schema_version(conn), 15)
        finally:
            conn.close()

    def test_migration_0010_aligns_chunks_fts_rowids_and_adds_file_index(self) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript(db_migrations.CONNECTION_PRAGMAS)
            for version, migrate in db_migrations.MIGRATIONS[:9]:
                db_migrations._run_migration(conn, version, migrate)
            conn.execute("DROP TABLE chunks_fts")
            conn.execute(
                """
                CREATE VIRTUAL TABLE chunks_fts USING fts5(
                  chunk_id UNINDEXED,
                  source_kind UNINDEXED,
                  source_ref UNINDEXED,
                  title,
                  text,
                  tokenize='porter unicode61'
                )
                """
            )
            conn.execute(
                "INSERT INTO repos (owner, name, full_name, created_at, updated_at) "
                "VALUES ('acme', 'widget', 'acme/widget', 'now', 'now')"
            )
            conn.execute(
                "INSERT INTO chunks (rowid, chunk_id, repo_id, source_kind, source_ref, "
                "file_path, title, content_sha256, text, updated_at) "
                "VALUES (5, 'c5', 1, 'code_symbol', 'src/a.py#L1-L2', 'src/a.py', "
                "'fn a', 'sha5', 'def a(): pass', 'now')"
            )
            conn.execute(
                "INSERT INTO chunks_fts (rowid, chunk_id, source_kind, source_ref, title, text) "
                "VALUES (99, 'c5', 'code_symbol', 'src/a.py#L1-L2', 'fn a', 'def a(): pass')"
            )
            conn.commit()

            self.assertEqual(apply_migrations(conn), [10, 11, 12, 13, 14, 15])
            self.assertEqual(read_schema_version(conn), 15)
            fts_rows = conn.execute("SELECT rowid, chunk_id FROM chunks_fts").fetchall()
            self.assertEqual(fts_rows, [(5, "c5")])
            conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('integrity-check')")
            indexes = {
                str(r[0])
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
            self.assertIn("idx_chunks_repo_file", indexes)
        finally:
            conn.close()

    def test_migration_0011_converts_chunks_fts_to_external_content(self) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript(db_migrations.CONNECTION_PRAGMAS)
            for version, migrate in db_migrations.MIGRATIONS[:9]:
                db_migrations._run_migration(conn, version, migrate)
            conn.execute("DROP TABLE chunks_fts")
            conn.execute(
                """
                CREATE VIRTUAL TABLE chunks_fts USING fts5(
                  chunk_id UNINDEXED,
                  source_kind UNINDEXED,
                  source_ref UNINDEXED,
                  title,
                  text,
                  tokenize='porter unicode61'
                )
                """
            )
            conn.execute(
                "INSERT INTO repos (owner, name, full_name, created_at, updated_at) "
                "VALUES ('acme', 'widget', 'acme/widget', 'now', 'now')"
            )
            conn.execute(
                "INSERT INTO chunks (rowid, chunk_id, repo_id, source_kind, source_ref, "
                "file_path, title, content_sha256, text, updated_at) "
                "VALUES (5, 'c5', 1, 'code_symbol', 'src/a.py#L1-L2', 'src/a.py', "
                "'fn alpha', 'sha5', 'def alpha_unique_symbol(): pass', 'now')"
            )
            conn.execute(
                "INSERT INTO chunks_fts (rowid, chunk_id, source_kind, source_ref, title, text) "
                "VALUES (5, 'c5', 'code_symbol', 'src/a.py#L1-L2', 'fn alpha', "
                "'def alpha_unique_symbol(): pass')"
            )
            conn.commit()
            db_migrations._run_migration(
                conn, 10, db_migrations.migrate_0010_fts_rowid_and_file_index
            )

            before_tables = {
                str(r[0])
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertIn("chunks_fts_content", before_tables)

            self.assertEqual(apply_migrations(conn), [11, 12, 13, 14, 15])
            self.assertEqual(read_schema_version(conn), 15)
            after_tables = {
                str(r[0])
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertNotIn("chunks_fts_content", after_tables)
            conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('integrity-check')")
            hits = conn.execute(
                "SELECT c.chunk_id FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid "
                "WHERE chunks_fts MATCH 'alpha_unique_symbol'"
            ).fetchall()
            self.assertEqual(hits, [("c5",)])
        finally:
            conn.close()

    def test_schema_sql_has_only_v1_tables_and_matches_upgraded_v1_db(self) -> None:
        create_re = re.compile(
            r"CREATE\s+(?:VIRTUAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z0-9_]+)",
            re.IGNORECASE,
        )
        v1_tables = set(create_re.findall(SCHEMA_V1_SQL))
        current_baseline_tables = set(create_re.findall(SCHEMA_SQL))
        self.assertEqual(current_baseline_tables, v1_tables)

        fresh_path = Path(self.tmp.name) / "fresh.sqlite"
        upgraded_path = Path(self.tmp.name) / "upgraded.sqlite"
        _build_pre_migration_db(upgraded_path)
        fresh = PackratStore(fresh_path)
        upgraded = PackratStore(upgraded_path)
        try:
            def _snapshot(conn: sqlite3.Connection) -> tuple[dict[str, list[tuple[object, ...]]], dict[str, set[str]]]:
                tables = sorted(
                    str(r[0])
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' "
                        "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'chunks_fts%'"
                    )
                )
                cols = {
                    t: [
                        (str(c[1]), str(c[2]).upper(), int(c[3]), int(c[5]))
                        for c in conn.execute(f"PRAGMA table_info({t})")
                    ]
                    for t in tables
                }
                idxs = {
                    t: {
                        str(i[1])
                        for i in conn.execute(f"PRAGMA index_list({t})")
                        if not str(i[1]).startswith("sqlite_autoindex_")
                    }
                    for t in tables
                }
                return cols, idxs

            fresh_cols, fresh_idxs = _snapshot(fresh.conn)
            upg_cols, upg_idxs = _snapshot(upgraded.conn)
            self.assertEqual(fresh_cols, upg_cols)
            self.assertEqual(fresh_idxs, upg_idxs)
        finally:
            fresh.close()
            upgraded.close()

    def test_migration_0012_normalizes_utc_prunes_old_heads_and_backfills_participants(
        self,
    ) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript(db_migrations.CONNECTION_PRAGMAS)
            for version, migrate in db_migrations.MIGRATIONS[:11]:
                db_migrations._run_migration(conn, version, migrate)
            conn.execute(
                "INSERT INTO repos (owner, name, full_name, created_at, updated_at) "
                "VALUES ('acme', 'widget', 'acme/widget', 'now', 'now')"
            )
            conn.execute(OLD_PR_INSERT, (1, 7, "Add focus ring", "fix-focus"))
            conn.execute(
                "INSERT INTO commits (repo_id, sha, committed_at, title) "
                "VALUES (1, 'sha1', '2026-03-02T10:00:00-07:00', 'Offset commit')"
            )
            conn.execute(
                "INSERT INTO chunks (chunk_id, repo_id, source_kind, source_ref, title, "
                "content_sha256, text, updated_at) "
                "VALUES ('commit:1', 1, 'commit', 'acme/widget@sha1', 'Offset commit', "
                "'h1', 'body', '2026-03-02T10:00:00-07:00')"
            )
            conn.execute(
                "INSERT INTO check_runs (repo_id, pr_number, head_oid, run_key, name, kind) "
                "VALUES (1, 7, 'abc', 'CheckRun:1', 'ci', 'CheckRun'), "
                "(1, 7, 'old_sha', 'CheckRun:2', 'ci', 'CheckRun')"
            )
            comments_json = json.dumps(
                [
                    {"author": {"login": "carol"}},
                    {"author": {"login": "bob"}},
                ]
            )
            conn.execute(
                "INSERT INTO review_threads (thread_id, repo_id, pr_number, reviewer, "
                "comments_json) VALUES ('RT_1', 1, 7, 'dave', ?)",
                (comments_json,),
            )
            conn.commit()

            self.assertEqual(apply_migrations(conn), [12, 13, 14, 15])
            committed_at = conn.execute(
                "SELECT committed_at FROM commits WHERE sha = 'sha1'"
            ).fetchone()[0]
            self.assertEqual(committed_at, "2026-03-02T17:00:00Z")
            chunk_updated = conn.execute(
                "SELECT updated_at FROM chunks WHERE chunk_id = 'commit:1'"
            ).fetchone()[0]
            self.assertEqual(chunk_updated, "2026-03-02T17:00:00Z")
            heads = [
                r[0] for r in conn.execute("SELECT head_oid FROM check_runs").fetchall()
            ]
            self.assertEqual(heads, ["abc"])
            participants = conn.execute(
                "SELECT participants_json FROM review_threads WHERE thread_id = 'RT_1'"
            ).fetchone()[0]
            self.assertEqual(json.loads(participants), ["bob", "carol", "dave"])
            indexes = {
                str(r[0])
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
            }
            self.assertLessEqual({"idx_prs_node_id", "idx_issues_node_id"}, indexes)
        finally:
            conn.close()

    def test_migration_0013_normalizes_live_state_keys_with_aliases_and_views(
        self,
    ) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript(db_migrations.CONNECTION_PRAGMAS)
            for version, migrate in db_migrations.MIGRATIONS[:12]:
                db_migrations._run_migration(conn, version, migrate)
            # Recreate pre-v13 views so migrate_0013 must drop them before table rename.
            conn.executescript(
                """
                CREATE VIEW v_watches AS SELECT consumer, consumer_id, item_key FROM watches;
                CREATE VIEW v_viewer_items AS SELECT set_name, item_key FROM viewer_sets;
                CREATE VIEW v_service_health AS SELECT COUNT(*) AS active_leases FROM watches;
                """
            )
            conn.execute(
                "INSERT INTO repos (owner, name, full_name, created_at, updated_at, mode) "
                "VALUES ('google', 'a2ui-composer', 'google/a2ui-composer', 'now', 'now', 'mirror')"
            )
            conn.execute(
                "INSERT INTO repo_aliases (alias_full_name, repo_id, created_at) "
                "VALUES ('google/A2UI', 1, 'now')"
            )
            conn.execute(
                "INSERT INTO watches (consumer, consumer_id, item_key, freshness_s, lease_expires_at) "
                "VALUES ('monitor', 'c1', 'google/A2UI#12', 60, '2099-01-01T00:00:00Z'), "
                "('monitor', 'c1', 'google/a2ui-composer#12', 30, '2099-01-02T00:00:00Z'), "
                "('inflight', 'c2', 'new-org/new-repo#5', 120, '2099-01-01T00:00:00Z')"
            )
            conn.execute(
                "INSERT INTO viewer_sets (set_name, item_key, first_seen_at, last_seen_at) "
                "VALUES ('authored', 'google/A2UI#12', '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z')"
            )
            conn.execute(
                "INSERT INTO refresh_requests (item_key, requested_at) "
                "VALUES ('google/A2UI#12', '2026-01-01T00:00:00Z')"
            )
            conn.commit()
        finally:
            conn.close()

        store = PackratStore(self.db_path)
        try:
            self.assertEqual(store.applied_migrations, [13, 14, 15])
            watches = store.active_watches(now=1_800_000_000.0)
            self.assertEqual(
                [(w.consumer, w.item_key, w.freshness_s, w.lease_expires_at) for w in watches],
                [
                    ("monitor", "google/a2ui-composer#12", 30, "2099-01-02T00:00:00Z"),
                    ("inflight", "new-org/new-repo#5", 120, "2099-01-01T00:00:00Z"),
                ],
            )
            viewer_items = store.conn.execute(
                "SELECT set_name, item_key FROM v_viewer_items"
            ).fetchall()
            self.assertEqual(
                [tuple(r) for r in viewer_items],
                [("authored", "google/a2ui-composer#12")],
            )
            self.assertEqual(store.peek_refresh_requests(), ["google/a2ui-composer#12"])
            # Deleting the auto-created items repo cascades to watches.
            store.remove_repo("new-org/new-repo")
            self.assertEqual(
                [w.item_key for w in store.active_watches(now=1_800_000_000.0)],
                ["google/a2ui-composer#12"],
            )
        finally:
            store.close()

    def test_older_views_version_does_not_downgrade_views(self) -> None:
        PackratStore(self.db_path).close()
        older_views = Path(self.tmp.name) / "older_views.sql"
        older_views.write_text(
            "-- packrat-views-version: 1\n"
            "DROP VIEW IF EXISTS v_watches;\n"
            "CREATE VIEW v_watches AS SELECT 1 AS downgraded;\n",
            encoding="utf-8",
        )
        cookie = _schema_cookie(self.db_path)
        conn = sqlite3.connect(str(self.db_path))
        try:
            self.assertEqual(apply_migrations(conn, older_views), [])
            cols = [r[1] for r in conn.execute("PRAGMA table_info(v_watches)")]
            self.assertIn("item_key", cols)
            self.assertNotIn("downgraded", cols)
            self.assertEqual(_schema_cookie(self.db_path), cookie)
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
            conn.executescript("PRAGMA journal_mode = WAL;\n" + SCHEMA_V1_SQL)
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

    @unittest.skipUnless(shutil.which("duckdb"), "duckdb CLI not installed")
    def test_all_15_reader_views_match_columns_between_sqlite_and_duckdb(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PackratStore(Path(tmp) / "repos.sqlite")
            try:
                sqlite_cols = {
                    view: [
                        str(r[1])
                        for r in store.conn.execute(f"PRAGMA table_info({view})")
                    ]
                    for view in READER_VIEWS
                }
                init_path = store.write_init_sql()
            finally:
                store.close()
            describe_sql = " UNION ALL ".join(
                f"SELECT '{view}' AS view_name, list(column_name) AS cols "
                f"FROM (DESCRIBE {view})"
                for view in READER_VIEWS
            )
            res = subprocess.run(
                [
                    "duckdb",
                    "-batch",
                    "-dark-mode",
                    "-no-stdin",
                    "-json",
                    "-init",
                    str(init_path),
                    "-c",
                    describe_sql,
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
            duckdb_rows = json.loads(res.stdout[res.stdout.index("[") :])
            duckdb_cols = {str(r["view_name"]): list(r["cols"]) for r in duckdb_rows}
            self.assertEqual(duckdb_cols, sqlite_cols)


class TestMigration0014SymbolParsers(unittest.TestCase):
    """Verifies schema v14 upgrade path, same-line symbol retention, Q1 rewrite, and fresh parity."""

    def test_v13_to_v14_preserves_same_line_rows_rewrites_chunks_and_matches_fresh_columns(
        self,
    ) -> None:
        from db_migrations import MIGRATIONS, _run_migration, apply_migrations, refresh_views
        from models import EMBEDDING_DIM, l2_normalize

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            v13_db = tmp_path / "v13.sqlite"
            conn = sqlite3.connect(str(v13_db))
            try:
                for ver, mig in MIGRATIONS[:13]:
                    _run_migration(conn, ver, mig)
                refresh_views(conn)
                # Replace code_symbols with the true v13 DDL so the table rebuild runs.
                conn.execute("DROP TABLE code_symbols")
                conn.execute(
                    """
                    CREATE TABLE code_symbols (
                      repo_id INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
                      file_path TEXT NOT NULL,
                      symbol_name TEXT NOT NULL,
                      kind TEXT NOT NULL,
                      line_start INTEGER NOT NULL,
                      line_end INTEGER NOT NULL,
                      signature TEXT NOT NULL,
                      docstring TEXT NOT NULL DEFAULT '',
                      sha TEXT NOT NULL,
                      blob_oid TEXT,
                      PRIMARY KEY (repo_id, file_path, kind, symbol_name, line_start)
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX idx_code_symbols_name ON code_symbols(repo_id, symbol_name)"
                )
                conn.execute(
                    "INSERT INTO repos (repo_id, owner, name, full_name, mode, created_at, updated_at) "
                    "VALUES (1, 'octo-org', 'widgets', 'octo-org/widgets', 'mirror', 'now', 'now')"
                )
                # Same-line getter & setter with identical symbol_name and line_start
                conn.execute(
                    "INSERT INTO code_symbols (repo_id, file_path, symbol_name, kind, line_start, line_end, signature, docstring, sha, blob_oid) VALUES "
                    "(1, 'lib/w.dart', 'size', 'getter', 10, 10, 'int get size', '', 's1', 'b1'), "
                    "(1, 'lib/w.dart', 'size', 'setter', 10, 10, 'set size(int v)', '', 's1', 'b1'), "
                    "(1, 'lib/w.dart', 'Widget', 'class', 1, 20, 'class Widget', '', 's1', 'b1')"
                )
                conn.execute(
                    "INSERT INTO indexed_files (repo_id, file_path, blob_oid, sha) VALUES "
                    "(1, 'lib/w.dart', 'b1', 's1'), "
                    "(1, 'README.md', 'b2', 's1')"
                )
                conn.execute(
                    "INSERT INTO chunks (chunk_id, repo_id, source_kind, source_ref, file_path, title, content_sha256, is_untrusted, text, updated_at) VALUES "
                    "('octo-org/widgets@symbol:lib/w.dart:Widget:1', 1, 'code_symbol', 'lib/w.dart#L1-L20', 'lib/w.dart', 'class Widget', 'h1', 0, 'Widget docs lookup token', 'now'), "
                    "('octo-org/widgets@symbol:orphan.dart:Ghost:5', 1, 'code_symbol', 'orphan.dart#L5-L5', 'orphan.dart', 'class Ghost', 'h2', 0, 'orphan token', 'now')"
                )
                conn.execute(
                    "INSERT INTO chunks_fts(rowid, chunk_id, source_kind, source_ref, title, text) "
                    "SELECT rowid, chunk_id, source_kind, source_ref, title, text FROM chunks"
                )
                blob = bytes([128] * EMBEDDING_DIM)
                conn.execute(
                    "INSERT INTO chunk_embeddings (chunk_id, model_id, dim, scale, vector_u8, content_sha256, updated_at) "
                    "VALUES ('octo-org/widgets@symbol:lib/w.dart:Widget:1', 'm', ?, 1.0, ?, 'h1', 'now')",
                    (EMBEDDING_DIM, blob),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO service_state (key, value, updated_at) VALUES ('vector_cache_seq', '5', 'now')"
                )
                conn.commit()
            finally:
                conn.close()

            store = PackratStore(v13_db)
            try:
                self.assertEqual(store.applied_migrations, [14, 15])
                self.assertEqual(store.table_counts(1)["code_symbols"], 3)
                chunk_ids = {
                    str(r[0])
                    for r in store.conn.execute("SELECT chunk_id FROM chunks").fetchall()
                }
                self.assertEqual(
                    chunk_ids,
                    {
                        "octo-org/widgets@symbol:lib/w.dart:class:Widget:1",
                        "octo-org/widgets@symbol:orphan.dart:Ghost:5",
                    },
                )
                emb_row = store.conn.execute(
                    "SELECT chunk_id FROM chunk_embeddings"
                ).fetchone()
                self.assertEqual(
                    emb_row[0], "octo-org/widgets@symbol:lib/w.dart:class:Widget:1"
                )
                store.conn.execute(
                    "INSERT INTO chunks_fts(chunks_fts) VALUES('integrity-check')"
                )
                fts_hit = store.conn.execute(
                    "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH 'Widget'"
                ).fetchone()
                self.assertEqual(
                    fts_hit[0], "octo-org/widgets@symbol:lib/w.dart:class:Widget:1"
                )
                self.assertGreater(int(store.get_state("vector_cache_seq") or "0"), 5)
                self.assertEqual(
                    store.get_indexed_files(1)["README.md"][1], "md-sections@1"
                )
                self.assertEqual(
                    store.get_indexed_files(1)["lib/w.dart"][1], "regex@1"
                )

                upgraded_cols = {
                    str(t[0]): {
                        str(r[1])
                        for r in store.conn.execute(f"PRAGMA table_info({t[0]})")
                    }
                    for t in store.conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' "
                        "AND name NOT LIKE 'chunks_fts%' AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()
                }
            finally:
                store.close()

            fresh_db = tmp_path / "fresh.sqlite"
            fresh_store = PackratStore(fresh_db)
            try:
                fresh_cols = {
                    str(t[0]): {
                        str(r[1])
                        for r in fresh_store.conn.execute(f"PRAGMA table_info({t[0]})")
                    }
                    for t in fresh_store.conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' "
                        "AND name NOT LIKE 'chunks_fts%' AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()
                }
            finally:
                fresh_store.close()
            self.assertEqual(upgraded_cols, fresh_cols)



class TestMigration0015ManagedGit(unittest.TestCase):
    """Verifies schema v15 upgrade path, silent backfill, CHECK constraint, and fresh parity."""

    def test_v14_to_v15_backfills_git_source_silently_and_enforces_check(self) -> None:
        from db_migrations import MIGRATIONS, _run_migration, read_schema_version, refresh_views

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            v14_db = tmp_path / "v14.sqlite"
            conn = sqlite3.connect(str(v14_db))
            try:
                for ver, mig in MIGRATIONS[:14]:
                    _run_migration(conn, ver, mig)
                refresh_views(conn)
                conn.execute(
                    "INSERT INTO repos (repo_id, owner, name, full_name, local_path, last_indexed_sha, mode, created_at, updated_at) VALUES "
                    "(1, 'octo', 'checkout', 'octo/checkout', '/src/checkout', 'sha1', 'mirror', 'now', 'now'), "
                    "(2, 'flutter', 'flutter', 'flutter/flutter', NULL, 'sha2', 'mirror', 'now', 'now'), "
                    "(3, 'octo', 'gh-only', 'octo/gh-only', NULL, NULL, 'mirror', 'now', 'now')"
                )
                conn.commit()
            finally:
                conn.close()

            buf = io.StringIO()
            with mock.patch("sys.stdout", buf), mock.patch("sys.stderr", buf):
                store = PackratStore(v14_db)
            self.assertEqual(buf.getvalue(), "")
            try:
                self.assertEqual(read_schema_version(store.conn), 15)
                self.assertEqual(store.applied_migrations, [15])
                self.assertEqual(store.get_repo("octo/checkout").git_source, "path")  # type: ignore[union-attr]
                self.assertEqual(store.get_repo("flutter/flutter").git_source, "managed")  # type: ignore[union-attr]
                self.assertEqual(store.get_repo("octo/gh-only").git_source, "none")  # type: ignore[union-attr]
                with self.assertRaises(sqlite3.IntegrityError):
                    store.conn.execute(
                        "UPDATE repos SET git_source = 'invalid' WHERE repo_id = 1"
                    )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
