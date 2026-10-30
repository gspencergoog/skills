"""Unit tests for models.py."""

from __future__ import annotations

import unittest

from models import (
    EVENT_KINDS,
    SCHEMA_VERSION,
    CheckRun,
    Event,
    MonitorState,
    PrSnapshot,
    Thread,
    is_bot,
    parse_pr_ref,
    pr_key,
)
from testing import make_check, make_snapshot, make_thread


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


if __name__ == "__main__":
    unittest.main()
