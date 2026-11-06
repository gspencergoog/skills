"""Unit tests for models.py."""

from __future__ import annotations

import unittest

from models import (
    EVENT_KINDS,
    REVIEWER_EVENT_KINDS,
    SCHEMA_VERSION,
    CheckRun,
    Event,
    MonitorState,
    PrSnapshot,
    ReviewerEventKind,
    ReviewRole,
    ReviewSortTier,
    ReviewStep,
    Thread,
    ViewerReview,
    classify_review_turn,
    is_bot,
    parse_event_list,
    parse_pr_ref,
    pr_key,
)
from testing import make_check, make_snapshot, make_thread, make_viewer_review


class TestModels(unittest.TestCase):
    def test_parse_pr_ref_variants_and_errors(self) -> None:
        self.assertEqual(parse_pr_ref("acme/widget#3339"), ("acme/widget", 3339))
        self.assertEqual(
            parse_pr_ref("https://github.com/acme/widget/pull/3339/files"),
            ("acme/widget", 3339),
        )
        self.assertEqual(parse_pr_ref("3339", default_repo="acme/widget"), ("acme/widget", 3339))
        self.assertEqual(parse_pr_ref("#3339", default_repo="acme/widget"), ("acme/widget", 3339))
        self.assertEqual(pr_key("acme/widget", 3339), "acme/widget#3339")
        with self.assertRaises(ValueError):
            parse_pr_ref("3339")
        with self.assertRaises(ValueError):
            parse_pr_ref("not-a-pr", default_repo="acme/widget")

    def test_is_bot_detection(self) -> None:
        self.assertFalse(is_bot(None))
        self.assertFalse(is_bot({"login": "alice", "__typename": "User"}))
        self.assertTrue(is_bot({"login": "gemini-code-assist", "__typename": "Bot"}))
        self.assertTrue(is_bot({"login": "dependabot[bot]", "__typename": "User"}))

    def test_required_summary_states(self) -> None:
        empty = make_snapshot(checks=[])
        self.assertEqual(empty.required_summary(), "NONE")

        running = make_snapshot(
            checks=[
                make_check(name="req", status="IN_PROGRESS", conclusion=None, required=True),
                make_check(name="can", status="COMPLETED", conclusion="CANCELLED", required=False),
            ]
        )
        self.assertEqual(running.required_summary(), "PENDING")

        failed = make_snapshot(
            checks=[
                make_check(name="req", status="COMPLETED", conclusion="FAILURE", required=True),
                make_check(name="req2", status="IN_PROGRESS", conclusion=None, required=True),
            ]
        )
        self.assertEqual(failed.required_summary(), "FAILURE")

        green = make_snapshot(
            checks=[
                make_check(name="req", status="COMPLETED", conclusion="SUCCESS", required=True),
                make_check(name="can", status="COMPLETED", conclusion="CANCELLED", required=False),
            ]
        )
        self.assertEqual(green.required_summary(), "SUCCESS")

        # Non-required check failure also marks summary as FAILURE so it matches rollup_state.
        unmarked_fail = make_snapshot(
            checks=[
                make_check(name="c1", status="COMPLETED", conclusion="SUCCESS", required=True),
                make_check(name="c2", status="COMPLETED", conclusion="FAILURE", required=False),
            ]
        )
        self.assertEqual(unmarked_fail.required_summary(), "FAILURE")

    def test_round_trip_serialization_and_prune(self) -> None:
        snap = make_snapshot(threads=[make_thread()])
        ev = Event(
            pr=snap.key,
            kind="ci_check",
            from_value="IN_PROGRESS",
            to_value="FAILURE",
            actionable=True,
            suggested_action="diagnose_ci",
            summary="CI failed (test-suite)",
            detail={"run_id": 100},
        )
        self.assertIn(ev.kind, EVENT_KINDS)
        self.assertEqual(Event.from_dict(ev.to_dict()), ev)
        self.assertEqual(PrSnapshot.from_dict(snap.to_dict()), snap)
        self.assertEqual(CheckRun.from_dict(snap.checks[0].to_dict()), snap.checks[0])
        self.assertEqual(Thread.from_dict(snap.threads[0].to_dict()), snap.threads[0])

        state = MonitorState(
            schema=SCHEMA_VERSION,
            snapshots={snap.key: snap},
            notifications_last_modified="Tue, 27 Oct 2026 21:00:00 GMT",
            check_durations={"acme/widget:test-suite": [110, 120]},
            pushed_shas={snap.key: "abc"},
            reruns={snap.key: ["test-suite@abc"]},
            reported_threads={snap.key: {"PRRT_1": "2026-10-27T21:05:00Z"}},
        )
        restored = MonitorState.from_dict(state.to_dict())
        self.assertEqual(restored, state)

        # Legacy list format for reported_threads upgrades cleanly.
        legacy_dict = state.to_dict()
        legacy_dict["reported_threads"] = {snap.key: ["PRRT_1"]}
        upgraded = MonitorState.from_dict(legacy_dict)
        self.assertEqual(upgraded.reported_threads[snap.key], {"PRRT_1": ""})

        restored.prune({snap.key})
        self.assertEqual(restored.pushed_shas, {})
        self.assertEqual(restored.reruns, {})
        self.assertEqual(restored.reported_threads, {})

    def test_unsupported_schema_raises(self) -> None:
        with self.assertRaises(ValueError):
            MonitorState.from_dict({"schema": SCHEMA_VERSION + 1})

    def test_viewer_review_and_snapshot_round_trip(self) -> None:
        vr = make_viewer_review(
            state="COMMENTED",
            submitted_at="2026-10-27T21:00:00Z",
            commit_oid="aaa1111",
        )
        self.assertEqual(ViewerReview.from_dict(vr.to_dict()), vr)

        with_review = make_snapshot(
            viewer_last_review=vr,
            viewer_opinionated_state="CHANGES_REQUESTED",
        )
        self.assertEqual(PrSnapshot.from_dict(with_review.to_dict()), with_review)

        without_review = make_snapshot()
        raw = without_review.to_dict()
        raw.pop("viewer_last_review", None)
        raw.pop("viewer_opinionated_state", None)
        restored = PrSnapshot.from_dict(raw)
        self.assertIsNone(restored.viewer_last_review)
        self.assertIsNone(restored.viewer_opinionated_state)
        self.assertEqual(restored, without_review)

    def test_parse_event_list_for_author_and_reviewer_roles(self) -> None:
        self.assertIsNone(parse_event_list(None))
        self.assertIsNone(parse_event_list(""))
        self.assertIsNone(parse_event_list(" , "))

        author_kinds = parse_event_list("ci_check, head_push", ReviewRole.AUTHOR)
        self.assertEqual(author_kinds, {"ci_check", "head_push"})

        reviewer_kinds = parse_event_list(
            "new_review_request, author_head_push", ReviewRole.REVIEWER
        )
        self.assertEqual(
            reviewer_kinds,
            {
                ReviewerEventKind.NEW_REVIEW_REQUEST.value,
                ReviewerEventKind.AUTHOR_HEAD_PUSH.value,
            },
        )
        self.assertIn("terminal", REVIEWER_EVENT_KINDS)

        with self.assertRaises(ValueError):
            parse_event_list("new_review_request", ReviewRole.AUTHOR)
        with self.assertRaises(ValueError):
            parse_event_list("ci_check", ReviewRole.REVIEWER)

    def test_classify_review_turn_all_steps_and_edge_cases(self) -> None:
        # 1. Unreviewed open PR -> REQUESTED, REVIEW_REQUESTED
        unreviewed = make_snapshot(
            mine=False,
            requested_reviewers=["Bob"],
            viewer_last_review=None,
        )
        self.assertEqual(
            classify_review_turn(unreviewed, "bob"),
            (ReviewStep.REQUESTED, ReviewSortTier.REVIEW_REQUESTED),
        )

        # 2. Reviewed at current head, no new threads or direct re-request -> WAITING_ON_AUTHOR
        waiting = make_snapshot(
            mine=False,
            head_sha="aaa1111",
            requested_reviewers=["team:acme/core"],
            viewer_last_review=make_viewer_review(
                state="COMMENTED",
                submitted_at="2026-10-27T21:00:00Z",
                commit_oid="aaa1111",
            ),
            threads=[
                make_thread(
                    resolved=False,
                    last_author="bob",
                    last_comment_at="2026-10-27T21:05:00Z",
                ),
                make_thread(
                    thread_id="PRRT_2",
                    resolved=True,
                    last_author="alice",
                    last_comment_at="2026-10-27T21:10:00Z",
                ),
            ],
        )
        self.assertEqual(
            classify_review_turn(waiting, "Bob"),
            (ReviewStep.WAITING_ON_AUTHOR, ReviewSortTier.WAITING_ON_AUTHOR),
        )

        # 3a. Re-review needed on head push after COMMENTED review
        pushed = make_snapshot(
            mine=False,
            head_sha="bbb2222",
            requested_reviewers=[],
            viewer_last_review=make_viewer_review(
                state="COMMENTED",
                submitted_at="2026-10-27T21:00:00Z",
                commit_oid="aaa1111",
            ),
        )
        self.assertEqual(
            classify_review_turn(pushed, "bob"),
            (ReviewStep.RE_REVIEW_NEEDED, ReviewSortTier.RE_REVIEW_NEEDED),
        )

        # 3b. Re-review needed on unresolved non-viewer thread reply after review
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
                    resolved=False,
                    last_author="alice",
                    last_comment_at="2026-10-27T21:05:00Z",
                )
            ],
        )
        self.assertEqual(
            classify_review_turn(replied, "bob"),
            (ReviewStep.RE_REVIEW_NEEDED, ReviewSortTier.RE_REVIEW_NEEDED),
        )

        # 4. Approved open PR (including post-approval commit and lingering team request)
        approved_post_push = make_snapshot(
            mine=False,
            head_sha="ccc3333",
            requested_reviewers=["team:acme/core"],
            approvals=["bob"],
            viewer_opinionated_state="APPROVED",
            viewer_last_review=make_viewer_review(
                state="APPROVED",
                submitted_at="2026-10-27T21:00:00Z",
                commit_oid="aaa1111",
            ),
        )
        self.assertEqual(
            classify_review_turn(approved_post_push, "bob"),
            (ReviewStep.APPROVED, ReviewSortTier.APPROVED_OPEN),
        )

        # 4b. Direct user re-request after approval -> RE_REVIEW_NEEDED
        re_requested_after_approval = make_snapshot(
            mine=False,
            head_sha="ccc3333",
            requested_reviewers=["bob"],
            approvals=["bob"],
            viewer_opinionated_state="APPROVED",
            viewer_last_review=make_viewer_review(
                state="APPROVED",
                submitted_at="2026-10-27T21:00:00Z",
                commit_oid="aaa1111",
            ),
        )
        self.assertEqual(
            classify_review_turn(re_requested_after_approval, "bob"),
            (ReviewStep.RE_REVIEW_NEEDED, ReviewSortTier.RE_REVIEW_NEEDED),
        )

        # 5. Draft muting (unreviewed vs previously reviewed)
        draft_unreviewed = make_snapshot(
            mine=False,
            draft=True,
            viewer_last_review=None,
        )
        self.assertEqual(
            classify_review_turn(draft_unreviewed, "bob"),
            (ReviewStep.REQUESTED, ReviewSortTier.DRAFT_REQUESTED),
        )
        draft_reviewed = make_snapshot(
            mine=False,
            draft=True,
            head_sha="bbb2222",
            viewer_last_review=make_viewer_review(commit_oid="aaa1111"),
        )
        self.assertEqual(
            classify_review_turn(draft_reviewed, "bob"),
            (ReviewStep.WAITING_ON_AUTHOR, ReviewSortTier.DRAFT_REQUESTED),
        )

        # 6. Terminal MERGED vs unmerged CLOSED (step = 0)
        merged = make_snapshot(mine=False, state="MERGED")
        self.assertEqual(
            classify_review_turn(merged, "bob"),
            (ReviewStep.MERGED, ReviewSortTier.TERMINAL),
        )
        closed = make_snapshot(
            mine=False,
            state="CLOSED",
            viewer_last_review=make_viewer_review(state="APPROVED"),
            viewer_opinionated_state="APPROVED",
        )
        self.assertEqual(
            classify_review_turn(closed, "bob"),
            (ReviewStep.REQUESTED, ReviewSortTier.TERMINAL),
        )


if __name__ == "__main__":
    unittest.main()
