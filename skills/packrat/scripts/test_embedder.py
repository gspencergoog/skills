"""Unit tests for packrat offline embedders and incremental content_sha256 skip logic."""

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from db_store import PackratStore
from embedder import (
    _CLUSTER_STEM_PAIRS,
    ARCTIC_MODEL_ID,
    CONCEPT_CLUSTERS,
    CONCEPT_DIM,
    DETERMINISTIC_MODEL_ID,
    HASH_DIM,
    DeterministicHashEmbedder,
    _bigram_delta,
    _char_ngram_deltas,
    _ensure_onnx_deps,
    _fnv1a,
    _hash_sign,
    _stem_token,
    _tokenize,
    _unigram_delta,
    embed_structured_deterministic,
    embed_text_deterministic,
    ensure_onnx_deps,
    ensure_onnx_model,
    get_default_embedder,
    get_default_embedder_model_id,
    reset_onnx_deps_cache,
    sync_repo_embeddings,
)
from models import (
    EMBEDDING_DIM,
    StoredEmbeddingRecord,
    TextChunk,
    compute_content_sha256,
    l2_normalize,
    utc_now_iso,
)


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class TestEmbedder(unittest.TestCase):
    """Tests deterministic 384-dim embeddings, ONNX fallback, and incremental sync."""

    def test_deterministic_vector_dims_and_semantic_clustering(self) -> None:
        vec_auth_1 = embed_text_deterministic(
            "OAuth2 bearer token authentication and JWT session credentials"
        )
        vec_auth_2 = embed_text_deterministic(
            "SSO login permission RBAC token secret authentication"
        )
        vec_db = embed_text_deterministic(
            "SQLite WAL database schema migration and SQL table index"
        )

        self.assertEqual(len(vec_auth_1), EMBEDDING_DIM)
        self.assertAlmostEqual(math.sqrt(sum(x * x for x in vec_auth_1)), 1.0, places=5)

        sim_related = _cosine(vec_auth_1, vec_auth_2)
        sim_unrelated = _cosine(vec_auth_1, vec_db)
        self.assertGreater(sim_related, 0.35)
        self.assertGreater(sim_related, sim_unrelated + 0.25)

    def test_structured_blending_and_canonical_phrases(self) -> None:
        vec = embed_structured_deterministic(
            "Fix pull request rate limit on side pane",
            "When polling GitHub pull requests, avoid rate limiting errors in the side pane UI.",
            "pull_request",
        )
        self.assertEqual(len(vec), EMBEDDING_DIM)
        self.assertAlmostEqual(math.sqrt(sum(x * x for x in vec)), 1.0, places=5)

        query_vec = embed_text_deterministic("pull_request rate_limit side_pane")
        self.assertGreater(_cosine(vec, query_vec), 0.4)

        embedder = DeterministicHashEmbedder()
        doc_with_para = "First paragraph\n\nSecond paragraph with token"
        self.assertEqual(
            embedder.embed_texts([doc_with_para])[0],
            embed_text_deterministic(doc_with_para),
        )

    def test_incremental_embedding_skips_unchanged_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PackratStore(Path(tmp) / "repos.sqlite")
            try:
                store.register_repo("octo-org", "widgets")
                repo = store.get_repo("octo-org/widgets")
                self.assertIsNotNone(repo)
                assert repo is not None

                now = utc_now_iso()
                c1_text = "Surface controller manages widget component state"
                c2_text = "Markdown parser extracts headings and sections"
                chunks = [
                    TextChunk(
                        chunk_id="octo-org/widgets:symbol:src/a.ts:1",
                        repo_id=repo.repo_id,
                        source_kind="code_symbol",
                        source_ref="src/a.ts#L1-L10",
                        file_path="src/a.ts",
                        item_number=None,
                        title="SurfaceController",
                        text=c1_text,
                        content_sha256=compute_content_sha256(
                            "SurfaceController", c1_text
                        ),
                        is_untrusted=False,
                        updated_at=now,
                    ),
                    TextChunk(
                        chunk_id="octo-org/widgets:symbol:src/b.ts:1",
                        repo_id=repo.repo_id,
                        source_kind="code_symbol",
                        source_ref="src/b.ts#L1-L10",
                        file_path="src/b.ts",
                        item_number=None,
                        title="MarkdownParser",
                        text=c2_text,
                        content_sha256=compute_content_sha256(
                            "MarkdownParser", c2_text
                        ),
                        is_untrusted=False,
                        updated_at=now,
                    ),
                ]
                store.upsert_chunks(chunks)

                embedder = DeterministicHashEmbedder()
                first_res = sync_repo_embeddings(
                    store, repo.repo_id, embedder=embedder
                )
                self.assertEqual(first_res.embedded_count, 2)
                self.assertFalse(first_res.capped)

                # Second sync with identical content_sha256 must embed 0 chunks
                with mock.patch.object(
                    embedder, "embed_chunks", wraps=embedder.embed_chunks
                ) as spy:
                    second_res = sync_repo_embeddings(
                        store, repo.repo_id, embedder=embedder
                    )
                    self.assertEqual(second_res.embedded_count, 0)
                    self.assertFalse(second_res.capped)
                    spy.assert_not_called()

                # Modify only chunk 1's text & sha256; only 1 chunk should be re-embedded
                c1_updated = (
                    "Surface controller manages widget component state and focus rings"
                )
                store.upsert_chunks(
                    [
                        TextChunk(
                            chunk_id="octo-org/widgets:symbol:src/a.ts:1",
                            repo_id=repo.repo_id,
                            source_kind="code_symbol",
                            source_ref="src/a.ts#L1-L10",
                            file_path="src/a.ts",
                            item_number=None,
                            title="SurfaceController",
                            text=c1_updated,
                            content_sha256=compute_content_sha256(
                                "SurfaceController", c1_updated
                            ),
                            is_untrusted=False,
                            updated_at=utc_now_iso(),
                        )
                    ]
                )
                third_res = sync_repo_embeddings(
                    store, repo.repo_id, embedder=embedder
                )
                self.assertEqual(third_res.embedded_count, 1)
                self.assertFalse(third_res.capped)

                emb = store.get_embedding("octo-org/widgets:symbol:src/a.ts:1")
                self.assertIsNotNone(emb)
                assert emb is not None
                self.assertEqual(emb.model_id, DETERMINISTIC_MODEL_ID)
                self.assertEqual(len(emb.vector), EMBEDDING_DIM)
            finally:
                store.close()

    def test_fallback_when_onnx_model_dir_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            empty_dir = Path(tmp) / "no-model"
            self.assertFalse(ensure_onnx_model(empty_dir, allow_download=False))
            embedder = get_default_embedder(model_dir=empty_dir, prefer_onnx=True)
            self.assertEqual(embedder.model_id, DETERMINISTIC_MODEL_ID)
            q_vec = embedder.embed_query("how does surface controller work")
            self.assertEqual(len(q_vec), EMBEDDING_DIM)
            self.assertTrue(ensure_onnx_deps())
            self.assertTrue(_ensure_onnx_deps())

    def test_batch_progress_and_onnx_init_warning(self) -> None:
        warns: list[tuple[str, str]] = []
        advances: list[tuple[int, int | None]] = []

        class SpyReporter:
            def start_phase(
                self, repo: str, phase: str, total: int | None = None
            ) -> None:
                pass

            def advance(
                self, completed: int, total: int | None = None, *, detail: str = ""
            ) -> None:
                advances.append((completed, total))

            def finish_phase(self, summary: str = "") -> None:
                pass

            def warn(self, repo: str, message: str) -> None:
                warns.append((repo, message))

            def error(self, repo: str, message: str) -> None:
                pass

            def clear_active_line(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Create corrupt ONNX files to trigger fallback warning
            (tmp_path / "tokenizer.json").write_text("not-json", encoding="utf-8")
            (tmp_path / "model.onnx").write_bytes(b"corrupt")
            spy = SpyReporter()
            emb = get_default_embedder(
                model_dir=tmp_path, prefer_onnx=True, reporter=spy
            )
            self.assertEqual(emb.model_id, DETERMINISTIC_MODEL_ID)
            self.assertEqual(len(warns), 1)
            self.assertIn("ONNX runtime init failed", warns[0][1])

            store = PackratStore(tmp_path / "repos.sqlite")
            try:
                repo = store.register_repo("octo-org", "widgets")
                now = utc_now_iso()
                chunks = [
                    TextChunk(
                        chunk_id=f"octo-org/widgets:symbol:src/f{i}.ts:1",
                        repo_id=repo.repo_id,
                        source_kind="code_symbol",
                        source_ref=f"src/f{i}.ts#L1",
                        file_path=f"src/f{i}.ts",
                        item_number=None,
                        title=f"Sym{i}",
                        text=f"Function body {i}",
                        content_sha256=compute_content_sha256(
                            f"Sym{i}", f"Function body {i}"
                        ),
                        is_untrusted=False,
                        updated_at=now,
                    )
                    for i in range(5)
                ]
                store.upsert_chunks(chunks)
                count = sync_repo_embeddings(
                    store,
                    repo.repo_id,
                    embedder=emb,
                    batch_size=2,
                    reporter=spy,
                    repo_label="octo-org/widgets",
                )
                self.assertEqual(count.embedded_count, 5)
                self.assertFalse(count.capped)
                self.assertEqual(advances, [(2, 5), (4, 5), (5, 5)])
            finally:
                store.close()

    def test_max_chunks_cap_stops_early_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PackratStore(Path(tmp) / "repos.sqlite")
            try:
                repo = store.register_repo("octo-org", "widgets")
                now = utc_now_iso()
                store.upsert_chunks(
                    [
                        TextChunk(
                            chunk_id=f"octo-org/widgets:symbol:src/c{i}.ts:1",
                            repo_id=repo.repo_id,
                            source_kind="code_symbol",
                            source_ref=f"src/c{i}.ts#L1",
                            file_path=f"src/c{i}.ts",
                            item_number=None,
                            title=f"Sym{i}",
                            text=f"Body {i}",
                            content_sha256=compute_content_sha256(f"Sym{i}", f"Body {i}"),
                            is_untrusted=False,
                            updated_at=now,
                        )
                        for i in range(5)
                    ]
                )
                emb = DeterministicHashEmbedder()
                first = sync_repo_embeddings(
                    store, repo.repo_id, embedder=emb, batch_size=2, max_chunks=3
                )
                self.assertEqual(first.embedded_count, 3)
                self.assertTrue(first.capped)

                second = sync_repo_embeddings(
                    store, repo.repo_id, embedder=emb, batch_size=2, max_chunks=3
                )
                self.assertEqual(second.embedded_count, 2)
                self.assertFalse(second.capped)
            finally:
                store.close()

    def test_ensure_onnx_model_atomic_download_and_failure_cleanup(self) -> None:
        self.assertEqual(ARCTIC_MODEL_ID, "snowflake-arctic-embed-xs")
        with tempfile.TemporaryDirectory() as tmp:
            model_dir = Path(tmp) / "arctic-xs"
            downloaded_urls: list[str] = []

            def fake_retrieve(url: str, dest: Path, timeout: float = 30.0) -> None:
                downloaded_urls.append(url)
                self.assertIn(".tmp.", dest.name)
                Path(dest).write_bytes(b"fake-bytes")

            with mock.patch("embedder.ensure_onnx_deps", return_value=False):
                self.assertFalse(ensure_onnx_model(model_dir, allow_download=True))

            with mock.patch(
                "embedder._download_url_to_path", side_effect=fake_retrieve
            ):
                ok = ensure_onnx_model(model_dir, allow_download=True)
            self.assertTrue(ok)
            self.assertEqual(len(downloaded_urls), 2)
            self.assertTrue(
                all("Snowflake/snowflake-arctic-embed-xs" in u for u in downloaded_urls)
            )
            self.assertTrue((model_dir / "tokenizer.json").exists())
            self.assertTrue((model_dir / "onnx" / "model_quantized.onnx").exists())
            self.assertEqual(list(model_dir.rglob("*.tmp.*")), [])

            fail_dir = Path(tmp) / "arctic-fail"

            def fail_retrieve(url: str, dest: Path, timeout: float = 30.0) -> None:
                Path(dest).write_bytes(b"partial")
                raise OSError("network interrupted")

            with mock.patch(
                "embedder._download_url_to_path", side_effect=fail_retrieve
            ):
                emb = get_default_embedder(
                    model_dir=fail_dir, prefer_onnx=True, allow_download=True
                )
            self.assertEqual(emb.model_id, DETERMINISTIC_MODEL_ID)
            self.assertEqual(list(fail_dir.rglob("*.tmp.*")), [])

    def test_lazy_model_id_and_zero_pending_skips_onnx_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            empty_dir = tmp_path / "empty-model"
            onnx_dir = tmp_path / "present-model"
            onnx_dir.mkdir(parents=True)
            (onnx_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
            (onnx_dir / "model.onnx").write_bytes(b"fake-onnx")

            with (
                mock.patch("embedder.ensure_onnx_deps", return_value=True),
                mock.patch("embedder.OnnxArcticEmbedder") as mock_onnx_cls,
            ):
                self.assertEqual(
                    get_default_embedder_model_id(model_dir=empty_dir),
                    DETERMINISTIC_MODEL_ID,
                )
                self.assertEqual(
                    get_default_embedder_model_id(model_dir=onnx_dir),
                    ARCTIC_MODEL_ID,
                )
                self.assertEqual(
                    get_default_embedder(model_dir=empty_dir).model_id,
                    get_default_embedder_model_id(model_dir=empty_dir),
                )
                mock_onnx_cls.assert_not_called()

                store = PackratStore(tmp_path / "repos.sqlite")
                try:
                    repo = store.register_repo("octo-org", "widgets")
                    # 0 chunks in DB: must return 0 without instantiating OnnxArcticEmbedder
                    count_empty = sync_repo_embeddings(
                        store,
                        repo.repo_id,
                        embedder=None,
                        model_dir=onnx_dir,
                    )
                    self.assertEqual(count_empty.embedded_count, 0)
                    self.assertFalse(count_empty.capped)
                    mock_onnx_cls.assert_not_called()

                    # 1 chunk already embedded with ARCTIC_MODEL_ID: still 0 pending -> no ONNX init
                    now = utc_now_iso()
                    sha = compute_content_sha256("Sym", "Body")
                    chunk = TextChunk(
                        chunk_id="octo-org/widgets:symbol:src/a.ts:1",
                        repo_id=repo.repo_id,
                        source_kind="code_symbol",
                        source_ref="src/a.ts#L1",
                        file_path="src/a.ts",
                        item_number=None,
                        title="Sym",
                        text="Body",
                        content_sha256=sha,
                        is_untrusted=False,
                        updated_at=now,
                    )
                    store.upsert_chunks([chunk])
                    store.upsert_embeddings(
                        [
                            StoredEmbeddingRecord(
                                chunk_id=chunk.chunk_id,
                                model_id=ARCTIC_MODEL_ID,
                                content_sha256=sha,
                                vector=tuple(embed_text_deterministic("Sym Body")),
                                updated_at=now,
                            )
                        ]
                    )
                    count_up_to_date = sync_repo_embeddings(
                        store,
                        repo.repo_id,
                        embedder=None,
                        model_dir=onnx_dir,
                    )
                    self.assertEqual(count_up_to_date.embedded_count, 0)
                    self.assertFalse(count_up_to_date.capped)
                    mock_onnx_cls.assert_not_called()
                finally:
                    store.close()

    def test_negative_ensure_onnx_deps_memoizes_and_reset_clears(self) -> None:
        reset_onnx_deps_cache()
        try:
            failed_proc = mock.Mock(returncode=1, stdout="", stderr="offline")
            with (
                mock.patch("embedder._can_import_onnx_deps", return_value=False),
                mock.patch("embedder._resolve_uv_binary", return_value="/usr/bin/uv"),
                mock.patch(
                    "embedder.subprocess.run", return_value=failed_proc
                ) as mock_run,
            ):
                self.assertFalse(ensure_onnx_deps())
                self.assertFalse(ensure_onnx_deps())
                self.assertFalse(ensure_onnx_deps())
                self.assertEqual(mock_run.call_count, 1)

                reset_onnx_deps_cache()
                self.assertFalse(ensure_onnx_deps())
                self.assertEqual(mock_run.call_count, 2)
        finally:
            reset_onnx_deps_cache()

    def test_length_sorted_batching_orders_short_before_long(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PackratStore(Path(tmp) / "repos.sqlite")
            try:
                repo = store.register_repo("octo-org", "widgets")
                now = utc_now_iso()
                specs = [
                    ("c1_long", "AlphaTitle", "x" * 600),
                    ("c2_short", "B", "short"),
                    ("c3_long", "GammaTitle", "y" * 900),
                    ("c4_short", "D", "tiny text"),
                ]
                chunks = [
                    TextChunk(
                        chunk_id=f"octo-org/widgets:symbol:{cid}",
                        repo_id=repo.repo_id,
                        source_kind="code_symbol",
                        source_ref=f"src/{cid}.ts#L1",
                        file_path=f"src/{cid}.ts",
                        item_number=None,
                        title=title,
                        text=body,
                        content_sha256=compute_content_sha256(title, body),
                        is_untrusted=False,
                        updated_at=now,
                    )
                    for cid, title, body in specs
                ]
                store.upsert_chunks(chunks)

                embedder = DeterministicHashEmbedder()
                observed_batches: list[list[str]] = []
                orig_embed = embedder.embed_chunks

                def record_batches(
                    batch: list[TextChunk],
                ) -> list[list[float]]:
                    observed_batches.append(
                        [ch.chunk_id.split(":")[-1] for ch in batch]
                    )
                    return orig_embed(batch)

                with mock.patch.object(
                    embedder, "embed_chunks", side_effect=record_batches
                ):
                    res = sync_repo_embeddings(
                        store,
                        repo.repo_id,
                        embedder=embedder,
                        batch_size=2,
                    )
                self.assertEqual(res.embedded_count, 4)
                self.assertFalse(res.capped)
                self.assertEqual(
                    observed_batches,
                    [["c2_short", "c4_short"], ["c1_long", "c3_long"]],
                )
            finally:
                store.close()

    def test_keyset_paging_and_single_vector_cache_seq_bump(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PackratStore(Path(tmp) / "repos.sqlite")
            try:
                repo = store.register_repo("octo-org", "widgets")
                now = utc_now_iso()
                chunks = [
                    TextChunk(
                        chunk_id=f"octo-org/widgets:symbol:src/f{i:02d}.ts:1",
                        repo_id=repo.repo_id,
                        source_kind="code_symbol",
                        source_ref=f"src/f{i:02d}.ts#L1",
                        file_path=f"src/f{i:02d}.ts",
                        item_number=None,
                        title=f"Sym{i}",
                        text=f"Body text for symbol {i} " * ((i % 3) + 1),
                        content_sha256=compute_content_sha256(
                            f"Sym{i}", f"Body text for symbol {i} " * ((i % 3) + 1)
                        ),
                        is_untrusted=False,
                        updated_at=now,
                    )
                    for i in range(7)
                ]
                store.upsert_chunks(chunks)

                def read_seq() -> int:
                    row = store.conn.execute(
                        "SELECT value FROM service_state WHERE key = 'vector_cache_seq'"
                    ).fetchone()
                    return int(row["value"]) if row else 0

                seq_before = read_seq()
                bump_flags: list[bool] = []
                page_calls: list[tuple[int | None, str | None]] = []
                orig_upsert = store.upsert_embeddings
                orig_get_unembedded = store.get_unembedded_chunks

                def spy_upsert(
                    records: list[StoredEmbeddingRecord],
                    *,
                    now: float | None = None,
                    allow_seq_bump: bool = True,
                ) -> None:
                    bump_flags.append(allow_seq_bump)
                    orig_upsert(records, now=now, allow_seq_bump=allow_seq_bump)

                def spy_get_unembedded(
                    repo_id: int | None,
                    model_id: str,
                    *,
                    limit: int | None = None,
                    after_chunk_id: str | None = None,
                ) -> list[TextChunk]:
                    page_calls.append((limit, after_chunk_id))
                    return orig_get_unembedded(
                        repo_id,
                        model_id,
                        limit=limit,
                        after_chunk_id=after_chunk_id,
                    )

                with (
                    mock.patch.object(
                        store, "upsert_embeddings", side_effect=spy_upsert
                    ),
                    mock.patch.object(
                        store, "get_unembedded_chunks", side_effect=spy_get_unembedded
                    ),
                ):
                    res = sync_repo_embeddings(
                        store,
                        repo.repo_id,
                        embedder=DeterministicHashEmbedder(),
                        batch_size=2,
                        page_size=3,
                    )

                self.assertEqual(res.embedded_count, 7)
                self.assertFalse(res.capped)
                # 7 chunks with page_size=3 -> pages of 3, 3, 1 (2 + 2 + 1 = 5 batches)
                self.assertEqual(len(page_calls), 3)
                self.assertEqual(page_calls[0], (3, None))
                self.assertEqual(
                    page_calls[1], (3, "octo-org/widgets:symbol:src/f02.ts:1")
                )
                self.assertEqual(
                    page_calls[2], (3, "octo-org/widgets:symbol:src/f05.ts:1")
                )
                self.assertEqual(bump_flags, [False, False, False, False, True])
                self.assertEqual(read_seq(), seq_before + 1)
                self.assertEqual(
                    store.get_unembedded_chunks(repo.repo_id, DETERMINISTIC_MODEL_ID),
                    [],
                )
            finally:
                store.close()

    def test_multi_page_progress_constant_total_clamping_and_missing_count_fallback(
        self,
    ) -> None:
        class SpyReporter:
            def __init__(self) -> None:
                self.starts: list[tuple[str, str, int | None]] = []
                self.advances: list[tuple[int, int | None]] = []

            def start_phase(
                self, repo: str, phase: str, total: int | None = None
            ) -> None:
                self.starts.append((repo, phase, total))

            def advance(
                self, completed: int, total: int | None = None, *, detail: str = ""
            ) -> None:
                self.advances.append((completed, total))

            def finish_phase(self, summary: str = "") -> None:
                pass

            def warn(self, repo: str, message: str) -> None:
                pass

            def error(self, repo: str, message: str) -> None:
                pass

            def clear_active_line(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as tmp:
            store = PackratStore(Path(tmp) / "repos.sqlite")
            try:
                repo = store.register_repo("octo-org", "widgets")
                now = utc_now_iso()
                chunks = [
                    TextChunk(
                        chunk_id=f"octo-org/widgets:symbol:src/p{i:02d}.ts:1",
                        repo_id=repo.repo_id,
                        source_kind="code_symbol",
                        source_ref=f"src/p{i:02d}.ts#L1",
                        file_path=f"src/p{i:02d}.ts",
                        item_number=None,
                        title=f"Sym{i}",
                        text=f"Body text for symbol {i}",
                        content_sha256=compute_content_sha256(
                            f"Sym{i}", f"Body text for symbol {i}"
                        ),
                        is_untrusted=False,
                        updated_at=now,
                    )
                    for i in range(7)
                ]
                store.upsert_chunks(chunks)
                self.assertEqual(
                    store.count_unembedded_chunks(repo.repo_id, DETERMINISTIC_MODEL_ID),
                    7,
                )
                self.assertEqual(
                    store.count_unembedded_chunks(None, DETERMINISTIC_MODEL_ID),
                    7,
                )

                # 1. Small single-page sync (len(current_page) < page_size) does not call count_unembedded_chunks
                with mock.patch.object(
                    store,
                    "count_unembedded_chunks",
                    wraps=store.count_unembedded_chunks,
                ) as spy_count:
                    spy_small = SpyReporter()
                    res_small = sync_repo_embeddings(
                        store,
                        repo.repo_id,
                        embedder=DeterministicHashEmbedder(),
                        batch_size=4,
                        page_size=10,
                        max_chunks=4,
                        reporter=spy_small,
                        repo_label="octo-org/widgets",
                    )
                    spy_count.assert_not_called()
                self.assertEqual(res_small.embedded_count, 4)
                self.assertTrue(res_small.capped)
                self.assertEqual(spy_small.starts[0][2], 4)
                self.assertEqual(spy_small.advances, [(4, 4)])

                # Re-dirty all 7 chunks and test max_chunks clamping across multiple pages (7 pending, page_size=3, max_chunks=4 -> total=4)
                v1_chunks = [
                    TextChunk(
                        chunk_id=ch.chunk_id,
                        repo_id=ch.repo_id,
                        source_kind=ch.source_kind,
                        source_ref=ch.source_ref,
                        file_path=ch.file_path,
                        item_number=ch.item_number,
                        title=ch.title,
                        text=ch.text + " v1",
                        content_sha256=compute_content_sha256(ch.title, ch.text + " v1"),
                        is_untrusted=ch.is_untrusted,
                        updated_at=now,
                    )
                    for ch in chunks
                ]
                store.upsert_chunks(v1_chunks)
                spy_clamped = SpyReporter()
                res_clamped = sync_repo_embeddings(
                    store,
                    repo.repo_id,
                    embedder=DeterministicHashEmbedder(),
                    batch_size=2,
                    page_size=3,
                    max_chunks=4,
                    reporter=spy_clamped,
                    repo_label="octo-org/widgets",
                )
                self.assertEqual(res_clamped.embedded_count, 4)
                self.assertTrue(res_clamped.capped)
                self.assertEqual(spy_clamped.starts[0][2], 4)
                self.assertEqual(
                    spy_clamped.advances, [(2, 4), (3, 4), (4, 4)]
                )

                # Re-dirty all 7 chunks by bumping content_sha256
                updated_chunks = [
                    TextChunk(
                        chunk_id=ch.chunk_id,
                        repo_id=ch.repo_id,
                        source_kind=ch.source_kind,
                        source_ref=ch.source_ref,
                        file_path=ch.file_path,
                        item_number=ch.item_number,
                        title=ch.title,
                        text=ch.text + " v2",
                        content_sha256=compute_content_sha256(ch.title, ch.text + " v2"),
                        is_untrusted=ch.is_untrusted,
                        updated_at=now,
                    )
                    for ch in chunks
                ]
                store.upsert_chunks(updated_chunks)

                # 2. Multi-page full run (7 pending, page_size=3, batch_size=2): constant total=7 across all 3 pages
                spy_full = SpyReporter()
                res_full = sync_repo_embeddings(
                    store,
                    repo.repo_id,
                    embedder=DeterministicHashEmbedder(),
                    batch_size=2,
                    page_size=3,
                    reporter=spy_full,
                    repo_label="octo-org/widgets",
                )
                self.assertEqual(res_full.embedded_count, 7)
                self.assertFalse(res_full.capped)
                self.assertEqual(spy_full.starts[0][2], 7)
                self.assertEqual(
                    spy_full.advances,
                    [(2, 7), (3, 7), (5, 7), (6, 7), (7, 7)],
                )

                # 3. Store double lacking count_unembedded_chunks falls back to total=None when paging
                class PagingDoubleWithoutCount:
                    def get_unembedded_chunks(
                        self,
                        repo_id: int | None,
                        model_id: str,
                        *,
                        limit: int | None = None,
                        after_chunk_id: str | None = None,
                    ) -> list[TextChunk]:
                        del repo_id, model_id, limit
                        if after_chunk_id is None:
                            return updated_chunks[:3]
                        if after_chunk_id == updated_chunks[2].chunk_id:
                            return updated_chunks[3:5]
                        return []

                    def upsert_embeddings(
                        self,
                        records: list[StoredEmbeddingRecord],
                        *,
                        allow_seq_bump: bool = True,
                    ) -> None:
                        del records, allow_seq_bump

                spy_no_count = SpyReporter()
                res_no_count = sync_repo_embeddings(
                    PagingDoubleWithoutCount(),  # type: ignore[arg-type]
                    repo.repo_id,
                    embedder=DeterministicHashEmbedder(),
                    batch_size=2,
                    page_size=3,
                    reporter=spy_no_count,
                    repo_label="octo-org/widgets",
                )
                self.assertEqual(res_no_count.embedded_count, 5)
                self.assertEqual(spy_no_count.starts[0][2], None)
                self.assertEqual(
                    spy_no_count.advances,
                    [(2, None), (3, None), (5, None)],
                )
            finally:
                store.close()

    def test_deterministic_hash_embedder_bit_identical_reference_vectors(self) -> None:
        self.assertEqual(len(_CLUSTER_STEM_PAIRS), len(CONCEPT_CLUSTERS))
        _stem_token.cache_clear()
        _unigram_delta.cache_clear()
        _bigram_delta.cache_clear()
        _char_ngram_deltas.cache_clear()

        def reference_embed(text: str) -> list[float]:
            vec = [0.0] * EMBEDDING_DIM
            tokens = _tokenize(text)
            if not tokens:
                return vec
            token_set = set(tokens)
            for c_idx, cluster in enumerate(CONCEPT_CLUSTERS):
                matches = sum(
                    1
                    for kw in cluster
                    if kw in token_set or _stem_token.__wrapped__(kw) in token_set
                )
                if matches > 0:
                    vec[c_idx] += 2.8 + min(matches - 1, 3) * 0.6
            for i, tok in enumerate(tokens):
                weight = 2.2 if any(ch == "_" or ch.isdigit() for ch in tok) else 1.6
                idx = CONCEPT_DIM + (_fnv1a(f"u:{tok}") % HASH_DIM)
                vec[idx] += _hash_sign(f"u:{tok}") * weight
                if i + 1 < len(tokens):
                    bigram = f"b:{tok}_{tokens[i + 1]}"
                    b_idx = CONCEPT_DIM + (_fnv1a(bigram) % HASH_DIM)
                    vec[b_idx] += _hash_sign(bigram) * 1.2
                if len(tok) >= 4:
                    for j in range(len(tok) - 2):
                        trigram = f"c3:{tok[j : j + 3]}"
                        c_idx = CONCEPT_DIM + (_fnv1a(trigram) % HASH_DIM)
                        vec[c_idx] += _hash_sign(trigram) * 0.25
                    for j in range(len(tok) - 3):
                        quadgram = f"c4:{tok[j : j + 4]}"
                        c4_idx = CONCEPT_DIM + (_fnv1a(quadgram) % HASH_DIM)
                        vec[c4_idx] += _hash_sign(quadgram) * 0.25
            return list(l2_normalize(vec))

        samples = [
            "OAuth2 bearer_token v2_auth and CamelCaseIdentifier in https://example.com/api",
            "```python\ndef sync_repo_embeddings(store, repo_id):\n    return 0\n```\nInline `chunk_embeddings` SQLite WAL.",
            "Pull requests and rate limiting on side pane with race conditions and memory leaks",
            "OAuth2 bearer_token v2_auth and CamelCaseIdentifier in https://example.com/api",
        ]
        for sample in samples:
            self.assertEqual(embed_text_deterministic(sample), reference_embed(sample))
        self.assertGreater(_stem_token.cache_info().hits, 0)
        self.assertGreater(_unigram_delta.cache_info().hits, 0)
        self.assertGreater(_bigram_delta.cache_info().hits, 0)
        self.assertGreater(_char_ngram_deltas.cache_info().hits, 0)


if __name__ == "__main__":
    unittest.main()
