"""Diffs two PrSnapshot objects into typed Event items and evaluates terminal conditions."""

from __future__ import annotations

from dataclasses import dataclass, field

from models import Event, PrSnapshot


@dataclass
class DiffContext:
    """Per-conversation context used to suppress self-triggered or duplicate events."""

    viewer: str = ""
    pushed_shas: dict[str, str] = field(default_factory=dict)
    reported_threads: dict[str, dict[str, str]] = field(default_factory=dict)
    reruns: dict[str, list[str]] = field(default_factory=dict)


def _check_rerun_attempted(ctx: DiffContext, pr: str, check_name: str, head_sha: str) -> bool:
    token = f"{check_name}@{head_sha}"
    return token in (ctx.reruns.get(pr) or [])


def _prev_check_passed(prev: PrSnapshot | None, check_name: str) -> bool:
    if prev is None:
        return False
    for check in prev.checks:
        if check.name == check_name:
            return check.is_completed() and check.conclusion == "SUCCESS"
    return False


def _unread_human_threads(snap: PrSnapshot, ctx: DiffContext) -> list[str]:
    reported = ctx.reported_threads.get(snap.key) or {}
    unread: list[str] = []
    for thread in snap.threads:
        if thread.resolved:
            continue
        if ctx.viewer and thread.last_author == ctx.viewer:
            continue
        prev_ts = reported.get(thread.id)
        if prev_ts is None or (thread.last_comment_at and thread.last_comment_at > prev_ts):
            unread.append(thread.id)
    return unread


def _diff_initial(cur: PrSnapshot, ctx: DiffContext) -> list[Event]:
    events: list[Event] = []
    for check in cur.effective_required_checks():
        if check.is_failed():
            rerun_done = _check_rerun_attempted(ctx, cur.key, check.name, cur.head_sha)
            events.append(
                Event(
                    pr=cur.key,
                    kind="ci_check",
                    from_value=None,
                    to_value=check.conclusion,
                    actionable=True,
                    suggested_action="diagnose_ci",
                    summary=f"CI failed ({check.name})",
                    detail={
                        "check": check.name,
                        "required": check.required,
                        "run_id": check.run_id,
                        "url": check.url,
                        "rerun_attempted": rerun_done,
                    },
                )
            )
    if cur.mergeable == "CONFLICTING":
        events.append(
            Event(
                pr=cur.key,
                kind="conflicts",
                from_value=None,
                to_value="CONFLICTING",
                actionable=True,
                suggested_action="resolve_conflicts",
                summary="merge conflicts detected",
                detail={},
            )
        )
    new_threads = _unread_human_threads(cur, ctx)
    if new_threads:
        events.append(
            Event(
                pr=cur.key,
                kind="human_threads",
                from_value=0,
                to_value=len(new_threads),
                actionable=True,
                suggested_action="pr_feedback_handler",
                summary=f"{len(new_threads)} unresolved review thread(s)",
                detail={"thread_ids": new_threads},
            )
        )
    return events


def _diff_terminal(prev: PrSnapshot, cur: PrSnapshot, _ctx: DiffContext) -> list[Event]:
    if prev.state == cur.state:
        return []
    return [
        Event(
            pr=cur.key,
            kind="terminal",
            from_value=prev.state,
            to_value=cur.state,
            actionable=False,
            suggested_action=None,
            summary=f"PR {cur.state.lower()}",
            detail={},
        )
    ]


def _diff_draft(prev: PrSnapshot, cur: PrSnapshot, _ctx: DiffContext) -> list[Event]:
    if prev.draft == cur.draft:
        return []
    label = "marked as draft" if cur.draft else "marked ready for review"
    return [
        Event(
            pr=cur.key,
            kind="draft",
            from_value=prev.draft,
            to_value=cur.draft,
            actionable=False,
            suggested_action=None,
            summary=label,
            detail={},
        )
    ]


def _diff_base_change(prev: PrSnapshot, cur: PrSnapshot, _ctx: DiffContext) -> list[Event]:
    if prev.base_ref == cur.base_ref:
        return []
    return [
        Event(
            pr=cur.key,
            kind="base_change",
            from_value=prev.base_ref,
            to_value=cur.base_ref,
            actionable=False,
            suggested_action=None,
            summary=f"base branch retargeted to {cur.base_ref}",
            detail={},
        )
    ]


def _diff_head_push(prev: PrSnapshot, cur: PrSnapshot, ctx: DiffContext) -> list[Event]:
    if prev.head_sha == cur.head_sha:
        return []
    if cur.head_sha == ctx.pushed_shas.get(cur.key):
        return []
    if ctx.viewer and cur.last_committer == ctx.viewer:
        return []
    by = f" by {cur.last_committer}" if cur.last_committer else ""
    return [
        Event(
            pr=cur.key,
            kind="head_push",
            from_value=prev.head_sha[:7],
            to_value=cur.head_sha[:7],
            actionable=False,
            suggested_action=None,
            summary=f"new head {cur.head_sha[:7]} pushed{by}",
            detail={"committer": cur.last_committer},
        )
    ]


def _diff_review_decision(prev: PrSnapshot, cur: PrSnapshot, _ctx: DiffContext) -> list[Event]:
    if prev.review_decision == cur.review_decision or cur.review_decision is None:
        return []
    decision = cur.review_decision
    if decision == "CHANGES_REQUESTED":
        by = f" by {', '.join(cur.changes_requested)}" if cur.changes_requested else ""
        return [
            Event(
                pr=cur.key,
                kind="review_decision",
                from_value=prev.review_decision,
                to_value=decision,
                actionable=True,
                suggested_action="pr_feedback_handler",
                summary=f"changes requested{by}",
                detail={"reviewers": list(cur.changes_requested)},
            )
        ]
    action = "celebrate" if decision == "APPROVED" and cur.required_summary() == "SUCCESS" else None
    summary = "approved" if decision == "APPROVED" else f"review decision: {decision.lower()}"
    return [
        Event(
            pr=cur.key,
            kind="review_decision",
            from_value=prev.review_decision,
            to_value=decision,
            actionable=decision == "APPROVED",
            suggested_action=action,
            summary=summary,
            detail={"approvals": list(cur.approvals)},
        )
    ]


def _diff_approvals_dismissed(prev: PrSnapshot, cur: PrSnapshot, _ctx: DiffContext) -> list[Event]:
    if prev.head_sha == cur.head_sha:
        return []
    dismissed = sorted(set(prev.approvals) - set(cur.approvals) - set(cur.changes_requested))
    if not dismissed:
        return []
    return [
        Event(
            pr=cur.key,
            kind="approval_dismissed",
            from_value=prev.approvals,
            to_value=cur.approvals,
            actionable=False,
            suggested_action=None,
            summary=f"approval dismissed ({', '.join(dismissed)})",
            detail={"dismissed": dismissed},
        )
    ]


def _diff_review_requests(prev: PrSnapshot, cur: PrSnapshot, _ctx: DiffContext) -> list[Event]:
    if prev.requested_reviewers == cur.requested_reviewers and prev.assignees == cur.assignees:
        return []
    return [
        Event(
            pr=cur.key,
            kind="review_requests",
            from_value={"reviewers": prev.requested_reviewers, "assignees": prev.assignees},
            to_value={"reviewers": cur.requested_reviewers, "assignees": cur.assignees},
            actionable=False,
            suggested_action=None,
            summary="reviewers or assignees updated",
            detail={},
        )
    ]


def _diff_threads(prev: PrSnapshot, cur: PrSnapshot, ctx: DiffContext) -> list[Event]:
    prev_map = {t.id: t.last_comment_at for t in prev.threads}
    reported = ctx.reported_threads.get(cur.key) or {}
    baseline = {**prev_map, **reported}
    triggered: list[str] = []
    for thread in cur.threads:
        if thread.resolved:
            continue
        if ctx.viewer and thread.last_author == ctx.viewer:
            continue
        seen_at = baseline.get(thread.id)
        if seen_at is None or (thread.last_comment_at and thread.last_comment_at > seen_at):
            triggered.append(thread.id)
    if not triggered:
        return []
    return [
        Event(
            pr=cur.key,
            kind="human_threads",
            from_value=len(prev.threads),
            to_value=len(cur.threads),
            actionable=True,
            suggested_action="pr_feedback_handler",
            summary=f"{len(triggered)} new review comment thread(s)",
            detail={"thread_ids": triggered},
        )
    ]


def _diff_conflicts(prev: PrSnapshot, cur: PrSnapshot, _ctx: DiffContext) -> list[Event]:
    if prev.mergeable == cur.mergeable or cur.mergeable != "CONFLICTING":
        return []
    return [
        Event(
            pr=cur.key,
            kind="conflicts",
            from_value=prev.mergeable,
            to_value="CONFLICTING",
            actionable=True,
            suggested_action="resolve_conflicts",
            summary="merge conflicts detected",
            detail={},
        )
    ]


def _diff_merge_state(prev: PrSnapshot, cur: PrSnapshot, _ctx: DiffContext) -> list[Event]:
    if prev.merge_state == cur.merge_state:
        return []
    if cur.merge_state == "UNKNOWN":
        return []
    if prev.merge_state == "UNKNOWN" and cur.merge_state not in ("DIRTY", "CONFLICTING", "BEHIND"):
        return []
    actionable = cur.merge_state in ("BEHIND", "DIRTY")
    action: str | None = None
    if cur.merge_state == "BEHIND":
        action = "update_branch"
    elif cur.merge_state == "DIRTY":
        action = "resolve_conflicts"
    return [
        Event(
            pr=cur.key,
            kind="merge_state",
            from_value=prev.merge_state,
            to_value=cur.merge_state,
            actionable=actionable,
            suggested_action=action,
            summary=f"merge state: {cur.merge_state.lower()}",
            detail={},
        )
    ]


def _diff_stack_parent(prev: PrSnapshot, cur: PrSnapshot, _ctx: DiffContext) -> list[Event]:
    if prev.stack_parent_state == cur.stack_parent_state or cur.stack_parent is None:
        return []
    if cur.stack_parent_state not in ("MERGED", "CLOSED"):
        return []
    action = "rebase_onto_base" if cur.stack_parent_state == "MERGED" else None
    return [
        Event(
            pr=cur.key,
            kind="stack_parent",
            from_value=prev.stack_parent_state,
            to_value=cur.stack_parent_state,
            actionable=cur.stack_parent_state == "MERGED",
            suggested_action=action,
            summary=f"stack parent {cur.stack_parent} {cur.stack_parent_state.lower()}",
            detail={"parent": cur.stack_parent},
        )
    ]


def _diff_auto_merge(prev: PrSnapshot, cur: PrSnapshot, _ctx: DiffContext) -> list[Event]:
    if prev.auto_merge == cur.auto_merge:
        return []
    label = "auto-merge enabled" if cur.auto_merge else "auto-merge disabled"
    return [
        Event(
            pr=cur.key,
            kind="auto_merge",
            from_value=prev.auto_merge,
            to_value=cur.auto_merge,
            actionable=False,
            suggested_action=None,
            summary=label,
            detail={},
        )
    ]


def _select_ci_action(
    prev: PrSnapshot,
    cur: PrSnapshot,
    check_name: str,
    failed: bool,
    is_req: bool,
    rerun_done: bool,
) -> str | None:
    if not (failed and is_req):
        return None
    same_head = prev.head_sha == cur.head_sha
    if not same_head and _prev_check_passed(prev, check_name) and not rerun_done:
        return "rerun_ci"
    return "diagnose_ci"


def _check_completion_event(
    prev: PrSnapshot,
    cur: PrSnapshot,
    ctx: DiffContext,
    check_name: str,
    required_names: set[str],
) -> Event | None:
    cur_check = next((c for c in cur.checks if c.name == check_name), None)
    if cur_check is None or not cur_check.is_completed():
        return None
    prev_check = next((c for c in prev.checks if c.name == check_name), None)
    same_head = prev.head_sha == cur.head_sha
    prev_done = prev_check is not None and prev_check.is_completed()
    if same_head and prev_done and prev_check.conclusion == cur_check.conclusion:
        return None
    failed = cur_check.is_failed()
    recovered = (
        same_head
        and prev_check is not None
        and prev_check.is_failed()
        and cur_check.conclusion == "SUCCESS"
    )
    if not failed and not recovered:
        return None
    is_req = cur_check.name in required_names
    rerun_done = _check_rerun_attempted(ctx, cur.key, cur_check.name, cur.head_sha)
    action = _select_ci_action(prev, cur, cur_check.name, failed, is_req, rerun_done)
    verb = "failed" if failed else f"completed ({cur_check.conclusion})"
    return Event(
        pr=cur.key,
        kind="ci_check",
        from_value=prev_check.conclusion if prev_done else "IN_PROGRESS",
        to_value=cur_check.conclusion,
        actionable=bool(failed and is_req),
        suggested_action=action,
        summary=f"CI {verb} ({cur_check.name})",
        detail={
            "check": cur_check.name,
            "required": cur_check.required,
            "run_id": cur_check.run_id,
            "url": cur_check.url,
            "rerun_attempted": rerun_done,
        },
    )


def _diff_checks(prev: PrSnapshot, cur: PrSnapshot, ctx: DiffContext) -> list[Event]:
    events: list[Event] = []
    required_names = {c.name for c in cur.effective_required_checks()}
    for check in cur.checks:
        ev = _check_completion_event(prev, cur, ctx, check.name, required_names)
        if ev is not None:
            events.append(ev)
    prev_summary = prev.required_summary()
    cur_summary = cur.required_summary()
    if cur_summary == "SUCCESS" and prev_summary != "SUCCESS":
        action = "celebrate" if cur.review_decision == "APPROVED" else None
        events.append(
            Event(
                pr=cur.key,
                kind="ci_check",
                from_value=prev_summary,
                to_value="ALL_GREEN",
                actionable=action is not None,
                suggested_action=action,
                summary="all required CI checks passed",
                detail={"required": True},
            )
        )
    return events


_DETECTORS = (
    _diff_terminal,
    _diff_draft,
    _diff_base_change,
    _diff_head_push,
    _diff_review_decision,
    _diff_approvals_dismissed,
    _diff_review_requests,
    _diff_threads,
    _diff_conflicts,
    _diff_merge_state,
    _diff_stack_parent,
    _diff_auto_merge,
    _diff_checks,
)


def diff_snapshots(prev: PrSnapshot | None, cur: PrSnapshot, ctx: DiffContext) -> list[Event]:
    """Computes ordered ``Event`` transitions between ``prev`` and ``cur``."""
    if prev is None:
        return _diff_initial(cur, ctx)
    events: list[Event] = []
    for detector in _DETECTORS:
        events.extend(detector(prev, cur, ctx))
    return events


def filter_events(events: list[Event], kinds: set[str] | None) -> list[Event]:
    """Filters ``events`` to ``kinds`` when provided."""
    if not kinds:
        return list(events)
    return [ev for ev in events if ev.kind in kinds]


def is_green(snap: PrSnapshot) -> bool:
    """Returns True when a PR is open, mergeable, approved (if required), and CI-green."""
    if snap.state != "OPEN" or snap.draft:
        return False
    if snap.merge_state == "CLEAN":
        return True
    if snap.mergeable != "MERGEABLE":
        return False
    if snap.review_decision not in ("APPROVED", None):
        return False
    req_sum = snap.required_summary()
    if req_sum in ("FAILURE", "PENDING"):
        return False
    return snap.rollup_state in ("SUCCESS", None)


def is_terminal(snapshots: dict[str, PrSnapshot], until: str | None = None) -> bool:
    """Checks whether all watched PRs satisfy the terminal condition."""
    if not snapshots:
        return False
    mode = (until or "closed").lower()
    if mode == "merged":
        return all(s.state == "MERGED" for s in snapshots.values())
    if mode == "all-green":
        return all(s.state == "MERGED" or is_green(s) for s in snapshots.values())
    return all(s.state in ("MERGED", "CLOSED") for s in snapshots.values())


def summarize(events: list[Event]) -> str:
    """Formats a single-line human summary grouping event summaries by PR."""
    if not events:
        return "no changes"
    by_pr: dict[str, list[str]] = {}
    for ev in events:
        short_pr = ev.pr.split("/", 1)[-1]
        by_pr.setdefault(short_pr, []).append(ev.summary)
    parts = [f"{pr}: {', '.join(items)}" for pr, items in by_pr.items()]
    return "; ".join(parts)


def snapshot_thread_watermarks(snap: PrSnapshot, viewer: str = "") -> dict[str, str]:
    """Returns ``{thread_id: last_comment_at}`` for unresolved non-viewer threads on ``snap``."""
    out: dict[str, str] = {}
    for thread in snap.threads:
        if thread.resolved:
            continue
        if viewer and thread.last_author == viewer:
            continue
        out[thread.id] = thread.last_comment_at
    return out
