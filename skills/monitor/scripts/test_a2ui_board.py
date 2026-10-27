"""Unit tests for a2ui_board.py."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from a2ui_board import (
    board_data_model,
    board_dsl,
    item_for,
    lifecycle_step,
    sync_pane_store,
    write_board_file,
)
from models import Event
from testing import make_check, make_snapshot, make_thread


class TestA2uiBoard(unittest.TestCase):
    def test_lifecycle_steps(self) -> None:
        self.assertEqual(lifecycle_step(make_snapshot(draft=True)), 0)
        self.assertEqual(lifecycle_step(make_snapshot(state="CLOSED")), 0)
        self.assertEqual(
            lifecycle_step(
                make_snapshot(
                    checks=[make_check(status="IN_PROGRESS", conclusion=None)],
                    rollup_state="PENDING",
                )
            ),
            1,
        )
        self.assertEqual(
            lifecycle_step(
                make_snapshot(
                    review_decision="REVIEW_REQUIRED",
                    checks=[make_check(status="COMPLETED", conclusion="SUCCESS")],
                )
            ),
            2,
        )
        self.assertEqual(
            lifecycle_step(
                make_snapshot(
                    review_decision="APPROVED",
                    checks=[make_check(status="COMPLETED", conclusion="SUCCESS")],
                )
            ),
            3,
        )
        self.assertEqual(lifecycle_step(make_snapshot(state="MERGED")), 4)

    def test_badges_and_watch_only(self) -> None:
        conflict_snap = make_snapshot(
            mergeable="CONFLICTING",
            review_decision="CHANGES_REQUESTED",
            checks=[make_check(status="COMPLETED", conclusion="FAILURE")],
        )
        item = item_for(conflict_snap)
        self.assertEqual((item["state"], item["tone"]), ("Conflicts", "danger"))
        self.assertEqual((item["ci"], item["ciTone"]), ("CI failed", "danger"))
        self.assertEqual((item["review"], item["reviewTone"]), ("Changes requested", "danger"))

        running_snap = make_snapshot(
            mine=False,
            checks=[make_check(status="IN_PROGRESS", conclusion=None)],
            threads=[make_thread()],
        )
        watch_item = item_for(running_snap, eta_s=180)
        self.assertEqual((watch_item["state"], watch_item["tone"]), ("Watching", "neutral"))
        self.assertEqual((watch_item["ci"], watch_item["ciTone"]), ("CI running, ETA 3m", "active"))
        self.assertEqual(
            (watch_item["review"], watch_item["reviewTone"]), ("1 unresolved", "warning")
        )
        self.assertEqual(watch_item["detail"], "Watch only")

    def test_board_dsl_escaping_and_stack_wave(self) -> None:
        parent = make_snapshot(number=3338, key="acme/widget#3338", head_ref="feat/base")
        child = make_snapshot(
            number=3339,
            key="acme/widget#3339",
            title='fix: handle "quotes", \\slashes\\, and [brackets]',
            base_ref="feat/base",
            stack_parent="acme/widget#3338",
        )
        ev = Event(
            pr=child.key,
            kind="ci_check",
            from_value="IN_PROGRESS",
            to_value="SUCCESS",
            actionable=False,
            suggested_action=None,
            summary="all required CI checks passed",
        )
        dsl = board_dsl(
            {parent.key: parent, child.key: child},
            [ev],
            eta={child.key: 45},
            observed_at="2026-10-27T21:15:00Z",
        )
        parsed: dict[str, object] = {}
        for line in dsl.splitlines():
            key, val = line.split(" = ", 1)
            parsed[key] = json.loads(val)
        self.assertEqual(parsed["$/title"], "PR Monitor")
        items = parsed["$/items"]
        assert isinstance(items, list)
        self.assertEqual(items[0]["wave"], "1")
        self.assertEqual(items[1]["wave"], "2")
        self.assertEqual(items[1]["base"], "PR 3338")
        self.assertEqual(items[1]["title"], 'fix: handle "quotes", \\slashes\\, and [brackets]')
        self.assertEqual(items[1]["detail"], "all required CI checks passed")

    def test_write_board_file_and_sync_pane_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            board_path = Path(tmp) / "board.a2ui"
            store_dir = Path(tmp) / "plugin_data"
            snap1 = make_snapshot(number=3339, key="acme/widget#3339")
            dm1 = board_data_model({snap1.key: snap1}, [], observed_at="2026-10-27T21:15:00Z")
            dsl1 = board_dsl({snap1.key: snap1}, [], observed_at="2026-10-27T21:15:00Z")

            written = write_board_file(board_path, dsl1)
            self.assertEqual(written, board_path)
            self.assertIn('$/title = "PR Monitor"', board_path.read_text(encoding="utf-8"))

            # First sync creates the "monitor" surface at seq 1.
            ok1 = sync_pane_store(
                "conv-123", dm1, store_dir=store_dir, observed_at="2026-10-27T21:15:00Z"
            )
            self.assertTrue(ok1)
            conv_file = store_dir / "conversations" / "conv-123.jsonl"
            lines1 = [
                json.loads(line) for line in conv_file.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(lines1), 1)
            self.assertEqual(lines1[0]["seq"], 1)
            self.assertIn("createSurface", lines1[0]["message"])

            # Second sync with identical items is a no-op.
            ok_noop = sync_pane_store(
                "conv-123", dm1, store_dir=store_dir, observed_at="2026-10-27T21:16:00Z"
            )
            self.assertFalse(ok_noop)
            self.assertEqual(len(conv_file.read_text(encoding="utf-8").splitlines()), 1)

            # Third sync with updated PR state appends updateDataModel for /subtitle and /items.
            snap2 = make_snapshot(number=3339, key="acme/widget#3339", state="MERGED")
            dm2 = board_data_model({snap2.key: snap2}, [], observed_at="2026-10-27T21:17:00Z")
            ok2 = sync_pane_store(
                "conv-123", dm2, store_dir=store_dir, observed_at="2026-10-27T21:17:00Z"
            )
            self.assertTrue(ok2)
            lines2 = [
                json.loads(line) for line in conv_file.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(lines2), 3)
            self.assertEqual(lines2[1]["seq"], 2)
            self.assertEqual(lines2[1]["message"]["updateDataModel"]["path"], "/subtitle")
            self.assertEqual(lines2[2]["seq"], 3)
            self.assertEqual(lines2[2]["message"]["updateDataModel"]["path"], "/items")

            # Invalid conversation_id returns False safely.
            self.assertFalse(sync_pane_store("../bad", dm2, store_dir=store_dir))


if __name__ == "__main__":
    unittest.main()
