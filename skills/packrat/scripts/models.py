"""Data models, vector serialization helpers, and untrusted-content framing for packrat."""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

EMBEDDING_DIM = 384
_PACK_FORMAT = f"<{EMBEDDING_DIM}f"
_PACK_BYTE_LEN = EMBEDDING_DIM * 4
_CLOSING_UNTRUSTED_RE = re.compile(r"</\s*untrusted_repo_content\b[^>]*>", re.IGNORECASE)


def utc_now_iso() -> str:
    """Returns the current UTC timestamp in ISO-8601 format."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_content_sha256(title: str, text: str) -> str:
    """Computes a stable SHA-256 hex digest over normalized title and body text."""
    payload = f"{title.strip()}\n{text.strip()}".encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()


def l2_normalize(vec: Sequence[float]) -> tuple[float, ...]:
    """L2-normalizes a sequence of floats to unit length."""
    sum_sq = sum(v * v for v in vec)
    if sum_sq <= 0.0:
        return tuple(0.0 for _ in vec)
    inv = 1.0 / math.sqrt(sum_sq)
    return tuple(v * inv for v in vec)


def pack_f32_vector(vec: Sequence[float]) -> bytes:
    """Packs a 384-dimensional float vector into a little-endian IEEE-754 float32 BLOB."""
    if len(vec) != EMBEDDING_DIM:
        raise ValueError(f"Expected {EMBEDDING_DIM}-dim vector, got {len(vec)}")
    return struct.pack(_PACK_FORMAT, *vec)


def unpack_f32_vector(blob: bytes) -> tuple[float, ...]:
    """Unpacks a little-endian IEEE-754 float32 BLOB into a 384-float tuple."""
    if len(blob) != _PACK_BYTE_LEN:
        raise ValueError(f"Expected {_PACK_BYTE_LEN} bytes, got {len(blob)}")
    return struct.unpack(_PACK_FORMAT, blob)


def format_vector_json(vec: Sequence[float]) -> str:
    """Formats a float vector as a compact JSON array for DuckDB FLOAT[384] parsing."""
    return json.dumps([round(float(x), 6) for x in vec], separators=(",", ":"))


def wrap_untrusted_text(source_ref: str, text: str) -> str:
    """Wraps untrusted external GitHub prose in explicit XML boundary tags."""
    safe_ref = (
        source_ref.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    safe_body = _CLOSING_UNTRUSTED_RE.sub("&lt;/untrusted_repo_content&gt;", text or "")
    return f'<untrusted_repo_content source="{safe_ref}">\n{safe_body}\n</untrusted_repo_content>'


BACKFILL_STREAMS: tuple[str, ...] = ("issues", "prs", "discussions")


@dataclass(frozen=True)
class StreamBackfill:
    """Backfill progress for one GitHub stream of a repository.

    A non-None `cursor` means a backfill is in progress and resumes from that
    GraphQL `endCursor`. `done_at` records when the backfill reached the last
    page; while it is None, the stream's older history may be missing.
    """

    stream: str
    cursor: str | None
    done_at: str | None

    @property
    def complete(self) -> bool:
        """Whether the stream's full history has been fetched at least once."""
        return self.done_at is not None


@dataclass(frozen=True)
class RepoConfig:
    """Configuration and sync watermarks for a tracked repository."""

    repo_id: int
    owner: str
    name: str
    full_name: str
    local_path: str | None = None
    default_branch: str = "main"
    events_etag: str | None = None
    events_last_modified: str | None = None
    poll_interval_sec: int = 60
    last_events_check_at: str | None = None
    issues_watermark: str | None = None
    prs_watermark: str | None = None
    discussions_watermark: str | None = None
    last_full_sweep_at: str | None = None
    last_indexed_sha: str | None = None
    created_at: str = ""
    updated_at: str = ""
    issues_backfill_cursor: str | None = None
    issues_backfill_done_at: str | None = None
    prs_backfill_cursor: str | None = None
    prs_backfill_done_at: str | None = None
    discussions_backfill_cursor: str | None = None
    discussions_backfill_done_at: str | None = None

    def backfill(self, stream: str) -> StreamBackfill:
        """Returns the backfill progress for `stream`.

        Raises:
            ValueError: if `stream` is not one of BACKFILL_STREAMS.
        """
        if stream not in BACKFILL_STREAMS:
            raise ValueError(f"Unknown GitHub stream '{stream}'; expected one of {BACKFILL_STREAMS}.")
        return StreamBackfill(
            stream=stream,
            cursor=getattr(self, f"{stream}_backfill_cursor"),
            done_at=getattr(self, f"{stream}_backfill_done_at"),
        )


@dataclass(frozen=True)
class IssueRecord:
    """Normalized GitHub issue record."""

    repo_id: int
    number: int
    title: str
    state: str
    updated_at: str
    node_id: str | None = None
    author: str | None = None
    author_association: str | None = None
    body: str = ""
    labels: tuple[str, ...] = ()
    assignees: tuple[str, ...] = ()
    created_at: str | None = None
    closed_at: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class PrFileRecord:
    """File touched by a pull request."""

    repo_id: int
    pr_number: int
    file_path: str
    change_type: str = "MODIFIED"
    additions: int = 0
    deletions: int = 0


@dataclass(frozen=True)
class ReviewRecord:
    """Top-level pull request review."""

    repo_id: int
    review_id: str
    pr_number: int
    state: str
    author: str | None = None
    author_association: str | None = None
    body: str = ""
    submitted_at: str | None = None


@dataclass(frozen=True)
class ReviewThreadRecord:
    """Inline pull request review thread."""

    thread_id: str
    repo_id: int
    pr_number: int
    file_path: str | None = None
    line: int | None = None
    start_line: int | None = None
    is_resolved: bool = False
    is_outdated: bool = False
    reviewer: str | None = None
    author_association: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    body: str = ""
    comments_json: str = "[]"


@dataclass(frozen=True)
class CommentRecord:
    """Issue or pull request timeline comment."""

    repo_id: int
    comment_id: str
    parent_kind: str
    parent_number: int
    body: str
    author: str | None = None
    author_association: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class IssuePrLink:
    """Materialized cross-reference between an issue and a pull request."""

    repo_id: int
    issue_number: int
    pr_number: int
    link_type: str = "references"


@dataclass(frozen=True)
class PrRecord:
    """Normalized GitHub pull request record with nested children."""

    repo_id: int
    number: int
    title: str
    state: str
    updated_at: str
    node_id: str | None = None
    is_draft: bool = False
    author: str | None = None
    author_association: str | None = None
    body: str = ""
    labels: tuple[str, ...] = ()
    base_ref: str | None = None
    head_ref: str | None = None
    head_sha: str | None = None
    merge_commit_sha: str | None = None
    review_decision: str | None = None
    mergeable: str | None = None
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    created_at: str | None = None
    merged_at: str | None = None
    closed_at: str | None = None
    url: str | None = None
    files: tuple[PrFileRecord, ...] = field(default_factory=tuple)
    reviews: tuple[ReviewRecord, ...] = field(default_factory=tuple)
    review_threads: tuple[ReviewThreadRecord, ...] = field(default_factory=tuple)
    comments: tuple[CommentRecord, ...] = field(default_factory=tuple)
    issue_links: tuple[IssuePrLink, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DiscussionRecord:
    """Normalized GitHub discussion record."""

    repo_id: int
    number: int
    title: str
    updated_at: str
    node_id: str | None = None
    category: str | None = None
    author: str | None = None
    body: str = ""
    answer_body: str | None = None
    created_at: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class CommitFileRecord:
    """File modified by a git commit."""

    repo_id: int
    sha: str
    file_path: str
    change_type: str = "M"
    old_path: str | None = None
    additions: int = 0
    deletions: int = 0


@dataclass(frozen=True)
class CommitRecord:
    """Git commit metadata on the canonical branch."""

    repo_id: int
    sha: str
    committed_at: str
    title: str
    author_name: str | None = None
    author_email: str | None = None
    message: str = ""
    pr_number: int | None = None
    files: tuple[CommitFileRecord, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CodeSymbolRecord:
    """AST symbol span indexed from the canonical branch."""

    repo_id: int
    file_path: str
    symbol_name: str
    kind: str
    line_start: int
    line_end: int
    signature: str
    sha: str
    docstring: str = ""


@dataclass(frozen=True)
class DocSectionRecord:
    """Markdown section span indexed from the canonical branch."""

    repo_id: int
    file_path: str
    section_id: int
    level: int
    heading: str
    breadcrumb: str
    anchor_slug: str
    line_start: int
    line_end: int
    subtree_line_end: int
    content: str
    sha: str


@dataclass(frozen=True)
class TextChunk:
    """Searchable text segment stored in chunks and chunks_fts."""

    chunk_id: str
    repo_id: int
    source_kind: str
    source_ref: str
    title: str
    text: str
    content_sha256: str
    is_untrusted: bool = True
    file_path: str | None = None
    item_number: int | None = None
    updated_at: str = ""


@dataclass(frozen=True)
class StoredEmbeddingRecord:
    """Persisted 384-dimensional vector embedding for a chunk."""

    chunk_id: str
    model_id: str
    content_sha256: str
    vector: tuple[float, ...]
    updated_at: str = ""


@dataclass(frozen=True)
class SearchFilters:
    """Predicate filters for hybrid, lexical, and vector search."""

    repo: str | None = None
    kinds: tuple[str, ...] = ()
    path_prefix: str | None = None
    state: str | None = None


@dataclass(frozen=True)
class SearchHit:
    """Ranked result returned by hybrid Reciprocal Rank Fusion search."""

    chunk_id: str
    repo_full_name: str
    source_kind: str
    source_ref: str
    title: str
    snippet: str
    rrf_score: float
    bm25_rank: int | None
    vector_rank: int | None
    cosine_score: float | None
    is_untrusted: bool
    file_path: str | None = None
    item_number: int | None = None


@dataclass
class SyncStats:
    """Summary counts from a repository sync run."""

    repo_full_name: str
    events_not_modified: bool = False
    skipped_reason: str | None = None
    issues_upserted: int = 0
    prs_upserted: int = 0
    discussions_upserted: int = 0
    commits_upserted: int = 0
    symbols_upserted: int = 0
    doc_sections_upserted: int = 0
    chunks_upserted: int = 0
    embeddings_updated: int = 0
    indexed_ref: str | None = None
    indexed_sha: str | None = None
    backfill_incomplete: list[str] = field(default_factory=list)
    duration_sec: float = 0.0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
