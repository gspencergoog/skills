"""Normalizes raw GitHub GraphQL pull request nodes into PrSnapshot objects."""

from __future__ import annotations

from typing import Any

from models import CheckRun, PrSnapshot, Thread, is_bot, parse_pr_ref, pr_key

_STATUS_CONTEXT_MAP: dict[str, tuple[str, str | None]] = {
    "SUCCESS": ("COMPLETED", "SUCCESS"),
    "FAILURE": ("COMPLETED", "FAILURE"),
    "ERROR": ("COMPLETED", "ERROR"),
    "PENDING": ("IN_PROGRESS", None),
    "EXPECTED": ("IN_PROGRESS", None),
}


def _check_truncation(conn: dict[str, Any] | None, label: str, out: list[str]) -> list[Any]:
    if not isinstance(conn, dict):
        return []
    nodes = [n for n in (conn.get("nodes") or []) if isinstance(n, dict)]
    total = conn.get("totalCount")
    if isinstance(total, int) and total > len(nodes):
        out.append(label)
    return nodes


def _extract_run_id(node: dict[str, Any]) -> int | None:
    suite = node.get("checkSuite") if isinstance(node.get("checkSuite"), dict) else {}
    wf_run = suite.get("workflowRun") if isinstance(suite.get("workflowRun"), dict) else {}
    db_id = wf_run.get("databaseId")
    return int(db_id) if isinstance(db_id, int) else None


def _normalize_check_run(node: dict[str, Any]) -> CheckRun | None:
    name = str(node.get("name") or "").strip()
    if not name:
        return None
    return CheckRun(
        name=name,
        status=str(node.get("status") or "QUEUED"),
        conclusion=node.get("conclusion"),
        required=bool(node.get("isRequired", False)),
        url=node.get("detailsUrl"),
        run_id=_extract_run_id(node),
        started_at=node.get("startedAt"),
        completed_at=node.get("completedAt"),
    )


def _normalize_status_context(node: dict[str, Any]) -> CheckRun | None:
    name = str(node.get("context") or "").strip()
    if not name:
        return None
    raw_state = str(node.get("state") or "PENDING").upper()
    status, conclusion = _STATUS_CONTEXT_MAP.get(raw_state, ("COMPLETED", raw_state))
    created = node.get("createdAt")
    return CheckRun(
        name=name,
        status=status,
        conclusion=conclusion,
        required=bool(node.get("isRequired", False)),
        url=node.get("targetUrl"),
        run_id=None,
        started_at=created,
        completed_at=created if status == "COMPLETED" else None,
    )


def _normalize_check_node(node: dict[str, Any]) -> CheckRun | None:
    typename = node.get("__typename")
    if typename == "CheckRun":
        return _normalize_check_run(node)
    if typename == "StatusContext":
        return _normalize_status_context(node)
    return None


def normalize_checks(
    rollup: dict[str, Any] | None, truncated: list[str] | None = None
) -> list[CheckRun]:
    """Converts a ``statusCheckRollup`` object into deduplicated ``CheckRun`` items."""
    if not isinstance(rollup, dict):
        return []
    trunc_list = truncated if truncated is not None else []
    nodes = _check_truncation(rollup.get("contexts"), "contexts", trunc_list)
    by_name: dict[str, CheckRun] = {}
    for node in nodes:
        run = _normalize_check_node(node)
        if run is not None:
            by_name[run.name] = run
    return list(by_name.values())


def _extract_reviewers(raw: dict[str, Any], truncated: list[str]) -> list[str]:
    nodes = _check_truncation(raw.get("reviewRequests"), "reviewRequests", truncated)
    reviewers: list[str] = []
    for node in nodes:
        req = node.get("requestedReviewer")
        if not isinstance(req, dict):
            continue
        if req.get("login"):
            reviewers.append(str(req["login"]))
        elif req.get("slug"):
            reviewers.append(f"team:{req['slug']}")
    return sorted(set(reviewers))


def _extract_reviews(raw: dict[str, Any]) -> tuple[list[str], list[str]]:
    conn = raw.get("latestOpinionatedReviews")
    nodes = (conn.get("nodes") or []) if isinstance(conn, dict) else []
    approvals: set[str] = set()
    changes_req: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            continue
        author = node.get("author")
        login = str(author.get("login") or "") if isinstance(author, dict) else ""
        if not login:
            continue
        state = str(node.get("state") or "")
        if state == "APPROVED":
            approvals.add(login)
        elif state == "CHANGES_REQUESTED":
            changes_req.add(login)
    return sorted(approvals), sorted(changes_req)


def _extract_threads(raw: dict[str, Any], truncated: list[str]) -> list[Thread]:
    nodes = _check_truncation(raw.get("reviewThreads"), "reviewThreads", truncated)
    threads: list[Thread] = []
    for node in nodes:
        tid = str(node.get("id") or "")
        if not tid:
            continue
        comments_conn = node.get("comments")
        c_nodes = (comments_conn.get("nodes") or []) if isinstance(comments_conn, dict) else []
        last = c_nodes[-1] if c_nodes and isinstance(c_nodes[-1], dict) else {}
        author_obj = last.get("author") if isinstance(last.get("author"), dict) else None
        last_login = str((author_obj or {}).get("login") or "")
        threads.append(
            Thread(
                id=tid,
                resolved=bool(node.get("isResolved", False)),
                last_author=last_login,
                last_author_is_bot=is_bot(author_obj),
                last_comment_at=str(last.get("createdAt") or ""),
            )
        )
    return threads


def _extract_commit(
    raw: dict[str, Any], truncated: list[str]
) -> tuple[str | None, str | None, list[CheckRun]]:
    commits_conn = raw.get("commits")
    c_nodes = (commits_conn.get("nodes") or []) if isinstance(commits_conn, dict) else []
    if not c_nodes or not isinstance(c_nodes[-1], dict):
        return None, None, []
    commit = c_nodes[-1].get("commit")
    if not isinstance(commit, dict):
        return None, None, []
    committer = commit.get("committer") if isinstance(commit.get("committer"), dict) else {}
    user = committer.get("user") if isinstance(committer.get("user"), dict) else {}
    last_committer = str(user["login"]) if user.get("login") else None
    rollup = commit.get("statusCheckRollup")
    rollup_state = (
        str(rollup["state"]) if isinstance(rollup, dict) and rollup.get("state") else None
    )
    checks = normalize_checks(rollup if isinstance(rollup, dict) else None, truncated)
    return last_committer, rollup_state, checks


def _resolve_pr_state(raw: dict[str, Any]) -> str:
    raw_state = str(raw.get("state") or "OPEN").upper()
    if raw.get("merged") or raw_state == "MERGED":
        return "MERGED"
    if raw_state == "CLOSED":
        return "CLOSED"
    return "OPEN"


def _extract_assignees(raw: dict[str, Any]) -> list[str]:
    conn = raw.get("assignees")
    nodes = (conn.get("nodes") or []) if isinstance(conn, dict) else []
    return sorted({str(n["login"]) for n in nodes if isinstance(n, dict) and n.get("login")})


def normalize_pr(raw: dict[str, Any], viewer: str, repo: str | None = None) -> PrSnapshot:
    """Maps a GraphQL ``pullRequest`` node into a ``PrSnapshot``."""
    url = str(raw.get("url") or "")
    number = int(raw["number"])
    resolved_repo = repo or parse_pr_ref(url)[0]
    author_obj = raw.get("author") if isinstance(raw.get("author"), dict) else {}
    author = str(author_obj.get("login") or "")

    truncated: list[str] = []
    reviewers = _extract_reviewers(raw, truncated)
    approvals, changes_req = _extract_reviews(raw)
    threads = _extract_threads(raw, truncated)
    last_committer, rollup_state, checks = _extract_commit(raw, truncated)

    return PrSnapshot(
        key=pr_key(resolved_repo, number),
        number=number,
        repo=resolved_repo,
        title=str(raw.get("title") or ""),
        url=url,
        author=author,
        mine=bool(viewer and author == viewer),
        state=_resolve_pr_state(raw),
        draft=bool(raw.get("isDraft", False)),
        head_sha=str(raw.get("headRefOid") or ""),
        head_ref=str(raw.get("headRefName") or ""),
        base_ref=str(raw.get("baseRefName") or ""),
        last_committer=last_committer,
        rollup_state=rollup_state,
        mergeable=str(raw.get("mergeable") or "UNKNOWN"),
        merge_state=str(raw.get("mergeStateStatus") or "UNKNOWN"),
        review_decision=raw.get("reviewDecision"),
        requested_reviewers=reviewers,
        assignees=_extract_assignees(raw),
        approvals=approvals,
        changes_requested=changes_req,
        checks=checks,
        threads=threads,
        auto_merge=raw.get("autoMergeRequest") is not None,
        stack_parent=None,
        stack_parent_state=None,
        truncated=truncated,
        updated_at=str(raw.get("updatedAt") or ""),
    )


def resolve_stack_parents(snapshots: dict[str, PrSnapshot]) -> None:
    """Populates ``stack_parent`` and ``stack_parent_state`` across watched snapshots."""
    by_head: dict[tuple[str, str], PrSnapshot] = {
        (snap.repo, snap.head_ref): snap for snap in snapshots.values() if snap.head_ref
    }
    for snap in snapshots.values():
        parent = by_head.get((snap.repo, snap.base_ref))
        if parent is not None and parent.key != snap.key:
            snap.stack_parent = parent.key
            snap.stack_parent_state = parent.state
        else:
            snap.stack_parent = None
            snap.stack_parent_state = None
