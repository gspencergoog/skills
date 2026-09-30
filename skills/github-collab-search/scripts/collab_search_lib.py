#!/usr/bin/env python3
"""collab_search_lib.py - Core library for discovering and summarizing GitHub interactions between users.

Overcomes GitHub's search API boolean OR qualifier limitation and 1,000-result cap
by querying GraphQL with date-partitioned bisection and participant set filtering.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


@dataclass
class UserActivity:
    """Records actions performed by a user within a thread."""

    user: str
    is_author: bool = False
    reviews: List[Dict[str, Any]] = field(default_factory=list)
    comments: List[Dict[str, Any]] = field(default_factory=list)
    review_comments: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class CollaborativeThread:
    """Represents a GitHub Pull Request or Issue where target users interacted."""

    number: int
    title: str
    type: str  # "PR" or "Issue"
    state: str  # "OPEN", "MERGED", "CLOSED"
    url: str
    created_at: str
    updated_at: str
    closed_at: Optional[str] = None
    merged_at: Optional[str] = None
    author: str = ""
    participants: Set[str] = field(default_factory=set)
    user_a_activity: Optional[UserActivity] = None
    user_b_activity: Optional[UserActivity] = None


def run_gh_cmd(
    cmd: Sequence[str],
    cwd: Optional[Path] = None,
) -> Tuple[int, str, str]:
    """Runs a GitHub CLI command with sanitized environment."""
    env = dict(os.environ)
    if "GITHUB_TOKEN" in env:
        del env["GITHUB_TOKEN"]

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            env=env,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except OSError as e:
        return 1, "", str(e)


def detect_repo_slug(cwd: Optional[Path] = None) -> Tuple[str, str]:
    """Detects GitHub owner and repo name from local git remotes."""
    start_dir = cwd or Path.cwd()
    code, stdout, _ = run_gh_cmd(["git", "remote", "get-url", "upstream"], cwd=start_dir)
    if code != 0 or not stdout:
        code, stdout, _ = run_gh_cmd(["git", "remote", "get-url", "origin"], cwd=start_dir)
        if code != 0 or not stdout:
            return "", ""

    url = stdout.strip()
    m = re.search(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$", url)
    if m:
        return m.group("owner"), m.group("repo")
    return "", ""


def build_search_query(
    target: str,
    user_a: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> str:
    """Constructs GraphQL search query string."""
    scope = target if (target.startswith("repo:") or target.startswith("org:")) else f"repo:{target}"
    parts = [scope, "is:issue,pr", f"involves:{user_a}"]
    if start_date and end_date:
        parts.append(f"updated:{start_date}..{end_date}")
    elif start_date:
        parts.append(f"updated:>={start_date}")
    elif end_date:
        parts.append(f"updated:<={end_date}")
    return " ".join(parts)


GRAPHQL_SEARCH_QUERY = """
query($query: String!, $cursor: String) {
  search(query: $query, type: ISSUE, first: 100, after: $cursor) {
    issueCount
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      __typename
      ... on PullRequest {
        number
        title
        url
        state
        isDraft
        createdAt
        updatedAt
        closedAt
        mergedAt
        author { login }
        comments(first: 100) {
          nodes {
            author { login }
            body
            createdAt
            url
          }
        }
        reviews(first: 50) {
          nodes {
            author { login }
            state
            submittedAt
            url
            body
            comments(first: 50) {
              nodes {
                author { login }
                body
                createdAt
                path
                url
              }
            }
          }
        }
      }
      ... on Issue {
        number
        title
        url
        state
        createdAt
        updatedAt
        closedAt
        author { login }
        comments(first: 100) {
          nodes {
            author { login }
            body
            createdAt
            url
          }
        }
      }
    }
  }
}
"""


def fetch_graphql_page(
    query_str: str,
    cursor: Optional[str] = None,
    cwd: Optional[Path] = None,
) -> Tuple[int, List[Dict[str, Any]], bool, Optional[str]]:
    """Fetches a single page of search results via GitHub GraphQL API."""
    cmd = ["gh", "api", "graphql", "-f", f"query={GRAPHQL_SEARCH_QUERY}", "-F", f"query={query_str}"]
    if cursor:
        cmd.extend(["-F", f"cursor={cursor}"])

    code, stdout, stderr = run_gh_cmd(cmd, cwd=cwd)
    if code != 0 or not stdout:
        return 0, [], False, None

    try:
        data = json.loads(stdout)
        search_data = data.get("data", {}).get("search", {})
        total_count = search_data.get("issueCount", 0)
        page_info = search_data.get("pageInfo", {})
        nodes = search_data.get("nodes", [])
        return total_count, nodes, page_info.get("hasNextPage", False), page_info.get("endCursor")
    except json.JSONDecodeError:
        return 0, [], False, None


def fetch_all_nodes_in_window(
    target: str,
    user_a: str,
    start_date: str,
    end_date: str,
    cwd: Optional[Path] = None,
    depth: int = 0,
) -> List[Dict[str, Any]]:
    """Fetches all items in a date window, bisecting if results approach the 1,000 cap."""
    query_str = build_search_query(target, user_a, start_date, end_date)
    total_count, first_page_nodes, has_next, cursor = fetch_graphql_page(query_str, cwd=cwd)

    # If result count exceeds 900 and date window is > 1 day, bisect to avoid 1000-cap truncation
    try:
        d_start = datetime.fromisoformat(start_date)
        d_end = datetime.fromisoformat(end_date)
        days_span = (d_end - d_start).days
    except ValueError:
        days_span = 0

    if total_count >= 900 and days_span > 1 and depth < 6:
        mid_date = (d_start + timedelta(days=days_span // 2)).strftime("%Y-%m-%d")
        nodes_left = fetch_all_nodes_in_window(target, user_a, start_date, mid_date, cwd=cwd, depth=depth + 1)
        nodes_right = fetch_all_nodes_in_window(target, user_a, mid_date, end_date, cwd=cwd, depth=depth + 1)

        # Deduplicate by number and typename
        seen_keys: Set[Tuple[str, int]] = set()
        deduped: List[Dict[str, Any]] = []
        for n in nodes_left + nodes_right:
            key = (n.get("__typename", ""), n.get("number", 0))
            if key not in seen_keys:
                seen_keys.add(key)
                deduped.append(n)
        return deduped

    # Collect remaining pages in current window
    all_nodes = list(first_page_nodes)
    while has_next and cursor:
        _, next_nodes, has_next, cursor = fetch_graphql_page(query_str, cursor=cursor, cwd=cwd)
        all_nodes.extend(next_nodes)

    return all_nodes


def parse_thread_node(
    node: Dict[str, Any],
    user_a: str,
    user_b: Optional[str] = None,
) -> Optional[CollaborativeThread]:
    """Parses a GraphQL search node and extracts user interactions."""
    typename = node.get("__typename", "")
    is_pr = typename == "PullRequest"
    number = node.get("number", 0)
    title = node.get("title", "")
    url = node.get("url", "")
    created_at = node.get("createdAt", "")
    updated_at = node.get("updatedAt", "")
    closed_at = node.get("closedAt")
    merged_at = node.get("mergedAt") if is_pr else None

    raw_state = node.get("state", "OPEN")
    if is_pr and merged_at:
        state = "MERGED"
    else:
        state = raw_state

    author_dict = node.get("author") or {}
    author_login = author_dict.get("login", "")

    participants: Set[str] = set()
    if author_login:
        participants.add(author_login.lower())

    # User activities
    activity_a = UserActivity(user=user_a, is_author=(author_login.lower() == user_a.lower()))
    activity_b = UserActivity(user=user_b, is_author=(author_login.lower() == user_b.lower())) if user_b else None

    # Parse standard comments
    raw_comments = node.get("comments", {}).get("nodes", []) or []
    for c in raw_comments:
        c_author = (c.get("author") or {}).get("login", "")
        if c_author:
            c_author_lower = c_author.lower()
            participants.add(c_author_lower)
            if c_author_lower == user_a.lower():
                activity_a.comments.append(c)
            elif user_b and c_author_lower == user_b.lower():
                activity_b.comments.append(c)

    # Parse PR reviews and inline review comments
    if is_pr:
        raw_reviews = node.get("reviews", {}).get("nodes", []) or []
        for r in raw_reviews:
            r_author = (r.get("author") or {}).get("login", "")
            if r_author:
                r_author_lower = r_author.lower()
                participants.add(r_author_lower)
                if r_author_lower == user_a.lower():
                    activity_a.reviews.append(r)
                elif user_b and r_author_lower == user_b.lower():
                    activity_b.reviews.append(r)

            # Inline review comments inside the review
            r_comments = r.get("comments", {}).get("nodes", []) or []
            for rc in r_comments:
                rc_author = (rc.get("author") or {}).get("login", "")
                if rc_author:
                    rc_author_lower = rc_author.lower()
                    participants.add(rc_author_lower)
                    if rc_author_lower == user_a.lower():
                        activity_a.review_comments.append(rc)
                    elif user_b and rc_author_lower == user_b.lower():
                        activity_b.review_comments.append(rc)

    # If user_b is requested, both user_a and user_b must be in participants
    if user_b:
        if user_a.lower() not in participants or user_b.lower() not in participants:
            return None

    return CollaborativeThread(
        number=number,
        title=title,
        type="PR" if is_pr else "Issue",
        state=state,
        url=url,
        created_at=created_at,
        updated_at=updated_at,
        closed_at=closed_at,
        merged_at=merged_at,
        author=author_login,
        participants=participants,
        user_a_activity=activity_a,
        user_b_activity=activity_b,
    )


def extract_threads(
    nodes: List[Dict[str, Any]],
    user_a: str,
    user_b: Optional[str] = None,
) -> List[CollaborativeThread]:
    """Filters raw nodes into CollaborativeThread items."""
    results: List[CollaborativeThread] = []
    seen_keys: Set[Tuple[str, int]] = set()

    for node in nodes:
        key = (node.get("__typename", ""), node.get("number", 0))
        if key in seen_keys:
            continue
        seen_keys.add(key)

        thread = parse_thread_node(node, user_a=user_a, user_b=user_b)
        if thread:
            results.append(thread)

    # Sort descending by updated_at
    results.sort(key=lambda t: t.updated_at, reverse=True)
    return results


def generate_markdown_report(
    threads: List[CollaborativeThread],
    target: str,
    user_a: str,
    user_b: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> str:
    """Renders a comprehensive, structured markdown digest of collaborative interactions."""
    lines: List[str] = []
    title_pair = f"{user_a} & {user_b}" if user_b else user_a
    date_str = f" ({since} to {until})" if (since and until) else ""

    lines.append(f"# GitHub Collaboration Report: {title_pair}")
    lines.append(f"**Target**: `{target}`{date_str}\n")

    # Metrics summary
    total_threads = len(threads)
    total_prs = sum(1 for t in threads if t.type == "PR")
    merged_prs = sum(1 for t in threads if t.type == "PR" and t.state == "MERGED")
    open_prs = sum(1 for t in threads if t.type == "PR" and t.state == "OPEN")
    total_issues = sum(1 for t in threads if t.type == "Issue")

    lines.append("## Overview Statistics")
    lines.append(f"- **Total Collaborative Threads**: {total_threads}")
    lines.append(f"- **Pull Requests**: {total_prs} (Merged: {merged_prs}, Open: {open_prs})")
    lines.append(f"- **Issues**: {total_issues}\n")
    lines.append("---")

    # Categorize
    prs = [t for t in threads if t.type == "PR"]
    issues = [t for t in threads if t.type == "Issue"]

    if prs:
        lines.append("\n## Pull Request Collaborations")
        for t in prs:
            state_badge = f"[{t.state}]"
            lines.append(f"\n### PR #{t.number}: {t.title} {state_badge}")
            lines.append(f"- **URL**: {t.url}")
            lines.append(f"- **Author**: @{t.author} | **Updated**: {t.updated_at[:10]}")

            # Summarize activities
            if t.user_a_activity:
                act = t.user_a_activity
                details = []
                if act.is_author:
                    details.append("Author")
                if act.reviews:
                    states = [r.get("state", "") for r in act.reviews]
                    details.append(f"{len(act.reviews)} review(s) ({', '.join(states)})")
                if act.comments or act.review_comments:
                    total_c = len(act.comments) + len(act.review_comments)
                    details.append(f"{total_c} comment(s)")
                lines.append(f"- **@{user_a}**: {', '.join(details) if details else 'Participant'}")

            if t.user_b_activity and user_b:
                act = t.user_b_activity
                details = []
                if act.is_author:
                    details.append("Author")
                if act.reviews:
                    states = [r.get("state", "") for r in act.reviews]
                    details.append(f"{len(act.reviews)} review(s) ({', '.join(states)})")
                if act.comments or act.review_comments:
                    total_c = len(act.comments) + len(act.review_comments)
                    details.append(f"{total_c} comment(s)")
                lines.append(f"- **@{user_b}**: {', '.join(details) if details else 'Participant'}")

    if issues:
        lines.append("\n## Issue Discussions")
        for t in issues:
            state_badge = f"[{t.state}]"
            lines.append(f"\n### Issue #{t.number}: {t.title} {state_badge}")
            lines.append(f"- **URL**: {t.url}")
            lines.append(f"- **Author**: @{t.author} | **Updated**: {t.updated_at[:10]}")

            if t.user_a_activity and t.user_a_activity.comments:
                lines.append(f"- **@{user_a}**: {len(t.user_a_activity.comments)} comment(s)")
            if t.user_b_activity and user_b and t.user_b_activity.comments:
                lines.append(f"- **@{user_b}**: {len(t.user_b_activity.comments)} comment(s)")

    return "\n".join(lines)


def partition_into_chunks(content: str, max_chunk_kb: int = 120) -> List[str]:
    """Partitions markdown report along thread boundaries into chunks <= max_chunk_kb."""
    max_bytes = max_chunk_kb * 1024
    if len(content.encode("utf-8")) <= max_bytes:
        return [content]

    # Split on H3 headers (### PR # or ### Issue #)
    sections = re.split(r"(?=\n### (?:PR|Issue) #)", content)
    header = sections[0]
    items = sections[1:] if len(sections) > 1 else []

    chunks: List[str] = []
    current_chunk: List[str] = [header]
    current_size = len(header.encode("utf-8"))

    for item in items:
        item_size = len(item.encode("utf-8"))
        if current_size + item_size > max_bytes and len(current_chunk) > 1:
            chunks.append("".join(current_chunk))
            current_chunk = [header, item]
            current_size = len(header.encode("utf-8")) + item_size
        else:
            current_chunk.append(item)
            current_size += item_size

    if current_chunk:
        chunks.append("".join(current_chunk))

    return chunks
