"""Unit tests for packrat offline embedders and incremental content_sha256 skip logic."""

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from db_store import PackratStore
from embedder import (
    ARCTIC_MODEL_ID,
    DETERMINISTIC_MODEL_ID,
    DeterministicHashEmbedder,
    _ensure_onnx_deps,
    embed_structured_deterministic,
    embed_text_deterministic,
    ensure_onnx_deps,
    ensure_onnx_model,
    get_default_embedder,
    sync_repo_embeddings,
)
from models import (
    EMBEDDING_DIM,
    TextChunk,
    compute_content_sha256,
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
                first_count = sync_repo_embeddings(
                    store, repo.repo_id, embedder=embedder
                )
                self.assertEqual(first_count, 2)

                # Second sync with identical content_sha256 must embed 0 chunks
                with mock.patch.object(
                    embedder, "embed_chunks", wraps=embedder.embed_chunks
                ) as spy:
                    second_count = sync_repo_embeddings(
                        store, repo.repo_id, embedder=embedder
                    )
                    self.assertEqual(second_count, 0)
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
                third_count = sync_repo_embeddings(
                    store, repo.repo_id, embedder=embedder
                )
                self.assertEqual(third_count, 1)

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
                self.assertEqual(count, 5)
                self.assertEqual(advances, [(2, 5), (4, 5), (5, 5)])
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


if __name__ == "__main__":
    unittest.main()
