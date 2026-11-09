"""Unit tests for models.py."""

from __future__ import annotations

import unittest

from models import (
    EVENT_KINDS,
    REVIEWER_EVENT_KINDS,
    SCHEMA_VERSION,
    CheckRun,
    DrawerReason,
    Event,
    MonitorState,
    MuteEntry,
    PrSnapshot,
    ReviewerEventKind,
    ReviewRole,
    ReviewSortTier,
    ReviewStep,
    Thread,
    TriageEntry,
    TriageVerdict,
    ViewerReview,
    classify_review_turn,
    dormant_age_days,
    has_unanswered_direct_request,
    is_bot,
    is_dormant_review,
    is_mute_active,
    parse_event_list,
    parse_iso_seconds,
    parse_pr_ref,
    pr_key,
    viewer_thread_progress,
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

    def test_parse_iso_seconds_and_is_bot_detection(self) -> None:
        self.assertIsNone(parse_iso_seconds(None))
        self.assertIsNone(parse_iso_seconds(""))
        self.assertIsNone(parse_iso_seconds("not-a-date"))
        ts = parse_iso_seconds("2026-10-27T21:00:00Z")
        self.assertIsNotNone(ts)
        self.assertEqual(ts, parse_iso_seconds("2026-10-27T21:00:00+00:00"))

        self.assertFalse(is_bot(None))
        self.assertFalse(is_bot({"login": "alice", "__typename": "User"}))
        self.assertTrue(is_bot({"login": "gemini-code-assist", "__typename": "Bot"}))
        self.assertTrue(is_bot({"login": "dependabot[bot]", "__typename": "User"}))
        self.assertTrue(is_bot({"login": "ci-helper", "is_bot": True}))

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
        snap = make_snapshot(
            threads=[
                make_thread(
                    outdated=True,
                    participants=("bob", "alice"),
                )
            ],
            head_committed_date="2026-10-27T21:00:00Z",
            viewer_requested_at="2026-10-27T20:55:00Z",
            viewer_last_comment_at="2026-10-27T21:10:00Z",
            author_last_comment_at="2026-10-27T21:04:00Z",
            discussion_watermark="IC_1:2026-10-27T21:10:00Z",
        )
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

        mute = MuteEntry(
            muted_at="2026-10-27T22:00:00Z",
            head_sha="aaa1111",
            updated_at="2026-10-27T21:05:00Z",
            was_requested=True,
            reason="shelved",
        )
        self.assertEqual(MuteEntry.from_dict(mute.to_dict()), mute)
        triage = TriageEntry(
            watermark="IC_1:2026-10-27T21:10:00Z",
            verdict=TriageVerdict.LIKELY_SHELVED,
            summary="Participants agreed to drop this approach.",
            evaluated_at="2026-10-27T22:01:00Z",
        )
        self.assertEqual(TriageEntry.from_dict(triage.to_dict()), triage)
        self.assertEqual(
            TriageEntry.from_dict({"verdict": "unknown_value"}).verdict,
            TriageVerdict.ACTIVE,
        )
        self.assertEqual(DrawerReason.MUTED.value, "muted")
        self.assertEqual(DrawerReason.DORMANT.value, "dormant")

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

    def test_viewer_review_and_legacy_snapshot_round_trip(self) -> None:
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

        without_review = make_snapshot(threads=[make_thread()])
        raw = without_review.to_dict()
        for key in (
            "viewer_last_review",
            "viewer_opinionated_state",
            "head_committed_date",
            "viewer_requested_at",
            "viewer_last_comment_at",
            "author_last_comment_at",
            "discussion_watermark",
        ):
            raw.pop(key, None)
        raw["threads"][0].pop("outdated", None)
        raw["threads"][0].pop("participants", None)
        restored = PrSnapshot.from_dict(raw)
        self.assertIsNone(restored.viewer_last_review)
        self.assertEqual(restored.threads[0].outdated, False)
        self.assertEqual(restored.threads[0].participants, ())
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

        # 3a. Summary-only review + head push -> RE_REVIEW_NEEDED
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

        # 3b. Unhydrated pre-v9 fallback: unresolved non-viewer thread reply -> RE_REVIEW_NEEDED
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

        # 7. Pre-v8 / legacy state fallback when viewer_last_review is None
        legacy_changes_req = make_snapshot(
            mine=False,
            requested_reviewers=[],
            viewer_last_review=None,
            viewer_opinionated_state="CHANGES_REQUESTED",
            changes_requested=["bob"],
        )
        self.assertEqual(
            classify_review_turn(legacy_changes_req, "bob"),
            (ReviewStep.WAITING_ON_AUTHOR, ReviewSortTier.WAITING_ON_AUTHOR),
        )

    def test_thread_gated_re_review_scenarios(self) -> None:
        review = make_viewer_review(
            state="CHANGES_REQUESTED",
            submitted_at="2026-10-27T17:21:00Z",
            commit_oid="4505fa4",
        )
        # Scenario 1 (#3261 / #2572 pattern): unrelated post-review commit while all 3 viewer
        # threads remain unresolved, non-outdated, and last authored by viewer -> WAITING_ON_AUTHOR.
        untouched_push = make_snapshot(
            mine=False,
            head_sha="ddd4444",
            head_committed_date="2026-10-27T19:00:00Z",
            requested_reviewers=[],
            viewer_last_review=review,
            threads=[
                make_thread(
                    thread_id=f"PRRT_{idx}",
                    resolved=False,
                    outdated=False,
                    last_author="bob",
                    last_comment_at="2026-10-27T17:21:00Z",
                    participants=("bob",),
                )
                for idx in (1, 2, 3)
            ],
        )
        self.assertEqual(
            classify_review_turn(untouched_push, "bob"),
            (ReviewStep.WAITING_ON_AUTHOR, ReviewSortTier.WAITING_ON_AUTHOR),
        )
        self.assertEqual(viewer_thread_progress(untouched_push, "bob"), (0, 3))

        # Scenario 2 (#1960 pattern A): inline reply review bound to older SHA while head commit
        # predates submitted_at -> post_review_commit is False -> WAITING_ON_AUTHOR.
        older_commit_head = make_snapshot(
            mine=False,
            head_sha="bac822e",
            head_committed_date="2026-10-27T14:30:00Z",
            requested_reviewers=[],
            viewer_last_review=review,
            threads=[],
        )
        self.assertEqual(
            classify_review_turn(older_commit_head, "bob"),
            (ReviewStep.WAITING_ON_AUTHOR, ReviewSortTier.WAITING_ON_AUTHOR),
        )

        # Scenario 3 (#1960 pattern B): peer thread receives an author reply while one of viewer's
        # own threads stays untouched -> WAITING_ON_AUTHOR with (1, 2) viewer thread progress.
        partial_threads = make_snapshot(
            mine=False,
            head_sha="ddd4444",
            head_committed_date="2026-10-27T19:00:00Z",
            requested_reviewers=[],
            viewer_last_review=review,
            threads=[
                make_thread(
                    thread_id="t_peer",
                    resolved=False,
                    last_author="alice",
                    last_comment_at="2026-10-27T19:05:00Z",
                    participants=("carol", "alice"),
                ),
                make_thread(
                    thread_id="t_viewer_done",
                    resolved=True,
                    last_author="alice",
                    last_comment_at="2026-10-27T19:05:00Z",
                    participants=("bob", "alice"),
                ),
                make_thread(
                    thread_id="t_viewer_open",
                    resolved=False,
                    outdated=False,
                    last_author="bob",
                    last_comment_at="2026-10-27T17:21:00Z",
                    participants=("bob",),
                ),
            ],
        )
        self.assertEqual(
            classify_review_turn(partial_threads, "bob"),
            (ReviewStep.WAITING_ON_AUTHOR, ReviewSortTier.WAITING_ON_AUTHOR),
        )
        self.assertEqual(viewer_thread_progress(partial_threads, "bob"), (1, 2))

        # Scenario 4 (#3938 pattern): all viewer threads addressed (one outdated by post-review
        # commit, one replied by human) -> promotes to RE_REVIEW_NEEDED.
        all_addressed = make_snapshot(
            mine=False,
            head_sha="ddd4444",
            head_committed_date="2026-10-27T19:00:00Z",
            requested_reviewers=[],
            viewer_last_review=review,
            threads=[
                make_thread(
                    thread_id="t_viewer_outdated",
                    resolved=False,
                    outdated=True,
                    last_author="bob",
                    last_comment_at="2026-10-27T17:21:00Z",
                    participants=("bob",),
                ),
                make_thread(
                    thread_id="t_viewer_replied",
                    resolved=False,
                    outdated=False,
                    last_author="alice",
                    last_comment_at="2026-10-27T19:10:00Z",
                    participants=("bob", "alice"),
                ),
            ],
        )
        self.assertEqual(
            classify_review_turn(all_addressed, "bob"),
            (ReviewStep.RE_REVIEW_NEEDED, ReviewSortTier.RE_REVIEW_NEEDED),
        )
        self.assertEqual(viewer_thread_progress(all_addressed, "bob"), (2, 2))

        # Bot reply on viewer thread does NOT count as addressing it.
        bot_reply_only = make_snapshot(
            mine=False,
            head_sha="aaa1111",
            requested_reviewers=[],
            viewer_last_review=make_viewer_review(
                state="COMMENTED",
                submitted_at="2026-10-27T17:21:00Z",
                commit_oid="aaa1111",
            ),
            threads=[
                make_thread(
                    thread_id="t_bot",
                    resolved=False,
                    last_author="ci-bot",
                    last_author_is_bot=True,
                    last_comment_at="2026-10-27T19:10:00Z",
                    participants=("bob", "ci-bot"),
                ),
            ],
        )
        self.assertEqual(
            classify_review_turn(bot_reply_only, "bob"),
            (ReviewStep.WAITING_ON_AUTHOR, ReviewSortTier.WAITING_ON_AUTHOR),
        )

    def test_post_request_timeline_comment_demotes_direct_request(self) -> None:
        # Scenario (#3874 pattern): author pushes bbb2222 at 22:00Z and re-requests viewer at
        # 22:03:21Z; viewer posts top-level timeline comment at 22:36:32Z agreeing not to pursue ->
        # demotes to WAITING_ON_AUTHOR.
        shelved = make_snapshot(
            mine=False,
            head_sha="bbb2222",
            head_committed_date="2026-10-29T22:00:00Z",
            requested_reviewers=["bob"],
            viewer_requested_at="2026-10-29T22:03:21Z",
            viewer_last_comment_at="2026-10-29T22:36:32Z",
            author_last_comment_at="2026-10-29T22:03:00Z",
            viewer_last_review=make_viewer_review(
                state="COMMENTED",
                submitted_at="2026-10-29T21:00:00Z",
                commit_oid="aaa1111",
            ),
        )
        self.assertFalse(has_unanswered_direct_request(shelved, "bob"))
        self.assertEqual(
            classify_review_turn(shelved, "bob"),
            (ReviewStep.WAITING_ON_AUTHOR, ReviewSortTier.WAITING_ON_AUTHOR),
        )

        # Author follows up at 23:00Z -> request becomes active again -> RE_REVIEW_NEEDED.
        revived = make_snapshot(
            **{
                **shelved.to_dict(),
                "author_last_comment_at": "2026-10-29T23:00:00Z",
                "checks": [make_check()],
                "threads": [],
                "viewer_last_review": shelved.viewer_last_review,
            }
        )
        self.assertTrue(has_unanswered_direct_request(revived, "bob"))
        self.assertEqual(
            classify_review_turn(revived, "bob"),
            (ReviewStep.RE_REVIEW_NEEDED, ReviewSortTier.RE_REVIEW_NEEDED),
        )

    def test_mute_active_and_dormant_review_predicates(self) -> None:
        snap = make_snapshot(
            mine=False,
            head_sha="aaa1111",
            head_committed_date="2026-10-01T10:00:00Z",
            updated_at="2026-10-01T12:00:00Z",
            requested_reviewers=["bob"],
            viewer_requested_at="2026-10-01T11:00:00Z",
            viewer_last_review=make_viewer_review(
                state="COMMENTED",
                submitted_at="2026-10-01T10:30:00Z",
                commit_oid="aaa1111",
            ),
        )
        mute = MuteEntry(
            muted_at="2026-10-02T00:00:00Z",
            head_sha="aaa1111",
            updated_at="2026-10-01T12:00:00Z",
            was_requested=True,
        )
        self.assertTrue(is_mute_active(snap, mute, "bob"))

        # F4: Pushing a commit whose local author/commit date predates muted_at STILL wakes mute
        # because head_sha changed from mute.head_sha.
        pre_committed_push = make_snapshot(
            mine=False,
            head_sha="bbb2222",
            head_committed_date="2026-10-01T23:00:00Z",
            requested_reviewers=["bob"],
            viewer_requested_at="2026-10-01T11:00:00Z",
        )
        self.assertFalse(is_mute_active(pre_committed_push, mute, "bob"))

        # Fresh ReviewRequestedEvent after muted_at wakes mute even at same head_sha.
        fresh_request = make_snapshot(
            mine=False,
            head_sha="aaa1111",
            requested_reviewers=["bob"],
            viewer_requested_at="2026-10-03T00:00:00Z",
        )
        self.assertFalse(is_mute_active(fresh_request, mute, "bob"))

        # Closed/merged PRs do not stay actively muted.
        closed_snap = make_snapshot(mine=False, state="CLOSED", head_sha="aaa1111")
        self.assertFalse(is_mute_active(closed_snap, mute, "bob"))

        # Dormancy: unrequested Waiting PR idle 40 days -> dormant, unless pinned_at is recent.
        idle_waiting = make_snapshot(
            mine=False,
            head_sha="aaa1111",
            updated_at="2026-09-25T12:00:00Z",
            requested_reviewers=[],
            viewer_last_review=make_viewer_review(
                state="COMMENTED",
                submitted_at="2026-09-25T12:00:00Z",
                commit_oid="aaa1111",
            ),
        )
        now_epoch = parse_iso_seconds("2026-11-09T12:00:00Z")
        assert now_epoch is not None
        self.assertEqual(dormant_age_days(idle_waiting, now_epoch), 45)
        self.assertTrue(is_dormant_review(idle_waiting, "bob", 30, now_epoch))
        self.assertFalse(is_dormant_review(idle_waiting, "bob", 0, now_epoch))
        self.assertFalse(
            is_dormant_review(
                idle_waiting,
                "bob",
                30,
                now_epoch,
                pinned_at="2026-11-08T12:00:00Z",
            )
        )


if __name__ == "__main__":
    unittest.main()
