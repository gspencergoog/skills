"""Unit tests for Wave 1: models, PackratStore (SQLite WAL + FTS5), and DuckDB v2.0 init.sql."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import time
import unittest
from collections.abc import Sequence
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from db_migrations import LATEST_SCHEMA_VERSION
from db_store import (
    BACKFILL_COLUMNS,
    ExportIntegrityError,
    PackratStore,
    _compact_and_verify_export_conn,
    render_init_sql,
)
from models import (
    BACKFILL_STREAMS,
    EMBEDDING_DIM,
    REPO_MODE_ITEMS,
    REPO_MODE_MIRROR,
    TEXT_LINK_TYPES,
    TIMELINE_LINK_TYPES,
    CheckRunRecord,
    CodeSymbolRecord,
    CommentRecord,
    CommitFileRecord,
    CommitRecord,
    DiscussionRecord,
    DocSectionRecord,
    ExportSummary,
    IssuePrLink,
    IssueRecord,
    ItemCommentRecord,
    LatestReviewRecord,
    NotificationRecord,
    OpinionatedReviewRecord,
    PrFileRecord,
    PrLiveRecord,
    PrRecord,
    RepoConfig,
    ReviewRecord,
    ReviewRequestRecord,
    ReviewThreadRecord,
    SearchFilters,
    StoredEmbeddingRecord,
    StreamBackfill,
    SyncStats,
    TextChunk,
    ThreadActivityRecord,
    TimelineEventRecord,
    TimelineMode,
    TimelineStats,
    TimelineTarget,
    compute_content_sha256,
    dequantize_u8_vector,
    iso_at,
    l2_normalize,
    quantize_u8_vector,
    unpack_f32_vector,
    wrap_untrusted_text,
)


def _pack_f32_vector(vec: Sequence[float]) -> bytes:
    """Packs a 384-dim float vector into a little-endian float32 BLOB for tests."""
    return struct.pack(f"<{EMBEDDING_DIM}f", *vec)


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

        self.assertEqual(
            self.store.list_indexed_file_paths(repo.repo_id),
            {"src/controller.ts", "docs/spec.md"},
        )

        # Delete src/controller.ts and verify symbols, chunks, and FTS5 rows are pruned.
        self.store.replace_code_index_for_files(
            repo_id=repo.repo_id,
            changed_paths=[],
            deleted_paths=["src/controller.ts"],
            symbols=[],
            doc_sections=[],
            chunks=[],
        )
        self.assertEqual(
            self.store.list_indexed_file_paths(repo.repo_id), {"docs/spec.md"}
        )
        self.assertEqual(self.store.table_counts(repo.repo_id)["code_symbols"], 0)
        self.assertEqual(self.store.table_counts(repo.repo_id)["doc_sections"], 1)
        fts_after = self.store.conn.execute(
            "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH 'subscription'"
        ).fetchall()
        self.assertEqual(len(fts_after), 0)
        self.store.conn.execute(
            "INSERT INTO chunks_fts(chunks_fts) VALUES('integrity-check')"
        )

        # Update ch2 text in place and confirm old tokens are removed and integrity-check passes.
        ch2_updated = replace(
            ch2,
            text="Reactive signal updates patch the surface model.",
            content_sha256=compute_content_sha256(
                ch2.title,
                "Reactive signal updates patch the surface model.",
            ),
        )
        self.store.upsert_chunks([ch2_updated])
        self.store.conn.execute(
            "INSERT INTO chunks_fts(chunks_fts) VALUES('integrity-check')"
        )
        old_token_hits = self.store.conn.execute(
            "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH 'incremental'"
        ).fetchall()
        # Title still has "Incremental", so update title as well and verify removal.
        ch2_retitled = replace(
            ch2_updated,
            title="Reactive Signal Updates",
            content_sha256=compute_content_sha256(
                "Reactive Signal Updates",
                "Reactive signal updates patch the surface model.",
            ),
        )
        self.store.upsert_chunks([ch2_retitled])
        self.store.conn.execute(
            "INSERT INTO chunks_fts(chunks_fts) VALUES('integrity-check')"
        )
        self.assertEqual(len(old_token_hits), 1)
        removed_hits = self.store.conn.execute(
            "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH 'incremental'"
        ).fetchall()
        self.assertEqual(len(removed_hits), 0)
        new_hits = self.store.conn.execute(
            "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH 'reactive'"
        ).fetchall()
        self.assertEqual([str(r["chunk_id"]) for r in new_hits], [ch2.chunk_id])

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

        vec = l2_normalize(
            [1.0 if i == 0 else 0.5 if i == 1 else 0.0 for i in range(EMBEDDING_DIM)]
        )
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
        self.assertEqual(
            len(self.store.get_unembedded_chunks(repo.repo_id, "test-model")), 0
        )

        loaded_emb = self.store.get_embedding(chunk.chunk_id)
        self.assertIsNotNone(loaded_emb)
        assert loaded_emb is not None
        self.assertAlmostEqual(loaded_emb.vector[0], vec[0], places=5)
        self.assertAlmostEqual(loaded_emb.vector[1], vec[1], places=2)

        cols = {
            str(r[1])
            for r in self.store.conn.execute("PRAGMA table_info(chunk_embeddings)")
        }
        self.assertIn("vector_u8", cols)
        self.assertIn("scale", cols)
        self.assertNotIn("vector_f32", cols)
        self.assertNotIn("vector_json", cols)

        u8_blob, u8_scale = quantize_u8_vector(vec)
        self.assertEqual(len(u8_blob), EMBEDDING_DIM)
        reconstructed = dequantize_u8_vector(u8_blob, u8_scale)
        reconstructed_norm = l2_normalize(reconstructed)
        cosine = sum(a * b for a, b in zip(vec, reconstructed_norm))
        self.assertGreater(cosine, 0.999)

        packed = _pack_f32_vector(vec)
        self.assertEqual(len(packed), EMBEDDING_DIM * 4)
        self.assertAlmostEqual(unpack_f32_vector(packed)[1], vec[1], places=5)

        framed = wrap_untrusted_text(
            "octo-org/widgets#2972&x=1",
            "Hello </untrusted_repo_content> and </UNTRUSTED_REPO_CONTENT >",
        )
        self.assertIn(
            '<untrusted_repo_content source="octo-org/widgets#2972&amp;x=1">', framed
        )
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

    def _add_chunk_with_embedding(
        self, repo_id: int, full_name: str, number: int
    ) -> str:
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
        vec = l2_normalize(
            [1.0 if i == number % EMBEDDING_DIM else 0.0 for i in range(EMBEDDING_DIM)]
        )
        self.store.upsert_embeddings(
            [
                StoredEmbeddingRecord(
                    chunk_id=chunk_id,
                    model_id="test-model",
                    content_sha256=sha,
                    vector=vec,
                )
            ]
        )
        return chunk_id

    def test_rename_repo_rewrites_chunk_ids_and_keeps_embeddings(self) -> None:
        repo = self.store.register_repo("octo-org", "widgets")
        # A second repo whose name shares the first one's prefix must not be touched.
        other = self.store.register_repo("octo-org", "widgets-extra")
        self.store.update_repo_sync_state(
            repo.repo_id, events_etag='"abc"', issues_watermark="2026-10-30T00:00:00Z"
        )
        self._add_chunk_with_embedding(repo.repo_id, repo.full_name, 7)
        other_chunk = self._add_chunk_with_embedding(other.repo_id, other.full_name, 8)

        renamed = self.store.rename_repo("octo-org/widgets", "new-org", "widgets")

        self.assertEqual(renamed.repo_id, repo.repo_id)
        self.assertEqual(renamed.full_name, "new-org/widgets")
        self.assertEqual(self.store.get_repo("octo-org/widgets"), renamed)
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
            [
                ("new-org/widgets#issue:7", "new-org/widgets#7"),
                (other_chunk, "octo-org/widgets-extra#8"),
            ],
        )
        self.store.conn.execute(
            "INSERT INTO chunks_fts(chunks_fts) VALUES('integrity-check')"
        )
        self.assertEqual(
            self.store.conn.execute("PRAGMA foreign_key_check").fetchall(), []
        )

        with self.assertRaises(ValueError):
            self.store.rename_repo("new-org/widgets", "octo-org", "widgets-extra")
        with self.assertRaises(ValueError):
            self.store.rename_repo("missing/repo", "new-org", "repo")
        with self.assertRaises(ValueError):
            self.store.rename_repo("new-org/widgets", "invalid-no-slash")

        renamed_two_arg = self.store.rename_repo("new-org/widgets", "final-org/widgets")
        self.assertEqual(renamed_two_arg.full_name, "final-org/widgets")

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
                cols = {
                    str(r["name"])
                    for r in store.conn.execute("PRAGMA table_info(repos)")
                }
                self.assertTrue(set(BACKFILL_COLUMNS) <= cols)
                repo = store.get_repo("octo-org/widgets")
                assert repo is not None
                self.assertEqual(repo.prs_watermark, "2026-10-01T00:00:00Z")
                for stream in BACKFILL_STREAMS:
                    self.assertEqual(
                        repo.backfill(stream), StreamBackfill(stream, None, None)
                    )
            finally:
                store.close()

    def test_set_and_reset_backfill_state(self) -> None:
        repo = self.store.register_repo("octo-org", "widgets")
        self.store.update_repo_sync_state(
            repo.repo_id, issues_watermark="2026-10-30T00:00:00Z"
        )

        self.store.set_backfill_state(repo.repo_id, "prs", cursor="CUR_4", done_at=None)
        loaded = self.store.get_repo_by_id(repo.repo_id)
        assert loaded is not None
        self.assertEqual(loaded.backfill("prs"), StreamBackfill("prs", "CUR_4", None))
        self.assertFalse(loaded.backfill("prs").complete)

        self.store.set_backfill_state(
            repo.repo_id, "prs", cursor=None, done_at="2026-11-02T00:00:00Z"
        )
        loaded = self.store.get_repo_by_id(repo.repo_id)
        assert loaded is not None
        self.assertEqual(
            loaded.backfill("prs"), StreamBackfill("prs", None, "2026-11-02T00:00:00Z")
        )
        self.assertTrue(loaded.backfill("prs").complete)

        self.store.reset_backfill_state(repo.repo_id)
        loaded = self.store.get_repo_by_id(repo.repo_id)
        assert loaded is not None
        self.assertEqual(loaded.backfill("prs"), StreamBackfill("prs", None, None))
        self.assertIsNone(loaded.issues_watermark)

        with self.assertRaises(ValueError):
            self.store.set_backfill_state(
                repo.repo_id, "wikis", cursor=None, done_at=None
            )
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
            for r in self.store.conn.execute(
                "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH 'subscription'"
            )
        ]
        self.assertEqual(fts_ids, [other_chunk])

    def _run_duckdb_json(self, init_file: Path, query: str) -> list[dict[str, Any]]:
        """Executes a DuckDB SQL query with init_file and returns parsed JSON rows."""
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
        out = res.stdout.strip()
        return json.loads(out) if out else []

    def _seed_collaboration_fixture(self) -> None:
        """Populates PRs, reviews, inline threads, issues, and discussions for collaboration tests."""
        repo = self.store.register_repo("octo-org", "widgets")
        nested_comments = json.dumps(
            [
                {"author": {"login": "Carol"}},
                {"author": None},
                {"author": {"login": ""}},
                {"author": {"login": "Bob"}},
                {"author": {"login": "Bob"}},
            ]
        )
        self.store.upsert_pull_requests(
            [
                PrRecord(
                    repo_id=repo.repo_id,
                    number=101,
                    title="Refactor WidgetController lifecycle",
                    state="MERGED",
                    author="Alice",
                    created_at="2026-02-01T10:00:00Z",
                    updated_at="2026-02-05T12:00:00Z",
                    merged_at="2026-02-05T12:00:00Z",
                    url="https://github.com/octo-org/widgets/pull/101",
                    reviews=(
                        ReviewRecord(
                            repo_id=repo.repo_id,
                            review_id="RV_101_1",
                            pr_number=101,
                            state="approved",
                            author="Bob",
                        ),
                        ReviewRecord(
                            repo_id=repo.repo_id,
                            review_id="RV_101_2",
                            pr_number=101,
                            state="",
                            author="Carol",
                        ),
                        ReviewRecord(
                            repo_id=repo.repo_id,
                            review_id="RV_101_ghost",
                            pr_number=101,
                            state="COMMENTED",
                            author=None,
                        ),
                    ),
                    review_threads=(
                        ReviewThreadRecord(
                            thread_id="RT_101_nested",
                            repo_id=repo.repo_id,
                            pr_number=101,
                            file_path="src/controller.ts",
                            line=20,
                            reviewer="Carol",
                            body="Carol and Bob thread",
                            comments_json=nested_comments,
                        ),
                    ),
                    comments=(
                        CommentRecord(
                            repo_id=repo.repo_id,
                            comment_id="PR_IC_101",
                            parent_kind="pr",
                            parent_number=101,
                            author="Bob",
                            body="Ship it!",
                            url="https://github.com/octo-org/widgets/pull/101#issuecomment-1",
                        ),
                    ),
                ),
                PrRecord(
                    repo_id=repo.repo_id,
                    number=102,
                    title="Co-reviewed surface adapter",
                    state="OPEN",
                    author="Carol",
                    created_at="2026-03-10T09:00:00Z",
                    updated_at="2026-03-12T09:00:00Z",
                    url="https://github.com/octo-org/widgets/pull/102",
                    review_threads=(
                        ReviewThreadRecord(
                            thread_id="RT_102_empty_list",
                            repo_id=repo.repo_id,
                            pr_number=102,
                            reviewer="Alice",
                            comments_json="[]",
                        ),
                        ReviewThreadRecord(
                            thread_id="RT_102_empty_str",
                            repo_id=repo.repo_id,
                            pr_number=102,
                            reviewer="Bob",
                            comments_json="",
                        ),
                        ReviewThreadRecord(
                            thread_id="RT_102_null_reviewer",
                            repo_id=repo.repo_id,
                            pr_number=102,
                            reviewer=None,
                            comments_json="[]",
                        ),
                    ),
                ),
                PrRecord(
                    repo_id=repo.repo_id,
                    number=103,
                    title="Out-of-window legacy PR",
                    state="MERGED",
                    author="Alice",
                    created_at="2025-11-10T00:00:00Z",
                    updated_at="2025-11-15T00:00:00Z",
                    merged_at="2025-11-15T00:00:00Z",
                    url="https://github.com/octo-org/widgets/pull/103",
                    reviews=(
                        ReviewRecord(
                            repo_id=repo.repo_id,
                            review_id="RV_103_1",
                            pr_number=103,
                            state="CHANGES_REQUESTED",
                            author="Bob",
                        ),
                    ),
                ),
                PrRecord(
                    repo_id=repo.repo_id,
                    number=104,
                    title="Solo Alice and Carol PR",
                    state="MERGED",
                    author="Alice",
                    created_at="2026-04-01T00:00:00Z",
                    updated_at="2026-04-02T00:00:00Z",
                    url="https://github.com/octo-org/widgets/pull/104",
                    reviews=(
                        ReviewRecord(
                            repo_id=repo.repo_id,
                            review_id="RV_104_1",
                            pr_number=104,
                            state="APPROVED",
                            author="Carol",
                        ),
                    ),
                ),
            ]
        )
        self.store.upsert_issues(
            [
                IssueRecord(
                    repo_id=repo.repo_id,
                    number=201,
                    title="Memory retention on widget teardown",
                    state="OPEN",
                    author="Alice",
                    created_at="2026-02-10T00:00:00Z",
                    updated_at="2026-02-11T00:00:00Z",
                    url="https://github.com/octo-org/widgets/issues/201",
                )
            ],
            comments=[
                CommentRecord(
                    repo_id=repo.repo_id,
                    comment_id="ISS_IC_201",
                    parent_kind="issue",
                    parent_number=201,
                    author="Bob",
                    body="Reproduced on Linux.",
                    url="https://github.com/octo-org/widgets/issues/201#issuecomment-99",
                )
            ],
        )
        self.store.upsert_discussions(
            [
                DiscussionRecord(
                    repo_id=repo.repo_id,
                    number=301,
                    title="Unanswered RFC",
                    author="Alice",
                    answer_body=None,
                    created_at="2026-02-20T00:00:00Z",
                    updated_at="2026-02-20T00:00:00Z",
                    url="https://github.com/octo-org/widgets/discussions/301",
                ),
                DiscussionRecord(
                    repo_id=repo.repo_id,
                    number=302,
                    title="Empty answer RFC",
                    author="Bob",
                    answer_body="",
                    created_at="2026-02-21T00:00:00Z",
                    updated_at="2026-02-21T00:00:00Z",
                    url="https://github.com/octo-org/widgets/discussions/302",
                ),
                DiscussionRecord(
                    repo_id=repo.repo_id,
                    number=303,
                    title="Answered state management Q&A",
                    author="Alice",
                    answer_body="Use signal bindings.",
                    created_at="2026-02-22T00:00:00Z",
                    updated_at="2026-02-22T00:00:00Z",
                    url="https://github.com/octo-org/widgets/discussions/303",
                ),
            ]
        )

    def test_v_item_participants_collaboration_and_edge_cases(self) -> None:
        if not shutil.which("duckdb"):
            self.skipTest("duckdb CLI is not installed")
        self._seed_collaboration_fixture()
        init_file = self.store.write_init_sql(self.tmp_dir / "init.sql")

        # 1. Verify PR #101 roles, nested comments_json reply extraction, ghost filtering, and parent URL.
        pr101_rows = self._run_duckdb_json(
            init_file,
            "SELECT participant, role, url, item_author, merged_at "
            "FROM v_item_participants WHERE kind = 'pr' AND number = 101 "
            "ORDER BY participant, role;",
        )
        self.assertEqual(
            [(r["participant"], r["role"]) for r in pr101_rows],
            [
                ("alice", "author"),
                ("bob", "comment"),
                ("bob", "inline_comment"),
                ("bob", "review:APPROVED"),
                ("carol", "inline_comment"),
                ("carol", "review:COMMENTED"),
            ],
        )
        self.assertTrue(
            all(
                r["url"] == "https://github.com/octo-org/widgets/pull/101"
                for r in pr101_rows
            )
        )
        self.assertTrue(all(r["item_author"] == "alice" for r in pr101_rows))

        # 2. Verify rt.reviewer fallback when comments_json is '[]' or '' and null filtering on PR #102.
        pr102_rows = self._run_duckdb_json(
            init_file,
            "SELECT participant, role FROM v_item_participants "
            "WHERE kind = 'pr' AND number = 102 ORDER BY participant, role;",
        )
        self.assertEqual(
            [(r["participant"], r["role"]) for r in pr102_rows],
            [
                ("alice", "inline_comment"),
                ("bob", "inline_comment"),
                ("carol", "author"),
            ],
        )

        # 3. Verify issue comment uses parent issue URL and NULL merged_at.
        issue_rows = self._run_duckdb_json(
            init_file,
            "SELECT participant, role, url, state, merged_at FROM v_item_participants "
            "WHERE kind = 'issue' AND number = 201 ORDER BY participant;",
        )
        self.assertEqual(
            issue_rows,
            [
                {
                    "participant": "alice",
                    "role": "author",
                    "url": "https://github.com/octo-org/widgets/issues/201",
                    "state": "OPEN",
                    "merged_at": None,
                },
                {
                    "participant": "bob",
                    "role": "comment",
                    "url": "https://github.com/octo-org/widgets/issues/201",
                    "state": "OPEN",
                    "merged_at": None,
                },
            ],
        )

        # 4. Verify discussions OPEN vs ANSWERED state mapping.
        disc_rows = self._run_duckdb_json(
            init_file,
            "SELECT number, participant, state, role, merged_at FROM v_item_participants "
            "WHERE kind = 'discussion' ORDER BY number;",
        )
        self.assertEqual(
            disc_rows,
            [
                {
                    "number": 301,
                    "participant": "alice",
                    "state": "OPEN",
                    "role": "author",
                    "merged_at": None,
                },
                {
                    "number": 302,
                    "participant": "bob",
                    "state": "OPEN",
                    "role": "author",
                    "merged_at": None,
                },
                {
                    "number": 303,
                    "participant": "alice",
                    "state": "ANSWERED",
                    "role": "author",
                    "merged_at": None,
                },
            ],
        )

        # 5. Verify two-user intersection (@alice & @bob) within a 2026 H1 date window.
        shared = self._run_duckdb_json(
            init_file,
            """
            SELECT
              kind,
              number,
              LIST(DISTINCT role ORDER BY role) FILTER (WHERE participant = 'alice') AS alice_roles,
              LIST(DISTINCT role ORDER BY role) FILTER (WHERE participant = 'bob') AS bob_roles
            FROM v_item_participants
            WHERE repo = 'octo-org/widgets'
              AND participant IN ('alice', 'bob')
              AND updated_at >= '2026-01-01T00:00:00Z'
              AND updated_at <= '2026-06-30T23:59:59Z'
            GROUP BY kind, number
            HAVING COUNT(DISTINCT participant) = 2
            ORDER BY kind, number;
            """,
        )
        self.assertEqual(
            shared,
            [
                {
                    "kind": "issue",
                    "number": 201,
                    "alice_roles": ["author"],
                    "bob_roles": ["comment"],
                },
                {
                    "kind": "pr",
                    "number": 101,
                    "alice_roles": ["author"],
                    "bob_roles": ["comment", "inline_comment", "review:APPROVED"],
                },
                {
                    "kind": "pr",
                    "number": 102,
                    "alice_roles": ["inline_comment"],
                    "bob_roles": ["inline_comment"],
                },
            ],
        )


T0 = 1_800_000_000.0
"""Fixed epoch seconds used as the base time for deterministic live-store tests."""


class TestLiveStore(unittest.TestCase):
    """Tests the live GitHub methods of `LiveStoreMixin` and the reader views."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="packrat-live-"))
        self.store = PackratStore(self.tmp_dir / "repos.sqlite")
        self.repo = self.store.ensure_items_repo("Octo-Org", "Widgets")
        self.key = "Octo-Org/Widgets#7"

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _pr(self, **overrides: Any) -> PrLiveRecord:
        base = PrLiveRecord(
            repo_id=self.repo.repo_id,
            number=7,
            title="Add widgets",
            state="OPEN",
            updated_at="2027-01-15T08:00:00Z",
            node_id="PR_node7",
            author="alice",
            head_sha="sha1",
            is_cross_repository=False,
            rollup_state="PENDING",
            rollup_total=3,
        )
        return replace(base, **overrides)

    def _view(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        return [dict(r) for r in self.store.conn.execute(sql, params).fetchall()]

    def _kinds(self) -> list[str]:
        return [c.kind for c in self.store.changes_since(0)]

    def _check(self, name: str, **overrides: Any) -> CheckRunRecord:
        return replace(
            CheckRunRecord(name=name, kind="CheckRun", status="COMPLETED"), **overrides
        )

    def _repo_names(self, mode: str | None) -> list[str]:
        return [r.full_name for r in self.store.list_repos(mode)]

    def _count(self, table: str) -> int:
        return int(
            self.store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )

    # ----- repositories -----------------------------------------------------------

    def test_ensure_items_repo_and_register_promotes_to_mirror(self) -> None:
        self.assertEqual(self.repo.mode, REPO_MODE_ITEMS)
        again = self.store.ensure_items_repo("octo-org", "widgets")
        self.assertEqual(again.repo_id, self.repo.repo_id)
        mirror = self.store.register_repo("acme", "tools")
        self.assertEqual(mirror.mode, REPO_MODE_MIRROR)
        self.assertEqual(self._repo_names(REPO_MODE_ITEMS), ["Octo-Org/Widgets"])
        self.assertEqual(self._repo_names(REPO_MODE_MIRROR), ["acme/tools"])
        self.assertEqual(self._repo_names(None), ["Octo-Org/Widgets", "acme/tools"])

        promoted = self.store.register_repo("Octo-Org", "Widgets")
        self.assertEqual(promoted.repo_id, self.repo.repo_id)
        self.assertEqual(promoted.mode, REPO_MODE_MIRROR)
        self.assertEqual(self._repo_names(REPO_MODE_ITEMS), [])

    # ----- live PR state ----------------------------------------------------------

    def test_upsert_pr_live_change_detection(self) -> None:
        first = self.store.upsert_pr_live(self._pr(), now=T0)
        self.assertIn("title", first)
        self.assertIn("rollup_state", first)
        self.assertEqual(len(self.store.changes_since(0)), 1)

        self.assertEqual(self.store.upsert_pr_live(self._pr(), now=T0 + 60), [])
        self.assertEqual(len(self.store.changes_since(0)), 1)

        changed = self.store.upsert_pr_live(
            self._pr(title="Add more widgets"), now=T0 + 120
        )
        self.assertEqual(changed, ["title"])
        feed = self.store.changes_since(0)
        self.assertEqual(len(feed), 2)
        self.assertEqual((feed[1].item_key, feed[1].kind), (self.key, "pr"))
        self.assertEqual(feed[1].fields, ("title",))
        self.assertEqual(feed[1].at, iso_at(T0 + 120))

    def test_v_pr_live_shows_rollup_only_for_current_head(self) -> None:
        self.store.upsert_pr_live(self._pr(), now=T0)
        rows = self._view("SELECT * FROM v_pr_live WHERE item_key = ?", self.key)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["repo_mode"], REPO_MODE_ITEMS)
        self.assertEqual((row["rollup_state"], row["rollup_total"]), ("PENDING", 3))
        self.assertEqual(row["live_fetched_at"], iso_at(T0))
        self.assertEqual(row["is_cross_repository"], 0)

        # A stale rollup for another head must not be shown.
        self.store.conn.execute(
            "UPDATE pr_rollup SET head_oid = 'other' WHERE repo_id = ?",
            (self.repo.repo_id,),
        )
        row = self._view("SELECT * FROM v_pr_live WHERE item_key = ?", self.key)[0]
        self.assertIsNone(row["rollup_state"])
        self.assertIsNone(row["rollup_total"])

    def test_head_change_reports_rollup_fields(self) -> None:
        self.store.upsert_pr_live(self._pr(), now=T0)
        changed = self.store.upsert_pr_live(self._pr(head_sha="sha2"), now=T0 + 60)
        self.assertEqual(changed, ["head_sha", "rollup_state", "rollup_total"])

    def test_rollup_etags_round_trip_and_clear_on_head_change(self) -> None:
        self.assertEqual(self.store.get_rollup_etags(self.key), (None, None, None))
        self.store.upsert_pr_live(self._pr(), now=T0)
        self.store.set_rollup_etags(self.key, "sha1", 'W/"c1"', 'W/"s1"', now=T0 + 1)
        self.assertEqual(
            self.store.get_rollup_etags(self.key), ("sha1", 'W/"c1"', 'W/"s1"')
        )
        lower_key = self.key.lower()
        self.assertEqual(self.store.get_rollup_etags(lower_key)[1], 'W/"c1"')

        # Same head: rollup state kept and ETags kept by a refresh.
        self.store.upsert_pr_live(self._pr(), now=T0 + 2)
        self.assertEqual(
            self.store.get_rollup_etags(self.key), ("sha1", 'W/"c1"', 'W/"s1"')
        )

        self.store.upsert_pr_live(self._pr(head_sha="sha2"), now=T0 + 3)
        self.assertEqual(self.store.get_rollup_etags(self.key), ("sha2", None, None))

    def test_set_rollup_etags_new_head_clears_rollup_state(self) -> None:
        self.store.upsert_pr_live(self._pr(), now=T0)
        self.store.set_rollup_etags(self.key, "sha1", "c1", "s1", now=T0 + 1)
        row = self._view("SELECT * FROM v_pr_live WHERE item_key = ?", self.key)[0]
        self.assertEqual(row["rollup_state"], "PENDING")

        self.store.set_rollup_etags(self.key, "sha9", "c9", None, now=T0 + 2)
        stored = self._view("SELECT state, total, head_oid FROM pr_rollup")[0]
        self.assertEqual(stored, {"state": None, "total": None, "head_oid": "sha9"})

    # ----- per-PR child tables ----------------------------------------------------

    def test_replace_check_runs(self) -> None:
        rid = self.repo.repo_id
        self.store.upsert_pr_live(self._pr(), now=T0)
        runs = [
            self._check("lint", is_required=True),
            self._check("test", conclusion="FAILURE"),
        ]
        self.assertTrue(self.store.replace_check_runs(rid, 7, "sha1", runs, now=T0 + 1))
        self.assertEqual(self._kinds(), ["pr", "checks"])
        self.assertFalse(
            self.store.replace_check_runs(rid, 7, "sha1", runs, now=T0 + 2)
        )
        self.assertEqual(self._kinds(), ["pr", "checks"])

        rows = self._view("SELECT name, is_required FROM v_pr_checks ORDER BY name")
        self.assertEqual(
            rows,
            [{"name": "lint", "is_required": 1}, {"name": "test", "is_required": None}],
        )

        self.store.upsert_pr_live(self._pr(head_sha="sha2"), now=T0 + 3)
        self.assertEqual(self._view("SELECT * FROM v_pr_checks"), [])
        new_runs = [self._check("build")]
        self.assertTrue(
            self.store.replace_check_runs(rid, 7, "sha2", new_runs, now=T0 + 4)
        )
        stored = self._view("SELECT head_oid, name FROM check_runs")
        self.assertEqual(stored, [{"head_oid": "sha2", "name": "build"}])
        view = self._view("SELECT item_key, head_oid, name FROM v_pr_checks")
        self.assertEqual(
            view, [{"item_key": self.key, "head_oid": "sha2", "name": "build"}]
        )

    def test_v_pr_checks_hides_rows_for_other_heads(self) -> None:
        rid = self.repo.repo_id
        self.store.upsert_pr_live(self._pr(), now=T0)
        self.store.replace_check_runs(rid, 7, "sha0", [self._check("old")], now=T0 + 1)
        self.assertEqual(self._view("SELECT * FROM v_pr_checks"), [])
        self.assertEqual(len(self._view("SELECT * FROM check_runs")), 1)

    def test_replace_review_requests(self) -> None:
        rid = self.repo.repo_id
        reqs = [
            ReviewRequestRecord("bob", "User", "2027-01-15T07:30:00Z"),
            ReviewRequestRecord("org/team", "Team"),
        ]
        self.assertTrue(self.store.replace_review_requests(rid, 7, reqs, now=T0))
        self.assertFalse(
            self.store.replace_review_requests(rid, 7, list(reversed(reqs)), now=T0)
        )
        sql = (
            "SELECT item_key, reviewer, reviewer_kind, requested_at "
            "FROM v_pr_review_requests ORDER BY reviewer"
        )
        rows = self._view(sql)
        self.assertEqual(
            rows,
            [
                {
                    "item_key": self.key,
                    "reviewer": "bob",
                    "reviewer_kind": "User",
                    "requested_at": "2027-01-15T07:30:00Z",
                },
                {
                    "item_key": self.key,
                    "reviewer": "org/team",
                    "reviewer_kind": "Team",
                    "requested_at": None,
                },
            ],
        )

        re_requested = [
            ReviewRequestRecord("bob", "User", "2027-01-15T08:00:00Z"),
            ReviewRequestRecord("org/team", "Team"),
        ]
        self.assertTrue(
            self.store.replace_review_requests(rid, 7, re_requested, now=T0 + 1)
        )
        self.assertFalse(
            self.store.replace_review_requests(rid, 7, re_requested, now=T0 + 1)
        )
        self.assertEqual(self._view(sql)[0]["requested_at"], "2027-01-15T08:00:00Z")

        self.assertTrue(self.store.replace_review_requests(rid, 7, [], now=T0 + 2))
        self.assertEqual(self._view(sql), [])
        self.assertEqual(
            self._kinds(),
            ["review_requests", "review_requests", "review_requests"],
        )
        self.assertEqual(self.store.changes_since(0)[0].fields, ("review_requests",))

    def test_replace_latest_opinionated_reviews(self) -> None:
        rid = self.repo.repo_id
        reviews = [
            OpinionatedReviewRecord("bob", "COMMENTED", "User", "2027-01-15T08:00:00Z"),
            OpinionatedReviewRecord("bob", "APPROVED", "User", "2027-01-15T09:00:00Z"),
        ]
        self.assertTrue(
            self.store.replace_latest_opinionated_reviews(rid, 7, reviews, now=T0)
        )
        self.assertFalse(
            self.store.replace_latest_opinionated_reviews(rid, 7, reviews, now=T0)
        )
        rows = self._view(
            "SELECT item_key, author, state, submitted_at FROM v_pr_latest_reviews"
        )
        expected = {
            "item_key": self.key,
            "author": "bob",
            "state": "APPROVED",
            "submitted_at": "2027-01-15T09:00:00Z",
        }
        self.assertEqual(rows, [expected])
        self.assertEqual(self._kinds(), ["reviews"])

    def test_replace_latest_reviews(self) -> None:
        rid = self.repo.repo_id
        reviews = [
            LatestReviewRecord(
                "bob", "COMMENTED", "User", "2027-01-15T08:00:00Z", "sha0"
            ),
            LatestReviewRecord(
                "bob", "COMMENTED", "User", "2027-01-15T08:30:00Z", "sha1"
            ),
        ]
        self.assertTrue(self.store.replace_latest_reviews(rid, 7, reviews, now=T0))
        self.assertFalse(self.store.replace_latest_reviews(rid, 7, reviews, now=T0))
        rows = self._view(
            "SELECT item_key, repo_full_name, pr_number, author, author_type, state, "
            "submitted_at, commit_oid FROM v_pr_latest_author_reviews"
        )
        expected = {
            "item_key": self.key,
            "repo_full_name": self.repo.full_name,
            "pr_number": 7,
            "author": "bob",
            "author_type": "User",
            "state": "COMMENTED",
            "submitted_at": "2027-01-15T08:30:00Z",
            "commit_oid": "sha1",
        }
        self.assertEqual(rows, [expected])
        updated = [
            LatestReviewRecord(
                "bob", "APPROVED", "User", "2027-01-15T09:00:00Z", "sha2"
            )
        ]
        self.assertTrue(self.store.replace_latest_reviews(rid, 7, updated, now=T0 + 1))
        changes = self.store.changes_since(0)
        self.assertEqual([c.kind for c in changes], ["reviews", "reviews"])
        self.assertEqual(changes[-1].fields, ("latest_reviews",))

    def test_replace_thread_activity(self) -> None:
        rid = self.repo.repo_id
        threads = [
            ThreadActivityRecord(
                "T1",
                last_author="bob",
                last_at="2027-01-15T08:00:00Z",
                participants=("alice", "bob"),
            )
        ]
        self.assertTrue(self.store.replace_thread_activity(rid, 7, threads, now=T0))
        self.assertFalse(
            self.store.replace_thread_activity(rid, 7, threads, now=T0 + 1)
        )
        with_carol = [replace(threads[0], participants=("alice", "bob", "carol"))]
        self.assertTrue(
            self.store.replace_thread_activity(rid, 7, with_carol, now=T0 + 2)
        )
        self.assertFalse(
            self.store.replace_thread_activity(rid, 7, with_carol, now=T0 + 3)
        )
        resolved = [replace(with_carol[0], is_resolved=True)]
        self.assertTrue(self.store.upsert_thread_activity(rid, 7, resolved, now=T0 + 4))
        rows = self._view(
            "SELECT item_key, thread_id, is_resolved, is_outdated, participants_json "
            "FROM v_pr_threads"
        )
        expected = {
            "item_key": self.key,
            "thread_id": "T1",
            "is_resolved": 1,
            "is_outdated": 0,
            "participants_json": '["alice", "bob", "carol"]',
        }
        self.assertEqual(rows, [expected])
        self.assertEqual(self._kinds(), ["threads", "threads", "threads"])

    # ----- notifications and viewer sets ------------------------------------------

    def test_upsert_notifications_new_unchanged_updated(self) -> None:
        n1 = NotificationRecord(
            "N1", "review_requested", "PullRequest", self.key, "u1", True
        )
        n2 = NotificationRecord("N2", "subscribed", "Release", None, "u1", True)
        self.assertEqual(self.store.upsert_notifications([n1, n2], now=T0), [self.key])
        self.assertEqual(self.store.upsert_notifications([n1, n2], now=T0 + 1), [])
        self.assertEqual(
            self.store.upsert_notifications([replace(n1, unread=False)], T0 + 2),
            [self.key],
        )
        self.assertEqual(
            self.store.upsert_notifications(
                [replace(n1, unread=False, updated_at="u2")], T0 + 3
            ),
            [self.key],
        )
        self.assertEqual(self._kinds(), ["notification"] * 3)

        rows = self._view(
            "SELECT thread_id, unread, seen_at FROM v_notifications ORDER BY thread_id"
        )
        self.assertEqual(
            rows[0], {"thread_id": "N1", "unread": 0, "seen_at": iso_at(T0 + 3)}
        )
        self.assertEqual(
            rows[1], {"thread_id": "N2", "unread": 1, "seen_at": iso_at(T0 + 1)}
        )

    def test_replace_viewer_set(self) -> None:
        a, b, c = "o/r#1", "o/r#2", "o/r#3"
        self.assertEqual(
            self.store.replace_viewer_set("authored", [a, b], now=T0), ([a, b], [])
        )
        self.assertEqual(
            self.store.replace_viewer_set("authored", [b, c], now=T0 + 60), ([c], [a])
        )
        rows = self._view(
            "SELECT item_key, first_seen_at, last_seen_at FROM v_viewer_items "
            "WHERE set_name = 'authored' ORDER BY item_key"
        )
        self.assertEqual(
            rows,
            [
                {
                    "item_key": b,
                    "first_seen_at": iso_at(T0),
                    "last_seen_at": iso_at(T0 + 60),
                },
                {
                    "item_key": c,
                    "first_seen_at": iso_at(T0 + 60),
                    "last_seen_at": iso_at(T0 + 60),
                },
            ],
        )
        self.assertEqual(self.store.get_state("viewer_set:authored"), iso_at(T0 + 60))
        feed = self.store.changes_since(0)
        self.assertEqual(
            [(ch.item_key, ch.kind) for ch in feed][2:], [(c, "viewer"), (a, "viewer")]
        )
        self.assertEqual(feed[0].fields, ("authored",))
        self.assertEqual(
            self.store.replace_viewer_set("authored", [b, c], now=T0 + 90), ([], [])
        )

    # ----- watches and refresh requests -------------------------------------------

    def test_add_watch_canonicalizes_and_creates_items_repo(self) -> None:
        keys = self.store.add_watch(
            "monitor", "c1", ["octo-org/widgets#7", "new-org/thing#1"], 60, 600, now=T0
        )
        self.assertEqual(keys, [self.key, "new-org/thing#1"])
        new_repo = self.store.get_repo("new-org/thing")
        self.assertIsNotNone(new_repo)
        self.assertEqual(new_repo.mode, REPO_MODE_ITEMS)
        leases = self.store.active_watches(now=T0)
        self.assertEqual([w.item_key for w in leases], [self.key, "new-org/thing#1"])
        self.assertEqual(leases[0].freshness_s, 60)
        self.assertEqual(leases[0].lease_expires_at, iso_at(T0 + 600))

    def test_watch_renew_expire_and_prune(self) -> None:
        k1, k2 = self.key, "Octo-Org/Widgets#8"
        self.store.add_watch("monitor", "c1", [k1, k2], 60, 100, now=T0)
        self.store.add_watch("inflight", "x", [k1], 30, 50, now=T0)
        self.assertEqual(len(self.store.active_watches(now=T0 + 49)), 3)
        self.assertEqual(len(self.store.active_watches(now=T0 + 50)), 2)

        renewed = self.store.renew_watch(
            "monitor", "c1", 300, ["octo-org/widgets#8"], now=T0
        )
        self.assertEqual(renewed, 1)
        self.assertEqual(
            [w.item_key for w in self.store.active_watches(now=T0 + 200)], [k2]
        )
        self.assertEqual(self.store.renew_watch("monitor", "c1", 500, now=T0), 2)
        self.assertEqual(len(self.store.active_watches(now=T0 + 400)), 2)

        self.assertEqual(self.store.prune_expired_watches(now=T0 + 60), 1)
        self.assertEqual(self.store.prune_expired_watches(now=T0 + 60), 0)
        self.assertEqual(self.store.renew_watch("monitor", "c1", 500, [], now=T0), 0)

    def test_remove_watch_subset_and_all(self) -> None:
        k1, k2, k3 = self.key, "Octo-Org/Widgets#8", "Octo-Org/Widgets#9"
        self.store.add_watch("monitor", "c1", [k1, k2, k3], 60, 100, now=T0)
        self.store.add_watch("monitor", "c2", [k1], 60, 100, now=T0)
        self.assertEqual(
            self.store.remove_watch("monitor", "c1", ["OCTO-ORG/widgets#8"]), 1
        )
        self.assertEqual(self.store.remove_watch("monitor", "c1", []), 0)
        self.assertEqual(self.store.remove_watch("monitor", "c1"), 2)
        remaining = self.store.active_watches(now=T0)
        self.assertEqual([(w.consumer_id, w.item_key) for w in remaining], [("c2", k1)])

    def test_request_and_take_refresh(self) -> None:
        keys = self.store.request_refresh(
            ["octo-org/widgets#7", "unknown/repo#2"], now=T0 + 5
        )
        self.assertEqual(keys, [self.key, "unknown/repo#2"])
        unknown_repo = self.store.get_repo("unknown/repo")
        assert unknown_repo is not None
        self.assertEqual(unknown_repo.mode, "items")
        self.store.request_refresh(["Octo-Org/Widgets#8"], now=T0)
        self.assertEqual(
            self.store.peek_refresh_requests(),
            ["Octo-Org/Widgets#8", self.key, "unknown/repo#2"],
        )
        self.assertEqual(
            self.store.take_refresh_requests(),
            ["Octo-Org/Widgets#8", self.key, "unknown/repo#2"],
        )
        self.assertEqual(self.store.peek_refresh_requests(), [])
        self.assertEqual(self.store.take_refresh_requests(), [])

    # ----- change feed ------------------------------------------------------------

    def test_change_feed_limit_and_sequence_bounds(self) -> None:
        self.assertEqual(self.store.max_change_seq(), 0)
        self.assertEqual(self.store.min_change_seq(), 0)
        seqs = [
            self.store.append_change(self.key, "pr", ["title"], now=T0 + i)
            for i in range(5)
        ]
        self.assertEqual(seqs, [1, 2, 3, 4, 5])
        self.assertEqual([c.seq for c in self.store.changes_since(1, limit=2)], [2, 3])
        self.assertEqual([c.seq for c in self.store.changes_since(5)], [])
        self.assertEqual(self.store.min_change_seq(), 0)

        # The default 7-day window ends at T0 + 2, so seqs 1 and 2 are pruned.
        self.store.prune(now=T0 + 2 + 7 * 86400)
        self.assertEqual([c.seq for c in self.store.changes_since(0)], [3, 4, 5])
        self.assertEqual(self.store.min_change_seq(), 2)
        self.assertEqual(self.store.max_change_seq(), 5)

        self.store.prune(now=T0 + 100 * 86400)
        self.assertEqual(self.store.changes_since(0), [])
        self.assertEqual(self.store.max_change_seq(), 5)
        self.assertEqual(self.store.min_change_seq(), 5)
        self.assertEqual(self.store.append_change(self.key, "pr", [], now=T0), 6)

    # ----- api calls, heartbeat, service state ------------------------------------

    def test_record_api_call_and_service_health(self) -> None:
        empty = self._view("SELECT * FROM v_service_health")
        self.assertEqual(len(empty), 1)
        self.assertIsNone(empty[0]["pid"])
        self.assertEqual(empty[0]["active_leases"], 0)

        now = time.time()
        call = {"worker": "w", "cost": 1, "reset_at": 123, "status": 200, "now": now}
        self.store.record_api_call(resource="graphql", remaining=4000, **call)
        self.store.record_api_call(resource="graphql", remaining=3990, **call)
        self.store.record_api_call(resource="graphql", remaining=None, **call)
        self.store.record_api_call(resource="core", remaining=4500, **call)
        self.store.write_heartbeat(4242, "1.0", now=now - 30)
        self.store.add_watch("monitor", "c1", [self.key], 60, 600, now=now)
        self.store.add_watch("monitor", "c2", [self.key], 60, 10, now=now - 600)

        row = self._view("SELECT * FROM v_service_health")[0]
        self.assertEqual(row["graphql_remaining"], 3990)
        self.assertEqual(row["graphql_reset_at"], 123)
        self.assertEqual(row["core_remaining"], 4500)
        self.assertEqual(row["pid"], 4242)
        self.assertGreaterEqual(row["beat_age_s"], 29)
        self.assertLess(row["beat_age_s"], 120)
        self.assertEqual(row["active_leases"], 1)

    def test_heartbeat_started_at_per_pid(self) -> None:
        self.assertIsNone(self.store.read_heartbeat())
        self.store.write_heartbeat(100, "1.0", now=T0)
        self.store.write_heartbeat(100, "1.1", now=T0 + 10)
        beat = self.store.read_heartbeat()
        self.assertEqual(beat.pid, 100)
        self.assertEqual(beat.started_at, iso_at(T0))
        self.assertEqual(beat.beat_at, iso_at(T0 + 10))
        self.assertEqual(beat.version, "1.1")

        self.store.write_heartbeat(200, "1.1", now=T0 + 20)
        beat = self.store.read_heartbeat()
        self.assertEqual((beat.pid, beat.started_at), (200, iso_at(T0 + 20)))

    def test_get_and_set_state(self) -> None:
        self.assertIsNone(self.store.get_state("cursor"))
        self.store.set_state("cursor", "abc", now=T0)
        self.assertEqual(self.store.get_state("cursor"), "abc")
        self.store.set_state("cursor", "def", now=T0 + 1)
        self.assertEqual(self.store.get_state("cursor"), "def")
        self.store.set_state("cursor", None)
        self.assertIsNone(self.store.get_state("cursor"))
        count = self.store.conn.execute(
            "SELECT COUNT(*) FROM service_state"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    # ----- node ids ---------------------------------------------------------------

    def test_node_ids_before_and_after_pr_row(self) -> None:
        self.assertEqual(self.store.get_node_ids([self.key, "nope/repo#1"]), {})
        self.store.set_node_id("octo-org/widgets#7", "PR_early")
        self.store.set_node_id("nope/repo#1", "PR_unknown")
        lower = "octo-org/widgets#7"
        self.assertEqual(
            self.store.get_node_ids([lower, "nope/repo#1"]),
            {lower: "PR_early", "nope/repo#1": "PR_unknown"},
        )
        self.assertEqual(self.store.get_state(f"node_id:{self.key}"), "PR_early")

        self.store.upsert_pr_live(self._pr(node_id="PR_row"), now=T0)
        self.assertEqual(self.store.get_node_ids([self.key]), {self.key: "PR_row"})
        self.store.set_node_id(self.key, "PR_updated")
        self.assertEqual(self.store.get_node_ids([self.key]), {self.key: "PR_updated"})
        node = self.store.conn.execute("SELECT node_id FROM pull_requests").fetchone()[
            0
        ]
        self.assertEqual(node, "PR_updated")

    def test_upsert_item_comments_inserts_new_and_keeps_mirror_bodies(self) -> None:
        self.store.upsert_pr_live(self._pr(), now=T0)
        self.store.conn.execute(
            "INSERT INTO comments (repo_id, comment_id, parent_kind, parent_number, author, body,"
            " created_at) VALUES (?, 'IC_old', 'pr', 7, 'carol', 'mirrored body', ?)",
            (self.repo.repo_id, "2027-01-15T07:00:00Z"),
        )
        self.store.conn.commit()
        seq = self.store.max_change_seq()
        comments = [
            ItemCommentRecord("IC_old", "carol", "User", "2027-01-15T07:00:00Z"),
            ItemCommentRecord("IC_new", "ci-bot", "Bot", "2027-01-15T07:30:00Z"),
        ]
        self.assertTrue(
            self.store.upsert_item_comments(self.repo.repo_id, "pr", 7, comments)
        )
        self.assertEqual([c.kind for c in self.store.changes_since(seq)], ["comments"])
        self.assertFalse(
            self.store.upsert_item_comments(self.repo.repo_id, "pr", 7, comments)
        )
        rows = self._view(
            "SELECT comment_id, author_type FROM v_item_comments WHERE item_key = ? "
            "ORDER BY created_at",
            self.key,
        )
        self.assertEqual(
            [(r["comment_id"], r["author_type"]) for r in rows],
            [("IC_old", "User"), ("IC_new", "Bot")],
        )
        body = self.store.conn.execute(
            "SELECT body FROM comments WHERE comment_id = 'IC_old'"
        ).fetchone()[0]
        self.assertEqual(body, "mirrored body")

    def test_v_item_comments_keeps_ten_newest(self) -> None:
        self.store.upsert_pr_live(self._pr(), now=T0)
        comments = [
            ItemCommentRecord(
                f"IC_{i:02d}", "carol", "User", f"2027-01-15T07:{i:02d}:00Z"
            )
            for i in range(12)
        ]
        self.store.upsert_item_comments(self.repo.repo_id, "pr", 7, comments)
        rows = self._view("SELECT comment_id FROM v_item_comments ORDER BY created_at")
        self.assertEqual(
            [r["comment_id"] for r in rows], [f"IC_{i:02d}" for i in range(2, 12)]
        )

    def test_pr_body_change_detection(self) -> None:
        self.store.upsert_pr_live(self._pr(body="first"), now=T0)
        self.assertEqual(self.store.upsert_pr_live(self._pr(body="first"), now=T0), [])
        self.assertEqual(
            self.store.upsert_pr_live(self._pr(body="second"), now=T0), ["body"]
        )
        self.assertEqual(self.store.upsert_pr_live(self._pr(body=None), now=T0), [])
        body = self._view("SELECT body FROM v_pr_basic WHERE item_key = ?", self.key)[
            0
        ]["body"]
        self.assertEqual(body, "second")

    def test_same_named_check_runs_are_kept_separately(self) -> None:
        self.store.upsert_pr_live(self._pr(), now=T0)
        runs = [
            self._check("build", conclusion="SUCCESS", run_key="CheckRun:1"),
            self._check("build", conclusion="FAILURE", run_key="CheckRun:2"),
            self._check("lint", conclusion="SUCCESS"),
        ]
        self.assertTrue(
            self.store.replace_check_runs(self.repo.repo_id, 7, "sha1", runs, now=T0)
        )
        rows = self._view(
            "SELECT run_key, name, conclusion FROM v_pr_checks ORDER BY run_key"
        )
        self.assertEqual(
            [(r["run_key"], r["name"], r["conclusion"]) for r in rows],
            [
                ("CheckRun:1", "build", "SUCCESS"),
                ("CheckRun:2", "build", "FAILURE"),
                ("CheckRun:lint", "lint", "SUCCESS"),
            ],
        )
        self.assertFalse(
            self.store.replace_check_runs(self.repo.repo_id, 7, "sha1", runs, now=T0)
        )

    def test_register_repo_promotes_items_repo_with_different_case(self) -> None:
        # H1: the items repo was created in canonical case ("Octo-Org/Widgets").
        promoted = self.store.register_repo("octo-org", "widgets", local_path="/src/w")
        self.assertEqual(promoted.repo_id, self.repo.repo_id)
        self.assertEqual(promoted.mode, REPO_MODE_MIRROR)
        self.assertEqual(promoted.full_name, "Octo-Org/Widgets")
        self.assertEqual(promoted.local_path, "/src/w")
        self.assertEqual(self._count("repos"), 1)

    def test_get_live_fetched_at(self) -> None:
        self.assertEqual(self.store.get_live_fetched_at([self.key]), {})
        self.store.upsert_pr_live(self._pr(), now=T0)
        self.assertEqual(
            self.store.get_live_fetched_at([self.key, "x/y#1"]), {self.key: T0}
        )
        self.assertEqual(
            self.store.get_live_fetched_at(["octo-org/widgets#7"]), {self.key: T0}
        )

    def test_mirror_upsert_of_live_pr_appends_change_rows(self) -> None:
        # M2: the mirror writing first must not swallow the change for readers.
        self.store.upsert_pr_live(self._pr(), now=T0)
        seq = self.store.max_change_seq()
        mirror_comment = CommentRecord(
            repo_id=self.repo.repo_id,
            comment_id="IC_m",
            parent_kind="pr",
            parent_number=7,
            body="mirrored",
            author="carol",
            created_at="2027-01-15T07:00:00Z",
        )
        mirror_pr = PrRecord(
            repo_id=self.repo.repo_id,
            number=7,
            title="Renamed",
            state="MERGED",
            updated_at="2027-01-15T09:00:00Z",
            node_id="PR_node7",
            author="alice",
            head_sha="sha1",
            comments=(mirror_comment,),
        )
        self.store.upsert_pull_requests([mirror_pr])
        changes = self.store.changes_since(seq)
        self.assertEqual([c.kind for c in changes], ["pr"])
        self.assertTrue({"title", "state", "updated_at"} <= set(changes[0].fields))
        live = ItemCommentRecord("IC_m", "carol", "User", "2027-01-15T07:00:00Z")
        self.assertTrue(
            self.store.upsert_item_comments(self.repo.repo_id, "pr", 7, [live])
        )
        self.assertEqual(self.store.changes_since(changes[0].seq)[0].kind, "comments")

    def test_mirror_upsert_of_unwatched_pr_appends_nothing(self) -> None:
        pr = PrRecord(
            repo_id=self.repo.repo_id, number=8, title="t", state="OPEN", updated_at="x"
        )
        self.store.upsert_pull_requests([pr])
        self.store.upsert_pull_requests([replace(pr, title="t2")])
        self.assertEqual(self.store.changes_since(0), [])

    def test_idle_refresh_without_head_sha_reports_nothing(self) -> None:
        # L5
        self.store.upsert_pr_live(self._pr(head_sha=None), now=T0)
        self.assertEqual(self.store.upsert_pr_live(self._pr(head_sha=None), now=T0), [])

    def test_get_pr_head_sha(self) -> None:
        self.assertIsNone(self.store.get_pr_head_sha(self.key))
        self.assertIsNone(self.store.get_pr_head_sha("nope/repo#1"))
        self.store.upsert_pr_live(self._pr(head_sha="sha9"), now=T0)
        self.assertEqual(self.store.get_pr_head_sha("octo-org/widgets#7"), "sha9")
        self.store.upsert_pr_live(self._pr(head_sha=None), now=T0)
        self.assertIsNone(self.store.get_pr_head_sha(self.key))

    # ----- retention --------------------------------------------------------------

    def test_prune_keeps_recent_and_deletes_old_rows(self) -> None:
        old, recent = T0, T0 + 40 * 86400
        call = {
            "worker": "w",
            "resource": "core",
            "cost": 1,
            "remaining": 1,
            "reset_at": 1,
        }
        for moment in (old, recent):
            self.store.append_change(self.key, "pr", [], now=moment)
            self.store.record_api_call(status=200, now=moment, **call)
        self.store.upsert_notifications(
            [NotificationRecord("N-old", item_key=None)], now=old
        )
        self.store.upsert_notifications(
            [NotificationRecord("N-new", item_key=None)], now=recent
        )
        self.store.replace_viewer_set("stale", ["o/r#1"], now=old)
        self.store.replace_viewer_set("fresh", ["o/r#2"], now=recent)
        self.store.add_watch("monitor", "c1", [self.key], 60, 10, now=recent - 100)
        self.store.add_watch("monitor", "c2", [self.key], 60, 1000, now=recent)
        self.store.enqueue_timeline_dirty(
            [(self.repo.repo_id, 41)], reason="mention", now=old
        )
        self.store.enqueue_timeline_dirty(
            [(self.repo.repo_id, 42)], reason="mention", now=recent
        )

        deleted = self.store.prune(now=recent + 60)
        # Each viewer-set addition also appends a change row, so two changes are old.
        expected = {
            "changes": 2,
            "api_calls": 1,
            "notifications": 1,
            "viewer_sets": 1,
            "timeline_dirty": 1,
            "watches": 1,
            "items_repos": 0,
        }
        self.assertEqual(deleted, expected)
        self.assertEqual(
            {t: self._count(t) for t in expected if t != "items_repos"},
            {k: v for k, v in expected.items() if k != "items_repos"},
        )
        remaining = self._view("SELECT set_name FROM viewer_sets")
        self.assertEqual(remaining, [{"set_name": "fresh"}])

    def test_merge_repo_alias_items_repo_rewrites_keys_and_deletes_alias(self) -> None:
        alias_repo = self.store.ensure_items_repo("Old-Org", "Old-Widgets", now=T0)
        alias_key7 = "Old-Org/Old-Widgets#7"
        alias_key8 = "Old-Org/Old-Widgets#8"
        self.store.upsert_pr_live(self._pr(repo_id=alias_repo.repo_id), now=T0)
        self.store.upsert_chunks(
            [
                TextChunk(
                    chunk_id="Old-Org/Old-Widgets#pr:7",
                    repo_id=alias_repo.repo_id,
                    source_kind="pr",
                    source_ref=alias_key7,
                    title="PR 7",
                    text="widget focus",
                    content_sha256="sha7",
                )
            ],
            now=T0,
        )
        # Colliding watch on #7 plus a non-colliding watch on #8.
        self.store.add_watch(
            "monitor", "c1", [self.key, alias_key7, alias_key8], 60, 600, now=T0
        )
        self.store.upsert_notifications(
            [
                NotificationRecord(
                    "N-alias", "mention", "PullRequest", alias_key7, "u1", True
                )
            ],
            now=T0,
        )
        self.store.replace_viewer_set(
            "authored", [self.key, alias_key7, alias_key8], now=T0
        )
        self.store.request_refresh([self.key, alias_key7, alias_key8], now=T0)
        self.store.set_state("node_id:Old-Org/Old-Widgets#8", "PR_node8", now=T0)

        canonical = self.store.merge_repo_alias(
            alias_repo.repo_id, "octo-org/widgets", now=T0 + 10
        )
        self.assertEqual(canonical.repo_id, self.repo.repo_id)
        self.assertEqual(canonical.full_name, "Octo-Org/Widgets")
        self.assertIsNone(self.store.get_repo_by_id(alias_repo.repo_id))
        self.assertEqual(self._count("chunks_fts"), 0)
        self.assertEqual(self.store.get_repo("old-org/old-widgets"), self.repo)
        self.assertEqual(
            self.store.ensure_items_repo("old-org", "old-widgets").repo_id,
            self.repo.repo_id,
        )
        self.assertEqual(
            self.store.canonical_item_key("old-org/old-widgets#8"),
            "Octo-Org/Widgets#8",
        )
        self.assertEqual(
            [w.item_key for w in self.store.active_watches(now=T0)],
            ["Octo-Org/Widgets#7", "Octo-Org/Widgets#8"],
        )
        self.assertEqual(
            self.store.peek_refresh_requests(),
            ["Octo-Org/Widgets#7", "Octo-Org/Widgets#8"],
        )
        viewer_keys = [
            r["item_key"]
            for r in self._view("SELECT item_key FROM v_viewer_items ORDER BY item_key")
        ]
        self.assertEqual(viewer_keys, ["Octo-Org/Widgets#7", "Octo-Org/Widgets#8"])
        notif_keys = [
            r["item_key"] for r in self._view("SELECT item_key FROM v_notifications")
        ]
        self.assertEqual(notif_keys, ["Octo-Org/Widgets#7"])
        change_keys = {c.item_key for c in self.store.changes_since(0)}
        self.assertEqual(change_keys, {"Octo-Org/Widgets#7", "Octo-Org/Widgets#8"})
        self.assertEqual(self.store.get_state("node_id:Octo-Org/Widgets#8"), "PR_node8")

    def test_merge_repo_alias_mirror_repo_keeps_mirror_and_logs(self) -> None:
        mirror = self.store.register_repo("legacy-org", "widgets", now=T0)
        self.store.add_watch("monitor", "c1", ["legacy-org/widgets#7"], 60, 600, now=T0)
        with self.assertLogs("live_store", level="WARNING") as cm:
            canonical = self.store.merge_repo_alias(
                mirror.repo_id, "Octo-Org/Widgets", now=T0 + 5
            )
        self.assertEqual(canonical.repo_id, self.repo.repo_id)
        self.assertTrue(any("mirror mode" in msg for msg in cm.output))
        kept = self.store.get_repo_by_id(mirror.repo_id)
        self.assertIsNotNone(kept)
        self.assertEqual(kept.mode, REPO_MODE_MIRROR)
        self.assertEqual(
            self.store.canonical_item_key("legacy-org/widgets#7"), self.key
        )
        self.assertEqual(
            [w.item_key for w in self.store.active_watches(now=T0)], [self.key]
        )

    def test_prune_deletes_idle_items_repos_fts_and_cascaded_aliases(self) -> None:
        old = T0
        recent = T0 + 40 * 86400
        # Keep self.repo active via an unexpired watch.
        self.store.add_watch("monitor", "c0", [self.key], 60, 600, now=recent)

        idle = self.store.ensure_items_repo("idle-org", "gone", now=old)
        alias_src = self.store.ensure_items_repo("ancient-org", "gone", now=old)
        self.store.merge_repo_alias(alias_src.repo_id, "idle-org/gone", now=old)
        self.store.upsert_chunks(
            [
                TextChunk(
                    chunk_id="idle-org/gone#pr:1",
                    repo_id=idle.repo_id,
                    source_kind="pr",
                    source_ref="idle-org/gone#1",
                    title="Old PR",
                    text="obsolete",
                    content_sha256="sha-old",
                )
            ],
            now=old,
        )
        self.store.add_watch("monitor", "c-exp", ["idle-org/gone#1"], 60, 10, now=old)
        self.store.upsert_notifications(
            [
                NotificationRecord(
                    "N-idle", "mention", "PullRequest", "idle-org/gone#1", "u", True
                )
            ],
            now=old,
        )
        self.store.replace_viewer_set("old_set", ["idle-org/gone#1"], now=old)

        watched = self.store.ensure_items_repo("watched-org", "kept", now=old)
        self.store.add_watch(
            "monitor", "c-act", ["watched-org/kept#1"], 60, 600, now=recent
        )

        notified = self.store.ensure_items_repo("notified-org", "kept", now=old)
        self.store.upsert_notifications(
            [
                NotificationRecord(
                    "N-kept", "mention", "PullRequest", "notified-org/kept#1", "u", True
                )
            ],
            now=recent,
        )

        viewed = self.store.ensure_items_repo("viewed-org", "kept", now=old)
        self.store.replace_viewer_set("authored", ["viewed-org/kept#1"], now=recent)

        fresh = self.store.ensure_items_repo("fresh-org", "kept", now=recent)
        mirror = self.store.register_repo("mirror-org", "kept", now=old)

        deleted = self.store.prune(now=recent + 60)
        self.assertEqual(deleted["items_repos"], 1)
        self.assertIsNone(self.store.get_repo_by_id(idle.repo_id))
        self.assertIsNone(self.store.get_repo("ancient-org/gone"))
        self.assertEqual(self._count("repo_aliases"), 0)
        self.assertEqual(self._count("chunks_fts"), 0)
        for kept_repo in (watched, notified, viewed, fresh, mirror):
            self.assertIsNotNone(self.store.get_repo_by_id(kept_repo.repo_id))


class TestTimelineStore(unittest.TestCase):
    """Tests timeline event storage, candidate selection, the dirty queue, and derived links."""

    SYNCED = "2027-01-10T00:00:00Z"

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="packrat-timeline-"))
        self.store = PackratStore(self.tmp_dir / "repos.sqlite")
        self.repo = self.store.register_repo("octo-org", "widgets")
        self.other = self.store.register_repo("octo-org", "gadgets")
        self.rid = self.repo.repo_id

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _pr(self, number: int, **overrides: Any) -> PrRecord:
        base = PrRecord(
            repo_id=self.rid,
            number=number,
            title=f"PR {number}",
            state="OPEN",
            updated_at="2027-01-05T00:00:00Z",
            node_id=f"PR_{number}",
        )
        return replace(base, **overrides)

    def _issue(self, number: int, **overrides: Any) -> IssueRecord:
        base = IssueRecord(
            repo_id=self.rid,
            number=number,
            title=f"Issue {number}",
            state="OPEN",
            updated_at="2027-01-05T00:00:00Z",
            node_id=f"I_{number}",
        )
        return replace(base, **overrides)

    def _event(self, event_id: str, **overrides: Any) -> TimelineEventRecord:
        base = TimelineEventRecord(
            event_id=event_id,
            repo_id=self.rid,
            item_kind="issue",
            item_number=1,
            event_type="LabeledEvent",
            created_at="2027-01-06T00:00:00Z",
        )
        return replace(base, **overrides)

    def _links(self) -> list[tuple[int, int, str]]:
        rows = self.store.conn.execute(
            "SELECT issue_number, pr_number, link_type FROM issue_pr_links "
            "WHERE repo_id = ? ORDER BY issue_number, pr_number, link_type",
            (self.rid,),
        ).fetchall()
        return [(int(r[0]), int(r[1]), str(r[2])) for r in rows]

    def _set_synced(
        self, table: str, number: int, synced: str | None, xref: str | None
    ) -> None:
        with self.store.conn:
            self.store.conn.execute(
                f"UPDATE {table} SET timeline_synced_at = ?, xref_synced_at = ? "
                "WHERE repo_id = ? AND number = ?",
                (synced, xref, self.rid, number),
            )

    def _synced(self, table: str, number: int) -> tuple[str | None, str | None]:
        row = self.store.conn.execute(
            f"SELECT timeline_synced_at, xref_synced_at FROM {table} "
            "WHERE repo_id = ? AND number = ?",
            (self.rid, number),
        ).fetchone()
        return row[0], row[1]

    def _dirty_numbers(self) -> list[int]:
        rows = self.store.conn.execute(
            "SELECT item_number FROM timeline_dirty WHERE repo_id = ? ORDER BY item_number",
            (self.rid,),
        ).fetchall()
        return [int(r[0]) for r in rows]

    def test_sync_stats_with_timeline_serializes_to_json(self) -> None:
        stats = SyncStats(
            repo_full_name="octo-org/widgets",
            timeline=TimelineStats(items_synced=2, capped_items=["octo-org/widgets#1"]),
        )
        decoded = json.loads(json.dumps(asdict(stats)))
        self.assertEqual(decoded["timeline"]["capped_items"], ["octo-org/widgets#1"])
        self.assertEqual(decoded["timeline"]["graphql_cost"], 0)
        self.assertIsNone(asdict(SyncStats(repo_full_name="x/y"))["timeline"])
        self.assertEqual(set(TEXT_LINK_TYPES) & set(TIMELINE_LINK_TYPES), set())

    def test_resave_pr_keeps_timeline_links_and_replaces_text_links(self) -> None:
        text_link = IssuePrLink(self.rid, 1, 5, "closes")
        self.store.upsert_issues([self._issue(1), self._issue(2)])
        self.store.upsert_pull_requests([self._pr(5, issue_links=(text_link,))])
        self.store.upsert_timeline_events(
            [
                self._event(
                    "E1",
                    event_type="ClosedEvent",
                    subject="octo-org/widgets#5",
                    subject_type="PullRequest",
                ),
                self._event(
                    "E2",
                    item_number=2,
                    event_type="CrossReferencedEvent",
                    subject="octo-org/widgets#5",
                    subject_type="PullRequest",
                ),
            ],
            repo_full_name="octo-org/widgets",
        )
        self.assertEqual(
            self._links(), [(1, 5, "closed_by"), (1, 5, "closes"), (2, 5, "xref")]
        )

        new_link = IssuePrLink(self.rid, 3, 5, "references")
        self.store.upsert_pull_requests(
            [self._pr(5, updated_at="2027-01-06T00:00:00Z", issue_links=(new_link,))]
        )
        self.assertEqual(
            self._links(), [(1, 5, "closed_by"), (2, 5, "xref"), (3, 5, "references")]
        )

    def test_mirror_resave_with_none_author_type_keeps_existing(self) -> None:
        def review(author_type: str | None) -> ReviewRecord:
            return ReviewRecord(
                self.rid, "R_1", 5, "APPROVED", author="helper", author_type=author_type
            )

        def stored() -> str | None:
            return self.store.conn.execute(
                "SELECT author_type FROM reviews WHERE review_id = 'R_1'"
            ).fetchone()[0]

        self.store.upsert_pull_requests([self._pr(5, reviews=(review("Bot"),))])
        self.assertEqual(stored(), "Bot")
        self.store.upsert_pull_requests(
            [self._pr(5, updated_at="2027-01-06T00:00:00Z", reviews=(review(None),))]
        )
        self.assertEqual(stored(), "Bot")
        self.store.upsert_pull_requests(
            [self._pr(5, updated_at="2027-01-07T00:00:00Z", reviews=(review("User"),))]
        )
        self.assertEqual(stored(), "User")

    def test_timeline_candidates_per_mode_and_since_offset(self) -> None:
        self.store.upsert_pull_requests(
            [
                self._pr(1, state="MERGED", updated_at="2027-01-09T00:00:00Z"),
                self._pr(2, updated_at="2027-01-08T00:00:00Z"),
                self._pr(3, updated_at="2027-01-12T00:00:00Z"),
                self._pr(4, updated_at="2027-01-01T00:00:00Z"),
                self._pr(9, node_id=None),
            ]
        )
        self.store.upsert_issues(
            [
                self._issue(6, state="CLOSED", updated_at="2027-01-11T00:00:00Z"),
                self._issue(7, updated_at="2027-01-09T00:00:00Z"),
            ]
        )
        self._set_synced("pull_requests", 3, self.SYNCED, self.SYNCED)
        self._set_synced("pull_requests", 4, self.SYNCED, self.SYNCED)
        self._set_synced("issues", 6, self.SYNCED, None)

        def keys(mode: TimelineMode) -> list[tuple[str, int]]:
            targets = self.store.timeline_candidates(self.rid, mode, 50)
            return [(t.item_kind, t.item_number) for t in targets]

        # Open items first, then updated_at descending; no node_id, no candidate.
        self.assertEqual(
            keys(TimelineMode.BACKFILL), [("issue", 7), ("pr", 2), ("pr", 1)]
        )
        self.assertEqual(keys(TimelineMode.INCREMENTAL), [("pr", 3), ("issue", 6)])
        self.assertEqual(
            keys(TimelineMode.ALL),
            [("pr", 3), ("issue", 7), ("pr", 2), ("issue", 6), ("pr", 1)],
        )
        self.assertEqual(
            len(self.store.timeline_candidates(self.rid, TimelineMode.ALL, 2)), 2
        )

        incremental = self.store.timeline_candidates(
            self.rid, TimelineMode.INCREMENTAL, 1
        )
        self.assertEqual(
            incremental,
            [TimelineTarget(self.rid, "pr", 3, "PR_3", since="2027-01-09T23:55:00Z")],
        )
        backfill = self.store.timeline_candidates(self.rid, TimelineMode.BACKFILL, 1)
        self.assertIsNone(backfill[0].since)
        self.assertEqual(self.store.count_timeline_pending(self.rid), 3)
        self.assertEqual(self.store.count_timeline_pending(self.other.repo_id), 0)

    def test_get_item_updated_at(self) -> None:
        self.store.upsert_pull_requests(
            [self._pr(2, updated_at="2027-01-08T00:00:00Z")]
        )
        self.store.upsert_issues([self._issue(4, updated_at="2027-01-09T00:00:00Z")])
        self.assertEqual(
            self.store.get_item_updated_at(self.rid, "pr", 2), "2027-01-08T00:00:00Z"
        )
        self.assertEqual(
            self.store.get_item_updated_at(self.rid, "issue", 4), "2027-01-09T00:00:00Z"
        )
        self.assertIsNone(self.store.get_item_updated_at(self.rid, "issue", 2))
        self.assertIsNone(self.store.get_item_updated_at(self.other.repo_id, "pr", 2))
        with self.assertRaises(ValueError):
            self.store.get_item_updated_at(self.rid, "discussion", 2)

    def test_mark_timeline_synced_full_and_xref_only(self) -> None:
        self.store.upsert_pull_requests([self._pr(2)])
        self.store.mark_timeline_synced(
            self.rid, "pr", 2, synced_at=self.SYNCED, xref_only=False
        )
        self.assertEqual(self._synced("pull_requests", 2), (self.SYNCED, self.SYNCED))
        later = "2027-01-11T00:00:00Z"
        self.store.mark_timeline_synced(
            self.rid, "pr", 2, synced_at=later, xref_only=True
        )
        self.assertEqual(self._synced("pull_requests", 2), (self.SYNCED, later))
        with self.assertRaises(ValueError):
            self.store.mark_timeline_synced(
                self.rid, "discussion", 2, synced_at=later, xref_only=True
            )

    def test_dirty_queue_resolution(self) -> None:
        self.store.upsert_pull_requests([self._pr(2), self._pr(3)])
        self.store.upsert_issues([self._issue(4)])
        self._set_synced("pull_requests", 2, self.SYNCED, "2027-01-11T00:00:00Z")
        self._set_synced("issues", 4, self.SYNCED, None)
        # PR 3 has never been fetched; 99 isn't stored yet; 8 is the source itself.
        queued = self.store.enqueue_timeline_dirty(
            [(self.rid, n) for n in (4, 2, 3, 99, 8, 2)],
            reason="mention",
            now=T0,
            source=(self.rid, 8),
        )
        self.assertEqual(queued, 4)
        self.assertEqual(
            self.store.enqueue_timeline_dirty([(self.rid, 2)], reason="commit", now=T0),
            0,
        )

        targets = self.store.take_timeline_dirty(self.rid, 10)
        self.assertEqual(
            targets,
            [
                TimelineTarget(self.rid, "pr", 2, "PR_2", since="2027-01-10T23:55:00Z"),
                TimelineTarget(
                    self.rid, "issue", 4, "I_4", since="2027-01-09T23:55:00Z"
                ),
            ],
        )
        self.assertEqual(self._dirty_numbers(), [2, 4, 99])
        self.assertEqual(len(self.store.take_timeline_dirty(self.rid, 1)), 1)

        self.store.mark_timeline_synced(
            self.rid, "pr", 2, synced_at=iso_at(T0 + 10), xref_only=True
        )
        self.assertEqual(self._dirty_numbers(), [4, 99])

    def test_mark_synced_keeps_dirty_row_queued_after_run_start(self) -> None:
        self.store.upsert_issues([self._issue(4)])
        self._set_synced("issues", 4, self.SYNCED, self.SYNCED)
        self.store.enqueue_timeline_dirty(
            [(self.rid, 4)], reason="mention", now=T0 + 60
        )
        self.store.mark_timeline_synced(
            self.rid, "issue", 4, synced_at=iso_at(T0), xref_only=False
        )
        self.assertEqual(self._dirty_numbers(), [4])

    def test_derived_link_rules(self) -> None:
        events = [
            # Issue 1 closed by PR 5 in the same repo: closed_by.
            self._event(
                "C1",
                event_type="ClosedEvent",
                subject="Octo-Org/Widgets#5",
                subject_type="PullRequest",
            ),
            # Issue 2 closed by a commit: no link here.
            self._event(
                "C2",
                item_number=2,
                event_type="ClosedEvent",
                subject="abc123",
                subject_type="Commit",
            ),
            # PR 5's timeline shows issue 3 connected to it: xref.
            self._event(
                "X1",
                item_kind="pr",
                item_number=5,
                event_type="ConnectedEvent",
                subject="octo-org/widgets#3",
                subject_type="Issue",
            ),
            # PR-to-PR and issue-to-issue references: no link.
            self._event(
                "X2",
                item_kind="pr",
                item_number=5,
                event_type="CrossReferencedEvent",
                subject="octo-org/widgets#6",
                subject_type="PullRequest",
            ),
            self._event(
                "X3",
                event_type="CrossReferencedEvent",
                subject="octo-org/widgets#4",
                subject_type="Issue",
            ),
            # Cross-repo references: no link.
            self._event(
                "X4",
                event_type="CrossReferencedEvent",
                subject="octo-org/gadgets#5",
                subject_type="PullRequest",
            ),
            self._event(
                "C3",
                event_type="ClosedEvent",
                subject="octo-org/gadgets#9",
                subject_type="PullRequest",
            ),
            # Labels never link, even when named like an item key.
            self._event("L1", subject="octo-org/widgets#5", subject_type="Label"),
        ]
        written = self.store.upsert_timeline_events(
            events, repo_full_name="octo-org/widgets"
        )
        self.assertEqual(written, len(events))
        self.assertEqual(self._links(), [(1, 5, "closed_by"), (3, 5, "xref")])

        # Re-fetches replace by event_id and keep links unique.
        again = self.store.upsert_timeline_events(
            [replace(events[0], actor="alice")], repo_full_name="octo-org/widgets"
        )
        self.assertEqual(again, 1)
        self.assertEqual(
            self.store.table_counts(self.rid)["timeline_events"], len(events)
        )
        actor = self.store.conn.execute(
            "SELECT actor FROM timeline_events WHERE event_id = 'C1'"
        ).fetchone()[0]
        self.assertEqual(actor, "alice")
        self.assertEqual(self._links(), [(1, 5, "closed_by"), (3, 5, "xref")])
        none_written = self.store.upsert_timeline_events(
            [], repo_full_name="octo-org/widgets"
        )
        self.assertEqual(none_written, 0)

    def test_reset_backfill_state_clears_timeline_progress(self) -> None:
        self.store.upsert_pull_requests([self._pr(2)])
        self.store.upsert_issues([self._issue(4)])
        self._set_synced("pull_requests", 2, self.SYNCED, self.SYNCED)
        self._set_synced("issues", 4, self.SYNCED, self.SYNCED)
        self.store.reset_backfill_state(self.rid)
        self.assertEqual(self._synced("pull_requests", 2), (None, None))
        self.assertEqual(self._synced("issues", 4), (None, None))
        self.assertEqual(self.store.count_timeline_pending(self.rid), 2)

    def _subjects(self) -> dict[str, str]:
        rows = self.store.conn.execute(
            "SELECT event_id, subject FROM timeline_events ORDER BY event_id"
        ).fetchall()
        return {str(r[0]): str(r[1]) for r in rows}

    def _seed_subject_events(self) -> None:
        self.store.upsert_timeline_events(
            [
                self._event(
                    "A", subject="octo-org/widgets#5", subject_type="PullRequest"
                ),
                self._event("B", subject="Octo-Org/Widgets#6", subject_type="Issue"),
                self._event("C", subject="octo-org/widgets#1", subject_type="Label"),
                self._event(
                    "D", subject="octo-org/widgets-extra#2", subject_type="Issue"
                ),
            ],
            repo_full_name="octo-org/widgets",
        )
        self.store.upsert_timeline_events(
            [
                self._event(
                    "E",
                    repo_id=self.other.repo_id,
                    subject="octo-org/widgets#7",
                    subject_type="PullRequest",
                )
            ],
            repo_full_name="octo-org/gadgets",
        )

    def test_rename_repo_rewrites_item_subjects_only(self) -> None:
        self._seed_subject_events()
        self.store.rename_repo("octo-org/widgets", "octo-org/sprockets")
        self.assertEqual(
            self._subjects(),
            {
                "A": "octo-org/sprockets#5",
                "B": "octo-org/sprockets#6",
                "C": "octo-org/widgets#1",
                "D": "octo-org/widgets-extra#2",
                "E": "octo-org/sprockets#7",
            },
        )

    def test_merge_repo_alias_rewrites_item_subjects_only(self) -> None:
        self._seed_subject_events()
        # The alias is a mirror repo, so it is kept and the merge logs a warning.
        with self.assertLogs(level="WARNING"):
            self.store.merge_repo_alias(self.rid, "octo-org/sprockets")
        subjects = self._subjects()
        self.assertEqual(subjects["A"], "octo-org/sprockets#5")
        self.assertEqual(subjects["B"], "octo-org/sprockets#6")
        self.assertEqual(subjects["C"], "octo-org/widgets#1")
        self.assertEqual(subjects["D"], "octo-org/widgets-extra#2")
        self.assertEqual(subjects["E"], "octo-org/sprockets#7")

    def test_remove_repo_cascades_timeline_rows(self) -> None:
        self._seed_subject_events()
        self.store.enqueue_timeline_dirty([(self.rid, 5)], reason="mention")
        counts = self.store.table_counts(self.rid)
        self.assertEqual((counts["timeline_events"], counts["timeline_dirty"]), (4, 1))
        self.assertTrue(self.store.remove_repo("octo-org/widgets"))
        totals = self.store.table_counts()
        self.assertEqual((totals["timeline_events"], totals["timeline_dirty"]), (1, 0))


class TestWave2Track2AStoreEnhancements(unittest.TestCase):
    """Covers Wave 2 Track 2A identity single-writer, embedding preservation, and batching."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="packrat-w2-"))
        self.store = PackratStore(self.tmp_dir / "repos.sqlite")
        self.repo = self.store.register_repo("octo-org", "widgets", now=T0)
        self.other = self.store.register_repo("octo-org", "widgets-extra", now=T0)

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_busy_timeout_pragma_is_5000ms(self) -> None:
        timeout_ms = self.store.conn.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertEqual(timeout_ms, 5000)

    def test_rename_repo_records_alias_and_rewrites_live_state(self) -> None:
        key = "octo-org/widgets#7"
        other_key = "octo-org/widgets-extra#7"
        self.store.add_watch("monitor", "c1", [key, other_key], 60, 600, now=T0)
        self.store.replace_viewer_set("authored", [key, other_key], now=T0)
        self.store.request_refresh([key, other_key], now=T0)
        self.store.upsert_notifications(
            [
                NotificationRecord("N1", "mention", "PullRequest", key, "u1", True),
                NotificationRecord(
                    "N2", "mention", "PullRequest", other_key, "u2", True
                ),
            ],
            now=T0,
        )
        self.store.set_state(f"node_id:{key}", "PR_node7", now=T0)
        self.store.set_state(f"node_id:{other_key}", "PR_extra7", now=T0)

        renamed = self.store.rename_repo("octo-org/widgets", "new-org", "widgets")
        self.assertEqual(renamed.full_name, "new-org/widgets")
        # Alias table resolves old name to renamed repo
        self.assertEqual(self.store.get_repo("octo-org/widgets"), renamed)
        self.assertEqual(
            self.store.canonical_item_key("octo-org/widgets#7"), "new-org/widgets#7"
        )
        self.assertEqual(
            [w.item_key for w in self.store.active_watches(now=T0)],
            ["new-org/widgets#7", other_key],
        )
        self.assertEqual(
            self.store.peek_refresh_requests(), ["new-org/widgets#7", other_key]
        )
        notifs = {
            r["thread_id"]: r["item_key"]
            for r in self.store.conn.execute(
                "SELECT thread_id, item_key FROM notifications"
            ).fetchall()
        }
        self.assertEqual(notifs, {"N1": "new-org/widgets#7", "N2": other_key})
        changes = {c.item_key for c in self.store.changes_since(0)}
        self.assertEqual(changes, {"new-org/widgets#7", other_key})
        self.assertIsNone(self.store.get_state(f"node_id:{key}"))
        self.assertEqual(self.store.get_state("node_id:new-org/widgets#7"), "PR_node7")
        self.assertEqual(self.store.get_state(f"node_id:{other_key}"), "PR_extra7")

    def test_remove_repo_deletes_live_logs_and_node_id_state(self) -> None:
        key = "octo-org/widgets#7"
        other_key = "octo-org/widgets-extra#7"
        self.store.append_change(key, "pr", ["title"], now=T0)
        self.store.append_change(other_key, "pr", ["title"], now=T0)
        self.store.upsert_notifications(
            [
                NotificationRecord("N1", "mention", "PullRequest", key, "u1", True),
                NotificationRecord(
                    "N2", "mention", "PullRequest", other_key, "u2", True
                ),
            ],
            now=T0,
        )
        self.store.set_state(f"node_id:{key}", "PR_node7", now=T0)
        self.store.set_state(f"node_id:{other_key}", "PR_extra7", now=T0)

        self.assertTrue(self.store.remove_repo("octo-org/widgets"))
        self.assertEqual({c.item_key for c in self.store.changes_since(0)}, {other_key})
        notifs = [
            r["item_key"]
            for r in self.store.conn.execute(
                "SELECT item_key FROM notifications"
            ).fetchall()
        ]
        self.assertEqual(notifs, [other_key])
        self.assertIsNone(self.store.get_state(f"node_id:{key}"))
        self.assertEqual(self.store.get_state(f"node_id:{other_key}"), "PR_extra7")

    def test_mirror_pr_monotonic_guard_and_skeleton_promotion_and_noop_skip(
        self,
    ) -> None:
        live_pr = PrLiveRecord(
            repo_id=self.repo.repo_id,
            number=10,
            title="Live Title",
            state="OPEN",
            updated_at="2027-01-15T10:00:00Z",
            node_id="PR_10",
            author="alice",
            head_sha="sha-live",
            rollup_state="SUCCESS",
            rollup_total=4,
        )
        self.store.upsert_pr_live(live_pr, now=T0)
        seq_after_live = self.store.max_change_seq()

        # 1. Stale mirror write must not regress live PR row or append a change row.
        stale_mirror = PrRecord(
            repo_id=self.repo.repo_id,
            number=10,
            title="Stale Mirror Title",
            state="OPEN",
            updated_at="2027-01-15T09:00:00Z",
            node_id="PR_10",
            author="alice",
            head_sha="sha-old",
            body="Stale body",
            files=(PrFileRecord(self.repo.repo_id, 10, "stale.py", "MODIFIED", 1, 1),),
        )
        self.store.upsert_pull_requests([stale_mirror])
        self.assertEqual(self.store.max_change_seq(), seq_after_live)
        row = self.store.conn.execute(
            "SELECT title, head_sha, body, live_fetched_at FROM pull_requests "
            "WHERE repo_id = ? AND number = 10",
            (self.repo.repo_id,),
        ).fetchone()
        self.assertEqual(row["title"], "Live Title")
        self.assertEqual(row["head_sha"], "sha-live")
        self.assertEqual(row["body"], "")
        self.assertIsNotNone(row["live_fetched_at"])
        pr_files_count = self.store.conn.execute(
            "SELECT COUNT(*) FROM pr_files WHERE repo_id = ?",
            (self.repo.repo_id,),
        ).fetchone()[0]
        self.assertEqual(pr_files_count, 0)

        # 2. Promoting a live PR row with identical updated_at and head_sha writes body and child tables.
        promoting_mirror = PrRecord(
            repo_id=self.repo.repo_id,
            number=10,
            title="Live Title",
            state="OPEN",
            updated_at="2027-01-15T10:00:00Z",
            node_id="PR_10",
            author="alice",
            head_sha="sha-live",
            body="Full mirror body",
            files=(PrFileRecord(self.repo.repo_id, 10, "app.py", "MODIFIED", 5, 2),),
            reviews=(
                ReviewRecord(self.repo.repo_id, "R_10", 10, "APPROVED", author="bob"),
            ),
        )
        self.store.upsert_pull_requests([promoting_mirror])
        row = self.store.conn.execute(
            "SELECT body, live_fetched_at FROM pull_requests WHERE repo_id = ? AND number = 10",
            (self.repo.repo_id,),
        ).fetchone()
        self.assertEqual(row["body"], "Full mirror body")
        self.assertIsNone(row["live_fetched_at"])
        pr_files_count = self.store.conn.execute(
            "SELECT COUNT(*) FROM pr_files WHERE repo_id = ?",
            (self.repo.repo_id,),
        ).fetchone()[0]
        reviews_count = self.store.conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE repo_id = ?",
            (self.repo.repo_id,),
        ).fetchone()[0]
        self.assertEqual(pr_files_count, 1)
        self.assertEqual(reviews_count, 1)

        # 3. Re-upserting an unchanged mirror PR skips child table DELETE/INSERT statements.
        traced_sql: list[str] = []
        self.store.conn.set_trace_callback(traced_sql.append)
        try:
            self.store.upsert_pull_requests([promoting_mirror])
        finally:
            self.store.conn.set_trace_callback(None)
        self.assertFalse(
            any("DELETE FROM pr_files" in s for s in traced_sql),
            f"Expected no child table deletes on unchanged PR, got: {traced_sql}",
        )

    def test_replace_code_index_for_files_preserves_unchanged_symbol_embeddings(
        self,
    ) -> None:
        sym_keep = CodeSymbolRecord(
            repo_id=self.repo.repo_id,
            file_path="src/lib.py",
            symbol_name="keep_fn",
            kind="function",
            line_start=1,
            line_end=3,
            signature="def keep_fn():",
            sha="head1",
        )
        sym_drop = CodeSymbolRecord(
            repo_id=self.repo.repo_id,
            file_path="src/lib.py",
            symbol_name="drop_fn",
            kind="function",
            line_start=5,
            line_end=7,
            signature="def drop_fn():",
            sha="head1",
        )
        ch_keep = TextChunk(
            chunk_id="octo-org/widgets#code_symbol:src/lib.py:keep_fn:1",
            repo_id=self.repo.repo_id,
            source_kind="code_symbol",
            source_ref="octo-org/widgets:src/lib.py#L1-L3",
            file_path="src/lib.py",
            title="function keep_fn in src/lib.py",
            text="def keep_fn():\n    return 1",
            content_sha256="sha-keep",
        )
        ch_drop = TextChunk(
            chunk_id="octo-org/widgets#code_symbol:src/lib.py:drop_fn:5",
            repo_id=self.repo.repo_id,
            source_kind="code_symbol",
            source_ref="octo-org/widgets:src/lib.py#L5-L7",
            file_path="src/lib.py",
            title="function drop_fn in src/lib.py",
            text="def drop_fn():\n    return 2",
            content_sha256="sha-drop",
        )
        self.store.replace_code_index_for_files(
            self.repo.repo_id,
            changed_paths=["src/lib.py"],
            deleted_paths=[],
            symbols=[sym_keep, sym_drop],
            doc_sections=[],
            chunks=[ch_keep, ch_drop],
        )
        vec = tuple(1.0 / (EMBEDDING_DIM**0.5) for _ in range(EMBEDDING_DIM))
        self.store.upsert_embeddings(
            [
                StoredEmbeddingRecord(
                    chunk_id=ch_keep.chunk_id,
                    model_id="test-model",
                    content_sha256="sha-keep",
                    vector=vec,
                ),
                StoredEmbeddingRecord(
                    chunk_id=ch_drop.chunk_id,
                    model_id="test-model",
                    content_sha256="sha-drop",
                    vector=vec,
                ),
            ]
        )

        # Edit src/lib.py so keep_fn survives with identical chunk_id and drop_fn is removed.
        self.store.replace_code_index_for_files(
            self.repo.repo_id,
            changed_paths=["src/lib.py"],
            deleted_paths=[],
            symbols=[sym_keep],
            doc_sections=[],
            chunks=[ch_keep],
        )
        self.assertIsNotNone(self.store.get_embedding(ch_keep.chunk_id))
        self.assertIsNone(self.store.get_embedding(ch_drop.chunk_id))
        self.assertEqual(
            self.store.get_unembedded_chunks(self.repo.repo_id, "test-model"), []
        )
        self.store.conn.execute(
            "INSERT INTO chunks_fts(chunks_fts) VALUES('integrity-check')"
        )

    def test_upsert_chunks_batches_select_and_skips_unchanged_and_metadata_fts(
        self,
    ) -> None:
        c1 = TextChunk(
            chunk_id="octo-org/widgets#issue:1",
            repo_id=self.repo.repo_id,
            source_kind="issue",
            source_ref="octo-org/widgets#1",
            item_number=1,
            title="Issue 1",
            text="Alpha beta",
            content_sha256="sha-1",
            updated_at="2027-01-01T00:00:00Z",
        )
        self.store.upsert_chunks([c1], now=T0)
        seq1 = self.store.get_state("vector_cache_seq")

        # 1. Identical re-upsert performs no FTS writes and does not bump vector_cache_seq.
        traced: list[str] = []
        self.store.conn.set_trace_callback(traced.append)
        try:
            self.store.upsert_chunks([c1], now=T0 + 10)
        finally:
            self.store.conn.set_trace_callback(None)
        self.assertEqual(self.store.get_state("vector_cache_seq"), seq1)
        self.assertFalse(
            any("chunks_fts" in s or "UPDATE chunks" in s for s in traced),
            f"Expected zero writes for unchanged chunk, got: {traced}",
        )

        # 2. Metadata-only change (updated_at) updates chunks without touching chunks_fts or vector_cache_seq.
        c1_meta = replace(c1, updated_at="2027-01-02T00:00:00Z")
        traced.clear()
        self.store.conn.set_trace_callback(traced.append)
        try:
            self.store.upsert_chunks([c1_meta], now=T0 + 20)
        finally:
            self.store.conn.set_trace_callback(None)
        self.assertEqual(self.store.get_state("vector_cache_seq"), seq1)
        self.assertTrue(any("UPDATE chunks" in s for s in traced))
        self.assertFalse(any("chunks_fts" in s for s in traced))
        self.store.conn.execute(
            "INSERT INTO chunks_fts(chunks_fts) VALUES('integrity-check')"
        )

        # 3. Searchable text change updates chunks_fts, bumps vector_cache_seq, and passes integrity-check.
        c1_text = replace(c1_meta, text="Gamma delta", content_sha256="sha-2")
        self.store.upsert_chunks([c1_text], now=T0 + 30)
        self.assertNotEqual(self.store.get_state("vector_cache_seq"), seq1)
        self.store.conn.execute(
            "INSERT INTO chunks_fts(chunks_fts) VALUES('integrity-check')"
        )
        hits = self.store.query_fts_candidates("Gamma", SearchFilters(), 10)
        self.assertEqual([cid for cid, _ in hits], [c1.chunk_id])

    def test_vector_cache_watermark_ignores_items_repos_and_avoids_table_scans(
        self,
    ) -> None:
        wm_before = self.store.vector_cache_watermark()
        self.store.ensure_items_repo("hot-org", "ephemeral", now=T0)
        self.assertEqual(self.store.vector_cache_watermark(), wm_before)

        for sql in (
            "SELECT (SELECT MAX(rowid) FROM chunk_embeddings), (SELECT MIN(rowid) FROM chunk_embeddings)",
            "SELECT (SELECT MAX(rowid) FROM chunks), (SELECT MIN(rowid) FROM chunks)",
        ):
            plan = " ".join(
                str(r["detail"])
                for r in self.store.conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
            )
            self.assertNotIn("SCAN chunk_embeddings", plan)
            self.assertNotIn("SCAN chunks", plan)

    def test_prune_deletes_superseded_check_runs_and_live_queries_use_indexes(
        self,
    ) -> None:
        self.store.upsert_pr_live(
            PrLiveRecord(
                repo_id=self.repo.repo_id,
                number=7,
                title="PR 7",
                state="OPEN",
                updated_at="2027-01-15T08:00:00Z",
                node_id="PR_7",
                author="alice",
                head_sha="sha-old",
            ),
            now=T0,
        )
        self.store.replace_check_runs(
            self.repo.repo_id,
            7,
            "sha-old",
            [CheckRunRecord(name="ci", kind="CheckRun", status="COMPLETED")],
            now=T0,
        )
        # Advance PR head_sha and insert a stale check_runs row for a different head_oid
        self.store.conn.execute(
            "UPDATE pull_requests SET head_sha = 'sha-new' WHERE repo_id = ? AND number = 7",
            (self.repo.repo_id,),
        )
        self.store.conn.execute(
            "INSERT INTO check_runs (repo_id, pr_number, head_oid, run_key, name, kind) "
            "VALUES (?, 7, 'sha-new', 'CheckRun:ci', 'ci', 'CheckRun')",
            (self.repo.repo_id,),
        )
        self.store.conn.commit()
        self.store.prune(now=T0 + 60)
        heads = [
            r["head_oid"]
            for r in self.store.conn.execute(
                "SELECT head_oid FROM check_runs WHERE repo_id = ? AND pr_number = 7",
                (self.repo.repo_id,),
            ).fetchall()
        ]
        self.assertEqual(heads, ["sha-new"])

        # Empty take_refresh_requests and unchanged upsert_item_comments issue zero write statements.
        comments = [ItemCommentRecord("IC_1", "alice", "User", "2027-01-15T08:00:00Z")]
        self.assertFalse(
            self.store.upsert_item_comments(self.repo.repo_id, "pr", 7, [])
        )
        self.assertTrue(
            self.store.upsert_item_comments(self.repo.repo_id, "pr", 7, comments)
        )
        traced: list[str] = []
        self.store.conn.set_trace_callback(traced.append)
        try:
            self.assertEqual(self.store.take_refresh_requests(), [])
            self.assertFalse(
                self.store.upsert_item_comments(self.repo.repo_id, "pr", 7, comments)
            )
            self.assertEqual(self.store.upsert_notifications([]), [])
        finally:
            self.store.conn.set_trace_callback(None)
        self.assertFalse(
            any(
                s.lstrip().upper().startswith(("BEGIN", "INSERT", "UPDATE", "DELETE"))
                for s in traced
            ),
            f"Expected zero write transactions on no-op live calls, got: {traced}",
        )


_EXPORT_REPO_ID_TABLES: tuple[str, ...] = (
    "issues",
    "pull_requests",
    "pr_files",
    "reviews",
    "review_threads",
    "comments",
    "discussions",
    "issue_pr_links",
    "commits",
    "commit_files",
    "code_symbols",
    "doc_sections",
    "indexed_files",
    "chunks",
    "repo_aliases",
    "timeline_events",
    "timeline_dirty",
    "pr_rollup",
    "check_runs",
    "review_requests",
    "latest_opinionated_reviews",
    "latest_reviews",
    "thread_activity",
)

_EXPORT_LIVE_PR_TABLES: tuple[str, ...] = (
    "pr_rollup",
    "check_runs",
    "review_requests",
    "latest_opinionated_reviews",
    "latest_reviews",
    "thread_activity",
)

_EXPORT_PERSONAL_TABLES: tuple[str, ...] = (
    "notifications",
    "viewer_sets",
    "watches",
    "refresh_requests",
    "changes",
    "api_calls",
    "service_heartbeat",
)


def _expected_export_state_keys(kept_repo_ids: Sequence[int]) -> set[str]:
    """Returns the exact `service_state` key set expected on an exported snapshot."""
    keys = {"vector_cache_seq"}
    for rid in kept_repo_ids:
        keys.update(
            {
                f"signals_events:{rid}",
                f"events_last_id:{rid}",
                f"refetch_open_prs:{rid}",
                f"gap_cursor:{rid}:prs",
                f"gap_floor:{rid}:prs",
                f"gap_cursor:{rid}:issues",
                f"gap_floor:{rid}:issues",
            }
        )
    return keys


def _distinct_repo_ids_by_table(conn: sqlite3.Connection) -> dict[str, set[int]]:
    """Maps each `repo_id`-keyed child table to the distinct `repo_id` values stored."""
    return {
        tbl: {int(r[0]) for r in conn.execute(f"SELECT DISTINCT repo_id FROM {tbl}")}
        for tbl in _EXPORT_REPO_ID_TABLES
    }


def _personal_table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Returns row counts for every personal and runtime table."""
    return {
        tbl: int(conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0])
        for tbl in _EXPORT_PERSONAL_TABLES
    }


def _unit_vector_for_repo(repo_id: int) -> tuple[float, ...]:
    """Builds a deterministic L2-normalized 384-float test vector for `repo_id`."""
    coords = [0.25] + [0.0] * (EMBEDDING_DIM - 1)
    coords[repo_id % EMBEDDING_DIM] = 1.0
    return l2_normalize(coords)


class TestExportDatabase(unittest.TestCase):
    """Tests `PackratStore.export_database` scrubbing, compaction, and guardrails."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="packrat-export-"))
        self.db_path = self.tmp_dir / "repos.sqlite"
        self.store = PackratStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _seed_repo_mirror_artifacts(self, repo: RepoConfig, token: str) -> None:
        """Populates mirror, code-index, FTS5, embedding, timeline, and alias rows."""
        rid = repo.repo_id
        slug = repo.full_name
        self.store.upsert_issues(
            [
                IssueRecord(
                    repo_id=rid,
                    number=1,
                    node_id=f"I_{rid}_1",
                    title=f"Issue for {token}",
                    state="OPEN",
                    updated_at="2027-01-10T10:00:00Z",
                    author="alice",
                    body=f"Issue body mentioning {token}",
                )
            ]
        )
        self.store.upsert_pull_requests(
            [
                PrRecord(
                    repo_id=rid,
                    number=2,
                    node_id=f"PR_{rid}_2",
                    title=f"PR for {token}",
                    state="OPEN",
                    updated_at="2027-01-10T11:00:00Z",
                    author="bob",
                    head_sha=f"headsha{rid}",
                    body=f"Closes #1 for {token}",
                    files=(
                        PrFileRecord(
                            repo_id=rid,
                            pr_number=2,
                            file_path="src/mod.py",
                            additions=10,
                            deletions=2,
                        ),
                    ),
                    reviews=(
                        ReviewRecord(
                            repo_id=rid,
                            review_id=f"REV_{rid}_2",
                            pr_number=2,
                            state="APPROVED",
                            author="carol",
                            submitted_at="2027-01-10T11:30:00Z",
                        ),
                    ),
                    review_threads=(
                        ReviewThreadRecord(
                            thread_id=f"RT_{rid}_2",
                            repo_id=rid,
                            pr_number=2,
                            file_path="src/mod.py",
                            line=12,
                            reviewer="carol",
                            body="Looks solid.",
                        ),
                    ),
                    comments=(
                        CommentRecord(
                            repo_id=rid,
                            comment_id=f"CM_{rid}_2",
                            parent_kind="pr",
                            parent_number=2,
                            author="carol",
                            body="Ship it.",
                        ),
                    ),
                    issue_links=(
                        IssuePrLink(
                            repo_id=rid,
                            issue_number=1,
                            pr_number=2,
                            link_type="closes",
                        ),
                    ),
                )
            ]
        )
        self.store.upsert_discussions(
            [
                DiscussionRecord(
                    repo_id=rid,
                    number=3,
                    node_id=f"D_{rid}_3",
                    category="General",
                    author="alice",
                    title=f"Discussion for {token}",
                    body=f"Design notes for {token}",
                    updated_at="2027-01-10T12:00:00Z",
                )
            ]
        )
        self.store.upsert_timeline_events(
            [
                TimelineEventRecord(
                    event_id=f"TE_{rid}_1",
                    repo_id=rid,
                    item_kind="pr",
                    item_number=2,
                    event_type="ReadyForReviewEvent",
                    created_at="2027-01-10T11:15:00Z",
                    actor="bob",
                    actor_type="User",
                )
            ],
            repo_full_name=slug,
        )
        self.store.enqueue_timeline_dirty([(rid, 1)], reason="mention", now=T0)
        self.store.upsert_commits(
            [
                CommitRecord(
                    repo_id=rid,
                    sha=f"commitsha{rid}",
                    committed_at="2027-01-10T11:20:00Z",
                    title=f"Implement {token} (#2)",
                    author_name="Bob",
                    author_email="bob@example.com",
                    pr_number=2,
                    files=(
                        CommitFileRecord(
                            repo_id=rid,
                            sha=f"commitsha{rid}",
                            file_path="src/mod.py",
                            additions=10,
                            deletions=2,
                        ),
                    ),
                )
            ]
        )
        sym_title = f"function handle_{token}"
        sym_text = f"def handle_{token}(): return '{token} sharedexporttoken'"
        sym_sha = compute_content_sha256(sym_title, sym_text)
        sym_chunk = TextChunk(
            chunk_id=f"{slug}#code_symbol:src/mod.py:handle_{token}:1",
            repo_id=rid,
            source_kind="code_symbol",
            source_ref=f"{slug}:src/mod.py#L1-L4",
            file_path="src/mod.py",
            title=sym_title,
            text=sym_text,
            content_sha256=sym_sha,
            is_untrusted=False,
        )
        pr_title = f"PR for {token}"
        pr_text = f"Pull request summary for {token} sharedexporttoken"
        pr_sha = compute_content_sha256(pr_title, pr_text)
        pr_chunk = TextChunk(
            chunk_id=f"{slug}#pr:2",
            repo_id=rid,
            source_kind="pr",
            source_ref=f"{slug}#2",
            item_number=2,
            title=pr_title,
            text=pr_text,
            content_sha256=pr_sha,
            is_untrusted=True,
        )
        self.store.replace_code_index_for_files(
            repo_id=rid,
            changed_paths=["src/mod.py", "docs/guide.md"],
            deleted_paths=[],
            symbols=[
                CodeSymbolRecord(
                    repo_id=rid,
                    file_path="src/mod.py",
                    symbol_name=f"handle_{token}",
                    kind="function",
                    line_start=1,
                    line_end=4,
                    signature=f"def handle_{token}() -> str",
                    sha=f"commitsha{rid}",
                    blob_oid=f"blobsym{rid}",
                )
            ],
            doc_sections=[
                DocSectionRecord(
                    repo_id=rid,
                    file_path="docs/guide.md",
                    section_id=1,
                    level=1,
                    heading=f"Guide {token}",
                    breadcrumb=f"Guide {token}",
                    anchor_slug=f"guide-{token}",
                    line_start=1,
                    line_end=8,
                    subtree_line_end=8,
                    content=f"Documentation for {token}.",
                    sha=f"commitsha{rid}",
                    blob_oid=f"blobdoc{rid}",
                )
            ],
            chunks=[sym_chunk],
            now=T0,
        )
        self.store.upsert_chunks([pr_chunk], now=T0)
        self.store.upsert_indexed_files(
            rid,
            [
                ("src/mod.py", f"blobsym{rid}", f"commitsha{rid}"),
                ("docs/guide.md", f"blobdoc{rid}", f"commitsha{rid}"),
            ],
        )
        vec = _unit_vector_for_repo(rid)
        self.store.upsert_embeddings(
            [
                StoredEmbeddingRecord(
                    chunk_id=sym_chunk.chunk_id,
                    model_id="test-model",
                    content_sha256=sym_sha,
                    vector=vec,
                ),
                StoredEmbeddingRecord(
                    chunk_id=pr_chunk.chunk_id,
                    model_id="test-model",
                    content_sha256=pr_sha,
                    vector=vec,
                ),
            ],
            now=T0,
        )
        with self.store.conn:
            self.store.conn.execute(
                "INSERT INTO repo_aliases (alias_full_name, repo_id, created_at) "
                "VALUES (?, ?, ?)",
                (f"legacy-{slug}", rid, iso_at(T0)),
            )

    def _seed_repo_live_and_sync_state(self, repo: RepoConfig, token: str) -> None:
        """Populates live PR status tables and per-repository `service_state` keys."""
        rid = repo.repo_id
        self.store.upsert_pr_live(
            PrLiveRecord(
                repo_id=rid,
                number=2,
                title=f"PR for {token}",
                state="OPEN",
                updated_at="2027-01-10T11:45:00Z",
                node_id=f"PR_{rid}_2",
                author="bob",
                head_sha=f"headsha{rid}",
                rollup_state="SUCCESS",
                rollup_total=2,
            ),
            now=T0,
        )
        self.store.replace_check_runs(
            rid,
            2,
            f"headsha{rid}",
            [
                CheckRunRecord(
                    name="unit-tests",
                    kind="CheckRun",
                    status="COMPLETED",
                    conclusion="SUCCESS",
                    run_key=f"CheckRun:{rid}01",
                )
            ],
            now=T0,
        )
        self.store.replace_review_requests(
            rid,
            2,
            [
                ReviewRequestRecord(
                    reviewer="dave",
                    reviewer_kind="User",
                    requested_at="2027-01-10T11:40:00Z",
                )
            ],
            now=T0,
        )
        self.store.replace_latest_opinionated_reviews(
            rid,
            2,
            [
                OpinionatedReviewRecord(
                    author="carol",
                    state="APPROVED",
                    author_type="User",
                    submitted_at="2027-01-10T11:30:00Z",
                )
            ],
            now=T0,
        )
        self.store.replace_latest_reviews(
            rid,
            2,
            [
                LatestReviewRecord(
                    author="carol",
                    state="APPROVED",
                    author_type="User",
                    submitted_at="2027-01-10T11:30:00Z",
                    commit_oid=f"headsha{rid}",
                )
            ],
            now=T0,
        )
        self.store.replace_thread_activity(
            rid,
            2,
            [
                ThreadActivityRecord(
                    thread_id=f"RT_{rid}_2",
                    is_resolved=False,
                    is_outdated=False,
                    last_author="carol",
                    last_author_type="User",
                    last_at="2027-01-10T11:35:00Z",
                    participants=("bob", "carol"),
                )
            ],
            now=T0,
        )
        self.store.set_state(f"signals_events:{rid}", f'"etag-{rid}"', now=T0)
        self.store.set_state(f"events_last_id:{rid}", f"900{rid}", now=T0)
        self.store.set_state(f"refetch_open_prs:{rid}", "0", now=T0)
        self.store.set_gap_state(
            rid,
            "prs",
            cursor=f"GAP_CUR_PRS_{rid}",
            floor="2027-01-01T00:00:00Z",
            now=T0,
        )
        self.store.set_gap_state(
            rid,
            "issues",
            cursor=f"GAP_CUR_ISSUES_{rid}",
            floor="2027-01-02T00:00:00Z",
            now=T0,
        )

    def _seed_personal_and_session_state(self, slugs: Sequence[str]) -> None:
        """Populates all 7 personal/runtime tables and personal `service_state` keys."""
        self.store.upsert_notifications(
            [
                NotificationRecord(
                    thread_id=f"NT_{idx}",
                    reason="review_requested",
                    subject_type="PullRequest",
                    item_key=f"{slug}#2",
                    updated_at="2027-01-10T12:00:00Z",
                    unread=True,
                )
                for idx, slug in enumerate(slugs, start=1)
            ],
            now=T0,
        )
        self.store.replace_viewer_set(
            "authored", [f"{slug}#2" for slug in slugs], now=T0
        )
        self.store.replace_viewer_set(
            "review_requested", [f"{slug}#1" for slug in slugs], now=T0
        )
        self.store.add_watch(
            "monitor",
            "conv-1",
            [f"{slug}#2" for slug in slugs],
            freshness_s=30,
            ttl_s=600,
            now=T0,
        )
        self.store.request_refresh([f"{slug}#2" for slug in slugs], now=T0)
        self.store.append_change(f"{slugs[0]}#2", "pr", ["title"], now=T0)
        for idx in range(30):
            self.store.record_api_call(
                worker="hot",
                resource=f"graphql:pr_batch:{idx}:{'x' * 240}",
                cost=1,
                remaining=4999 - idx,
                reset_at=int(T0) + 3600,
                status=200,
                now=T0 + idx,
            )
        self.store.write_heartbeat(
            pid=42424,
            version="abc123",
            now=T0,
            last_ok_at=iso_at(T0),
            consecutive_failed_ticks=0,
        )
        self.store.set_state("viewer_login", "sender-octocat", now=T0)
        self.store.set_state(
            "notifications_last_modified",
            "Wed, 11 Nov 2026 20:00:00 GMT",
            now=T0,
        )
        for slug in slugs:
            self.store.set_state(f"node_id:{slug}#999", f"NODE_{slug}", now=T0)
        self.store.set_state("signals_events:9999", '"etag-orphan"', now=T0)
        self.store.set_state("gap_cursor:9999:prs", "ORPHAN_CUR", now=T0)
        self.store.set_state("ephemeral_session_token", "local-only", now=T0)

    def _seed_four_repo_fixture(
        self,
    ) -> tuple[RepoConfig, RepoConfig, RepoConfig, RepoConfig]:
        """Seeds 2 `mirror` repos, 2 `items` repos, child rows, and personal state."""
        m1 = self.store.register_repo(
            "octo-org", "widgets", local_path="/tmp/octo-widgets", now=T0
        )
        m2 = self.store.register_repo(
            "octo-org", "gadgets", local_path="/tmp/octo-gadgets", now=T0
        )
        i1 = self.store.ensure_items_repo("items-org", "alpha", now=T0)
        i2 = self.store.ensure_items_repo("items-org", "beta", now=T0)
        pairs = (
            (m1, "widgetsunique"),
            (m2, "gadgetsunique"),
            (i1, "alphaunique"),
            (i2, "betaunique"),
        )
        for repo, token in pairs:
            self._seed_repo_mirror_artifacts(repo, token)
            self._seed_repo_live_and_sync_state(repo, token)
        self._seed_personal_and_session_state([repo.full_name for repo, _ in pairs])
        return m1, m2, i1, i2

    def _assert_exported_sqlite_invariants(
        self,
        dest: Path,
        expected_repos: Sequence[RepoConfig],
    ) -> dict[str, str]:
        """Verifies raw SQLite file mode, compaction, cascades, and scrubbed tables."""
        for suffix in ("-wal", "-shm", "-journal", f".tmp.{os.getpid()}"):
            self.assertFalse(Path(f"{dest}{suffix}").exists())

        expected_ids = [r.repo_id for r in expected_repos]
        expected_id_set = set(expected_ids)
        expected_prefixes = tuple(f"{r.full_name}#" for r in expected_repos)

        with contextlib.closing(sqlite3.connect(str(dest))) as conn:
            journal_mode = str(
                conn.execute("PRAGMA journal_mode").fetchone()[0]
            ).lower()
            freelist_count = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
            self.assertEqual(journal_mode, "delete")
            self.assertEqual(freelist_count, 0)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            conn.execute(
                "INSERT INTO chunks_fts(chunks_fts, rank) VALUES('integrity-check', 1)"
            )

            repo_rows = conn.execute(
                "SELECT repo_id, full_name, mode, local_path FROM repos ORDER BY repo_id"
            ).fetchall()
            expected_repo_rows = [
                (r.repo_id, r.full_name, "mirror", None)
                for r in sorted(expected_repos, key=lambda item: item.repo_id)
            ]
            self.assertEqual(repo_rows, expected_repo_rows)

            changes_seq_rows = conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = 'changes'"
            ).fetchall()
            self.assertEqual(changes_seq_rows, [])

            self.assertEqual(
                _personal_table_counts(conn),
                {tbl: 0 for tbl in _EXPORT_PERSONAL_TABLES},
            )
            self.assertEqual(
                _distinct_repo_ids_by_table(conn),
                {tbl: expected_id_set for tbl in _EXPORT_REPO_ID_TABLES},
            )

            emb_ids = [
                str(r[0])
                for r in conn.execute(
                    "SELECT chunk_id FROM chunk_embeddings ORDER BY chunk_id"
                )
            ]
            self.assertEqual(len(emb_ids), 2 * len(expected_repos))
            self.assertTrue(all(cid.startswith(expected_prefixes) for cid in emb_ids))
            orphan_embs = int(
                conn.execute(
                    "SELECT COUNT(*) FROM chunk_embeddings "
                    "WHERE chunk_id NOT IN (SELECT chunk_id FROM chunks)"
                ).fetchone()[0]
            )
            self.assertEqual(orphan_embs, 0)

            state_map = {
                str(r[0]): str(r[1])
                for r in conn.execute("SELECT key, value FROM service_state")
            }
            self.assertEqual(set(state_map), _expected_export_state_keys(expected_ids))
            return state_map

    def test_full_export_scrubs_personal_and_items_state_and_compacts(self) -> None:
        """Verifies full export across 2 mirror and 2 items repos and compaction invariants."""
        m1, m2, i1, i2 = self._seed_four_repo_fixture()
        all_ids = {m1.repo_id, m2.repo_id, i1.repo_id, i2.repo_id}
        self.assertEqual(
            _distinct_repo_ids_by_table(self.store.conn),
            {tbl: all_ids for tbl in _EXPORT_REPO_ID_TABLES},
        )
        self.assertTrue(
            all(count > 0 for count in _personal_table_counts(self.store.conn).values())
        )
        self.assertGreater(self.store.max_change_seq(), 0)

        source_seq_before = self.store.get_state("vector_cache_seq")
        self.assertIsNotNone(source_seq_before)
        assert source_seq_before is not None
        self.store.wal_checkpoint_truncate()

        dest = self.tmp_dir / "exports" / "portable.sqlite"
        phases: list[str] = []
        summary = self.store.export_database(
            dest, on_progress=phases.append, now=T0 + 500
        )

        self.assertEqual(phases, ["backup", "scrub", "vacuum", "verify"])
        self.assertIsInstance(summary, ExportSummary)
        self.assertEqual(summary.source_db_path, str(self.db_path.resolve()))
        self.assertEqual(summary.dest_db_path, str(dest.resolve()))
        self.assertEqual(summary.schema_version, LATEST_SCHEMA_VERSION)
        self.assertEqual(
            summary.exported_repos, ("octo-org/widgets", "octo-org/gadgets")
        )
        self.assertEqual(summary.removed_items_repos, 2)
        self.assertEqual(summary.removed_mirror_repos, 0)
        self.assertEqual(summary.chunks_remaining, 4)
        self.assertEqual(summary.embeddings_remaining, 4)
        self.assertGreater(summary.dest_bytes, 0)
        self.assertGreater(summary.source_bytes, summary.dest_bytes)
        self.assertGreaterEqual(summary.duration_sec, 0.0)

        state_map = self._assert_exported_sqlite_invariants(dest, [m1, m2])
        self.assertEqual(state_map["vector_cache_seq"], str(int(source_seq_before) + 1))

        # Source database remains untouched after export.
        self.assertEqual(self.store.get_state("vector_cache_seq"), source_seq_before)
        self.assertEqual(self.store.get_state("viewer_login"), "sender-octocat")
        self.assertGreater(self.store.max_change_seq(), 0)
        widgets_src = self.store.get_repo("octo-org/widgets")
        assert widgets_src is not None
        self.assertEqual(widgets_src.local_path, "/tmp/octo-widgets")
        self.assertEqual(len(self.store.list_repos()), 4)

    def test_filtered_export_cascades_child_tables_and_preserves_fts_and_live_pr(
        self,
    ) -> None:
        """Verifies single-repo export cascades excluded repos and retains BM25 and live PR rows."""
        m1, _m2, _i1, _i2 = self._seed_four_repo_fixture()
        dest = self.tmp_dir / "widgets_only.sqlite"
        summary = self.store.export_database(dest, repo_ids=[m1.repo_id], now=T0 + 600)

        self.assertEqual(summary.exported_repos, ("octo-org/widgets",))
        self.assertEqual(summary.removed_items_repos, 2)
        self.assertEqual(summary.removed_mirror_repos, 1)
        self.assertEqual(summary.chunks_remaining, 2)
        self.assertEqual(summary.embeddings_remaining, 2)

        self._assert_exported_sqlite_invariants(dest, [m1])

        exported_store = PackratStore(dest)
        try:
            widgets_hits = [
                cid
                for cid, _ in exported_store.query_fts_candidates(
                    "widgetsunique", SearchFilters(), 10
                )
            ]
            shared_hits = [
                cid
                for cid, _ in exported_store.query_fts_candidates(
                    "sharedexporttoken", SearchFilters(), 10
                )
            ]
            self.assertEqual(len(widgets_hits), 2)
            self.assertEqual(set(widgets_hits), set(shared_hits))
            for excluded_token in ("gadgetsunique", "alphaunique", "betaunique"):
                self.assertEqual(
                    exported_store.query_fts_candidates(
                        excluded_token, SearchFilters(), 10
                    ),
                    [],
                )

            for live_tbl in _EXPORT_LIVE_PR_TABLES:
                cnt = int(
                    exported_store.conn.execute(
                        f"SELECT COUNT(*) FROM {live_tbl} WHERE repo_id = ?",
                        (m1.repo_id,),
                    ).fetchone()[0]
                )
                self.assertEqual(cnt, 1, f"Expected 1 row in {live_tbl}")

            live_prs = exported_store.conn.execute(
                "SELECT item_key, rollup_state FROM v_pr_live"
            ).fetchall()
            self.assertEqual(
                [(str(r["item_key"]), str(r["rollup_state"])) for r in live_prs],
                [("octo-org/widgets#2", "SUCCESS")],
            )
            live_checks = exported_store.conn.execute(
                "SELECT item_key, name, conclusion FROM v_pr_checks"
            ).fetchall()
            self.assertEqual(
                [
                    (str(r["item_key"]), str(r["name"]), str(r["conclusion"]))
                    for r in live_checks
                ],
                [("octo-org/widgets#2", "unit-tests", "SUCCESS")],
            )
        finally:
            exported_store.close()

    def test_export_when_zero_repositories_are_removed(self) -> None:
        """Verifies export when only `mirror` repos exist and zero repos are excluded."""
        m1 = self.store.register_repo(
            "octo-org", "widgets", local_path="/tmp/octo-widgets", now=T0
        )
        m2 = self.store.register_repo(
            "octo-org", "gadgets", local_path="/tmp/octo-gadgets", now=T0
        )
        self._seed_repo_mirror_artifacts(m1, "widgetsunique")
        self._seed_repo_live_and_sync_state(m1, "widgetsunique")
        self._seed_repo_mirror_artifacts(m2, "gadgetsunique")
        self._seed_repo_live_and_sync_state(m2, "gadgetsunique")
        self._seed_personal_and_session_state([m1.full_name, m2.full_name])

        dest = self.tmp_dir / "all_mirrors.sqlite"
        summary = self.store.export_database(dest, repo_ids=None, now=T0 + 100)
        self.assertEqual(
            summary.exported_repos, ("octo-org/widgets", "octo-org/gadgets")
        )
        self.assertEqual(summary.removed_items_repos, 0)
        self.assertEqual(summary.removed_mirror_repos, 0)
        self.assertEqual(summary.chunks_remaining, 4)
        self.assertEqual(summary.embeddings_remaining, 4)
        self._assert_exported_sqlite_invariants(dest, [m1, m2])

        reordered_dest = self.tmp_dir / "reordered_mirrors.sqlite"
        reordered = self.store.export_database(
            reordered_dest, repo_ids=[m2.repo_id, m1.repo_id], now=T0 + 110
        )
        self.assertEqual(
            reordered.exported_repos, ("octo-org/gadgets", "octo-org/widgets")
        )
        self.assertEqual(reordered.removed_mirror_repos, 0)

    def test_overwrite_unlinks_stale_wal_sidecars_without_replay_corruption(
        self,
    ) -> None:
        """Verifies `force=True` removes destination WAL, SHM, and journal sidecars."""
        m1 = self.store.register_repo(
            "octo-org", "widgets", local_path="/tmp/octo-widgets", now=T0
        )
        self._seed_repo_mirror_artifacts(m1, "widgetsunique")
        self._seed_repo_live_and_sync_state(m1, "widgetsunique")

        dest = self.tmp_dir / "existing_wal.sqlite"
        wal_path = Path(f"{dest}-wal")
        shm_path = Path(f"{dest}-shm")
        journal_path = Path(f"{dest}-journal")

        stale_conn = sqlite3.connect(str(dest))
        try:
            stale_conn.execute("PRAGMA journal_mode = WAL")
            stale_conn.execute("PRAGMA wal_autocheckpoint = 0")
            stale_conn.execute("CREATE TABLE stale_only (payload TEXT)")
            stale_conn.execute(
                "INSERT INTO stale_only (payload) VALUES ('uncheckpointed-wal-row')"
            )
            stale_conn.commit()
            base_bytes = dest.read_bytes()
            wal_bytes = wal_path.read_bytes()
            shm_bytes = shm_path.read_bytes()
        finally:
            stale_conn.close()

        # Restore uncheckpointed WAL and SHM bytes after close so sidecars persist on disk.
        dest.write_bytes(base_bytes)
        wal_path.write_bytes(wal_bytes)
        shm_path.write_bytes(shm_bytes)
        journal_path.write_bytes(b"stale-rollback-journal")
        self.assertGreater(wal_path.stat().st_size, 0)
        self.assertTrue(shm_path.exists())
        self.assertTrue(journal_path.exists())

        self.store.export_database(dest, force=True, now=T0 + 200)

        self.assertFalse(wal_path.exists())
        self.assertFalse(shm_path.exists())
        self.assertFalse(journal_path.exists())

        reopened = PackratStore(dest)
        try:
            integrity = str(
                reopened.conn.execute("PRAGMA integrity_check").fetchone()[0]
            )
            self.assertEqual(integrity, "ok")
            stale_tbl = reopened.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'stale_only'"
            ).fetchone()
            self.assertIsNone(stale_tbl)
            self.assertEqual(
                [r.full_name for r in reopened.list_repos()], ["octo-org/widgets"]
            )
            hits = reopened.query_fts_candidates("widgetsunique", SearchFilters(), 10)
            self.assertEqual(len(hits), 2)
        finally:
            reopened.close()

    def test_compact_and_verify_raises_export_integrity_error_on_fts_or_fk_drift(
        self,
    ) -> None:
        """Verifies `_compact_and_verify_export_conn` and cleanup on FTS5 or FK drift."""
        corrupt_fts_path = self.tmp_dir / "corrupt_fts.sqlite"
        fts_store = PackratStore(corrupt_fts_path)
        try:
            repo = fts_store.register_repo("octo-org", "widgets", now=T0)
            sha = compute_content_sha256("Title", "Orphaned FTS token")
            fts_store.upsert_chunks(
                [
                    TextChunk(
                        chunk_id="octo-org/widgets#issue:1",
                        repo_id=repo.repo_id,
                        source_kind="issue",
                        source_ref="octo-org/widgets#1",
                        item_number=1,
                        title="Title",
                        text="Orphaned FTS token",
                        content_sha256=sha,
                    )
                ],
                now=T0,
            )
            with fts_store.conn:
                fts_store.conn.execute(
                    "DELETE FROM chunks WHERE chunk_id = 'octo-org/widgets#issue:1'"
                )
        finally:
            fts_store.close()

        with (
            contextlib.closing(sqlite3.connect(str(corrupt_fts_path))) as raw_conn,
            self.assertRaises(ExportIntegrityError),
        ):
            _compact_and_verify_export_conn(raw_conn)

        # Failed export_database cleans up temporary files and leaves existing dest intact.
        m1 = self.store.register_repo("octo-org", "widgets", now=T0)
        self._seed_repo_mirror_artifacts(m1, "widgetsunique")
        with self.store.conn:
            self.store.conn.execute("DELETE FROM chunks")
        dest = self.tmp_dir / "protected_dest.sqlite"
        dest.write_bytes(b"preserve-existing-bytes")
        with self.assertRaises(ExportIntegrityError):
            self.store.export_database(dest, force=True)
        self.assertEqual(dest.read_bytes(), b"preserve-existing-bytes")
        self.assertEqual(list(self.tmp_dir.glob("*.tmp.*")), [])

        # Foreign-key violation also raises ExportIntegrityError.
        corrupt_fk_path = self.tmp_dir / "corrupt_fk.sqlite"
        fk_store = PackratStore(corrupt_fk_path)
        fk_store.close()
        with contextlib.closing(sqlite3.connect(str(corrupt_fk_path))) as fk_conn:
            fk_conn.execute("PRAGMA foreign_keys = OFF")
            with fk_conn:
                fk_conn.execute(
                    "INSERT INTO issues (repo_id, number, state, title, updated_at) "
                    "VALUES (99999, 1, 'OPEN', 'Orphan issue', '2027-01-01T00:00:00Z')"
                )
            with self.assertRaises(ExportIntegrityError):
                _compact_and_verify_export_conn(fk_conn)

    def test_destination_path_guards(self) -> None:
        """Verifies destination overwrite, directory, memory, and source-alias guards."""
        self.store.register_repo("octo-org", "widgets", now=T0)

        existing = self.tmp_dir / "already_exists.sqlite"
        existing.write_bytes(b"original-content")
        with self.assertRaises(ValueError):
            self.store.export_database(existing, force=False)
        self.assertEqual(existing.read_bytes(), b"original-content")

        dangling_link = self.tmp_dir / "dangling_dest.sqlite"
        dangling_link.symlink_to(self.tmp_dir / "nonexistent_target.sqlite")
        with self.assertRaises(ValueError):
            self.store.export_database(dangling_link, force=False)

        existing_dir = self.tmp_dir / "subdir"
        existing_dir.mkdir()
        with self.assertRaises(ValueError):
            self.store.export_database(existing_dir, force=False)
        with self.assertRaises(ValueError):
            self.store.export_database(existing_dir, force=True)

        symlink_to_src = self.tmp_dir / "src_symlink.sqlite"
        symlink_to_src.symlink_to(self.db_path)
        hardlink_to_src = self.tmp_dir / "src_hardlink.sqlite"
        os.link(self.db_path, hardlink_to_src)

        forbidden_destinations: tuple[Path | str, ...] = (
            self.db_path,
            self.tmp_dir / "." / "repos.sqlite",
            symlink_to_src,
            hardlink_to_src,
            Path(f"{self.db_path}-wal"),
            Path(f"{self.db_path}-shm"),
            Path(f"{self.db_path}-journal"),
            ":memory:",
            "",
            "   ",
            "file:memexport?mode=memory",
        )
        for bad_dest in forbidden_destinations:
            with self.assertRaises(
                ValueError, msg=f"Expected rejection for {bad_dest!r}"
            ):
                self.store.export_database(bad_dest, force=True)

    def test_repo_selection_guards_and_memory_source_export(self) -> None:
        """Verifies `repo_ids` validation and exporting from a `:memory:` source store."""
        dest = self.tmp_dir / "never_created.sqlite"
        with self.assertRaises(ValueError):
            self.store.export_database(dest, repo_ids=None)

        items_repo = self.store.ensure_items_repo("items-org", "alpha", now=T0)
        with self.assertRaises(ValueError):
            self.store.export_database(dest, repo_ids=None)

        mirror_repo = self.store.register_repo("octo-org", "widgets", now=T0)
        invalid_selections: tuple[Sequence[int], ...] = (
            [],
            [999999],
            [items_repo.repo_id],
            [mirror_repo.repo_id, items_repo.repo_id],
        )
        for selection in invalid_selections:
            with self.assertRaises(
                ValueError, msg=f"Expected rejection for {selection!r}"
            ):
                self.store.export_database(dest, repo_ids=selection)
        self.assertFalse(dest.exists())

        mem_store = PackratStore(":memory:")
        try:
            mem_store.register_repo("octo-org", "widgets", now=T0)
            self.assertIsNone(mem_store.get_state("vector_cache_seq"))
            mem_dest = self.tmp_dir / "from_memory.sqlite"
            summary = mem_store.export_database(mem_dest, now=T0 + 300)
            self.assertEqual(summary.source_db_path, ":memory:")
            self.assertEqual(summary.source_bytes, 0)
            self.assertGreater(summary.dest_bytes, 0)
        finally:
            mem_store.close()

        with contextlib.closing(sqlite3.connect(str(mem_dest))) as conn:
            seq = conn.execute(
                "SELECT value FROM service_state WHERE key = 'vector_cache_seq'"
            ).fetchone()[0]
            self.assertEqual(seq, "1")

    def test_export_reports_live_progress_and_resists_concurrent_wal_writes(
        self,
    ) -> None:
        """Verifies live reporter output across all 4 phases and WAL snapshot pinning."""
        import io
        from unittest import mock

        from progress import StderrProgressReporter

        self._seed_four_repo_fixture()
        self.store.wal_checkpoint_truncate()

        buf = io.StringIO()
        reporter = StderrProgressReporter(
            "plain",
            stream=buf,
            is_tty=False,
            grace_sec=0.0,
            plain_interval_sec=0.0,
        )
        writer = PackratStore(self.db_path)
        try:
            dest = self.tmp_dir / "progress_export.sqlite"
            phases: list[str] = []

            def inject_concurrent_wal_write(phase: str) -> None:
                phases.append(phase)
                if phase == "backup":
                    writer.set_state("concurrent_during_backup", "yes", now=T0 + 50)

            with (
                mock.patch("db_store._EXPORT_BACKUP_STEP_PAGES", 4),
                mock.patch("db_store._EXPORT_VM_PROGRESS_OPS", 50),
            ):
                self.store.export_database(
                    dest,
                    on_progress=inject_concurrent_wal_write,
                    reporter=reporter,
                    now=T0 + 400,
                )
        finally:
            writer.close()

        self.assertEqual(phases, ["backup", "scrub", "vacuum", "verify"])
        progress_text = buf.getvalue()
        self.assertIn("[export] backup:", progress_text)
        self.assertIn("[export] backup done:", progress_text)
        self.assertIn("[export] scrub:", progress_text)
        self.assertIn("[export] scrub done:", progress_text)
        self.assertIn("[export] vacuum: compacting", progress_text)
        self.assertIn("[export] vacuum done:", progress_text)
        self.assertIn("[export] verify: step 1/2, checking FTS5 index", progress_text)
        self.assertIn("[export] verify: step 2/2, checking foreign keys", progress_text)
        self.assertIn("[export] verify done:", progress_text)


if __name__ == "__main__":
    unittest.main()
