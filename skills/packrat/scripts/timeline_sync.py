"""Timeline stream: fetches PR and issue timeline events into `timeline_events`.

`sync_repo_timelines()` picks stored items whose timeline is stale
(`PackratStore.timeline_candidates`) or that other items mentioned
(`PackratStore.take_timeline_dirty`), fetches up to 20 of them per GraphQL
query through aliased `node(id:)` lookups with a per-item `since:` argument,
and writes the parsed events with `PackratStore.upsert_timeline_events`.

An item is marked synced (`mark_timeline_synced`) only after every page of
its timeline was written, using the run's start time, so a failed or
interrupted run refetches it next time. Writes are idempotent upserts by
event id, which makes the `since` overlap safe.

Timeline text such as renamed titles and dismissal messages is untrusted; it
is kept in `payload_json` only.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from db_store import TIMELINE_SINCE_OVERLAP_S, PackratStore
from gh_transport import BudgetDeferred, GhRunner
from github_sync import _classify_gh_failure, _split_gh_include_output
from models import (
    RepoConfig,
    TimelineEventRecord,
    TimelineMode,
    TimelineStats,
    TimelineTarget,
    format_item_key,
    iso_at,
)
from progress import NULL_REPORTER, ProgressReporter
from rate_budget import Priority, RateBudget

PR_TIMELINE_TYPES: tuple[str, ...] = (
    "PULL_REQUEST_REVIEW",
    "READY_FOR_REVIEW_EVENT",
    "CONVERT_TO_DRAFT_EVENT",
    "REVIEW_REQUESTED_EVENT",
    "REVIEW_REQUEST_REMOVED_EVENT",
    "REVIEW_DISMISSED_EVENT",
    "HEAD_REF_FORCE_PUSHED_EVENT",
    "MERGED_EVENT",
    "CLOSED_EVENT",
    "REOPENED_EVENT",
    "CROSS_REFERENCED_EVENT",
    "CONNECTED_EVENT",
    "DISCONNECTED_EVENT",
    "REFERENCED_EVENT",
    "BASE_REF_CHANGED_EVENT",
    "AUTOMATIC_BASE_CHANGE_SUCCEEDED_EVENT",
    "AUTOMATIC_BASE_CHANGE_FAILED_EVENT",
    "AUTO_MERGE_ENABLED_EVENT",
    "AUTO_MERGE_DISABLED_EVENT",
    "ADDED_TO_MERGE_QUEUE_EVENT",
    "REMOVED_FROM_MERGE_QUEUE_EVENT",
    "HEAD_REF_DELETED_EVENT",
    "HEAD_REF_RESTORED_EVENT",
    "LABELED_EVENT",
    "UNLABELED_EVENT",
    "ASSIGNED_EVENT",
    "UNASSIGNED_EVENT",
    "RENAMED_TITLE_EVENT",
    "MILESTONED_EVENT",
    "DEMILESTONED_EVENT",
)
"""Pull request timeline item types fetched on a full (non-xref) pass."""

ISSUE_TIMELINE_TYPES: tuple[str, ...] = (
    "CLOSED_EVENT",
    "REOPENED_EVENT",
    "CROSS_REFERENCED_EVENT",
    "CONNECTED_EVENT",
    "DISCONNECTED_EVENT",
    "REFERENCED_EVENT",
    "LABELED_EVENT",
    "UNLABELED_EVENT",
    "ASSIGNED_EVENT",
    "UNASSIGNED_EVENT",
    "RENAMED_TITLE_EVENT",
    "MILESTONED_EVENT",
    "DEMILESTONED_EVENT",
    "MARKED_AS_DUPLICATE_EVENT",
    "UNMARKED_AS_DUPLICATE_EVENT",
    "TRANSFERRED_EVENT",
)
"""Issue timeline item types fetched on a full (non-xref) pass."""

XREF_TYPES: tuple[str, ...] = (
    "CROSS_REFERENCED_EVENT",
    "CONNECTED_EVENT",
    "DISCONNECTED_EVENT",
    "REFERENCED_EVENT",
)
"""Item types fetched when draining the cross-reference (dirty) queue."""

TIMELINE_BATCH_SIZE = 20
"""Items per aliased `node(id:)` query."""

TIMELINE_PAGE_SIZE = 100
"""`timelineItems(first:)` page size."""

SINCE_OVERLAP_S = TIMELINE_SINCE_OVERLAP_S
"""Seconds the store subtracts from the last sync time to form `since:`."""

MAX_EXTRA_PAGES = 20
"""Follow-up pages per item before it is marked synced anyway and reported as capped."""

GH_TIMEOUT_S = 30
"""Per-call `gh api graphql` timeout in seconds."""

_PERMANENT_ERROR_TYPES = frozenset({"NOT_FOUND", "FORBIDDEN"})
_LINKED_ITEM_TYPES = frozenset({"Issue", "PullRequest"})

_ACTOR_FRAGMENT = "fragment Actor on Actor { login __typename }"
_LINK_REF_FRAGMENT = (
    "fragment LinkRef on ReferencedSubject { __typename "
    "... on Issue { number repository { nameWithOwner } } "
    "... on PullRequest { number repository { nameWithOwner } } }"
)
_REVIEWER = (
    "requestedReviewer { __typename ... on User { login } ... on Bot { login } "
    "... on Mannequin { login } ... on Team { slug organization { login } } }"
)
_ASSIGNEE = (
    "assignee { __typename ... on User { login } ... on Bot { login } "
    "... on Mannequin { login } }"
)
_CLOSER = (
    "stateReason closer { __typename "
    "... on PullRequest { number repository { nameWithOwner } } "
    "... on Commit { oid } }"
)
_ITEM_REF = (
    "{ __typename ... on Issue { number repository { nameWithOwner } } "
    "... on PullRequest { number repository { nameWithOwner } } }"
)
_SHARED_SELECTIONS: tuple[tuple[str, str], ...] = (
    ("ClosedEvent", _CLOSER),
    ("ReopenedEvent", ""),
    ("CrossReferencedEvent", "willCloseTarget isCrossRepository source { ...LinkRef }"),
    ("ConnectedEvent", "subject { ...LinkRef }"),
    ("DisconnectedEvent", "subject { ...LinkRef }"),
    ("ReferencedEvent", "isCrossRepository commit { oid }"),
    ("LabeledEvent", "label { name }"),
    ("UnlabeledEvent", "label { name }"),
    ("AssignedEvent", _ASSIGNEE),
    ("UnassignedEvent", _ASSIGNEE),
    ("RenamedTitleEvent", "previousTitle currentTitle"),
    ("MilestonedEvent", "milestoneTitle"),
    ("DemilestonedEvent", "milestoneTitle"),
)
_PR_ONLY_SELECTIONS: tuple[tuple[str, str], ...] = (
    ("ReadyForReviewEvent", ""),
    ("ConvertToDraftEvent", ""),
    ("ReviewRequestedEvent", _REVIEWER),
    ("ReviewRequestRemovedEvent", _REVIEWER),
    ("ReviewDismissedEvent", "dismissalMessage review { author { ...Actor } }"),
    ("HeadRefForcePushedEvent", "beforeCommit { oid } afterCommit { oid }"),
    ("MergedEvent", "commit { oid } mergeRefName"),
    ("BaseRefChangedEvent", "previousRefName currentRefName"),
    ("AutomaticBaseChangeSucceededEvent", "oldBase newBase"),
    ("AutomaticBaseChangeFailedEvent", "oldBase newBase"),
    ("AutoMergeEnabledEvent", ""),
    ("AutoMergeDisabledEvent", "reason"),
    ("AddedToMergeQueueEvent", ""),
    ("RemovedFromMergeQueueEvent", "reason"),
    ("HeadRefDeletedEvent", "headRefName"),
    ("HeadRefRestoredEvent", ""),
)
_ISSUE_ONLY_SELECTIONS: tuple[tuple[str, str], ...] = (
    ("MarkedAsDuplicateEvent", f"duplicate {_ITEM_REF} canonical {_ITEM_REF}"),
    ("UnmarkedAsDuplicateEvent", ""),
    ("TransferredEvent", "fromRepository { nameWithOwner }"),
)


def _fragment(
    name: str, on_type: str, *, head: str, selections: Sequence[tuple[str, str]]
) -> str:
    """Renders one union fragment with an inline fragment per member type."""
    lines = [f"fragment {name} on {on_type} {{", "  __typename"]
    if head:
        lines.append(f"  {head}")
    for type_name, extra in selections:
        rest = f" {extra}" if extra else ""
        lines.append(
            f"  ... on {type_name} {{ id createdAt actor {{ ...Actor }}{rest} }}"
        )
    lines.append("}")
    return "\n".join(lines)


PR_TIMELINE_FRAGMENT = _fragment(
    "PrTimelineItem",
    "PullRequestTimelineItems",
    head="... on PullRequestReview { id createdAt submittedAt state author { ...Actor } }",
    selections=(*_PR_ONLY_SELECTIONS, *_SHARED_SELECTIONS),
)
"""Selection for `PullRequest.timelineItems` nodes, validated against GitHub."""

ISSUE_TIMELINE_FRAGMENT = _fragment(
    "IssueTimelineItem",
    "IssueTimelineItems",
    head="",
    selections=(*_SHARED_SELECTIONS, *_ISSUE_ONLY_SELECTIONS),
)
"""Selection for `Issue.timelineItems` nodes, validated against GitHub."""

_KIND_SHAPES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "pr": ("PullRequest", "PrTimelineItem", PR_TIMELINE_TYPES),
    "issue": ("Issue", "IssueTimelineItem", ISSUE_TIMELINE_TYPES),
}


def _kind_shape(item_kind: str) -> tuple[str, str, tuple[str, ...]]:
    """Returns `(GraphQL type, fragment name, full item types)` for an item kind.

    Raises:
        ValueError: If `item_kind` is neither "pr" nor "issue".
    """
    shape = _KIND_SHAPES.get(item_kind)
    if shape is None:
        raise ValueError(f"Unknown item kind '{item_kind}'; expected 'pr' or 'issue'.")
    return shape


def _alias_block(
    alias: str, target: TimelineTarget, *, xref_only: bool, after: str | None
) -> str:
    """Renders one `alias: node(id:)` block selecting the item's timeline page."""
    type_name, fragment_name, full_types = _kind_shape(target.item_kind)
    item_types = XREF_TYPES if xref_only else full_types
    args = [f"first: {TIMELINE_PAGE_SIZE}"]
    if after:
        args.append(f"after: {json.dumps(after)}")
    if target.since:
        args.append(f"since: {json.dumps(target.since)}")
    args.append(f"itemTypes: [{', '.join(item_types)}]")
    return (
        f"  {alias}: node(id: {json.dumps(target.node_id)}) {{\n"
        f"    __typename\n"
        f"    ... on {type_name} {{\n"
        f"      timelineItems({', '.join(args)}) {{\n"
        f"        pageInfo {{ hasNextPage endCursor }}\n"
        f"        nodes {{ ...{fragment_name} }}\n"
        f"      }}\n"
        f"    }}\n"
        f"  }}"
    )


def _document(blocks: Sequence[str], kinds: set[str]) -> str:
    """Assembles a query from alias blocks plus only the fragments they use.

    GraphQL rejects documents with unused fragments, so the PR and issue
    fragments are included only when a target of that kind is present.
    """
    fragments = [_ACTOR_FRAGMENT, _LINK_REF_FRAGMENT]
    if "pr" in kinds:
        fragments.append(PR_TIMELINE_FRAGMENT)
    if "issue" in kinds:
        fragments.append(ISSUE_TIMELINE_FRAGMENT)
    body = "\n".join(blocks)
    return (
        f"query {{\n  rateLimit {{ cost remaining resetAt }}\n{body}\n}}\n\n"
        + "\n".join(fragments)
        + "\n"
    )


def build_timeline_query(targets: Sequence[TimelineTarget], *, xref_only: bool) -> str:
    """Builds one GraphQL document fetching the first timeline page of each target.

    Each target becomes alias `t<i>` over `node(id:)` with a
    `timelineItems(first: 100, since:, itemTypes:)` selection; `since` is
    omitted for targets that were never fetched.

    Args:
        targets: Items to fetch, at most `TIMELINE_BATCH_SIZE` in practice.
        xref_only: Whether to fetch only `XREF_TYPES` instead of the full
            per-kind type list.

    Raises:
        ValueError: If `targets` is empty or holds an unknown item kind.
    """
    if not targets:
        raise ValueError("build_timeline_query requires at least one target")
    blocks = [
        _alias_block(f"t{i}", t, xref_only=xref_only, after=None)
        for i, t in enumerate(targets)
    ]
    return _document(blocks, {t.item_kind for t in targets})


def build_timeline_page_query(
    target: TimelineTarget, after: str, *, xref_only: bool
) -> str:
    """Builds a follow-up page query (alias `t0`) for one item's timeline after `after`."""
    block = _alias_block("t0", target, xref_only=xref_only, after=after)
    return _document([block], {target.item_kind})


# ----- parsing ---------------------------------------------------------------------

Extracted = tuple[str | None, str | None, dict[str, Any]]
"""`(subject, subject_type, payload)` produced by a per-type extractor."""


def _login(actor: Any) -> tuple[str | None, str | None]:
    """Returns `(login, __typename)` of an actor-like object, or `(None, None)`."""
    if not isinstance(actor, Mapping):
        return None, None
    return actor.get("login"), actor.get("__typename")


def _oid(obj: Any) -> str | None:
    """Returns `obj.oid` for a commit object, or None."""
    return obj.get("oid") if isinstance(obj, Mapping) else None


def _item_ref(ref: Any, default_repo: str) -> tuple[str | None, str | None]:
    """Maps an Issue / PullRequest reference to `(owner/name#N, __typename)`."""
    if not isinstance(ref, Mapping) or ref.get("__typename") not in _LINKED_ITEM_TYPES:
        return None, None
    number = ref.get("number")
    if number is None:
        return None, None
    repo = (ref.get("repository") or {}).get("nameWithOwner") or default_repo
    owner, _, name = str(repo).partition("/")
    return format_item_key(owner, name, int(number)), str(ref["__typename"])


def _pick(node: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    """Copies the given raw fields of `node` that are present into a payload dict."""
    return {k: node[k] for k in keys if k in node}


def _no_subject(_node: Mapping[str, Any], _repo: str) -> Extracted:
    return None, None, {}


def _review(node: Mapping[str, Any], _repo: str) -> Extracted:
    return node.get("state"), None, _pick(node, "state", "submittedAt", "createdAt")


def _reviewer(node: Mapping[str, Any], _repo: str) -> Extracted:
    reviewer = node.get("requestedReviewer")
    if not isinstance(reviewer, Mapping):
        return None, None, {}
    if reviewer.get("__typename") == "Team":
        org = (reviewer.get("organization") or {}).get("login") or ""
        return f"{org}/{reviewer.get('slug') or ''}", "Team", {}
    login, kind = _login(reviewer)
    return login, kind, {}


def _dismissed(node: Mapping[str, Any], _repo: str) -> Extracted:
    login, kind = _login((node.get("review") or {}).get("author"))
    return login, kind, _pick(node, "dismissalMessage")


def _force_pushed(node: Mapping[str, Any], _repo: str) -> Extracted:
    before, after = _oid(node.get("beforeCommit")), _oid(node.get("afterCommit"))
    return after, "Commit" if after else None, {"beforeOid": before, "afterOid": after}


def _merged(node: Mapping[str, Any], _repo: str) -> Extracted:
    oid = _oid(node.get("commit"))
    return oid, "Commit" if oid else None, _pick(node, "mergeRefName")


def _closed(node: Mapping[str, Any], repo: str) -> Extracted:
    payload = _pick(node, "stateReason")
    closer = node.get("closer")
    if isinstance(closer, Mapping) and closer.get("__typename") == "Commit":
        return _oid(closer), "Commit", payload
    key, kind = _item_ref(closer, repo)
    return key, kind, payload


def _cross_referenced(node: Mapping[str, Any], repo: str) -> Extracted:
    key, kind = _item_ref(node.get("source"), repo)
    return key, kind, _pick(node, "willCloseTarget", "isCrossRepository")


def _connected(node: Mapping[str, Any], repo: str) -> Extracted:
    key, kind = _item_ref(node.get("subject"), repo)
    return key, kind, {}


def _referenced(node: Mapping[str, Any], _repo: str) -> Extracted:
    oid = _oid(node.get("commit"))
    return oid, "Commit" if oid else None, _pick(node, "isCrossRepository")


def _ref_change(node: Mapping[str, Any], _repo: str) -> Extracted:
    current = node.get("currentRefName")
    return (
        current,
        "Ref" if current else None,
        _pick(node, "previousRefName", "currentRefName"),
    )


def _base_change(node: Mapping[str, Any], _repo: str) -> Extracted:
    new_base = node.get("newBase")
    return new_base, "Ref" if new_base else None, _pick(node, "oldBase", "newBase")


def _reason(node: Mapping[str, Any], _repo: str) -> Extracted:
    return None, None, _pick(node, "reason")


def _head_deleted(node: Mapping[str, Any], _repo: str) -> Extracted:
    ref = node.get("headRefName")
    return ref, "Ref" if ref else None, {}


def _label(node: Mapping[str, Any], _repo: str) -> Extracted:
    name = (node.get("label") or {}).get("name")
    return name, "Label" if name else None, {}


def _assignee(node: Mapping[str, Any], _repo: str) -> Extracted:
    login, kind = _login(node.get("assignee"))
    return login, kind, {}


def _renamed(node: Mapping[str, Any], _repo: str) -> Extracted:
    return None, None, _pick(node, "previousTitle", "currentTitle")


def _milestone(node: Mapping[str, Any], _repo: str) -> Extracted:
    title = node.get("milestoneTitle")
    return title, "Milestone" if title else None, {}


def _duplicate(node: Mapping[str, Any], repo: str) -> Extracted:
    key, kind = _item_ref(node.get("canonical"), repo)
    duplicate, _ = _item_ref(node.get("duplicate"), repo)
    return key, kind, {"duplicate": duplicate}


def _transferred(node: Mapping[str, Any], _repo: str) -> Extracted:
    source = (node.get("fromRepository") or {}).get("nameWithOwner")
    return source, "Repository" if source else None, {}


_EXTRACTORS: dict[str, Callable[[Mapping[str, Any], str], Extracted]] = {
    "PullRequestReview": _review,
    "ReadyForReviewEvent": _no_subject,
    "ConvertToDraftEvent": _no_subject,
    "ReviewRequestedEvent": _reviewer,
    "ReviewRequestRemovedEvent": _reviewer,
    "ReviewDismissedEvent": _dismissed,
    "HeadRefForcePushedEvent": _force_pushed,
    "MergedEvent": _merged,
    "ClosedEvent": _closed,
    "ReopenedEvent": _no_subject,
    "CrossReferencedEvent": _cross_referenced,
    "ConnectedEvent": _connected,
    "DisconnectedEvent": _connected,
    "ReferencedEvent": _referenced,
    "BaseRefChangedEvent": _ref_change,
    "AutomaticBaseChangeSucceededEvent": _base_change,
    "AutomaticBaseChangeFailedEvent": _base_change,
    "AutoMergeEnabledEvent": _no_subject,
    "AutoMergeDisabledEvent": _reason,
    "AddedToMergeQueueEvent": _no_subject,
    "RemovedFromMergeQueueEvent": _reason,
    "HeadRefDeletedEvent": _head_deleted,
    "HeadRefRestoredEvent": _no_subject,
    "LabeledEvent": _label,
    "UnlabeledEvent": _label,
    "AssignedEvent": _assignee,
    "UnassignedEvent": _assignee,
    "RenamedTitleEvent": _renamed,
    "MilestonedEvent": _milestone,
    "DemilestonedEvent": _milestone,
    "MarkedAsDuplicateEvent": _duplicate,
    "UnmarkedAsDuplicateEvent": _no_subject,
    "TransferredEvent": _transferred,
}
"""Per-`__typename` extractors; unknown types keep the raw node as payload."""


def _parse_node(
    target: TimelineTarget, node: Mapping[str, Any], repo_full_name: str
) -> TimelineEventRecord | None:
    """Maps one timeline node to a record, or None when it has no id or timestamp."""
    type_name = str(node.get("__typename") or "")
    is_review = type_name == "PullRequestReview"
    created_at = (node.get("submittedAt") if is_review else None) or node.get(
        "createdAt"
    )
    if not node.get("id") or not created_at:
        return None
    extractor = _EXTRACTORS.get(type_name)
    if extractor is None:
        subject, subject_type, payload = None, None, dict(node)
    else:
        subject, subject_type, payload = extractor(node, repo_full_name)
    actor, actor_type = _login(node.get("author") if is_review else node.get("actor"))
    return TimelineEventRecord(
        event_id=str(node["id"]),
        repo_id=target.repo_id,
        item_kind=target.item_kind,
        item_number=target.item_number,
        event_type=type_name,
        created_at=str(created_at),
        actor=actor,
        actor_type=actor_type,
        subject=subject,
        subject_type=subject_type,
        payload_json=json.dumps(payload, sort_keys=True) if payload else "{}",
    )


def parse_timeline_items(
    target: TimelineTarget,
    nodes: Sequence[dict[str, Any]],
    *,
    repo_full_name: str,
) -> list[TimelineEventRecord]:
    """Maps `timelineItems.nodes` of one item to `TimelineEventRecord`s.

    Linked issues and pull requests become `owner/name#N` subjects with
    `subject_type` "Issue" or "PullRequest"; team reviewers become
    `org/slug`. Reviews use `submittedAt` (falling back to `createdAt`) as
    `created_at`, the author as actor, and the review state as subject.
    Unknown types keep `subject=None` and the raw node in `payload_json`.

    Args:
        target: The item the nodes belong to.
        nodes: Raw GraphQL timeline nodes; null entries are skipped.
        repo_full_name: `owner/name` used for linked items that lack a
            repository (GitHub always sends one; this is a fallback).
    """
    records = []
    for node in nodes:
        if isinstance(node, Mapping):
            record = _parse_node(target, node, repo_full_name)
            if record is not None:
                records.append(record)
    return records


# ----- sync ------------------------------------------------------------------------


def _item_label(repo: RepoConfig, target: TimelineTarget) -> str:
    """Returns the `owner/name#N` key of a target for messages."""
    return format_item_key(repo.owner, repo.name, target.item_number)


def _retryable_aliases(payload: Mapping[str, Any]) -> set[str]:
    """Returns aliases whose GraphQL errors are worth retrying on a later run.

    `NOT_FOUND` (deleted item) and `FORBIDDEN` (inaccessible item) are
    permanent, so those aliases are not returned and their null nodes get
    marked synced instead of being refetched on every run.
    """
    retryable: set[str] = set()
    for err in payload.get("errors") or ():
        path = err.get("path") if isinstance(err, Mapping) else None
        if path and err.get("type") not in _PERMANENT_ERROR_TYPES:
            retryable.add(str(path[0]))
    return retryable


def _loads_object(body: str) -> dict[str, Any] | None:
    """Parses a JSON object from a response body, or None."""
    try:
        payload = json.loads(body) if body.strip() else None
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


@dataclass
class _TimelineRun:
    """State of one `sync_repo_timelines` call: shared stats, stop flag, run start."""

    store: PackratStore
    repo: RepoConfig
    runner: GhRunner
    budget: RateBudget | None
    priority: Priority
    reporter: ProgressReporter
    started_at: str
    stats: TimelineStats = field(default_factory=TimelineStats)
    stopped: bool = False

    def query(self, query: str) -> dict[str, Any] | None:
        """Runs one budgeted GraphQL query and returns the response object.

        Returns None (setting `stopped` and recording the reason) when the
        budget denies the call or the response carries no `data` object.
        """
        is_budgeted = bool(getattr(self.runner, "is_budgeted_runner", False))
        if (
            self.budget is not None
            and not is_budgeted
            and not self.budget.allows("graphql", self.priority)
        ):
            self.stats.deferred = True
            self.stopped = True
            return None
        try:
            res = self.runner(
                ["api", "graphql", "-i", "-f", f"query={query}"], GH_TIMEOUT_S
            )
        except BudgetDeferred:
            self.stats.deferred = True
            self.stopped = True
            return None
        if res.error_kind == "deferred_rate_limit":
            self.stats.deferred = True
            self.stopped = True
            return None
        headers, body = _split_gh_include_output(res.stdout)
        if self.budget is not None and not is_budgeted:
            self.budget.record(headers, worker="timeline")
        payload = _loads_object(body)
        data = payload.get("data") if payload else None
        if not isinstance(data, dict):
            self._fail(*_classify_gh_failure(res, body))
            return None
        cost = (data.get("rateLimit") or {}).get("cost")
        self.stats.graphql_cost += int(cost or 0)
        return payload

    def _fail(self, kind: str, detail: str) -> None:
        """Records a whole-query failure and stops the run."""
        self.stats.errors.append(f"timeline: {kind}: {detail}")
        self.stats.deferred = self.stats.deferred or kind == "rate_limited"
        self.stopped = True

    def _collect_or_write(
        self,
        target: TimelineTarget,
        connection: Mapping[str, Any],
        events_out: list[TimelineEventRecord] | None,
    ) -> None:
        """Appends parsed timeline events to `events_out`, or writes immediately when None."""
        if events_out is None:
            self.write(target, connection)
            return
        events_out.extend(
            parse_timeline_items(
                target,
                connection.get("nodes") or [],
                repo_full_name=self.repo.full_name,
            )
        )

    def write(self, target: TimelineTarget, connection: Mapping[str, Any]) -> None:
        """Parses and upserts one page of a target's timeline."""
        events = parse_timeline_items(
            target, connection.get("nodes") or [], repo_full_name=self.repo.full_name
        )
        self.stats.events_written += self.store.upsert_timeline_events(
            events, repo_full_name=self.repo.full_name
        )

    def _full_synced_at(self, target: TimelineTarget) -> str:
        """Returns `max(run start, stored updated_at)` for a full (non-xref) fetch.

        A GitHub `updatedAt` ahead of the local clock would otherwise keep
        `updated_at > timeline_synced_at` true and refetch the item on every
        run. Both values use the `iso_at` format, so text comparison is valid.
        """
        updated_at = (
            self.store.get_item_updated_at(
                target.repo_id, target.item_kind, target.item_number
            )
            or ""
        )
        return max(self.started_at, updated_at)

    def mark(self, target: TimelineTarget, *, xref_only: bool) -> None:
        """Marks a target synced and counts it.

        xref-only fetches use the run's start time. Full fetches use the
        later of the run's start time and the item's stored `updated_at`
        (see `_full_synced_at`).
        """
        synced_at = self.started_at if xref_only else self._full_synced_at(target)
        self.store.mark_timeline_synced(
            target.repo_id,
            target.item_kind,
            target.item_number,
            synced_at=synced_at,
            xref_only=xref_only,
        )
        if xref_only:
            self.stats.xref_items_synced += 1
        else:
            self.stats.items_synced += 1

    def mark_batch(self, targets: Sequence[TimelineTarget], *, xref_only: bool) -> None:
        """Marks a batch of targets synced in one store transaction when available."""
        if not targets:
            return
        mark_many = getattr(self.store, "mark_timelines_synced", None)
        if mark_many is None:
            for target in targets:
                self.mark(target, xref_only=xref_only)
            return
        if xref_only:
            synced_at = self.started_at
        else:
            synced_at = max(self._full_synced_at(t) for t in targets)
        mark_many(
            self.repo.repo_id,
            [(t.item_kind, t.item_number) for t in targets],
            synced_at=synced_at,
            xref_only=xref_only,
        )
        if xref_only:
            self.stats.xref_items_synced += len(targets)
        else:
            self.stats.items_synced += len(targets)

    def follow_pages(
        self,
        target: TimelineTarget,
        connection: Mapping[str, Any],
        *,
        xref_only: bool,
        events_out: list[TimelineEventRecord] | None = None,
    ) -> bool:
        """Fetches follow-up pages up to `MAX_EXTRA_PAGES`.

        Returns:
            True when the item may be marked synced (last page reached or
            capped), False when a page fetch failed.
        """
        page_info = connection.get("pageInfo") or {}
        pages = 0
        while page_info.get("hasNextPage") and pages < MAX_EXTRA_PAGES:
            next_conn = self._fetch_page(target, page_info.get("endCursor"), xref_only)
            if next_conn is None:
                return False
            self._collect_or_write(target, next_conn, events_out)
            page_info = next_conn.get("pageInfo") or {}
            pages += 1
        if page_info.get("hasNextPage"):
            key = _item_label(self.repo, target)
            self.stats.capped_items.append(key)
            self.reporter.warn(
                self.repo.full_name,
                f"timeline: {key} has more than {MAX_EXTRA_PAGES + 1} pages; marked synced anyway",
            )
        return True

    def _fetch_page(
        self, target: TimelineTarget, cursor: str | None, xref_only: bool
    ) -> Mapping[str, Any] | None:
        """Fetches one follow-up page, or None after recording why it failed."""
        if not cursor:
            self.stats.errors.append(
                f"timeline: {_item_label(self.repo, target)}: missing endCursor"
            )
            return None
        payload = self.query(
            build_timeline_page_query(target, cursor, xref_only=xref_only)
        )
        if payload is None:
            return None
        conn = (payload["data"].get("t0") or {}).get("timelineItems")
        if not isinstance(conn, Mapping):
            self.stats.errors.append(
                f"timeline: {_item_label(self.repo, target)}: page unavailable"
            )
            return None
        return conn

    def sync_target(
        self,
        target: TimelineTarget,
        node: Any,
        *,
        failed: bool,
        xref_only: bool,
        events_out: list[TimelineEventRecord] | None = None,
    ) -> bool:
        """Collects or writes one item's pages and returns True when it may be marked synced.

        A null node (deleted or inaccessible item) or a node of another type
        is marked synced with no events, so it is not retried forever. A null
        node whose alias carried a non-NOT_FOUND error is left for retry.
        """
        if node is None and failed:
            self.stats.errors.append(
                f"timeline: {_item_label(self.repo, target)}: fetch failed"
            )
            return False
        conn = node.get("timelineItems") if isinstance(node, Mapping) else None
        if isinstance(conn, Mapping):
            self._collect_or_write(target, conn, events_out)
            if not self.follow_pages(
                target, conn, xref_only=xref_only, events_out=events_out
            ):
                return False
        if events_out is None:
            self.mark(target, xref_only=xref_only)
        return True

    def sync_batch(
        self, targets: Sequence[TimelineTarget], *, xref_only: bool
    ) -> set[tuple[str, int]]:
        """Fetches and stores one batch of up to `TIMELINE_BATCH_SIZE` items."""
        payload = self.query(build_timeline_query(targets, xref_only=xref_only))
        if payload is None:
            return set()
        failed = _retryable_aliases(payload)
        data = payload["data"]
        batch_events: list[TimelineEventRecord] = []
        synced_targets: list[TimelineTarget] = []
        for i, target in enumerate(targets):
            if self.stopped:
                break
            alias = f"t{i}"
            if self.sync_target(
                target,
                data.get(alias),
                failed=alias in failed,
                xref_only=xref_only,
                events_out=batch_events,
            ):
                synced_targets.append(target)
        if batch_events:
            self.stats.events_written += self.store.upsert_timeline_events(
                batch_events, repo_full_name=self.repo.full_name
            )
        self.mark_batch(synced_targets, xref_only=xref_only)
        return {(t.item_kind, t.item_number) for t in synced_targets}

    def run_pass(
        self,
        fetch: Callable[[int], list[TimelineTarget]],
        *,
        xref_only: bool,
        max_batches: int,
    ) -> None:
        """Loops `fetch` in batches until it runs dry, the run stops, or `max_batches`.

        Targets already handled in this pass are skipped, so an item that
        stays a candidate (for example one updated or re-queued during the
        run) is fetched at most once per pass.
        """
        seen: set[tuple[str, int]] = set()
        unsynced_seen: set[tuple[str, int]] = set()
        phase = "timeline xrefs" if xref_only else "timeline"
        self.reporter.start_phase(self.repo.full_name, phase)
        for _ in range(max_batches):
            fresh = [
                t
                for t in fetch(TIMELINE_BATCH_SIZE + len(unsynced_seen))
                if (t.item_kind, t.item_number) not in seen
            ][:TIMELINE_BATCH_SIZE]
            if not fresh or self.stopped:
                break
            batch_keys = {(t.item_kind, t.item_number) for t in fresh}
            seen.update(batch_keys)
            synced_keys = self.sync_batch(fresh, xref_only=xref_only)
            unsynced_seen.update(batch_keys - synced_keys)
            self.reporter.advance(
                len(seen), detail=f"{self.stats.events_written} events"
            )
        self.reporter.finish_phase(f"{len(seen)} items")


def sync_repo_timelines(
    store: PackratStore,
    repo: RepoConfig,
    *,
    mode: TimelineMode,
    runner: GhRunner,
    budget: RateBudget | None,
    max_batches: int,
    drain_xrefs: bool,
    priority: Priority | None = None,
    reporter: ProgressReporter = NULL_REPORTER,
) -> TimelineStats:
    """Fetches stale timelines for one repository and optionally drains its xref queue.

    The candidate pass selects items with `store.timeline_candidates(mode)`;
    the xref pass (when `drain_xrefs`) takes items from
    `store.take_timeline_dirty` and fetches only `XREF_TYPES`. Each pass runs
    at most `max_batches` queries of `TIMELINE_BATCH_SIZE` items, not counting
    follow-up pages. The run stops early, with `deferred=True`, when the
    budget denies a call or GitHub reports a rate limit; other query failures
    stop it with an entry in `errors`.

    Args:
        store: Open store.
        repo: Repository whose items to fetch.
        mode: Which candidates the first pass selects.
        runner: `gh` runner. In the service it must gate at the same
            priority as `priority`.
        budget: Shared rate budget checked before each call and updated
            from response headers, or None to skip both.
        max_batches: Batch cap per pass.
        drain_xrefs: Whether to run the xref-queue pass after the candidate pass.
        priority: Budget priority; defaults to `BACKFILL` for `BACKFILL`
            mode and `MIRROR` otherwise.
        reporter: Progress sink.

    Returns:
        Counts, summed `rateLimit.cost`, capped item keys, the number of
        items still waiting for a first fetch, and any errors.
    """
    if priority is None:
        priority = (
            Priority.BACKFILL if mode == TimelineMode.BACKFILL else Priority.MIRROR
        )
    run = _TimelineRun(
        store=store,
        repo=repo,
        runner=runner,
        budget=budget,
        priority=priority,
        reporter=reporter,
        started_at=iso_at(),
    )
    run.run_pass(
        lambda limit: store.timeline_candidates(repo.repo_id, mode, limit),
        xref_only=False,
        max_batches=max_batches,
    )
    if drain_xrefs and not run.stopped:
        run.run_pass(
            lambda limit: store.take_timeline_dirty(repo.repo_id, limit),
            xref_only=True,
            max_batches=max_batches,
        )
    run.stats.pending_backfill = store.count_timeline_pending(repo.repo_id)
    return run.stats
