"""Hybrid Reciprocal Rank Fusion (FTS5 BM25 + 384-dim cosine) search engine for packrat."""

from __future__ import annotations

import heapq
import json
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from db_store import PackratStore
from embedder import Embedder, ensure_onnx_deps, get_default_embedder
from models import (
    EMBEDDING_DIM,
    SearchFilters,
    SearchHit,
    TextChunk,
    l2_normalize,
    wrap_untrusted_text,
)

SearchMode = Literal["hybrid", "fts", "vector"]
VALID_SEARCH_MODES: tuple[SearchMode, ...] = ("hybrid", "fts", "vector")

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}")
RRF_K_DEFAULT = 60
_MMAP_BLOCK_ROWS = 8192
_UV_SITE_CACHE_FILE = "uv_site_packages.json"
_MMAP_META_FILE = "vectors.meta.json"
_MMAP_U8_FILE = "vectors.u8.mmap"
_MMAP_SCALES_FILE = "vectors.scales.f32.mmap"
_MMAP_ROWIDS_FILE = "vectors.rowids.i64.mmap"


def _can_import_numpy() -> bool:
    """Returns True if numpy can be imported from current sys.path."""
    try:
        import numpy  # noqa: F401

        return True
    except ImportError:
        return False


def _insert_missing_paths(paths: list[Any]) -> list[str]:
    """Prepends existing string paths not yet in sys.path and returns added entries."""
    inserted: list[str] = []
    for p in reversed(paths):
        if isinstance(p, str) and p not in sys.path and Path(p).exists():
            sys.path.insert(0, p)
            inserted.append(p)
    return inserted


def _try_load_cached_uv_paths(cache_dir: Path, py_tag: str) -> bool:
    """Attempts to load numpy using cached uv site-packages paths."""
    cache_file = cache_dir / _UV_SITE_CACHE_FILE
    if not cache_file.exists():
        return False
    inserted: list[str] = []
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        paths = data.get(py_tag) if isinstance(data, dict) else None
        if not isinstance(paths, list):
            return False
        inserted = _insert_missing_paths(paths)
        return _can_import_numpy()
    except (OSError, ValueError):
        return False
    finally:
        if inserted and not _can_import_numpy():
            for p in inserted:
                if p in sys.path:
                    sys.path.remove(p)


def _save_cached_uv_paths(cache_dir: Path, py_tag: str) -> None:
    """Atomically persists discovered uv site-packages paths under cache_dir."""
    if not cache_dir.exists():
        return
    cache_file = cache_dir / _UV_SITE_CACHE_FILE
    payload: dict[str, Any] = {}
    if cache_file.exists():
        try:
            existing = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                payload = existing
        except (OSError, ValueError):
            pass
    payload[py_tag] = [
        p for p in sys.path if "uv/archive-v0" in p or "site-packages" in p
    ]
    tmp_file = cache_dir / f"{_UV_SITE_CACHE_FILE}.tmp.{os.getpid()}"
    try:
        tmp_file.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp_file, cache_file)
    except OSError:
        tmp_file.unlink(missing_ok=True)


def _ensure_numpy(
    cache_dir: Path | None = None, *, allow_cache_write: bool = True
) -> bool:
    """Returns True when numpy is importable, discovering uv site-packages if needed."""
    if _can_import_numpy():
        return True
    py_tag = f"python{sys.version_info.major}.{sys.version_info.minor}"
    if cache_dir is not None and _try_load_cached_uv_paths(cache_dir, py_tag):
        return True
    if not allow_cache_write or not ensure_onnx_deps():
        return False
    if not _can_import_numpy():
        return False
    if cache_dir is not None:
        _save_cached_uv_paths(cache_dir, py_tag)
    return True


def sanitize_fts5_query(raw_query: str) -> str:
    """Converts arbitrary user/code query strings into a safe FTS5 MATCH expression."""
    tokens = _TOKEN_RE.findall(raw_query)
    if not tokens:
        return ""
    seen: list[str] = []
    for tok in tokens:
        low = tok.lower()
        if low not in seen:
            seen.append(low)
    quoted = [f'"{tok}"' for tok in seen[:16]]
    if len(quoted) == 1:
        return quoted[0]
    phrase = " ".join(quoted[:6])
    or_clause = " OR ".join(quoted)
    return f"({phrase}) OR ({or_clause})"


def _build_filter_sql(filters: SearchFilters) -> tuple[str, list[Any]]:
    """Builds SQL WHERE predicates and bind parameters for SearchFilters."""
    return PackratStore.build_search_filter_sql(filters)


def search_fts5_bm25(
    store: PackratStore,
    query: str,
    filters: SearchFilters,
    limit: int = 50,
) -> list[tuple[str, int, float]]:
    """Executes an FTS5 BM25 lexical search and returns (chunk_id, rank_1_based, score)."""
    fts_expr = sanitize_fts5_query(query)
    if not fts_expr:
        return []
    rows = store.query_fts_candidates(fts_expr, filters, limit=limit)
    return [
        (chunk_id, idx + 1, raw_bm25) for idx, (chunk_id, raw_bm25) in enumerate(rows)
    ]


def _query_mmap_watermark(store: PackratStore) -> list[Any]:
    """Computes an O(log N) watermark tuple over service_state, chunk_embeddings, chunks, and repos."""
    return store.vector_cache_watermark()


def _mmap_paths(cache_dir: Path) -> tuple[Path, Path, Path, Path]:
    """Returns (meta_path, u8_path, scales_path, rowids_path) in cache_dir."""
    return (
        cache_dir / _MMAP_META_FILE,
        cache_dir / _MMAP_U8_FILE,
        cache_dir / _MMAP_SCALES_FILE,
        cache_dir / _MMAP_ROWIDS_FILE,
    )


def _try_load_existing_mmap(
    cache_dir: Path, watermark: list[Any]
) -> dict[str, Any] | None:
    """Returns mmap cache metadata if all sidecar files exist and match watermark."""
    meta_path, u8_path, scales_path, rowids_path = _mmap_paths(cache_dir)
    if not (
        meta_path.exists()
        and u8_path.exists()
        and scales_path.exists()
        and rowids_path.exists()
    ):
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        count = int(meta.get("count", -1))
        if (
            meta.get("watermark") != watermark
            or count < 0
            or u8_path.stat().st_size != count * EMBEDDING_DIM
            or scales_path.stat().st_size != count * 4
            or rowids_path.stat().st_size != count * 8
        ):
            return None
        return {
            "count": count,
            "uniform_model_id": meta.get("uniform_model_id"),
            "u8_path": u8_path,
            "scales_path": scales_path,
            "rowids_path": rowids_path,
        }
    except (OSError, ValueError, TypeError):
        return None


def _write_vector_mmap_files(
    cache_dir: Path,
    rows: list[Any],
    watermark: list[Any],
    uniform_model: str | None,
) -> dict[str, Any] | None:
    """Writes sidecar np.memmap files atomically and returns cache metadata."""
    import numpy as np

    meta_path, u8_path, scales_path, rowids_path = _mmap_paths(cache_dir)
    n_rows = len(rows)
    pid = os.getpid()
    u8_tmp = cache_dir / f"{_MMAP_U8_FILE}.tmp.{pid}"
    scales_tmp = cache_dir / f"{_MMAP_SCALES_FILE}.tmp.{pid}"
    rowids_tmp = cache_dir / f"{_MMAP_ROWIDS_FILE}.tmp.{pid}"
    meta_tmp = cache_dir / f"{_MMAP_META_FILE}.tmp.{pid}"
    try:
        u8_arr = np.frombuffer(
            b"".join(bytes(r[2]) for r in rows), dtype=np.uint8
        ).reshape(n_rows, EMBEDDING_DIM)
        scales_arr = np.array([float(r[1]) for r in rows], dtype=np.float32)
        rowids_arr = np.array([int(r[0]) for r in rows], dtype=np.int64)

        u8_tmp.write_bytes(u8_arr.tobytes())
        scales_tmp.write_bytes(scales_arr.tobytes())
        rowids_tmp.write_bytes(rowids_arr.tobytes())
        meta_payload = {
            "watermark": watermark,
            "count": n_rows,
            "uniform_model_id": uniform_model,
        }
        meta_tmp.write_text(json.dumps(meta_payload), encoding="utf-8")

        meta_path.unlink(missing_ok=True)
        os.replace(u8_tmp, u8_path)
        os.replace(scales_tmp, scales_path)
        os.replace(rowids_tmp, rowids_path)
        os.replace(meta_tmp, meta_path)
    except OSError:
        meta_path.unlink(missing_ok=True)
        for tmp in (u8_tmp, scales_tmp, rowids_tmp, meta_tmp):
            tmp.unlink(missing_ok=True)
        return None
    return {
        "count": n_rows,
        "uniform_model_id": uniform_model,
        "u8_path": u8_path,
        "scales_path": scales_path,
        "rowids_path": rowids_path,
    }


def _ensure_vector_mmap_cache(
    store: PackratStore,
    *,
    cache_dir: Path | None = None,
    allow_rebuild: bool = True,
) -> dict[str, Any] | None:
    """Ensures sidecar np.memmap files match SQLite chunk_embeddings and returns cache metadata."""
    resolved_dir = cache_dir if cache_dir is not None else store.default_cache_dir
    if resolved_dir is None or not resolved_dir.exists():
        return None

    watermark = _query_mmap_watermark(store)
    existing = _try_load_existing_mmap(resolved_dir, watermark)
    if existing is not None or not allow_rebuild:
        return existing

    rows = store.read_all_embeddings_for_mmap()
    models = {str(r[3]) for r in rows if r[3]}
    uniform_model = next(iter(models)) if len(models) == 1 else None
    meta_path, u8_path, scales_path, rowids_path = _mmap_paths(resolved_dir)
    if not rows:
        for path in (meta_path, u8_path, scales_path, rowids_path):
            path.unlink(missing_ok=True)
        return {
            "count": 0,
            "uniform_model_id": uniform_model,
            "u8_path": u8_path,
            "scales_path": scales_path,
            "rowids_path": rowids_path,
        }
    return _write_vector_mmap_files(resolved_dir, rows, watermark, uniform_model)


def _resolve_filtered_mmap_indices(
    store: PackratStore,
    rowids_mmap: Any,
    filters: SearchFilters,
    model_id: str | None,
) -> Any:
    """Returns sorted mmap row indices matching `filters` and `model_id`."""
    import numpy as np

    rowids = store.resolve_filtered_chunk_ids(filters, model_id)
    if not rowids:
        return np.empty(0, dtype=np.int64)
    n_rows = len(rowids_mmap)
    matching_rowids = np.array(rowids, dtype=np.int64)
    idx = np.searchsorted(rowids_mmap, matching_rowids)
    valid_mask = (idx < n_rows) & (
        rowids_mmap[np.minimum(idx, n_rows - 1)] == matching_rowids
    )
    return idx[valid_mask]


def _score_mmap_rows(
    u8_mmap: Any,
    scales_mmap: Any,
    rowids_mmap: Any,
    q_f32: Any,
    idx: Any | None,
) -> tuple[Any, Any]:
    """Computes cosine similarity scores in blocks over all rows or `idx` subset."""
    import numpy as np

    q_offset = np.float32(128.0 * float(np.sum(q_f32)))
    if idx is None:
        n_rows = len(rowids_mmap)
        scores = np.empty(n_rows, dtype=np.float32)
        for start in range(0, n_rows, _MMAP_BLOCK_ROWS):
            end = min(start + _MMAP_BLOCK_ROWS, n_rows)
            raw_dots = u8_mmap[start:end].astype(np.float32) @ q_f32
            scores[start:end] = (raw_dots - q_offset) * scales_mmap[start:end]
        return scores, rowids_mmap

    m_rows = len(idx)
    scores = np.empty(m_rows, dtype=np.float32)
    for start in range(0, m_rows, _MMAP_BLOCK_ROWS):
        end = min(start + _MMAP_BLOCK_ROWS, m_rows)
        sub_idx = idx[start:end]
        raw_dots = u8_mmap[sub_idx].astype(np.float32) @ q_f32
        scores[start:end] = (raw_dots - q_offset) * scales_mmap[sub_idx]
    return scores, rowids_mmap[idx]


def _top_k_rowid_pairs(
    scores: Any, target_rowids: Any, limit: int
) -> list[tuple[int, float]]:
    """Selects the top `limit` (rowid, score) pairs from `scores`."""
    import numpy as np

    k = min(limit, len(scores))
    if len(scores) <= limit:
        top_idx = np.argsort(scores)[::-1]
    else:
        part_idx = np.argpartition(scores, -k)[-k:]
        top_idx = part_idx[np.argsort(scores[part_idx])[::-1]]
    return [(int(target_rowids[i]), float(scores[i])) for i in top_idx]


def _map_winning_rowids_to_chunks(
    store: PackratStore,
    winning_pairs: list[tuple[int, float]],
    limit: int,
) -> list[tuple[str, int, float]]:
    """Resolves chunk_ids for `winning_pairs` and returns ranked (chunk_id, rank, score) tuples."""
    if not winning_pairs:
        return []
    winning_rowids = [rid for rid, _ in winning_pairs]
    rowid_to_cid = store.get_embedding_chunk_ids_by_rowids(winning_rowids)
    scored_hits = [
        (score_val, rowid_to_cid[rid])
        for rid, score_val in winning_pairs
        if rid in rowid_to_cid
    ]
    scored_hits.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [(cid, rank + 1, sim) for rank, (sim, cid) in enumerate(scored_hits[:limit])]


def _search_vector_numpy(
    store: PackratStore,
    q_norm: Sequence[float],
    filters: SearchFilters,
    limit: int,
    model_id: str | None,
    *,
    cache_dir: Path | None = None,
    allow_rebuild: bool = True,
) -> list[tuple[str, int, float]] | None:
    """Scores vectors using sidecar np.memmap SIMD matrix multiplication."""
    cache = _ensure_vector_mmap_cache(
        store, cache_dir=cache_dir, allow_rebuild=allow_rebuild
    )
    if cache is None:
        return None
    n_rows = int(cache["count"])
    if n_rows == 0 or limit <= 0:
        return []

    import numpy as np

    try:
        u8_mmap = np.memmap(
            cache["u8_path"], dtype=np.uint8, mode="r", shape=(n_rows, EMBEDDING_DIM)
        )
        scales_mmap = np.memmap(
            cache["scales_path"], dtype=np.float32, mode="r", shape=(n_rows,)
        )
        rowids_mmap = np.memmap(
            cache["rowids_path"], dtype=np.int64, mode="r", shape=(n_rows,)
        )
        q_f32 = np.asarray(q_norm, dtype=np.float32)

        has_filters = bool(
            filters.repo or filters.kinds or filters.path_prefix or filters.state
        )
        needs_model_filter = bool(
            model_id and model_id != cache.get("uniform_model_id")
        )
        idx = None
        if has_filters or needs_model_filter:
            idx = _resolve_filtered_mmap_indices(store, rowids_mmap, filters, model_id)
            if len(idx) == 0:
                return []

        scores, target_rowids = _score_mmap_rows(
            u8_mmap, scales_mmap, rowids_mmap, q_f32, idx
        )
        winning_pairs = _top_k_rowid_pairs(scores, target_rowids, limit)
    except (OSError, ValueError):
        return None
    return _map_winning_rowids_to_chunks(store, winning_pairs, limit)


def _search_vector_stdlib(
    store: PackratStore,
    q_norm: Sequence[float],
    filters: SearchFilters,
    limit: int,
    model_id: str | None,
) -> list[tuple[str, int, float]]:
    """Pure-stdlib Python fallback scanning offset uint8 blobs over active query dimensions."""
    if limit <= 0:
        return []
    nz_dims = [
        (i, round(q_i * 16384.0)) for i, q_i in enumerate(q_norm) if abs(q_i) > 1e-6
    ]
    if not nz_dims:
        return []
    q_offset_int = 128 * sum(w for _, w in nz_dims)
    inv_q_scale = 1.0 / 16384.0

    heap: list[tuple[float, str]] = []
    for chunk_id, scale, blob in store.stream_filtered_embeddings(filters, model_id):
        dot_int = sum(w * blob[i] for i, w in nz_dims) - q_offset_int
        sim = dot_int * scale * inv_q_scale
        item = (sim, chunk_id)
        if len(heap) < limit:
            heapq.heappush(heap, item)
        elif item > heap[0]:
            heapq.heapreplace(heap, item)

    heap.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [(chunk_id, idx + 1, float(sim)) for idx, (sim, chunk_id) in enumerate(heap)]


def search_vector_cosine(
    store: PackratStore,
    query_vec: Sequence[float],
    filters: SearchFilters,
    limit: int = 50,
    model_id: str | None = None,
    *,
    cache_dir: Path | None = None,
    allow_cache_rebuild: bool = True,
) -> list[tuple[str, int, float]]:
    """Scores 384-dim offset uint8 embeddings via np.memmap SIMD or pure-stdlib fallback."""
    q_norm = l2_normalize(query_vec)
    if not any(q_norm):
        return []
    resolved_cache_dir = cache_dir if cache_dir is not None else store.default_cache_dir
    if resolved_cache_dir is not None and _ensure_numpy(
        resolved_cache_dir, allow_cache_write=allow_cache_rebuild
    ):
        mmap_hits = _search_vector_numpy(
            store,
            q_norm,
            filters,
            limit,
            model_id,
            cache_dir=resolved_cache_dir,
            allow_rebuild=allow_cache_rebuild,
        )
        if mmap_hits is not None:
            return mmap_hits
    return _search_vector_stdlib(store, q_norm, filters, limit, model_id)


def _format_snippet(chunk: TextChunk, max_chars: int = 420) -> str:
    """Formats a chunk snippet and wraps untrusted external GitHub prose in XML tags."""
    raw = chunk.text.strip()
    if len(raw) > max_chars:
        raw = raw[: max_chars - 3].rstrip() + "..."
    if chunk.is_untrusted:
        return wrap_untrusted_text(chunk.source_ref, raw)
    return raw


def _repo_names_by_id(store: PackratStore) -> dict[int, str]:
    """Returns a lookup map of repo_id -> full_name."""
    return {r.repo_id: r.full_name for r in store.list_repos()}


def _compute_rrf_scores(
    fts_ranks: dict[str, int],
    vec_ranks: dict[str, int],
    rrf_k: int,
) -> dict[str, float]:
    """Computes Reciprocal Rank Fusion scores across lexical and vector rank maps."""
    scores: dict[str, float] = {}
    for cid in set(fts_ranks) | set(vec_ranks):
        total = 0.0
        if cid in fts_ranks:
            total += 1.0 / (rrf_k + fts_ranks[cid])
        if cid in vec_ranks:
            total += 1.0 / (rrf_k + vec_ranks[cid])
        scores[cid] = total
    return scores


def _build_search_hits(
    store: PackratStore,
    ranked_ids: list[str],
    rrf_scores: dict[str, float],
    fts_ranks: dict[str, int],
    vec_ranks: dict[str, int],
    vec_scores: dict[str, float],
) -> list[SearchHit]:
    """Materializes `SearchHit` records for the top `ranked_ids`."""
    chunk_map = store.get_chunks_by_ids(ranked_ids)
    repo_names = _repo_names_by_id(store)
    hits: list[SearchHit] = []
    for cid in ranked_ids:
        ch = chunk_map.get(cid)
        if ch is None:
            continue
        hits.append(
            SearchHit(
                chunk_id=cid,
                repo_full_name=repo_names.get(ch.repo_id, str(ch.repo_id)),
                source_kind=ch.source_kind,
                source_ref=ch.source_ref,
                title=ch.title,
                snippet=_format_snippet(ch),
                rrf_score=round(rrf_scores[cid], 6),
                bm25_rank=fts_ranks.get(cid),
                vector_rank=vec_ranks.get(cid),
                cosine_score=round(vec_scores[cid], 4) if cid in vec_scores else None,
                is_untrusted=ch.is_untrusted,
                file_path=ch.file_path,
                item_number=ch.item_number,
            )
        )
    return hits


def hybrid_rrf_search(
    store: PackratStore,
    query: str,
    filters: SearchFilters | None = None,
    mode: SearchMode = "hybrid",
    limit: int = 15,
    rrf_k: int = RRF_K_DEFAULT,
    embedder: Embedder | None = None,
    *,
    cache_dir: Path | None = None,
    allow_cache_rebuild: bool = True,
) -> list[SearchHit]:
    """Runs FTS5 BM25, 384-dim cosine, or Reciprocal Rank Fusion hybrid search."""
    if mode not in VALID_SEARCH_MODES:
        raise ValueError(
            f"Unknown search mode {mode!r}; expected one of {VALID_SEARCH_MODES}."
        )
    active_filters = filters or SearchFilters()
    candidate_pool = max(limit * 4, 50)

    fts_ranks: dict[str, int] = {}
    if mode in ("hybrid", "fts"):
        fts_ranks = {
            cid: rank
            for cid, rank, _ in search_fts5_bm25(
                store, query, active_filters, limit=candidate_pool
            )
        }

    vec_ranks: dict[str, int] = {}
    vec_scores: dict[str, float] = {}
    if mode in ("hybrid", "vector"):
        active_embedder = embedder or get_default_embedder()
        q_vec = active_embedder.embed_query(query)
        for cid, rank, sim in search_vector_cosine(
            store,
            q_vec,
            active_filters,
            limit=candidate_pool,
            model_id=active_embedder.model_id,
            cache_dir=cache_dir,
            allow_cache_rebuild=allow_cache_rebuild,
        ):
            vec_ranks[cid] = rank
            vec_scores[cid] = sim

    rrf_scores = _compute_rrf_scores(fts_ranks, vec_ranks, rrf_k)
    if not rrf_scores:
        return []

    ranked_ids = sorted(
        rrf_scores,
        key=lambda cid: (rrf_scores[cid], vec_scores.get(cid, -1.0)),
        reverse=True,
    )[:limit]
    return _build_search_hits(
        store, ranked_ids, rrf_scores, fts_ranks, vec_ranks, vec_scores
    )
