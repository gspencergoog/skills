"""`hot` worker: refreshes leased and dirty pull requests through GraphQL node lookups.

`HotRefresher.refresh_hot()` fetches up to 20 PRs per GraphQL query by node id
and writes them through the Wave 1 store methods, which append change-feed
rows only when something changed. The query is built per batch because
`isRequired(pullRequestNumber:)` takes a literal PR number, so each PR gets
its own aliased `node(id:)` block with an inline commit/rollup selection; the
number-independent fields live in one shared fragment.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from gh_transport import GhApiError
from models import (
    CheckRunRecord,
    ItemCommentRecord,
    LatestReviewRecord,
    OpinionatedReviewRecord,
    PrLiveRecord,
    ReviewRequestRecord,
    ThreadActivityRecord,
    format_item_key,
    parse_item_key,
)
from rate_budget import Priority

HOT_BATCH_SIZE = 20
NODE_ID_BATCH_SIZE = 50
MAX_EXTRA_PAGES = 10
"""Safety cap on follow-up pages per PR for review threads or check contexts."""

HOT_PR_FRAGMENT = """
fragment HotPrFields on PullRequest {
  id
  number
  title
  body
  url
  state
  isDraft
  mergedAt
  closedAt
  createdAt
  updatedAt
  repository { nameWithOwner }
  author { login __typename }
  baseRefName
  headRefName
  headRefOid
  isCrossRepository
  headRepositoryOwner { login }
  mergeable
  mergeStateStatus
  reviewDecision
  autoMergeRequest { enabledAt }
  reviewRequests(first: 20) {
    nodes {
      requestedReviewer {
        __typename
        ... on User { login }
        ... on Bot { login }
        ... on Mannequin { login }
        ... on Team { slug organization { login } }
      }
    }
  }
  latestOpinionatedReviews(first: 20) {
    nodes { state submittedAt author { login __typename } }
  }
  reviews(last: 20) {
    nodes { state submittedAt commit { oid } author { login __typename } }
  }
  reviewThreads(first: 50) {
    pageInfo { hasNextPage endCursor }
    nodes { ...HotThread }
  }
  comments(last: 10) { nodes { id createdAt author { login __typename } } }
  timelineItems(last: 20, itemTypes: [REVIEW_REQUESTED_EVENT]) {
    nodes {
      ... on ReviewRequestedEvent {
        createdAt
        requestedReviewer {
          __typename
          ... on User { login }
          ... on Bot { login }
          ... on Mannequin { login }
          ... on Team { slug organization { login } }
        }
      }
    }
  }
}

fragment HotThread on PullRequestReviewThread {
  id
  isResolved
  isOutdated
  firstComment: comments(first: 1) { nodes { author { login } } }
  comments(last: 10) { nodes { createdAt author { login __typename } } }
}
""".strip()


def _contexts_selection(number: int, after: str | None = None) -> str:
    """Returns the head-commit rollup selection with `isRequired` for PR `number`."""
    after_arg = f", after: {json.dumps(after)}" if after else ""
    return f"""commits(last: 1) {{
        nodes {{
          commit {{
            oid
            committedDate
            statusCheckRollup {{
              state
              contexts(first: 100{after_arg}) {{
                totalCount
                pageInfo {{ hasNextPage endCursor }}
                nodes {{
                  __typename
                  ... on CheckRun {{
                    databaseId
                    name
                    status
                    conclusion
                    detailsUrl
                    startedAt
                    completedAt
                    isRequired(pullRequestNumber: {number})
                    checkSuite {{ workflowRun {{ databaseId }} }}
                  }}
                  ... on StatusContext {{
                    context
                    state
                    targetUrl
                    createdAt
                    isRequired(pullRequestNumber: {number})
                  }}
                }}
              }}
            }}
          }}
        }}
      }}"""


def build_hot_query(targets: Sequence[tuple[str, int]]) -> str:
    """Builds one GraphQL document refreshing `(node_id, pr_number)` targets.

    Each target becomes alias `pr<i>` over `node(id:)` with the shared
    `HotPrFields` fragment and its own rollup selection carrying the literal
    PR number that `isRequired(pullRequestNumber:)` needs.
    """
    if not targets:
        raise ValueError("build_hot_query requires at least one target")
    blocks = [
        f"""  pr{i}: node(id: {json.dumps(node_id)}) {{
    ... on PullRequest {{
      ...HotPrFields
      {_contexts_selection(number)}
    }}
  }}"""
        for i, (node_id, number) in enumerate(targets)
    ]
    body = "\n".join(blocks)
    return f"query {{\n  rateLimit {{ cost remaining resetAt }}\n{body}\n}}\n\n{HOT_PR_FRAGMENT}\n"


def build_node_id_query(keys: Sequence[str]) -> str:
    """Builds an aliased `repository { pullRequest(number:) { id } }` lookup for `keys`."""
    blocks = []
    for i, key in enumerate(keys):
        owner, name, number = parse_item_key(key)
        blocks.append(
            f"  k{i}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) "
            f"{{ pullRequest(number: {number}) {{ id }} }}"
        )
    return "query {\n" + "\n".join(blocks) + "\n}\n"


def build_threads_page_query(node_id: str, after: str) -> str:
    """Builds a follow-up query for a PR's review threads after cursor `after`."""
    return f"""query {{
  node(id: {json.dumps(node_id)}) {{
    ... on PullRequest {{
      reviewThreads(first: 50, after: {json.dumps(after)}) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{ ...HotThread }}
      }}
    }}
  }}
}}

fragment HotThread on PullRequestReviewThread {{
  id
  isResolved
  isOutdated
  firstComment: comments(first: 1) {{ nodes {{ author {{ login }} }} }}
  comments(last: 10) {{ nodes {{ createdAt author {{ login __typename }} }} }}
}}
"""


def build_contexts_page_query(node_id: str, number: int, after: str) -> str:
    """Builds a follow-up query for a PR's head-commit check contexts after `after`."""
    return f"""query {{
  node(id: {json.dumps(node_id)}) {{
    ... on PullRequest {{
      {_contexts_selection(number, after)}
    }}
  }}
}}
"""


class GraphqlTransport(Protocol):
    """The subset of `gh_transport.GhTransport` the hot worker uses."""

    def graphql(
        self, query: str, variables: dict[str, Any], *, worker: str, priority: Priority
    ) -> dict[str, Any]:
        """Runs one GraphQL query and returns the decoded payload."""
        ...


# ----- parsing --------------------------------------------------------------------------


def _dict(value: Any) -> dict[str, Any]:
    """Returns `value` when it is a dict, else an empty dict."""
    return value if isinstance(value, dict) else {}


def _nodes(conn: Any) -> list[dict[str, Any]]:
    """Returns the dict nodes of a GraphQL connection."""
    raw = _dict(conn).get("nodes")
    return [n for n in raw if isinstance(n, dict)] if isinstance(raw, list) else []


def actor_type(actor: Any) -> str | None:
    """Maps a GraphQL actor's `__typename` to "User", "Bot", or the raw type name."""
    typename = _dict(actor).get("__typename")
    return str(typename) if typename else None


def _login(actor: Any) -> str | None:
    """Returns an actor's login, if any."""
    login = _dict(actor).get("login")
    return str(login) if login else None


def _head_commit(node: dict[str, Any]) -> dict[str, Any]:
    """Returns the PR's last commit object (`commits(last: 1)`), or {}."""
    commits = _nodes(node.get("commits"))
    return _dict(commits[0].get("commit")) if commits else {}


def _rollup(node: dict[str, Any]) -> dict[str, Any]:
    """Returns the head commit's `statusCheckRollup`, or {}."""
    return _dict(_head_commit(node).get("statusCheckRollup"))


def parse_check_context(ctx: dict[str, Any]) -> CheckRunRecord | None:
    """Converts one `statusCheckRollup.contexts` node into a CheckRunRecord."""
    required = ctx.get("isRequired")
    is_required = required if isinstance(required, bool) else None
    if ctx.get("__typename") == "CheckRun" and ctx.get("name"):
        run_id = _dict(_dict(ctx.get("checkSuite")).get("workflowRun")).get(
            "databaseId"
        )
        check_id = ctx.get("databaseId")
        return CheckRunRecord(
            name=str(ctx["name"]),
            kind="CheckRun",
            status=ctx.get("status"),
            conclusion=ctx.get("conclusion"),
            details_url=ctx.get("detailsUrl"),
            started_at=ctx.get("startedAt"),
            completed_at=ctx.get("completedAt"),
            is_required=is_required,
            workflow_run_id=run_id if isinstance(run_id, int) else None,
            run_key=f"CheckRun:{check_id}" if isinstance(check_id, int) else None,
        )
    if ctx.get("__typename") == "StatusContext" and ctx.get("context"):
        return CheckRunRecord(
            name=str(ctx["context"]),
            kind="StatusContext",
            conclusion=ctx.get("state"),
            details_url=ctx.get("targetUrl"),
            started_at=ctx.get("createdAt"),
            is_required=is_required,
        )
    return None


def parse_review_request(
    node: dict[str, Any], *, requested_at: str | None = None
) -> ReviewRequestRecord | None:
    """Converts one `reviewRequests` or `ReviewRequestedEvent` node; teams become `org/slug`."""
    reviewer = _dict(node.get("requestedReviewer"))
    kind = reviewer.get("__typename")
    raw_at = requested_at if requested_at is not None else node.get("requestedAt")
    ts = str(raw_at) if isinstance(raw_at, str) and raw_at else None
    if kind == "Team" and reviewer.get("slug"):
        org = _login(reviewer.get("organization"))
        slug = str(reviewer["slug"])
        return ReviewRequestRecord(
            reviewer=f"{org}/{slug}" if org else slug,
            reviewer_kind="Team",
            requested_at=ts,
        )
    login = _login(reviewer)
    if login and kind:
        return ReviewRequestRecord(
            reviewer=login, reviewer_kind=str(kind), requested_at=ts
        )
    return None


def _reviewer_request_key(rec: ReviewRequestRecord) -> str:
    """Returns the case-normalized lookup key for `rec` (`team:org/slug` or `login`)."""
    if rec.reviewer_kind == "Team":
        return f"team:{rec.reviewer.lower()}"
    return rec.reviewer.lower()


def _parse_requested_at_map(node: dict[str, Any]) -> dict[str, str]:
    """Maps normalized reviewer keys to their latest `ReviewRequestedEvent.createdAt`."""
    latest: dict[str, str] = {}
    for ev in _nodes(node.get("timelineItems")):
        created_at = ev.get("createdAt")
        if not isinstance(created_at, str) or not created_at:
            continue
        parsed = parse_review_request(ev)
        if parsed is None:
            continue
        key = _reviewer_request_key(parsed)
        if created_at >= latest.get(key, ""):
            latest[key] = created_at
    return latest


def _parse_review_requests(node: dict[str, Any]) -> list[ReviewRequestRecord]:
    """Parses `reviewRequests` nodes and attaches latest `requested_at` timestamps."""
    requested_at = _parse_requested_at_map(node)
    out: list[ReviewRequestRecord] = []
    for raw in _nodes(node.get("reviewRequests")):
        rec = parse_review_request(raw)
        if rec is None:
            continue
        ts = requested_at.get(_reviewer_request_key(rec)) or rec.requested_at
        out.append(
            ReviewRequestRecord(rec.reviewer, rec.reviewer_kind, ts) if ts else rec
        )
    return out


def _thread_participants(node: dict[str, Any]) -> tuple[str, ...]:
    """Returns ordered deduplicated logins across `firstComment` and `comments`."""
    seen: set[str] = set()
    ordered: list[str] = []
    for item in (*_nodes(node.get("firstComment")), *_nodes(node.get("comments"))):
        login = _login(item.get("author"))
        if login and login.lower() not in seen:
            seen.add(login.lower())
            ordered.append(login)
    return tuple(ordered)


def parse_thread(node: dict[str, Any]) -> ThreadActivityRecord | None:
    """Converts one review-thread node into resolution state, last comment, and participants."""
    if not node.get("id"):
        return None
    comments = _nodes(node.get("comments"))
    last = comments[-1] if comments else {}
    author = last.get("author")
    return ThreadActivityRecord(
        thread_id=str(node["id"]),
        is_resolved=bool(node.get("isResolved")),
        is_outdated=bool(node.get("isOutdated")),
        last_author=_login(author),
        last_author_type=actor_type(author),
        last_at=last.get("createdAt"),
        participants=_thread_participants(node),
    )


def item_comments_from_graphql(connection: object) -> tuple[ItemCommentRecord, ...]:
    """Parses a `comments(last: N) { nodes { id createdAt author { login __typename } } }`."""
    out: list[ItemCommentRecord] = []
    for node in _nodes(connection):
        if not node.get("id"):
            continue
        author = _dict(node.get("author"))
        out.append(
            ItemCommentRecord(
                comment_id=str(node["id"]),
                author=_login(author),
                author_type=actor_type(author),
                created_at=node.get("createdAt"),
            )
        )
    return tuple(out)


def _opt_bool(value: Any) -> bool | None:
    """Returns `value` if it is a bool, else None."""
    return value if isinstance(value, bool) else None


def parse_pr_live(repo_id: int, node: dict[str, Any]) -> PrLiveRecord:
    """Builds the PrLiveRecord for one `HotPrFields` node plus its head-commit rollup."""
    head = _head_commit(node)
    rollup = _rollup(node)
    total = _dict(rollup.get("contexts")).get("totalCount")
    author = node.get("author")
    return PrLiveRecord(
        repo_id=repo_id,
        number=int(node["number"]),
        title=str(node.get("title") or ""),
        state=str(node.get("state") or "OPEN"),
        updated_at=str(node.get("updatedAt") or ""),
        node_id=node.get("id"),
        is_draft=bool(node.get("isDraft")),
        url=node.get("url"),
        author=_login(author),
        author_type=actor_type(author),
        base_ref=node.get("baseRefName"),
        head_ref=node.get("headRefName"),
        head_sha=node.get("headRefOid") or head.get("oid"),
        head_committed_date=head.get("committedDate"),
        head_repo_owner=_login(node.get("headRepositoryOwner")),
        is_cross_repository=_opt_bool(node.get("isCrossRepository")),
        mergeable=node.get("mergeable"),
        merge_state_status=node.get("mergeStateStatus"),
        review_decision=node.get("reviewDecision"),
        auto_merge_enabled_at=_dict(node.get("autoMergeRequest")).get("enabledAt"),
        created_at=node.get("createdAt"),
        merged_at=node.get("mergedAt"),
        closed_at=node.get("closedAt"),
        rollup_state=rollup.get("state") if rollup else None,
        rollup_total=total if isinstance(total, int) else None,
        body=str(node.get("body") or ""),
    )


@dataclass
class HotPrSnapshot:
    """Everything one refresh learned about a PR, ready to write to the store."""

    record: PrLiveRecord
    checks: list[CheckRunRecord] = field(default_factory=list)
    review_requests: list[ReviewRequestRecord] = field(default_factory=list)
    reviews: list[OpinionatedReviewRecord] = field(default_factory=list)
    latest_reviews: list[LatestReviewRecord] = field(default_factory=list)
    threads: list[ThreadActivityRecord] = field(default_factory=list)
    comments: list[ItemCommentRecord] = field(default_factory=list)
    repo_full_name: str | None = None


def parse_latest_reviews(conn: Any) -> list[LatestReviewRecord]:
    """Extracts the latest non-pending review per author from a `reviews` connection."""
    by_author: dict[str, LatestReviewRecord] = {}
    for rev in _nodes(conn):
        login = _login(rev.get("author"))
        state = str(rev.get("state") or "")
        if not login or state in ("", "PENDING"):
            continue
        commit_oid = (rev.get("commit") or {}).get("oid") or None
        record = LatestReviewRecord(
            author=login,
            state=state,
            author_type=actor_type(rev.get("author")),
            submitted_at=rev.get("submittedAt"),
            commit_oid=commit_oid,
        )
        prior = by_author.get(login)
        if prior is None or (record.submitted_at or "") >= (prior.submitted_at or ""):
            by_author[login] = record
    return list(by_author.values())


def parse_hot_node(repo_id: int, node: dict[str, Any]) -> HotPrSnapshot:
    """Parses one aliased PR node (first pages of threads and contexts) into a snapshot."""
    contexts = _nodes(_dict(_rollup(node).get("contexts")))
    raw_repo = _dict(node.get("repository")).get("nameWithOwner")
    repo_full_name = (
        str(raw_repo) if isinstance(raw_repo, str) and "/" in raw_repo else None
    )
    reviews = [
        OpinionatedReviewRecord(
            author=str(_login(r.get("author"))),
            author_type=actor_type(r.get("author")),
            state=str(r.get("state") or "COMMENTED"),
            submitted_at=r.get("submittedAt"),
        )
        for r in _nodes(node.get("latestOpinionatedReviews"))
        if _login(r.get("author"))
    ]
    return HotPrSnapshot(
        record=parse_pr_live(repo_id, node),
        checks=[c for c in map(parse_check_context, contexts) if c is not None],
        review_requests=_parse_review_requests(node),
        reviews=reviews,
        latest_reviews=parse_latest_reviews(node.get("reviews")),
        threads=[t for t in map(parse_thread, _nodes(node.get("reviewThreads"))) if t],
        comments=list(item_comments_from_graphql(node.get("comments"))),
        repo_full_name=repo_full_name,
    )


def _page_info(conn: Any) -> str | None:
    """Returns `endCursor` when the connection reports another page, else None."""
    info = _dict(_dict(conn).get("pageInfo"))
    cursor = info.get("endCursor")
    return str(cursor) if info.get("hasNextPage") and cursor else None


# ----- worker ---------------------------------------------------------------------------


class HotStore(Protocol):
    """The `PackratStore` methods the hot worker uses."""

    def get_node_ids(self, item_keys: Iterable[str]) -> dict[str, str]: ...

    def set_node_id(self, item_key: str, node_id: str) -> None: ...

    def ensure_items_repo(self, owner: str, name: str) -> Any: ...

    def upsert_pr_live(
        self, record: PrLiveRecord, now: float | None = None
    ) -> list[str]: ...

    def replace_check_runs(
        self,
        repo_id: int,
        pr_number: int,
        head_oid: str,
        runs: Sequence[CheckRunRecord],
        now: float | None = None,
    ) -> bool: ...

    def replace_review_requests(
        self, repo_id: int, pr_number: int, requests: Sequence[ReviewRequestRecord]
    ) -> bool: ...

    def replace_latest_opinionated_reviews(
        self, repo_id: int, pr_number: int, reviews: Sequence[OpinionatedReviewRecord]
    ) -> bool: ...

    def replace_latest_reviews(
        self,
        repo_id: int,
        pr_number: int,
        reviews: Sequence[LatestReviewRecord],
        now: float | None = None,
    ) -> bool: ...

    def replace_thread_activity(
        self, repo_id: int, pr_number: int, threads: Sequence[ThreadActivityRecord]
    ) -> bool: ...

    def upsert_item_comments(
        self,
        repo_id: int,
        parent_kind: str,
        number: int,
        comments: Sequence[ItemCommentRecord],
    ) -> bool: ...


class HotRefresher:
    """Refreshes PRs by node id in batches and writes them to the store.

    All calls are charged at `Priority.HOT` by default. HOT is never deferred
    by the budget; it stops only on a GitHub rate-limit response.
    """

    def __init__(
        self,
        transport: GraphqlTransport,
        store: HotStore,
        *,
        batch_size: int = HOT_BATCH_SIZE,
        worker: str = "hot",
        priority: Priority = Priority.HOT,
    ) -> None:
        """Initializes the worker.

        Args:
            transport: Budgeted GraphQL transport (`GhTransport`).
            store: The packrat store.
            batch_size: PRs per GraphQL query.
            worker: Worker name recorded in `api_calls`.
            priority: Budget priority of every call.
        """
        self.transport = transport
        self.store = store
        self.batch_size = max(1, batch_size)
        self.worker = worker
        self.priority = priority

    def _query(self, query: str) -> dict[str, Any]:
        """Runs `query` through the transport and returns its `data` object."""
        payload = self.transport.graphql(
            query, {}, worker=self.worker, priority=self.priority
        )
        return _dict(payload.get("data"))

    def resolve_node_ids(self, keys: Sequence[str]) -> dict[str, str]:
        """Returns GraphQL node ids for `keys`, looking up and saving missing ones.

        Keys whose PR does not exist (or is not visible) are left out.
        """
        known = self.store.get_node_ids(keys)
        missing = [k for k in dict.fromkeys(keys) if k not in known]
        for start in range(0, len(missing), NODE_ID_BATCH_SIZE):
            chunk = missing[start : start + NODE_ID_BATCH_SIZE]
            data = self._query(build_node_id_query(chunk))
            for i, key in enumerate(chunk):
                node_id = _dict(_dict(data.get(f"k{i}")).get("pullRequest")).get("id")
                if node_id:
                    self.store.set_node_id(key, str(node_id))
                    known[key] = str(node_id)
        return known

    def _extra_threads(
        self, node_id: str, after: str | None
    ) -> list[ThreadActivityRecord]:
        """Fetches review threads beyond the first page."""
        out: list[ThreadActivityRecord] = []
        pages = 0
        while after and pages < MAX_EXTRA_PAGES:
            conn = _dict(
                self._query(build_threads_page_query(node_id, after)).get("node")
            ).get("reviewThreads")
            out.extend(t for t in map(parse_thread, _nodes(conn)) if t)
            after = _page_info(conn)
            pages += 1
        return out

    def _extra_contexts(
        self, node_id: str, number: int, after: str | None
    ) -> list[CheckRunRecord]:
        """Fetches head-commit check contexts beyond the first page."""
        out: list[CheckRunRecord] = []
        pages = 0
        while after and pages < MAX_EXTRA_PAGES:
            node = _dict(
                self._query(build_contexts_page_query(node_id, number, after)).get(
                    "node"
                )
            )
            conn = _dict(_rollup(node).get("contexts"))
            out.extend(c for c in map(parse_check_context, _nodes(conn)) if c)
            after = _page_info(conn)
            pages += 1
        return out

    def _complete(self, snap: HotPrSnapshot, node: dict[str, Any]) -> None:
        """Adds follow-up pages of threads and check contexts to `snap` when needed."""
        node_id = str(node.get("id") or "")
        snap.threads.extend(
            self._extra_threads(node_id, _page_info(node.get("reviewThreads")))
        )
        contexts = _dict(_rollup(node).get("contexts"))
        snap.checks.extend(
            self._extra_contexts(node_id, snap.record.number, _page_info(contexts))
        )

    def _write_threads(
        self, repo_id: int, number: int, threads: Sequence[ThreadActivityRecord]
    ) -> bool:
        """Replaces or upserts review thread activity on `self.store`."""
        replace_fn = getattr(self.store, "replace_thread_activity", None)
        if replace_fn is not None:
            return bool(replace_fn(repo_id, number, threads))
        upsert_fn = self.store.upsert_thread_activity
        return bool(upsert_fn(repo_id, number, threads))

    def _write(self, snap: HotPrSnapshot) -> bool:
        """Writes one snapshot through the store; True when anything changed."""
        rec = snap.record
        changed = bool(self.store.upsert_pr_live(rec))
        if rec.head_sha:
            changed |= self.store.replace_check_runs(
                rec.repo_id, rec.number, rec.head_sha, snap.checks
            )
        changed |= self.store.replace_review_requests(
            rec.repo_id, rec.number, snap.review_requests
        )
        changed |= self.store.replace_latest_opinionated_reviews(
            rec.repo_id, rec.number, snap.reviews
        )
        changed |= self.store.replace_latest_reviews(
            rec.repo_id, rec.number, snap.latest_reviews
        )
        changed |= self._write_threads(rec.repo_id, rec.number, snap.threads)
        changed |= self.store.upsert_item_comments(
            rec.repo_id, "pr", rec.number, snap.comments
        )
        return changed

    def _stored_repo_for_node(
        self, node: dict[str, Any], repo_name: str, requested_key: str | None
    ) -> Any:
        """Finds the stored repository for `node` or `requested_key`."""
        find_by_node = getattr(self.store, "find_repo_by_node_id", None)
        node_id = node.get("id")
        if find_by_node is not None and isinstance(node_id, str) and node_id:
            prior = find_by_node(node_id)
            if prior is not None and prior.full_name.lower() != repo_name.lower():
                return prior
        if requested_key is not None:
            req_owner, req_name, _ = parse_item_key(requested_key)
            return self.store.ensure_items_repo(req_owner, req_name)
        owner, name = repo_name.split("/", 1)
        return self.store.ensure_items_repo(owner, name)

    def _resolve_node_repo(
        self, node: dict[str, Any], repo_name: str, requested_key: str | None
    ) -> Any:
        """Resolves the canonical repository for `repo_name`, merging an alias when needed."""
        stored = self._stored_repo_for_node(node, repo_name, requested_key)
        merge_fn = getattr(self.store, "merge_repo_alias", None)
        if merge_fn is not None and stored.full_name.lower() != repo_name.lower():
            return merge_fn(stored.repo_id, repo_name)
        return stored

    def _apply_node(
        self, node: dict[str, Any], requested_key: str | None = None
    ) -> str | None:
        """Parses, completes, and stores one PR node; returns its key when it changed."""
        repo_name = str(_dict(node.get("repository")).get("nameWithOwner") or "")
        if "/" not in repo_name or node.get("number") is None:
            return None
        repo = self._resolve_node_repo(node, repo_name, requested_key)
        snap = parse_hot_node(repo.repo_id, node)
        self._complete(snap, node)
        if not self._write(snap):
            return None
        return format_item_key(repo.owner, repo.name, snap.record.number)

    def _refresh_batch(
        self,
        targets: Sequence[tuple[str, int]],
        requested_keys: Sequence[str] | None = None,
    ) -> list[str]:
        """Refreshes one batch of `(node_id, number)` targets, splitting on transient errors."""
        try:
            data = self._query(build_hot_query(targets))
        except GhApiError as exc:
            if len(targets) <= 1 or exc.status in (401, 403):
                raise
            mid = len(targets) // 2
            left_keys = requested_keys[:mid] if requested_keys is not None else None
            right_keys = requested_keys[mid:] if requested_keys is not None else None
            return self._refresh_batch(targets[:mid], left_keys) + self._refresh_batch(
                targets[mid:], right_keys
            )
        changed: list[str] = []
        for i in range(len(targets)):
            node = data.get(f"pr{i}")
            if isinstance(node, dict):
                req_key = (
                    requested_keys[i]
                    if requested_keys is not None and i < len(requested_keys)
                    else None
                )
                key = self._apply_node(node, req_key)
                if key:
                    changed.append(key)
        return changed

    def refresh_hot(self, keys: Sequence[str]) -> list[str]:
        """Refreshes the PRs in `keys` and returns the item keys whose stored data changed.

        Keys that aren't PRs (or aren't visible) are skipped. HOT is never
        deferred by the budget; it stops only on a GitHub rate-limit response,
        which raises `RateLimited` (a `BudgetDeferred`) from the transport.
        """
        unique_keys = list(dict.fromkeys(keys))
        node_ids = self.resolve_node_ids(unique_keys)
        resolved_keys = [k for k in unique_keys if k in node_ids]
        targets = [(node_ids[k], parse_item_key(k)[2]) for k in resolved_keys]
        changed: list[str] = []
        for start in range(0, len(targets), self.batch_size):
            end = start + self.batch_size
            changed.extend(
                self._refresh_batch(targets[start:end], resolved_keys[start:end])
            )
        return changed
