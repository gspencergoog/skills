"""Conditional HTTP ETag /events polling and GraphQL UPDATED_AT delta syncer for packrat."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from db_store import PackratStore
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

SAFETY_SWEEP_INTERVAL_SEC = 600
PAGE_SIZE_LADDER = (25, 12, 5)

_CLOSING_ISSUE_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b",
    re.IGNORECASE,
)
_REF_ISSUE_RE = re.compile(r"(?:^|[\s(])#(\d+)\b")

ISSUES_DELTA_QUERY = """
query($owner: String!, $name: String!, $pageSize: Int!, $cursor: String) {
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

PRS_DELTA_QUERY = """
query($owner: String!, $name: String!, $pageSize: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: $pageSize, after: $cursor, orderBy: {field: UPDATED_AT, direction: DESC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
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
            author { login }
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
      }
    }
  }
}
"""

DISCUSSIONS_DELTA_QUERY = """
query($owner: String!, $name: String!, $pageSize: Int!, $cursor: String) {
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
class GhCommandResult:
    """Result of executing a gh CLI subprocess."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


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


GhRunner = Callable[[Sequence[str], int], GhCommandResult]


def run_gh_command(args: Sequence[str], timeout: int = 20) -> GhCommandResult:
    """Executes gh with non-interactive environment variables and a strict timeout."""
    env = {
        **os.environ,
        "GH_PROMPT_DISABLED": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "PAGER": "cat",
    }
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            check=False,
        )
        return GhCommandResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            timed_out=False,
        )
    except subprocess.TimeoutExpired as exc:
        return GhCommandResult(
            returncode=124,
            stdout=exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
            stderr="gh command timed out (possible locked keyring or network stall)",
            timed_out=True,
        )
    except OSError as exc:
        return GhCommandResult(returncode=127, stdout="", stderr=str(exc), timed_out=False)


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


def _split_http_response(raw: str) -> tuple[int, dict[str, str], str]:
    """Splits `gh api -i` output into status code, lowercase header dict, and body."""
    normalized = raw.replace("\r\n", "\n")
    parts = normalized.split("\n\n", 1)
    header_block = parts[0]
    body = parts[1] if len(parts) > 1 else ""
    lines = [ln for ln in header_block.splitlines() if ln.strip()]
    if not lines:
        return 0, {}, body

    status_code = 0
    first_tokens = lines[0].split()
    if len(first_tokens) >= 2 and first_tokens[1].isdigit():
        status_code = int(first_tokens[1])

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return status_code, headers, body


def poll_repo_events(
    repo: RepoConfig,
    runner: GhRunner = run_gh_command,
) -> EventsPollResult:
    """Polls /repos/<owner>/<name>/events with If-None-Match and If-Modified-Since."""
    args: list[str] = [
        "api",
        "-i",
        f"repos/{repo.owner}/{repo.name}/events?per_page=30",
    ]
    if repo.events_etag:
        args.extend(["-H", f"If-None-Match: {repo.events_etag}"])
    if repo.events_last_modified:
        args.extend(["-H", f"If-Modified-Since: {repo.events_last_modified}"])

    res = runner(args, 15)
    status_code, headers, body = _split_http_response(res.stdout)
    etag = headers.get("etag") or repo.events_etag
    last_mod = headers.get("last-modified") or repo.events_last_modified
    poll_interval = int(headers.get("x-poll-interval", str(repo.poll_interval_sec or 60)))

    if status_code == 304 or "304 Not Modified" in res.stdout or "304 Not Modified" in res.stderr:
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
        )

    try:
        parsed = json.loads(body) if body.strip() else []
        events = tuple(parsed) if isinstance(parsed, list) else ()
    except json.JSONDecodeError:
        events = ()

    return EventsPollResult(
        status_code=status_code,
        not_modified=False,
        etag=etag,
        last_modified=last_mod,
        poll_interval_sec=poll_interval,
        events=events,
    )


def extract_issue_links(repo_id: int, pr_number: int, body: str, closing_nums: Sequence[int]) -> tuple[IssuePrLink, ...]:
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
        IssuePrLink(repo_id=repo_id, issue_number=issue_num, pr_number=pr_number, link_type=ltype)
        for issue_num, ltype in sorted(links.items())
    )


def _login(node: dict[str, Any] | None) -> str | None:
    """Safely extracts author.login from a GraphQL actor object."""
    if not isinstance(node, dict):
        return None
    author = node.get("author")
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


def _extract_string_field_tuple(node: dict[str, Any], conn_key: str, item_key: str) -> tuple[str, ...]:
    """Extracts a tuple of strings from a nested GraphQL connection's nodes."""
    return tuple(str(n[item_key]) for n in _connection_nodes(node, conn_key) if n.get(item_key))


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
        c_title = f"Comment on {parent_label} #{parent_number} by {cm.author or 'unknown'}"
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
    comments = _parse_comments(repo.repo_id, "issue", number, _connection_nodes(node, "comments"))
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
    chunks = [issue_chunk, *_comments_to_chunks(repo, "Issue", number, updated_at, comments)]
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
        reviewer=_login(first),
        author_association=first.get("authorAssociation"),
        created_at=first.get("createdAt"),
        updated_at=t_updated,
        body=combined_body,
        comments_json=json.dumps(c_nodes),
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


def _parse_pr_files(repo_id: int, pr_number: int, node: dict[str, Any]) -> tuple[PrFileRecord, ...]:
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


def _parse_pr_reviews(repo_id: int, pr_number: int, node: dict[str, Any]) -> tuple[ReviewRecord, ...]:
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
    comments = _parse_comments(repo.repo_id, "pr", number, _connection_nodes(node, "comments"))
    merge_commit = node.get("mergeCommit")

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
        merge_commit_sha=merge_commit.get("oid") if isinstance(merge_commit, dict) else None,
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
    chunks = [pr_chunk, *thread_chunks, *_comments_to_chunks(repo, "PR", number, updated_at, comments)]
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
    answer_body = str(ans.get("body")) if isinstance(ans, dict) and ans.get("body") else None
    disc = DiscussionRecord(
        repo_id=repo.repo_id,
        number=number,
        node_id=node.get("id"),
        category=str(cat.get("name")) if isinstance(cat, dict) and cat.get("name") else None,
        author=_login(node),
        title=title,
        body=body,
        answer_body=answer_body,
        created_at=node.get("createdAt"),
        updated_at=updated_at,
        url=node.get("url"),
    )
    full_text = f"{body}\n\nAccepted Answer:\n{answer_body}".strip() if answer_body else body
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


def _extract_graphql_connection(stdout: str, field_name: str) -> dict[str, Any] | None:
    """Parses GraphQL JSON output and returns data.repository.<field_name> if valid."""
    if not stdout.strip():
        return None
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    repo_obj = (payload.get("data") or {}).get("repository") or {}
    conn = repo_obj.get(field_name)
    return conn if isinstance(conn, dict) else None


def _is_fatal_auth_error(stderr: str) -> bool:
    """Returns True when stderr indicates an unrecoverable auth or keyring failure."""
    lowered = stderr.lower()
    return "401" in lowered or "403" in lowered or "locked" in lowered


@dataclass(frozen=True)
class PageFetchOutcome:
    """Structured result of fetching a single GraphQL page across halving steps."""

    connection: dict[str, Any] | None
    error_kind: str | None = None
    error_detail: str | None = None


@dataclass(frozen=True)
class PageBatch:
    """Parsed page payload delivered to on_page callbacks."""

    nodes: list[dict[str, Any]]
    cursor: str | None
    items_fetched: int
    total_count: int | None
    oldest_date: str | None


def _classify_gh_failure(res: GhCommandResult) -> tuple[str, str]:
    """Classifies a failed GraphQL subprocess result into (error_kind, short_reason)."""
    raw_err = (res.stderr or res.stdout or "").strip()
    first_line = raw_err.splitlines()[0].strip() if raw_err else f"gh exited with {res.returncode}"
    if res.timed_out or res.returncode == 124:
        return "timeout", first_line
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
) -> PageFetchOutcome:
    """Executes a GraphQL page query, halving pageSize (25 -> 12 -> 5) on 5xx/timeouts."""
    last_kind = "http_error"
    last_detail = "GraphQL page fetch failed"
    for idx, page_size in enumerate(PAGE_SIZE_LADDER):
        args = _build_graphql_args(owner, name, query, page_size, cursor)
        res = runner(args, 25)
        if res.returncode == 0:
            conn = _extract_graphql_connection(res.stdout, field_name)
            if conn is not None:
                return PageFetchOutcome(connection=conn)
        last_kind, last_detail = _classify_gh_failure(res)
        if last_kind == "auth_error":
            break
        if idx + 1 < len(PAGE_SIZE_LADDER):
            next_size = PAGE_SIZE_LADDER[idx + 1]
            reporter.warn(
                repo_full_name,
                f"github {stream}: page request failed at pageSize={page_size} ({last_detail}); retrying at pageSize={next_size}",
            )
    return PageFetchOutcome(connection=None, error_kind=last_kind, error_detail=last_detail)


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

StreamPersister = Callable[[PackratStore, RepoConfig, list[dict[str, Any]], SyncStats], None]
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
    reached_end: bool = False
    failed: bool = False
    error_kind: str | None = None
    error_detail: str | None = None


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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None, bool, int | None, str | None]:
    """Unpacks a GraphQL connection page into (nodes, accepted, newest, reached_wm, total_count, oldest_date)."""
    nodes = [n for n in (conn.get("nodes") or []) if isinstance(n, dict) and n.get("number")]
    accepted, newest, reached_wm = _filter_nodes_above_watermark(nodes, stop_watermark, newest_updated)
    raw_total = conn.get("totalCount")
    total_count = raw_total if isinstance(raw_total, int) and not isinstance(raw_total, bool) else None
    sample = accepted or nodes
    stamps = [str(n.get("updatedAt"))[:10] for n in sample if n.get("updatedAt")]
    oldest_date = min(stamps) if stamps else None
    return nodes, accepted, newest, reached_wm, total_count, oldest_date


def _record_walk_failure(
    walk: PageWalk,
    repo_name: str,
    stream: str,
    stats: SyncStats,
    reporter: ProgressReporter,
) -> None:
    """Appends a structured walk failure message to stats.errors and notifies reporter."""
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
) -> PageWalk:
    """Pages a GraphQL connection in UPDATED_AT DESC order, handing each page to on_page.

    Stops when GitHub reports no further pages, when stop_watermark is reached,
    after max_pages pages, when a page fetch fails, or when GitHub returns a
    missing or repeated endCursor (which would otherwise loop forever).
    """
    walk = PageWalk()
    cursor = start_cursor
    while walk.pages < max_pages:
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
        )
        if outcome.connection is None:
            walk.failed = True
            walk.error_kind = outcome.error_kind
            walk.error_detail = outcome.error_detail
            return walk
        nodes, accepted, walk.newest_updated, reached_wm, total_count, oldest_date = _extract_page_metadata(
            outcome.connection, stop_watermark, walk.newest_updated
        )
        if total_count is not None:
            walk.total_count = total_count
        end_cursor, reached_end, bad_cursor = _classify_page_end(
            outcome.connection, cursor, reached_wm or not nodes
        )
        walk.pages += 1
        walk.items_fetched += len(accepted)
        on_page(
            PageBatch(
                nodes=accepted,
                cursor=cursor if bad_cursor else end_cursor,
                items_fetched=walk.items_fetched,
                total_count=walk.total_count,
                oldest_date=oldest_date,
            )
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


def _persist_issues(store: PackratStore, repo: RepoConfig, nodes: list[dict[str, Any]], stats: SyncStats) -> None:
    """Upserts issue nodes with their comments and search chunks."""
    if not nodes:
        return
    issues: list[IssueRecord] = []
    comments: list[CommentRecord] = []
    chunks: list[TextChunk] = []
    for node in nodes:
        iss, cms, chs = _parse_issue_node(repo, node)
        issues.append(iss)
        comments.extend(cms)
        chunks.extend(chs)
    store.upsert_issues(issues, comments)
    store.upsert_chunks(chunks)
    stats.issues_upserted += len(issues)
    stats.chunks_upserted += len(chunks)


def _persist_prs(store: PackratStore, repo: RepoConfig, nodes: list[dict[str, Any]], stats: SyncStats) -> None:
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
    stats.discussions_upserted += len(discussions)
    stats.chunks_upserted += len(chunks)


STREAM_SPECS: tuple[StreamSpec, ...] = (
    StreamSpec("issues", ISSUES_DELTA_QUERY, "issues", _persist_issues),
    StreamSpec("prs", PRS_DELTA_QUERY, "pullRequests", _persist_prs),
    StreamSpec("discussions", DISCUSSIONS_DELTA_QUERY, "discussions", _persist_discussions),
)


def _store_watermark(store: PackratStore, repo: RepoConfig, stream: str, value: str | None) -> None:
    """Persists a stream's UPDATED_AT watermark (None leaves the stored value unchanged)."""
    store.update_repo_sync_state(repo.repo_id, **{f"{stream}_watermark": value})


def _run_incremental_walk(
    store: PackratStore,
    repo: RepoConfig,
    spec: StreamSpec,
    *,
    max_pages: int,
    runner: GhRunner,
    stats: SyncStats,
    reporter: ProgressReporter = NULL_REPORTER,
) -> None:
    """Fetches items updated since the stream's watermark.

    The watermark only advances when the walk reaches it (or the last page), so a
    walk cut short by max_pages or an error never leaves a gap behind it.
    """
    watermark = getattr(repo, f"{spec.stream}_watermark")
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
    )
    reporter.finish_phase()
    _record_walk_failure(walk, repo.full_name, spec.stream, stats, reporter)
    if walk.reached_end:
        _store_watermark(store, repo, spec.stream, _max_timestamp(watermark, walk.newest_updated))


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
) -> None:
    """Fetches a stream's older history until the last page, saving progress after every page.

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
            _store_watermark(store, repo, spec.stream, _max_timestamp(old_watermark, _newest_updated(batch.nodes)))
            watermark_pending = False
        store.set_backfill_state(repo.repo_id, spec.stream, cursor=batch.cursor, done_at=None)
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
    )
    reporter.finish_phase()
    _record_walk_failure(walk, repo.full_name, spec.stream, stats, reporter)
    if walk.reached_end:
        store.set_backfill_state(repo.repo_id, spec.stream, cursor=None, done_at=utc_now_iso())
    else:
        stats.backfill_incomplete.append(spec.stream)


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
) -> None:
    """Syncs one GitHub stream, backfilling its history until the last page if needed.

    - Backfill complete: incremental walk only.
    - Backfill never started (or no watermark yet): backfill from the newest item.
    - Backfill in progress: incremental walk, then resume the backfill from its cursor.
    """
    state = repo.backfill(spec.stream)
    walk_args: dict[str, Any] = {"runner": runner, "stats": stats, "reporter": reporter}
    if state.complete:
        _run_incremental_walk(store, repo, spec, max_pages=max_pages, **walk_args)
        return
    if state.cursor is None or getattr(repo, f"{spec.stream}_watermark") is None:
        _run_backfill_walk(store, repo, spec, start_cursor=None, max_pages=backfill_max_pages, **walk_args)
        return
    _run_incremental_walk(store, repo, spec, max_pages=max_pages, **walk_args)
    _run_backfill_walk(store, repo, spec, start_cursor=state.cursor, max_pages=backfill_max_pages, **walk_args)


def sync_github_metadata(
    store: PackratStore,
    repo: RepoConfig,
    force: bool = False,
    max_pages: int = 10,
    backfill_max_pages: int = DEFAULT_BACKFILL_MAX_PAGES,
    runner: GhRunner = run_gh_command,
    reporter: ProgressReporter = NULL_REPORTER,
) -> SyncStats:
    """Polls /events with ETag and syncs issues, PRs, and discussions via GraphQL.

    Args:
        max_pages: page budget per stream for incremental updates.
        backfill_max_pages: safety cap on pages per stream while backfilling
            history; an unfinished backfill resumes on the next sync.
        force: refetch every stream's full history, ignoring ETags and watermarks.
    """
    stats = SyncStats(repo_full_name=repo.full_name)
    now_iso = utc_now_iso()
    poll = poll_repo_events(repo, runner=runner)
    if poll.error:
        stats.skipped_reason = f"github_poll_error: {poll.error}"
        msg = f"github events poll failed: {poll.error}"
        stats.errors.append(msg)
        reporter.error(repo.full_name, msg)
        return stats

    if force:
        store.reset_backfill_state(repo.repo_id)
        repo = store.get_repo_by_id(repo.repo_id) or repo
    sweep_due = needs_safety_sweep(repo, now_iso=now_iso)
    backfill_pending = not all(repo.backfill(s).complete for s in BACKFILL_STREAMS)
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

    for spec in STREAM_SPECS:
        _sync_stream(
            store,
            repo,
            spec,
            max_pages=max_pages,
            backfill_max_pages=backfill_max_pages,
            runner=runner,
            stats=stats,
            reporter=reporter,
        )

    done_iso = utc_now_iso()
    sweep_ts = repo.last_full_sweep_at if stats.errors else done_iso
    store.update_repo_sync_state(
        repo.repo_id,
        events_etag=poll.etag,
        events_last_modified=poll.last_modified,
        poll_interval_sec=poll.poll_interval_sec,
        last_events_check_at=done_iso,
        last_full_sweep_at=sweep_ts,
    )
    return stats
