"""Data models and shared helpers for the /monitor skill."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from typing import Any

SCHEMA_VERSION = 1


class ReviewRole(StrEnum):
    """Role of the monitor session relative to the watched pull requests."""

    AUTHOR = "author"
    REVIEWER = "reviewer"


class ReviewStep(IntEnum):
    """Five-step progression index for a pull request in the reviewer queue."""

    REQUESTED = 0
    WAITING_ON_AUTHOR = 1
    RE_REVIEW_NEEDED = 2
    APPROVED = 3
    MERGED = 4


class ReviewSortTier(IntEnum):
    """Six-tier priority bucket for sorting reviewer queue rows."""

    RE_REVIEW_NEEDED = 0
    REVIEW_REQUESTED = 1
    DRAFT_REQUESTED = 2
    WAITING_ON_AUTHOR = 3
    APPROVED_OPEN = 4
    TERMINAL = 5


class TriageVerdict(StrEnum):
    """Discussion triage classification for post-review pull request comments."""

    ACTIVE = "active"
    LIKELY_SHELVED = "likely_shelved"
    DEFERRED = "deferred"


class DrawerReason(StrEnum):
    """Reason a pull request row is partitioned into the bottom collapsible drawer."""

    MUTED = "muted"
    DORMANT = "dormant"


class ReviewerEventKind(StrEnum):
    """Event kinds emitted when diffing pull request snapshots in reviewer mode."""

    NEW_REVIEW_REQUEST = "new_review_request"
    DRAFT_READY = "draft_ready"
    AUTHOR_HEAD_PUSH = "author_head_push"
    AUTHOR_THREAD_REPLY = "author_thread_reply"
    RE_REQUESTED_REVIEW = "re_requested_review"
    APPROVAL_DISMISSED = "approval_dismissed"
    TERMINAL = "terminal"


REVIEWER_EVENT_KINDS: tuple[str, ...] = tuple(k.value for k in ReviewerEventKind)

EVENT_KINDS: tuple[str, ...] = (
    "review_decision",
    "human_threads",
    "review_requests",
    "ci_check",
    "conflicts",
    "merge_state",
    "head_push",
    "base_change",
    "draft",
    "terminal",
    "stack_parent",
    "approval_dismissed",
    "auto_merge",
)

FAILED_CONCLUSIONS: frozenset[str] = frozenset(
    {"FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
)

_PR_KEY_RE = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)$")
_PR_URL_RE = re.compile(r"^https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)")
_BARE_NUM_RE = re.compile(r"^#?(\d+)$")


def pr_key(repo: str, number: int) -> str:
    """Formats a canonical PR key ``owner/repo#number``."""
    return f"{repo}#{number}"


def parse_pr_ref(text: str, default_repo: str | None = None) -> tuple[str, int]:
    """Parses ``owner/repo#N``, a GitHub PR URL, or ``N`` (with ``default_repo``)."""
    cleaned = text.strip()
    match = _PR_KEY_RE.match(cleaned) or _PR_URL_RE.match(cleaned)
    if match:
        return match.group(1), int(match.group(2))
    bare = _BARE_NUM_RE.match(cleaned)
    if bare and default_repo and "/" in default_repo:
        return default_repo, int(bare.group(1))
    raise ValueError(
        f"Invalid PR reference {text!r}; expected owner/repo#N, PR URL, or N with --repo"
    )


def parse_iso_seconds(raw: str | None) -> float | None:
    """Parses an ISO-8601 timestamp into UTC epoch seconds, returning ``None`` when invalid."""
    if not raw or not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def is_bot(author: dict[str, Any] | None) -> bool:
    """Returns whether a GraphQL author object represents a bot account."""
    if not author:
        return False
    typename = str(author.get("__typename") or "")
    login = str(author.get("login") or "")
    return typename == "Bot" or login.endswith("[bot]") or bool(author.get("is_bot"))


@dataclass
class CheckRun:
    """Normalized status check or workflow job run on a PR head commit."""

    name: str
    status: str
    conclusion: str | None = None
    required: bool = False
    url: str | None = None
    run_id: int | None = None
    started_at: str | None = None
    completed_at: str | None = None

    def is_failed(self) -> bool:
        """Returns whether the check completed with a failing conclusion."""
        return self.status == "COMPLETED" and (self.conclusion in FAILED_CONCLUSIONS)

    def is_completed(self) -> bool:
        """Returns whether the check has finished running."""
        return self.status == "COMPLETED"

    def to_dict(self) -> dict[str, Any]:
        """Serializes the check run into a JSON-compatible dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CheckRun:
        """Deserializes a ``CheckRun`` from stored state."""
        return cls(
            name=str(data["name"]),
            status=str(data["status"]),
            conclusion=data.get("conclusion"),
            required=bool(data.get("required", False)),
            url=data.get("url"),
            run_id=int(data["run_id"]) if data.get("run_id") is not None else None,
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
        )


@dataclass
class Thread:
    """Normalized review thread summary on a pull request."""

    id: str
    resolved: bool
    last_author: str
    last_author_is_bot: bool
    last_comment_at: str
    outdated: bool = False
    participants: tuple[str, ...] = ()

    def has_participant(self, login_norm: str) -> bool:
        """Returns whether ``login_norm`` participated in or last authored this thread."""
        if not login_norm:
            return False
        if self.participants:
            return any(p.lower() == login_norm for p in self.participants)
        return self.last_author.lower() == login_norm

    def to_dict(self) -> dict[str, Any]:
        """Serializes the thread into a JSON-compatible dictionary."""
        raw = asdict(self)
        raw["participants"] = list(self.participants)
        return raw

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Thread:
        """Deserializes a ``Thread`` from stored state, defaulting v9 fields cleanly."""
        raw_participants = data.get("participants") or ()
        participants = tuple(str(p) for p in raw_participants if str(p))
        return cls(
            id=str(data["id"]),
            resolved=bool(data["resolved"]),
            last_author=str(data.get("last_author") or ""),
            last_author_is_bot=bool(data.get("last_author_is_bot", False)),
            last_comment_at=str(data.get("last_comment_at") or ""),
            outdated=bool(data.get("outdated", False)),
            participants=participants,
        )


@dataclass(frozen=True)
class ViewerReview:
    """Most recent non-pending review submitted by the authenticated viewer."""

    state: str
    submitted_at: str
    commit_oid: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serializes the viewer review into a JSON-compatible dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ViewerReview:
        """Deserializes a ``ViewerReview`` from stored state."""
        return cls(
            state=str(data.get("state") or ""),
            submitted_at=str(data.get("submitted_at") or ""),
            commit_oid=data.get("commit_oid"),
        )


@dataclass(frozen=True)
class MuteEntry:
    """Cross-session mute state recorded for a pull request in ``review_overrides.json``."""

    muted_at: str
    head_sha: str
    updated_at: str
    was_requested: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serializes the mute entry into a JSON-compatible dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MuteEntry:
        """Deserializes a ``MuteEntry`` from JSON."""
        return cls(
            muted_at=str(data.get("muted_at") or ""),
            head_sha=str(data.get("head_sha") or ""),
            updated_at=str(data.get("updated_at") or ""),
            was_requested=bool(data.get("was_requested", False)),
            reason=str(data.get("reason") or ""),
        )


@dataclass(frozen=True)
class TriageEntry:
    """Watermarked discussion triage classification recorded in ``review_overrides.json``."""

    watermark: str
    verdict: TriageVerdict
    summary: str
    evaluated_at: str

    def to_dict(self) -> dict[str, Any]:
        """Serializes the triage entry into a JSON-compatible dictionary."""
        return {
            "watermark": self.watermark,
            "verdict": self.verdict.value,
            "summary": self.summary,
            "evaluated_at": self.evaluated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TriageEntry:
        """Deserializes a ``TriageEntry`` from JSON."""
        raw_verdict = str(data.get("verdict") or TriageVerdict.ACTIVE.value)
        try:
            verdict = TriageVerdict(raw_verdict)
        except ValueError:
            verdict = TriageVerdict.ACTIVE
        return cls(
            watermark=str(data.get("watermark") or ""),
            verdict=verdict,
            summary=str(data.get("summary") or ""),
            evaluated_at=str(data.get("evaluated_at") or ""),
        )


def _resolve_snapshot_coords(data: dict[str, Any]) -> tuple[str, str, int]:
    url = str(data["url"])
    repo = str(data["repo"])
    number = int(data["number"])
    if url:
        try:
            repo, number = parse_pr_ref(url)
        except ValueError:
            pass
    return url, repo, number


def _snapshot_turn_fields(data: dict[str, Any]) -> dict[str, Any]:
    raw_viewer_review = data.get("viewer_last_review")
    viewer_last_review = (
        ViewerReview.from_dict(raw_viewer_review) if isinstance(raw_viewer_review, dict) else None
    )
    return {
        "viewer_last_review": viewer_last_review,
        "viewer_opinionated_state": data.get("viewer_opinionated_state"),
        "head_committed_date": data.get("head_committed_date"),
        "viewer_requested_at": data.get("viewer_requested_at"),
        "viewer_last_comment_at": data.get("viewer_last_comment_at"),
        "author_last_comment_at": data.get("author_last_comment_at"),
        "discussion_watermark": data.get("discussion_watermark"),
    }


@dataclass
class PrSnapshot:
    """Point-in-time state of a watched GitHub pull request."""

    key: str
    number: int
    repo: str
    title: str
    url: str
    author: str
    mine: bool
    state: str
    draft: bool
    head_sha: str
    head_ref: str
    base_ref: str
    last_committer: str | None
    rollup_state: str | None
    mergeable: str
    merge_state: str
    review_decision: str | None
    requested_reviewers: list[str] = field(default_factory=list)
    assignees: list[str] = field(default_factory=list)
    approvals: list[str] = field(default_factory=list)
    changes_requested: list[str] = field(default_factory=list)
    checks: list[CheckRun] = field(default_factory=list)
    threads: list[Thread] = field(default_factory=list)
    auto_merge: bool = False
    stack_parent: str | None = None
    stack_parent_state: str | None = None
    truncated: list[str] = field(default_factory=list)
    updated_at: str = ""
    viewer_last_review: ViewerReview | None = None
    viewer_opinionated_state: str | None = None
    head_committed_date: str | None = None
    viewer_requested_at: str | None = None
    viewer_last_comment_at: str | None = None
    author_last_comment_at: str | None = None
    discussion_watermark: str | None = None

    def effective_required_checks(self) -> list[CheckRun]:
        """Returns all checks on the pull request so status and rollup stay consistent."""
        return list(self.checks)

    def required_summary(self) -> str:
        """Summarizes required checks as ``NONE``, ``FAILURE``, ``PENDING``, or ``SUCCESS``."""
        relevant = self.effective_required_checks()
        if not relevant:
            return "NONE"
        if any(c.is_failed() for c in relevant):
            return "FAILURE"
        if any(not c.is_completed() for c in relevant):
            return "PENDING"
        return "SUCCESS"

    def to_dict(self) -> dict[str, Any]:
        """Serializes the snapshot into a JSON-compatible dictionary."""
        raw = asdict(self)
        raw["threads"] = [t.to_dict() for t in self.threads]
        return raw

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PrSnapshot:
        """Deserializes a ``PrSnapshot`` from stored JSON state."""
        url, repo, number = _resolve_snapshot_coords(data)
        return cls(
            key=pr_key(repo, number),
            number=number,
            repo=repo,
            title=str(data["title"]),
            url=url,
            author=str(data["author"]),
            mine=bool(data["mine"]),
            state=str(data["state"]),
            draft=bool(data["draft"]),
            head_sha=str(data["head_sha"]),
            head_ref=str(data["head_ref"]),
            base_ref=str(data["base_ref"]),
            last_committer=data.get("last_committer"),
            rollup_state=data.get("rollup_state"),
            mergeable=str(data.get("mergeable") or "UNKNOWN"),
            merge_state=str(data.get("merge_state") or "UNKNOWN"),
            review_decision=data.get("review_decision"),
            requested_reviewers=list(data.get("requested_reviewers") or []),
            assignees=list(data.get("assignees") or []),
            approvals=list(data.get("approvals") or []),
            changes_requested=list(data.get("changes_requested") or []),
            checks=[CheckRun.from_dict(c) for c in (data.get("checks") or [])],
            threads=[Thread.from_dict(t) for t in (data.get("threads") or [])],
            auto_merge=bool(data.get("auto_merge", False)),
            stack_parent=data.get("stack_parent"),
            stack_parent_state=data.get("stack_parent_state"),
            truncated=list(data.get("truncated") or []),
            updated_at=str(data.get("updated_at") or ""),
            **_snapshot_turn_fields(data),
        )


def parse_event_list(
    raw: str | None, role: ReviewRole | str = ReviewRole.AUTHOR
) -> set[str] | None:
    """Parses and validates comma-separated event names against ``role``'s event kinds."""
    if not raw:
        return None
    items = {part.strip() for part in raw.split(",") if part.strip()}
    if not items:
        return None
    valid = REVIEWER_EVENT_KINDS if ReviewRole(role) == ReviewRole.REVIEWER else EVENT_KINDS
    unknown = sorted(items - set(valid))
    if unknown:
        raise ValueError(
            f"Unknown event kind(s): {', '.join(unknown)}. Valid kinds: {', '.join(valid)}"
        )
    return items


def _is_directly_requested_login(snap: PrSnapshot, viewer_norm: str) -> bool:
    return bool(viewer_norm) and any(r.lower() == viewer_norm for r in snap.requested_reviewers)


def _is_viewer_approved(snap: PrSnapshot, viewer_norm: str) -> bool:
    if snap.viewer_opinionated_state == "APPROVED":
        return True
    return bool(viewer_norm) and any(a.lower() == viewer_norm for a in snap.approvals)


def effective_viewer_review(snap: PrSnapshot, viewer: str) -> ViewerReview | None:
    """Returns ``snap.viewer_last_review``, or a fallback review from opinionated state."""
    if snap.viewer_last_review is not None:
        return snap.viewer_last_review
    viewer_norm = viewer.strip().lower()
    if snap.viewer_opinionated_state in ("APPROVED", "CHANGES_REQUESTED"):
        return ViewerReview(state=snap.viewer_opinionated_state, submitted_at="", commit_oid=None)
    if viewer_norm and any(a.lower() == viewer_norm for a in snap.changes_requested):
        return ViewerReview(state="CHANGES_REQUESTED", submitted_at="", commit_oid=None)
    if viewer_norm and any(a.lower() == viewer_norm for a in snap.approvals):
        return ViewerReview(state="APPROVED", submitted_at="", commit_oid=None)
    return None


def is_post_review_commit(snap: PrSnapshot, last: ViewerReview) -> bool:
    """Returns whether ``snap.head_sha`` moved to a commit authored after ``last.submitted_at``."""
    if not last.commit_oid or not snap.head_sha or snap.head_sha == last.commit_oid:
        return False
    commit_ts = parse_iso_seconds(snap.head_committed_date)
    review_ts = parse_iso_seconds(last.submitted_at)
    if commit_ts is not None and review_ts is not None:
        return commit_ts > review_ts
    return True


def _latest_human_peer_thread_ts(snap: PrSnapshot, viewer_norm: str) -> float | None:
    timestamps = [
        ts
        for t in snap.threads
        if not t.last_author_is_bot and t.last_author.lower() != viewer_norm
        for ts in (parse_iso_seconds(t.last_comment_at),)
        if ts is not None
    ]
    return max(timestamps) if timestamps else None


def has_unanswered_direct_request(snap: PrSnapshot, viewer: str) -> bool:
    """Returns whether ``viewer`` has a direct review request not answered by a newer comment."""
    viewer_norm = viewer.strip().lower()
    if not _is_directly_requested_login(snap, viewer_norm):
        return False
    v_comment_ts = parse_iso_seconds(snap.viewer_last_comment_at)
    req_ts = parse_iso_seconds(snap.viewer_requested_at)
    if v_comment_ts is None or req_ts is None or v_comment_ts <= req_ts:
        return True
    commit_ts = parse_iso_seconds(snap.head_committed_date)
    if commit_ts is not None and v_comment_ts <= commit_ts:
        return True
    author_ts = parse_iso_seconds(snap.author_last_comment_at)
    if author_ts is not None and author_ts > v_comment_ts:
        return True
    peer_thread_ts = _latest_human_peer_thread_ts(snap, viewer_norm)
    return peer_thread_ts is not None and peer_thread_ts > v_comment_ts


_has_unanswered_direct_request = has_unanswered_direct_request


def _has_post_review_human_reply(thread: Thread, last: ViewerReview, viewer_norm: str) -> bool:
    if thread.last_author_is_bot or thread.last_author.lower() == viewer_norm:
        return False
    if not thread.last_comment_at or not last.submitted_at:
        return False
    reply_ts = parse_iso_seconds(thread.last_comment_at)
    review_ts = parse_iso_seconds(last.submitted_at)
    if reply_ts is not None and review_ts is not None:
        return reply_ts > review_ts
    return thread.last_comment_at > last.submitted_at


def _is_viewer_thread_addressed(
    thread: Thread, last: ViewerReview, viewer_norm: str, post_review_commit: bool
) -> bool:
    if thread.resolved:
        return True
    if thread.outdated and post_review_commit:
        return True
    return _has_post_review_human_reply(thread, last, viewer_norm)


def _has_post_review_author_comment(snap: PrSnapshot, last: ViewerReview) -> bool:
    author_ts = parse_iso_seconds(snap.author_last_comment_at)
    review_ts = parse_iso_seconds(last.submitted_at)
    if author_ts is None or review_ts is None:
        return False
    viewer_comment_ts = parse_iso_seconds(snap.viewer_last_comment_at)
    anchor_ts = max(review_ts, viewer_comment_ts) if viewer_comment_ts is not None else review_ts
    return author_ts > anchor_ts


def _needs_re_review_unhydrated(
    snap: PrSnapshot, last: ViewerReview, viewer_norm: str, post_review_commit: bool
) -> bool:
    if any(
        not t.resolved and _has_post_review_human_reply(t, last, viewer_norm) for t in snap.threads
    ):
        return True
    if any(
        not t.resolved and not t.outdated and t.last_author.lower() == viewer_norm
        for t in snap.threads
    ):
        return False
    return post_review_commit or _has_post_review_author_comment(snap, last)


def _needs_re_review(
    snap: PrSnapshot, last: ViewerReview, viewer_norm: str, active_request: bool
) -> bool:
    if active_request:
        return True
    post_review_commit = is_post_review_commit(snap, last)
    if snap.threads and all(not t.participants for t in snap.threads):
        return _needs_re_review_unhydrated(snap, last, viewer_norm, post_review_commit)
    viewer_threads = [t for t in snap.threads if t.has_participant(viewer_norm)]
    if not viewer_threads:
        return post_review_commit or _has_post_review_author_comment(snap, last)
    if any(
        not _is_viewer_thread_addressed(t, last, viewer_norm, post_review_commit)
        for t in viewer_threads
    ):
        return False
    any_reply = any(_has_post_review_human_reply(t, last, viewer_norm) for t in viewer_threads)
    return post_review_commit or any_reply


def viewer_thread_progress(snap: PrSnapshot, viewer: str) -> tuple[int, int]:
    """Returns ``(addressed_count, total_viewer_threads)`` for ``viewer``'s inline threads."""
    viewer_norm = viewer.strip().lower()
    last = effective_viewer_review(snap, viewer_norm)
    if not viewer_norm or last is None:
        return (0, 0)
    viewer_threads = [t for t in snap.threads if t.has_participant(viewer_norm)]
    if not viewer_threads:
        return (0, 0)
    post_commit = is_post_review_commit(snap, last)
    addressed = sum(
        1 for t in viewer_threads if _is_viewer_thread_addressed(t, last, viewer_norm, post_commit)
    )
    return (addressed, len(viewer_threads))


def classify_review_turn(snap: PrSnapshot, viewer: str) -> tuple[ReviewStep, ReviewSortTier]:
    """Computes the 5-step progression index and 6-tier sort bucket for a reviewer row."""
    if snap.state == "MERGED":
        return (ReviewStep.MERGED, ReviewSortTier.TERMINAL)
    if snap.state == "CLOSED":
        return (ReviewStep.REQUESTED, ReviewSortTier.TERMINAL)
    viewer_norm = viewer.strip().lower()
    last = effective_viewer_review(snap, viewer_norm)
    if snap.draft:
        step = ReviewStep.WAITING_ON_AUTHOR if last is not None else ReviewStep.REQUESTED
        return (step, ReviewSortTier.DRAFT_REQUESTED)

    direct_requested = _is_directly_requested_login(snap, viewer_norm)
    active_request = has_unanswered_direct_request(snap, viewer_norm)
    if _is_viewer_approved(snap, viewer_norm) and not active_request:
        return (ReviewStep.APPROVED, ReviewSortTier.APPROVED_OPEN)
    if direct_requested and not active_request:
        return (ReviewStep.WAITING_ON_AUTHOR, ReviewSortTier.WAITING_ON_AUTHOR)

    if last is None:
        return (ReviewStep.REQUESTED, ReviewSortTier.REVIEW_REQUESTED)
    if _needs_re_review(snap, last, viewer_norm, active_request):
        return (ReviewStep.RE_REVIEW_NEEDED, ReviewSortTier.RE_REVIEW_NEEDED)
    return (ReviewStep.WAITING_ON_AUTHOR, ReviewSortTier.WAITING_ON_AUTHOR)


def _has_post_mute_push(snap: PrSnapshot, mute: MuteEntry, muted_ts: float | None) -> bool:
    if mute.head_sha and snap.head_sha:
        return snap.head_sha != mute.head_sha
    commit_ts = parse_iso_seconds(snap.head_committed_date)
    return muted_ts is not None and commit_ts is not None and commit_ts > muted_ts


def _has_post_mute_request(
    snap: PrSnapshot, mute: MuteEntry, viewer_norm: str, muted_ts: float | None
) -> bool:
    req_ts = parse_iso_seconds(snap.viewer_requested_at)
    if req_ts is not None and muted_ts is not None:
        return req_ts > muted_ts
    return _is_directly_requested_login(snap, viewer_norm) and not mute.was_requested


def is_mute_active(snap: PrSnapshot, mute: MuteEntry | None, viewer: str) -> bool:
    """Returns whether ``mute`` still suppresses ``snap`` without post-mute activity."""
    if mute is None or snap.state != "OPEN":
        return False
    muted_ts = parse_iso_seconds(mute.muted_at)
    if _has_post_mute_push(snap, mute, muted_ts):
        return False
    viewer_norm = viewer.strip().lower()
    return not _has_post_mute_request(snap, mute, viewer_norm, muted_ts)


def dormant_age_days(snap: PrSnapshot, now_epoch: float) -> int:
    """Returns whole days elapsed since ``snap.updated_at`` (clamped to ``>= 0``)."""
    updated_ts = parse_iso_seconds(snap.updated_at)
    if updated_ts is None:
        return 0
    return max(0, int((now_epoch - updated_ts) // 86400))


def is_dormant_review(
    snap: PrSnapshot,
    viewer: str,
    stale_days: int,
    now_epoch: float,
    pinned_at: str | None = None,
) -> bool:
    """Returns whether an open unrequested Waiting or Approved PR is idle past ``stale_days``."""
    if stale_days <= 0 or snap.state != "OPEN" or snap.draft:
        return False
    viewer_norm = viewer.strip().lower()
    if _is_directly_requested_login(snap, viewer_norm):
        return False
    if effective_viewer_review(snap, viewer_norm) is None:
        return False
    step, _tier = classify_review_turn(snap, viewer_norm)
    if step not in (ReviewStep.WAITING_ON_AUTHOR, ReviewStep.APPROVED):
        return False
    updated_ts = parse_iso_seconds(snap.updated_at)
    if updated_ts is None:
        return False
    pinned_ts = parse_iso_seconds(pinned_at)
    anchor_ts = max(updated_ts, pinned_ts) if pinned_ts is not None else updated_ts
    return (now_epoch - anchor_ts) >= (stale_days * 86400)


@dataclass
class Event:
    """A single state transition detected between two PR snapshots."""

    pr: str
    kind: str
    from_value: Any
    to_value: Any
    actionable: bool
    suggested_action: str | None
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serializes the event into the output schema dictionary."""
        return {
            "pr": self.pr,
            "kind": self.kind,
            "from": self.from_value,
            "to": self.to_value,
            "actionable": self.actionable,
            "suggested_action": self.suggested_action,
            "summary": self.summary,
            "detail": dict(self.detail),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Event:
        """Deserializes an ``Event`` from stored JSON."""
        return cls(
            pr=str(data["pr"]),
            kind=str(data["kind"]),
            from_value=data.get("from", data.get("from_value")),
            to_value=data.get("to", data.get("to_value")),
            actionable=bool(data.get("actionable", False)),
            suggested_action=data.get("suggested_action"),
            summary=str(data.get("summary") or ""),
            detail=dict(data.get("detail") or {}),
        )


def _parse_reported_threads(raw_threads: Any) -> dict[str, dict[str, str]]:
    if not isinstance(raw_threads, dict):
        return {}
    reported: dict[str, dict[str, str]] = {}
    for pr, val in raw_threads.items():
        if isinstance(val, list):
            reported[str(pr)] = {str(tid): "" for tid in val}
        elif isinstance(val, dict):
            reported[str(pr)] = {str(k): str(v) for k, v in val.items()}
    return reported


@dataclass
class MonitorState:
    """Persisted state across monitor invocations within a conversation."""

    schema: int = SCHEMA_VERSION
    snapshots: dict[str, PrSnapshot] = field(default_factory=dict)
    notifications_last_modified: str | None = None
    check_durations: dict[str, list[int]] = field(default_factory=dict)
    pushed_shas: dict[str, str] = field(default_factory=dict)
    reruns: dict[str, list[str]] = field(default_factory=dict)
    reported_threads: dict[str, dict[str, str]] = field(default_factory=dict)
    daemon: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> MonitorState:
        """Creates a fresh ``MonitorState`` with empty maps."""
        return cls()

    def prune(self, terminal_keys: set[str]) -> None:
        """Removes per-PR tracking maps for PRs that have reached a terminal state."""
        for key in terminal_keys:
            self.pushed_shas.pop(key, None)
            self.reruns.pop(key, None)
            self.reported_threads.pop(key, None)

    def to_dict(self) -> dict[str, Any]:
        """Serializes the monitor state into a JSON-compatible dictionary."""
        return {
            "schema": self.schema,
            "snapshots": {k: v.to_dict() for k, v in self.snapshots.items()},
            "notifications_last_modified": self.notifications_last_modified,
            "check_durations": {k: list(v) for k, v in self.check_durations.items()},
            "pushed_shas": dict(self.pushed_shas),
            "reruns": {k: list(v) for k, v in self.reruns.items()},
            "reported_threads": {k: dict(v) for k, v in self.reported_threads.items()},
            "daemon": dict(self.daemon),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MonitorState:
        """Deserializes a ``MonitorState`` from JSON, upgrading legacy keys cleanly."""
        schema = int(data.get("schema", SCHEMA_VERSION))
        if schema > SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported state schema version {schema} (max supported: {SCHEMA_VERSION})"
            )
        snapshots: dict[str, PrSnapshot] = {}
        aliases: dict[str, str] = {}
        for k, v in (data.get("snapshots") or {}).items():
            snap = PrSnapshot.from_dict(v)
            snapshots[snap.key] = snap
            if str(k) != snap.key:
                aliases[str(k)] = snap.key

        def rekey(raw_key: str) -> str:
            return aliases.get(str(raw_key), str(raw_key))

        reported = {
            rekey(k): v for k, v in _parse_reported_threads(data.get("reported_threads")).items()
        }
        return cls(
            schema=schema,
            snapshots=snapshots,
            notifications_last_modified=data.get("notifications_last_modified"),
            check_durations={
                str(k): [int(x) for x in v] for k, v in (data.get("check_durations") or {}).items()
            },
            pushed_shas={rekey(k): str(v) for k, v in (data.get("pushed_shas") or {}).items()},
            reruns={rekey(k): [str(x) for x in v] for k, v in (data.get("reruns") or {}).items()},
            reported_threads=reported,
            daemon=dict(data.get("daemon") or {}),
        )
