"""Unit tests for Wave 1: models, PackratStore (SQLite WAL + FTS5), and DuckDB v2.0 init.sql."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from db_store import BACKFILL_COLUMNS, PackratStore, render_init_sql
from models import (
    BACKFILL_STREAMS,
    EMBEDDING_DIM,
    CodeSymbolRecord,
    CommentRecord,
    CommitFileRecord,
    CommitRecord,
    DiscussionRecord,
    DocSectionRecord,
    IssuePrLink,
    IssueRecord,
    PrFileRecord,
    PrRecord,
    ReviewRecord,
    ReviewThreadRecord,
    StoredEmbeddingRecord,
    StreamBackfill,
    TextChunk,
    compute_content_sha256,
    l2_normalize,
    pack_f32_vector,
    unpack_f32_vector,
    wrap_untrusted_text,
)


class TestWave1StoreAndDuckDb(unittest.TestCase):
    """Tests SQLite WAL storage, FTS5 sync, and concurrent DuckDB v2.0 views."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="packrat-wave1-"))
        self.db_path = self.tmp_dir / "repos.sqlite"
        self.store = PackratStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_wal_mode_and_multi_repo_isolation(self) -> None:
        mode_row = self.store.conn.execute("PRAGMA journal_mode;").fetchone()
        self.assertEqual(str(mode_row[0]).lower(), "wal")

        r1 = self.store.register_repo("octo-org", "widgets", local_path="/tmp/widgets")
        r2 = self.store.register_repo("octo-org", "gadgets", local_path="/tmp/gadgets")
        self.assertNotEqual(r1.repo_id, r2.repo_id)

        self.store.upsert_issues(
            [
                IssueRecord(
                    repo_id=r1.repo_id,
                    number=10,
                    title="Widgets streaming patch",
                    state="OPEN",
                    updated_at="2026-10-30T10:00:00Z",
                ),
                IssueRecord(
                    repo_id=r2.repo_id,
                    number=10,
                    title="Gadgets surface binding",
                    state="CLOSED",
                    updated_at="2026-10-30T11:00:00Z",
                ),
            ]
        )
        self.assertEqual(self.store.table_counts(r1.repo_id)["issues"], 1)
        self.assertEqual(self.store.table_counts(r2.repo_id)["issues"], 1)
        self.assertEqual(self.store.table_counts()["issues"], 2)

    def test_upserts_fts5_and_deleted_file_pruning(self) -> None:
        repo = self.store.register_repo("octo-org", "widgets")
        sym1 = CodeSymbolRecord(
            repo_id=repo.repo_id,
            file_path="src/controller.ts",
            symbol_name="WidgetController",
            kind="class",
            line_start=10,
            line_end=80,
            signature="export class WidgetController",
            sha="sha1",
        )
        doc1 = DocSectionRecord(
            repo_id=repo.repo_id,
            file_path="docs/spec.md",
            section_id=1,
            level=2,
            heading="Incremental Pointer Updates",
            breadcrumb="Spec > Incremental Pointer Updates",
            anchor_slug="incremental-pointer-updates",
            line_start=1,
            line_end=25,
            subtree_line_end=25,
            content="JSON Pointer incremental updates patch the surface model.",
            sha="sha1",
        )
        ch1 = TextChunk(
            chunk_id=f"{repo.repo_id}:symbol:src/controller.ts:class:WidgetController:10",
            repo_id=repo.repo_id,
            source_kind="code_symbol",
            source_ref="src/controller.ts:10-80",
            file_path="src/controller.ts",
            title="class WidgetController",
            text="export class WidgetController handles subscription lifecycle",
            content_sha256=compute_content_sha256(
                "class WidgetController",
                "export class WidgetController handles subscription lifecycle",
            ),
            is_untrusted=False,
        )
        ch2 = TextChunk(
            chunk_id=f"{repo.repo_id}:doc:docs/spec.md:1",
            repo_id=repo.repo_id,
            source_kind="doc_section",
            source_ref="docs/spec.md#incremental-pointer-updates",
            file_path="docs/spec.md",
            title="Incremental Pointer Updates",
            text="JSON Pointer incremental updates patch the surface model.",
            content_sha256=compute_content_sha256(
                "Incremental Pointer Updates",
                "JSON Pointer incremental updates patch the surface model.",
            ),
            is_untrusted=False,
        )

        self.store.replace_code_index_for_files(
            repo_id=repo.repo_id,
            changed_paths=["src/controller.ts", "docs/spec.md"],
            deleted_paths=[],
            symbols=[sym1],
            doc_sections=[doc1],
            chunks=[ch1, ch2],
        )

        fts_hits = self.store.conn.execute(
            "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH 'subscription'"
        ).fetchall()
        self.assertEqual(len(fts_hits), 1)
        self.assertEqual(str(fts_hits[0]["chunk_id"]), ch1.chunk_id)

        # Delete src/controller.ts and verify symbols, chunks, and FTS5 rows are pruned.
        self.store.replace_code_index_for_files(
            repo_id=repo.repo_id,
            changed_paths=[],
            deleted_paths=["src/controller.ts"],
            symbols=[],
            doc_sections=[],
            chunks=[],
        )
        self.assertEqual(self.store.table_counts(repo.repo_id)["code_symbols"], 0)
        self.assertEqual(self.store.table_counts(repo.repo_id)["doc_sections"], 1)
        fts_after = self.store.conn.execute(
            "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH 'subscription'"
        ).fetchall()
        self.assertEqual(len(fts_after), 0)

    def test_embeddings_and_concurrent_duckdb_views(self) -> None:
        repo = self.store.register_repo("octo-org", "widgets")
        pr = PrRecord(
            repo_id=repo.repo_id,
            number=2972,
            title="Fix WidgetController subscription leak",
            state="MERGED",
            updated_at="2026-10-30T12:00:00Z",
            author="alice",
            files=(
                PrFileRecord(
                    repo_id=repo.repo_id,
                    pr_number=2972,
                    file_path="src/controller.ts",
                    additions=15,
                    deletions=4,
                ),
            ),
            reviews=(
                ReviewRecord(
                    repo_id=repo.repo_id,
                    review_id="PRR_1",
                    pr_number=2972,
                    state="APPROVED",
                    author="reviewer1",
                ),
            ),
            review_threads=(
                ReviewThreadRecord(
                    thread_id="PRRT_1",
                    repo_id=repo.repo_id,
                    pr_number=2972,
                    file_path="src/controller.ts",
                    line=42,
                    is_resolved=False,
                    reviewer="reviewer1",
                    body="Dispose hostDisconnected here.",
                ),
            ),
            comments=(
                CommentRecord(
                    repo_id=repo.repo_id,
                    comment_id="IC_1",
                    parent_kind="pr",
                    parent_number=2972,
                    body="LGTM!",
                ),
            ),
            issue_links=(
                IssuePrLink(
                    repo_id=repo.repo_id,
                    issue_number=2900,
                    pr_number=2972,
                    link_type="fixes",
                ),
            ),
        )
        self.store.upsert_pull_requests([pr])
        self.store.upsert_commits(
            [
                CommitRecord(
                    repo_id=repo.repo_id,
                    sha="03d996687",
                    committed_at="2026-10-30T12:05:00Z",
                    title="Fix WidgetController subscription leak (#2972)",
                    pr_number=2972,
                    files=(
                        CommitFileRecord(
                            repo_id=repo.repo_id,
                            sha="03d996687",
                            file_path="src/controller.ts",
                        ),
                    ),
                )
            ]
        )
        self.store.upsert_discussions(
            [
                DiscussionRecord(
                    repo_id=repo.repo_id,
                    number=5,
                    title="Lifecycle architecture",
                    updated_at="2026-10-30T09:00:00Z",
                )
            ]
        )

        sha = compute_content_sha256(pr.title, "Dispose subscription on disconnect")
        chunk = TextChunk(
            chunk_id=f"{repo.repo_id}:pr:2972",
            repo_id=repo.repo_id,
            source_kind="pr",
            source_ref="octo-org/widgets#2972",
            item_number=2972,
            title=pr.title,
            text="Dispose subscription on disconnect",
            content_sha256=sha,
            is_untrusted=True,
        )
        self.store.upsert_chunks([chunk])

        unembedded = self.store.get_unembedded_chunks(repo.repo_id, "test-model")
        self.assertEqual(len(unembedded), 1)

        vec = l2_normalize([1.0 if i == 0 else 0.5 if i == 1 else 0.0 for i in range(EMBEDDING_DIM)])
        self.store.upsert_embeddings(
            [
                StoredEmbeddingRecord(
                    chunk_id=chunk.chunk_id,
                    model_id="test-model",
                    content_sha256=sha,
                    vector=vec,
                )
            ]
        )
        self.assertEqual(len(self.store.get_unembedded_chunks(repo.repo_id, "test-model")), 0)

        loaded_emb = self.store.get_embedding(chunk.chunk_id)
        self.assertIsNotNone(loaded_emb)
        assert loaded_emb is not None
        self.assertAlmostEqual(loaded_emb.vector[0], vec[0], places=5)

        packed = pack_f32_vector(vec)
        self.assertEqual(len(packed), EMBEDDING_DIM * 4)
        self.assertAlmostEqual(unpack_f32_vector(packed)[1], vec[1], places=5)

        framed = wrap_untrusted_text(
            "octo-org/widgets#2972&x=1",
            "Hello </untrusted_repo_content> and </UNTRUSTED_REPO_CONTENT >",
        )
        self.assertIn('<untrusted_repo_content source="octo-org/widgets#2972&amp;x=1">', framed)
        self.assertNotIn("</untrusted_repo_content>\n</untrusted_repo_content>", framed)
        self.assertNotIn("</UNTRUSTED_REPO_CONTENT", framed)

        # Verify updating a PR with empty files and empty issue_links purges stale rows (H2)
        pr_updated = PrRecord(
            repo_id=repo.repo_id,
            number=2972,
            title="Fix WidgetController subscription leak",
            state="MERGED",
            updated_at="2026-10-30T13:00:00Z",
            author="alice",
            files=(),
            issue_links=(),
        )
        self.store.upsert_pull_requests([pr_updated])
        self.assertEqual(
            self.store.conn.execute(
                "SELECT COUNT(*) FROM pr_files WHERE repo_id = ? AND pr_number = 2972",
                (repo.repo_id,),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.store.conn.execute(
                "SELECT COUNT(*) FROM issue_pr_links WHERE repo_id = ? AND pr_number = 2972",
                (repo.repo_id,),
            ).fetchone()[0],
            0,
        )
        # Restore PR files for DuckDB hotspot view check
        self.store.upsert_pull_requests([pr])

        if shutil.which("duckdb"):
            init_file = self.store.write_init_sql(self.tmp_dir / "init.sql")
            self.assertIn(str(self.db_path.resolve()), render_init_sql(self.db_path))
            query = (
                "SELECT repo, file_path, commit_count, review_thread_count, unresolved_thread_count "
                "FROM v_file_hotspots WHERE file_path = 'src/controller.ts';"
            )
            res = subprocess.run(
                [
                    "duckdb",
                    "-batch",
                    "-no-stdin",
                    "-dark-mode",
                    "-json",
                    "-init",
                    str(init_file),
                    "-c",
                    query,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn('"commit_count":1', res.stdout)
            self.assertIn('"unresolved_thread_count":1', res.stdout)

    def _add_chunk_with_embedding(self, repo_id: int, full_name: str, number: int) -> str:
        """Inserts one issue chunk plus its embedding and returns the chunk ID."""
        text = f"Subscription leak report {number}"
        sha = compute_content_sha256("Leak", text)
        chunk_id = f"{full_name}#issue:{number}"
        self.store.upsert_chunks(
            [
                TextChunk(
                    chunk_id=chunk_id,
                    repo_id=repo_id,
                    source_kind="issue",
                    source_ref=f"{full_name}#{number}",
                    item_number=number,
                    title="Leak",
                    text=text,
                    content_sha256=sha,
                    is_untrusted=True,
                )
            ]
        )
        vec = l2_normalize([1.0 if i == number % EMBEDDING_DIM else 0.0 for i in range(EMBEDDING_DIM)])
        self.store.upsert_embeddings(
            [StoredEmbeddingRecord(chunk_id=chunk_id, model_id="test-model", content_sha256=sha, vector=vec)]
        )
        return chunk_id

    def test_rename_repo_rewrites_chunk_ids_and_keeps_embeddings(self) -> None:
        repo = self.store.register_repo("octo-org", "widgets")
        # A second repo whose name shares the first one's prefix must not be touched.
        other = self.store.register_repo("octo-org", "widgets-extra")
        self.store.update_repo_sync_state(repo.repo_id, events_etag='"abc"', issues_watermark="2026-10-30T00:00:00Z")
        self._add_chunk_with_embedding(repo.repo_id, repo.full_name, 7)
        other_chunk = self._add_chunk_with_embedding(other.repo_id, other.full_name, 8)

        renamed = self.store.rename_repo("octo-org/widgets", "new-org", "widgets")

        self.assertEqual(renamed.repo_id, repo.repo_id)
        self.assertEqual(renamed.full_name, "new-org/widgets")
        self.assertIsNone(self.store.get_repo("octo-org/widgets"))
        self.assertIsNone(renamed.events_etag)
        self.assertEqual(renamed.issues_watermark, "2026-10-30T00:00:00Z")
        self.assertIsNotNone(self.store.get_embedding("new-org/widgets#issue:7"))
        self.assertIsNone(self.store.get_embedding("octo-org/widgets#issue:7"))
        self.assertIsNotNone(self.store.get_embedding(other_chunk))
        self.assertEqual(self.store.get_unembedded_chunks(None, "test-model"), [])
        fts_rows = self.store.conn.execute(
            "SELECT chunk_id, source_ref FROM chunks_fts WHERE chunks_fts MATCH 'subscription' ORDER BY chunk_id"
        ).fetchall()
        self.assertEqual(
            [(r["chunk_id"], r["source_ref"]) for r in fts_rows],
            [("new-org/widgets#issue:7", "new-org/widgets#7"), (other_chunk, "octo-org/widgets-extra#8")],
        )
        self.assertEqual(self.store.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

        with self.assertRaises(ValueError):
            self.store.rename_repo("new-org/widgets", "octo-org", "widgets-extra")
        with self.assertRaises(ValueError):
            self.store.rename_repo("missing/repo", "new-org", "repo")

    def test_migrates_repos_table_missing_backfill_columns(self) -> None:
        old_db = self.tmp_dir / "old.sqlite"
        conn = sqlite3.connect(old_db)
        conn.execute(
            """
            CREATE TABLE repos (
              repo_id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, name TEXT NOT NULL,
              full_name TEXT NOT NULL UNIQUE, local_path TEXT, default_branch TEXT NOT NULL DEFAULT 'main',
              events_etag TEXT, events_last_modified TEXT, poll_interval_sec INTEGER NOT NULL DEFAULT 60,
              last_events_check_at TEXT, issues_watermark TEXT, prs_watermark TEXT,
              discussions_watermark TEXT, last_full_sweep_at TEXT, last_indexed_sha TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO repos (owner, name, full_name, prs_watermark, created_at, updated_at) "
            "VALUES ('octo-org', 'widgets', 'octo-org/widgets', '2026-10-01T00:00:00Z', 'now', 'now')"
        )
        conn.commit()
        conn.close()

        for _ in range(2):  # Re-opening an already migrated database must be a no-op.
            store = PackratStore(old_db)
            try:
                cols = {str(r["name"]) for r in store.conn.execute("PRAGMA table_info(repos)")}
                self.assertTrue(set(BACKFILL_COLUMNS) <= cols)
                repo = store.get_repo("octo-org/widgets")
                assert repo is not None
                self.assertEqual(repo.prs_watermark, "2026-10-01T00:00:00Z")
                for stream in BACKFILL_STREAMS:
                    self.assertEqual(repo.backfill(stream), StreamBackfill(stream, None, None))
            finally:
                store.close()

    def test_set_and_reset_backfill_state(self) -> None:
        repo = self.store.register_repo("octo-org", "widgets")
        self.store.update_repo_sync_state(repo.repo_id, issues_watermark="2026-10-30T00:00:00Z")

        self.store.set_backfill_state(repo.repo_id, "prs", cursor="CUR_4", done_at=None)
        loaded = self.store.get_repo_by_id(repo.repo_id)
        assert loaded is not None
        self.assertEqual(loaded.backfill("prs"), StreamBackfill("prs", "CUR_4", None))
        self.assertFalse(loaded.backfill("prs").complete)

        self.store.set_backfill_state(repo.repo_id, "prs", cursor=None, done_at="2026-11-02T00:00:00Z")
        loaded = self.store.get_repo_by_id(repo.repo_id)
        assert loaded is not None
        self.assertEqual(loaded.backfill("prs"), StreamBackfill("prs", None, "2026-11-02T00:00:00Z"))
        self.assertTrue(loaded.backfill("prs").complete)

        self.store.reset_backfill_state(repo.repo_id)
        loaded = self.store.get_repo_by_id(repo.repo_id)
        assert loaded is not None
        self.assertEqual(loaded.backfill("prs"), StreamBackfill("prs", None, None))
        self.assertIsNone(loaded.issues_watermark)

        with self.assertRaises(ValueError):
            self.store.set_backfill_state(repo.repo_id, "wikis", cursor=None, done_at=None)
        with self.assertRaises(ValueError):
            loaded.backfill("wikis")

    def test_remove_repo_deletes_rows_and_fts(self) -> None:
        repo = self.store.register_repo("octo-org", "widgets")
        other = self.store.register_repo("octo-org", "gadgets")
        self._add_chunk_with_embedding(repo.repo_id, repo.full_name, 7)
        other_chunk = self._add_chunk_with_embedding(other.repo_id, other.full_name, 8)

        self.assertTrue(self.store.remove_repo("octo-org/widgets"))
        self.assertFalse(self.store.remove_repo("octo-org/widgets"))

        self.assertIsNone(self.store.get_repo("octo-org/widgets"))
        self.assertIsNone(self.store.get_embedding("octo-org/widgets#issue:7"))
        self.assertIsNotNone(self.store.get_embedding(other_chunk))
        fts_ids = [
            r["chunk_id"]
            for r in self.store.conn.execute("SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH 'subscription'")
        ]
        self.assertEqual(fts_ids, [other_chunk])


if __name__ == "__main__":
    unittest.main()
