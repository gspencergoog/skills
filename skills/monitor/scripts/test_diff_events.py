"""Unit tests for diff_events.py."""

from __future__ import annotations

import unittest

from diff_events import (
    DiffContext,
    diff_snapshots,
    filter_events,
    is_green,
    is_terminal,
    snapshot_thread_watermarks,
    summarize,
)
from testing import make_check, make_snapshot, make_thread


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


if __name__ == "__main__":
    unittest.main()
