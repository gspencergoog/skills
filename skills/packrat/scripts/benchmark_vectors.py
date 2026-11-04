#!/usr/bin/env python3
"""Reproducible benchmark suite for packrat offset uint8 vector storage and cosine search.

Measures and reports:
1. Storage footprint (legacy float32 + JSON vs. offset uint8 + float32 scale, plus SQLite file size).
2. Query latency (p50 and p95 across 10 representative queries) for:
   - Legacy Python struct.unpack("<384f") dot-product loop
   - Pure-stdlib Python offset uint8 sparse-query scan (_search_vector_stdlib)
   - Contiguous np.memmap SIMD matrix multiplication (_search_vector_numpy, cold and warm)
   - Optional 1,000,000-vector synthetic np.memmap benchmark (--scale-1m)
3. Retrieval accuracy vs. exact float32 cosine similarity:
   - Pure vector Recall@15
   - Hybrid RRF Top-12 overlap and Rank-1 agreement
"""

from __future__ import annotations

import argparse
import json
import statistics
import struct
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from db_store import DEFAULT_DB_PATH, PackratStore
from embedder import Embedder, get_default_embedder
from models import (
    EMBEDDING_DIM,
    SearchFilters,
    dequantize_u8_vector,
    l2_normalize,
    quantize_u8_vector,
    unpack_f32_vector,
)
from search_engine import (
    RRF_K_DEFAULT,
    _compute_rrf_scores,
    _ensure_numpy,
    _search_vector_numpy,
    _search_vector_stdlib,
    search_fts5_bm25,
    search_vector_cosine,
)

_PACK_FORMAT = f"<{EMBEDDING_DIM}f"


def _pack_f32_vector(vec: Sequence[float]) -> bytes:
    """Packs a 384-dimensional float vector into a little-endian IEEE-754 float32 BLOB."""
    return struct.pack(_PACK_FORMAT, *vec)


BENCHMARK_QUERIES: tuple[str, ...] = (
    "keyboard focus navigation and tab order",
    "catalog component registration and schema validation",
    "surface controller state lifecycle and disposal",
    "markdown table parser and heading extraction",
    "OAuth2 bearer token authentication and rate limit",
    "SQLite WAL database migration and foreign keys",
    "pull request review thread resolution and status checks",
    "streaming JSONL增量 incremental event synchronization",
    "Flutter widget tree rebuild and layout constraints",
    "memory mapped vector cosine similarity search",
)


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Computes a linear-interpolated percentile from a sorted list of floats."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * (pct / 100.0)
    low = int(pos)
    high = min(low + 1, len(sorted_vals) - 1)
    frac = pos - low
    return sorted_vals[low] * (1.0 - frac) + sorted_vals[high] * frac


def _legacy_f32_search(
    f32_rows: list[tuple[str, bytes]],
    q_norm: tuple[float, ...],
    limit: int,
) -> list[tuple[str, int, float]]:
    """Simulates the legacy struct.unpack('<384f') + Python generator dot-product loop."""
    scored: list[tuple[float, str]] = []
    for cid, blob in f32_rows:
        vec = unpack_f32_vector(blob)
        sim = sum(a * b for a, b in zip(q_norm, vec))
        scored.append((sim, cid))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [(cid, idx + 1, float(sim)) for idx, (sim, cid) in enumerate(scored[:limit])]


def _run_1m_mmap_benchmark(query_vecs: list[tuple[float, ...]]) -> dict[str, Any]:
    """Benchmarks np.memmap SIMD cosine search across 1,000,000 384-dim uint8 vectors."""
    import numpy as np

    n_rows = 1_000_000
    with tempfile.TemporaryDirectory(prefix="packrat-bench-1m-") as tmp:
        tmp_dir = Path(tmp)
        u8_path = tmp_dir / "vectors_1m.u8.mmap"
        scales_path = tmp_dir / "scales_1m.f32.mmap"

        u8_mmap = np.memmap(
            u8_path, dtype=np.uint8, mode="w+", shape=(n_rows, EMBEDDING_DIM)
        )
        rng = np.random.default_rng(42)
        block = 100_000
        for start in range(0, n_rows, block):
            u8_mmap[start : start + block] = rng.integers(
                1, 256, size=(block, EMBEDDING_DIM), dtype=np.uint8
            )
        u8_mmap.flush()

        scales_mmap = np.memmap(
            scales_path, dtype=np.float32, mode="w+", shape=(n_rows,)
        )
        scales_mmap[:] = np.float32(1.0 / 127.0)
        scales_mmap.flush()

        u8_ro = np.memmap(
            u8_path, dtype=np.uint8, mode="r", shape=(n_rows, EMBEDDING_DIM)
        )
        scales_ro = np.memmap(scales_path, dtype=np.float32, mode="r", shape=(n_rows,))

        latencies_ms: list[float] = []
        block_rows = 16384
        for q_norm in query_vecs:
            t0 = time.perf_counter()
            q_f32 = np.asarray(q_norm, dtype=np.float32)
            q_offset = np.float32(128.0 * float(np.sum(q_f32)))
            scores = np.empty(n_rows, dtype=np.float32)
            for start in range(0, n_rows, block_rows):
                end = min(start + block_rows, n_rows)
                raw_dots = u8_ro[start:end].astype(np.float32) @ q_f32
                scores[start:end] = (raw_dots - q_offset) * scales_ro[start:end]
            part_idx = np.argpartition(scores, -50)[-50:]
            _ = part_idx[np.argsort(scores[part_idx])[::-1]]
            latencies_ms.append((time.perf_counter() - t0) * 1000.0)

        latencies_ms.sort()
        return {
            "vectors": n_rows,
            "mmap_mib": round(
                (u8_path.stat().st_size + scales_path.stat().st_size) / (1024 * 1024), 2
            ),
            "p50_ms": round(_percentile(latencies_ms, 50), 2),
            "p95_ms": round(_percentile(latencies_ms, 95), 2),
        }


def _run_stdlib_u8_benchmark(
    store: PackratStore,
    query_vecs: Sequence[tuple[float, ...]],
    filters: SearchFilters,
    active_model: str,
) -> tuple[list[float], list[list[tuple[str, int, float]]]]:
    """Measures pure-stdlib offset uint8 sparse scan latency and collects rankings."""
    stdlib_latencies_ms: list[float] = []
    u8_vec_results: list[list[tuple[str, int, float]]] = []
    for q_norm in query_vecs:
        t0 = time.perf_counter()
        res = _search_vector_stdlib(
            store, q_norm, filters, limit=60, model_id=active_model
        )
        stdlib_latencies_ms.append((time.perf_counter() - t0) * 1000.0)
        u8_vec_results.append(res)
    stdlib_latencies_ms.sort()
    return stdlib_latencies_ms, u8_vec_results


def _collect_candidate_chunks_and_fts(
    store: PackratStore,
    u8_vec_results: Sequence[Sequence[tuple[str, int, float]]],
    filters: SearchFilters,
) -> tuple[set[str], list[list[tuple[str, int, float]]]]:
    """Runs FTS5 BM25 over benchmark queries and collects candidate chunk IDs."""
    candidate_cids: set[str] = set()
    fts_hits_per_query: list[list[tuple[str, int, float]]] = []
    for q_str, u8_res in zip(BENCHMARK_QUERIES, u8_vec_results):
        candidate_cids.update(cid for cid, _, _ in u8_res)
        fts_hits = search_fts5_bm25(store, q_str, filters, limit=60)
        fts_hits_per_query.append(fts_hits)
        candidate_cids.update(cid for cid, _, _ in fts_hits)
    return candidate_cids, fts_hits_per_query


def _reembed_candidates_exact(
    store: PackratStore,
    embedder: Embedder,
    candidate_cids: set[str],
    active_model: str,
) -> tuple[dict[str, bytes], dict[str, tuple[bytes, float]]]:
    """Re-embeds candidate chunks in exact float32 when active_model matches embedder."""
    exact_f32_by_cid: dict[str, bytes] = {}
    re_quantized_u8_by_cid: dict[str, tuple[bytes, float]] = {}
    if not candidate_cids or active_model != embedder.model_id:
        return exact_f32_by_cid, re_quantized_u8_by_cid

    chunk_map = store.get_chunks_by_ids(sorted(candidate_cids))
    ordered_cids = [cid for cid in sorted(candidate_cids) if cid in chunk_map]
    for start in range(0, len(ordered_cids), 64):
        batch_cids = ordered_cids[start : start + 64]
        batch_chunks = [chunk_map[c] for c in batch_cids]
        vecs = embedder.embed_chunks(batch_chunks)
        for cid, raw_v in zip(batch_cids, vecs):
            norm_v = l2_normalize(raw_v)
            exact_f32_by_cid[cid] = _pack_f32_vector(norm_v)
            re_quantized_u8_by_cid[cid] = quantize_u8_vector(norm_v)
    return exact_f32_by_cid, re_quantized_u8_by_cid


def _build_eval_rows(
    rows: Sequence[Any],
    exact_f32_by_cid: dict[str, bytes],
    re_quantized_u8_by_cid: dict[str, tuple[bytes, float]],
) -> tuple[list[tuple[str, bytes]], list[tuple[str, bytes, float]]]:
    """Constructs full-corpus float32 and uint8 evaluation rows."""
    f32_rows: list[tuple[str, bytes]] = []
    u8_eval_rows: list[tuple[str, bytes, float]] = []
    for r in rows:
        cid = str(r["chunk_id"])
        if cid in exact_f32_by_cid:
            f32_rows.append((cid, exact_f32_by_cid[cid]))
            u8_b, u8_s = re_quantized_u8_by_cid[cid]
            u8_eval_rows.append((cid, u8_b, u8_s))
        else:
            raw_u8 = bytes(r["vector_u8"])
            raw_s = float(r["scale"])
            vec = dequantize_u8_vector(raw_u8, raw_s)
            f32_rows.append((cid, _pack_f32_vector(vec)))
            u8_eval_rows.append((cid, raw_u8, raw_s))
    return f32_rows, u8_eval_rows


def _score_u8_eval_rows(
    u8_eval_rows: Sequence[tuple[str, bytes, float]],
    q_norm: tuple[float, ...],
    limit: int = 60,
) -> list[tuple[str, int, float]]:
    """Scores quantized uint8 evaluation rows for a single normalized query."""
    q_offset = 128.0 * sum(q_norm)
    scored_u8: list[tuple[float, str]] = []
    for cid, u8_b, u8_s in u8_eval_rows:
        raw_dot = sum(b * q for b, q in zip(u8_b, q_norm))
        scored_u8.append(((raw_dot - q_offset) * u8_s, cid))
    scored_u8.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [
        (cid, idx + 1, float(sim)) for idx, (sim, cid) in enumerate(scored_u8[:limit])
    ]


def _run_legacy_and_quant_eval(
    f32_rows: list[tuple[str, bytes]],
    u8_eval_rows: Sequence[tuple[str, bytes, float]],
    query_vecs: Sequence[tuple[float, ...]],
) -> tuple[
    list[float],
    list[list[tuple[str, int, float]]],
    list[list[tuple[str, int, float]]],
]:
    """Measures legacy float32 unpack latency and produces exact vs quantized rankings."""
    legacy_latencies_ms: list[float] = []
    f32_vec_results: list[list[tuple[str, int, float]]] = []
    u8_quant_results: list[list[tuple[str, int, float]]] = []
    for q_norm in query_vecs:
        t0 = time.perf_counter()
        res = _legacy_f32_search(f32_rows, q_norm, limit=60)
        legacy_latencies_ms.append((time.perf_counter() - t0) * 1000.0)
        f32_vec_results.append(res)
        u8_quant_results.append(_score_u8_eval_rows(u8_eval_rows, q_norm, limit=60))
    legacy_latencies_ms.sort()
    return legacy_latencies_ms, f32_vec_results, u8_quant_results


def _run_numpy_mmap_benchmark(
    store: PackratStore,
    query_vecs: Sequence[tuple[float, ...]],
    filters: SearchFilters,
    active_model: str,
    n_vecs: int,
    has_np: bool,
) -> tuple[float | None, list[float], int]:
    """Measures cold and warm np.memmap SIMD search in an isolated temporary cache directory."""
    if not has_np or n_vecs == 0:
        return None, [], 0

    warm_mmap_latencies_ms: list[float] = []
    with tempfile.TemporaryDirectory(prefix="packrat-bench-mmap-") as tmp:
        tmp_cache_dir = Path(tmp)
        t0 = time.perf_counter()
        _ = search_vector_cosine(
            store,
            query_vecs[0],
            filters,
            limit=60,
            model_id=active_model,
            cache_dir=tmp_cache_dir,
        )
        cold_mmap_ms = round((time.perf_counter() - t0) * 1000.0, 2)

        for q_norm in query_vecs:
            t0 = time.perf_counter()
            res_np = _search_vector_numpy(
                store,
                q_norm,
                filters,
                limit=60,
                model_id=active_model,
                cache_dir=tmp_cache_dir,
            )
            warm_mmap_latencies_ms.append((time.perf_counter() - t0) * 1000.0)
            assert res_np is not None

        mmap_bytes = sum(
            (tmp_cache_dir / f).stat().st_size
            for f in (
                "vectors.u8.mmap",
                "vectors.scales.f32.mmap",
                "vectors.rowids.i64.mmap",
            )
            if (tmp_cache_dir / f).exists()
        )
    warm_mmap_latencies_ms.sort()
    return cold_mmap_ms, warm_mmap_latencies_ms, mmap_bytes


def _top_k_rrf_cids(
    fts_ranks: dict[str, int], vec_ranks: dict[str, int], k: int = 12
) -> list[str]:
    """Returns top `k` chunk IDs ranked by RRF over lexical and vector ranks."""
    rrf = _compute_rrf_scores(fts_ranks, vec_ranks, RRF_K_DEFAULT)
    return sorted(rrf, key=lambda c: rrf[c], reverse=True)[:k]


def _evaluate_query_accuracy(
    fts_hits: Sequence[tuple[str, int, float]],
    f32_res: Sequence[tuple[str, int, float]],
    u8_res: Sequence[tuple[str, int, float]],
) -> tuple[float | None, float | None, bool]:
    """Computes (recall_at_15, rrf_top12_overlap, rrf_rank1_match) for one query."""
    f32_top15 = {cid for cid, _, _ in f32_res[:15]}
    u8_top15 = {cid for cid, _, _ in u8_res[:15]}
    recall = (len(f32_top15 & u8_top15) / len(f32_top15)) if f32_top15 else None

    fts_ranks = {cid: rank for cid, rank, _ in fts_hits}
    f32_top12 = _top_k_rrf_cids(fts_ranks, {cid: r for cid, r, _ in f32_res})
    u8_top12 = _top_k_rrf_cids(fts_ranks, {cid: r for cid, r, _ in u8_res})
    if not f32_top12:
        return recall, None, False
    overlap = len(set(f32_top12) & set(u8_top12)) / len(f32_top12)
    rank1_match = bool(u8_top12 and f32_top12[0] == u8_top12[0])
    return recall, overlap, rank1_match


def _compute_accuracy_metrics(
    fts_hits_per_query: Sequence[Sequence[tuple[str, int, float]]],
    f32_vec_results: Sequence[Sequence[tuple[str, int, float]]],
    u8_quant_results: Sequence[Sequence[tuple[str, int, float]]],
) -> dict[str, Any]:
    """Computes Recall@15 and Hybrid RRF Top-12 overlap vs exact float32 search."""
    recalls_at_15: list[float] = []
    rrf_top12_overlaps: list[float] = []
    rrf_rank1_matches = 0
    for fts_hits, f32_res, u8_res in zip(
        fts_hits_per_query, f32_vec_results, u8_quant_results
    ):
        recall, overlap, rank1_match = _evaluate_query_accuracy(
            fts_hits, f32_res, u8_res
        )
        if recall is not None:
            recalls_at_15.append(recall)
        if overlap is not None:
            rrf_top12_overlaps.append(overlap)
        if rank1_match:
            rrf_rank1_matches += 1

    return {
        "vector_recall_at_15_pct": round(statistics.mean(recalls_at_15) * 100.0, 2)
        if recalls_at_15
        else 100.0,
        "hybrid_rrf_top12_overlap_pct": round(
            statistics.mean(rrf_top12_overlaps) * 100.0, 2
        )
        if rrf_top12_overlaps
        else 100.0,
        "hybrid_rrf_rank1_matches": f"{rrf_rank1_matches}/{len(BENCHMARK_QUERIES)}",
    }


def run_benchmark(db_path: Path, scale_1m: bool = False) -> dict[str, Any]:
    """Runs storage, latency, and accuracy benchmarks on `db_path`."""
    store = PackratStore(db_path)
    try:
        embedder = get_default_embedder()
        rows = store.conn.execute(
            """
            SELECT ce.chunk_id, ce.scale, ce.vector_u8, ce.model_id
            FROM chunk_embeddings ce
            JOIN chunks c ON c.chunk_id = ce.chunk_id
            WHERE ce.content_sha256 = c.content_sha256
            ORDER BY ce.rowid ASC
            """
        ).fetchall()
        n_vecs = len(rows)
        active_model = str(rows[0]["model_id"]) if rows else embedder.model_id
        query_vecs = [l2_normalize(embedder.embed_query(q)) for q in BENCHMARK_QUERIES]
        filters = SearchFilters()

        stdlib_latencies_ms, u8_vec_results = _run_stdlib_u8_benchmark(
            store, query_vecs, filters, active_model
        )
        candidate_cids, fts_hits_per_query = _collect_candidate_chunks_and_fts(
            store, u8_vec_results, filters
        )
        exact_f32_by_cid, re_quantized_u8_by_cid = _reembed_candidates_exact(
            store, embedder, candidate_cids, active_model
        )
        f32_rows, u8_eval_rows = _build_eval_rows(
            rows, exact_f32_by_cid, re_quantized_u8_by_cid
        )
        legacy_latencies_ms, f32_vec_results, u8_quant_results = (
            _run_legacy_and_quant_eval(f32_rows, u8_eval_rows, query_vecs)
        )

        has_np = _ensure_numpy(db_path.parent)
        cold_mmap_ms, warm_mmap_latencies_ms, mmap_bytes = _run_numpy_mmap_benchmark(
            store, query_vecs, filters, active_model, n_vecs, has_np
        )
        sqlite_mib = (
            round(db_path.stat().st_size / (1024 * 1024), 2)
            if db_path.exists()
            else 0.0
        )

        report: dict[str, Any] = {
            "db_path": str(db_path),
            "vectors_count": n_vecs,
            "active_model_id": active_model,
            "storage": {
                "legacy_bytes_per_vector": 4280,
                "quantized_bytes_per_vector": 388,
                "sqlite_db_mib": sqlite_mib,
                "mmap_cache_mib": round(mmap_bytes / (1024 * 1024), 2),
            },
            "latency_ms": {
                "legacy_f32_unpack": {
                    "p50_ms": round(_percentile(legacy_latencies_ms, 50), 2),
                    "p95_ms": round(_percentile(legacy_latencies_ms, 95), 2),
                },
                "stdlib_u8_sparse": {
                    "p50_ms": round(_percentile(stdlib_latencies_ms, 50), 2),
                    "p95_ms": round(_percentile(stdlib_latencies_ms, 95), 2),
                },
                "numpy_mmap_simd": {
                    "cold_rebuild_ms": cold_mmap_ms,
                    "warm_p50_ms": round(_percentile(warm_mmap_latencies_ms, 50), 2)
                    if warm_mmap_latencies_ms
                    else None,
                    "warm_p95_ms": round(_percentile(warm_mmap_latencies_ms, 95), 2)
                    if warm_mmap_latencies_ms
                    else None,
                },
            },
            "accuracy": _compute_accuracy_metrics(
                fts_hits_per_query, f32_vec_results, u8_quant_results
            ),
        }
        if scale_1m and has_np:
            report["scale_1m"] = _run_1m_mmap_benchmark(query_vecs)
        return report
    finally:
        store.close()


def _format_markdown_report(rep: dict[str, Any]) -> str:
    """Formats the benchmark dictionary as a Markdown table summary."""
    st = rep["storage"]
    lat = rep["latency_ms"]
    acc = rep["accuracy"]
    lines = [
        f"## `packrat` Vector Store Benchmark ({rep['vectors_count']:,} vectors, model `{rep['active_model_id']}`)",
        "",
        "| Engine / Metric | p50 Latency | p95 Latency | Bytes / Vector | Notes |",
        "| :--- | :--- | :--- | :--- | :--- |",
        f"| Legacy `float32` (`struct.unpack` loop) | `{lat['legacy_f32_unpack']['p50_ms']} ms` | `{lat['legacy_f32_unpack']['p95_ms']} ms` | `{st['legacy_bytes_per_vector']} B` | In-memory unpack baseline (excludes ~900 ms SQLite overflow I/O) |",
        f"| Pure-stdlib Python `uint8` sparse scan | `{lat['stdlib_u8_sparse']['p50_ms']} ms` | `{lat['stdlib_u8_sparse']['p95_ms']} ms` | `{st['quantized_bytes_per_vector']} B` | Zero-dependency fallback (`b[i]` over non-zero query dims) |",
        f"| **`np.memmap` SIMD matmul (warm)** | **`{lat['numpy_mmap_simd']['warm_p50_ms']} ms`** | **`{lat['numpy_mmap_simd']['warm_p95_ms']} ms`** | **`{st['quantized_bytes_per_vector']} B`** | Cold cache build: `{lat['numpy_mmap_simd']['cold_rebuild_ms']} ms`; cache size: `{st['mmap_cache_mib']} MiB` |",
    ]
    if "scale_1m" in rep:
        s1m = rep["scale_1m"]
        lines.append(
            f"| **`np.memmap` SIMD @ 1,000,000 vectors** | **`{s1m['p50_ms']} ms`** | **`{s1m['p95_ms']} ms`** | **`388 B`** | Synthetic 1M corpus (`{s1m['mmap_mib']} MiB` mmap) |"
        )
    lines.extend(
        [
            "",
            f"- **SQLite DB size**: `{st['sqlite_db_mib']} MiB`",
            f"- **Vector Recall@15**: `{acc['vector_recall_at_15_pct']}%`",
            f"- **Hybrid RRF Top-12 overlap**: `{acc['hybrid_rrf_top12_overlap_pct']}%` (Rank-1 agreement: `{acc['hybrid_rrf_rank1_matches']}`)",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark packrat vector storage and search."
    )
    parser.add_argument(
        "--db", default=str(DEFAULT_DB_PATH), help="Path to repos.sqlite."
    )
    parser.add_argument(
        "--scale-1m",
        action="store_true",
        help="Also run 1,000,000-vector np.memmap benchmark.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    args = parser.parse_args()

    rep = run_benchmark(Path(args.db).expanduser(), scale_1m=args.scale_1m)
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        print(_format_markdown_report(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
