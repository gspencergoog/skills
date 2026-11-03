"""Hybrid Reciprocal Rank Fusion (FTS5 BM25 + 384-dim cosine) search engine for packrat."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from db_store import PackratStore
from embedder import Embedder, get_default_embedder
from models import (
    SearchFilters,
    SearchHit,
    TextChunk,
    l2_normalize,
    unpack_f32_vector,
    wrap_untrusted_text,
)

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}")
RRF_K_DEFAULT = 60


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
    clauses: list[str] = []
    params: list[Any] = []

    if filters.repo:
        clauses.append("(LOWER(r.full_name) = LOWER(?) OR LOWER(r.name) = LOWER(?))")
        params.extend([filters.repo, filters.repo])

    if filters.kinds:
        placeholders = ",".join("?" for _ in filters.kinds)
        clauses.append(f"c.source_kind IN ({placeholders})")
        params.extend(filters.kinds)

    if filters.path_prefix:
        escaped_prefix = (
            filters.path_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        clauses.append("c.file_path LIKE ? ESCAPE '\\'")
        params.append(f"{escaped_prefix}%")

    if filters.state:
        clauses.append(
            """(
              (c.source_kind = 'issue' AND EXISTS (
                SELECT 1 FROM issues i
                WHERE i.repo_id = c.repo_id AND i.number = c.item_number
                  AND UPPER(i.state) = UPPER(?)
              ))
              OR
              (c.source_kind = 'pr' AND EXISTS (
                SELECT 1 FROM pull_requests p
                WHERE p.repo_id = c.repo_id AND p.number = c.item_number
                  AND UPPER(p.state) = UPPER(?)
              ))
            )"""
        )
        params.extend([filters.state, filters.state])

    if not clauses:
        return "", params
    return " AND " + " AND ".join(clauses), params


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
    filter_sql, filter_params = _build_filter_sql(filters)
    sql = f"""
    SELECT
      c.chunk_id,
      bm25(chunks_fts, 1.0, 1.0, 1.5, 5.0, 1.0) AS raw_bm25
    FROM chunks_fts
    JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id
    JOIN repos r ON r.repo_id = c.repo_id
    WHERE chunks_fts MATCH ?
    {filter_sql}
    ORDER BY raw_bm25 ASC
    LIMIT ?
    """
    rows = store.conn.execute(sql, [fts_expr, *filter_params, limit]).fetchall()
    return [(str(row["chunk_id"]), idx + 1, float(row["raw_bm25"])) for idx, row in enumerate(rows)]


def search_vector_cosine(
    store: PackratStore,
    query_vec: Sequence[float],
    filters: SearchFilters,
    limit: int = 50,
    model_id: str | None = None,
) -> list[tuple[str, int, float]]:
    """Scans 384-dim float32 embeddings and returns top (chunk_id, rank_1_based, cosine)."""
    q_norm = l2_normalize(query_vec)
    if not any(q_norm):
        return []
    filter_sql, filter_params = _build_filter_sql(filters)
    model_sql = " AND ce.model_id = ?" if model_id else ""
    sql_params = [model_id, *filter_params] if model_id else filter_params
    sql = f"""
    SELECT
      ce.chunk_id,
      ce.vector_f32
    FROM chunk_embeddings ce
    JOIN chunks c ON c.chunk_id = ce.chunk_id
    JOIN repos r ON r.repo_id = c.repo_id
    WHERE ce.content_sha256 = c.content_sha256
    {model_sql}
    {filter_sql}
    """
    rows = store.conn.execute(sql, sql_params).fetchall()
    scored: list[tuple[float, str]] = []
    for row in rows:
        vec = unpack_f32_vector(bytes(row["vector_f32"]))
        sim = sum(a * b for a, b in zip(q_norm, vec))
        scored.append((sim, str(row["chunk_id"])))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [(chunk_id, idx + 1, float(sim)) for idx, (sim, chunk_id) in enumerate(scored[:limit])]


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


def hybrid_rrf_search(
    store: PackratStore,
    query: str,
    filters: SearchFilters | None = None,
    mode: str = "hybrid",
    limit: int = 15,
    rrf_k: int = RRF_K_DEFAULT,
    embedder: Embedder | None = None,
) -> list[SearchHit]:
    """Runs FTS5 BM25, 384-dim cosine, or Reciprocal Rank Fusion hybrid search."""
    active_filters = filters or SearchFilters()
    candidate_pool = max(limit * 4, 50)

    fts_ranks: dict[str, int] = {}
    if mode in ("hybrid", "fts"):
        for cid, rank, _ in search_fts5_bm25(store, query, active_filters, limit=candidate_pool):
            fts_ranks[cid] = rank

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
