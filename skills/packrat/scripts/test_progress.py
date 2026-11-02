"""Unit tests for the adaptive stderr progress and diagnostic reporter."""

from __future__ import annotations

import io
import unittest

from progress import (
    NULL_REPORTER,
    StderrProgressReporter,
    format_duration,
    format_eta,
    format_status_line,
)


class FakeClock:
    """Deterministic monotonic clock for testing time-throttled progress emission."""

    def __init__(self, initial: float = 100.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now

    def step(self, delta: float) -> None:
        self.now += delta


class TestProgressReporter(unittest.TestCase):
    """Tests grace window suppression, 1s TTY throttling, 15s plain heartbeats, and warning/error clearing."""

    def test_format_helpers_and_null_reporter(self) -> None:
        self.assertEqual(format_duration(0.4), "0.4s")
        self.assertEqual(format_duration(12.2), "12s")
        self.assertEqual(format_duration(258.0), "4m18s")
        self.assertEqual(format_duration(3720.0), "1h02m")
        self.assertEqual(format_eta(10.0, 25, 100), "ETA 30s")
        self.assertEqual(format_eta(10.0, 100, 100), "")
        self.assertEqual(format_eta(10.0, 0, 100), "")

        line = format_status_line("octo-org/widgets", "github prs", 250, 1000, 30.0, "page 10")
        self.assertIn("[octo-org/widgets] github prs: 250/1,000 (25%)", line)
        self.assertIn("page 10", line)
        self.assertIn("ETA 1m30s", line)

        NULL_REPORTER.start_phase("octo-org/widgets", "git", 10)
        NULL_REPORTER.advance(5, 10, detail="a.py")
        NULL_REPORTER.warn("octo-org/widgets", "warn")
        NULL_REPORTER.error("octo-org/widgets", "err")
        NULL_REPORTER.clear_active_line()
        NULL_REPORTER.finish_phase("done")

    def test_grace_window_keeps_fast_operations_silent(self) -> None:
        clock = FakeClock()
        for is_tty in (True, False):
            buf = io.StringIO()
            rep = StderrProgressReporter("auto", stream=buf, is_tty=is_tty, clock=clock)
            rep.start_phase("octo-org/widgets", "github issues", total=100)
            clock.step(0.8)
            rep.advance(50, 100)
            clock.step(1.1)  # 1.9s total (< 2.0s grace_sec)
            rep.advance(100, 100)
            rep.finish_phase()
            self.assertEqual(buf.getvalue(), "")

    def test_tty_throttles_to_once_per_second_and_clears_before_warnings(self) -> None:
        clock = FakeClock()
        buf = io.StringIO()
        rep = StderrProgressReporter("auto", stream=buf, is_tty=True, clock=clock, max_width=120)

        rep.start_phase("octo-org/widgets", "github prs", total=100)
        clock.step(2.0)  # Reaches 2.0s grace window -> first emit
        rep.advance(10, 100, detail="page 1")
        clock.step(0.4)  # +0.4s (< 1.0s tty_interval_sec) -> suppressed
        rep.advance(20, 100, detail="page 2")
        clock.step(0.6)  # +1.0s since first emit -> second emit
        rep.advance(30, 100, detail="page 3")

        raw_mid = buf.getvalue()
        self.assertIn("page 1", raw_mid)
        self.assertNotIn("page 2", raw_mid)
        self.assertIn("page 3", raw_mid)

        # Emitting a warning clears the active \r line first
        rep.warn("octo-org/widgets", "retrying at pageSize=12")
        rep.finish_phase()
        out = buf.getvalue()
        self.assertIn("\r\x1b[K[octo-org/widgets] warning: retrying at pageSize=12\n", out)

    def test_plain_mode_throttles_to_15_seconds_without_ansi_escapes(self) -> None:
        clock = FakeClock()
        buf = io.StringIO()
        rep = StderrProgressReporter("auto", stream=buf, is_tty=False, clock=clock)

        rep.start_phase("octo-org/widgets", "embeddings (deterministic-384-v2)", total=1000)
        clock.step(2.5)  # First heartbeat after grace window
        rep.advance(100, 1000)
        clock.step(10.0)  # Suppressed (< 15s)
        rep.advance(400, 1000)
        clock.step(5.0)  # +15.0s since first heartbeat -> second heartbeat
        rep.advance(600, 1000)
        rep.finish_phase()

        out = buf.getvalue()
        self.assertNotIn("\r", out)
        self.assertNotIn("\x1b", out)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("100/1,000 (10%)", lines[0])
        self.assertIn("600/1,000 (60%)", lines[1])

    def test_quiet_and_off_modes_suppress_progress_and_keep_errors(self) -> None:
        clock = FakeClock()
        buf = io.StringIO()
        rep = StderrProgressReporter("off", quiet=True, stream=buf, is_tty=True, clock=clock)

        rep.start_phase("octo-org/widgets", "github prs", total=50)
        clock.step(5.0)
        rep.advance(25, 50)
        rep.warn("octo-org/widgets", "transient timeout")
        rep.error("octo-org/widgets", "HTTP 401 Bad credentials")
        rep.finish_phase("done")

        self.assertEqual(buf.getvalue(), "[octo-org/widgets] error: HTTP 401 Bad credentials\n")


if __name__ == "__main__":
    unittest.main()
