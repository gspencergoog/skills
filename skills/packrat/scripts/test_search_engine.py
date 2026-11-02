"""Unit tests for packrat hybrid Reciprocal Rank Fusion (FTS5 + cosine) search engine."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from db_store import PackratStore
from embedder import DeterministicHashEmbedder, sync_repo_embeddings
from models import (
    IssueRecord,
    SearchFilters,
    TextChunk,
    compute_content_sha256,
    utc_now_iso,
)
from search_engine import hybrid_rrf_search, sanitize_fts5_query


class TestSearchEngine(unittest.TestCase):
    """Tests FTS5 query sanitization, RRF ranking, untrusted framing, and filters."""

    def test_sanitize_fts5_query_strips_syntax_hazards(self) -> None:
        expr = sanitize_fts5_query('SurfaceController::render() && --flag "Fixes #101"')
        self.assertIn('"surfacecontroller"', expr)
        self.assertIn('"render"', expr)
        self.assertNotIn("::", expr)
        self.assertNotIn("&&", expr)

    def test_hybrid_rrf_ranking_filters_and_untrusted_framing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PackratStore(Path(tmp) / "repos.sqlite")
            try:
                store.register_repo("octo-org", "widgets")
                store.register_repo("octo-org", "gadgets")
                widgets = store.get_repo("octo-org/widgets")
                gadgets = store.get_repo("octo-org/gadgets")
                assert widgets is not None and gadgets is not None
                now = utc_now_iso()

                store.upsert_issues(
                    [
                        IssueRecord(
                            repo_id=widgets.repo_id,
                            number=42,
                            title="OAuth2 bearer token refresh race condition",
                            state="OPEN",
                            updated_at=now,
                            body="Concurrent surface requests fail when OAuth2 bearer token expires.",
                        ),
                        IssueRecord(
                            repo_id=widgets.repo_id,
                            number=43,
                            title="Legacy closed auth issue",
                            state="CLOSED",
                            updated_at=now,
                            body="OAuth2 bearer token refresh old bug.",
                        ),
                    ],
                    [],
                )

                chunks = [
                    # Matches BOTH lexical ("OAuth2 bearer token") and semantic auth cluster
                    TextChunk(
                        chunk_id="octo-org/widgets#issue:42",
                        repo_id=widgets.repo_id,
                        source_kind="issue",
                        source_ref="octo-org/widgets#42",
                        item_number=42,
                        title="Issue #42: OAuth2 bearer token refresh race condition",
                        text="Concurrent surface requests fail when OAuth2 bearer token expires.",
                        content_sha256=compute_content_sha256(
                            "Issue #42", "Concurrent surface requests fail when OAuth2 bearer token expires."
                        ),
                        is_untrusted=True,
                        updated_at=now,
                    ),
                    # Closed issue
                    TextChunk(
                        chunk_id="octo-org/widgets#issue:43",
                        repo_id=widgets.repo_id,
                        source_kind="issue",
                        source_ref="octo-org/widgets#43",
                        item_number=43,
                        title="Issue #43: Legacy closed auth issue",
                        text="OAuth2 bearer token refresh old bug.",
                        content_sha256=compute_content_sha256("Issue #43", "OAuth2 bearer token refresh old bug."),
                        is_untrusted=True,
                        updated_at=now,
                    ),
                    # Trusted code symbol in src/auth/token.ts
                    TextChunk(
                        chunk_id="octo-org/widgets@symbol:src/auth/token.ts:refreshBearerToken:10",
                        repo_id=widgets.repo_id,
                        source_kind="code_symbol",
                        source_ref="src/auth/token.ts#L10-L25",
                        file_path="src/auth/token.ts",
                        title="function refreshBearerToken in src/auth/token.ts",
                        text="export async function refreshBearerToken(sessionCredential: string): Promise<string>",
                        content_sha256=compute_content_sha256(
                            "refreshBearerToken",
                            "export async function refreshBearerToken(sessionCredential: string): Promise<string>",
                        ),
                        is_untrusted=False,
                        updated_at=now,
                    ),
                    # Other repo chunk (octo-org/gadgets)
                    TextChunk(
                        chunk_id="octo-org/gadgets@symbol:lib/auth.dart:AuthClient:1",
                        repo_id=gadgets.repo_id,
                        source_kind="code_symbol",
                        source_ref="lib/auth.dart#L1-L20",
                        file_path="lib/auth.dart",
                        title="class AuthClient in lib/auth.dart",
                        text="class AuthClient handles OAuth2 bearer token refresh in Flutter",
                        content_sha256=compute_content_sha256(
                            "AuthClient", "class AuthClient handles OAuth2 bearer token refresh in Flutter"
                        ),
                        is_untrusted=False,
                        updated_at=now,
                    ),
                ]
                store.upsert_chunks(chunks)

                embedder = DeterministicHashEmbedder()
                sync_repo_embeddings(store, None, embedder=embedder)

                hits = hybrid_rrf_search(
                    store,
                    "OAuth2 bearer token refresh::race()",
                    filters=SearchFilters(repo="octo-org/widgets"),
                    mode="hybrid",
                    embedder=embedder,
                )
                self.assertGreaterEqual(len(hits), 2)
                self.assertEqual(hits[0].chunk_id, "octo-org/widgets#issue:42")
                self.assertIsNotNone(hits[0].bm25_rank)
                self.assertIsNotNone(hits[0].vector_rank)
                self.assertTrue(hits[0].is_untrusted)
                self.assertIn('<untrusted_repo_content source="octo-org/widgets#42">', hits[0].snippet)

                # Trusted code symbol must NOT have untrusted XML tags
                code_hits = [h for h in hits if h.source_kind == "code_symbol"]
                self.assertEqual(len(code_hits), 1)
                self.assertFalse(code_hits[0].is_untrusted)
                self.assertNotIn("<untrusted_repo_content", code_hits[0].snippet)

                # Filter by state=OPEN excludes closed issue #43
                open_hits = hybrid_rrf_search(
                    store,
                    "OAuth2 bearer token",
                    filters=SearchFilters(repo="octo-org/widgets", kinds=("issue",), state="OPEN"),
                    mode="hybrid",
                    embedder=embedder,
                )
                self.assertEqual([h.item_number for h in open_hits], [42])

                # Filter by path_prefix="src/auth"
                path_hits = hybrid_rrf_search(
                    store,
                    "credential token",
                    filters=SearchFilters(repo="octo-org/widgets", path_prefix="src/auth"),
                    mode="vector",
                    embedder=embedder,
                )
                self.assertEqual(len(path_hits), 1)
                self.assertEqual(path_hits[0].file_path, "src/auth/token.ts")

                # Verify path_prefix escapes SQL LIKE '_' wildcard (M3)
                wildcard_hits = hybrid_rrf_search(
                    store,
                    "credential token",
                    filters=SearchFilters(repo="octo-org/widgets", path_prefix="src_auth"),
                    mode="vector",
                    embedder=embedder,
                )
                self.assertEqual(len(wildcard_hits), 0)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
