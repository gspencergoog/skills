"""Data models and shared helpers for the /monitor skill."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = 1

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
        return cls(
            key=str(data["key"]),
            number=int(data["number"]),
            repo=str(data["repo"]),
            title=str(data["title"]),
            url=str(data["url"]),
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
        )


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
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MonitorState:
        schema = int(data.get("schema", SCHEMA_VERSION))
        if schema > SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported state schema version {schema} (max supported: {SCHEMA_VERSION})"
            )
        return cls(
            schema=schema,
            snapshots={
                str(k): PrSnapshot.from_dict(v) for k, v in (data.get("snapshots") or {}).items()
            },
            notifications_last_modified=data.get("notifications_last_modified"),
            check_durations={
                str(k): [int(x) for x in v] for k, v in (data.get("check_durations") or {}).items()
            },
            pushed_shas={str(k): str(v) for k, v in (data.get("pushed_shas") or {}).items()},
            reruns={str(k): [str(x) for x in v] for k, v in (data.get("reruns") or {}).items()},
            reported_threads=_parse_reported_threads(data.get("reported_threads")),
        )
