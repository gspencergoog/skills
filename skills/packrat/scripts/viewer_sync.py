"""The packrat service's `viewer` worker: the viewer's own PRs, review requests, and issues.

`ViewerSync` runs three GitHub searches (ported from `inflight`'s
`SEARCH_QUERY`) and records their members in the `viewer_sets` table:

- `authored`: PRs the viewer wrote (open, merged, or closed), updated within
  the lookback window, so recently merged PRs stay on the board.
- `review_requested`: open PRs that request the viewer's review. No lookback
  applies, because a pending request stays actionable however old the PR is.
- `issues`: issues (open or closed) that involve the viewer, updated within
  the lookback window.

PR search results carry only node ids, keys, and state. Full PR fields come
from the injected `HotRefresh`, so the PR field list lives in one place
(`hot_sync`). Issue fields and their latest comments are stored in `issues`
and `comments`. Repositories that are not yet registered are created in
`items` mode. The viewer's open PRs get a service-owned lease so the service's
lease sweep keeps them fresh. The viewer's login is saved in `service_state`
(`viewer_login`).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from db_store import PackratStore
from gh_transport import BudgetDeferred, GhApiError
from hot_sync import item_comments_from_graphql
from models import IssueRecord, ItemCommentRecord, format_item_key
from rate_budget import Priority

LOGGER = logging.getLogger(__name__)

WORKER_NAME = "viewer"
"""Worker name recorded in `api_calls`."""

SEARCH_PAGE_SIZE = 20
"""Search page size; `first: 50` returned HTTP 502 in the `inflight` spike."""

MIN_PAGE_SIZE = 5
"""Smallest page size tried after halving on server errors."""

MAX_SEARCH_PAGES = 25
"""Upper bound on search pages per refresh, matching `inflight`."""

VIEWER_LOGIN_STATE_KEY = "viewer_login"
"""`service_state` key holding the authenticated user's login (`v_service_health.viewer_login`)."""

VIEWER_CONSUMER = "packrat-viewer"
"""`watches.consumer` of the service-owned leases on the viewer's PRs."""

VIEWER_CONSUMER_ID = "viewer"
"""`watches.consumer_id` of the service-owned leases on the viewer's PRs."""

VIEWER_FRESHNESS_S = 600
"""Freshness requested by the viewer's leases, in seconds."""

VIEWER_LEASE_TTL_S = 1800
"""Lease lifetime in seconds: two 15-minute viewer intervals, so one missed run keeps it."""

SET_AUTHORED = "authored"
SET_REVIEW_REQUESTED = "review_requested"
SET_ISSUES = "issues"

_ALIAS_TO_SET: dict[str, str] = {
    "authored": SET_AUTHORED,
    "reviewRequested": SET_REVIEW_REQUESTED,
    "issues": SET_ISSUES,
}
"""Maps each search alias in `SEARCH_QUERY` to its `viewer_sets.set_name`."""

_ALIAS_VARIABLES: dict[str, tuple[str, str, str]] = {
    "authored": ("authored", "afterAuthored", "withAuthored"),
    "reviewRequested": ("reviewReq", "afterReview", "withReview"),
    "issues": ("issues", "afterIssues", "withIssues"),
}
"""Per alias: the query-string, cursor, and include-flag variable names."""

SEARCH_QUERY = """
query PackratViewerSearch(
  $authored: String!, $reviewReq: String!, $issues: String!, $first: Int!,
  $afterAuthored: String, $afterReview: String, $afterIssues: String,
  $withAuthored: Boolean!, $withReview: Boolean!, $withIssues: Boolean!
) {
  rateLimit { cost remaining limit resetAt }
  viewer { login }
  authored: search(type: ISSUE, query: $authored, first: $first, after: $afterAuthored)
    @include(if: $withAuthored) {
    pageInfo { hasNextPage endCursor }
    nodes { ...PrKey }
  }
  reviewRequested: search(type: ISSUE, query: $reviewReq, first: $first, after: $afterReview)
    @include(if: $withReview) {
    pageInfo { hasNextPage endCursor }
    nodes { ...PrKey }
  }
  issues: search(type: ISSUE, query: $issues, first: $first, after: $afterIssues)
    @include(if: $withIssues) {
    pageInfo { hasNextPage endCursor }
    nodes { ...IssueFields }
  }
}
fragment PrKey on PullRequest {
  id number state repository { nameWithOwner }
}
fragment IssueFields on Issue {
  id number title body state url createdAt updatedAt closedAt
  repository { nameWithOwner }
  author { login }
  authorAssociation
  labels(first: 20) { nodes { name } }
  assignees(first: 10) { nodes { login } }
  comments(last: 10) { nodes { id createdAt author { login __typename } } }
}
"""
"""Paged search over authored PRs, review requests, and issues.

The issue fragment selects every `IssueRecord` field, so an issue in a
`mirror` repository is refreshed rather than overwritten with blanks.
"""


class HotRefresh(Protocol):
    """Refreshes full PR fields by item key (satisfied by `hot_sync.HotRefresher`)."""

    def refresh_hot(self, keys: Sequence[str]) -> list[str]:
        """Refreshes the PRs and returns the item keys whose stored data changed."""
        ...


class GraphqlTransport(Protocol):
    """The part of `gh_transport.GhTransport` this worker uses."""

    def graphql(
        self,
        query: str,
        variables: Mapping[str, Any],
        *,
        worker: str,
        priority: Priority,
    ) -> dict[str, Any]:
        """Runs one budgeted GraphQL query and returns the decoded payload."""
        ...


@dataclass
class _SearchResult:
    """Raw search nodes and paging state per alias."""

    nodes: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {alias: [] for alias in _ALIAS_TO_SET}
    )
    cursors: dict[str, str | None] = field(
        default_factory=lambda: dict.fromkeys(_ALIAS_TO_SET)
    )
    active: dict[str, bool] = field(
        default_factory=lambda: dict.fromkeys(_ALIAS_TO_SET, True)
    )
    viewer_login: str | None = None

    def absorb(self, data: Mapping[str, Any]) -> None:
        """Adds one page's nodes and advances each active alias's cursor.

        An active alias missing from `data` is treated as finished.
        """
        viewer = data.get("viewer")
        if isinstance(viewer, dict) and isinstance(viewer.get("login"), str):
            self.viewer_login = viewer["login"]
        for alias in _ALIAS_TO_SET:
            if not self.active[alias]:
                continue
            connection = data.get(alias)
            if not isinstance(connection, dict):
                self.active[alias] = False
                continue
            self.nodes[alias].extend(
                n for n in connection.get("nodes") or [] if _is_node(n)
            )
            page_info = connection.get("pageInfo") or {}
            self.cursors[alias] = page_info.get("endCursor")
            self.active[alias] = bool(
                page_info.get("hasNextPage") and self.cursors[alias]
            )


def lookback_date(lookback_days: int, now: float) -> str:
    """Returns the UTC date `lookback_days` before `now`, as `YYYY-MM-DD`."""
    return datetime.fromtimestamp(now - lookback_days * 86400, UTC).strftime("%Y-%m-%d")


def search_strings(lookback_days: int, now: float) -> dict[str, str]:
    """Builds the three search strings, keyed by search alias."""
    since = f"updated:>={lookback_date(lookback_days, now)}"
    return {
        "authored": f"is:pr author:@me archived:false {since}",
        "reviewRequested": "is:pr is:open review-requested:@me archived:false",
        "issues": f"is:issue involves:@me archived:false {since}",
    }


def _is_node(node: Any) -> bool:
    """Search nodes are `{}` for non-matching types and None for hidden items."""
    return isinstance(node, dict) and bool(node)


def _logins(connection: Any, key: str) -> tuple[str, ...]:
    """Extracts `nodes[*].<key>` strings from a GraphQL connection."""
    nodes = connection.get("nodes") if isinstance(connection, dict) else None
    values = (n.get(key) for n in nodes or [] if isinstance(n, dict))
    return tuple(v for v in values if isinstance(v, str))


class ViewerSync:
    """Refreshes the viewer's PR and issue sets."""

    def __init__(
        self,
        transport: GraphqlTransport,
        store: PackratStore,
        hot: HotRefresh,
        *,
        clock: Callable[[], float] = time.time,
        page_size: int = SEARCH_PAGE_SIZE,
    ) -> None:
        """Initializes the worker.

        Args:
            transport: Budgeted GraphQL transport, usually a `GhTransport`.
            store: Store that receives viewer sets, issues, node ids, and leases.
            hot: Refresher for full PR fields of newly seen PRs.
            clock: Returns the current time in epoch seconds.
            page_size: Initial search page size; halved on server errors.
        """
        self._transport = transport
        self._store = store
        self._hot = hot
        self._clock = clock
        self._initial_page_size = page_size
        self._page_size = page_size

    def refresh_viewer(
        self, lookback_days: int = 7, now: float | None = None
    ) -> list[str]:
        """Runs the viewer searches and stores the results.

        Nothing is written unless every search page succeeds, so a failed
        search never looks like items leaving a set.

        Args:
            lookback_days: Only authored PRs and issues updated this many days
                ago or later are included.
            now: Current time in epoch seconds (default: the clock).

        Returns:
            Sorted item keys that entered or left any viewer set.

        Raises:
            BudgetDeferred: The budget denied a search request.
            GhApiError: A search failed even at the smallest page size.
        """
        moment = self._clock() if now is None else now
        result = self._search_all(search_strings(lookback_days, moment))
        if result.viewer_login:
            self._store.set_state(
                VIEWER_LOGIN_STATE_KEY, result.viewer_login, now=moment
            )
        authored_keys, authored_open = self._pr_keys(result.nodes["authored"])
        review_keys, review_open = self._pr_keys(result.nodes["reviewRequested"])
        open_prs: set[str] = set()
        open_prs.update(authored_open)
        open_prs.update(review_open)
        sets = {
            SET_AUTHORED: authored_keys,
            SET_REVIEW_REQUESTED: review_keys,
            SET_ISSUES: self._store_issues(result.nodes["issues"]),
        }
        changed: set[str] = set()
        new_prs: set[str] = set()
        for set_name, keys in sets.items():
            added, removed = self._store.replace_viewer_set(set_name, keys, now=moment)
            changed.update(added, removed)
            if set_name != SET_ISSUES:
                new_prs.update(added)
        pr_keys = sets[SET_AUTHORED] | sets[SET_REVIEW_REQUESTED]
        unleased = self._update_leases(open_prs, moment)
        # PRs that just closed or merged lose their lease here, so refresh them
        # once now; otherwise their live row could stay OPEN indefinitely (H2).
        self._refresh_new_prs(sorted((new_prs | unleased) & pr_keys))
        return sorted(changed)

    # ----- search -----------------------------------------------------------------

    def _search_all(self, strings: Mapping[str, str]) -> _SearchResult:
        """Pages every search until none has a next page, or the page cap is hit."""
        self._page_size = self._initial_page_size
        result = _SearchResult()
        for _ in range(MAX_SEARCH_PAGES):
            result.absorb(self._search_page(strings, result.cursors, result.active))
            if not any(result.active.values()):
                return result
        LOGGER.warning("viewer search stopped at the %d-page cap", MAX_SEARCH_PAGES)
        return result

    def _search_page(
        self,
        strings: Mapping[str, str],
        cursors: Mapping[str, str | None],
        active: Mapping[str, bool],
    ) -> dict[str, Any]:
        """Runs one search page, halving the page size on server errors down to 5."""
        while True:
            variables: dict[str, Any] = {"first": self._page_size}
            for alias, (query_var, cursor_var, flag_var) in _ALIAS_VARIABLES.items():
                variables[query_var] = strings[alias]
                variables[cursor_var] = cursors[alias]
                variables[flag_var] = active[alias]
            try:
                payload = self._transport.graphql(
                    SEARCH_QUERY,
                    variables,
                    worker=WORKER_NAME,
                    priority=Priority.VIEWER,
                )
            except GhApiError as exc:
                if exc.status < 500 or self._page_size <= MIN_PAGE_SIZE:
                    raise
                self._page_size = max(MIN_PAGE_SIZE, self._page_size // 2)
                LOGGER.info(
                    "viewer search failed (%s); retrying with first=%d",
                    exc,
                    self._page_size,
                )
                continue
            data = payload.get("data")
            return data if isinstance(data, dict) else {}

    # ----- results ----------------------------------------------------------------

    def _node_key(self, node: Mapping[str, Any]) -> tuple[int, str] | None:
        """Returns `(repo_id, canonical_key)` for a search node, registering its repo."""
        repo = node.get("repository") or {}
        full_name = repo.get("nameWithOwner") if isinstance(repo, dict) else None
        number = node.get("number")
        if (
            not isinstance(full_name, str)
            or "/" not in full_name
            or not isinstance(number, int)
        ):
            return None
        node_id = node.get("id")
        if isinstance(node_id, str) and node_id:
            prior = self._store.find_repo_by_node_id(node_id)
            if prior is not None and prior.full_name.lower() != full_name.lower():
                config = self._store.merge_repo_alias(prior.repo_id, full_name)
                return config.repo_id, format_item_key(
                    config.owner, config.name, number
                )
        owner, name = full_name.split("/", 1)
        config = self._store.ensure_items_repo(owner, name)
        if config.full_name.lower() != full_name.lower():
            config = self._store.merge_repo_alias(config.repo_id, full_name)
        return config.repo_id, format_item_key(config.owner, config.name, number)

    def _pr_keys(self, nodes: Iterable[Mapping[str, Any]]) -> tuple[set[str], set[str]]:
        """Resolves PR search nodes to `(keys, page_open_prs)` and remembers their node ids."""
        keys: set[str] = set()
        page_open_prs: set[str] = set()
        for node in nodes:
            resolved = self._node_key(node)
            if resolved is None:
                continue
            keys.add(resolved[1])
            if node.get("state") == "OPEN":
                page_open_prs.add(resolved[1])
            node_id = node.get("id")
            if isinstance(node_id, str) and node_id:
                self._store.set_node_id(resolved[1], node_id)
        return keys, page_open_prs

    def _store_issues(self, nodes: Iterable[Mapping[str, Any]]) -> set[str]:
        """Upserts issue search nodes into `issues` and returns their canonical keys."""
        keys: set[str] = set()
        records: list[IssueRecord] = []
        comments: list[tuple[IssueRecord, tuple[ItemCommentRecord, ...]]] = []
        for node in nodes:
            resolved = self._node_key(node)
            if resolved is None:
                continue
            keys.add(resolved[1])
            record = _issue_record(resolved[0], node)
            records.append(record)
            comments.append((record, item_comments_from_graphql(node.get("comments"))))
        self._store.upsert_issues(records)
        for record, latest in comments:
            self._store.upsert_item_comments(
                record.repo_id, "issue", record.number, latest
            )
        return keys

    # ----- leases and hot refresh -------------------------------------------------

    def _update_leases(self, pr_keys: set[str], now: float) -> set[str]:
        """Leases the viewer's open PRs and drops the other viewer leases.

        Returns:
            Keys whose viewer lease was dropped (closed, merged, or out of every set).
        """
        if pr_keys:
            self._store.add_watch(
                VIEWER_CONSUMER,
                VIEWER_CONSUMER_ID,
                sorted(pr_keys),
                VIEWER_FRESHNESS_S,
                VIEWER_LEASE_TTL_S,
                now=now,
            )
        stale = {
            w.item_key
            for w in self._store.active_watches(now=now)
            if w.consumer == VIEWER_CONSUMER and w.consumer_id == VIEWER_CONSUMER_ID
        } - pr_keys
        if stale:
            self._store.remove_watch(VIEWER_CONSUMER, VIEWER_CONSUMER_ID, sorted(stale))
        return stale

    def _refresh_new_prs(self, keys: Sequence[str]) -> None:
        """Fetches full fields for PRs new to the viewer sets or just unleased.

        On a budget deferral, a rate limit (`RateLimited` is a `BudgetDeferred`),
        or an API error, the keys are queued as refresh requests, which the
        service retries; unleased PRs would otherwise never be refreshed again.
        """
        if not keys:
            return
        try:
            self._hot.refresh_hot(keys)
        except (BudgetDeferred, GhApiError) as exc:
            LOGGER.info(
                "viewer hot refresh of %d PRs queued for retry: %s", len(keys), exc
            )
            self._store.request_refresh(keys)


def _issue_record(repo_id: int, node: Mapping[str, Any]) -> IssueRecord:
    """Builds an `IssueRecord` from an `IssueFields` search node."""
    author = node.get("author")
    return IssueRecord(
        repo_id=repo_id,
        number=int(node["number"]),
        title=str(node.get("title") or ""),
        state=str(node.get("state") or "OPEN"),
        updated_at=str(node.get("updatedAt") or ""),
        node_id=node.get("id"),
        author=author.get("login") if isinstance(author, dict) else None,
        author_association=node.get("authorAssociation"),
        body=str(node.get("body") or ""),
        labels=_logins(node.get("labels"), "name"),
        assignees=_logins(node.get("assignees"), "login"),
        created_at=node.get("createdAt"),
        closed_at=node.get("closedAt"),
        url=node.get("url"),
    )
