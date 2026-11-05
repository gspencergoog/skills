"""Data models, vector serialization helpers, and untrusted-content framing for packrat."""

from __future__ import annotations

import hashlib
import math
import re
import struct
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

EMBEDDING_DIM = 384
_PACK_FORMAT = f"<{EMBEDDING_DIM}f"
_PACK_BYTE_LEN = EMBEDDING_DIM * 4
_CLOSING_UNTRUSTED_RE = re.compile(
    r"</\s*untrusted_repo_content\b[^>]*>", re.IGNORECASE
)

RepoMode = Literal["mirror", "items"]
BackfillStream = Literal["issues", "prs", "discussions"]
SourceKind = Literal[
    "issue", "pr", "discussion", "commit", "code_symbol", "doc_section"
]
ParentKind = Literal["pr", "issue"]
ChangeKind = Literal[
    "pr",
    "checks",
    "review_requests",
    "reviews",
    "threads",
    "comments",
    "notification",
    "viewer",
]


def iso_at(now: float | None = None) -> str:
    """Formats epoch seconds (default: now) as ISO-8601 UTC with second precision."""
    moment = time.time() if now is None else now
    return datetime.fromtimestamp(moment, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now_iso() -> str:
    """Returns the current UTC timestamp in ISO-8601 format."""
    return iso_at()


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


def unpack_f32_vector(blob: bytes) -> tuple[float, ...]:
    """Unpacks a little-endian IEEE-754 float32 BLOB into a 384-float tuple."""
    if len(blob) != _PACK_BYTE_LEN:
        raise ValueError(f"Expected {_PACK_BYTE_LEN} bytes, got {len(blob)}")
    return struct.unpack(_PACK_FORMAT, blob)


def quantize_u8_vector(vec: Sequence[float]) -> tuple[bytes, float]:
    """Quantizes a 384-dim float vector to per-vector scaled offset uint8 bytes.

    Each component maps to `round(x / scale) + 128`, clipped to `[1, 255]`, so
    byte 128 represents 0.0 and the range is symmetric around it. `scale` is the
    largest absolute component divided by 127, so the largest component maps to
    byte 1 or 255.

    Args:
        vec: The vector to quantize, usually L2-normalized.

    Returns:
        A `(blob, scale)` pair, where `blob` holds one byte per component and
        `x ≈ (blob[i] - 128) * scale`.

    Raises:
        ValueError: If `vec` doesn't have `EMBEDDING_DIM` components.
    """
    if len(vec) != EMBEDDING_DIM:
        raise ValueError(f"Expected {EMBEDDING_DIM}-dim vector, got {len(vec)}")
    max_abs = max(abs(float(x)) for x in vec)
    scale = max(max_abs / 127.0, 1e-8)
    inv_scale = 1.0 / scale
    blob = bytes(max(1, min(255, round(float(x) * inv_scale) + 128)) for x in vec)
    return blob, scale


def dequantize_u8_vector(blob: bytes, scale: float) -> tuple[float, ...]:
    """Reconstructs a float vector from `quantize_u8_vector` output."""
    if len(blob) != EMBEDDING_DIM:
        raise ValueError(f"Expected {EMBEDDING_DIM} bytes, got {len(blob)}")
    s = float(scale)
    return tuple((b - 128) * s for b in blob)


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


BACKFILL_STREAMS: tuple[BackfillStream, ...] = ("issues", "prs", "discussions")

REPO_MODE_MIRROR: RepoMode = "mirror"
"""Repository mode: full history is backfilled, events are polled, and code is indexed."""

REPO_MODE_ITEMS: RepoMode = "items"
"""Repository mode: only items that are watched, notified, or in a viewer set are stored."""

_ITEM_KEY_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#([0-9]+)$")


def format_item_key(owner: str, name: str, number: int) -> str:
    """Formats the canonical `owner/name#number` key for an issue or pull request."""
    return f"{owner}/{name}#{int(number)}"


def parse_item_key(key: str) -> tuple[str, str, int]:
    """Parses an `owner/name#number` item key.

    Args:
        key: Item key such as "octo-org/widgets#123".

    Returns:
        `(owner, name, number)`.

    Raises:
        ValueError: If `key` is not of the form `owner/name#number`.
    """
    match = _ITEM_KEY_RE.match(key.strip())
    if match is None:
        raise ValueError(f"Invalid item key '{key}'; expected owner/name#number.")
    return match.group(1), match.group(2), int(match.group(3))


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
    mode: RepoMode = REPO_MODE_MIRROR

    @property
    def is_mirror(self) -> bool:
        """Whether packrat mirrors this repository's full history (not just watched items)."""
        return self.mode == REPO_MODE_MIRROR

    def backfill(self, stream: str) -> StreamBackfill:
        """Returns the backfill progress for `stream`.

        Raises:
            ValueError: if `stream` is not one of BACKFILL_STREAMS.
        """
        if stream not in BACKFILL_STREAMS:
            raise ValueError(
                f"Unknown GitHub stream '{stream}'; expected one of {BACKFILL_STREAMS}."
            )
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
    author_type: str | None = None


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
    parent_kind: ParentKind
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
    head_repo_owner: str | None = None
    is_cross_repository: bool | None = None
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
    source_kind: SourceKind
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
    source_kind: SourceKind
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


TEXT_LINK_TYPES: tuple[str, ...] = ("closes", "references")
"""`issue_pr_links.link_type` values derived from PR text; replaced on every PR save."""

TIMELINE_LINK_TYPES: tuple[str, ...] = ("closed_by", "xref")
"""`issue_pr_links.link_type` values derived from timeline events; kept across PR saves."""


class TimelineMode(StrEnum):
    """Which stored items a timeline sync run selects.

    Attributes:
        INCREMENTAL: Items fetched before whose `updated_at` is newer than
            their `timeline_synced_at`.
        BACKFILL: Items whose timeline has never been fetched
            (`timeline_synced_at IS NULL`).
        ALL: Both of the above.
    """

    INCREMENTAL = "incremental"
    BACKFILL = "backfill"
    ALL = "all"


@dataclass(frozen=True)
class TimelineEventRecord:
    """One issue or pull request timeline event.

    Attributes:
        event_id: GraphQL node id, so re-fetches upsert.
        item_kind: "pr" or "issue".
        event_type: GraphQL `__typename`, such as "ReadyForReviewEvent".
        actor_type: "User", "Bot", "Mannequin", or "Organization".
        subject: Reviewer login, team `org/slug`, label, assignee, ref name,
            linked item key `owner/name#N`, or commit sha.
        subject_type: "User", "Team", "Label", "Ref", "Issue",
            "PullRequest", "Commit", or "Milestone".
        payload_json: Raw event fields as JSON. Untrusted repository text.
    """

    event_id: str
    repo_id: int
    item_kind: str
    item_number: int
    event_type: str
    created_at: str
    actor: str | None = None
    actor_type: str | None = None
    subject: str | None = None
    subject_type: str | None = None
    payload_json: str = "{}"


@dataclass(frozen=True)
class TimelineTarget:
    """An issue or pull request whose timeline should be fetched.

    Attributes:
        item_kind: "pr" or "issue".
        node_id: GraphQL node id of the item.
        since: ISO-8601 UTC lower bound for `timelineItems(since:)`, or None
            for a first (backfill) fetch.
    """

    repo_id: int
    item_kind: str
    item_number: int
    node_id: str
    since: str | None


@dataclass
class TimelineStats:
    """Summary counts from a timeline sync run.

    JSON-safe on purpose: `SyncStats` is serialized with `asdict()` and
    `json.dumps`. Per-item new event types for a future change feed are left
    out of v1.

    Attributes:
        capped_items: Item keys that still had pages after the page cap.
        graphql_cost: Sum of `rateLimit.cost` over the run.
    """

    items_synced: int = 0
    events_written: int = 0
    xref_items_synced: int = 0
    pending_backfill: int = 0
    capped_items: list[str] = field(default_factory=list)
    graphql_cost: int = 0
    deferred: bool = False
    errors: list[str] = field(default_factory=list)


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
    deferred: bool = False
    deferred_streams: list[str] = field(default_factory=list)
    duration_sec: float = 0.0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    timeline: TimelineStats | None = None


@dataclass(frozen=True)
class PrLiveRecord:
    """Live pull request state fetched for a watched or viewer PR.

    Written by `PackratStore.upsert_pr_live()`, which creates the
    `pull_requests` row when it is missing (for `items` repositories) and
    otherwise updates only these columns, leaving mirror-only columns such as
    `body` untouched. `rollup_*` fields go to `pr_rollup` for `head_sha`.
    """

    repo_id: int
    number: int
    title: str
    state: str
    updated_at: str
    node_id: str | None = None
    is_draft: bool = False
    url: str | None = None
    author: str | None = None
    author_type: str | None = None
    base_ref: str | None = None
    head_ref: str | None = None
    head_sha: str | None = None
    head_committed_date: str | None = None
    head_repo_owner: str | None = None
    is_cross_repository: bool | None = None
    mergeable: str | None = None
    merge_state_status: str | None = None
    review_decision: str | None = None
    auto_merge_enabled_at: str | None = None
    created_at: str | None = None
    merged_at: str | None = None
    closed_at: str | None = None
    rollup_state: str | None = None
    rollup_total: int | None = None
    body: str | None = None


@dataclass(frozen=True)
class ItemCommentRecord:
    """A conversation comment on an issue or PR, as stored by live refreshes.

    Attributes:
        comment_id: GraphQL node id of the comment.
        author_type: "User", "Bot", or another actor `__typename`.
    """

    comment_id: str
    author: str | None = None
    author_type: str | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class CheckRunRecord:
    """One check run or commit status context on a pull request's head commit.

    Attributes:
        kind: "CheckRun" or "StatusContext".
        is_required: Whether branch protection requires this check, when known.
        run_key: Stable identity within the head commit, such as
            "CheckRun:123456"; see `key`.
    """

    name: str
    kind: str
    status: str | None = None
    conclusion: str | None = None
    details_url: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    is_required: bool | None = None
    workflow_run_id: int | None = None
    run_key: str | None = None

    @property
    def key(self) -> str:
        """Returns the row key: `run_key` when known, else `kind:name`.

        Check runs carry `CheckRun:<databaseId>`, so same-named runs (one job
        name in two workflows, or one workflow triggered by two events) stay
        separate rows. Status contexts are unique per commit by name.

        `statusCheckRollup.contexts` does not return superseded re-run
        attempts: on the live mirror (100 PRs, 45 same-name pairs) no two rows
        with one name share a `workflow_run_id`, which a re-run attempt would.
        So every row is a current result and readers should judge each one.
        """
        return self.run_key or f"{self.kind}:{self.name}"


@dataclass(frozen=True)
class ReviewRequestRecord:
    """A pending review request on a pull request.

    Attributes:
        reviewer: User login or `org/team-slug`.
        reviewer_kind: "User", "Team", "Bot", or "Mannequin".
    """

    reviewer: str
    reviewer_kind: str


@dataclass(frozen=True)
class OpinionatedReviewRecord:
    """The latest approving or change-requesting review per author.

    Attributes:
        author_type: "User" or "Bot".
        state: "APPROVED", "CHANGES_REQUESTED", "COMMENTED", or "DISMISSED".
    """

    author: str
    state: str
    author_type: str | None = None
    submitted_at: str | None = None


@dataclass(frozen=True)
class ThreadActivityRecord:
    """A review thread's resolution state and its last comment."""

    thread_id: str
    is_resolved: bool = False
    is_outdated: bool = False
    last_author: str | None = None
    last_author_type: str | None = None
    last_at: str | None = None


@dataclass(frozen=True)
class NotificationRecord:
    """One GitHub notification thread.

    Attributes:
        item_key: `owner/name#number` of the subject, or None when the subject
            is not an issue or pull request (for example a release).
        unread: Whether GitHub still marks the thread unread.
    """

    thread_id: str
    reason: str | None = None
    subject_type: str | None = None
    item_key: str | None = None
    updated_at: str | None = None
    unread: bool = False


@dataclass(frozen=True)
class WatchLease:
    """A consumer's lease on one item.

    Attributes:
        consumer: Consumer kind, such as "monitor" or "inflight".
        consumer_id: Consumer instance, such as a conversation hash.
        freshness_s: Maximum acceptable data age in seconds.
        lease_expires_at: ISO-8601 UTC time when the lease lapses unless renewed.
    """

    consumer: str
    consumer_id: str
    item_key: str
    freshness_s: int
    lease_expires_at: str


@dataclass(frozen=True)
class ChangeRecord:
    """One row of the change feed.

    Attributes:
        seq: Monotonic sequence number.
        kind: What changed: "pr", "checks", "review_requests", "reviews",
            "threads", "comments", "notification", or "viewer".
        fields: Names of the fields that changed.
        at: ISO-8601 UTC time of the change.
    """

    seq: int
    item_key: str
    kind: ChangeKind
    fields: tuple[str, ...]
    at: str


@dataclass(frozen=True)
class ServiceHeartbeat:
    """The `packrat serve` liveness record."""

    pid: int | None
    started_at: str | None
    beat_at: str | None
    version: str | None
