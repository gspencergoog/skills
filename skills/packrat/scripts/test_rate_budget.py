"""Unit tests for the header-based GitHub rate budget."""

from __future__ import annotations

import json
import multiprocessing
import tempfile
import time
import unittest
from pathlib import Path

from rate_budget import (
    RATE_BUDGET_FILENAME,
    Priority,
    RateBudget,
    RateSnapshot,
    parse_rate_headers,
)

RESET_AT = 2_000_000_000
BEFORE_RESET = RESET_AT - 600.0


def _headers(
    remaining: int, *, limit: int = 5000, resource: str = "graphql"
) -> dict[str, str]:
    """Builds a complete `X-RateLimit-*` header set using GitHub's mixed-case names."""
    return {
        "X-RateLimit-Resource": resource,
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(RESET_AT),
        "X-RateLimit-Used": str(limit - remaining),
    }


def _record_many(path: str, values: list[int]) -> None:
    """Records one graphql snapshot per value from a separate process."""
    budget = RateBudget(Path(path))
    for remaining in values:
        budget.record(_headers(remaining), worker="race")


class TestParseRateHeaders(unittest.TestCase):
    """Tests parsing of `X-RateLimit-*` headers."""

    def test_parses_case_insensitive_headers(self) -> None:
        snap = parse_rate_headers(_headers(4013), now=123.0)
        self.assertEqual(
            snap,
            RateSnapshot(
                resource="graphql",
                limit=5000,
                remaining=4013,
                reset_at=RESET_AT,
                observed_at=123.0,
            ),
        )
        lowered = {k.lower(): v for k, v in _headers(10).items()}
        parsed = parse_rate_headers(lowered, now=1.0)
        assert parsed is not None
        self.assertEqual(parsed.remaining, 10)

    def test_missing_or_malformed_headers_return_none(self) -> None:
        for key in (
            "X-RateLimit-Resource",
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
        ):
            headers = _headers(100)
            del headers[key]
            self.assertIsNone(parse_rate_headers(headers, now=0.0), key)
        bad = _headers(100)
        bad["X-RateLimit-Remaining"] = "lots"
        self.assertIsNone(parse_rate_headers(bad, now=0.0))
        self.assertIsNone(parse_rate_headers({}, now=0.0))


class TestRateBudget(unittest.TestCase):
    """Tests recording, expiry, and priority decisions."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / RATE_BUDGET_FILENAME
        self.budget = RateBudget(self.path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_record_without_headers_writes_nothing(self) -> None:
        self.assertIsNone(self.budget.record({"etag": "x"}, worker="sync"))
        self.assertFalse(self.path.exists())
        self.assertIsNone(self.budget.current("graphql", now=BEFORE_RESET))
        self.assertTrue(
            self.budget.allows("graphql", Priority.BACKFILL, now=BEFORE_RESET)
        )

    def test_record_persists_atomically_per_resource(self) -> None:
        self.budget.record(_headers(4000), worker="sync")
        self.budget.record(_headers(59, limit=60, resource="core"), worker="sync")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(data["resources"]), ["core", "graphql"])
        self.assertEqual(data["resources"]["graphql"]["remaining"], 4000)
        leftovers = [
            p.name for p in self.path.parent.iterdir() if p.name.endswith(".tmp")
        ]
        self.assertEqual(leftovers, [])
        other = RateBudget(self.path)
        snap = other.current("core", now=BEFORE_RESET)
        assert snap is not None
        self.assertEqual((snap.remaining, snap.limit), (59, 60))

    def test_lower_remaining_wins_within_window_and_newer_window_wins(self) -> None:
        self.budget.record(_headers(3000), worker="a")
        self.budget.record(_headers(3500), worker="b")  # stale response recorded late
        snap = self.budget.current("graphql", now=BEFORE_RESET)
        assert snap is not None
        self.assertEqual(snap.remaining, 3000)

        newer = _headers(4999)
        newer["X-RateLimit-Reset"] = str(RESET_AT + 3600)
        self.budget.record(newer, worker="a")
        snap = self.budget.current("graphql", now=BEFORE_RESET)
        assert snap is not None
        self.assertEqual((snap.remaining, snap.reset_at), (4999, RESET_AT + 3600))

    def test_equal_snapshot_in_same_window_skips_disk_write(self) -> None:
        self.budget.record(_headers(4000), worker="a")
        writes = 0
        orig_write = self.budget._write_all

        def counting_write(snapshots: dict[str, RateSnapshot]) -> None:
            nonlocal writes
            writes += 1
            orig_write(snapshots)

        self.budget._write_all = counting_write  # type: ignore[method-assign]
        self.budget.record(_headers(4000), worker="b")
        self.assertEqual(writes, 0)

        # Changing the limit with equal remaining and reset_at still supersedes.
        self.budget.record(_headers(4000, limit=10000), worker="b")
        self.assertEqual(writes, 1)
        snap = self.budget.current("graphql", now=BEFORE_RESET)
        assert snap is not None
        self.assertEqual(snap.limit, 10000)

    def test_reserve_and_floor_thresholds(self) -> None:
        cases = {
            # remaining: (HOT, VIEWER, MIRROR, BACKFILL)
            1000: (True, True, True, True),  # exactly 20%
            999: (True, True, False, False),  # below the reserve
            250: (True, True, False, False),  # exactly 5%
            249: (True, False, False, False),  # below the floor
            0: (True, False, False, False),
        }
        for remaining, expected in cases.items():
            budget = RateBudget(
                Path(self.tmp.name) / f"b{remaining}" / RATE_BUDGET_FILENAME
            )
            budget.record(_headers(remaining), worker="sync")
            got = tuple(budget.allows("graphql", p, now=BEFORE_RESET) for p in Priority)
            self.assertEqual(got, expected, remaining)

    def test_snapshot_expires_after_reset(self) -> None:
        self.budget.record(_headers(0), worker="sync")
        self.assertFalse(
            self.budget.allows("graphql", Priority.MIRROR, now=BEFORE_RESET)
        )
        self.assertIsNone(self.budget.current("graphql", now=RESET_AT))
        self.assertTrue(
            self.budget.allows("graphql", Priority.BACKFILL, now=RESET_AT + 1)
        )
        self.assertIn("graphql", self.budget.last_snapshots())

    def test_unknown_resource_and_corrupt_file_allow(self) -> None:
        self.budget.record(_headers(0), worker="sync")
        self.assertTrue(self.budget.allows("core", Priority.BACKFILL, now=BEFORE_RESET))
        self.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.budget.current("graphql", now=BEFORE_RESET))
        self.assertTrue(
            self.budget.allows("graphql", Priority.BACKFILL, now=BEFORE_RESET)
        )
        self.budget.record(_headers(10), worker="sync")
        self.assertIsNotNone(self.budget.current("graphql", now=BEFORE_RESET))

    def test_defaults_now_to_wall_clock(self) -> None:
        headers = _headers(0)
        headers["X-RateLimit-Reset"] = str(int(time.time()) + 3600)
        self.budget.record(headers, worker="sync")
        self.assertIsNotNone(self.budget.current("graphql"))
        self.assertFalse(self.budget.allows("graphql", Priority.VIEWER))

    def test_injected_clock_and_optional_worker(self) -> None:
        now = BEFORE_RESET
        budget = RateBudget(self.path, clock=lambda: now)
        snap = budget.record(_headers(100))
        assert snap is not None
        self.assertEqual(snap.observed_at, BEFORE_RESET)
        self.assertIsNotNone(budget.current("graphql"))
        self.assertFalse(budget.allows("graphql", Priority.VIEWER))

        now = float(RESET_AT + 10)
        self.assertIsNone(budget.current("graphql"))
        self.assertTrue(budget.allows("graphql", Priority.BACKFILL))

    def test_two_writers_racing_keep_valid_json_and_lowest_remaining(self) -> None:
        ctx = multiprocessing.get_context("spawn")
        procs = [
            ctx.Process(
                target=_record_many, args=(str(self.path), list(range(4000, 3800, -2)))
            ),
            ctx.Process(
                target=_record_many, args=(str(self.path), list(range(3999, 3799, -2)))
            ),
        ]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(timeout=30)
            self.assertEqual(proc.exitcode, 0)
        snap = self.budget.current("graphql", now=BEFORE_RESET)
        assert snap is not None
        self.assertEqual(snap.remaining, 3801)


if __name__ == "__main__":
    unittest.main()
