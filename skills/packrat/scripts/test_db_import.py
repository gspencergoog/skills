"""Store-level tests for `PackratStore.import_database`, fixups, and preflight guards."""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import db_import
import db_store
from db_import import (
    IMPORT_SEC_PER_GIB,
    LONG_COPY_WARN_SEC,
    MIN_SELECTED_SHARE,
    ImportAbortedError,
    ImportIntegrityError,
    is_sqlite_path_alias,
    mirror_freshness_key,
    preview_import_database,
    selected_bytes_estimate,
    validate_import_preflight,
)
from db_migrations import (
    CONNECTION_PRAGMAS,
    LATEST_SCHEMA_VERSION,
    MIGRATIONS,
    MIN_IMPORT_SCHEMA_VERSION,
    SchemaTooNewError,
    _run_migration,
    read_schema_version,
)
from db_schema import (
    GIT_SOURCE_KINDS,
    PERSONAL_TABLES,
    REPO_GIT_TABLES,
    REPO_GITHUB_TABLES,
)
from db_store import PackratStore
from models import (
    EMBEDDING_DIM,
    CheckRunRecord,
    CodeSymbolRecord,
    CommentRecord,
    CommitFileRecord,
    CommitRecord,
    DiscussionRecord,
    DocSectionRecord,
    FreshnessKey,
    IssuePrLink,
    IssueRecord,
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
    StoredEmbeddingRecord,
    TextChunk,
    ThreadActivityRecord,
    TimelineEventRecord,
    compute_content_sha256,
    iso_at,
    l2_normalize,
    quantize_u8_vector,
)
from progress import StderrProgressReporter
from search_engine import hybrid_rrf_search

T0 = 1_800_000_000.0
"""Fixed epoch seconds used for deterministic timestamps across import tests."""


def _token_vector(token: str) -> tuple[float, ...]:
    """Builds a deterministic L2-normalized 384-float vector keyed by `token`."""
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    primary = int.from_bytes(digest[:2], "little") % EMBEDDING_DIM
    secondary = (primary + 1 + digest[2]) % EMBEDDING_DIM
    coords = [0.02] * EMBEDDING_DIM
    coords[primary] = 1.0
    coords[secondary] = 0.5
    return l2_normalize(coords)


class _TokenEmbedder:
    """Deterministic stub embedder mapping query tokens to `_token_vector`."""

    model_id = "test-model"

    def embed_query(self, query: str) -> tuple[float, ...]:
        """Returns the unit vector for `query`."""
        return _token_vector(query.strip())


def _seed_repo_github_rows(
    store: PackratStore, repo: RepoConfig, token: str, updated_at: str
) -> list[TextChunk]:
    """Populates GitHub mirror tables and returns a `#`-prefixed PR chunk."""
    rid = repo.repo_id
    slug = repo.full_name
    store.upsert_issues(
        [
            IssueRecord(
                repo_id=rid,
                number=1,
                node_id=f"I_{token}_1",
                title=f"Issue for {token}",
                state="OPEN",
                updated_at=updated_at,
                author="alice",
                body=f"Issue body mentioning {token}",
            )
        ]
    )
    store.upsert_pull_requests(
        [
            PrRecord(
                repo_id=rid,
                number=2,
                node_id=f"PR_{token}_2",
                title=f"PR for {token}",
                state="OPEN",
                updated_at=updated_at,
                author="bob",
                head_sha=f"headsha_{token}",
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
                        review_id=f"REV_{token}_2",
                        pr_number=2,
                        state="APPROVED",
                        author="carol",
                        submitted_at=updated_at,
                    ),
                ),
                review_threads=(
                    ReviewThreadRecord(
                        thread_id=f"RT_{token}_2",
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
                        comment_id=f"CM_{token}_2",
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
            ),
            PrRecord(
                repo_id=rid,
                number=4,
                node_id=f"PR_{token}_4",
                title=f"Follow-up PR for {token}",
                state="OPEN",
                updated_at=updated_at,
                author="carol",
                head_sha=f"headsha4_{token}",
                body=f"Follow-up work for {token}",
            ),
        ]
    )
    store.upsert_discussions(
        [
            DiscussionRecord(
                repo_id=rid,
                number=3,
                node_id=f"D_{token}_3",
                category="General",
                author="alice",
                title=f"Discussion for {token}",
                body=f"Design notes for {token}",
                updated_at=updated_at,
            )
        ]
    )
    store.upsert_timeline_events(
        [
            TimelineEventRecord(
                event_id=f"TE_{token}_1",
                repo_id=rid,
                item_kind="pr",
                item_number=2,
                event_type="CrossReferencedEvent",
                created_at=updated_at,
                actor="bob",
                actor_type="User",
                subject=f"{slug}#1",
                subject_type="Issue",
            ),
            TimelineEventRecord(
                event_id=f"TE_{token}_2",
                repo_id=rid,
                item_kind="pr",
                item_number=2,
                event_type="LabeledEvent",
                created_at=updated_at,
                actor="bob",
                actor_type="User",
                subject=f"{slug}#keep-label",
                subject_type="Label",
            ),
        ],
        repo_full_name=slug,
    )
    store.enqueue_timeline_dirty([(rid, 1)], reason="mention", now=T0)
    pr_title = f"PR for {token}"
    pr_text = f"Pull request summary for {token} sharedimporttoken"
    pr_chunk = TextChunk(
        chunk_id=f"{slug}#pr:2",
        repo_id=rid,
        source_kind="pr",
        source_ref=f"{slug}#2",
        item_number=2,
        title=pr_title,
        text=pr_text,
        content_sha256=compute_content_sha256(pr_title, pr_text),
        is_untrusted=True,
    )
    store.upsert_chunks([pr_chunk], now=T0)
    return [pr_chunk]


def _seed_repo_git_rows(
    store: PackratStore, repo: RepoConfig, token: str, committed_at: str
) -> list[TextChunk]:
    """Populates all 5 `REPO_GIT_TABLES` and `@`/`:` git chunks for `repo`."""
    rid = repo.repo_id
    slug = repo.full_name
    sha = f"commitsha_{token}"
    store.upsert_commits(
        [
            CommitRecord(
                repo_id=rid,
                sha=sha,
                committed_at=committed_at,
                title=f"Implement {token} (#2)",
                author_name="Bob",
                author_email="bob@example.com",
                pr_number=2,
                files=(
                    CommitFileRecord(
                        repo_id=rid,
                        sha=sha,
                        file_path="src/mod.py",
                        additions=10,
                        deletions=2,
                    ),
                ),
            )
        ]
    )
    sym_title = f"function handle_{token}"
    sym_text = f"def handle_{token}(): return '{token} sharedimporttoken'"
    sym_chunk = TextChunk(
        chunk_id=f"{slug}@symbol:src/mod.py:handle_{token}:1",
        repo_id=rid,
        source_kind="code_symbol",
        source_ref=f"{slug}@{sha}",
        file_path="src/mod.py",
        title=sym_title,
        text=sym_text,
        content_sha256=compute_content_sha256(sym_title, sym_text),
        is_untrusted=False,
    )
    doc_title = f"Guide {token}"
    doc_text = f"Documentation section for {token}"
    doc_chunk = TextChunk(
        chunk_id=f"{slug}:doc:docs/guide.md:1",
        repo_id=rid,
        source_kind="doc_section",
        source_ref=f"{slug}:docs/guide.md#L1-L8",
        file_path="docs/guide.md",
        title=doc_title,
        text=doc_text,
        content_sha256=compute_content_sha256(doc_title, doc_text),
        is_untrusted=False,
    )
    commit_title = f"Commit {token}"
    commit_text = f"Commit message body for {token}"
    commit_chunk = TextChunk(
        chunk_id=f"{slug}@commit:{sha}",
        repo_id=rid,
        source_kind="commit",
        source_ref=f"{slug}@{sha}",
        title=commit_title,
        text=commit_text,
        content_sha256=compute_content_sha256(commit_title, commit_text),
        is_untrusted=False,
    )
    store.replace_code_index_for_files(
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
                sha=sha,
                blob_oid=f"blobsym_{token}",
            )
        ],
        doc_sections=[
            DocSectionRecord(
                repo_id=rid,
                file_path="docs/guide.md",
                section_id=1,
                level=1,
                heading=doc_title,
                breadcrumb=doc_title,
                anchor_slug=f"guide-{token}",
                line_start=1,
                line_end=8,
                subtree_line_end=8,
                content=doc_text,
                sha=sha,
                blob_oid=f"blobdoc_{token}",
            )
        ],
        chunks=[sym_chunk, doc_chunk],
        now=T0,
    )
    store.upsert_chunks([commit_chunk], now=T0)
    store.upsert_indexed_files(
        rid,
        [
            ("src/mod.py", f"blobsym_{token}", sha, "py-ast@1"),
            ("docs/guide.md", f"blobdoc_{token}", sha, "md-sections@1"),
        ],
    )
    store.set_commits_backfill_state(rid, cursor=None, done_at=committed_at, now=T0)
    return [sym_chunk, doc_chunk, commit_chunk]


def _seed_repo_live_and_sync_state(
    store: PackratStore, repo: RepoConfig, token: str, updated_at: str
) -> None:
    """Populates live PR status tables and per-repository `service_state` keys."""
    rid = repo.repo_id
    head_sha = f"headsha_{token}"
    store.upsert_pr_live(
        PrLiveRecord(
            repo_id=rid,
            number=2,
            title=f"PR for {token}",
            state="OPEN",
            updated_at=updated_at,
            node_id=f"PR_{token}_2",
            author="bob",
            head_sha=head_sha,
            rollup_state="SUCCESS",
            rollup_total=2,
        ),
        now=T0,
    )
    store.replace_check_runs(
        rid,
        2,
        head_sha,
        [
            CheckRunRecord(
                name="unit-tests",
                kind="CheckRun",
                status="COMPLETED",
                conclusion="SUCCESS",
                run_key=f"CheckRun:{token}",
            )
        ],
        now=T0,
    )
    store.replace_review_requests(
        rid,
        2,
        [
            ReviewRequestRecord(
                reviewer="dave",
                reviewer_kind="User",
                requested_at=updated_at,
            )
        ],
        now=T0,
    )
    store.replace_latest_opinionated_reviews(
        rid,
        2,
        [
            OpinionatedReviewRecord(
                author="carol",
                state="APPROVED",
                author_type="User",
                submitted_at=updated_at,
            )
        ],
        now=T0,
    )
    store.replace_latest_reviews(
        rid,
        2,
        [
            LatestReviewRecord(
                author="carol",
                state="APPROVED",
                author_type="User",
                submitted_at=updated_at,
                commit_oid=head_sha,
            )
        ],
        now=T0,
    )
    store.replace_thread_activity(
        rid,
        2,
        [
            ThreadActivityRecord(
                thread_id=f"RTA_{token}_2",
                is_resolved=False,
                is_outdated=False,
                last_author="carol",
                last_author_type="User",
                last_at=updated_at,
                participants=("bob", "carol"),
            )
        ],
        now=T0,
    )
    store.set_state(f"signals_events:{rid}", f'"etag-{token}"', now=T0)
    store.set_state(f"events_last_id:{rid}", f"evt-{token}", now=T0)
    store.set_state(f"refetch_open_prs:{rid}", "0", now=T0)
    store.set_gap_state(
        rid,
        "prs",
        cursor=f"GAP_PRS_{token}",
        floor="2027-01-01T00:00:00Z",
        now=T0,
    )
    store.set_gap_state(
        rid,
        "issues",
        cursor=f"GAP_ISSUES_{token}",
        floor="2027-01-02T00:00:00Z",
        now=T0,
    )


def _seed_repo_mirror_artifacts(
    store: PackratStore,
    repo: RepoConfig,
    token: str,
    *,
    watermark: str = "2027-01-20T12:00:00Z",
    include_git: bool = True,
    mark_backfills_done: bool = True,
) -> None:
    """Seeds GitHub tables, optional git tables, embeddings, watermarks, and live state."""
    chunks = _seed_repo_github_rows(store, repo, token, watermark)
    if include_git:
        chunks.extend(_seed_repo_git_rows(store, repo, token, watermark))
    vec = _token_vector(token)
    store.upsert_embeddings(
        [
            StoredEmbeddingRecord(
                chunk_id=ch.chunk_id,
                model_id="test-model",
                content_sha256=ch.content_sha256,
                vector=vec,
            )
            for ch in chunks
        ],
        now=T0,
    )
    if mark_backfills_done:
        for stream in ("issues", "prs", "discussions"):
            store.set_backfill_state(
                repo.repo_id, stream, cursor=None, done_at=watermark, now=T0
            )
    store.update_repo_sync_state(
        repo.repo_id,
        events_etag=f'"etag-{token}"',
        events_last_modified="Wed, 20 Jan 2027 12:00:00 GMT",
        last_events_check_at=watermark,
        issues_watermark=watermark,
        prs_watermark=watermark,
        discussions_watermark=watermark,
        last_full_sweep_at=watermark,
        last_indexed_sha=f"commitsha_{token}" if include_git else None,
        now=T0,
    )
    _seed_repo_live_and_sync_state(store, repo, token, watermark)


def _seed_personal_and_session_state(
    store: PackratStore, slug: str, *, viewer_login: str = "local-viewer"
) -> None:
    """Populates all 7 personal/runtime tables and personal `service_state` keys."""
    store.upsert_notifications(
        [
            NotificationRecord(
                thread_id=f"NT_{viewer_login}_1",
                reason="review_requested",
                subject_type="PullRequest",
                item_key=f"{slug}#2",
                updated_at="2027-01-10T12:00:00Z",
                unread=True,
            )
        ],
        now=T0,
    )
    store.replace_viewer_set("authored", [f"{slug}#2"], now=T0)
    store.add_watch("monitor", "conv-local", [f"{slug}#2"], 30, 600, now=T0)
    store.request_refresh([f"{slug}#2"], now=T0)
    store.append_change(f"{slug}#2", "pr", ["title"], now=T0)
    store.record_api_call(
        worker="hot",
        resource="graphql",
        cost=1,
        remaining=4800,
        reset_at=int(T0) + 3600,
        status=200,
        now=T0,
    )
    store.write_heartbeat(
        pid=42424,
        version="v-local",
        now=T0,
        last_ok_at=iso_at(T0),
        consecutive_failed_ticks=0,
    )
    store.set_state("viewer_login", viewer_login, now=T0)
    store.set_state(
        "notifications_last_modified",
        "Thu, 12 Nov 2026 10:00:00 GMT",
        now=T0,
    )
    store.set_state(f"node_id:{slug}#999", f"NODE_{slug}", now=T0)


def _assert_db_integrity(testcase: unittest.TestCase, conn: sqlite3.Connection) -> None:
    """Checks SQLite foreign keys and external-content `chunks_fts` synchronization."""
    testcase.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
    with conn:
        conn.execute(
            "INSERT INTO chunks_fts(chunks_fts, rank) VALUES('integrity-check', 1)"
        )
    testcase.assertFalse(conn.in_transaction)


def _build_v11_source_db(path: Path) -> None:
    """Creates a schema v11 database with pre-v12 timestamps, threads, and stale checks."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(CONNECTION_PRAGMAS)
        for version, migrate in MIGRATIONS[:11]:
            _run_migration(conn, version, migrate)
        conn.execute(
            """
            INSERT INTO repos (
              owner, name, full_name, mode, last_indexed_sha,
              issues_watermark, prs_watermark, discussions_watermark,
              issues_backfill_done_at, prs_backfill_done_at, discussions_backfill_done_at,
              created_at, updated_at
            ) VALUES (
              'octo-org', 'widgets', 'octo-org/widgets', 'mirror', 'v11sha',
              '2027-02-01T00:00:00Z', '2027-02-01T00:00:00Z', '2027-02-01T00:00:00Z',
              '2027-02-01T00:00:00Z', '2027-02-01T00:00:00Z', '2027-02-01T00:00:00Z',
              '2027-02-01T00:00:00Z', '2027-02-01T00:00:00Z'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO pull_requests (
              repo_id, number, node_id, state, title, head_sha, updated_at
            ) VALUES (
              1, 7, 'PR_v11_7', 'OPEN', 'Normalize v11 import', 'head_live',
              '2027-02-01T00:00:00Z'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO commits (repo_id, sha, committed_at, title)
            VALUES (1, 'v11sha', '2026-03-02T10:00:00-07:00', 'Offset commit v11token')
            """
        )
        conn.execute(
            """
            INSERT INTO code_symbols (
              repo_id, file_path, symbol_name, kind, line_start, line_end, signature, sha
            ) VALUES (
              1, 'src/v11.py', 'run_v11', 'function', 1, 5, 'def run_v11()', 'v11sha'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO doc_sections (
              repo_id, file_path, section_id, level, heading, breadcrumb, anchor_slug,
              line_start, line_end, subtree_line_end, content, sha
            ) VALUES (
              1, 'docs/v11.md', 1, 1, 'V11', 'V11', 'v11', 1, 4, 4, 'V11 docs', 'v11sha'
            )
            """
        )
        comments_json = json.dumps(
            [{"author": {"login": "carol"}}, {"author": {"login": "bob"}}]
        )
        conn.execute(
            """
            INSERT INTO review_threads (thread_id, repo_id, pr_number, reviewer, comments_json)
            VALUES ('RT_v11_7', 1, 7, 'dave', ?)
            """,
            (comments_json,),
        )
        conn.execute(
            """
            INSERT INTO check_runs (repo_id, pr_number, head_oid, run_key, name, kind)
            VALUES
              (1, 7, 'head_live', 'CheckRun:live', 'ci', 'CheckRun'),
              (1, 7, 'head_stale', 'CheckRun:stale', 'ci', 'CheckRun')
            """
        )
        title = "Offset commit v11token"
        text = "Commit body for v11token"
        content_sha = compute_content_sha256(title, text)
        conn.execute(
            """
            INSERT INTO chunks (
              rowid, chunk_id, repo_id, source_kind, source_ref, title, content_sha256,
              text, updated_at
            ) VALUES (
              1, 'octo-org/widgets@commit:v11sha', 1, 'commit',
              'octo-org/widgets@v11sha', ?, ?, ?, '2026-03-02T10:00:00-07:00'
            )
            """,
            (title, content_sha, text),
        )
        conn.execute(
            """
            INSERT INTO chunks_fts (rowid, chunk_id, source_kind, source_ref, title, text)
            VALUES (
              1, 'octo-org/widgets@commit:v11sha', 'commit', 'octo-org/widgets@v11sha', ?, ?
            )
            """,
            (title, text),
        )
        u8_blob, scale = quantize_u8_vector(_token_vector("v11token"))
        conn.execute(
            """
            INSERT INTO chunk_embeddings (
              chunk_id, model_id, dim, scale, vector_u8, content_sha256, updated_at
            ) VALUES (
              'octo-org/widgets@commit:v11sha', 'test-model', ?, ?, ?, ?,
              '2026-03-02T10:00:00-07:00'
            )
            """,
            (EMBEDDING_DIM, scale, u8_blob, content_sha),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


class TestImportDatabase(unittest.TestCase):
    """Tests `PackratStore.import_database`, previews, fixups, and preflight checks."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="packrat-import-"))
        self.dest_path = self.tmp_dir / "dest" / "repos.sqlite"
        self.dest_path.parent.mkdir(parents=True, exist_ok=True)
        self.dest_store = PackratStore(self.dest_path)

    def tearDown(self) -> None:
        self.dest_store.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _new_source_store(self, name: str = "src_raw.sqlite") -> tuple[Path, PackratStore]:
        """Creates a fresh source `PackratStore` in a separate subdirectory."""
        src_dir = self.tmp_dir / "sources"
        src_dir.mkdir(parents=True, exist_ok=True)
        path = src_dir / name
        return path, PackratStore(path)

    def _make_git_checkout(self, name: str = "checkout") -> Path:
        """Creates a directory containing a `.git` subdirectory for `--path` tests."""
        checkout = self.tmp_dir / name
        (checkout / ".git").mkdir(parents=True, exist_ok=True)
        return checkout

    def test_reexports_and_freshness_key_semantics(self) -> None:
        """Checks `db_store` re-exports, `FreshnessKey` ordering, and byte estimates."""
        self.assertEqual(db_store.MIN_IMPORT_SCHEMA_VERSION, 11)
        self.assertIs(db_store.ImportAbortedError, ImportAbortedError)
        self.assertIs(db_store.ImportIntegrityError, ImportIntegrityError)
        self.assertIs(db_store.mirror_freshness_key, mirror_freshness_key)
        self.assertIs(db_store.preview_import_database, preview_import_database)
        self.assertIs(db_store.validate_import_preflight, validate_import_preflight)
        self.assertIs(db_store.is_sqlite_path_alias, is_sqlite_path_alias)

        # Completed backfill count dominates newer watermarks; git index breaks ties.
        self.assertGreater(
            FreshnessKey(3, "2027-01-01T00:00:00Z", 0),
            FreshnessKey(2, "2027-05-01T00:00:00Z", 1),
        )
        self.assertGreater(
            FreshnessKey(3, "2027-02-01T00:00:00Z", 0),
            FreshnessKey(3, "2027-01-01T00:00:00Z", 1),
        )
        self.assertGreater(
            FreshnessKey(3, "2027-02-01T00:00:00Z", 1),
            FreshnessKey(3, "2027-02-01T00:00:00Z", 0),
        )

        # Offset watermarks normalize to UTC, fallback uses last_full_sweep_at, and
        # local poll clocks (last_events_check_at, updated_at) never contribute.
        offset_key = mirror_freshness_key(
            {
                "issues_backfill_done_at": "2027-01-01T00:00:00Z",
                "prs_backfill_done_at": "2027-01-01T00:00:00Z",
                "issues_watermark": "2027-02-01T11:30:00+02:00",
                "prs_watermark": "2027-02-01T08:00:00Z",
                "last_events_check_at": "2099-01-01T00:00:00Z",
                "updated_at": "2099-01-01T00:00:00Z",
                "last_indexed_sha": "abc123",
            }
        )
        self.assertEqual(
            offset_key, FreshnessKey(2, "2027-02-01T09:30:00Z", 1)
        )

        sweep_fallback = mirror_freshness_key(
            {
                "last_full_sweep_at": "2027-01-15T06:00:00Z",
                "last_events_check_at": "2099-01-01T00:00:00Z",
                "updated_at": "2099-01-01T00:00:00Z",
            }
        )
        self.assertEqual(sweep_fallback, FreshnessKey(0, "2027-01-15T06:00:00Z", 0))

        unsynced = mirror_freshness_key(
            {
                "last_events_check_at": "2099-01-01T00:00:00Z",
                "updated_at": "2099-01-01T00:00:00Z",
            }
        )
        self.assertEqual(unsynced, FreshnessKey(0, "", 0))

        self.assertEqual(
            selected_bytes_estimate(100_000, 0, 500),
            int(100_000 * MIN_SELECTED_SHARE),
        )
        self.assertEqual(selected_bytes_estimate(100_000, 250, 500), 50_000)

    def test_multi_repo_merge_preserves_local_promotes_skips_and_adds(self) -> None:
        """Merges repos B, C, and D while preserving untouched local mirror A and personal state."""
        # Repo A (octo-org/sprockets, dest repo_id=1): untouched local mirror.
        repo_a = self.dest_store.register_repo(
            "octo-org", "sprockets", local_path="/tmp/local-sprockets", now=T0
        )
        _seed_repo_mirror_artifacts(
            self.dest_store,
            repo_a,
            "sprockets_local",
            watermark="2027-01-25T00:00:00Z",
        )
        _seed_personal_and_session_state(self.dest_store, repo_a.full_name)

        # Repo B (Octo-Org/Widgets, dest repo_id=2): items stub with case mismatch,
        # watches, viewer_sets (including 'issues' set), and an existing local_path.
        repo_b = self.dest_store.ensure_items_repo("Octo-Org", "Widgets", now=T0)
        with self.dest_store.conn:
            self.dest_store.conn.execute(
                "UPDATE repos SET local_path = ?, events_etag = '\"stale-b\"', "
                "events_last_modified = 'stale-mod' WHERE repo_id = ?",
                ("/tmp/local-widgets", repo_b.repo_id),
            )
        self.dest_store.add_watch(
            "monitor",
            "conv-b",
            ["Octo-Org/Widgets#1", "Octo-Org/Widgets#2"],
            freshness_s=30,
            ttl_s=600,
            now=T0,
        )
        self.dest_store.replace_viewer_set(
            "authored", ["Octo-Org/Widgets#2", "octo-org/sprockets#2"], now=T0
        )
        self.dest_store.replace_viewer_set(
            "review_requested", ["Octo-Org/Widgets#4"], now=T0
        )
        self.dest_store.replace_viewer_set(
            "issues", ["Octo-Org/Widgets#1", "Octo-Org/Widgets#9"], now=T0
        )
        self.dest_store.take_refresh_requests()

        # Repo C (octo-org/gadgets, dest repo_id=3): newer mirror than source.
        repo_c = self.dest_store.register_repo(
            "octo-org", "gadgets", local_path="/tmp/local-gadgets", now=T0
        )
        _seed_repo_mirror_artifacts(
            self.dest_store,
            repo_c,
            "gadgets_dest_newer",
            watermark="2027-02-20T00:00:00Z",
        )

        # Build exported source with colliding repo_id=1 on D (octo-org/anvil),
        # lowercase B (octo-org/widgets), and older C with newer HTTP 304 check time.
        _, src_store = self._new_source_store("multi_src_raw.sqlite")
        exported_src = self.tmp_dir / "sources" / "multi_exported.sqlite"
        try:
            src_d = src_store.register_repo(
                "octo-org", "anvil", local_path="/Users/sender/anvil", now=T0
            )
            self.assertEqual(src_d.repo_id, repo_a.repo_id)
            _seed_repo_mirror_artifacts(
                src_store, src_d, "anvil_src", watermark="2027-02-10T00:00:00Z"
            )

            src_b = src_store.register_repo(
                "octo-org", "widgets", local_path="/Users/sender/widgets", now=T0
            )
            _seed_repo_mirror_artifacts(
                src_store, src_b, "widgets_src", watermark="2027-02-10T00:00:00Z"
            )

            src_c = src_store.register_repo(
                "octo-org", "gadgets", local_path="/Users/sender/gadgets", now=T0
            )
            _seed_repo_mirror_artifacts(
                src_store, src_c, "gadgets_src_older", watermark="2027-01-15T00:00:00Z"
            )
            src_store.update_repo_sync_state(
                src_c.repo_id,
                last_events_check_at="2027-04-01T00:00:00Z",
                now=T0 + 50_000,
            )
            src_store.export_database(exported_src, now=T0 + 100)
        finally:
            src_store.close()

        # Prime a vector mmap cache before import to verify vector_cache_seq invalidation.
        cache_dir = self.tmp_dir / "vec_cache"
        cache_dir.mkdir()
        embedder = _TokenEmbedder()
        pre_hits = hybrid_rrf_search(
            self.dest_store,
            "sprockets_local",
            mode="hybrid",
            embedder=embedder,
            cache_dir=cache_dir,
        )
        self.assertTrue(pre_hits)

        seq_before = self.dest_store.max_change_seq()
        vec_seq_before = int(self.dest_store.get_state("vector_cache_seq") or "0")
        summary = self.dest_store.import_database(exported_src, now=T0 + 500)

        self.assertFalse(summary.dry_run)
        self.assertEqual(summary.added_count, 1)
        self.assertEqual(summary.promoted_count, 1)
        self.assertEqual(summary.replaced_count, 0)
        self.assertEqual(summary.skipped_older_count, 1)
        self.assertEqual(summary.failed_count, 0)
        self.assertEqual(summary.not_attempted_count, 0)
        self.assertEqual(summary.chunks_imported, 8)
        self.assertEqual(summary.embeddings_imported, 8)
        self.assertEqual(
            [(o.full_name, o.disposition) for o in summary.outcomes],
            [
                ("octo-org/anvil", "added"),
                ("octo-org/widgets", "promoted"),
                ("octo-org/gadgets", "skipped_older"),
            ],
        )
        _assert_db_integrity(self, self.dest_store.conn)

        # Repo A and personal state are untouched despite colliding src repo_id=1.
        after_a = self.dest_store.get_repo("octo-org/sprockets")
        assert after_a is not None
        self.assertEqual(after_a.repo_id, repo_a.repo_id)
        self.assertEqual(after_a.local_path, "/tmp/local-sprockets")
        self.assertEqual(
            self.dest_store.get_state(f"signals_events:{repo_a.repo_id}"),
            '"etag-sprockets_local"',
        )
        self.assertEqual(self.dest_store.get_state("viewer_login"), "local-viewer")
        self.assertEqual(
            self.dest_store.get_state("notifications_last_modified"),
            "Thu, 12 Nov 2026 10:00:00 GMT",
        )
        self.assertIsNotNone(self.dest_store.read_heartbeat())

        self._assert_promoted_repo_b_invariants(repo_b.repo_id, seq_before)

        # Repo C skipped_older despite newer last_events_check_at on source.
        after_c = self.dest_store.get_repo("octo-org/gadgets")
        assert after_c is not None
        self.assertEqual(after_c.prs_watermark, "2027-02-20T00:00:00Z")
        self.assertEqual(after_c.local_path, "/tmp/local-gadgets")
        self.assertEqual(
            hybrid_rrf_search(self.dest_store, "gadgets_src_older", mode="fts"), []
        )

        # Repo D added with remapped repo_id, remapped service_state, and hybrid search hits.
        after_d = self.dest_store.get_repo("octo-org/anvil")
        assert after_d is not None
        self.assertNotEqual(after_d.repo_id, repo_a.repo_id)
        self.assertIsNone(after_d.local_path)
        self.assertEqual(
            self.dest_store.get_state(f"signals_events:{after_d.repo_id}"),
            '"etag-anvil_src"',
        )
        self.assertEqual(
            self.dest_store.get_state(f"gap_cursor:{after_d.repo_id}:prs"),
            "GAP_PRS_anvil_src",
        )
        self.assertGreater(
            int(self.dest_store.get_state("vector_cache_seq") or "0"), vec_seq_before
        )

        d_hits = hybrid_rrf_search(
            self.dest_store,
            "anvil_src",
            mode="hybrid",
            embedder=embedder,
            cache_dir=cache_dir,
        )
        self.assertEqual([h.repo_full_name for h in d_hits[:4]], ["octo-org/anvil"] * 4)
        self.assertIsNone(d_hits[4].bm25_rank)
        self.assertIn(1, {h.bm25_rank for h in d_hits})
        self.assertIn(1, {h.vector_rank for h in d_hits})
        self.assertGreater(d_hits[0].cosine_score or 0.0, 0.99)
        for p_table in PERSONAL_TABLES:
            p_cnt = int(
                self.dest_store.conn.execute(
                    f"SELECT COUNT(*) FROM {p_table}"
                ).fetchone()[0]
            )
            self.assertGreater(p_cnt, 0, f"Expected preserved rows in {p_table}")

    def _assert_promoted_repo_b_invariants(
        self, repo_b_id: int, seq_before: int
    ) -> None:
        """Checks promoted `Octo-Org/Widgets` prefixes, aliases, refreshes, and changes."""
        after_b = self.dest_store.get_repo("Octo-Org/Widgets")
        assert after_b is not None
        self.assertEqual(after_b.repo_id, repo_b_id)
        self.assertEqual(after_b.full_name, "Octo-Org/Widgets")
        self.assertEqual(after_b.mode, "mirror")
        self.assertEqual(after_b.local_path, "/tmp/local-widgets")
        self.assertIsNone(after_b.events_etag)
        self.assertIsNone(after_b.events_last_modified)
        for gh_table in REPO_GITHUB_TABLES:
            cnt = int(
                self.dest_store.conn.execute(
                    f"SELECT COUNT(*) FROM {gh_table} WHERE repo_id = ?", (repo_b_id,)
                ).fetchone()[0]
            )
            self.assertGreater(cnt, 0, f"Expected copied rows in {gh_table}")

        b_chunks = self.dest_store.conn.execute(
            "SELECT chunk_id, source_ref FROM chunks WHERE repo_id = ? ORDER BY chunk_id",
            (repo_b_id,),
        ).fetchall()
        b_chunk_ids = [str(r[0]) for r in b_chunks]
        b_refs = [str(r[1]) for r in b_chunks]
        self.assertEqual(
            b_chunk_ids,
            [
                "Octo-Org/Widgets#pr:2",
                "Octo-Org/Widgets:doc:docs/guide.md:1",
                "Octo-Org/Widgets@commit:commitsha_widgets_src",
                "Octo-Org/Widgets@symbol:src/mod.py:handle_widgets_src:1",
            ],
        )
        self.assertEqual(
            b_refs,
            [
                "Octo-Org/Widgets#2",
                "Octo-Org/Widgets:docs/guide.md#L1-L8",
                "Octo-Org/Widgets@commitsha_widgets_src",
                "Octo-Org/Widgets@commitsha_widgets_src",
            ],
        )
        b_emb_ids = [
            str(r[0])
            for r in self.dest_store.conn.execute(
                "SELECT ce.chunk_id FROM chunk_embeddings ce "
                "JOIN chunks c ON c.chunk_id = ce.chunk_id "
                "WHERE c.repo_id = ? ORDER BY ce.chunk_id",
                (repo_b_id,),
            )
        ]
        self.assertEqual(b_emb_ids, b_chunk_ids)

        timeline_subjects = {
            str(r[0]): str(r[1])
            for r in self.dest_store.conn.execute(
                "SELECT subject_type, subject FROM timeline_events WHERE repo_id = ?",
                (repo_b_id,),
            )
        }
        self.assertEqual(
            timeline_subjects,
            {"Issue": "Octo-Org/Widgets#1", "Label": "octo-org/widgets#keep-label"},
        )
        self.assertEqual(
            sorted(self.dest_store.peek_refresh_requests()),
            ["Octo-Org/Widgets#1", "Octo-Org/Widgets#2", "Octo-Org/Widgets#4"],
        )
        new_changes = self.dest_store.changes_since(seq_before)
        self.assertEqual(
            [(c.item_key, c.kind, c.fields) for c in new_changes],
            [
                ("Octo-Org/Widgets#2", "pr", ("pr", "checks", "reviews", "threads")),
                ("Octo-Org/Widgets#4", "pr", ("pr", "checks", "reviews", "threads")),
            ],
        )
        active_watch_keys = {w.item_key for w in self.dest_store.active_watches(now=T0)}
        self.assertGreaterEqual(
            active_watch_keys, {"Octo-Org/Widgets#1", "Octo-Org/Widgets#2"}
        )

    def test_replaced_mirror_scrubs_unexported_foreign_state_and_queues_refreshes(
        self,
    ) -> None:
        """Imports directly from a raw store without copying foreign paths or personal rows."""
        dest_w = self.dest_store.register_repo(
            "octo-org", "widgets", local_path="/tmp/dest-widgets", now=T0
        )
        _seed_repo_mirror_artifacts(
            self.dest_store,
            dest_w,
            "widgets_old_local",
            watermark="2027-01-10T00:00:00Z",
        )
        self.dest_store.add_watch(
            "monitor", "c1", ["octo-org/widgets#2"], freshness_s=30, ttl_s=600, now=T0
        )
        self.dest_store.replace_viewer_set(
            "review_requested", ["octo-org/widgets#4"], now=T0
        )
        self.dest_store.take_refresh_requests()
        self.dest_store.set_state("viewer_login", "recipient-user", now=T0)

        raw_src_path, raw_src = self._new_source_store("unexported_src.sqlite")
        try:
            src_w = raw_src.register_repo(
                "octo-org", "widgets", local_path="/Users/foreign/widgets", now=T0
            )
            src_a = raw_src.register_repo(
                "octo-org", "anvil", local_path="/Users/foreign/anvil", now=T0
            )
            _seed_repo_mirror_artifacts(
                raw_src, src_w, "widgets_newer_src", watermark="2027-03-01T00:00:00Z"
            )
            _seed_repo_mirror_artifacts(
                raw_src, src_a, "anvil_newer_src", watermark="2027-03-01T00:00:00Z"
            )
            _seed_personal_and_session_state(
                raw_src, "octo-org/widgets", viewer_login="foreign-sender"
            )
            raw_src.wal_checkpoint_truncate()
        finally:
            raw_src.close()

        seq_before = self.dest_store.max_change_seq()
        summary = self.dest_store.import_database(raw_src_path, now=T0 + 200)

        self.assertEqual(summary.replaced_count, 1)
        self.assertEqual(summary.added_count, 1)
        updated_w = self.dest_store.get_repo("octo-org/widgets")
        added_a = self.dest_store.get_repo("octo-org/anvil")
        assert updated_w is not None and added_a is not None
        self.assertEqual(updated_w.local_path, "/tmp/dest-widgets")
        self.assertIsNone(added_a.local_path)
        self.assertEqual(self.dest_store.get_state("viewer_login"), "recipient-user")
        self.assertEqual(
            sorted(self.dest_store.peek_refresh_requests()),
            ["octo-org/widgets#2", "octo-org/widgets#4"],
        )
        changes = self.dest_store.changes_since(seq_before)
        self.assertEqual(
            [(c.item_key, c.kind) for c in changes],
            [("octo-org/widgets#2", "pr"), ("octo-org/widgets#4", "pr")],
        )
        notif_threads = [
            str(r[0])
            for r in self.dest_store.conn.execute("SELECT thread_id FROM notifications")
        ]
        self.assertEqual(notif_threads, [])

    def test_skipped_older_binds_local_path_and_leaves_clean_tx_for_next_copy(
        self,
    ) -> None:
        """Binds `--path` on `skipped_older` inside `with conn:` without breaking later `BEGIN IMMEDIATE`."""
        dest_w = self.dest_store.register_repo("octo-org", "widgets", now=T0)
        _seed_repo_mirror_artifacts(
            self.dest_store,
            dest_w,
            "widgets_dest_fresh",
            watermark="2027-03-01T00:00:00Z",
        )
        checkout = self._make_git_checkout("widgets-checkout")

        _, src_store = self._new_source_store("skip_then_copy.sqlite")
        exported_src = self.tmp_dir / "sources" / "skip_then_copy_exp.sqlite"
        try:
            src_w = src_store.register_repo("octo-org", "widgets", now=T0)
            src_g = src_store.register_repo("octo-org", "gadgets", now=T0)
            _seed_repo_mirror_artifacts(
                src_store, src_w, "widgets_src_stale", watermark="2027-01-10T00:00:00Z"
            )
            _seed_repo_mirror_artifacts(
                src_store, src_g, "gadgets_src_fresh", watermark="2027-02-15T00:00:00Z"
            )
            src_store.export_database(exported_src, now=T0 + 50)
        finally:
            src_store.close()

        first = self.dest_store.import_database(
            exported_src,
            repo_tokens=["octo-org/widgets"],
            local_path=str(checkout),
            now=T0 + 100,
        )
        self.assertEqual(first.skipped_older_count, 1)
        self.assertTrue(first.outcomes[0].bound_local_path)
        self.assertFalse(self.dest_store.conn.in_transaction)
        rebound = self.dest_store.get_repo("octo-org/widgets")
        assert rebound is not None
        self.assertEqual(rebound.local_path, str(checkout.resolve()))

        # Follow immediately with a multi-repo import (repo 1 skipped_older, repo 2 added)
        # and verify `_bind_local_path` inside a multi-repo sequence also leaves no open tx.
        second = self.dest_store.import_database(exported_src, now=T0 + 200)
        self.assertEqual(second.skipped_older_count, 1)
        self.assertEqual(second.added_count, 1)
        self.assertFalse(self.dest_store.conn.in_transaction)
        self.assertIsNotNone(self.dest_store.get_repo("octo-org/gadgets"))

        # Single-repo `--path` also binds on a newly added repository.
        anvil_checkout = self._make_git_checkout("gadgets-checkout")
        third = self.dest_store.import_database(
            exported_src,
            repo_tokens=["octo-org/gadgets"],
            local_path=str(anvil_checkout),
            allow_older=True,
            now=T0 + 300,
        )
        self.assertEqual(third.replaced_count, 1)
        self.assertTrue(third.outcomes[0].bound_local_path)
        gadgets = self.dest_store.get_repo("octo-org/gadgets")
        assert gadgets is not None
        self.assertEqual(gadgets.local_path, str(anvil_checkout.resolve()))

    def test_preserve_local_git_with_interrupted_commit_chunks_and_allow_older_overwrite(
        self,
    ) -> None:
        """Preserves local git tables and chunks when `src.last_indexed_sha` is NULL unless `allow_older`."""
        dest_w = self.dest_store.register_repo(
            "octo-org", "widgets", local_path="/tmp/local-widgets", now=T0
        )
        _seed_repo_mirror_artifacts(
            self.dest_store,
            dest_w,
            "localgitsurvivor",
            watermark="2027-01-10T00:00:00Z",
            include_git=True,
        )

        # Build a GitHub-only newer source (`last_indexed_sha IS NULL`) that also holds
        # partial commit rows/chunks from an interrupted `_sync_commits_phase`.
        src_path, src_store = self._new_source_store("github_only_interrupted.sqlite")
        try:
            src_w = src_store.register_repo("octo-org", "widgets", now=T0)
            _seed_repo_mirror_artifacts(
                src_store,
                src_w,
                "newgithubpr",
                watermark="2027-02-25T00:00:00Z",
                include_git=False,
            )
            src_store.upsert_commits(
                [
                    CommitRecord(
                        repo_id=src_w.repo_id,
                        sha="partial_src_sha",
                        committed_at="2027-02-24T00:00:00Z",
                        title="Interrupted commit partialsrctoken",
                    )
                ]
            )
            partial_title = "Interrupted commit partialsrctoken"
            partial_text = "Partial commit body partialsrctoken"
            partial_sha = compute_content_sha256(partial_title, partial_text)
            partial_chunk = TextChunk(
                chunk_id="octo-org/widgets@commit:partial_src_sha",
                repo_id=src_w.repo_id,
                source_kind="commit",
                source_ref="octo-org/widgets@partial_src_sha",
                title=partial_title,
                text=partial_text,
                content_sha256=partial_sha,
                is_untrusted=False,
            )
            src_store.upsert_chunks([partial_chunk], now=T0)
            src_store.upsert_embeddings(
                [
                    StoredEmbeddingRecord(
                        chunk_id=partial_chunk.chunk_id,
                        model_id="test-model",
                        content_sha256=partial_sha,
                        vector=_token_vector("partialsrctoken"),
                    )
                ],
                now=T0,
            )
            src_store.wal_checkpoint_truncate()
        finally:
            src_store.close()

        kept = self.dest_store.import_database(src_path, allow_older=False, now=T0 + 100)
        self.assertEqual(kept.replaced_count, 1)
        self.assertTrue(kept.outcomes[0].preserved_local_git)
        self.assertEqual(kept.chunks_imported, 1)
        self.assertEqual(kept.embeddings_imported, 1)
        _assert_db_integrity(self, self.dest_store.conn)

        after_kept = self.dest_store.get_repo("octo-org/widgets")
        assert after_kept is not None
        self.assertEqual(after_kept.last_indexed_sha, "commitsha_localgitsurvivor")
        self.assertEqual(after_kept.commits_backfill_done_at, "2027-01-10T00:00:00Z")
        self.assertEqual(after_kept.prs_watermark, "2027-02-25T00:00:00Z")
        for git_table in REPO_GIT_TABLES:
            cnt = int(
                self.dest_store.conn.execute(
                    f"SELECT COUNT(*) FROM {git_table} WHERE repo_id = ?",
                    (dest_w.repo_id,),
                ).fetchone()[0]
            )
            self.assertGreater(cnt, 0, f"Expected preserved rows in {git_table}")

        git_kinds = {
            str(r[0])
            for r in self.dest_store.conn.execute(
                "SELECT DISTINCT source_kind FROM chunks WHERE repo_id = ? "
                "AND source_kind IN ('commit', 'code_symbol', 'doc_section')",
                (dest_w.repo_id,),
            )
        }
        self.assertEqual(git_kinds, set(GIT_SOURCE_KINDS))
        self.assertTrue(
            hybrid_rrf_search(self.dest_store, "localgitsurvivor", mode="fts")
        )
        self.assertTrue(hybrid_rrf_search(self.dest_store, "newgithubpr", mode="fts"))
        self.assertEqual(
            hybrid_rrf_search(self.dest_store, "partialsrctoken", mode="fts"), []
        )

        # Passing allow_older=True overwrites local git tables and git chunks cleanly.
        overwritten = self.dest_store.import_database(
            src_path, allow_older=True, now=T0 + 200
        )
        self.assertEqual(overwritten.replaced_count, 1)
        self.assertFalse(overwritten.outcomes[0].preserved_local_git)
        _assert_db_integrity(self, self.dest_store.conn)

        after_overwritten = self.dest_store.get_repo("octo-org/widgets")
        assert after_overwritten is not None
        self.assertIsNone(after_overwritten.last_indexed_sha)
        for cleared_table in ("code_symbols", "doc_sections", "indexed_files"):
            cnt = int(
                self.dest_store.conn.execute(
                    f"SELECT COUNT(*) FROM {cleared_table} WHERE repo_id = ?",
                    (dest_w.repo_id,),
                ).fetchone()[0]
            )
            self.assertEqual(cnt, 0, f"Expected 0 rows in {cleared_table}")
        self.assertEqual(
            hybrid_rrf_search(self.dest_store, "localgitsurvivor", mode="fts"), []
        )
        self.assertTrue(
            hybrid_rrf_search(self.dest_store, "partialsrctoken", mode="fts")
        )

    def test_schema_v11_source_column_intersection_and_v12_fixups(self) -> None:
        """Imports a real schema v11 source and normalizes all Migration 12 invariants."""
        v11_path = self.tmp_dir / "sources" / "source_v11.sqlite"
        v11_path.parent.mkdir(parents=True, exist_ok=True)
        _build_v11_source_db(v11_path)

        with contextlib.closing(sqlite3.connect(str(v11_path))) as raw_v11:
            self.assertEqual(read_schema_version(raw_v11), MIN_IMPORT_SCHEMA_VERSION)
            v11_tables = {
                str(r[0])
                for r in raw_v11.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertNotIn("indexed_files", v11_tables)
            v11_repo_cols = {
                str(r[1]) for r in raw_v11.execute("PRAGMA table_info(repos)")
            }
            self.assertNotIn("commits_backfill_done_at", v11_repo_cols)

        preview = preview_import_database(self.dest_path, v11_path)
        self.assertEqual(preview.source_schema_version, 11)
        self.assertEqual(preview.dest_schema_version, LATEST_SCHEMA_VERSION)
        self.assertEqual(preview.added_count, 1)

        summary = self.dest_store.import_database(v11_path, now=T0 + 100)
        self.assertEqual(summary.source_schema_version, 11)
        self.assertEqual(summary.dest_schema_version, LATEST_SCHEMA_VERSION)
        self.assertEqual(summary.added_count, 1)
        self.assertEqual(summary.chunks_imported, 1)
        self.assertEqual(summary.embeddings_imported, 1)
        _assert_db_integrity(self, self.dest_store.conn)

        imported = self.dest_store.get_repo("octo-org/widgets")
        assert imported is not None
        self.assertIsNone(imported.commits_backfill_cursor)
        self.assertIsNone(imported.commits_backfill_done_at)

        conn = self.dest_store.conn
        committed_at = conn.execute(
            "SELECT committed_at FROM commits WHERE repo_id = ?", (imported.repo_id,)
        ).fetchone()[0]
        self.assertEqual(committed_at, "2026-03-02T17:00:00Z")

        chunk_updated = conn.execute(
            "SELECT updated_at FROM chunks WHERE repo_id = ?", (imported.repo_id,)
        ).fetchone()[0]
        self.assertEqual(chunk_updated, "2026-03-02T17:00:00Z")

        participants = conn.execute(
            "SELECT participants_json FROM review_threads WHERE thread_id = 'RT_v11_7'"
        ).fetchone()[0]
        self.assertEqual(json.loads(participants), ["bob", "carol", "dave"])

        check_heads = [
            str(r[0])
            for r in conn.execute(
                "SELECT head_oid FROM check_runs WHERE repo_id = ?", (imported.repo_id,)
            )
        ]
        self.assertEqual(check_heads, ["head_live"])

        idx_count = conn.execute(
            "SELECT COUNT(*) FROM indexed_files WHERE repo_id = ?", (imported.repo_id,)
        ).fetchone()[0]
        self.assertEqual(idx_count, 0)
        sym_blob = conn.execute(
            "SELECT blob_oid FROM code_symbols WHERE repo_id = ?", (imported.repo_id,)
        ).fetchone()[0]
        self.assertIsNone(sym_blob)
        self.assertTrue(hybrid_rrf_search(self.dest_store, "v11token", mode="fts"))

        # Replacing an existing v13 row from a v11 source clears unshared git backfill columns.
        self.dest_store.set_commits_backfill_state(
            imported.repo_id,
            cursor="cur_old",
            done_at="2027-01-01T00:00:00Z",
            now=T0 + 150,
        )
        replaced = self.dest_store.import_database(
            v11_path, allow_older=True, now=T0 + 200
        )
        self.assertEqual(replaced.replaced_count, 1)
        refreshed = self.dest_store.get_repo("octo-org/widgets")
        assert refreshed is not None
        self.assertIsNone(refreshed.commits_backfill_cursor)
        self.assertIsNone(refreshed.commits_backfill_done_at)

    def test_alias_matching_rename_safety_and_split_alias_preflight(self) -> None:
        """Matches renamed repos via `repo_aliases`, avoids shadowing, and rejects split aliases."""
        dest_w = self.dest_store.register_repo("octo-org", "widgets", now=T0)
        dest_g = self.dest_store.register_repo("octo-org", "gadgets", now=T0)
        _seed_repo_mirror_artifacts(
            self.dest_store, dest_w, "widgets_old", watermark="2027-01-05T00:00:00Z"
        )
        _seed_repo_mirror_artifacts(
            self.dest_store, dest_g, "gadgets_local", watermark="2027-01-05T00:00:00Z"
        )

        # Source renamed octo-org/widgets -> octo-org/sprockets and carries aliases.
        src_path, src_store = self._new_source_store("renamed_src.sqlite")
        try:
            src_s = src_store.register_repo("octo-org", "sprockets", now=T0)
            _seed_repo_mirror_artifacts(
                src_store, src_s, "sprockets_renamed", watermark="2027-02-20T00:00:00Z"
            )
            with src_store.conn:
                src_store.conn.executemany(
                    "INSERT INTO repo_aliases (alias_full_name, repo_id, created_at) "
                    "VALUES (?, ?, ?)",
                    [
                        ("octo-org/widgets", src_s.repo_id, iso_at(T0)),
                        ("octo-org/anvil", src_s.repo_id, iso_at(T0)),
                    ],
                )
            src_store.wal_checkpoint_truncate()
        finally:
            src_store.close()

        # Select via alias token ('octo-org/anvil'); matches dest 'octo-org/widgets'.
        summary = self.dest_store.import_database(
            src_path, repo_tokens=["octo-org/anvil"], now=T0 + 100
        )
        self.assertEqual(summary.replaced_count, 1)
        self.assertEqual(summary.outcomes[0].dest_repo_id, dest_w.repo_id)
        _assert_db_integrity(self, self.dest_store.conn)

        aliases = {
            str(r[0]).lower()
            for r in self.dest_store.conn.execute(
                "SELECT alias_full_name FROM repo_aliases WHERE repo_id = ?",
                (dest_w.repo_id,),
            )
        }
        self.assertEqual(aliases, {"octo-org/sprockets", "octo-org/anvil"})
        rewritten = [
            str(r[0])
            for r in self.dest_store.conn.execute(
                "SELECT chunk_id FROM chunks WHERE repo_id = ? ORDER BY chunk_id",
                (dest_w.repo_id,),
            )
        ]
        self.assertTrue(all(cid.startswith("octo-org/widgets") for cid in rewritten))

        # Split alias collision: separate destination has both octo-org/widgets and
        # octo-org/sprockets as distinct repo_ids, so preflight rejects before writing.
        split_dest_path = self.tmp_dir / "split_dest" / "repos.sqlite"
        split_dest_path.parent.mkdir(parents=True, exist_ok=True)
        split_store = PackratStore(split_dest_path)
        try:
            split_store.register_repo("octo-org", "widgets", now=T0)
            split_store.register_repo("octo-org", "sprockets", now=T0)
            with self.assertRaisesRegex(ValueError, "several destination repositories"):
                validate_import_preflight(split_dest_path, src_path)
            with self.assertRaisesRegex(ValueError, "several destination repositories"):
                preview_import_database(split_dest_path, src_path)
            with self.assertRaisesRegex(ValueError, "several destination repositories"):
                split_store.import_database(src_path)
        finally:
            split_store.close()

    def test_preflight_schema_bounds_self_aliases_and_selection_guards(self) -> None:
        """Checks schema version bounds, self-alias detection, `--repo`, and `--path` errors."""
        _, src_store = self._new_source_store("valid_src.sqlite")
        valid_src = self.tmp_dir / "sources" / "valid_exp.sqlite"
        try:
            repo = src_store.register_repo("octo-org", "widgets", now=T0)
            _seed_repo_mirror_artifacts(src_store, repo, "widgets_valid")
            src_store.export_database(valid_src, now=T0 + 10)
        finally:
            src_store.close()

        # Schema > LATEST_SCHEMA_VERSION (99) raises SchemaTooNewError.
        v99_src = self.tmp_dir / "sources" / "v99.sqlite"
        shutil.copy2(valid_src, v99_src)
        with contextlib.closing(sqlite3.connect(str(v99_src))) as conn, conn:
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (99, 'now')"
            )
        with self.assertRaises(SchemaTooNewError):
            validate_import_preflight(self.dest_path, v99_src)
        with self.assertRaises(SchemaTooNewError):
            preview_import_database(self.dest_path, v99_src)
        with self.assertRaises(SchemaTooNewError):
            self.dest_store.import_database(v99_src)

        # Schema < MIN_IMPORT_SCHEMA_VERSION (v10 and unversioned 0) raises ValueError.
        v10_src = self.tmp_dir / "sources" / "v10.sqlite"
        with contextlib.closing(sqlite3.connect(str(v10_src))) as conn:
            conn.executescript(CONNECTION_PRAGMAS)
            for version, migrate in MIGRATIONS[:10]:
                _run_migration(conn, version, migrate)
        unversioned_src = self.tmp_dir / "sources" / "v0.sqlite"
        with contextlib.closing(sqlite3.connect(str(unversioned_src))) as conn, conn:
            conn.execute("CREATE TABLE dummy (x INTEGER)")
        for old_src in (v10_src, unversioned_src):
            with self.assertRaises(ValueError):
                validate_import_preflight(self.dest_path, old_src)
            with self.assertRaises(ValueError):
                self.dest_store.import_database(old_src)

        # Preview destination schema guards (too new -> SchemaTooNewError, behind -> ValueError).
        with self.assertRaises(SchemaTooNewError):
            preview_import_database(v99_src, valid_src)
        with self.assertRaises(ValueError):
            preview_import_database(v10_src, valid_src)

        # Self-alias and invalid source path rejection.
        symlink_to_dest = self.tmp_dir / "dest_symlink.sqlite"
        symlink_to_dest.symlink_to(self.dest_path)
        hardlink_to_dest = self.tmp_dir / "dest_hardlink.sqlite"
        os.link(self.dest_path, hardlink_to_dest)
        journal_sidecar = Path(f"{self.dest_path}-journal")
        journal_sidecar.write_bytes(b"journal")
        bad_sources: tuple[Path | str, ...] = (
            self.dest_path,
            self.dest_path.parent / "." / self.dest_path.name,
            symlink_to_dest,
            hardlink_to_dest,
            Path(f"{self.dest_path}-wal"),
            Path(f"{self.dest_path}-shm"),
            journal_sidecar,
            ":memory:",
            "",
            "file:mem?mode=memory",
            self.tmp_dir,
            self.tmp_dir / "missing.sqlite",
        )
        for bad_src in bad_sources:
            with self.assertRaises(ValueError, msg=f"Expected ValueError for {bad_src!r}"):
                validate_import_preflight(self.dest_path, bad_src)

        # Token and `--path` validation errors.
        selection_src, sel_store = self._new_source_store("selection_guards.sqlite")
        try:
            sel_store.register_repo("octo-org", "widgets", now=T0)
            sel_store.register_repo("octo-org", "gadgets", now=T0)
            sel_store.ensure_items_repo("octo-org", "sprockets", now=T0)
            sel_store.wal_checkpoint_truncate()
        finally:
            sel_store.close()

        empty_mirror_src, empty_store = self._new_source_store("items_only.sqlite")
        try:
            empty_store.ensure_items_repo("octo-org", "widgets", now=T0)
            empty_store.wal_checkpoint_truncate()
        finally:
            empty_store.close()
        with self.assertRaisesRegex(ValueError, "no mirror repositories"):
            validate_import_preflight(self.dest_path, empty_mirror_src)

        checkout = self._make_git_checkout("valid-checkout")
        no_git_dir = self.tmp_dir / "not-a-git-repo"
        no_git_dir.mkdir()
        with self.assertRaisesRegex(ValueError, "items mode"):
            validate_import_preflight(
                self.dest_path, selection_src, repo_tokens=["octo-org/sprockets"]
            )
        with self.assertRaisesRegex(ValueError, "not a mirror"):
            validate_import_preflight(
                self.dest_path, selection_src, repo_tokens=["octo-org/anvil"]
            )
        with self.assertRaisesRegex(ValueError, "single repository"):
            validate_import_preflight(
                self.dest_path, selection_src, local_path=str(checkout)
            )
        with self.assertRaisesRegex(ValueError, "no .git"):
            validate_import_preflight(
                self.dest_path,
                selection_src,
                repo_tokens=["widgets"],
                local_path=str(no_git_dir),
            )

    def test_ro_open_falls_back_to_immutable_only_without_wal_sidecar(self) -> None:
        """Falls back to `immutable=1` only when `<src>-wal` is absent on disk."""
        _, src_store = self._new_source_store("ro_raw.sqlite")
        clean_src = self.tmp_dir / "sources" / "ro_clean.sqlite"
        try:
            repo = src_store.register_repo("octo-org", "widgets", now=T0)
            _seed_repo_mirror_artifacts(src_store, repo, "widgets_ro")
            src_store.export_database(clean_src, now=T0 + 10)
        finally:
            src_store.close()

        real_connect = sqlite3.connect

        def connect_refusing_plain_ro(database: str, *args: Any, **kwargs: Any) -> Any:
            if (
                kwargs.get("uri")
                and str(clean_src) in str(database)
                and "mode=ro" in str(database)
                and "immutable=1" not in str(database)
            ):
                raise sqlite3.OperationalError("unable to open database file")
            return real_connect(database, *args, **kwargs)

        with mock.patch("db_import.sqlite3.connect", side_effect=connect_refusing_plain_ro):
            resolved = validate_import_preflight(self.dest_path, clean_src)
            self.assertEqual(resolved, clean_src.resolve())
            preview = preview_import_database(self.dest_path, clean_src)
            self.assertEqual(preview.added_count, 1)

            wal_sidecar = Path(f"{clean_src}-wal")
            wal_sidecar.write_bytes(b"uncheckpointed-wal-frames")
            with self.assertRaises(sqlite3.OperationalError):
                validate_import_preflight(self.dest_path, clean_src)
            with self.assertRaises(sqlite3.OperationalError):
                preview_import_database(self.dest_path, clean_src)

    def test_chunk_scaled_disk_preflight_blocks_full_import_but_allows_subset(
        self,
    ) -> None:
        """Scales required free disk by selected chunks so small `--repo` subsets pass."""
        _, src_store = self._new_source_store("disk_raw.sqlite")
        disk_src = self.tmp_dir / "sources" / "disk_bundle.sqlite"
        try:
            big_repo = src_store.register_repo("octo-org", "widgets", now=T0)
            small_repo = src_store.register_repo("octo-org", "gadgets", now=T0)
            _seed_repo_mirror_artifacts(src_store, big_repo, "widgets_big")
            extra_chunks = [
                TextChunk(
                    chunk_id=f"octo-org/widgets#issue:{idx}",
                    repo_id=big_repo.repo_id,
                    source_kind="issue",
                    source_ref=f"octo-org/widgets#{idx}",
                    item_number=idx,
                    title=f"Extra issue {idx}",
                    text=f"Body {idx} " * 40,
                    content_sha256=compute_content_sha256(
                        f"Extra issue {idx}", f"Body {idx} " * 40
                    ),
                )
                for idx in range(10, 26)
            ]
            src_store.upsert_chunks(extra_chunks, now=T0)
            _seed_repo_mirror_artifacts(
                src_store, small_repo, "gadgets_small", include_git=False
            )
            src_store.export_database(disk_src, now=T0 + 10)
        finally:
            src_store.close()

        src_bytes = disk_src.stat().st_size
        subset_est = selected_bytes_estimate(src_bytes, 1, 21)
        tight_free = 2 * subset_est + 64
        self.assertLess(tight_free, 2 * src_bytes)
        fake_usage = shutil._ntuple_diskusage(10 * src_bytes, 8 * src_bytes, tight_free)  # type: ignore[attr-defined]

        with mock.patch("db_import.shutil.disk_usage", return_value=fake_usage):
            with self.assertRaisesRegex(ValueError, "Not enough free disk space"):
                validate_import_preflight(self.dest_path, disk_src)
            with self.assertRaisesRegex(ValueError, "Not enough free disk space"):
                self.dest_store.import_database(disk_src)
            self.assertEqual(self.dest_store.list_repos(), [])

            summary = self.dest_store.import_database(
                disk_src, repo_tokens=["octo-org/gadgets"], now=T0 + 50
            )
            self.assertEqual(summary.added_count, 1)
            self.assertEqual(
                [r.full_name for r in self.dest_store.list_repos()],
                ["octo-org/gadgets"],
            )

        zero_usage = shutil._ntuple_diskusage(10 * src_bytes, 10 * src_bytes, 0)  # type: ignore[attr-defined]
        with mock.patch("db_import.shutil.disk_usage", return_value=zero_usage):
            preview = preview_import_database(self.dest_path, disk_src)
            self.assertTrue(preview.dry_run)

    def test_preview_import_database_zero_writes_and_named_json_keys(self) -> None:
        """Previews missing and existing destinations without creating files or `init.sql`."""
        _, src_store = self._new_source_store("preview_src_raw.sqlite")
        src_exp = self.tmp_dir / "sources" / "preview_src.sqlite"
        try:
            w = src_store.register_repo("octo-org", "widgets", now=T0)
            g = src_store.register_repo("octo-org", "gadgets", now=T0)
            s = src_store.register_repo("octo-org", "sprockets", now=T0)
            a = src_store.register_repo("octo-org", "anvil", now=T0)
            _seed_repo_mirror_artifacts(
                src_store, w, "w_src", watermark="2027-02-10T00:00:00Z"
            )
            _seed_repo_mirror_artifacts(
                src_store, g, "g_src", watermark="2027-01-10T00:00:00Z"
            )
            _seed_repo_mirror_artifacts(
                src_store,
                s,
                "s_src",
                watermark="2027-02-10T00:00:00Z",
                include_git=False,
            )
            _seed_repo_mirror_artifacts(
                src_store, a, "a_src", watermark="2027-02-10T00:00:00Z"
            )
            src_store.export_database(src_exp, now=T0 + 10)
        finally:
            src_store.close()

        # 1. Missing destination directory and file: zero files created, named JSON keys.
        missing_dest = self.tmp_dir / "nonexistent_dir" / "nested" / "repos.sqlite"
        checkout = self._make_git_checkout("preview-checkout")
        missing_preview = preview_import_database(
            missing_dest,
            src_exp,
            repo_tokens=["octo-org/widgets"],
            local_path=str(checkout),
        )
        self.assertTrue(missing_preview.dry_run)
        self.assertEqual(missing_preview.added_count, 1)
        self.assertTrue(missing_preview.outcomes[0].bound_local_path)
        self.assertFalse(missing_dest.exists())
        self.assertFalse(missing_dest.parent.exists())

        payload = json.loads(json.dumps(dataclasses.asdict(missing_preview)))
        self.assertEqual(
            set(payload["outcomes"][0]["src_freshness"]),
            {"done_backfills", "sync_watermark", "has_git_index"},
        )

        # 2. Existing destination: byte-for-byte unchanged, zero WAL frames, zero init.sql.
        existing_dir = self.tmp_dir / "existing_preview"
        existing_dir.mkdir()
        existing_dest = existing_dir / "repos.sqlite"
        ex_store = PackratStore(existing_dest)
        try:
            ex_store.ensure_items_repo("Octo-Org", "Widgets", now=T0)
            ex_g = ex_store.register_repo("octo-org", "gadgets", now=T0)
            ex_s = ex_store.register_repo("octo-org", "sprockets", now=T0)
            _seed_repo_mirror_artifacts(
                ex_store, ex_g, "g_dest_newer", watermark="2027-03-01T00:00:00Z"
            )
            _seed_repo_mirror_artifacts(
                ex_store,
                ex_s,
                "s_dest_git",
                watermark="2027-01-15T00:00:00Z",
                include_git=True,
            )
            ex_store.wal_checkpoint_truncate()
        finally:
            ex_store.close()
        init_sql_path = existing_dir / "init.sql"
        init_sql_path.unlink(missing_ok=True)
        bytes_before = existing_dest.read_bytes()

        existing_preview = preview_import_database(existing_dest, src_exp)
        self.assertTrue(existing_preview.dry_run)
        self.assertEqual(
            [(o.full_name, o.disposition, o.preserved_local_git) for o in existing_preview.outcomes],
            [
                ("octo-org/widgets", "promoted", False),
                ("octo-org/gadgets", "skipped_older", False),
                ("octo-org/sprockets", "replaced", True),
                ("octo-org/anvil", "added", False),
            ],
        )
        self.assertEqual(existing_preview.chunks_imported, 9)
        self.assertEqual(existing_preview.embeddings_imported, 9)
        self.assertEqual(existing_dest.read_bytes(), bytes_before)
        self.assertFalse(init_sql_path.exists())
        wal_path = Path(f"{existing_dest}-wal")
        self.assertEqual(wal_path.stat().st_size if wal_path.exists() else 0, 0)

    def test_mid_batch_failure_rolls_back_inflight_repo_and_chains_cause(self) -> None:
        """Rolls back only the failing repository, preserves prior commits, and chains `__cause__`."""
        dest_g = self.dest_store.register_repo("octo-org", "gadgets", now=T0)
        _seed_repo_mirror_artifacts(
            self.dest_store,
            dest_g,
            "gadgets_keep_on_rollback",
            watermark="2027-01-05T00:00:00Z",
        )

        _, src_store = self._new_source_store("abort_src_raw.sqlite")
        abort_src = self.tmp_dir / "sources" / "abort_src.sqlite"
        try:
            w = src_store.register_repo("octo-org", "widgets", now=T0)
            g = src_store.register_repo("octo-org", "gadgets", now=T0)
            s = src_store.register_repo("octo-org", "sprockets", now=T0)
            _seed_repo_mirror_artifacts(
                src_store, w, "widgets_committed", watermark="2027-02-15T00:00:00Z"
            )
            _seed_repo_mirror_artifacts(
                src_store, g, "gadgets_boom", watermark="2027-02-15T00:00:00Z"
            )
            _seed_repo_mirror_artifacts(
                src_store, s, "sprockets_unreached", watermark="2027-02-15T00:00:00Z"
            )
            src_store.export_database(abort_src, now=T0 + 10)
        finally:
            src_store.close()

        real_rebuild_fts = db_import._rebuild_fts
        calls = 0

        def skip_fts_on_second_repo(
            conn: sqlite3.Connection, dest_id: int, preserve_git: bool
        ) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                return
            real_rebuild_fts(conn, dest_id, preserve_git)

        buf = io.StringIO()
        reporter = StderrProgressReporter(
            "plain", stream=buf, is_tty=False, grace_sec=0.0, plain_interval_sec=0.0
        )
        with (
            mock.patch("db_import._rebuild_fts", side_effect=skip_fts_on_second_repo),
            mock.patch("db_import.LONG_COPY_WARN_SEC", 0.0),
            self.assertRaises(ImportAbortedError) as ctx_err,
        ):
            self.dest_store.import_database(
                abort_src, reporter=reporter, now=T0 + 100
            )

        err = ctx_err.exception
        self.assertIsInstance(err.__cause__, ImportIntegrityError)
        self.assertFalse(self.dest_store.conn.in_transaction)
        attached = [
            str(r[1]) for r in self.dest_store.conn.execute("PRAGMA database_list")
        ]
        self.assertNotIn("src_db", attached)

        partial = err.summary
        self.assertEqual(partial.added_count, 1)
        self.assertEqual(partial.failed_count, 1)
        self.assertEqual(partial.not_attempted_count, 1)
        self.assertEqual(
            [(o.full_name, o.disposition) for o in partial.outcomes],
            [
                ("octo-org/widgets", "added"),
                ("octo-org/gadgets", "failed"),
                ("octo-org/sprockets", "not_attempted"),
            ],
        )
        self.assertIn("chunks_fts", partial.outcomes[1].error or "")
        self.assertIsNone(partial.outcomes[2].error)
        self.assertIn("holds the write lock", buf.getvalue())
        self.assertGreater(IMPORT_SEC_PER_GIB, 0.0)
        self.assertGreater(LONG_COPY_WARN_SEC, 0.0)

        # Repo 1 stayed committed, repo 2 rolled back to its original rows, repo 3 absent.
        _assert_db_integrity(self, self.dest_store.conn)
        self.assertIsNotNone(self.dest_store.get_repo("octo-org/widgets"))
        self.assertTrue(
            hybrid_rrf_search(self.dest_store, "widgets_committed", mode="fts")
        )
        self.assertTrue(
            hybrid_rrf_search(self.dest_store, "gadgets_keep_on_rollback", mode="fts")
        )
        self.assertEqual(
            hybrid_rrf_search(self.dest_store, "gadgets_boom", mode="fts"), []
        )
        self.assertIsNone(self.dest_store.get_repo("octo-org/sprockets"))

    def test_import_v13_snapshot_resets_fingerprint_applies_v14_fixups_and_reindexes(
        self,
    ) -> None:
        import subprocess

        from db_migrations import MIGRATIONS, _run_migration, refresh_views
        from git_indexer import sync_git_codebase
        from parsers.python.symbol_parser import parse_python
        from symbol_parsers import ParsedFile, ParsedSymbol, SymbolParsers

        checkout = self.tmp_dir / "v13-import-work"
        subprocess.run(["git", "init", "-b", "main", str(checkout)], check=True)
        subprocess.run(["git", "-C", str(checkout), "config", "user.name", "T"], check=True)
        subprocess.run(["git", "-C", str(checkout), "config", "user.email", "t@e"], check=True)
        (checkout / "mod.py").write_text(
            'def greet() -> str:\n    """Say hi."""\n    return "hi"\n',
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(checkout), "commit", "-m", "feat: add mod.py"], check=True
        )
        head_sha = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        dest_w = self.dest_store.register_repo(
            "octo-org", "widgets", local_path=str(checkout), now=T0
        )
        self.dest_store.set_parser_fingerprint(dest_w.repo_id, ".py=py-ast@1", now=T0)

        v13_path = self.tmp_dir / "sources" / "legacy_v13.sqlite"
        v13_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(v13_path))
        try:
            for ver, mig in MIGRATIONS[:13]:
                _run_migration(conn, ver, mig)
            refresh_views(conn)
            conn.execute(
                "INSERT INTO repos (repo_id, owner, name, full_name, mode, created_at, updated_at, last_indexed_sha, prs_watermark) "
                "VALUES (1, 'octo-org', 'widgets', 'octo-org/widgets', 'mirror', ?, ?, ?, '2027-04-01T00:00:00Z')",
                ("2027-01-01T00:00:00Z", "2027-04-01T00:00:00Z", head_sha),
            )
            conn.execute(
                "INSERT INTO code_symbols (repo_id, file_path, symbol_name, kind, line_start, line_end, signature, docstring, sha, blob_oid) "
                "VALUES (1, 'mod.py', 'greet', 'function', 1, 3, 'def greet() -> str', '', ?, 'oldblob')",
                (head_sha,),
            )
            conn.execute(
                "INSERT INTO indexed_files (repo_id, file_path, blob_oid, sha) "
                "VALUES (1, 'mod.py', 'oldblob', ?), (1, 'README.md', 'docblob', ?)",
                (head_sha, head_sha),
            )
            conn.execute(
                "INSERT INTO chunks (chunk_id, repo_id, source_kind, source_ref, file_path, title, content_sha256, is_untrusted, text, updated_at) "
                "VALUES ('octo-org/widgets@symbol:mod.py:greet:1', 1, 'code_symbol', 'mod.py#L1-L3', 'mod.py', 'function greet', 'csha', 0, 'def greet', 'now')"
            )
            conn.commit()
        finally:
            conn.close()

        summary = self.dest_store.import_database(v13_path, allow_older=True, now=T0 + 200)
        self.assertEqual(summary.replaced_count, 1)

        imported = self.dest_store.get_repo("octo-org/widgets")
        assert imported is not None
        self.assertIsNone(imported.parser_fingerprint)
        idx_map = self.dest_store.get_indexed_files(imported.repo_id)
        self.assertEqual(idx_map["mod.py"][1], "regex@1")
        self.assertEqual(idx_map["README.md"][1], "md-sections@1")
        rewritten = self.dest_store.conn.execute(
            "SELECT chunk_id FROM chunks WHERE repo_id = ?", (imported.repo_id,)
        ).fetchone()
        self.assertEqual(
            rewritten[0], "octo-org/widgets@symbol:mod.py:function:greet:1"
        )

        # Next sync sees parser_fingerprint NULL != py-ast@1 and re-indexes mod.py with docstrings
        stats = sync_git_codebase(self.dest_store, imported)
        self.assertEqual(stats.symbols_upserted, 1)
        sym_row = self.dest_store.conn.execute(
            "SELECT docstring FROM code_symbols WHERE repo_id = ? AND file_path = 'mod.py'",
            (imported.repo_id,),
        ).fetchone()
        self.assertEqual(sym_row[0], "Say hi.")

    def test_v15_import_git_source_precedence_and_local_git_poll_preservation(self) -> None:
        src_db_path = Path(self.tmp_dir) / "v15_src.sqlite"
        src_store = PackratStore(src_db_path)
        try:
            src_store.register_repo("octo", "keep-path", local_path="/sender/kp", now=T0)
            src_store.register_repo("octo", "keep-mgr", git_source="managed", now=T0)
            src_store.register_repo("octo", "from-none", git_source="none", now=T0)
            src_store.register_repo("octo", "new-mgr", git_source="managed", git_url="git@github.com:octo/new-mgr.git", now=T0)
            src_store.register_repo("octo", "override-path", git_source="managed", now=T0)
            # Export nulls git_url and maps keep-path -> managed.
            exported_path = Path(self.tmp_dir) / "v15_exp.sqlite"
            src_store.export_database(exported_path, now=T0 + 1)
        finally:
            src_store.close()

        d_path = self.dest_store.register_repo("octo", "keep-path", local_path="/dest/kp", now=T0)
        d_mgr = self.dest_store.register_repo("octo", "keep-mgr", git_source="managed", git_url="git@github.com:octo/keep-mgr.git", now=T0)
        self.dest_store.register_repo("octo", "from-none", git_source="managed", now=T0)
        self.dest_store.set_state(f"git_poll:{d_mgr.repo_id}", '{"remote_sha":"local_sha"}', now=T0)

        summary = self.dest_store.import_database(exported_path, allow_older=True, now=T0 + 10)
        self.assertEqual(summary.added_count + summary.replaced_count, 5)
        self.assertEqual(self.dest_store.get_repo("octo/keep-path").git_source, "path")  # type: ignore[union-attr]
        self.assertEqual(self.dest_store.get_repo("octo/keep-path").local_path, "/dest/kp")  # type: ignore[union-attr]
        # Destination managed binding wins even when source from-none was git_source='none'.
        self.assertEqual(self.dest_store.get_repo("octo/from-none").git_source, "managed")  # type: ignore[union-attr]
        # Destination git_url and git_poll:<id> survive import; source git_url never copies.
        self.assertEqual(self.dest_store.get_repo("octo/keep-mgr").git_url, "git@github.com:octo/keep-mgr.git")  # type: ignore[union-attr]
        self.assertEqual(self.dest_store.get_state(f"git_poll:{d_mgr.repo_id}"), '{"remote_sha":"local_sha"}')
        self.assertEqual(self.dest_store.get_repo("octo/new-mgr").git_source, "managed")  # type: ignore[union-attr]
        self.assertIsNone(self.dest_store.get_repo("octo/new-mgr").git_url)  # type: ignore[union-attr]

        # Single-repo --path overrides any prior source/dest binding to path.
        override_dir = Path(self.tmp_dir) / "override_co"
        (override_dir / ".git").mkdir(parents=True)
        self.dest_store.import_database(
            exported_path,
            repo_tokens=["octo/override-path"],
            local_path=str(override_dir),
            allow_older=True,
            now=T0 + 20,
        )
        ov = self.dest_store.get_repo("octo/override-path")
        assert ov is not None
        self.assertEqual(ov.git_source, "path")
        self.assertEqual(ov.local_path, str(override_dir.resolve()))
        self.assertIsNone(d_path.git_url)


if __name__ == "__main__":
    unittest.main()
