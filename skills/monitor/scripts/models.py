"""Data models and shared helpers for the /monitor skill."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
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


def is_bot(author: dict[str, Any] | None) -> bool:
    """Returns True when a GraphQL author object represents a bot account."""
    if not author:
        return False
    typename = str(author.get("__typename") or "")
    login = str(author.get("login") or "")
    return typename == "Bot" or login.endswith("[bot]")


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
        return self.status == "COMPLETED" and (self.conclusion in FAILED_CONCLUSIONS)

    def is_completed(self) -> bool:
        return self.status == "COMPLETED"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CheckRun:
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Thread:
        return cls(
            id=str(data["id"]),
            resolved=bool(data["resolved"]),
            last_author=str(data.get("last_author") or ""),
            last_author_is_bot=bool(data.get("last_author_is_bot", False)),
            last_comment_at=str(data.get("last_comment_at") or ""),
        )


@dataclass(frozen=True)
class ViewerReview:
    """Most recent non-pending review submitted by the authenticated viewer."""

    state: str
    submitted_at: str
    commit_oid: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ViewerReview:
        return cls(
            state=str(data.get("state") or ""),
            submitted_at=str(data.get("submitted_at") or ""),
            commit_oid=data.get("commit_oid"),
        )


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
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PrSnapshot:
        url = str(data["url"])
        repo = str(data["repo"])
        number = int(data["number"])
        if url:
            try:
                repo, number = parse_pr_ref(url)
            except ValueError:
                pass
        raw_viewer_review = data.get("viewer_last_review")
        viewer_last_review = (
            ViewerReview.from_dict(raw_viewer_review)
            if isinstance(raw_viewer_review, dict)
            else None
        )
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
            viewer_last_review=viewer_last_review,
            viewer_opinionated_state=data.get("viewer_opinionated_state"),
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
    valid = (
        REVIEWER_EVENT_KINDS
        if ReviewRole(role) == ReviewRole.REVIEWER
        else EVENT_KINDS
    )
    unknown = sorted(items - set(valid))
    if unknown:
        raise ValueError(
            f"Unknown event kind(s): {', '.join(unknown)}. Valid kinds: {', '.join(valid)}"
        )
    return items


def _is_viewer_approved(snap: PrSnapshot, viewer_norm: str) -> bool:
    if snap.viewer_opinionated_state == "APPROVED":
        return True
    return bool(viewer_norm) and any(a.lower() == viewer_norm for a in snap.approvals)


def _needs_re_review(
    snap: PrSnapshot, last: ViewerReview, viewer_norm: str, direct_requested: bool
) -> bool:
    if direct_requested:
        return True
    if bool(last.commit_oid) and bool(snap.head_sha) and snap.head_sha != last.commit_oid:
        return True
    return any(
        not t.resolved
        and t.last_author.lower() != viewer_norm
        and bool(t.last_comment_at)
        and bool(last.submitted_at)
        and t.last_comment_at > last.submitted_at
        for t in snap.threads
    )


def classify_review_turn(snap: PrSnapshot, viewer: str) -> tuple[ReviewStep, ReviewSortTier]:
    """Computes the 5-step progression index and 6-tier sort bucket for a reviewer row."""
    if snap.state == "MERGED":
        return (ReviewStep.MERGED, ReviewSortTier.TERMINAL)
    if snap.state == "CLOSED":
        return (ReviewStep.REQUESTED, ReviewSortTier.TERMINAL)
    if snap.draft:
        step = (
            ReviewStep.WAITING_ON_AUTHOR
            if snap.viewer_last_review is not None
            else ReviewStep.REQUESTED
        )
        return (step, ReviewSortTier.DRAFT_REQUESTED)

    viewer_norm = viewer.strip().lower()
    direct_requested = bool(viewer_norm) and any(
        r.lower() == viewer_norm for r in snap.requested_reviewers
    )
    if _is_viewer_approved(snap, viewer_norm) and not direct_requested:
        return (ReviewStep.APPROVED, ReviewSortTier.APPROVED_OPEN)

    last = snap.viewer_last_review
    if last is None:
        return (ReviewStep.REQUESTED, ReviewSortTier.REVIEW_REQUESTED)
    if _needs_re_review(snap, last, viewer_norm, direct_requested):
        return (ReviewStep.RE_REVIEW_NEEDED, ReviewSortTier.RE_REVIEW_NEEDED)
    return (ReviewStep.WAITING_ON_AUTHOR, ReviewSortTier.WAITING_ON_AUTHOR)


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
        return cls()

    def prune(self, terminal_keys: set[str]) -> None:
        """Removes per-PR tracking maps for PRs that have reached a terminal state."""
        for key in terminal_keys:
            self.pushed_shas.pop(key, None)
            self.reruns.pop(key, None)
            self.reported_threads.pop(key, None)

    def to_dict(self) -> dict[str, Any]:
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
        rekey = lambda k: aliases.get(str(k), str(k))
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
