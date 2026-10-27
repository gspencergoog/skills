"""Unit tests for state.py."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from models import MonitorState
from state import (
    StateError,
    append_event_log,
    eta_seconds,
    load_state,
    median_duration,
    parse_iso8601,
    record_completed_durations,
    record_duration,
    save_state,
)
from testing import make_check, make_snapshot


class TestState(unittest.TestCase):
    def test_missing_and_corrupt_state_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nested" / "state.json"
            self.assertEqual(load_state(missing), MonitorState.empty())

            missing.parent.mkdir(parents=True, exist_ok=True)
            missing.write_text("{not valid json", encoding="utf-8")
            with self.assertRaises(StateError):
                load_state(missing)

            missing.write_text("[]", encoding="utf-8")
            with self.assertRaises(StateError):
                load_state(missing)

    def test_atomic_save_and_load_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "state.json"
            state = MonitorState.empty()
            snap = make_snapshot()
            state.snapshots[snap.key] = snap
            record_duration(state, snap.repo, "test-suite", 120)
            save_state(path, state)
            loaded = load_state(path)
            self.assertEqual(loaded, state)

    def test_duration_window_of_five_and_eta(self) -> None:
        state = MonitorState.empty()
        for dur in [100, 200, 300, 400, 500, 600]:
            record_duration(state, "acme/widget", "test-web", dur)
        self.assertEqual(state.check_durations["acme/widget:test-web"], [200, 300, 400, 500, 600])
        self.assertEqual(median_duration(state, "acme/widget", "test-web"), 400)
        self.assertIsNone(median_duration(state, "acme/widget", "unknown"))

        # Running required check with 100s elapsed out of 400s median -> 300s remaining.
        start_epoch = parse_iso8601("2026-10-27T21:00:00Z")
        assert start_epoch is not None
        running_snap = make_snapshot(
            checks=[
                make_check(
                    name="test-web",
                    status="IN_PROGRESS",
                    conclusion=None,
                    required=True,
                    started_at="2026-10-27T21:00:00Z",
                    completed_at=None,
                )
            ]
        )
        self.assertEqual(eta_seconds(state, running_snap, now_epoch=start_epoch + 100), 300)

        # No history -> None ETA.
        empty_state = MonitorState.empty()
        self.assertIsNone(eta_seconds(empty_state, running_snap, now_epoch=start_epoch + 100))

    def test_record_completed_durations_dedupes_same_head(self) -> None:
        state = MonitorState.empty()
        prev = make_snapshot(
            checks=[
                make_check(
                    name="test-web",
                    status="IN_PROGRESS",
                    conclusion=None,
                    started_at="2026-10-27T21:00:00Z",
                    completed_at=None,
                )
            ]
        )
        cur = make_snapshot(
            checks=[
                make_check(
                    name="test-web",
                    status="COMPLETED",
                    conclusion="SUCCESS",
                    started_at="2026-10-27T21:00:00Z",
                    completed_at="2026-10-27T21:03:00Z",
                )
            ]
        )
        record_completed_durations(state, prev, cur)
        self.assertEqual(state.check_durations["acme/widget:test-web"], [180])
        # Second refresh on same head must not duplicate the sample.
        record_completed_durations(state, cur, cur)
        self.assertEqual(state.check_durations["acme/widget:test-web"], [180])

    def test_append_event_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "logs" / "events.jsonl"
            append_event_log(log_path, {"seq": 1})
            append_event_log(log_path, {"seq": 2})
            lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(lines, [{"seq": 1}, {"seq": 2}])


if __name__ == "__main__":
    unittest.main()
