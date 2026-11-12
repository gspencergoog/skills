"""Conditional HTTP ETag /events polling and GraphQL UPDATED_AT delta syncer for packrat."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from db_migrations import REFETCH_OPEN_PRS_KEY_PREFIX
from db_store import PackratStore
from gh_transport import (
    BudgetDeferred,
    GhCommandResult,
    GhRunner,
    _is_rate_limited,
    _split_http_response,
    run_gh_command,
)
from mentions import RepoResolver, caching_resolver, extract_item_mentions
from models import (
    BACKFILL_STREAMS,
    CommentRecord,
    DiscussionRecord,
    IssuePrLink,
    IssueRecord,
    PrFileRecord,
    PrRecord,
    RepoConfig,
    ReviewRecord,
    ReviewThreadRecord,
    SyncStats,
    TextChunk,
    compute_content_sha256,
    utc_now_iso,
)
from progress import NULL_REPORTER, ProgressReporter
from rate_budget import Priority, RateBudget

SAFETY_SWEEP_INTERVAL_SEC = 600
PAGE_SIZE_LADDER: tuple[int, ...] = (25, 12, 5, 2, 1)
ADAPTIVE_PAGE_COOLDOWN = 20
BACKOFF_DELAYS_S: tuple[float, ...] = (1.0, 3.0, 9.0)
_TRANSPORT_ERROR_MARKERS: tuple[str, ...] = (
    "tls handshake timeout",
    "error connecting to",
    "connection reset",
    "eof",
)

_CLOSING_ISSUE_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b",
    re.IGNORECASE,
)
_REF_ISSUE_RE = re.compile(r"(?:^|[\s(])#(\d+)\b")

ISSUES_DELTA_QUERY = """
query($owner: String!, $name: String!, $pageSize: Int!, $cursor: String) {
  rateLimit { cost }
  repository(owner: $owner, name: $name) {
    issues(first: $pageSize, after: $cursor, orderBy: {field: UPDATED_AT, direction: DESC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        number
        title
        state
        updatedAt
        createdAt
        closedAt
        url
        body
        author { login }
        authorAssociation
        labels(first: 20) { nodes { name } }
        assignees(first: 10) { nodes { login } }
        comments(first: 30) {
          nodes {
            id
            body
            createdAt
            updatedAt
            url
            author { login }
            authorAssociation
          }
        }
      }
    }
  }
}
"""

_PR_NODE_FIELDS = """\
        id
        number
        title
        state
        isDraft
        updatedAt
        createdAt
        mergedAt
        closedAt
        url
        body
        baseRefName
        headRefName
        headRefOid
        isCrossRepository
        headRepositoryOwner { login }
        mergeCommit { oid }
        reviewDecision
        mergeable
        additions
        deletions
        changedFiles
        author { login }
        authorAssociation
        labels(first: 20) { nodes { name } }
        closingIssuesReferences(first: 10) { nodes { number } }
        files(first: 50) {
          nodes { path changeType additions deletions }
        }
        reviews(first: 20) {
          nodes {
            id
            state
            body
            submittedAt
            author { login __typename }
            authorAssociation
          }
        }
        reviewThreads(first: 30) {
          nodes {
            id
            path
            line
            startLine
            isResolved
            isOutdated
            comments(first: 15) {
              nodes {
                id
                body
                createdAt
                updatedAt
                author { login }
                authorAssociation
              }
            }
          }
        }
        comments(first: 30) {
          nodes {
            id
            body
            createdAt
            updatedAt
            url
            author { login }
            authorAssociation
          }
        }
"""
"""Pull request node selection for `PRS_DELTA_QUERY`."""


def _pr_connection_query(connection_args: str) -> str:
    """Builds a repository `pullRequests(...)` page query with `_PR_NODE_FIELDS`."""
    return (
        "query($owner: String!, $name: String!, $pageSize: Int!, $cursor: String) {\n"
        "  rateLimit { cost }\n"
        "  repository(owner: $owner, name: $name) {\n"
        f"    pullRequests({connection_args}) {{\n"
        "      totalCount\n"
        "      pageInfo { hasNextPage endCursor }\n"
        "      nodes {\n"
        f"{_PR_NODE_FIELDS}"
        "      }\n"
        "    }\n"
        "  }\n"
        "}\n"
    )


PRS_DELTA_QUERY = _pr_connection_query(
    "first: $pageSize, after: $cursor, orderBy: {field: UPDATED_AT, direction: DESC}"
)

OPEN_PRS_QUERY = """
query($owner: String!, $name: String!, $pageSize: Int!, $cursor: String) {
  rateLimit { cost }
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: $pageSize, after: $cursor, orderBy: {field: UPDATED_AT, direction: DESC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        isCrossRepository
        headRepositoryOwner { login }
      }
    }
  }
}
"""
"""Pages every open pull request, for the one-time fork-field refetch."""

DISCUSSIONS_DELTA_QUERY = """
query($owner: String!, $name: String!, $pageSize: Int!, $cursor: String) {
  rateLimit { cost }
  repository(owner: $owner, name: $name) {
    discussions(first: $pageSize, after: $cursor, orderBy: {field: UPDATED_AT, direction: DESC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        number
        title
        updatedAt
        createdAt
        url
        body
        author { login }
        category { name }
        answer { body }
      }
    }
  }
}
"""


@dataclass(frozen=True)
class EventsPollResult:
    """Parsed HTTP response from conditional /repos/<owner>/<name>/events polling."""

    status_code: int
    not_modified: bool
    etag: str | None
    last_modified: str | None
    poll_interval_sec: int
    events: tuple[dict[str, Any], ...] = ()
    error: str | None = None
    rate_limited: bool = False


_KEYRING_ERROR_SUBSTRINGS: tuple[str, ...] = (
    "keyring",
    "secret service",
    "dbus",
    "d-bus",
    "locked",
)


@dataclass(frozen=True)
class GhAuthProbe:
    """Outcome of a non-interactive GitHub CLI authentication token check.

    Attributes:
        ok: Whether `gh auth token` returned a non-empty token within the timeout.
        error_kind: Failure category ("keyring_locked" or "auth_error"), or None when ok.
        detail: Single-line diagnostic message when `ok` is False.
    """

    ok: bool
    error_kind: str | None = None
    detail: str = ""


AuthProbeFn = Callable[[], GhAuthProbe]


def _is_keyring_error(text: str) -> bool:
    """Checks whether diagnostic output indicates a locked OS keyring or D-Bus error."""
    lowered = text.lower()
    return any(token in lowered for token in _KEYRING_ERROR_SUBSTRINGS)


def _rejects_no_refresh_flag(res: GhCommandResult) -> bool:
    """Checks whether an older gh CLI rejected the --no-refresh flag."""
    if res.returncode == 0 or res.timed_out:
        return False
    combined = f"{res.stderr}\n{res.stdout}".lower()
    return "--no-refresh" in combined


def _first_gh_detail(res: GhCommandResult, fallback: str) -> str:
    """Extracts the first non-empty diagnostic line from a gh command result."""
    raw = (res.stderr or res.stdout or "").strip()
    if not raw:
        return fallback
    return raw.splitlines()[0].strip() or fallback


def probe_gh_auth(
    runner: GhRunner = run_gh_command,
    timeout: int = 5,
) -> GhAuthProbe:
    """Probes whether `gh` can read an auth token without blocking on a locked keyring.

    Args:
        runner: Subprocess runner invoking `gh` arguments with a timeout.
        timeout: Maximum seconds to wait before classifying a hang as `keyring_locked`.

    Returns:
        A `GhAuthProbe` indicating token availability or failure classification.
    """
    res = runner(["auth", "token", "--no-refresh"], timeout)
    if _rejects_no_refresh_flag(res):
        res = runner(["auth", "token"], timeout)

    if res.timed_out or res.returncode == 124:
        return GhAuthProbe(
            ok=False,
            error_kind="keyring_locked",
            detail=_first_gh_detail(
                res, "gh auth token timed out (OS keyring likely locked)"
            ),
        )
    if res.returncode == 0 and res.stdout.strip():
        return GhAuthProbe(ok=True)

    combined = f"{res.stderr}\n{res.stdout}"
    if _is_keyring_error(combined):
        return GhAuthProbe(
            ok=False,
            error_kind="keyring_locked",
            detail=_first_gh_detail(res, "OS keyring is locked"),
        )
    return GhAuthProbe(
        ok=False,
        error_kind="auth_error",
        detail=_first_gh_detail(res, f"gh auth token exited with {res.returncode}"),
    )


def _parse_iso_epoch(ts: str | None) -> float | None:
    """Parses an ISO-8601 UTC timestamp string into epoch seconds."""
    if not ts:
        return None
    try:
        cleaned = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned).timestamp()
    except ValueError:
        return None


def needs_safety_sweep(
    repo: RepoConfig,
    now_iso: str | None = None,
    sweep_interval_sec: int = SAFETY_SWEEP_INTERVAL_SEC,
) -> bool:
    """Returns True when the 10-minute safety sweep interval has elapsed."""
    if not repo.last_full_sweep_at:
        return True
    last_epoch = _parse_iso_epoch(repo.last_full_sweep_at)
    now_epoch = _parse_iso_epoch(now_iso or utc_now_iso())
    if last_epoch is None or now_epoch is None:
        return True
    return (now_epoch - last_epoch) >= sweep_interval_sec


def _is_not_modified_response(status_code: int, res: GhCommandResult) -> bool:
    """Returns True when the HTTP status or output indicates 304 Not Modified."""
    return (
        status_code == 304
        or "304 Not Modified" in res.stdout
        or "304 Not Modified" in res.stderr
    )


def _parse_events_json(body: str) -> tuple[dict[str, Any], ...]:
    """Parses the /events JSON array body, returning () on empty or invalid JSON."""
    if not body.strip():
        return ()
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return ()
    return tuple(parsed) if isinstance(parsed, list) else ()


def poll_repo_events(
    repo: RepoConfig,
    runner: GhRunner = run_gh_command,
    budget: RateBudget | None = None,
) -> EventsPollResult:
    """Polls /repos/<owner>/<name>/events with If-None-Match and If-Modified-Since.

    Args:
        repo: Repository whose stored ETag and Last-Modified values are sent.
        runner: Subprocess runner invoking `gh` arguments with a timeout.
        budget: Optional shared rate budget that records the response's
            `X-RateLimit-*` headers.

    Returns:
        The parsed poll result. `rate_limited` is True when GitHub rejected the
        request for exceeding a rate limit.
    """
    args: list[str] = [
        "api",
        "-i",
        f"repos/{repo.owner}/{repo.name}/events?per_page=30",
    ]
    if repo.events_etag:
        args.extend(["-H", f"If-None-Match: {repo.events_etag}"])
    if repo.events_last_modified:
        args.extend(["-H", f"If-Modified-Since: {repo.events_last_modified}"])

    try:
        res = runner(args, 15)
    except BudgetDeferred as exc:
        return EventsPollResult(
            status_code=0,
            not_modified=False,
            etag=repo.events_etag,
            last_modified=repo.events_last_modified,
            poll_interval_sec=repo.poll_interval_sec,
            error=exc.detail,
            rate_limited=True,
        )
    status_code, headers, body = _split_http_response(res.stdout)
    if budget is not None and not bool(getattr(runner, "is_budgeted_runner", False)):
        budget.record(headers, worker="sync")
    etag = headers.get("etag") or repo.events_etag
    last_mod = headers.get("last-modified") or repo.events_last_modified
    poll_interval = int(
        headers.get("x-poll-interval", str(repo.poll_interval_sec or 60))
    )

    if _is_not_modified_response(status_code, res):
        return EventsPollResult(
            status_code=304,
            not_modified=True,
            etag=etag,
            last_modified=last_mod,
            poll_interval_sec=poll_interval,
        )

    if res.returncode != 0 or status_code == 0:
        err = res.stderr.strip() or f"gh api exited with {res.returncode}"
        return EventsPollResult(
            status_code=status_code or 500,
            not_modified=False,
            etag=repo.events_etag,
            last_modified=repo.events_last_modified,
            poll_interval_sec=repo.poll_interval_sec,
            error=err,
            rate_limited=_is_rate_limited(f"{res.stderr}\n{body}"),
        )

    return EventsPollResult(
        status_code=status_code,
        not_modified=False,
        etag=etag,
        last_modified=last_mod,
        poll_interval_sec=poll_interval,
        events=_parse_events_json(body),
    )


def extract_issue_links(
    repo_id: int, pr_number: int, body: str, closing_nums: Sequence[int]
) -> tuple[IssuePrLink, ...]:
    """Extracts closing and referenced issue numbers from a PR body and GraphQL nodes."""
    links: dict[int, str] = {}
    for num in closing_nums:
        if num != pr_number:
            links[num] = "closes"
    for match in _CLOSING_ISSUE_RE.finditer(body or ""):
        num = int(match.group(1))
        if num != pr_number:
            links[num] = "closes"
    for match in _REF_ISSUE_RE.finditer(body or ""):
        num = int(match.group(1))
        if num != pr_number and num not in links:
            links[num] = "references"
    return tuple(
        IssuePrLink(
            repo_id=repo_id,
            issue_number=issue_num,
            pr_number=pr_number,
            link_type=ltype,
        )
        for issue_num, ltype in sorted(links.items())
    )


def _login(node: dict[str, Any] | None, field: str = "author") -> str | None:
    """Safely extracts `<field>.login` (default `author.login`) from a GraphQL node."""
    if not isinstance(node, dict):
        return None
    author = node.get(field)
    if isinstance(author, dict) and author.get("login"):
        return str(author["login"])
    return None


def _parse_comments(
    repo_id: int,
    parent_kind: str,
    parent_number: int,
    nodes: Sequence[dict[str, Any]],
) -> tuple[CommentRecord, ...]:
    """Converts GraphQL comment nodes into CommentRecord instances."""
    out: list[CommentRecord] = []
    for c in nodes:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        out.append(
            CommentRecord(
                repo_id=repo_id,
                comment_id=str(c["id"]),
                parent_kind=parent_kind,
                parent_number=parent_number,
                author=_login(c),
                author_association=c.get("authorAssociation"),
                body=str(c.get("body") or ""),
                created_at=c.get("createdAt"),
                updated_at=c.get("updatedAt") or c.get("createdAt"),
                url=c.get("url"),
            )
        )
    return tuple(out)


def _connection_nodes(node: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Extracts a list of dict nodes from a nested GraphQL connection field."""
    conn = node.get(key)
    if not isinstance(conn, dict):
        return []
    raw_nodes = conn.get("nodes")
    if not isinstance(raw_nodes, list):
        return []
    return [n for n in raw_nodes if isinstance(n, dict)]


def _extract_string_field_tuple(
    node: dict[str, Any], conn_key: str, item_key: str
) -> tuple[str, ...]:
    """Extracts a tuple of strings from a nested GraphQL connection's nodes."""
    return tuple(
        str(n[item_key]) for n in _connection_nodes(node, conn_key) if n.get(item_key)
    )


def _comments_to_chunks(
    repo: RepoConfig,
    parent_label: str,
    parent_number: int,
    fallback_updated_at: str,
    comments: Sequence[CommentRecord],
) -> list[TextChunk]:
    """Builds searchable TextChunk records for non-empty issue or PR comments."""
    out: list[TextChunk] = []
    for cm in comments:
        if not cm.body.strip():
            continue
        c_title = (
            f"Comment on {parent_label} #{parent_number} by {cm.author or 'unknown'}"
        )
        out.append(
            TextChunk(
                chunk_id=f"{repo.full_name}#comment:{cm.comment_id}",
                repo_id=repo.repo_id,
                source_kind="comment",
                source_ref=f"{repo.full_name}#{parent_number}#comment-{cm.comment_id}",
                title=c_title,
                text=cm.body,
                content_sha256=compute_content_sha256(c_title, cm.body),
                is_untrusted=True,
                item_number=parent_number,
                updated_at=cm.updated_at or fallback_updated_at,
            )
        )
    return out


def _parse_issue_node(
    repo: RepoConfig,
    node: dict[str, Any],
) -> tuple[IssueRecord, tuple[CommentRecord, ...], list[TextChunk]]:
    """Parses one GraphQL issue node into an IssueRecord, comments, and TextChunks."""
    number = int(node["number"])
    title = str(node.get("title") or "")
    body = str(node.get("body") or "")
    updated_at = str(node.get("updatedAt") or utc_now_iso())
    issue = IssueRecord(
        repo_id=repo.repo_id,
        number=number,
        node_id=node.get("id"),
        state=str(node.get("state") or "OPEN"),
        author=_login(node),
        author_association=node.get("authorAssociation"),
        title=title,
        body=body,
        labels=_extract_string_field_tuple(node, "labels", "name"),
        assignees=_extract_string_field_tuple(node, "assignees", "login"),
        created_at=node.get("createdAt"),
        updated_at=updated_at,
        closed_at=node.get("closedAt"),
        url=node.get("url"),
    )
    comments = _parse_comments(
        repo.repo_id, "issue", number, _connection_nodes(node, "comments")
    )
    issue_chunk = TextChunk(
        chunk_id=f"{repo.full_name}#issue:{number}",
        repo_id=repo.repo_id,
        source_kind="issue",
        source_ref=f"{repo.full_name}#{number}",
        title=f"Issue #{number}: {title}",
        text=body,
        content_sha256=compute_content_sha256(title, body),
        is_untrusted=True,
        item_number=number,
        updated_at=updated_at,
    )
    chunks = [
        issue_chunk,
        *_comments_to_chunks(repo, "Issue", number, updated_at, comments),
    ]
    return issue, comments, chunks


def _parse_single_review_thread(
    repo: RepoConfig,
    pr_number: int,
    pr_updated_at: str,
    th: dict[str, Any],
) -> tuple[ReviewThreadRecord, TextChunk | None]:
    """Parses a single review thread dict into a ReviewThreadRecord and optional TextChunk."""
    thread_id = str(th["id"])
    c_nodes = _connection_nodes(th, "comments")
    first = c_nodes[0] if c_nodes else {}
    reviewer = _login(first)
    participants = sorted(
        {login for login in (reviewer, *(_login(c) for c in c_nodes)) if login}
    )
    combined_body = "\n---\n".join(
        f"{_login(c) or 'reviewer'}: {c.get('body') or ''}".strip()
        for c in c_nodes
        if str(c.get("body") or "").strip()
    )
    file_path = th.get("path")
    last_updated = c_nodes[-1].get("updatedAt") if c_nodes else None
    t_updated = str(last_updated or pr_updated_at)
    record = ReviewThreadRecord(
        thread_id=thread_id,
        repo_id=repo.repo_id,
        pr_number=pr_number,
        file_path=file_path,
        line=th.get("line"),
        start_line=th.get("startLine"),
        is_resolved=bool(th.get("isResolved")),
        is_outdated=bool(th.get("isOutdated")),
        reviewer=reviewer,
        author_association=first.get("authorAssociation"),
        created_at=first.get("createdAt"),
        updated_at=t_updated,
        body=combined_body,
        comments_json=json.dumps(c_nodes),
        participants_json=json.dumps(participants),
    )
    if not combined_body:
        return record, None
    loc = file_path or "general"
    t_title = f"PR #{pr_number} review thread on {loc}"
    chunk = TextChunk(
        chunk_id=f"{repo.full_name}#thread:{thread_id}",
        repo_id=repo.repo_id,
        source_kind="review_thread",
        source_ref=f"{repo.full_name}#{pr_number}:{loc}",
        title=t_title,
        text=combined_body,
        content_sha256=compute_content_sha256(t_title, combined_body),
        is_untrusted=True,
        file_path=file_path,
        item_number=pr_number,
        updated_at=t_updated,
    )
    return record, chunk


def _parse_review_threads(
    repo: RepoConfig,
    pr_number: int,
    pr_updated_at: str,
    raw_threads: Sequence[dict[str, Any]],
) -> tuple[tuple[ReviewThreadRecord, ...], list[TextChunk]]:
    """Parses PR review threads and produces searchable review_thread chunks."""
    threads: list[ReviewThreadRecord] = []
    chunks: list[TextChunk] = []
    for th in raw_threads:
        if not isinstance(th, dict) or not th.get("id"):
            continue
        rec, ch = _parse_single_review_thread(repo, pr_number, pr_updated_at, th)
        threads.append(rec)
        if ch is not None:
            chunks.append(ch)
    return tuple(threads), chunks


def _parse_pr_files(
    repo_id: int, pr_number: int, node: dict[str, Any]
) -> tuple[PrFileRecord, ...]:
    """Parses PR file nodes into PrFileRecord instances."""
    return tuple(
        PrFileRecord(
            repo_id=repo_id,
            pr_number=pr_number,
            file_path=str(f["path"]),
            change_type=str(f.get("changeType") or "MODIFIED"),
            additions=int(f.get("additions") or 0),
            deletions=int(f.get("deletions") or 0),
        )
        for f in _connection_nodes(node, "files")
        if f.get("path")
    )


def _author_type(node: dict[str, Any]) -> str | None:
    """Returns the GraphQL `author.__typename` (such as "User" or "Bot"), if selected."""
    author = node.get("author")
    if not isinstance(author, dict):
        return None
    typename = author.get("__typename")
    return str(typename) if typename else None


def _parse_pr_reviews(
    repo_id: int, pr_number: int, node: dict[str, Any]
) -> tuple[ReviewRecord, ...]:
    """Parses PR review nodes into ReviewRecord instances."""
    return tuple(
        ReviewRecord(
            repo_id=repo_id,
            review_id=str(rv["id"]),
            pr_number=pr_number,
            state=str(rv.get("state") or "COMMENTED"),
            author=_login(rv),
            author_association=rv.get("authorAssociation"),
            body=str(rv.get("body") or ""),
            submitted_at=rv.get("submittedAt"),
            author_type=_author_type(rv),
        )
        for rv in _connection_nodes(node, "reviews")
        if rv.get("id")
    )


def _parse_pr_node(
    repo: RepoConfig,
    node: dict[str, Any],
) -> tuple[PrRecord, list[TextChunk]]:
    """Parses one GraphQL pullRequest node into a PrRecord and TextChunks."""
    number = int(node["number"])
    title = str(node.get("title") or "")
    body = str(node.get("body") or "")
    updated_at = str(node.get("updatedAt") or utc_now_iso())
    closing_nums = [
        int(n["number"])
        for n in _connection_nodes(node, "closingIssuesReferences")
        if n.get("number")
    ]
    threads, thread_chunks = _parse_review_threads(
        repo, number, updated_at, _connection_nodes(node, "reviewThreads")
    )
    comments = _parse_comments(
        repo.repo_id, "pr", number, _connection_nodes(node, "comments")
    )
    merge_commit = node.get("mergeCommit")
    cross_repo = node.get("isCrossRepository")

    pr = PrRecord(
        repo_id=repo.repo_id,
        number=number,
        node_id=node.get("id"),
        state=str(node.get("state") or "OPEN"),
        is_draft=bool(node.get("isDraft")),
        author=_login(node),
        author_association=node.get("authorAssociation"),
        title=title,
        body=body,
        labels=_extract_string_field_tuple(node, "labels", "name"),
        base_ref=node.get("baseRefName"),
        head_ref=node.get("headRefName"),
        head_sha=node.get("headRefOid"),
        head_repo_owner=_login(node, "headRepositoryOwner"),
        is_cross_repository=cross_repo if isinstance(cross_repo, bool) else None,
        merge_commit_sha=merge_commit.get("oid")
        if isinstance(merge_commit, dict)
        else None,
        review_decision=node.get("reviewDecision"),
        mergeable=node.get("mergeable"),
        additions=int(node.get("additions") or 0),
        deletions=int(node.get("deletions") or 0),
        changed_files=int(node.get("changedFiles") or 0),
        created_at=node.get("createdAt"),
        updated_at=updated_at,
        merged_at=node.get("mergedAt"),
        closed_at=node.get("closedAt"),
        url=node.get("url"),
        files=_parse_pr_files(repo.repo_id, number, node),
        reviews=_parse_pr_reviews(repo.repo_id, number, node),
        review_threads=threads,
        comments=comments,
        issue_links=extract_issue_links(repo.repo_id, number, body, closing_nums),
    )

    pr_chunk = TextChunk(
        chunk_id=f"{repo.full_name}#pr:{number}",
        repo_id=repo.repo_id,
        source_kind="pr",
        source_ref=f"{repo.full_name}#{number}",
        title=f"PR #{number}: {title}",
        text=body,
        content_sha256=compute_content_sha256(title, body),
        is_untrusted=True,
        item_number=number,
        updated_at=updated_at,
    )
    chunks = [
        pr_chunk,
        *thread_chunks,
        *_comments_to_chunks(repo, "PR", number, updated_at, comments),
    ]
    return pr, chunks


def _parse_discussion_node(
    repo: RepoConfig,
    node: dict[str, Any],
) -> tuple[DiscussionRecord, TextChunk]:
    """Parses one GraphQL discussion node into a DiscussionRecord and TextChunk."""
    number = int(node["number"])
    title = str(node.get("title") or "")
    body = str(node.get("body") or "")
    updated_at = str(node.get("updatedAt") or utc_now_iso())
    cat = node.get("category")
    ans = node.get("answer")
    answer_body = (
        str(ans.get("body")) if isinstance(ans, dict) and ans.get("body") else None
    )
    disc = DiscussionRecord(
        repo_id=repo.repo_id,
        number=number,
        node_id=node.get("id"),
        category=str(cat.get("name"))
        if isinstance(cat, dict) and cat.get("name")
        else None,
        author=_login(node),
        title=title,
        body=body,
        answer_body=answer_body,
        created_at=node.get("createdAt"),
        updated_at=updated_at,
        url=node.get("url"),
    )
    full_text = (
        f"{body}\n\nAccepted Answer:\n{answer_body}".strip() if answer_body else body
    )
    chunk = TextChunk(
        chunk_id=f"{repo.full_name}#discussion:{number}",
        repo_id=repo.repo_id,
        source_kind="discussion",
        source_ref=f"{repo.full_name}#discussion-{number}",
        title=f"Discussion #{number}: {title}",
        text=full_text,
        content_sha256=compute_content_sha256(title, full_text),
        is_untrusted=True,
        item_number=number,
        updated_at=updated_at,
    )
    return disc, chunk


def _extract_graphql_connection(body: str, field_name: str) -> dict[str, Any] | None:
    """Parses a GraphQL JSON response body and returns data.repository.<field_name> if valid."""
    if not body.strip():
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    repo_obj = (payload.get("data") or {}).get("repository") or {}
    conn = repo_obj.get(field_name)
    return conn if isinstance(conn, dict) else None


def _split_gh_include_output(raw: str) -> tuple[dict[str, str], str]:
    """Separates `gh api -i` output into lowercase headers and the body.

    Output that does not start with an HTTP status line (for example from a
    runner that omits `-i`) is returned unchanged as the body with no headers.
    """
    if not raw.lstrip().startswith("HTTP/"):
        return {}, raw
    _status, headers, body = _split_http_response(raw.lstrip())
    return headers, body


def _is_fatal_auth_error(stderr: str) -> bool:
    """Returns True when stderr indicates an unrecoverable auth or keyring failure."""
    lowered = stderr.lower()
    return "401" in lowered or "403" in lowered or _is_keyring_error(lowered)


@dataclass(frozen=True)
class PageFetchOutcome:
    """Structured result of fetching a single GraphQL page across halving steps."""

    connection: dict[str, Any] | None
    error_kind: str | None = None
    error_detail: str | None = None
    used_page_size: int = PAGE_SIZE_LADDER[0]


@dataclass(frozen=True)
class PageBatch:
    """Parsed page payload delivered to on_page callbacks."""

    nodes: list[dict[str, Any]]
    cursor: str | None
    items_fetched: int
    total_count: int | None
    oldest_date: str | None


_EXPLICIT_ERROR_KINDS: dict[str, str] = {
    "rate_limited": "rate_limited",
    "deferred_rate_limit": "rate_limited",
    "auth_failed": "auth_error",
    "keyring_locked": "keyring_locked",
}


def _classify_gh_failure(
    res: GhCommandResult, body: str | None = None
) -> tuple[str, str]:
    """Classifies a failed GraphQL subprocess result into (error_kind, short_reason).

    Args:
        res: The failed subprocess result.
        body: Response body without the `-i` header block. Defaults to
            `res.stdout` for runners that return the body alone.

    Returns:
        `(error_kind, short_reason)`. Rate limits are detected before the
        401/403 checks so a 403 rate-limit response is not mistaken for an
        auth failure.
    """
    resp_body = res.stdout if body is None else body
    raw_err = (res.stderr or resp_body or "").strip()
    first_line = (
        raw_err.splitlines()[0].strip()
        if raw_err
        else f"gh exited with {res.returncode}"
    )
    if res.error_kind in (
        "rate_limited",
        "deferred_rate_limit",
        "auth_failed",
        "keyring_locked",
    ):
        return _EXPLICIT_ERROR_KINDS[res.error_kind], first_line
    if res.timed_out or res.returncode == 124:
        return "timeout", first_line
    if _is_rate_limited(f"{res.stderr}\n{resp_body}"):
        return "rate_limited", first_line
    if _is_keyring_error(raw_err):
        return "keyring_locked", first_line
    if _is_fatal_auth_error(raw_err):
        return "auth_error", first_line
    if res.returncode == 0:
        return "parse_error", "invalid or empty GraphQL JSON response"
    return "http_error", first_line


def _build_graphql_args(
    owner: str,
    name: str,
    query: str,
    page_size: int,
    cursor: str | None,
) -> list[str]:
    """Builds the gh api graphql argument list for a single page request."""
    args = [
        "api",
        "graphql",
        "-i",
        "-F",
        f"owner={owner}",
        "-F",
        f"name={name}",
        "-F",
        f"pageSize={page_size}",
        "-f",
        f"query={query}",
    ]
    if cursor:
        args.extend(["-F", f"cursor={cursor}"])
    return args


def _probe_timeout_auth(
    last_kind: str,
    last_detail: str,
    auth_probe: AuthProbeFn | None,
) -> tuple[str, str, bool]:
    """Checks auth_probe after a GraphQL timeout and signals whether to abort retries."""
    if last_kind in ("auth_error", "keyring_locked"):
        return last_kind, last_detail, True
    if last_kind == "timeout" and auth_probe is not None:
        probe = auth_probe()
        if not probe.ok:
            kind = probe.error_kind or "auth_error"
            detail = probe.detail or last_detail
            return kind, detail, True
    return last_kind, last_detail, False


def _is_live_gh_runner(runner: GhRunner) -> bool:
    """Returns True when `runner` invokes the live `gh` CLI subprocess."""
    return runner is run_gh_command or bool(getattr(runner, "_uses_gh_cli", False))


def _is_transport_error(detail: str) -> bool:
    """Returns True when `detail` indicates a transient client-side transport failure."""
    lowered = detail.lower()
    return any(marker in lowered for marker in _TRANSPORT_ERROR_MARKERS)


def _resolve_sleep_fn(
    runner: GhRunner,
    sleep_fn: Callable[[float], None] | None,
) -> Callable[[float], None]:
    """Returns the backoff sleep function for GraphQL page retries."""
    if sleep_fn is not None:
        return sleep_fn
    if _is_live_gh_runner(runner):
        return time.sleep
    return lambda _s: None


def _attempt_graphql_page(
    args: Sequence[str],
    field_name: str,
    runner: GhRunner,
    budget: RateBudget | None,
    auth_probe: AuthProbeFn | None,
) -> tuple[dict[str, Any] | None, str, str, bool]:
    """Runs one GraphQL page request and returns (connection, error_kind, error_detail, abort)."""
    is_budgeted = bool(getattr(runner, "is_budgeted_runner", False))
    try:
        res = runner(args, 25)
    except BudgetDeferred as exc:
        return None, DEFERRED_RATE_LIMIT, exc.detail, True
    if res.error_kind == DEFERRED_RATE_LIMIT:
        return None, DEFERRED_RATE_LIMIT, _first_gh_detail(res, "rate budget low"), True
    headers, body = _split_gh_include_output(res.stdout)
    if budget is not None and not is_budgeted:
        budget.record(headers, worker="sync")
    if res.returncode == 0:
        conn = _extract_graphql_connection(body, field_name)
        if conn is not None:
            return conn, "", "", False
    raw_kind, raw_detail = _classify_gh_failure(res, body)
    kind, detail, abort = _probe_timeout_auth(raw_kind, raw_detail, auth_probe)
    return None, kind, detail, abort or kind == "rate_limited"


def _fetch_graphql_page_with_halving(
    owner: str,
    name: str,
    query: str,
    field_name: str,
    cursor: str | None,
    runner: GhRunner,
    *,
    reporter: ProgressReporter = NULL_REPORTER,
    repo_full_name: str = "",
    stream: str = "",
    auth_probe: AuthProbeFn | None = None,
    budget: RateBudget | None = None,
    max_page_size: int = PAGE_SIZE_LADDER[0],
    sleep_fn: Callable[[float], None] | None = None,
) -> PageFetchOutcome:
    """Executes a GraphQL page query, stepping down pageSize (25 -> 12 -> 5 -> 2 -> 1) on 5xx/timeouts.

    Each response's `X-RateLimit-*` headers are recorded in `budget`. A
    rate-limited response ends the attempt at once instead of retrying at a
    smaller page size against an exhausted budget.
    """
    resolved_sleep = _resolve_sleep_fn(runner, sleep_fn)
    ladder = tuple(s for s in PAGE_SIZE_LADDER if s <= max_page_size) or (
        PAGE_SIZE_LADDER[-1],
    )
    last_kind = "http_error"
    last_detail = "GraphQL page fetch failed"
    for idx, page_size in enumerate(ladder):
        args = _build_graphql_args(owner, name, query, page_size, cursor)
        conn, last_kind, last_detail, abort = _attempt_graphql_page(
            args, field_name, runner, budget, auth_probe
        )
        if conn is None and not abort and _is_transport_error(last_detail):
            resolved_sleep(BACKOFF_DELAYS_S[0])
            conn, last_kind, last_detail, abort = _attempt_graphql_page(
                args, field_name, runner, budget, auth_probe
            )
        if conn is not None:
            return PageFetchOutcome(connection=conn, used_page_size=page_size)
        if abort or idx + 1 >= len(ladder):
            break
        next_size = ladder[idx + 1]
        reporter.warn(
            repo_full_name,
            f"github {stream}: page request failed at pageSize={page_size} ({last_detail}); retrying at pageSize={next_size}",
        )
        resolved_sleep(BACKOFF_DELAYS_S[min(idx, len(BACKOFF_DELAYS_S) - 1)])
    return PageFetchOutcome(
        connection=None,
        error_kind=last_kind,
        error_detail=last_detail,
        used_page_size=ladder[-1],
    )


def _filter_nodes_above_watermark(
    nodes: Sequence[dict[str, Any]],
    watermark: str | None,
    newest_updated: str | None,
) -> tuple[list[dict[str, Any]], str | None, bool]:
    """Filters page nodes newer than watermark and tracks the newest updatedAt timestamp."""
    accepted: list[dict[str, Any]] = []
    newest = newest_updated
    reached_watermark = False
    for node in nodes:
        node_updated = str(node.get("updatedAt") or "")
        if newest is None or (node_updated and node_updated > newest):
            newest = node_updated
        if watermark and node_updated and node_updated <= watermark:
            reached_watermark = True
            break
        accepted.append(node)
    return accepted, newest, reached_watermark


DEFAULT_BACKFILL_MAX_PAGES = 1000

StreamPersister = Callable[
    [PackratStore, RepoConfig, list[dict[str, Any]], SyncStats], None
]
PageHandler = Callable[[PageBatch], None]


@dataclass(frozen=True)
class StreamSpec:
    """Describes how to fetch and persist one GitHub stream."""

    stream: str
    query: str
    field_name: str
    persist: StreamPersister


@dataclass
class PageWalk:
    """Outcome of paging through one GraphQL connection."""

    pages: int = 0
    items_fetched: int = 0
    total_count: int | None = None
    newest_updated: str | None = None
    end_cursor: str | None = None
    reached_end: bool = False
    failed: bool = False
    deferred: bool = False
    error_kind: str | None = None
    error_detail: str | None = None


DEFERRED_RATE_LIMIT = "deferred_rate_limit"
_DEFERRING_ERROR_KINDS = ("rate_limited", DEFERRED_RATE_LIMIT)


def _max_timestamp(a: str | None, b: str | None) -> str | None:
    """Returns the later of two ISO-8601 timestamps, ignoring None."""
    if a is None or b is None:
        return a or b
    return max(a, b)


def _newest_updated(nodes: Sequence[dict[str, Any]]) -> str | None:
    """Returns the newest non-empty updatedAt value among nodes."""
    stamps = [str(n.get("updatedAt")) for n in nodes if n.get("updatedAt")]
    return max(stamps) if stamps else None


def _extract_page_metadata(
    conn: dict[str, Any],
    stop_watermark: str | None,
    newest_updated: str | None,
) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], str | None, bool, int | None, str | None
]:
    """Unpacks a GraphQL connection page into (nodes, accepted, newest, reached_wm, total_count, oldest_date)."""
    nodes = [
        n for n in (conn.get("nodes") or []) if isinstance(n, dict) and n.get("number")
    ]
    accepted, newest, reached_wm = _filter_nodes_above_watermark(
        nodes, stop_watermark, newest_updated
    )
    raw_total = conn.get("totalCount")
    total_count = (
        raw_total
        if isinstance(raw_total, int) and not isinstance(raw_total, bool)
        else None
    )
    sample = accepted or nodes
    stamps = [str(n.get("updatedAt"))[:10] for n in sample if n.get("updatedAt")]
    oldest_date = min(stamps) if stamps else None
    return nodes, accepted, newest, reached_wm, total_count, oldest_date


def _mark_stream_deferred(stats: SyncStats, stream: str) -> None:
    """Records that `stream` was deferred by the rate budget (not an error)."""
    stats.deferred = True
    if stream not in stats.deferred_streams:
        stats.deferred_streams.append(stream)


def _record_walk_failure(
    walk: PageWalk,
    repo_name: str,
    stream: str,
    stats: SyncStats,
    reporter: ProgressReporter,
) -> None:
    """Records a walk failure in stats and notifies reporter.

    Rate-limited and budget-deferred walks go into `stats.deferred_streams`
    with a warning; every other failure is appended to `stats.errors`.
    """
    if walk.error_kind in _DEFERRING_ERROR_KINDS:
        _mark_stream_deferred(stats, stream)
        detail = walk.error_detail or "rate budget low"
        reporter.warn(
            repo_name,
            f"github {stream} deferred after {walk.pages} page(s) ({walk.error_kind}: {detail})",
        )
        return
    if not walk.failed:
        return
    kind = walk.error_kind or "graphql_error"
    detail = walk.error_detail or "page fetch failed"
    msg = f"github {stream} sync failed after {walk.pages} page(s) ({kind}: {detail})"
    stats.errors.append(msg)
    reporter.error(repo_name, msg)


def _classify_page_end(
    conn: dict[str, Any],
    cursor: str | None,
    stop_here: bool,
) -> tuple[str | None, bool, bool]:
    """Returns (endCursor, reached_end, bad_cursor) for a fetched page.

    `bad_cursor` is True when more pages exist but GitHub returned no endCursor
    or repeated the cursor that was requested.
    """
    page_info = conn.get("pageInfo") or {}
    end_cursor = page_info.get("endCursor")
    reached_end = stop_here or not page_info.get("hasNextPage")
    bad_cursor = not reached_end and (not end_cursor or end_cursor == cursor)
    return end_cursor, reached_end, bad_cursor


def _next_adaptive_page_size(
    current_max: int,
    used_page_size: int,
    cooldown_remaining: int,
) -> tuple[int, int]:
    """Updates (current_max_page_size, cooldown_remaining) after a successful page fetch."""
    if used_page_size < current_max:
        return used_page_size, ADAPTIVE_PAGE_COOLDOWN
    if current_max >= PAGE_SIZE_LADDER[0]:
        return current_max, cooldown_remaining
    if cooldown_remaining > 1:
        return current_max, cooldown_remaining - 1
    curr_idx = PAGE_SIZE_LADDER.index(current_max)
    next_max = PAGE_SIZE_LADDER[max(0, curr_idx - 1)]
    next_cooldown = ADAPTIVE_PAGE_COOLDOWN if next_max < PAGE_SIZE_LADDER[0] else 0
    return next_max, next_cooldown


def _apply_walk_outcome(walk: PageWalk, outcome: PageFetchOutcome) -> bool:
    """Applies a failed or deferred PageFetchOutcome to `walk`; returns True when failed/deferred."""
    if outcome.connection is not None:
        return False
    if outcome.error_kind == DEFERRED_RATE_LIMIT:
        walk.deferred = True
    else:
        walk.failed = True
    walk.error_kind = outcome.error_kind
    walk.error_detail = outcome.error_detail
    return True


def _consume_fetched_page(
    walk: PageWalk,
    conn: dict[str, Any],
    cursor: str | None,
    stop_watermark: str | None,
    on_page: PageHandler,
) -> tuple[str | None, bool, bool]:
    """Updates `walk` for a fetched page, invokes `on_page`, and returns (end_cursor, reached_end, bad_cursor)."""
    nodes, accepted, walk.newest_updated, reached_wm, total_count, oldest_date = (
        _extract_page_metadata(conn, stop_watermark, walk.newest_updated)
    )
    if total_count is not None:
        walk.total_count = total_count
    end_cursor, reached_end, bad_cursor = _classify_page_end(
        conn, cursor, reached_wm or not nodes
    )
    next_cursor = cursor if bad_cursor else end_cursor
    walk.end_cursor = next_cursor
    walk.pages += 1
    walk.items_fetched += len(accepted)
    on_page(
        PageBatch(
            nodes=accepted,
            cursor=next_cursor,
            items_fetched=walk.items_fetched,
            total_count=walk.total_count,
            oldest_date=oldest_date,
        )
    )
    return end_cursor, reached_end, bad_cursor


def _walk_pages(
    repo: RepoConfig,
    spec: StreamSpec,
    *,
    start_cursor: str | None,
    stop_watermark: str | None,
    max_pages: int,
    runner: GhRunner,
    on_page: PageHandler,
    reporter: ProgressReporter = NULL_REPORTER,
    auth_probe: AuthProbeFn | None = None,
    budget: RateBudget | None = None,
    priority: Priority = Priority.MIRROR,
) -> PageWalk:
    """Pages a GraphQL connection in UPDATED_AT DESC order, handing each page to on_page.

    Stops when GitHub reports no further pages, when stop_watermark is reached,
    after max_pages pages, when a page fetch fails, when GitHub returns a
    missing or repeated endCursor (which would otherwise loop forever), or
    when `budget` denies the next page at `priority` (marked `deferred`).
    """
    walk = PageWalk(end_cursor=start_cursor)
    cursor = start_cursor
    current_max_page_size = PAGE_SIZE_LADDER[0]
    cooldown_remaining = 0
    is_budgeted = bool(getattr(runner, "is_budgeted_runner", False))
    while walk.pages < max_pages:
        if (
            budget is not None
            and not is_budgeted
            and not budget.allows("graphql", priority)
        ):
            walk.deferred = True
            walk.error_kind = DEFERRED_RATE_LIMIT
            walk.error_detail = f"graphql budget below the {priority.name} threshold"
            return walk
        outcome = _fetch_graphql_page_with_halving(
            repo.owner,
            repo.name,
            spec.query,
            spec.field_name,
            cursor,
            runner,
            reporter=reporter,
            repo_full_name=repo.full_name,
            stream=spec.stream,
            auth_probe=auth_probe,
            budget=budget,
            max_page_size=current_max_page_size,
        )
        if _apply_walk_outcome(walk, outcome):
            return walk
        assert outcome.connection is not None
        current_max_page_size, cooldown_remaining = _next_adaptive_page_size(
            current_max_page_size, outcome.used_page_size, cooldown_remaining
        )
        end_cursor, reached_end, bad_cursor = _consume_fetched_page(
            walk, outcome.connection, cursor, stop_watermark, on_page
        )
        if reached_end:
            walk.reached_end = True
            return walk
        if bad_cursor:
            walk.failed = True
            walk.error_kind = "bad_cursor"
            walk.error_detail = "GitHub returned a missing or repeated endCursor"
            return walk
        cursor = end_cursor
    return walk


MentionSource = tuple[int, Iterable[str]]
"""An item number in the synced repository and the texts it contains."""


def _thread_reply_bodies(comments_json: str) -> list[str]:
    """Returns every comment body stored in a review thread's `comments_json`."""
    try:
        nodes = json.loads(comments_json or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(nodes, list):
        return []
    return [str(n.get("body") or "") for n in nodes if isinstance(n, dict)]


def _pr_mention_texts(pr: PrRecord) -> Iterator[str]:
    """Yields the PR body, comments, reviews, threads, and thread replies."""
    yield pr.body
    for comment in pr.comments:
        yield comment.body
    for review in pr.reviews:
        yield review.body
    for thread in pr.review_threads:
        if thread.body:
            yield thread.body
        elif thread.comments_json not in ("", "[]"):
            yield from _thread_reply_bodies(thread.comments_json)


def _enqueue_mentions(
    store: PackratStore, repo: RepoConfig, sources: Iterable[MentionSource]
) -> None:
    """Queues every mirrored item mentioned by `sources` for a timeline refresh.

    Each source item's mention of itself is dropped. Mentions beyond the
    GraphQL query caps (comments, reviews, and review threads) are not seen.
    """
    resolve: RepoResolver = caching_resolver(store.get_repo)
    page_targets: set[tuple[int, int]] = set()
    for number, texts in sources:
        item_targets: set[tuple[int, int]] = set()
        for text in texts:
            item_targets |= extract_item_mentions(text, repo, resolve)
        item_targets.discard((repo.repo_id, number))
        page_targets |= item_targets
    if page_targets:
        store.enqueue_timeline_dirty(page_targets, reason="mention")


def _persist_issues(
    store: PackratStore, repo: RepoConfig, nodes: list[dict[str, Any]], stats: SyncStats
) -> None:
    """Upserts issue nodes with their comments and search chunks."""
    if not nodes:
        return
    issues: list[IssueRecord] = []
    comments: list[CommentRecord] = []
    chunks: list[TextChunk] = []
    mention_sources: list[MentionSource] = []
    for node in nodes:
        iss, cms, chs = _parse_issue_node(repo, node)
        issues.append(iss)
        comments.extend(cms)
        chunks.extend(chs)
        mention_sources.append((iss.number, [iss.body, *(c.body for c in cms)]))
    store.upsert_issues(issues, comments)
    store.upsert_chunks(chunks)
    _enqueue_mentions(store, repo, mention_sources)
    stats.issues_upserted += len(issues)
    stats.chunks_upserted += len(chunks)


def _persist_prs(
    store: PackratStore, repo: RepoConfig, nodes: list[dict[str, Any]], stats: SyncStats
) -> None:
    """Upserts pull request nodes with their threads, links, and search chunks."""
    if not nodes:
        return
    prs: list[PrRecord] = []
    chunks: list[TextChunk] = []
    for node in nodes:
        pr, chs = _parse_pr_node(repo, node)
        prs.append(pr)
        chunks.extend(chs)
    store.upsert_pull_requests(prs)
    store.upsert_chunks(chunks)
    _enqueue_mentions(store, repo, ((pr.number, _pr_mention_texts(pr)) for pr in prs))
    stats.prs_upserted += len(prs)
    stats.chunks_upserted += len(chunks)


def _persist_discussions(
    store: PackratStore, repo: RepoConfig, nodes: list[dict[str, Any]], stats: SyncStats
) -> None:
    """Upserts discussion nodes and their search chunks."""
    if not nodes:
        return
    discussions: list[DiscussionRecord] = []
    chunks: list[TextChunk] = []
    for node in nodes:
        disc, ch = _parse_discussion_node(repo, node)
        discussions.append(disc)
        chunks.append(ch)
    store.upsert_discussions(discussions)
    store.upsert_chunks(chunks)
    _enqueue_mentions(
        store,
        repo,
        ((d.number, (d.body, d.answer_body or "")) for d in discussions),
    )
    stats.discussions_upserted += len(discussions)
    stats.chunks_upserted += len(chunks)


STREAM_SPECS: tuple[StreamSpec, ...] = (
    StreamSpec("issues", ISSUES_DELTA_QUERY, "issues", _persist_issues),
    StreamSpec("prs", PRS_DELTA_QUERY, "pullRequests", _persist_prs),
    StreamSpec(
        "discussions", DISCUSSIONS_DELTA_QUERY, "discussions", _persist_discussions
    ),
)


def _store_watermark(
    store: PackratStore, repo: RepoConfig, stream: str, value: str | None
) -> None:
    """Persists a stream's UPDATED_AT watermark (None leaves the stored value unchanged)."""
    store.update_repo_sync_state(repo.repo_id, **{f"{stream}_watermark": value})


def _mark_backfill_incomplete(stats: SyncStats, stream: str) -> None:
    """Appends `stream` to `stats.backfill_incomplete` once."""
    if stream not in stats.backfill_incomplete:
        stats.backfill_incomplete.append(stream)


def _has_pending_gap(store: PackratStore, repo_id: int) -> bool:
    """Returns True when any stream has a pending incremental gap cursor."""
    get_gap = getattr(store, "get_gap_state", None)
    if get_gap is None:
        return False
    return any(get_gap(repo_id, s)[0] is not None for s in BACKFILL_STREAMS)


def _is_backfill_or_gap_pending(store: PackratStore, repo: RepoConfig) -> bool:
    """Returns True when any stream has unfinished backfill or a pending incremental gap."""
    if not all(repo.backfill(s).complete for s in BACKFILL_STREAMS):
        return True
    return _has_pending_gap(store, repo.repo_id)


def _resume_incremental_gap(
    store: PackratStore,
    repo: RepoConfig,
    spec: StreamSpec,
    *,
    gap_cursor: str,
    gap_floor: str | None,
    remaining_pages: int,
    runner: GhRunner,
    on_page: PageHandler,
    stats: SyncStats,
    reporter: ProgressReporter,
    auth_probe: AuthProbeFn | None,
    budget: RateBudget | None,
) -> PageWalk | None:
    """Resumes a pending incremental gap walk from `gap_cursor` down to `gap_floor`."""
    set_gap = getattr(store, "set_gap_state", None)
    if remaining_pages <= 0:
        _mark_backfill_incomplete(stats, spec.stream)
        return None
    gap_walk = _walk_pages(
        repo,
        spec,
        start_cursor=gap_cursor,
        stop_watermark=gap_floor,
        max_pages=remaining_pages,
        runner=runner,
        on_page=on_page,
        reporter=reporter,
        auth_probe=auth_probe,
        budget=budget,
        priority=Priority.MIRROR,
    )
    _record_walk_failure(gap_walk, repo.full_name, spec.stream, stats, reporter)
    if gap_walk.reached_end:
        if set_gap is not None:
            set_gap(repo.repo_id, spec.stream, cursor=None, floor=None)
        return gap_walk
    if not gap_walk.failed and gap_walk.end_cursor and set_gap is not None:
        set_gap(repo.repo_id, spec.stream, cursor=gap_walk.end_cursor, floor=gap_floor)
    _mark_backfill_incomplete(stats, spec.stream)
    return gap_walk


def _save_truncated_gap(
    store: PackratStore,
    repo: RepoConfig,
    spec: StreamSpec,
    *,
    watermark: str | None,
    newest_updated: str | None,
    end_cursor: str,
    gap_cursor: str | None,
    gap_floor: str | None,
    stats: SyncStats,
) -> None:
    """Advances the watermark and saves gap state after a truncated or deferred walk."""
    _store_watermark(
        store, repo, spec.stream, _max_timestamp(watermark, newest_updated)
    )
    effective_floor = gap_floor if gap_cursor is not None else watermark
    set_gap = getattr(store, "set_gap_state", None)
    if set_gap is not None:
        set_gap(repo.repo_id, spec.stream, cursor=end_cursor, floor=effective_floor)
    _mark_backfill_incomplete(stats, spec.stream)


def _run_incremental_walk(
    store: PackratStore,
    repo: RepoConfig,
    spec: StreamSpec,
    *,
    max_pages: int,
    runner: GhRunner,
    stats: SyncStats,
    reporter: ProgressReporter = NULL_REPORTER,
    auth_probe: AuthProbeFn | None = None,
    budget: RateBudget | None = None,
) -> PageWalk:
    """Fetches items updated since the stream's watermark at `MIRROR` priority.

    When a walk is cut short by `max_pages` or a mid-walk budget deferral
    after fetching at least one page, the watermark advances to the newest
    fetched item and `(end_cursor, old_watermark)` is persisted in gap state
    so the next sync resumes bridging the gap without refetching top pages.
    """
    watermark = getattr(repo, f"{spec.stream}_watermark")
    get_gap = getattr(store, "get_gap_state", None)
    gap_cursor, gap_floor = (
        get_gap(repo.repo_id, spec.stream) if get_gap is not None else (None, None)
    )
    wm_short = str(watermark)[:10] if watermark else "start"
    reporter.start_phase(repo.full_name, f"github {spec.stream}", total=None)

    def on_page(batch: PageBatch) -> None:
        spec.persist(store, repo, batch.nodes, stats)
        oldest = batch.oldest_date or "-"
        reporter.advance(
            batch.items_fetched,
            None,
            detail=f"oldest {oldest}, target >= {wm_short}",
        )

    walk = _walk_pages(
        repo,
        spec,
        start_cursor=None,
        stop_watermark=watermark,
        max_pages=max_pages,
        runner=runner,
        on_page=on_page,
        reporter=reporter,
        auth_probe=auth_probe,
        budget=budget,
        priority=Priority.MIRROR,
    )
    _record_walk_failure(walk, repo.full_name, spec.stream, stats, reporter)
    if walk.reached_end:
        _store_watermark(
            store, repo, spec.stream, _max_timestamp(watermark, walk.newest_updated)
        )
        if gap_cursor is not None:
            gap_walk = _resume_incremental_gap(
                store,
                repo,
                spec,
                gap_cursor=gap_cursor,
                gap_floor=gap_floor,
                remaining_pages=max_pages - walk.pages,
                runner=runner,
                on_page=on_page,
                stats=stats,
                reporter=reporter,
                auth_probe=auth_probe,
                budget=budget,
            )
            if gap_walk is not None and not gap_walk.reached_end:
                walk = gap_walk
    elif not walk.failed and walk.end_cursor:
        _save_truncated_gap(
            store,
            repo,
            spec,
            watermark=watermark,
            newest_updated=walk.newest_updated,
            end_cursor=walk.end_cursor,
            gap_cursor=gap_cursor,
            gap_floor=gap_floor,
            stats=stats,
        )
    elif gap_cursor is not None:
        _mark_backfill_incomplete(stats, spec.stream)
    reporter.finish_phase()
    return walk


def _run_backfill_walk(
    store: PackratStore,
    repo: RepoConfig,
    spec: StreamSpec,
    *,
    start_cursor: str | None,
    max_pages: int,
    runner: GhRunner,
    stats: SyncStats,
    reporter: ProgressReporter = NULL_REPORTER,
    auth_probe: AuthProbeFn | None = None,
    budget: RateBudget | None = None,
) -> PageWalk:
    """Fetches a stream's older history at `BACKFILL` priority, saving progress after every page.

    A walk that starts from the newest item (start_cursor is None) also moves the
    watermark to the newest updatedAt on its first page, so later incremental
    walks pick up newer changes while the saved cursor covers older items.
    """
    watermark_pending = start_cursor is None
    old_watermark = getattr(repo, f"{spec.stream}_watermark")
    reporter.start_phase(repo.full_name, f"github {spec.stream}", total=None)

    def on_page(batch: PageBatch) -> None:
        nonlocal watermark_pending
        spec.persist(store, repo, batch.nodes, stats)
        if watermark_pending:
            _store_watermark(
                store,
                repo,
                spec.stream,
                _max_timestamp(old_watermark, _newest_updated(batch.nodes)),
            )
            watermark_pending = False
        store.set_backfill_state(
            repo.repo_id, spec.stream, cursor=batch.cursor, done_at=None
        )
        detail = f"oldest {batch.oldest_date}" if batch.oldest_date else ""
        reporter.advance(batch.items_fetched, batch.total_count, detail=detail)

    walk = _walk_pages(
        repo,
        spec,
        start_cursor=start_cursor,
        stop_watermark=None,
        max_pages=max_pages,
        runner=runner,
        on_page=on_page,
        reporter=reporter,
        auth_probe=auth_probe,
        budget=budget,
        priority=Priority.BACKFILL,
    )
    reporter.finish_phase()
    _record_walk_failure(walk, repo.full_name, spec.stream, stats, reporter)
    if walk.reached_end:
        store.set_backfill_state(
            repo.repo_id, spec.stream, cursor=None, done_at=utc_now_iso()
        )
    else:
        stats.backfill_incomplete.append(spec.stream)
    return walk


def _sync_stream(
    store: PackratStore,
    repo: RepoConfig,
    spec: StreamSpec,
    *,
    max_pages: int,
    backfill_max_pages: int,
    runner: GhRunner,
    stats: SyncStats,
    reporter: ProgressReporter = NULL_REPORTER,
    auth_probe: AuthProbeFn | None = None,
    budget: RateBudget | None = None,
) -> str | None:
    """Syncs one GitHub stream, backfilling its history until the last page if needed.

    - Backfill complete: incremental walk only.
    - Backfill never started (or no watermark yet): backfill from the newest item.
    - Backfill in progress: incremental walk, then resume the backfill from its cursor,
      unless the incremental walk hit an auth failure or a rate limit.
    """
    state = repo.backfill(spec.stream)
    walk_args: dict[str, Any] = {
        "runner": runner,
        "stats": stats,
        "reporter": reporter,
        "auth_probe": auth_probe,
        "budget": budget,
    }
    if state.complete:
        return _run_incremental_walk(
            store, repo, spec, max_pages=max_pages, **walk_args
        ).error_kind
    if state.cursor is None or getattr(repo, f"{spec.stream}_watermark") is None:
        if backfill_max_pages <= 0:
            stats.backfill_incomplete.append(spec.stream)
            return None
        return _run_backfill_walk(
            store,
            repo,
            spec,
            start_cursor=None,
            max_pages=backfill_max_pages,
            **walk_args,
        ).error_kind
    inc_walk = _run_incremental_walk(
        store, repo, spec, max_pages=max_pages, **walk_args
    )
    if backfill_max_pages <= 0 or inc_walk.error_kind in (
        "keyring_locked",
        "auth_error",
        *_DEFERRING_ERROR_KINDS,
    ):
        stats.backfill_incomplete.append(spec.stream)
        return inc_walk.error_kind
    return _run_backfill_walk(
        store,
        repo,
        spec,
        start_cursor=state.cursor,
        max_pages=backfill_max_pages,
        **walk_args,
    ).error_kind


def _persist_open_pr_forks(
    store: PackratStore, repo: RepoConfig, nodes: list[dict[str, Any]], stats: SyncStats
) -> None:
    """Updates fork-owner columns on stored open pull requests without rewriting children."""
    if not nodes:
        return
    rows: list[tuple[int, str | None, bool | None]] = []
    for node in nodes:
        cross_repo = node.get("isCrossRepository")
        rows.append(
            (
                int(node["number"]),
                _login(node, "headRepositoryOwner"),
                cross_repo if isinstance(cross_repo, bool) else None,
            )
        )
    update_fn = getattr(store, "update_pr_fork_owners", None)
    if update_fn is not None:
        update_fn(repo.repo_id, rows)
        stats.prs_upserted += len(rows)
        return
    _persist_prs(store, repo, nodes, stats)


OPEN_PRS_SPEC = StreamSpec(
    "open_prs", OPEN_PRS_QUERY, "pullRequests", _persist_open_pr_forks
)
"""One-time walk over every open PR that fills fork fields added after the PRs were stored."""


def refetch_open_prs_key(repo_id: int) -> str:
    """Returns the `service_state` key that marks a pending open-PR refetch for a repo."""
    return f"{REFETCH_OPEN_PRS_KEY_PREFIX}{repo_id}"


def _run_open_prs_refetch(
    store: PackratStore,
    repo: RepoConfig,
    *,
    runner: GhRunner,
    stats: SyncStats,
    reporter: ProgressReporter = NULL_REPORTER,
    auth_probe: AuthProbeFn | None = None,
    budget: RateBudget | None = None,
    max_pages: int = DEFAULT_BACKFILL_MAX_PAGES,
) -> PageWalk:
    """Re-fetches every open PR at `MIRROR` priority and clears the pending flag.

    The flag is cleared only when the walk reached the last page. A deferred
    walk lands in `stats.deferred_streams` and a failed one in `stats.errors`,
    both under the stream name "open_prs", and the flag stays set so the next
    sync tries again from the first page.
    """
    reporter.start_phase(repo.full_name, f"github {OPEN_PRS_SPEC.stream}", total=None)

    def on_page(batch: PageBatch) -> None:
        OPEN_PRS_SPEC.persist(store, repo, batch.nodes, stats)
        reporter.advance(batch.items_fetched, batch.total_count)

    walk = _walk_pages(
        repo,
        OPEN_PRS_SPEC,
        start_cursor=None,
        stop_watermark=None,
        max_pages=max_pages,
        runner=runner,
        on_page=on_page,
        reporter=reporter,
        auth_probe=auth_probe,
        budget=budget,
        priority=Priority.MIRROR,
    )
    reporter.finish_phase()
    _record_walk_failure(walk, repo.full_name, OPEN_PRS_SPEC.stream, stats, reporter)
    if walk.reached_end:
        store.set_state(refetch_open_prs_key(repo.repo_id), None)
    return walk


def refetch_open_prs(
    store: PackratStore,
    repo: RepoConfig,
    runner: GhRunner = run_gh_command,
    *,
    budget: RateBudget | None = None,
    reporter: ProgressReporter = NULL_REPORTER,
) -> int:
    """Re-fetches every open pull request of `repo` with the full PR node fields.

    Fills fields added to the PR query after older rows were stored (such as
    `head_repo_owner` and `is_cross_repository`). Pages are requested at
    `Priority.MIRROR` and stop early when `budget` denies the next page. The
    repo's pending-refetch `service_state` flag is cleared once the walk
    reaches the last page.

    Args:
        store: Destination store.
        repo: Repository whose open PRs are refetched.
        runner: Runs `gh` arguments with a timeout.
        budget: Optional shared rate budget checked before every page.
        reporter: Progress sink.

    Returns:
        The number of pull requests upserted.
    """
    stats = SyncStats(repo_full_name=repo.full_name)
    _run_open_prs_refetch(
        store,
        repo,
        runner=runner,
        stats=stats,
        reporter=reporter,
        auth_probe=_resolve_auth_probe(runner, None),
        budget=budget,
    )
    return stats.prs_upserted


def _maybe_refetch_open_prs(
    store: PackratStore,
    repo: RepoConfig,
    *,
    runner: GhRunner,
    stats: SyncStats,
    reporter: ProgressReporter,
    auth_probe: AuthProbeFn | None,
    budget: RateBudget | None,
) -> str | None:
    """Runs the open-PR refetch when its flag is set; returns the walk's error kind."""
    if not store.get_state(refetch_open_prs_key(repo.repo_id)):
        return None
    walk = _run_open_prs_refetch(
        store,
        repo,
        runner=runner,
        stats=stats,
        reporter=reporter,
        auth_probe=auth_probe,
        budget=budget,
    )
    return walk.error_kind


def _sync_all_streams(
    store: PackratStore,
    repo: RepoConfig,
    *,
    max_pages: int,
    backfill_max_pages: int,
    runner: GhRunner,
    stats: SyncStats,
    reporter: ProgressReporter,
    auth_probe: AuthProbeFn | None,
    budget: RateBudget | None,
) -> None:
    """Syncs every stream in `STREAM_SPECS`, stopping on rate limits or a locked keyring."""
    for idx, spec in enumerate(STREAM_SPECS):
        stream_err = _sync_stream(
            store,
            repo,
            spec,
            max_pages=max_pages,
            backfill_max_pages=backfill_max_pages,
            runner=runner,
            stats=stats,
            reporter=reporter,
            auth_probe=auth_probe,
            budget=budget,
        )
        # Only a GitHub rate-limit response stops the remaining streams. A
        # budget deferral is per stream and priority: a BACKFILL deferral must
        # not stop a sibling's MIRROR walk, and each walk checks the budget
        # before its first call, so deferred siblings cost nothing.
        if stream_err == "rate_limited":
            _mark_remaining_deferred(stats, STREAM_SPECS[idx + 1 :])
            break
        if stream_err == "keyring_locked":
            break


def _resolve_auth_probe(
    runner: GhRunner,
    auth_probe: AuthProbeFn | None,
) -> AuthProbeFn | None:
    """Defaults auth_probe to probe_gh_auth when using a live gh CLI runner."""
    if auth_probe is not None:
        return auth_probe
    if _is_live_gh_runner(runner):
        return probe_gh_auth
    return None


def _skipped_events_poll(repo: RepoConfig) -> EventsPollResult:
    """Returns a "changed" poll result without calling /events.

    Its ETag fields are None, so the stored events ETag is left as it was.
    """
    return EventsPollResult(
        status_code=200,
        not_modified=False,
        etag=None,
        last_modified=None,
        poll_interval_sec=repo.poll_interval_sec,
    )


def _defer_events_poll(
    repo: RepoConfig, budget: RateBudget | None, runner: GhRunner | None = None
) -> EventsPollResult | None:
    """Returns a synthetic rate-limited poll result when the core budget is too low.

    Returns None when the poll may proceed.
    """
    if (
        budget is None
        or bool(getattr(runner, "is_budgeted_runner", False))
        or budget.allows("core", Priority.MIRROR)
    ):
        return None
    return EventsPollResult(
        status_code=0,
        not_modified=False,
        etag=repo.events_etag,
        last_modified=repo.events_last_modified,
        poll_interval_sec=repo.poll_interval_sec,
        error="core budget below the MIRROR threshold",
        rate_limited=True,
    )


def _record_poll_failure(
    stats: SyncStats,
    repo: RepoConfig,
    poll: EventsPollResult,
    reporter: ProgressReporter,
) -> None:
    """Records a failed /events poll as a deferral (rate limits) or an error."""
    if poll.rate_limited:
        _mark_stream_deferred(stats, "events")
        stats.skipped_reason = f"github_{DEFERRED_RATE_LIMIT}: {poll.error}"
        reporter.warn(repo.full_name, f"github sync deferred: {poll.error}")
        return
    stats.skipped_reason = f"github_poll_error: {poll.error}"
    msg = f"github events poll failed: {poll.error}"
    stats.errors.append(msg)
    reporter.error(repo.full_name, msg)


def _mark_remaining_deferred(stats: SyncStats, specs: Sequence[StreamSpec]) -> None:
    """Marks every stream in `specs` deferred after GitHub reported a rate limit."""
    for spec in specs:
        _mark_stream_deferred(stats, spec.stream)


def sync_github_metadata(
    store: PackratStore,
    repo: RepoConfig,
    force: bool = False,
    max_pages: int = 10,
    backfill_max_pages: int = DEFAULT_BACKFILL_MAX_PAGES,
    runner: GhRunner = run_gh_command,
    reporter: ProgressReporter = NULL_REPORTER,
    auth_probe: AuthProbeFn | None = None,
    budget: RateBudget | None = None,
    skip_events_poll: bool = False,
) -> SyncStats:
    """Polls /events with ETag and syncs issues, PRs, and discussions via GraphQL.

    When the repo's open-PR refetch flag is set (see `refetch_open_prs`), every
    open PR is refetched first, even when /events answers 304. The flag is
    cleared once that walk completes; a deferred or failed refetch is reported
    as stream "open_prs" and retried on the next sync.

    Args:
        max_pages: page budget per stream for incremental updates.
        backfill_max_pages: safety cap on pages per stream while backfilling
            history; an unfinished backfill resumes on the next sync.
        force: refetch every stream's full history, ignoring ETags and watermarks.
        auth_probe: optional zero-arg probe called after a GraphQL timeout to
            detect a locked OS keyring and abort retries early.
        budget: optional shared rate budget. Every response's `X-RateLimit-*`
            headers are recorded in it, and each page is skipped when the
            budget denies it (`MIRROR` for incremental, `BACKFILL` for history
            walks). Deferred and rate-limited streams are listed in
            `SyncStats.deferred_streams` instead of `errors`, and their
            watermarks and cursors stay where they were.
        skip_events_poll: skip the /events poll because the caller already
            saw a change (the service's signals worker); the streams are
            walked as if events changed.

    A run that ends with errors or deferrals keeps the previous events ETag,
    so the next sync's poll answers 200 and walks again instead of a 304.
    """
    stats = SyncStats(repo_full_name=repo.full_name)
    now_iso = utc_now_iso()
    poll = _skipped_events_poll(repo) if skip_events_poll else None
    poll = poll or _defer_events_poll(repo, budget, runner)
    poll = poll or poll_repo_events(repo, runner=runner, budget=budget)
    if poll.error:
        _record_poll_failure(stats, repo, poll, reporter)
        return stats

    if force:
        store.reset_backfill_state(repo.repo_id)
        repo = store.get_repo_by_id(repo.repo_id) or repo
    effective_probe = _resolve_auth_probe(runner, auth_probe)
    refetch_err = _maybe_refetch_open_prs(
        store,
        repo,
        runner=runner,
        stats=stats,
        reporter=reporter,
        auth_probe=effective_probe,
        budget=budget,
    )
    sweep_due = needs_safety_sweep(repo, now_iso=now_iso)
    backfill_pending = _is_backfill_or_gap_pending(store, repo)
    if poll.not_modified and not force and not sweep_due and not backfill_pending:
        stats.events_not_modified = True
        store.update_repo_sync_state(
            repo.repo_id,
            events_etag=poll.etag,
            events_last_modified=poll.last_modified,
            poll_interval_sec=poll.poll_interval_sec,
            last_events_check_at=now_iso,
        )
        return stats

    if refetch_err == "rate_limited":
        _mark_remaining_deferred(stats, STREAM_SPECS)
    elif refetch_err != "keyring_locked":
        _sync_all_streams(
            store,
            repo,
            max_pages=max_pages,
            backfill_max_pages=backfill_max_pages,
            runner=runner,
            stats=stats,
            reporter=reporter,
            auth_probe=effective_probe,
            budget=budget,
        )

    done_iso = utc_now_iso()
    incomplete = bool(stats.errors or stats.deferred)
    store.update_repo_sync_state(
        repo.repo_id,
        events_etag=None if incomplete else poll.etag,
        events_last_modified=None if incomplete else poll.last_modified,
        poll_interval_sec=poll.poll_interval_sec,
        last_events_check_at=done_iso,
        last_full_sweep_at=None if incomplete else done_iso,
    )
    return stats


def sync_backfill_only(
    store: PackratStore,
    repo: RepoConfig,
    *,
    backfill_max_pages: int = DEFAULT_BACKFILL_MAX_PAGES,
    runner: GhRunner = run_gh_command,
    reporter: ProgressReporter = NULL_REPORTER,
    auth_probe: AuthProbeFn | None = None,
    budget: RateBudget | None = None,
) -> SyncStats:
    """Runs only the history backfill walks of streams whose backfill isn't complete.

    Used by the `packrat serve` backfill child (`packrat sync --backfill-only`):
    the service's mirror worker owns the /events poll and the incremental
    delta, so this neither polls events nor walks newer items. A stream whose
    backfill never started walks from the newest item (which also moves its
    watermark); one in progress resumes from its saved cursor. Pages are
    requested at `Priority.BACKFILL`.
    """
    stats = SyncStats(repo_full_name=repo.full_name)
    effective_probe = _resolve_auth_probe(runner, auth_probe)
    pending = [s for s in STREAM_SPECS if not repo.backfill(s.stream).complete]
    for idx, spec in enumerate(pending):
        state = repo.backfill(spec.stream)
        has_watermark = getattr(repo, f"{spec.stream}_watermark") is not None
        walk = _run_backfill_walk(
            store,
            repo,
            spec,
            start_cursor=state.cursor if has_watermark else None,
            max_pages=backfill_max_pages,
            runner=runner,
            stats=stats,
            reporter=reporter,
            auth_probe=effective_probe,
            budget=budget,
        )
        if walk.error_kind in _DEFERRING_ERROR_KINDS:
            _mark_remaining_deferred(stats, pending[idx + 1 :])
            break
        if walk.error_kind == "keyring_locked":
            break
    return stats
