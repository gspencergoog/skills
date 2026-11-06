"""Unit tests for diff_events.py."""

from __future__ import annotations

import unittest

from diff_events import (
    DiffContext,
    diff_reviewer_snapshots,
    diff_snapshots,
    filter_events,
    is_green,
    is_terminal,
    snapshot_thread_watermarks,
    summarize,
)
from models import ReviewerEventKind
from testing import make_check, make_snapshot, make_thread, make_viewer_review


class TestDiffEvents(unittest.TestCase):
    def test_initial_observation_surfaces_existing_problems_only(self) -> None:
        healthy = make_snapshot()
        self.assertEqual(diff_snapshots(None, healthy, DiffContext(viewer="alice")), [])

        broken = make_snapshot(
            mergeable="CONFLICTING",
            checks=[
                make_check(
                    name="test-web", status="COMPLETED", conclusion="FAILURE", required=True
                ),
                make_check(
                    name="python-ci", status="COMPLETED", conclusion="FAILURE", required=False
                ),
                make_check(
                    name="cancelled-job", status="COMPLETED", conclusion="CANCELLED", required=False
                ),
            ],
            threads=[
                make_thread(
                    thread_id="t_bot", last_author="gemini-code-assist", last_author_is_bot=True
                ),
                make_thread(thread_id="t_human", last_author="bob", last_author_is_bot=False),
                make_thread(thread_id="t_self", last_author="alice", last_author_is_bot=False),
            ],
        )
        events = diff_snapshots(None, broken, DiffContext(viewer="alice"))
        kinds = [e.kind for e in events]
        self.assertEqual(kinds, ["ci_check", "ci_check", "conflicts", "human_threads"])
        self.assertTrue(all(e.actionable for e in events))
        thread_ev = next(e for e in events if e.kind == "human_threads")
        self.assertEqual(thread_ev.detail["thread_ids"], ["t_bot", "t_human"])

    def test_start_transitions_produce_no_events_and_completion_fires(self) -> None:
        ctx = DiffContext(viewer="alice")
        s1 = make_snapshot(checks=[make_check(name="test-web", status="QUEUED", conclusion=None)])
        s2 = make_snapshot(
            checks=[make_check(name="test-web", status="IN_PROGRESS", conclusion=None)]
        )
        self.assertEqual(diff_snapshots(s1, s2, ctx), [])

        s3 = make_snapshot(
            checks=[make_check(name="test-web", status="COMPLETED", conclusion="FAILURE")]
        )
        evs = diff_snapshots(s2, s3, ctx)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].kind, "ci_check")
        self.assertTrue(evs[0].actionable)
        self.assertEqual(evs[0].suggested_action, "diagnose_ci")

        # All required checks green emits ALL_GREEN.
        s4 = make_snapshot(
            review_decision="APPROVED",
            checks=[make_check(name="test-web", status="COMPLETED", conclusion="SUCCESS")],
        )
        evs_green = diff_snapshots(s3, s4, ctx)
        all_green_ev = next(e for e in evs_green if e.to_value == "ALL_GREEN")
        self.assertEqual(all_green_ev.suggested_action, "celebrate")

    def test_rerun_suggestion_on_new_head_when_previous_passed(self) -> None:
        prev = make_snapshot(
            head_sha="sha1",
            checks=[make_check(name="test-web", status="COMPLETED", conclusion="SUCCESS")],
        )
        cur = make_snapshot(
            head_sha="sha2",
            checks=[make_check(name="test-web", status="COMPLETED", conclusion="FAILURE")],
        )
        ctx = DiffContext(viewer="alice", pushed_shas={"acme/widget#3339": "sha2"})
        evs = diff_snapshots(prev, cur, ctx)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].suggested_action, "rerun_ci")

        # Once a rerun was recorded for test-web@sha2, suggest diagnose_ci instead.
        ctx.reruns["acme/widget#3339"] = ["test-web@sha2"]
        evs_after = diff_snapshots(prev, cur, ctx)
        self.assertEqual(evs_after[0].suggested_action, "diagnose_ci")
        self.assertTrue(evs_after[0].detail["rerun_attempted"])

    def test_unknown_merge_state_suppressed_and_self_push_suppressed(self) -> None:
        s_clean = make_snapshot(mergeable="MERGEABLE", merge_state="BLOCKED", head_sha="sha1")
        s_unknown = make_snapshot(mergeable="UNKNOWN", merge_state="UNKNOWN", head_sha="sha2")
        ctx = DiffContext(viewer="alice", pushed_shas={"acme/widget#3339": "sha2"})
        # Pushed by agent + UNKNOWN merge_state -> zero events.
        self.assertEqual(diff_snapshots(s_clean, s_unknown, ctx), [])
        # UNKNOWN -> BLOCKED is also suppressed.
        s_blocked = make_snapshot(mergeable="MERGEABLE", merge_state="BLOCKED", head_sha="sha2")
        self.assertEqual(diff_snapshots(s_unknown, s_blocked, ctx), [])
        # UNKNOWN -> BEHIND is reported.
        s_behind = make_snapshot(mergeable="MERGEABLE", merge_state="BEHIND", head_sha="sha2")
        evs = diff_snapshots(s_unknown, s_behind, ctx)
        self.assertEqual([e.kind for e in evs], ["merge_state"])
        self.assertEqual(evs[0].suggested_action, "update_branch")

        # Push by collaborator is reported.
        s_collab = make_snapshot(head_sha="sha3", last_committer="bob")
        evs_push = diff_snapshots(s_blocked, s_collab, ctx)
        self.assertEqual([e.kind for e in evs_push], ["head_push"])

    def test_thread_refires_on_new_human_reply(self) -> None:
        t1 = make_thread(thread_id="t1", last_author="bob", last_comment_at="2026-10-27T21:00:00Z")
        s1 = make_snapshot(threads=[t1])
        watermarks = snapshot_thread_watermarks(s1, viewer="alice")
        ctx = DiffContext(viewer="alice", reported_threads={s1.key: watermarks})
        # Same timestamp -> no event.
        self.assertEqual(diff_snapshots(s1, s1, ctx), [])

        # New human reply with newer timestamp -> fires again.
        t1_reply = make_thread(
            thread_id="t1", last_author="bob", last_comment_at="2026-10-27T21:10:00Z"
        )
        s2 = make_snapshot(threads=[t1_reply])
        evs = diff_snapshots(s1, s2, ctx)
        self.assertEqual([e.kind for e in evs], ["human_threads"])

        # Review bot comment (e.g. gemini-code-assist) also fires.
        t1_bot = make_thread(
            thread_id="t1",
            last_author="gemini-code-assist",
            last_author_is_bot=True,
            last_comment_at="2026-10-27T21:15:00Z",
        )
        s3 = make_snapshot(threads=[t1_bot])
        self.assertEqual([e.kind for e in diff_snapshots(s1, s3, ctx)], ["human_threads"])

        # Viewer's own reply -> does not fire.
        t1_self = make_thread(
            thread_id="t1",
            last_author="alice",
            last_author_is_bot=False,
            last_comment_at="2026-10-27T21:20:00Z",
        )
        s4 = make_snapshot(threads=[t1_self])
        self.assertEqual(diff_snapshots(s1, s4, ctx), [])

    def test_remaining_detectors_and_terminal_conditions(self) -> None:
        s1 = make_snapshot(
            approvals=["carol"],
            head_sha="sha1",
            stack_parent="acme/widget#3338",
            stack_parent_state="OPEN",
        )
        s2 = make_snapshot(
            state="MERGED",
            draft=True,
            base_ref="release",
            head_sha="sha2",
            last_committer="alice",
            review_decision="CHANGES_REQUESTED",
            changes_requested=["bob"],
            approvals=[],
            requested_reviewers=["dave"],
            mergeable="CONFLICTING",
            stack_parent="acme/widget#3338",
            stack_parent_state="MERGED",
            auto_merge=True,
        )
        evs = diff_snapshots(s1, s2, DiffContext(viewer="alice"))
        kinds = {e.kind for e in evs}
        self.assertTrue(
            {
                "terminal",
                "draft",
                "base_change",
                "review_decision",
                "approval_dismissed",
                "review_requests",
                "conflicts",
                "stack_parent",
                "auto_merge",
            }.issubset(kinds)
        )
        self.assertEqual(len(filter_events(evs, {"conflicts"})), 1)
        term_ev = next(e for e in evs if e.kind == "terminal")
        self.assertTrue(term_ev.actionable)
        self.assertEqual(term_ev.suggested_action, "celebrate")
        self.assertIn("widget#3339:", summarize(evs))

        # is_green with null reviewDecision and null rollup_state.
        no_rules_pr = make_snapshot(
            mergeable="MERGEABLE",
            merge_state="HAS_HOOKS",
            review_decision=None,
            rollup_state=None,
            checks=[],
        )
        self.assertTrue(is_green(no_rules_pr))
        self.assertTrue(is_terminal({no_rules_pr.key: no_rules_pr}, "all-green"))
        self.assertFalse(is_terminal({no_rules_pr.key: no_rules_pr}, "merged"))

    def test_is_terminal_none_vs_default(self) -> None:
        merged_pr = make_snapshot(state="MERGED")
        closed_pr = make_snapshot(number=3340, state="CLOSED")
        snaps = {merged_pr.key: merged_pr, closed_pr.key: closed_pr}

        self.assertFalse(is_terminal({}, None))
        self.assertFalse(is_terminal({}, "none"))
        self.assertFalse(is_terminal(snaps, "none"))
        self.assertFalse(is_terminal(snaps, " NONE "))
        self.assertTrue(is_terminal(snaps, None))
        self.assertTrue(is_terminal(snaps, "closed"))

    def test_reviewer_initial_observation_states(self) -> None:
        ctx = DiffContext(viewer="bob")

        # 1. Initial unreviewed open PR -> new_review_request
        unreviewed = make_snapshot(mine=False, requested_reviewers=["bob"], viewer_last_review=None)
        evs = diff_reviewer_snapshots(None, unreviewed, ctx)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].kind, ReviewerEventKind.NEW_REVIEW_REQUEST.value)
        self.assertIsNone(evs[0].from_value)
        self.assertEqual(evs[0].to_value, "REQUESTED")
        self.assertTrue(evs[0].actionable)
        self.assertEqual(evs[0].suggested_action, "review_pr")
        self.assertEqual(evs[0].summary, "review requested")
        self.assertEqual(evs[0].detail, {"author": "alice", "head_sha": "aaa1111"})

        # 2. Initial WAITING_ON_AUTHOR, APPROVED, draft, MERGED, CLOSED -> quiet
        waiting = make_snapshot(
            mine=False,
            head_sha="aaa1111",
            requested_reviewers=[],
            viewer_last_review=make_viewer_review(state="COMMENTED", commit_oid="aaa1111"),
        )
        approved = make_snapshot(
            mine=False,
            requested_reviewers=[],
            approvals=["bob"],
            viewer_opinionated_state="APPROVED",
            viewer_last_review=make_viewer_review(state="APPROVED", commit_oid="aaa1111"),
        )
        draft_pr = make_snapshot(mine=False, draft=True, viewer_last_review=None)
        merged_pr = make_snapshot(mine=False, state="MERGED")
        closed_pr = make_snapshot(mine=False, state="CLOSED")
        for quiet_snap in (waiting, approved, draft_pr, merged_pr, closed_pr):
            self.assertEqual(diff_reviewer_snapshots(None, quiet_snap, ctx), [])

        # 3a. Initial RE_REVIEW_NEEDED via direct re-request
        re_req = make_snapshot(
            mine=False,
            head_sha="aaa1111",
            requested_reviewers=["bob"],
            viewer_last_review=make_viewer_review(state="COMMENTED", commit_oid="aaa1111"),
        )
        evs_re_req = diff_reviewer_snapshots(None, re_req, ctx)
        self.assertEqual(
            [e.kind for e in evs_re_req], [ReviewerEventKind.RE_REQUESTED_REVIEW.value]
        )
        self.assertTrue(evs_re_req[0].actionable)
        self.assertEqual(evs_re_req[0].suggested_action, "review_pr")

        # 3b. Initial RE_REVIEW_NEEDED via post-review head push
        pushed = make_snapshot(
            mine=False,
            head_sha="bbb2222",
            requested_reviewers=[],
            viewer_last_review=make_viewer_review(state="COMMENTED", commit_oid="aaa1111"),
        )
        evs_push = diff_reviewer_snapshots(None, pushed, ctx)
        self.assertEqual([e.kind for e in evs_push], [ReviewerEventKind.AUTHOR_HEAD_PUSH.value])
        self.assertTrue(evs_push[0].actionable)
        self.assertEqual(evs_push[0].detail["reviewed_sha"], "aaa1111")

        # 3c. Initial RE_REVIEW_NEEDED via thread reply -> fires once, suppressed when watermarked
        replied = make_snapshot(
            mine=False,
            head_sha="aaa1111",
            requested_reviewers=[],
            viewer_last_review=make_viewer_review(
                state="CHANGES_REQUESTED",
                submitted_at="2026-10-27T21:00:00Z",
                commit_oid="aaa1111",
            ),
            threads=[
                make_thread(
                    thread_id="t1",
                    last_author="alice",
                    last_comment_at="2026-10-27T21:05:00Z",
                )
            ],
        )
        evs_reply = diff_reviewer_snapshots(None, replied, ctx)
        self.assertEqual([e.kind for e in evs_reply], [ReviewerEventKind.AUTHOR_THREAD_REPLY.value])
        self.assertEqual(evs_reply[0].detail["thread_ids"], ["t1"])

        watermarked_ctx = DiffContext(
            viewer="bob",
            reported_threads={replied.key: {"t1": "2026-10-27T21:05:00Z"}},
        )
        self.assertEqual(diff_reviewer_snapshots(None, replied, watermarked_ctx), [])

    def test_reviewer_draft_rework_muting_and_ready_promotion(self) -> None:
        ctx = DiffContext(viewer="bob")
        reviewed_open = make_snapshot(
            mine=False,
            draft=False,
            head_sha="aaa1111",
            requested_reviewers=[],
            viewer_last_review=make_viewer_review(
                state="CHANGES_REQUESTED",
                submitted_at="2026-10-27T21:00:00Z",
                commit_oid="aaa1111",
            ),
        )
        # Author converts PR to draft and pushes a new commit + replies -> muted while in draft
        draft_rework = make_snapshot(
            mine=False,
            draft=True,
            head_sha="bbb2222",
            requested_reviewers=[],
            viewer_last_review=reviewed_open.viewer_last_review,
            threads=[
                make_thread(
                    thread_id="t1",
                    last_author="alice",
                    last_comment_at="2026-10-27T21:05:00Z",
                )
            ],
        )
        self.assertEqual(diff_reviewer_snapshots(reviewed_open, draft_rework, ctx), [])

        # Author marks draft ready for review -> draft_ready (actionable because RE_REVIEW_NEEDED)
        ready_rework = make_snapshot(
            mine=False,
            draft=False,
            head_sha="bbb2222",
            requested_reviewers=[],
            viewer_last_review=reviewed_open.viewer_last_review,
        )
        evs_ready = diff_reviewer_snapshots(draft_rework, ready_rework, ctx)
        self.assertEqual([e.kind for e in evs_ready], [ReviewerEventKind.DRAFT_READY.value])
        self.assertTrue(evs_ready[0].actionable)
        self.assertEqual(evs_ready[0].suggested_action, "review_pr")
        self.assertEqual(evs_ready[0].summary, "ready for review")

        # Draft -> Ready when WAITING_ON_AUTHOR is non-actionable
        draft_waiting = make_snapshot(
            mine=False,
            draft=True,
            head_sha="aaa1111",
            requested_reviewers=[],
            viewer_last_review=reviewed_open.viewer_last_review,
        )
        ready_waiting = make_snapshot(
            mine=False,
            draft=False,
            head_sha="aaa1111",
            requested_reviewers=[],
            viewer_last_review=reviewed_open.viewer_last_review,
        )
        evs_waiting = diff_reviewer_snapshots(draft_waiting, ready_waiting, ctx)
        self.assertEqual([e.kind for e in evs_waiting], [ReviewerEventKind.DRAFT_READY.value])
        self.assertFalse(evs_waiting[0].actionable)
        self.assertIsNone(evs_waiting[0].suggested_action)

    def test_reviewer_head_push_and_post_approval_commit_suppression(self) -> None:
        ctx = DiffContext(viewer="bob")
        prev = make_snapshot(
            mine=False,
            head_sha="aaa1111",
            requested_reviewers=[],
            viewer_last_review=make_viewer_review(state="COMMENTED", commit_oid="aaa1111"),
        )
        cur = make_snapshot(
            mine=False,
            head_sha="bbb2222",
            requested_reviewers=[],
            viewer_last_review=make_viewer_review(state="COMMENTED", commit_oid="aaa1111"),
        )
        evs = diff_reviewer_snapshots(prev, cur, ctx)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].kind, ReviewerEventKind.AUTHOR_HEAD_PUSH.value)
        self.assertEqual(evs[0].from_value, "aaa1111")
        self.assertEqual(evs[0].to_value, "bbb2222")
        self.assertTrue(evs[0].actionable)
        self.assertEqual(evs[0].suggested_action, "review_pr")
        self.assertEqual(evs[0].summary, "author pushed bbb2222")
        self.assertEqual(evs[0].detail, {"reviewed_sha": "aaa1111"})

        # Post-approval commit when approval stays intact -> APPROVED stays quiet!
        approved_prev = make_snapshot(
            mine=False,
            head_sha="aaa1111",
            requested_reviewers=["team:acme/core"],
            approvals=["bob"],
            viewer_opinionated_state="APPROVED",
            viewer_last_review=make_viewer_review(state="APPROVED", commit_oid="aaa1111"),
        )
        approved_pushed = make_snapshot(
            mine=False,
            head_sha="ccc3333",
            requested_reviewers=["team:acme/core"],
            approvals=["bob"],
            viewer_opinionated_state="APPROVED",
            viewer_last_review=make_viewer_review(state="APPROVED", commit_oid="aaa1111"),
        )
        self.assertEqual(diff_reviewer_snapshots(approved_prev, approved_pushed, ctx), [])

    def test_reviewer_thread_reply_and_watermark_deduplication(self) -> None:
        review = make_viewer_review(
            state="COMMENTED",
            submitted_at="2026-10-27T21:00:00Z",
            commit_oid="aaa1111",
        )
        s1 = make_snapshot(
            mine=False,
            head_sha="aaa1111",
            requested_reviewers=[],
            viewer_last_review=review,
            threads=[
                make_thread(
                    thread_id="t1",
                    last_author="bob",
                    last_comment_at="2026-10-27T21:00:00Z",
                )
            ],
        )
        s2 = make_snapshot(
            mine=False,
            head_sha="aaa1111",
            requested_reviewers=[],
            viewer_last_review=review,
            threads=[
                make_thread(
                    thread_id="t1",
                    last_author="alice",
                    last_comment_at="2026-10-27T21:05:00Z",
                ),
                make_thread(
                    thread_id="t_self",
                    last_author="bob",
                    last_comment_at="2026-10-27T21:06:00Z",
                ),
            ],
        )
        ctx = DiffContext(viewer="bob")
        evs = diff_reviewer_snapshots(s1, s2, ctx)
        self.assertEqual([e.kind for e in evs], [ReviewerEventKind.AUTHOR_THREAD_REPLY.value])
        self.assertTrue(evs[0].actionable)
        self.assertEqual(evs[0].suggested_action, "review_pr")
        self.assertEqual(evs[0].detail["thread_ids"], ["t1"])

        # Watermarked at 21:05 -> suppressed on next poll at same timestamp
        watermarks = snapshot_thread_watermarks(s2, viewer="bob")
        ctx_marked = DiffContext(viewer="bob", reported_threads={s2.key: watermarks})
        self.assertEqual(diff_reviewer_snapshots(s2, s2, ctx_marked), [])

        # Newer reply at 21:10 -> fires again
        s3 = make_snapshot(
            mine=False,
            head_sha="aaa1111",
            requested_reviewers=[],
            viewer_last_review=review,
            threads=[
                make_thread(
                    thread_id="t1",
                    last_author="alice",
                    last_comment_at="2026-10-27T21:10:00Z",
                )
            ],
        )
        evs_newer = diff_reviewer_snapshots(s2, s3, ctx_marked)
        self.assertEqual([e.kind for e in evs_newer], [ReviewerEventKind.AUTHOR_THREAD_REPLY.value])
        self.assertEqual(evs_newer[0].detail["thread_ids"], ["t1"])

    def test_reviewer_direct_vs_team_rerequests_and_approval_dismissed(self) -> None:
        ctx = DiffContext(viewer="bob")
        review = make_viewer_review(state="COMMENTED", commit_oid="aaa1111")
        s_waiting = make_snapshot(
            mine=False,
            head_sha="aaa1111",
            requested_reviewers=[],
            viewer_last_review=review,
        )
        # Team request added after viewer already reviewed -> does NOT trigger re_requested_review
        s_team = make_snapshot(
            mine=False,
            head_sha="aaa1111",
            requested_reviewers=["team:acme/core"],
            viewer_last_review=review,
        )
        self.assertEqual(diff_reviewer_snapshots(s_waiting, s_team, ctx), [])

        # Direct viewer request added -> triggers re_requested_review
        s_direct = make_snapshot(
            mine=False,
            head_sha="aaa1111",
            requested_reviewers=["team:acme/core", "Bob"],
            viewer_last_review=review,
        )
        evs_direct = diff_reviewer_snapshots(s_team, s_direct, ctx)
        self.assertEqual(
            [e.kind for e in evs_direct], [ReviewerEventKind.RE_REQUESTED_REVIEW.value]
        )
        self.assertTrue(evs_direct[0].actionable)
        self.assertEqual(evs_direct[0].suggested_action, "review_pr")

        # Approval dismissed on open PR -> actionable approval_dismissed
        s_approved = make_snapshot(
            mine=False,
            head_sha="aaa1111",
            requested_reviewers=[],
            approvals=["bob"],
            viewer_opinionated_state="APPROVED",
            viewer_last_review=make_viewer_review(state="APPROVED", commit_oid="aaa1111"),
        )
        s_dismissed = make_snapshot(
            mine=False,
            head_sha="aaa1111",
            requested_reviewers=[],
            approvals=[],
            viewer_opinionated_state="DISMISSED",
            viewer_last_review=make_viewer_review(state="DISMISSED", commit_oid="aaa1111"),
        )
        evs_dismissed = diff_reviewer_snapshots(s_approved, s_dismissed, ctx)
        self.assertEqual(
            [e.kind for e in evs_dismissed], [ReviewerEventKind.APPROVAL_DISMISSED.value]
        )
        self.assertTrue(evs_dismissed[0].actionable)
        self.assertEqual(evs_dismissed[0].suggested_action, "review_pr")

        # Approval dismissed when converted to draft -> non-actionable
        s_dismissed_draft = make_snapshot(
            mine=False,
            draft=True,
            head_sha="aaa1111",
            requested_reviewers=[],
            approvals=[],
            viewer_opinionated_state="DISMISSED",
            viewer_last_review=make_viewer_review(state="DISMISSED", commit_oid="aaa1111"),
        )
        evs_dismissed_draft = diff_reviewer_snapshots(s_approved, s_dismissed_draft, ctx)
        self.assertEqual(
            [e.kind for e in evs_dismissed_draft], [ReviewerEventKind.APPROVAL_DISMISSED.value]
        )
        self.assertFalse(evs_dismissed_draft[0].actionable)
        self.assertIsNone(evs_dismissed_draft[0].suggested_action)

    def test_reviewer_terminal_merged_and_closed_events(self) -> None:
        ctx = DiffContext(viewer="bob")
        s_open = make_snapshot(mine=False, state="OPEN", requested_reviewers=["bob"])
        s_merged = make_snapshot(mine=False, state="MERGED", requested_reviewers=["bob"])
        s_closed = make_snapshot(mine=False, state="CLOSED", requested_reviewers=["bob"])

        evs_merged = diff_reviewer_snapshots(s_open, s_merged, ctx)
        self.assertEqual(len(evs_merged), 1)
        self.assertEqual(evs_merged[0].kind, ReviewerEventKind.TERMINAL.value)
        self.assertEqual(evs_merged[0].from_value, "OPEN")
        self.assertEqual(evs_merged[0].to_value, "MERGED")
        self.assertFalse(evs_merged[0].actionable)
        self.assertIsNone(evs_merged[0].suggested_action)
        self.assertEqual(evs_merged[0].summary, "PR merged")

        evs_closed = diff_reviewer_snapshots(s_open, s_closed, ctx)
        self.assertEqual(len(evs_closed), 1)
        self.assertEqual(evs_closed[0].kind, ReviewerEventKind.TERMINAL.value)
        self.assertEqual(evs_closed[0].from_value, "OPEN")
        self.assertEqual(evs_closed[0].to_value, "CLOSED")
        self.assertFalse(evs_closed[0].actionable)
        self.assertIsNone(evs_closed[0].suggested_action)
        self.assertEqual(evs_closed[0].summary, "PR closed")

        # Reopened unreviewed PR transitions CLOSED -> OPEN and fires new_review_request
        evs_reopened = diff_reviewer_snapshots(s_closed, s_open, ctx)
        self.assertEqual(
            [e.kind for e in evs_reopened], [ReviewerEventKind.NEW_REVIEW_REQUEST.value]
        )

        # Reopened previously reviewed PR with direct request fires re_requested_review
        review = make_viewer_review(state="CHANGES_REQUESTED", commit_oid="aaa1111")
        s_closed_reviewed = make_snapshot(
            mine=False,
            state="CLOSED",
            requested_reviewers=["bob"],
            viewer_last_review=review,
        )
        s_open_reviewed = make_snapshot(
            mine=False,
            state="OPEN",
            requested_reviewers=["bob"],
            viewer_last_review=review,
        )
        evs_reopened_reviewed = diff_reviewer_snapshots(s_closed_reviewed, s_open_reviewed, ctx)
        self.assertEqual(
            [e.kind for e in evs_reopened_reviewed], [ReviewerEventKind.RE_REQUESTED_REVIEW.value]
        )

        # Pre-v8 CHANGES_REQUESTED snapshot with viewer_last_review=None does not emit new_review_request
        s_legacy_cr = make_snapshot(
            mine=False,
            requested_reviewers=[],
            viewer_last_review=None,
            viewer_opinionated_state="CHANGES_REQUESTED",
            changes_requested=["bob"],
        )
        self.assertEqual(diff_reviewer_snapshots(None, s_legacy_cr, ctx), [])


if __name__ == "__main__":
    unittest.main()
