"""Unit tests for mine_mode.py: fresh-fetch gates, the row lifecycle, and recent removals."""

from __future__ import annotations

import unittest

from mine_mode import (
    GATE_LAPSE_S,
    GATE_SLACK_S,
    RECENT_RETIRED_MAX,
    RECENT_RETIRED_TTL_S,
    REMOVAL_REASONS,
    GateBook,
    GateReason,
    MineRows,
    RecentRetired,
    Resolution,
)

A = "acme/widget#1"
B = "acme/widget#2"
C = "other/repo#3"


class TestGateBook(unittest.TestCase):
    def test_no_gate_accepts_any_row(self) -> None:
        book = GateBook()
        self.assertFalse(book.holds(A))
        self.assertIsNone(book.reason(A))
        self.assertTrue(book.accepts(A, 5000.0, now=100.0))
        self.assertTrue(book.accepts(A, None, now=100.0))

    def test_row_fetched_before_the_gate_is_rejected(self) -> None:
        book = GateBook()
        book.set(A, at=100.0, reason=GateReason.SET_EXIT)
        # At now=130 the gate is 30 s old. A row fetched 300 s ago predates it.
        self.assertFalse(book.accepts(A, 300.0, now=130.0))
        # A row fetched 10 s ago came after the gate.
        self.assertTrue(book.accepts(A, 10.0, now=130.0))
        self.assertTrue(book.accepts(B, 300.0, now=130.0))

    def test_slack_accepts_a_row_fetched_in_the_same_second(self) -> None:
        book = GateBook()
        book.set(A, at=100.0, reason=GateReason.STARTUP)
        # packrat stores fetch times in whole seconds, so a row fetched right at
        # the gate can report an age up to one second past it.
        self.assertTrue(book.accepts(A, GATE_SLACK_S, now=100.0))
        self.assertTrue(book.accepts(A, 0.0, now=100.0))
        self.assertFalse(book.accepts(A, GATE_SLACK_S + 0.5, now=100.0))

    def test_gated_row_with_unknown_age_is_rejected(self) -> None:
        book = GateBook()
        book.set(A, at=100.0, reason=GateReason.RECOVERY)
        self.assertFalse(book.accepts(A, None, now=500.0))

    def test_gate_lapses_at_600_seconds_and_stays_until_cleared(self) -> None:
        book = GateBook()
        book.set(A, at=100.0, reason=GateReason.SET_EXIT)
        book.set(B, at=400.0, reason=GateReason.RECOVERY)
        self.assertEqual(book.lapsed(100.0 + GATE_LAPSE_S - 0.1), [])
        self.assertEqual(book.lapsed(100.0 + GATE_LAPSE_S), [(A, GateReason.SET_EXIT)])
        self.assertTrue(book.holds(A))
        book.clear(A)
        self.assertEqual(book.lapsed(100.0 + GATE_LAPSE_S), [])
        self.assertEqual(book.lapsed(400.0 + GATE_LAPSE_S), [(B, GateReason.RECOVERY)])

    def test_set_replaces_keys_and_reasons_are_reported(self) -> None:
        book = GateBook()
        book.set(A, at=100.0, reason=GateReason.STARTUP)
        book.set(B, at=110.0, reason=GateReason.RECOVERY)
        book.set(A, at=200.0, reason=GateReason.SET_EXIT)
        self.assertEqual(book.keys(), [A, B])
        self.assertEqual(book.reason(A), GateReason.SET_EXIT)
        self.assertFalse(book.holds_reason(GateReason.STARTUP))
        self.assertTrue(book.holds_reason(GateReason.RECOVERY))
        # The later gate time applies: a row fetched between the two is rejected.
        self.assertFalse(book.accepts(A, 60.0, now=210.0))
        book.clear(B)
        book.clear(B)
        self.assertEqual(book.keys(), [A])
        self.assertFalse(book.holds_reason(GateReason.RECOVERY))


class TestMineRowsLifecycle(unittest.TestCase):
    """One test per transition of the row lifecycle diagram in the design."""

    def _shown(self, rows: MineRows, *keys: str) -> None:
        """Puts ``keys`` in the set and gives each a board row."""
        rows.apply_discovery(list(keys), [], now=0.0)
        for key in keys:
            self.assertTrue(rows.mark_shown(key))

    def test_key_entering_the_set_becomes_pending(self) -> None:
        rows = MineRows()
        plan = rows.apply_discovery([A, B], [], now=10.0)
        self.assertEqual(plan.watched, [A, B])
        self.assertEqual(plan.lease, [A, B])
        self.assertEqual((plan.gate, plan.drop, plan.regained), ([], [], []))
        self.assertEqual(rows.pending, {A, B})
        self.assertEqual(rows.total_open, 2)

    def test_pending_key_is_shown_when_its_live_row_arrives(self) -> None:
        rows = MineRows()
        rows.apply_discovery([A], [], now=10.0)
        self.assertTrue(rows.mark_shown(A))  # announces pr_added
        self.assertEqual(rows.pending, set())
        self.assertFalse(rows.mark_shown(A))  # a later read announces nothing
        plan = rows.apply_discovery([A], [A], now=20.0)
        self.assertEqual((plan.lease, plan.gate, plan.drop), ([], [], []))

    def test_pending_key_that_leaves_the_set_first_is_dropped_silently(self) -> None:
        rows = MineRows()
        rows.apply_discovery([A, B], [], now=10.0)
        plan = rows.apply_discovery([B], [], now=20.0)
        self.assertEqual(plan.drop, [A])
        self.assertEqual(plan.gate, [])
        self.assertEqual(plan.watched, [B])
        self.assertEqual(rows.pending, {B})
        self.assertEqual(rows.pending_exit, {})

    def test_shown_row_that_merges_or_closes_is_retired_as_terminal(self) -> None:
        for state in ("MERGED", "CLOSED"):
            with self.subTest(state=state):
                rows = MineRows()
                self._shown(rows, A)
                self.assertEqual(
                    rows.resolve(A, state, in_set=True, gate_lapsed=False),
                    Resolution.RETIRE_TERMINAL,
                )

    def test_shown_open_row_in_the_set_is_kept(self) -> None:
        rows = MineRows()
        self._shown(rows, A)
        self.assertEqual(
            rows.resolve(A, "OPEN", in_set=True, gate_lapsed=False), Resolution.KEEP
        )
        self.assertEqual(
            rows.resolve(A, None, in_set=True, gate_lapsed=False), Resolution.KEEP
        )

    def test_shown_key_leaving_the_set_while_open_waits_to_exit(self) -> None:
        rows = MineRows()
        self._shown(rows, A, B)
        plan = rows.apply_discovery([B], [A, B], now=50.0)
        self.assertEqual(plan.gate, [A])
        self.assertEqual(plan.drop, [])
        self.assertEqual(plan.watched, [B, A])  # the row keeps its place on the board
        self.assertEqual(rows.pending_exit, {A: 50.0})
        self.assertEqual(rows.total_open, 1)
        # A second read of the same set does not gate the key again.
        again = rows.apply_discovery([B], [A, B], now=70.0)
        self.assertEqual(again.gate, [])
        self.assertEqual(rows.pending_exit, {A: 50.0})
        # No fresh row yet and the gate still holds: the row stays.
        self.assertEqual(
            rows.resolve(A, None, in_set=False, gate_lapsed=False), Resolution.KEEP
        )

    def test_exiting_key_that_reenters_the_set_is_shown_again(self) -> None:
        rows = MineRows()
        self._shown(rows, A)
        rows.apply_discovery([], [A], now=50.0)
        plan = rows.apply_discovery([A], [A], now=60.0)
        self.assertEqual(plan.regained, [A])
        self.assertEqual((plan.lease, plan.gate, plan.drop), ([], [], []))
        self.assertEqual(rows.pending_exit, {})
        self.assertFalse(rows.mark_shown(A))  # the row never left: no pr_added

        # resolve() makes the same move when it learns the key is in the set.
        rows.apply_discovery([], [A], now=70.0)
        self.assertEqual(
            rows.resolve(A, None, in_set=True, gate_lapsed=True), Resolution.KEEP
        )
        self.assertEqual(rows.pending_exit, {})

    def test_exiting_key_with_a_fresh_terminal_row_is_retired_as_terminal(self) -> None:
        rows = MineRows()
        self._shown(rows, A)
        rows.apply_discovery([], [A], now=50.0)
        self.assertEqual(
            rows.resolve(A, "MERGED", in_set=False, gate_lapsed=False),
            Resolution.RETIRE_TERMINAL,
        )
        self.assertEqual(rows.pending_exit, {})

    def test_exiting_key_with_a_fresh_open_row_is_retired_as_left_set(self) -> None:
        rows = MineRows()
        self._shown(rows, A)
        rows.apply_discovery([], [A], now=50.0)
        resolution = rows.resolve(A, "OPEN", in_set=False, gate_lapsed=False)
        self.assertEqual(resolution, Resolution.RETIRE_LEFT_SET)
        self.assertEqual(REMOVAL_REASONS[resolution], "left_set")

    def test_exiting_key_whose_gate_lapses_is_retired_as_unconfirmed(self) -> None:
        rows = MineRows()
        self._shown(rows, A)
        rows.apply_discovery([], [A], now=50.0)
        resolution = rows.resolve(A, None, in_set=False, gate_lapsed=True)
        self.assertEqual(resolution, Resolution.RETIRE_UNCONFIRMED)
        self.assertEqual(REMOVAL_REASONS[resolution], "left_set_unconfirmed")

    def test_retired_key_is_forgotten_and_a_reopened_pr_starts_pending(self) -> None:
        rows = MineRows()
        self._shown(rows, A)
        rows.resolve(A, "CLOSED", in_set=True, gate_lapsed=False)
        self.assertEqual((rows.pending, rows.pending_exit), (set(), {}))
        plan = rows.apply_discovery([A], [], now=90.0)
        self.assertEqual(plan.lease, [A])
        self.assertEqual(rows.pending, {A})
        self.assertTrue(rows.mark_shown(A))  # announced again

    def test_once_never_retires_an_unconfirmed_exit(self) -> None:
        rows = MineRows(once=True)
        self._shown(rows, A)
        rows.apply_discovery([], [A], now=50.0)
        self.assertEqual(
            rows.resolve(A, None, in_set=False, gate_lapsed=True), Resolution.KEEP
        )
        self.assertEqual(rows.pending_exit, {A: 50.0})
        # A fresh row still resolves the exit under --once.
        self.assertEqual(
            rows.resolve(A, "MERGED", in_set=False, gate_lapsed=True),
            Resolution.RETIRE_TERMINAL,
        )


class TestMineRowsCap(unittest.TestCase):
    def _keys(self, count: int) -> list[str]:
        return [f"acme/widget#{n}" for n in range(1, count + 1)]

    def test_61st_key_displaces_the_oldest_with_no_retirement(self) -> None:
        rows = MineRows(cap=60)
        keys = self._keys(60)  # newest first
        rows.apply_discovery(keys, [], now=0.0)
        for key in keys:
            rows.mark_shown(key)
        newest, oldest = "acme/widget#999", keys[-1]

        plan = rows.apply_discovery([newest, *keys], keys, now=10.0)
        self.assertEqual(plan.lease, [newest])
        self.assertEqual(plan.drop, [oldest])
        self.assertEqual(plan.gate, [])  # displaced, not waiting to exit
        self.assertEqual(len(plan.watched), 60)
        self.assertNotIn(oldest, plan.watched)
        self.assertEqual(rows.pending_exit, {})
        self.assertEqual(rows.total_open, 61)

        # The newest PR merges, so the displaced key is back in the newest 60. It
        # shows again with no pr_added.
        self.assertTrue(rows.mark_shown(newest))
        self.assertEqual(
            rows.resolve(newest, "MERGED", in_set=True, gate_lapsed=False),
            Resolution.RETIRE_TERMINAL,
        )
        back = rows.apply_discovery(keys, keys[:-1], now=20.0)
        self.assertEqual((back.lease, back.drop, back.gate), ([oldest], [], []))
        self.assertFalse(rows.mark_shown(oldest))

    def test_displaced_key_that_leaves_the_set_is_announced_if_it_returns(self) -> None:
        rows = MineRows(cap=1)
        rows.apply_discovery([A], [], now=0.0)
        rows.mark_shown(A)
        rows.apply_discovery([B, A], [A], now=10.0)  # A is pushed past the cap
        rows.apply_discovery([B], [], now=20.0)  # A merged while displaced
        rows.apply_discovery([A], [], now=30.0)  # reopened: a new row
        self.assertTrue(rows.mark_shown(A))

    def test_exiting_rows_keep_their_slot_under_the_cap(self) -> None:
        rows = MineRows(cap=2)
        rows.apply_discovery([A, B], [], now=0.0)
        rows.mark_shown(A)
        rows.mark_shown(B)
        # B leaves the set while two newer keys arrive. B keeps its slot, so only
        # the newest new key fits.
        plan = rows.apply_discovery([C, "acme/widget#9", A], [A, B], now=10.0)
        self.assertEqual(plan.gate, [B])
        self.assertEqual(plan.watched, [C, B])
        self.assertEqual(plan.lease, [C])
        self.assertEqual(plan.drop, [A])
        self.assertEqual(rows.total_open, 3)

    def test_pending_key_pushed_past_the_cap_is_dropped(self) -> None:
        rows = MineRows(cap=1)
        rows.apply_discovery([A], [], now=0.0)
        plan = rows.apply_discovery([B, A], [], now=10.0)
        self.assertEqual((plan.lease, plan.drop), ([B], [A]))
        self.assertEqual(rows.pending, {B})


class TestRecentRetired(unittest.TestCase):
    NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z

    def test_keeps_at_most_twenty_entries_newest_last(self) -> None:
        recent = RecentRetired()
        for n in range(RECENT_RETIRED_MAX + 5):
            recent.add(f"acme/widget#{n}", f"PR {n}", "MERGED", "2027-01-15T07:00:00Z")
        entries = recent.to_list()
        self.assertEqual(len(entries), RECENT_RETIRED_MAX)
        self.assertEqual(entries[0]["pr"], "acme/widget#5")
        self.assertEqual(
            entries[-1],
            {
                "pr": "acme/widget#24",
                "title": "PR 24",
                "state": "MERGED",
                "at": "2027-01-15T07:00:00Z",
            },
        )

    def test_prune_drops_entries_older_than_24_hours(self) -> None:
        self.assertEqual(RECENT_RETIRED_TTL_S, 86400)
        recent = RecentRetired(
            [
                {
                    "pr": A,
                    "title": "old",
                    "state": "MERGED",
                    "at": "2027-01-14T07:59:59Z",
                },
                {
                    "pr": B,
                    "title": "edge",
                    "state": "CLOSED",
                    "at": "2027-01-14T08:00:00Z",
                },
                {"pr": C, "title": "bad time", "state": "MERGED", "at": "yesterday"},
                "not an entry",  # type: ignore[list-item]
            ]
        )
        recent.prune(self.NOW)
        self.assertEqual([entry["pr"] for entry in recent.to_list()], [B])

    def test_a_pr_retired_twice_keeps_only_its_latest_entry(self) -> None:
        recent = RecentRetired()
        recent.add(A, "first", "CLOSED", "2027-01-15T06:00:00Z")
        recent.add(B, "other", "MERGED", "2027-01-15T06:30:00Z")
        recent.add(A, "second", "MERGED", "2027-01-15T07:00:00Z")
        self.assertEqual(
            [(e["pr"], e["title"]) for e in recent.to_list()],
            [(B, "other"), (A, "second")],
        )

    def test_to_list_returns_copies(self) -> None:
        recent = RecentRetired()
        recent.add(A, "t", "MERGED", "2027-01-15T07:00:00Z")
        recent.to_list()[0]["pr"] = "changed"
        self.assertEqual(recent.to_list()[0]["pr"], A)


if __name__ == "__main__":
    unittest.main()
