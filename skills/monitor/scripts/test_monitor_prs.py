"""Unit tests for monitor_prs.py."""

from __future__ import annotations

import copy
import io
import json
import os
import signal
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import packrat_source
from a2ui_board import review_board_data_model
from daemon_support import ControlReader
from models import Event, ReviewRole
from monitor_prs import Monitor, _LoopConfig, _Timers, parse_args
from state import MonitorState, load_state, save_state
from test_packrat_source import (
    FakePackratCli,
    _db,
    change,
    make_packrat_db,
    set_heartbeat,
    store_fixture_prs,
    store_graphql_pr,
)
from testing import FakeGh, load_fixture, make_snapshot

_REAL_PACKRAT_DB = packrat_source.DEFAULT_PACKRAT_DB


def setUpModule() -> None:
    # `--source auto` must never pick up a packrat database on the host running the tests.
    packrat_source.DEFAULT_PACKRAT_DB = Path(tempfile.gettempdir()) / "no-such-packrat.sqlite"


def tearDownModule() -> None:
    packrat_source.DEFAULT_PACKRAT_DB = _REAL_PACKRAT_DB


class FakeClock:
    """Controllable monotonic clock and sleep hook for deterministic loop tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += max(1.0, seconds)


class TestMonitorPrs(unittest.TestCase):
    def _run_monitor(
        self,
        fake_gh: FakeGh,
        argv: list[str],
        clock: FakeClock | None = None,
    ) -> tuple[int, str, str]:
        clk = clock or FakeClock()
        args = parse_args(argv)
        mon = Monitor(
            runner=fake_gh,
            state_path=args.state,
            clock=clk,
            sleep=clk.sleep,
            now=lambda: "2026-10-27T21:15:00Z",
            epoch_now=lambda: 1793135700.0,
        )
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            code = mon.run(args)
        return code, out_buf.getvalue(), err_buf.getvalue()

    def test_once_records_snapshot_and_second_run_exits_on_ci_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            out_path = str(Path(tmp) / "last.json")
            log_path = str(Path(tmp) / "events.jsonl")
            store_dir = str(Path(tmp) / "a2ui_store")

            # Run 1: initial open PR with 1 unresolved human thread -> surfaces once.
            fake1 = FakeGh([("graphql", 0, load_fixture("pr_open_ci_running.json"), "")])
            code1, stdout1, _ = self._run_monitor(
                fake1,
                [
                    "acme/widget#3339",
                    "--state",
                    state_path,
                    "--once",
                    "--conversation-id",
                    "conv-test-1",
                    "--a2ui-store-dir",
                    store_dir,
                    "--output",
                    out_path,
                    "--log",
                    log_path,
                ],
            )
            self.assertEqual(code1, 0)
            payload1 = json.loads(stdout1)
            self.assertEqual([e["kind"] for e in payload1["events"]], ["human_threads"])
            self.assertIn("a2ui", payload1)
            self.assertNotIn("dsl", payload1["a2ui"])
            self.assertTrue(payload1["a2ui"]["store_updated"])
            self.assertEqual(payload1["a2ui"]["store_status"], "written")
            self.assertTrue(Path(payload1["a2ui"]["dsl_file"]).exists())
            board_file = Path(store_dir) / "conversations" / "conv-test-1" / "monitor.jsonl"
            self.assertTrue(board_file.exists())
            self.assertTrue(Path(out_path).exists())
            self.assertTrue(Path(log_path).exists())

            # Reported thread watermark was saved after emit.
            saved = load_state(state_path)
            self.assertIn("PRRT_human_1", saved.reported_threads["acme/widget#3339"])

            # Run 2 (continuous, debounce=0): CI fails and review requests changes -> exits 0.
            fake2 = FakeGh([("graphql", 0, load_fixture("pr_ci_failed.json"), "")])
            code2, stdout2, _ = self._run_monitor(
                fake2,
                [
                    "acme/widget#3339",
                    "--state",
                    state_path,
                    "--debounce",
                    "0",
                    "--no-notifications",
                ],
            )
            self.assertEqual(code2, 0)
            payload2 = json.loads(stdout2)
            self.assertEqual(payload2["exit_reason"], "change")
            kinds2 = {e["kind"] for e in payload2["events"]}
            self.assertEqual(
                kinds2,
                {
                    "review_decision",
                    "review_requests",
                    "human_threads",
                    "merge_state",
                    "ci_check",
                },
            )

    def test_actionable_only_updates_pane_in_place_on_passive_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            store_dir = str(Path(tmp) / "a2ui_store")
            fake_seed = FakeGh([("graphql", 0, load_fixture("pr_open_ci_running.json"), "")])
            self._run_monitor(
                fake_seed,
                [
                    "acme/widget#3339",
                    "--state",
                    state_path,
                    "--once",
                    "--conversation-id",
                    "conv-passive",
                    "--a2ui-store-dir",
                    store_dir,
                ],
            )

            # Build a passive-only snapshot (draft toggled to True, thread already watermarked).
            passive_raw = json.loads(load_fixture("pr_open_ci_running.json"))
            passive_raw["data"]["pr0"]["isDraft"] = True

            # Refresh 1 (startup): passive-only change -> updates store in place, does not wake.
            # Refresh 2 (after poll): pr_ci_failed (actionable) -> wakes with exit 0!
            fake = FakeGh(
                [
                    ("graphql", 0, json.dumps(passive_raw), ""),
                    ("graphql", 0, load_fixture("pr_ci_failed.json"), ""),
                ]
            )
            code, stdout, _ = self._run_monitor(
                fake,
                [
                    "acme/widget#3339",
                    "--state",
                    state_path,
                    "--actionable-only",
                    "--debounce",
                    "0",
                    "--conversation-id",
                    "conv-passive",
                    "--a2ui-store-dir",
                    store_dir,
                    "--no-notifications",
                ],
            )
            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertTrue(all(ev["actionable"] for ev in payload["events"]))
            conv_dir = Path(store_dir) / "conversations" / "conv-passive"
            board_lines = (conv_dir / "monitor.jsonl").read_text(encoding="utf-8").splitlines()
            # The board file is replaced in place: seed create, passive tick, actionable emit.
            self.assertEqual(len(board_lines), 1)
            self.assertEqual(json.loads(board_lines[0])["seq"], 3)
            self.assertEqual((conv_dir / "_seq").read_text(encoding="utf-8").strip(), "3")

    def test_dismissed_board_returns_only_for_actionable_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = Path(tmp) / "a2ui_store"
            args = parse_args(
                [
                    "acme/widget#3339",
                    "--state",
                    str(Path(tmp) / "state.json"),
                    "--conversation-id",
                    "conv-dismiss",
                    "--a2ui-store-dir",
                    str(store_dir),
                ]
            )
            clk = FakeClock()
            mon = Monitor(
                runner=FakeGh([]),
                state_path=args.state,
                clock=clk,
                sleep=clk.sleep,
                now=lambda: "2026-10-27T21:15:00Z",
                epoch_now=lambda: 1793135700.0,
            )
            passive = Event(
                pr="acme/widget#3339",
                kind="draft",
                from_value=False,
                to_value=True,
                actionable=False,
                suggested_action=None,
                summary="Marked as draft",
            )
            actionable = Event(
                pr="acme/widget#3339",
                kind="ci_check",
                from_value=None,
                to_value="FAILURE",
                actionable=True,
                suggested_action="diagnose_ci",
                summary="CI failed (build)",
            )
            board_file = store_dir / "conversations" / "conv-dismiss" / "monitor.jsonl"

            # The first sync of the process creates the board.
            meta = mon._sync_board([], args)
            assert meta is not None
            self.assertEqual(meta["store_status"], "written")
            self.assertTrue(board_file.exists())

            # The user dismisses it: passive ticks leave it gone.
            board_file.unlink()
            for _ in range(2):
                meta = mon._sync_board([passive], args)
                assert meta is not None
                self.assertEqual(meta["store_status"], "dismissed")
                self.assertFalse(meta["store_updated"])
                self.assertFalse(board_file.exists())

            # An actionable event brings it back.
            meta = mon._sync_board([passive, actionable], args)
            assert meta is not None
            self.assertEqual(meta["store_status"], "written")
            self.assertTrue(meta["store_updated"])
            self.assertTrue(board_file.exists())

    def test_no_a2ui_omits_board_file_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            fake = FakeGh([("graphql", 0, load_fixture("pr_open_ci_running.json"), "")])
            code, stdout, _ = self._run_monitor(
                fake,
                ["acme/widget#3339", "--state", state_path, "--once", "--no-a2ui"],
            )
            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertNotIn("a2ui", payload)
            self.assertFalse((Path(tmp) / "board.a2ui").exists())

    def test_debounce_collects_second_change_and_beats_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            fake_seed = FakeGh([("graphql", 0, load_fixture("pr_open_ci_running.json"), "")])
            self._run_monitor(fake_seed, ["acme/widget#3339", "--state", state_path, "--once"])

            fake = FakeGh(
                [
                    ("graphql", 0, load_fixture("pr_ci_failed.json"), ""),
                    ("graphql", 0, load_fixture("pr_ci_failed.json"), ""),
                ]
            )
            code, stdout, _ = self._run_monitor(
                fake,
                [
                    "acme/widget#3339",
                    "--state",
                    state_path,
                    "--debounce",
                    "20",
                    "--timeout",
                    "15",
                    "--no-notifications",
                ],
            )
            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["exit_reason"], "change")
            self.assertTrue(len(payload["events"]) >= 1)

    def test_timeout_exits_2_when_no_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            fake_seed = FakeGh([("graphql", 0, load_fixture("pr_open_ci_running.json"), "")])
            self._run_monitor(fake_seed, ["acme/widget#3339", "--state", state_path, "--once"])

            fake = FakeGh(
                [
                    ("graphql", 0, load_fixture("pr_open_ci_running.json"), ""),
                    (
                        "notifications",
                        1,
                        load_fixture("notifications_304.txt"),
                        "gh: HTTP 304",
                    ),
                ]
            )
            code, stdout, _ = self._run_monitor(
                fake,
                [
                    "acme/widget#3339",
                    "--state",
                    state_path,
                    "--timeout",
                    "30",
                ],
            )
            self.assertEqual(code, 2)
            payload = json.loads(stdout)
            self.assertEqual(payload["exit_reason"], "timeout")
            self.assertEqual(payload["events"], [])

    def test_terminal_merged_and_all_green_exit_3(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            fake = FakeGh([("graphql", 0, load_fixture("pr_stack.json"), "")])
            code, stdout, _ = self._run_monitor(
                fake,
                [
                    "acme/widget#3338",
                    "acme/widget#3339",
                    "--state",
                    state_path,
                    "--until",
                    "all-green",
                ],
            )
            self.assertEqual(code, 3)
            payload = json.loads(stdout)
            self.assertEqual(payload["exit_reason"], "terminal")

    def test_partial_merged_wakes_actionable_only_with_exit_code_0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            stack_both_open = json.loads(load_fixture("pr_stack.json"))
            stack_both_open["data"]["pr0"]["pullRequest"]["state"] = "OPEN"
            stack_both_open["data"]["pr0"]["pullRequest"]["merged"] = False
            stack_both_open["data"]["pr1"]["pullRequest"]["baseRefName"] = "main"
            fake_seed = FakeGh([("graphql", 0, json.dumps(stack_both_open), "")])
            self._run_monitor(
                fake_seed,
                [
                    "acme/widget#3338",
                    "acme/widget#3339",
                    "--state",
                    state_path,
                    "--once",
                ],
            )

            # Now sibling pr0 (#3338) merges while pr1 (#3339) stays OPEN.
            sibling_merged = json.loads(load_fixture("pr_stack.json"))
            sibling_merged["data"]["pr1"]["pullRequest"]["baseRefName"] = "main"
            fake_watch = FakeGh([("graphql", 0, json.dumps(sibling_merged), "")])
            code, stdout, _ = self._run_monitor(
                fake_watch,
                [
                    "acme/widget#3338",
                    "acme/widget#3339",
                    "--state",
                    state_path,
                    "--actionable-only",
                    "--debounce",
                    "0",
                    "--no-notifications",
                ],
            )
            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["exit_reason"], "change")
            self.assertEqual(len(payload["events"]), 1)
            term_ev = payload["events"][0]
            self.assertEqual(term_ev["kind"], "terminal")
            self.assertEqual(term_ev["pr"], "acme/widget#3338")
            self.assertTrue(term_ev["actionable"])
            self.assertEqual(term_ev["suggested_action"], "celebrate")

    def test_event_filter_pushed_sha_and_partial_notification_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            fake_seed = FakeGh([("graphql", 0, load_fixture("pr_open_ci_running.json"), "")])
            self._run_monitor(fake_seed, ["acme/widget#3339", "--state", state_path, "--once"])

            fake = FakeGh(
                [
                    ("graphql", 0, load_fixture("pr_open_ci_running.json"), ""),
                    ("notifications", 0, load_fixture("notifications_200.txt"), ""),
                    ("graphql", 0, load_fixture("pr_ci_failed.json"), ""),
                ]
            )
            code, _, _ = self._run_monitor(
                fake,
                [
                    "acme/widget#3339",
                    "--state",
                    state_path,
                    "--events",
                    "terminal",
                    "--pushed-sha",
                    "acme/widget#3339=newsha123",
                    "--mark-rerun",
                    "acme/widget#3339=test-web@newsha123",
                    "--timeout",
                    "70",
                ],
            )
            self.assertEqual(code, 2)
            saved = load_state(state_path)
            self.assertEqual(saved.pushed_shas["acme/widget#3339"], "newsha123")
            self.assertEqual(saved.reruns["acme/widget#3339"], ["test-web@newsha123"])

    def test_stack_expansion_and_repeated_gh_error_exits_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            stack_fixture = json.loads(load_fixture("pr_stack.json"))
            child_only = {
                "data": {
                    "viewer": {"login": "alice"},
                    "pr0": stack_fixture["data"]["pr1"],
                }
            }
            parent_list = json.dumps(
                [
                    {
                        "number": 3338,
                        "headRefName": "feat/store-writer",
                        "baseRefName": "main",
                        "state": "MERGED",
                    }
                ]
            )
            fake = FakeGh(
                [
                    ("graphql", 0, json.dumps(child_only), ""),
                    ("--head feat/store-writer", 0, parent_list, ""),
                    ("--base feat/decision-planner", 0, "[]", ""),
                    ("graphql", 0, load_fixture("pr_stack.json"), ""),
                ]
            )
            code, stdout, _ = self._run_monitor(
                fake,
                ["acme/widget#3339", "--stack", "--once", "--state", state_path],
            )
            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(list(payload["prs"].keys()), ["acme/widget#3338", "acme/widget#3339"])

            # Startup failure -> exit 1.
            fake_err = FakeGh([("graphql", 1, "", "HTTP 500")])
            code_err, _, stderr_err = self._run_monitor(
                fake_err, ["acme/widget#3339", "--once", "--state", state_path]
            )
            self.assertEqual(code_err, 1)
            self.assertIn("error:", stderr_err)

            # Mid-loop 5 consecutive failures -> exit 1.
            fake_loop_err = FakeGh(
                [
                    ("graphql", 0, load_fixture("pr_open_ci_running.json"), ""),
                    ("graphql", 1, "", "err1"),
                    ("graphql", 1, "", "err2"),
                    ("graphql", 1, "", "err3"),
                    ("graphql", 1, "", "err4"),
                    ("graphql", 1, "", "err5"),
                ]
            )
            code_loop, _, _ = self._run_monitor(
                fake_loop_err,
                [
                    "acme/widget#3339",
                    "--state",
                    state_path,
                    "--events",
                    "terminal",
                    "--no-notifications",
                ],
            )
            self.assertEqual(code_loop, 1)

    def test_sigterm_saves_state_and_exits_143_without_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            fake = FakeGh([("graphql", 0, load_fixture("pr_open_ci_running.json"), "")])
            args = parse_args(
                [
                    "acme/widget#3339",
                    "--state",
                    state_path,
                    "--events",
                    "terminal",
                    "--no-notifications",
                ]
            )
            clk = FakeClock()

            def interrupt_on_sleep(seconds: float) -> None:
                clk.sleep(seconds)
                signal.raise_signal(signal.SIGTERM)

            mon = Monitor(
                runner=fake,
                state_path=state_path,
                clock=clk,
                sleep=interrupt_on_sleep,
                now=lambda: "2026-10-27T21:15:00Z",
            )
            out_buf = io.StringIO()
            with redirect_stdout(out_buf), redirect_stderr(io.StringIO()):
                code = mon.run(args)
            self.assertEqual(code, 143)
            self.assertEqual(out_buf.getvalue(), "")
            self.assertTrue(Path(state_path).exists())


def _two_open_prs() -> dict[str, Any]:
    """Returns the ``pr_stack.json`` payload with both PRs open, mergeable, and based on main."""
    raw = json.loads(load_fixture("pr_stack.json"))
    first = raw["data"]["pr0"]["pullRequest"]
    first.update(
        state="OPEN",
        merged=False,
        mergedAt=None,
        mergeable="MERGEABLE",
        mergeStateStatus="CLEAN",
    )
    raw["data"]["pr1"]["pullRequest"]["baseRefName"] = "main"
    return raw


def _only(raw: dict[str, Any], alias: str) -> str:
    """Builds a single-PR response whose ``pr0`` alias is ``raw``'s ``alias`` node."""
    return json.dumps({"data": {"viewer": raw["data"]["viewer"], "pr0": raw["data"][alias]}})


def _graphql_queries(fake: FakeGh) -> list[str]:
    return [" ".join(args) for args, _ in fake.calls if "graphql" in args]


NOT_MODIFIED = (
    "notifications",
    1,
    load_fixture("notifications_304.txt"),
    "gh: HTTP 304",
)
TWO_PRS = ["acme/widget#3338", "acme/widget#3339"]


class TestTargetedRefresh(unittest.TestCase):
    """``--poll`` refetches only flagged PRs; ``--sweep`` refetches every watched PR."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = str(Path(self.tmp.name) / "state.json")
        self.raw = _two_open_prs()
        seed = FakeGh([("graphql", 0, json.dumps(self.raw), "")])
        self._run(seed, [*TWO_PRS, "--once"])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, fake: FakeGh, extra: list[str]) -> tuple[int, str]:
        args = parse_args([*extra, "--state", self.state_path, "--no-a2ui"])
        clk = FakeClock()
        mon = Monitor(
            runner=fake,
            state_path=args.state,
            clock=clk,
            sleep=clk.sleep,
            now=lambda: "2026-10-27T21:15:00Z",
            epoch_now=lambda: 1793135700.0,
            notifier=None,
        )
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = mon.run(args)
        return code, err.getvalue()

    def _fake(self, *extra: tuple[str, int, str, str]) -> FakeGh:
        fake = FakeGh([("graphql", 0, json.dumps(self.raw), "")])
        for item in extra:
            fake.add(*item)
        for _ in range(12):
            fake.add(*NOT_MODIFIED)
        return fake

    def test_idle_cycle_makes_no_graphql_calls(self) -> None:
        fake = self._fake()
        code, _ = self._run(fake, [*TWO_PRS, "--timeout", "250"])
        self.assertEqual(code, 2)
        self.assertEqual(len(_graphql_queries(fake)), 1)  # startup refresh only
        notification_calls = [a for a, _ in fake.calls if "notifications?per_page=50&all=true" in a]
        self.assertEqual(len(notification_calls), 4)

    def test_notification_refreshes_only_the_named_pr(self) -> None:
        fake = FakeGh(
            [
                ("graphql", 0, json.dumps(self.raw), ""),
                ("notifications", 0, load_fixture("notifications_200.txt"), ""),
                ("graphql", 0, _only(self.raw, "pr1"), ""),
            ]
        )
        code, _ = self._run(fake, [*TWO_PRS, "--timeout", "70"])
        self.assertEqual(code, 2)
        queries = _graphql_queries(fake)
        self.assertEqual(len(queries), 2)
        self.assertIn("pullRequest(number: 3339)", queries[1])
        self.assertNotIn("pullRequest(number: 3338)", queries[1])

    def test_sweep_refreshes_every_watched_pr(self) -> None:
        fake = self._fake(("graphql", 0, json.dumps(self.raw), ""))
        code, _ = self._run(fake, [*TWO_PRS, "--sweep", "300", "--timeout", "310"])
        self.assertEqual(code, 2)
        queries = _graphql_queries(fake)
        self.assertEqual(len(queries), 2)
        self.assertIn("pullRequest(number: 3338)", queries[1])
        self.assertIn("pullRequest(number: 3339)", queries[1])

    def test_no_notifications_does_full_refresh_every_poll(self) -> None:
        fake = FakeGh([("graphql", 0, json.dumps(self.raw), "") for _ in range(3)])
        code, _ = self._run(fake, [*TWO_PRS, "--no-notifications", "--timeout", "250"])
        self.assertEqual(code, 2)
        queries = _graphql_queries(fake)
        self.assertEqual(len(queries), 3)
        for query in queries[1:]:
            self.assertIn("pullRequest(number: 3338)", query)
            self.assertIn("pullRequest(number: 3339)", query)
        self.assertFalse(any("notifications" in a for a, _ in fake.calls))

    def test_unknown_mergeable_refreshes_only_its_own_key(self) -> None:
        unknown = copy.deepcopy(self.raw)
        unknown["data"]["pr0"]["pullRequest"]["mergeable"] = "UNKNOWN"
        self._run(FakeGh([("graphql", 0, json.dumps(unknown), "")]), [*TWO_PRS, "--once"])
        fake = FakeGh(
            [
                ("graphql", 0, json.dumps(unknown), ""),
                ("graphql", 0, _only(self.raw, "pr0"), ""),
            ]
        )
        code, _ = self._run(fake, [*TWO_PRS, "--timeout", "15", "--events", "terminal"])
        self.assertEqual(code, 2)
        queries = _graphql_queries(fake)
        self.assertEqual(len(queries), 2)
        self.assertIn("pullRequest(number: 3338)", queries[1])
        self.assertNotIn("pullRequest(number: 3339)", queries[1])

    def test_graphql_cost_is_logged_to_stderr(self) -> None:
        priced = copy.deepcopy(self.raw)
        priced["data"]["rateLimit"] = {"cost": 2, "remaining": 4000, "resetAt": "x"}
        _, err = self._run(FakeGh([("graphql", 0, json.dumps(priced), "")]), [*TWO_PRS, "--once"])
        self.assertIn("graphql cost=2 remaining=4000", err)


class TestSelectRefreshTargets(unittest.TestCase):
    """Direct checks of which keys a loop step flags."""

    def setUp(self) -> None:
        self.mon = Monitor(runner=FakeGh([]), state_path="/nonexistent/state.json")
        self.mon.watched_keys = ["acme/widget#1", "acme/widget#2", "acme/widget#3"]
        for number in (1, 2, 3):
            snap = make_snapshot(number=number, rollup_state="SUCCESS")
            self.mon.state.snapshots[snap.key] = snap
        self.cfg = _LoopConfig(
            poll_s=120, sweep_s=600, notify_s=60, debounce_s=20, allowed_kinds=None
        )

    def _timers(self, **overrides: float) -> _Timers:
        values = {"next_poll": 1120.0, "next_sweep": 1600.0, "next_notify": 1060.0}
        values.update(overrides)
        return _Timers(**values)

    def _event(self, pr: str) -> Event:
        return Event(
            pr=pr,
            kind="ci_check",
            from_value=None,
            to_value="FAILURE",
            actionable=True,
            suggested_action=None,
            summary="lint failed",
        )

    def test_idle_step_flags_nothing(self) -> None:
        self.assertEqual(self.mon._select_refresh_targets(1010.0, self._timers(), [], self.cfg), [])

    def test_unknown_mergeable_flags_only_its_key(self) -> None:
        self.mon.state.snapshots["acme/widget#2"].mergeable = "UNKNOWN"
        targets = self.mon._select_refresh_targets(1010.0, self._timers(), [], self.cfg)
        self.assertEqual(targets, ["acme/widget#2"])

    def test_pending_debounce_flags_only_its_keys(self) -> None:
        accumulated = [self._event("acme/widget#3"), self._event("other/repo#9")]
        targets = self.mon._select_refresh_targets(1010.0, self._timers(), accumulated, self.cfg)
        self.assertEqual(targets, ["acme/widget#3"])

    def test_pending_rollup_flagged_when_poll_due(self) -> None:
        self.mon.state.snapshots["acme/widget#1"].rollup_state = "PENDING"
        timers = self._timers(next_notify=2000.0)
        self.assertEqual(self.mon._select_refresh_targets(1010.0, timers, [], self.cfg), [])
        targets = self.mon._select_refresh_targets(1120.0, timers, [], self.cfg)
        self.assertEqual(targets, ["acme/widget#1"])
        self.assertEqual(timers.next_poll, 1240.0)

    def test_expected_rollup_flagged_when_poll_due(self) -> None:
        self.mon.state.snapshots["acme/widget#3"].rollup_state = "EXPECTED"
        timers = self._timers(next_notify=2000.0)
        targets = self.mon._select_refresh_targets(1120.0, timers, [], self.cfg)
        self.assertEqual(targets, ["acme/widget#3"])

    def test_sweep_due_returns_full_refresh(self) -> None:
        timers = self._timers(next_sweep=1000.0)
        self.assertIsNone(self.mon._select_refresh_targets(1000.0, timers, [], self.cfg))

    def test_backoff_suppresses_refresh(self) -> None:
        self.mon.state.snapshots["acme/widget#2"].mergeable = "UNKNOWN"
        timers = self._timers(next_sweep=1000.0, backoff_until=1030.0)
        self.assertEqual(self.mon._select_refresh_targets(1010.0, timers, [], self.cfg), [])

    def test_control_pushed_sha_flags_key_until_refreshed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp) / "control.jsonl"
            control.write_text('{"pushed_sha": {"acme/widget#1": "c0ffee"}}\n', encoding="utf-8")
            self.mon.control_reader = ControlReader(control)
            self.mon._process_control(None)
        timers = self._timers(next_notify=2000.0)
        targets = self.mon._select_refresh_targets(1010.0, timers, [], self.cfg)
        self.assertEqual(targets, ["acme/widget#1"])
        self.mon._pushed_keys.difference_update(targets)
        self.assertEqual(self.mon._select_refresh_targets(1011.0, timers, [], self.cfg), [])


class FakeNotifier:
    def __init__(self, succeed: bool = True) -> None:
        self.succeed = succeed
        self.calls: list[dict[str, Any]] = []
        self.argv_prefix = ("fake-agentapi", "send-message")

    def send(self, conversation_id: str, title: str, body: str, timeout_s: float = 15.0) -> bool:
        self.calls.append({"conversation_id": conversation_id, "title": title, "body": body})
        return self.succeed


class TestDaemonMode(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)
        self.state_path = str(self.state_dir / "state.json")
        self.control_path = self.state_dir / "control.jsonl"
        self.armed_path = self.state_dir / "armed.json"
        self.output_path = str(self.state_dir / "last.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_daemon_already_running_returns_code_4(self) -> None:
        import fcntl

        lock_file = self.state_dir / "daemon.lock"
        pid_file = self.state_dir / "daemon.pid"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        pid_file.write_text("99999\n")
        fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            fake = FakeGh([("graphql", 0, load_fixture("pr_open_ci_running.json"), "")])
            args = parse_args(
                [
                    "acme/widget#3339",
                    "--state",
                    self.state_path,
                    "--conversation-id",
                    "conv-test",
                    "--daemon",
                    "--output",
                    self.output_path,
                    "--no-notifications",
                ]
            )
            mon = Monitor(runner=fake, state_path=self.state_path, notifier=FakeNotifier())
            out_buf = io.StringIO()
            with redirect_stdout(out_buf), redirect_stderr(io.StringIO()):
                code = mon.run(args)
            self.assertEqual(code, 4)
            data = json.loads(Path(self.output_path).read_text())
            self.assertEqual(data["exit_reason"], "already_running")
            self.assertEqual(data["pid"], 99999)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_daemon_notifies_and_respects_ack_and_stop(self) -> None:
        # Fixture sequence:
        # 1. initial refresh: ci running
        # 2. refresh step 1: ci failed -> actionable event -> should notify batch 1
        # 3. refresh step 2: another check failed -> should be held because awaiting ack
        # 4. write ack and stop to control -> unblocks gate, stops cleanly
        responses = [
            ("graphql", 0, load_fixture("pr_open_ci_running.json"), ""),
            ("graphql", 0, load_fixture("pr_ci_failed.json"), ""),
            ("graphql", 0, load_fixture("pr_ci_failed.json"), ""),
        ]
        fake = FakeGh(responses)
        notifier = FakeNotifier(succeed=True)
        clk = FakeClock()
        args = parse_args(
            [
                "acme/widget#3339",
                "--state",
                self.state_path,
                "--conversation-id",
                "conv-test",
                "--daemon",
                "--debounce",
                "0",
                "--poll",
                "10",
                "--output",
                self.output_path,
                "--no-notifications",
            ]
        )

        sleep_count = 0

        def sleep_and_feed_control(seconds: float) -> None:
            nonlocal sleep_count
            clk.sleep(seconds)
            sleep_count += 1
            if sleep_count == 2:
                # Write ack for batch 1 and stop
                self.control_path.write_text('{"ack": 1}\n{"stop": true}\n')

        mon = Monitor(
            runner=fake,
            state_path=self.state_path,
            clock=clk,
            sleep=sleep_and_feed_control,
            now=lambda: "2026-11-03T18:00:00Z",
            notifier=notifier,
        )

        out_buf = io.StringIO()
        with redirect_stdout(out_buf), redirect_stderr(io.StringIO()):
            code = mon.run(args)

        self.assertEqual(code, 0)
        self.assertFalse(self.armed_path.exists())  # disarmed on stop
        self.assertEqual(len(notifier.calls), 1)
        self.assertEqual(notifier.calls[0]["conversation_id"], "conv-test")
        self.assertIn("Batch 1", notifier.calls[0]["title"])
        self.assertIn("echo '{\"ack\": 1}'", notifier.calls[0]["body"])

        data = json.loads(Path(self.output_path).read_text())
        self.assertEqual(data["exit_reason"], "stopped")

    def test_daemon_scope_mine_filters_non_owned_prs(self) -> None:
        # Fixture with viewer != author (viewer is "bob", author is "alice")
        # In pr_ci_failed, viewer is also "bob" (mine == False)
        # Verify that when mine is False, notification is NOT sent for non-terminal events
        running_json = load_fixture("pr_open_ci_running.json").replace('"alice"', '"bob"', 1)
        failed_json = load_fixture("pr_ci_failed.json").replace('"alice"', '"bob"', 1)
        responses = [
            ("graphql", 0, running_json, ""),
            ("graphql", 0, failed_json, ""),
        ]
        fake = FakeGh(responses)
        notifier = FakeNotifier(succeed=True)
        clk = FakeClock()
        args = parse_args(
            [
                "acme/widget#3339",
                "--state",
                self.state_path,
                "--conversation-id",
                "conv-test",
                "--daemon",
                "--debounce",
                "0",
                "--notify-scope",
                "mine",
                "--poll",
                "10",
                "--output",
                self.output_path,
                "--no-notifications",
            ]
        )

        sleep_count = 0

        def sleep_and_stop(seconds: float) -> None:
            nonlocal sleep_count
            clk.sleep(seconds)
            sleep_count += 1
            if sleep_count == 1:
                self.control_path.write_text('{"stop": true}\n')

        mon = Monitor(
            runner=fake,
            state_path=self.state_path,
            clock=clk,
            sleep=sleep_and_stop,
            now=lambda: "2026-11-03T18:00:00Z",
            notifier=notifier,
        )

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = mon.run(args)

        self.assertEqual(code, 0)
        # Notification was NOT sent because mine == False and event was not terminal
        self.assertEqual(len(notifier.calls), 0)

    def test_daemon_requires_conversation_id(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(["acme/widget#3339", "--state", self.state_path, "--daemon"])

    def test_update_debounce_deadline_reschedules_when_past(self) -> None:
        clk = FakeClock()
        mon = Monitor(
            runner=FakeGh([]),
            state_path=self.state_path,
            clock=clk,
            notifier=FakeNotifier(),
        )
        events = [
            Event(
                pr="3339",
                kind="ci_failed",
                from_value="pending",
                to_value="failed",
                actionable=True,
                suggested_action=None,
                summary="lint failed",
            )
        ]
        new_deadline = mon._update_debounce_deadline(
            accumulated=events,
            debounce_deadline=100.0,
            now=150.0,
            poll_s=10,
            is_daemon=True,
        )
        self.assertEqual(new_deadline, 160.0)

        self.assertIsNone(
            mon._update_debounce_deadline(
                accumulated=[],
                debounce_deadline=100.0,
                now=150.0,
                poll_s=10,
                is_daemon=True,
            )
        )

    def test_daemon_dispatch_failure_retains_events_and_resets_gate(self) -> None:
        responses = [
            ("graphql", 0, load_fixture("pr_open_ci_running.json"), ""),
            ("graphql", 0, load_fixture("pr_ci_failed.json"), ""),
        ]
        fake = FakeGh(responses)
        notifier = FakeNotifier(succeed=False)
        clk = FakeClock()
        args = parse_args(
            [
                "acme/widget#3339",
                "--state",
                self.state_path,
                "--conversation-id",
                "conv-test",
                "--daemon",
                "--debounce",
                "0",
                "--output",
                self.output_path,
                "--no-notifications",
            ]
        )

        sleep_count = 0

        def sleep_and_stop(seconds: float) -> None:
            nonlocal sleep_count
            clk.sleep(seconds)
            sleep_count += 1
            if sleep_count == 1:
                self.control_path.write_text('{"stop": true}\n')

        mon = Monitor(
            runner=fake,
            state_path=self.state_path,
            clock=clk,
            sleep=sleep_and_stop,
            now=lambda: "2026-11-03T18:00:00Z",
            notifier=notifier,
        )

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = mon.run(args)

        self.assertEqual(code, 0)
        self.assertIsNone(mon.gate.awaiting)
        self.assertGreater(mon._next_send_retry, 0.0)


class TestPackratMode(unittest.TestCase):
    """``--source packrat|auto``: reads packrat's views and its change feed, not GitHub."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = make_packrat_db(self.dir / "repos.sqlite")
        self.state_path = str(self.dir / "state.json")
        self.clk = FakeClock()
        self.cli = FakePackratCli(on_wait=self.clk.sleep)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _monitor(
        self,
        fake_gh: FakeGh,
        notifier: Any = None,
        sleep: Any = None,
        state_path: str | None = None,
    ) -> Monitor:
        return Monitor(
            runner=fake_gh,
            state_path=state_path or self.state_path,
            clock=self.clk,
            sleep=sleep or self.clk.sleep,
            now=lambda: "2026-10-27T21:15:00Z",
            epoch_now=lambda: 1793135700.0,
            notifier=notifier,
            packrat_runner=self.cli,
        )

    def _run(
        self, extra: list[str], fake_gh: FakeGh | None = None, source: str = "packrat"
    ) -> tuple[int, dict[str, Any] | None, str]:
        argv = [*extra, "--state", self.state_path, "--source", source, "--packrat-db"]
        args = parse_args([*argv, str(self.db)])
        mon = self._monitor(fake_gh or FakeGh())
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = mon.run(args)
        payload = json.loads(out.getvalue()) if out.getvalue().strip() else None
        return code, payload, err.getvalue()

    def test_once_reads_views_with_zero_gh_calls(self) -> None:
        store_fixture_prs(self.db, "pr_open_ci_running.json")
        code, payload, _ = self._run(["acme/widget#3339", "--once"])
        self.assertEqual(code, 0)
        assert payload is not None
        self.assertEqual(payload["source"], "packrat")
        self.assertEqual(payload["prs"]["acme/widget#3339"]["rollup_state"], "PENDING")
        self.assertTrue(payload["prs"]["acme/widget#3339"]["mine"])  # viewer from packrat
        self.assertIn("via packrat", (self.dir / "board.a2ui").read_text(encoding="utf-8"))
        self.assertEqual(len(self.cli.by_command("add")), 1)

    def test_auto_picks_packrat_only_when_heartbeat_is_fresh(self) -> None:
        store_fixture_prs(self.db, "pr_open_ci_running.json")
        _, payload, _ = self._run(["acme/widget#3339", "--once"], source="auto")
        self.assertEqual(payload and payload["source"], "packrat")

        set_heartbeat(self.db, 500)
        fake = FakeGh([("graphql", 0, load_fixture("pr_open_ci_running.json"), "")])
        _, payload, _ = self._run(["acme/widget#3339", "--once"], fake, source="auto")
        self.assertEqual(payload and payload["source"], "github")
        self.assertEqual(len(fake.calls), 1)

        code, payload, err = self._run(["acme/widget#3339", "--once"], FakeGh())
        self.assertEqual((code, payload), (1, None))
        self.assertIn("packrat serve is not running", err)

    def test_packrat_db_with_tilde_is_expanded_before_the_heartbeat_check(self) -> None:
        store_fixture_prs(self.db, "pr_open_ci_running.json")
        with unittest.mock.patch.dict(os.environ, {"HOME": str(self.dir)}):
            args = parse_args(
                ["acme/widget#3339", "--once", "--state", self.state_path, "--source",
                 "packrat", "--packrat-db", "~/repos.sqlite"]
            )  # fmt: skip
            mon = self._monitor(FakeGh())
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = mon.run(args)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["source"], "packrat")

    def test_change_feed_drives_events_without_gh(self) -> None:
        store_fixture_prs(self.db, "pr_open_ci_running.json")
        self._run(["acme/widget#3339", "--once"])

        def ci_fails() -> dict[str, Any]:
            store_fixture_prs(self.db, "pr_ci_failed.json")
            return change(11, "acme/widget#3339", "other/repo#5")

        self.cli.changes.append(ci_fails)
        code, payload, _ = self._run(["acme/widget#3339", "--debounce", "0", "--timeout", "120"])
        self.assertEqual(code, 0)
        assert payload is not None
        self.assertEqual(payload["exit_reason"], "change")
        self.assertIn("ci_check", {e["kind"] for e in payload["events"]})

    def test_feed_reset_takes_a_full_snapshot(self) -> None:
        store_fixture_prs(self.db, "pr_open_ci_running.json")
        self._run(["acme/widget#3339", "--once"])

        def reset() -> dict[str, Any]:
            store_fixture_prs(self.db, "pr_ci_failed.json")
            return {"reset": True, "seq": 50}

        self.cli.changes.append(reset)
        code, payload, _ = self._run(["acme/widget#3339", "--debounce", "0", "--timeout", "120"])
        self.assertEqual(code, 0)
        self.assertIn("ci_check", {e["kind"] for e in (payload or {})["events"]})

    def test_stale_heartbeat_falls_back_to_github_once(self) -> None:
        store_fixture_prs(self.db, "pr_open_ci_running.json")
        self._run(["acme/widget#3339", "--once"])

        def service_dies() -> dict[str, Any]:
            set_heartbeat(self.db, 999)
            return {"seq": 10, "changes": []}

        self.cli.changes.append(service_dies)
        fake = FakeGh([("graphql", 0, load_fixture("pr_ci_failed.json"), "")])
        code, payload, err = self._run(
            ["acme/widget#3339", "--debounce", "0", "--timeout", "120"], fake
        )
        self.assertEqual(code, 0)
        assert payload is not None
        self.assertEqual(payload["source"], "github")
        self.assertIn("ci_check", {e["kind"] for e in payload["events"]})
        self.assertEqual(err.count("falling back to direct GitHub polling"), 1)
        board = (self.dir / "board.a2ui").read_text(encoding="utf-8")
        self.assertIn("via GitHub (packrat unavailable)", board)

    def test_mirrored_stack_expansion_makes_no_gh_calls(self) -> None:
        stack = json.loads(load_fixture("pr_stack.json"))
        stack["data"]["pr0"]["pullRequest"].update(state="OPEN", merged=False)
        for alias in ("pr0", "pr1"):
            store_graphql_pr(self.db, stack["data"][alias]["pullRequest"])
        code, payload, _ = self._run(["acme/widget#3339", "--stack", "--once"])  # FakeGh()
        self.assertEqual(code, 0)
        self.assertEqual(list((payload or {})["prs"]), ["acme/widget#3338", "acme/widget#3339"])

    def test_items_repo_stack_walks_with_gh(self) -> None:
        stack = json.loads(load_fixture("pr_stack.json"))
        stack["data"]["pr0"]["pullRequest"].update(state="OPEN", merged=False)
        for alias in ("pr0", "pr1"):
            store_graphql_pr(self.db, stack["data"][alias]["pullRequest"], mode="items")
        child_only = {"data": {"viewer": {"login": "alice"}, "pr0": stack["data"]["pr1"]}}
        parent = [{"number": 3338, "headRefName": "feat/store-writer", "baseRefName": "main"}]
        fake = FakeGh(
            [
                ("graphql", 0, json.dumps(child_only), ""),
                ("--head feat/store-writer", 0, json.dumps(parent), ""),
                ("--base feat/decision-planner", 0, "[]", ""),
            ]
        )
        code, payload, _ = self._run(["acme/widget#3339", "--stack", "--once"], fake)
        self.assertEqual(code, 0)
        self.assertEqual(list((payload or {})["prs"]), ["acme/widget#3338", "acme/widget#3339"])
        self.assertEqual(len(fake.calls), 3)  # stack walk only; snapshots come from packrat

    def test_missing_pr_is_refreshed_then_fetched_with_gh_after_startup_wait(
        self,
    ) -> None:
        fake = FakeGh([("graphql", 0, load_fixture("pr_open_ci_running.json"), "")])
        code, payload, _ = self._run(["acme/widget#3339", "--once"], fake)
        self.assertEqual(code, 0)
        self.assertEqual(
            self.cli.by_command("refresh"),
            [["refresh", "--json", "--", "acme/widget#3339"]],
        )
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual((payload or {})["source"], "packrat")

    def test_daemon_arms_with_source_renews_lease_and_refreshes_pushed_sha(
        self,
    ) -> None:
        store_fixture_prs(self.db, "pr_open_ci_running.json")
        control = self.dir / "control.jsonl"
        seen_armed: list[dict[str, Any]] = []

        def push_then_stop() -> dict[str, Any]:
            seen_armed.append(json.loads((self.dir / "armed.json").read_text(encoding="utf-8")))
            control.write_text(
                '{"pushed_sha": {"acme/widget#3339": "c0ffee"}}\n{"stop": true}\n',
                encoding="utf-8",
            )
            self.clk.now += 400  # past the 5-minute lease renewal
            return {"seq": 10, "changes": []}

        self.cli.changes.append(push_then_stop)
        args = parse_args(
            ["acme/widget#3339", "--state", self.state_path, "--daemon", "--conversation-id",
             "conv-pr", "--source", "packrat", "--packrat-db", str(self.db), "--no-a2ui"]
        )  # fmt: skip
        mon = self._monitor(FakeGh(), notifier=FakeNotifier())
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = mon.run(args)
        self.assertEqual(code, 0)
        self.assertEqual(seen_armed[0]["source"], "packrat")
        self.assertEqual(seen_armed[0]["packrat_db"], str(self.db.resolve()))
        self.assertEqual(
            self.cli.by_command("refresh"),
            [["refresh", "--json", "--", "acme/widget#3339"]],
        )
        self.assertEqual(len(self.cli.by_command("renew")), 0)  # stop came first
        self.assertEqual(len(self.cli.by_command("remove")), 1)  # lease released on exit

    def test_lease_renewed_every_five_minutes(self) -> None:
        store_fixture_prs(self.db, "pr_open_ci_running.json")
        code, _, _ = self._run(["acme/widget#3339", "--events", "terminal", "--timeout", "650"])
        self.assertEqual(code, 2)
        self.assertEqual(len(self.cli.by_command("renew")), 2)  # at ~300 s and ~600 s

    def test_redirected_repo_slug_is_canonicalized_before_packrat_watch_add(
        self,
    ) -> None:
        store_fixture_prs(self.db, "pr_open_ci_running.json")
        fake = FakeGh([("graphql", 0, load_fixture("pr_open_ci_running.json"), "")])
        code, payload, _ = self._run(["--repo", "legacy/Widget", "3339", "--once"], fake)
        self.assertEqual(code, 0)
        assert payload is not None
        self.assertEqual(list(payload["prs"]), ["acme/widget#3339"])
        self.assertEqual(payload["prs"]["acme/widget#3339"]["repo"], "acme/widget")
        add_calls = self.cli.by_command("add")
        self.assertEqual(len(add_calls), 1)
        self.assertEqual(add_calls[0][-2:], ["--", "acme/widget#3339"])
        self.assertEqual(self.cli.by_command("refresh"), [])
        saved = load_state(self.state_path)
        self.assertEqual(list(saved.snapshots), ["acme/widget#3339"])


def _review_pr_node(
    number: int = 3339,
    *,
    repo: str = "acme/widget",
    author: str = "bob",
    requested_viewer: str | None = "alice",
    state: str = "OPEN",
    head_sha: str = "1111111",
    updated_at: str = "2026-10-27T21:00:00Z",
    reviews: list[dict[str, Any]] | None = None,
    opinionated: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    base = copy.deepcopy(
        json.loads(load_fixture("pr_open_ci_running.json"))["data"]["pr0"]["pullRequest"]
    )
    base["number"] = number
    base["url"] = f"https://github.com/{repo}/pull/{number}"
    base["repository"] = {"nameWithOwner": repo}
    base["state"] = state
    base["merged"] = state == "MERGED"
    base["author"] = {"login": author, "__typename": "User"}
    base["headRefOid"] = head_sha
    base["updatedAt"] = updated_at
    base["reviewRequests"] = {
        "nodes": (
            [{"requestedReviewer": {"__typename": "User", "login": requested_viewer}}]
            if requested_viewer
            else []
        )
    }
    base["reviews"] = {"nodes": reviews or []}
    base["latestOpinionatedReviews"] = {"nodes": opinionated or []}
    base["reviewThreads"] = {"nodes": []}
    return base


def _search_response(
    viewer: str = "alice",
    requested: list[tuple[str, int]] | None = None,
    reviewed: list[tuple[str, int]] | None = None,
) -> str:
    return json.dumps(
        {
            "data": {
                "viewer": {"login": viewer},
                "reviewRequested": {
                    "nodes": [
                        {"number": num, "repository": {"nameWithOwner": repo}}
                        for repo, num in (requested or [])
                    ]
                },
                "reviewed": {
                    "nodes": [
                        {"number": num, "repository": {"nameWithOwner": repo}}
                        for repo, num in (reviewed or [])
                    ]
                },
            }
        }
    )


def _fetch_response(viewer: str, *prs: dict[str, Any]) -> str:
    data: dict[str, Any] = {"viewer": {"login": viewer}}
    for idx, pr in enumerate(prs):
        data[f"pr{idx}"] = {"pullRequest": pr}
    return json.dumps({"data": data})


class TestReviewsMode(unittest.TestCase):
    """Tests for ``--reviews`` and ``--wake-on-review`` across packrat and GitHub modes."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = make_packrat_db(self.dir / "repos.sqlite", viewer="alice")
        self.state_path = str(self.dir / "state.json")
        self.clk = FakeClock()
        self.cli = FakePackratCli(on_wait=self.clk.sleep)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _add_viewer_item(self, role: str, key: str) -> None:
        with _db(self.db) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO v_viewer_items VALUES (?,?,?,?)",
                (role, key, "2026-10-27T21:00:00Z", "2026-10-27T21:00:00Z"),
            )

    def _remove_viewer_item(self, key: str) -> None:
        with _db(self.db) as conn:
            conn.execute("DELETE FROM v_viewer_items WHERE item_key = ?", (key,))

    def _monitor(
        self,
        fake_gh: FakeGh,
        notifier: Any = None,
        sleep: Any = None,
        state_path: str | None = None,
    ) -> Monitor:
        return Monitor(
            runner=fake_gh,
            state_path=state_path or self.state_path,
            clock=self.clk,
            sleep=sleep or self.clk.sleep,
            now=lambda: "2026-10-27T21:15:00Z",
            epoch_now=lambda: 1793135700.0,
            notifier=notifier,
            packrat_runner=self.cli,
        )

    def test_reviews_cli_flag_validation_and_defaults(self) -> None:
        for invalid in (
            ["--reviews", "--stack", "--state", self.state_path],
            ["--reviews", "--until", "all-green", "--state", self.state_path],
            ["--reviews", "acme/widget#1", "--state", self.state_path],
            ["--wake-on-review", "acme/widget#1", "--state", self.state_path],
            ["--reviews", "--events", "ci_check", "--state", self.state_path],
            ["acme/widget#1", "--events", "new_review_request", "--state", self.state_path],
        ):
            with (
                self.subTest(argv=invalid),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                parse_args(invalid)

        rev_args = parse_args(["--reviews", "--state", self.state_path])
        self.assertEqual(rev_args.role, ReviewRole.REVIEWER)
        self.assertEqual(rev_args.until, "none")
        self.assertIsNone(rev_args.repo)
        self.assertFalse(rev_args.wake_on_review)

        rev_repo = parse_args(
            ["--reviews", "--wake-on-review", "--repo", "acme/widget", "--state", self.state_path]
        )
        self.assertEqual(rev_repo.repo, "acme/widget")
        self.assertTrue(rev_repo.wake_on_review)

        author_args = parse_args(["acme/widget#1", "--state", self.state_path])
        self.assertEqual(author_args.role, ReviewRole.AUTHOR)
        self.assertEqual(author_args.until, "closed")

    def test_reviews_packrat_once_and_startup_pruning_with_zero_gh_calls(self) -> None:
        prior_state = MonitorState(
            snapshots={
                "acme/widget#5": make_snapshot(number=5, state="CLOSED", author="bob", mine=False),
                "acme/widget#6": make_snapshot(number=6, state="OPEN", author="bob", mine=False),
            }
        )
        save_state(Path(self.state_path), prior_state)

        pr_open = _review_pr_node(3339, repo="acme/widget", requested_viewer="alice")
        pr_closed = _review_pr_node(
            6, repo="acme/widget", requested_viewer=None, state="CLOSED"
        )
        store_graphql_pr(self.db, pr_open)
        store_graphql_pr(self.db, pr_closed)
        self._add_viewer_item("review_requested", "acme/widget#3339")

        args = parse_args(
            [
                "--reviews",
                "--once",
                "--state",
                self.state_path,
                "--source",
                "packrat",
                "--packrat-db",
                str(self.db),
            ]
        )
        fake_gh = FakeGh([])
        mon = self._monitor(fake_gh)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = mon.run(args)
        self.assertEqual(code, 0)
        self.assertEqual(len(fake_gh.calls), 0)
        payload = json.loads(out.getvalue())
        self.assertNotIn("acme/widget#5", payload["prs"])
        self.assertIn("acme/widget#3339", payload["prs"])
        self.assertIn("acme/widget#6", payload["prs"])
        kinds = {(e["pr"], e["kind"]) for e in payload["events"]}
        self.assertIn(("acme/widget#3339", "new_review_request"), kinds)
        self.assertIn(("acme/widget#6", "terminal"), kinds)
        board_dsl = (self.dir / "board.a2ui").read_text(encoding="utf-8")
        self.assertIn("Review Queue", board_dsl)
        self.assertIn('"canReview": true', board_dsl)

    def test_reviews_empty_queue_github_sets_viewer_without_fetch_prs(self) -> None:
        store_dir = self.dir / "a2ui_store"
        fake_gh = FakeGh([("graphql", 0, _search_response("alice", [], []), "")])
        args = parse_args(
            [
                "--reviews",
                "--once",
                "--source",
                "github",
                "--state",
                self.state_path,
                "--conversation-id",
                "conv-empty",
                "--a2ui-store-dir",
                str(store_dir),
            ]
        )
        mon = self._monitor(fake_gh)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = mon.run(args)
        self.assertEqual(code, 0)
        self.assertEqual(mon.viewer, "alice")
        self.assertEqual(len(fake_gh.calls), 1)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["prs"], {})
        board_dsl = (self.dir / "board.a2ui").read_text(encoding="utf-8")
        self.assertIn("Queue empty", board_dsl)
        pane_file = store_dir / "conversations" / "conv-empty" / "monitor.jsonl"
        self.assertTrue(pane_file.exists())
        self.assertIn("trigger_pr_review", pane_file.read_text(encoding="utf-8"))

    def test_reviews_packrat_to_github_fallback_when_heartbeat_stale(self) -> None:
        pr_node = _review_pr_node(3339, repo="acme/widget", requested_viewer="alice")
        store_graphql_pr(self.db, pr_node)
        self._add_viewer_item("review_requested", "acme/widget#3339")
        set_heartbeat(self.db, 200)

        fake_gh = FakeGh(
            [
                ("graphql", 0, _search_response("alice", [("acme/widget", 3339)]), ""),
                ("graphql", 0, _fetch_response("alice", pr_node), ""),
            ]
        )
        args = parse_args(
            [
                "--reviews",
                "--once",
                "--source",
                "auto",
                "--packrat-db",
                str(self.db),
                "--state",
                self.state_path,
            ]
        )
        mon = self._monitor(fake_gh)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = mon.run(args)
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["source"], "github")
        self.assertEqual(len(fake_gh.calls), 2)

    def test_reviews_mid_session_discovery_packrat_and_github(self) -> None:
        # 1. Packrat mid-session discovery via batch.viewer_changed
        pr10 = _review_pr_node(10, repo="acme/widget", requested_viewer="alice")
        pr20 = _review_pr_node(20, repo="other/repo", requested_viewer="alice")
        store_graphql_pr(self.db, pr10)
        self._add_viewer_item("review_requested", "acme/widget#10")

        seed_args = parse_args(
            [
                "--reviews",
                "--once",
                "--source",
                "packrat",
                "--packrat-db",
                str(self.db),
                "--state",
                self.state_path,
            ]
        )
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self._monitor(FakeGh([])).run(seed_args)

        def arrive_pr20() -> dict[str, Any]:
            store_graphql_pr(self.db, pr20)
            self._add_viewer_item("review_requested", "other/repo#20")
            return {
                "seq": 15,
                "changes": [
                    {
                        "seq": 15,
                        "item_key": "other/repo#20",
                        "kind": "viewer",
                        "fields": ["review_requested"],
                        "at": "2026-10-27T21:05:00Z",
                    }
                ],
            }

        self.cli.changes.append(arrive_pr20)
        loop_args = parse_args(
            [
                "--reviews",
                "--debounce",
                "0",
                "--timeout",
                "120",
                "--source",
                "packrat",
                "--packrat-db",
                str(self.db),
                "--state",
                self.state_path,
            ]
        )
        fake_zero = FakeGh([])
        out_pr = io.StringIO()
        with redirect_stdout(out_pr), redirect_stderr(io.StringIO()):
            code_pr = self._monitor(fake_zero).run(loop_args)
        self.assertEqual(code_pr, 0)
        self.assertEqual(len(fake_zero.calls), 0)
        payload_pr = json.loads(out_pr.getvalue())
        self.assertIn("other/repo#20", payload_pr["prs"])
        self.assertEqual(payload_pr["events"][0]["pr"], "other/repo#20")
        self.assertEqual(payload_pr["events"][0]["kind"], "new_review_request")

        # 2. GitHub mid-session discovery via NotificationPoller.review_requested_unwatched
        gh_state = str(self.dir / "gh_state.json")
        notif_unwatched = (
            "HTTP/2.0 200 OK\r\nLast-Modified: Tue, 27 Oct 2026 21:16:00 GMT\r\n"
            "X-Poll-Interval: 60\r\n\r\n"
            + json.dumps(
                [
                    {
                        "reason": "review_requested",
                        "repository": {"full_name": "other/repo"},
                        "subject": {
                            "type": "PullRequest",
                            "url": "https://api.github.com/repos/other/repo/pulls/20",
                        },
                    }
                ]
            )
        )
        fake_gh = FakeGh(
            [
                ("graphql", 0, _search_response("alice", [], []), ""),
                ("notifications", 0, notif_unwatched, ""),
                ("graphql", 0, _search_response("alice", [("other/repo", 20)], []), ""),
                ("graphql", 0, _fetch_response("alice", pr20), ""),
            ]
        )
        gh_args = parse_args(
            [
                "--reviews",
                "--source",
                "github",
                "--debounce",
                "0",
                "--timeout",
                "120",
                "--state",
                gh_state,
            ]
        )
        out_gh = io.StringIO()
        with redirect_stdout(out_gh), redirect_stderr(io.StringIO()):
            code_gh = self._monitor(fake_gh, state_path=gh_state).run(gh_args)
        self.assertEqual(code_gh, 0)
        payload_gh = json.loads(out_gh.getvalue())
        self.assertIn("other/repo#20", payload_gh["prs"])
        self.assertEqual(payload_gh["events"][0]["kind"], "new_review_request")

    def test_reviews_mid_session_close_retains_snapshot_unleases_and_reopens(self) -> None:
        pr_open = _review_pr_node(3339, repo="acme/widget", requested_viewer="alice")
        store_graphql_pr(self.db, pr_open)
        self._add_viewer_item("review_requested", "acme/widget#3339")

        seed_args = parse_args(
            [
                "--reviews",
                "--once",
                "--source",
                "packrat",
                "--packrat-db",
                str(self.db),
                "--state",
                self.state_path,
            ]
        )
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self._monitor(FakeGh([])).run(seed_args)

        pr_closed = _review_pr_node(
            3339, repo="acme/widget", requested_viewer="alice", state="CLOSED"
        )

        def close_pr() -> dict[str, Any]:
            store_graphql_pr(self.db, pr_closed)
            return change(12, "acme/widget#3339")

        self.cli.changes.append(close_pr)
        close_args = parse_args(
            [
                "--reviews",
                "--debounce",
                "0",
                "--timeout",
                "120",
                "--source",
                "packrat",
                "--packrat-db",
                str(self.db),
                "--state",
                self.state_path,
            ]
        )
        mon_close = self._monitor(FakeGh([]))
        out_close = io.StringIO()
        with redirect_stdout(out_close), redirect_stderr(io.StringIO()):
            code_close = mon_close.run(close_args)
        self.assertEqual(code_close, 0)
        self.assertIn("acme/widget#3339", mon_close.state.snapshots)
        self.assertEqual(mon_close.state.snapshots["acme/widget#3339"].state, "CLOSED")
        self.assertNotIn("acme/widget#3339", mon_close.watched_keys)
        remove_calls = [
            c for c in self.cli.by_command("remove") if "acme/widget#3339" in c
        ]
        self.assertEqual(len(remove_calls), 1)
        dm_closed = review_board_data_model(mon_close.state.snapshots, viewer="alice")
        self.assertEqual(dm_closed["items"][0]["step"], 0)
        self.assertEqual(dm_closed["items"][0]["state"], "Closed")
        self.assertFalse(dm_closed["items"][0]["canReview"])

        # Reopen recovery on a subsequent discovery sweep / viewer_changed
        pr_reopened = _review_pr_node(
            3339,
            repo="acme/widget",
            requested_viewer="alice",
            state="OPEN",
            head_sha="2222222",
        )
        store_graphql_pr(self.db, pr_reopened)
        mon_close.packrat = mon_close._select_source(close_args)
        assert mon_close.packrat is not None
        added, _ = mon_close._discover_packrat_review_delta()
        self.assertEqual(added, ["acme/widget#3339"])
        self.assertIn("acme/widget#3339", mon_close.watched_keys)
        mon_close.refresh(added)
        self.assertEqual(mon_close.state.snapshots["acme/widget#3339"].state, "OPEN")
        dm_reopened = review_board_data_model(mon_close.state.snapshots, viewer="alice")
        self.assertTrue(dm_reopened["items"][0]["canReview"])

    def test_reviews_daemon_suppresses_wakeups_unless_wake_on_review(self) -> None:
        pr_rev1 = _review_pr_node(
            3339,
            repo="acme/widget",
            requested_viewer=None,
            head_sha="1111111",
            reviews=[
                {
                    "author": {"login": "alice", "__typename": "User"},
                    "state": "CHANGES_REQUESTED",
                    "submittedAt": "2026-10-27T20:00:00Z",
                    "commit": {"oid": "1111111"},
                }
            ],
            opinionated=[
                {
                    "author": {"login": "alice", "__typename": "User"},
                    "state": "CHANGES_REQUESTED",
                    "submittedAt": "2026-10-27T20:00:00Z",
                }
            ],
        )
        pr_rev2 = _review_pr_node(
            3339,
            repo="acme/widget",
            requested_viewer=None,
            head_sha="2222222",
            reviews=pr_rev1["reviews"]["nodes"],
            opinionated=pr_rev1["latestOpinionatedReviews"]["nodes"],
        )
        store_graphql_pr(self.db, pr_rev1)
        self._add_viewer_item("reviewed", "acme/widget#3339")

        control = self.dir / "control.jsonl"
        log_path = self.dir / "events.jsonl"
        step = 0

        def push_and_stop() -> dict[str, Any]:
            nonlocal step
            step += 1
            if step == 1:
                store_graphql_pr(self.db, pr_rev2)
                return change(11, "acme/widget#3339")
            control.write_text('{"stop": true}\n', encoding="utf-8")
            return {"seq": 12, "changes": []}

        # 1. Default --reviews --daemon: silent background update (0 notifier calls)
        self.cli.changes.extend([push_and_stop, push_and_stop])
        notifier_silent = FakeNotifier()
        args_silent = parse_args(
            [
                "--reviews",
                "--daemon",
                "--debounce",
                "0",
                "--conversation-id",
                "conv-rev",
                "--source",
                "packrat",
                "--packrat-db",
                str(self.db),
                "--state",
                self.state_path,
                "--log",
                str(log_path),
            ]
        )
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code_silent = self._monitor(FakeGh([]), notifier=notifier_silent).run(args_silent)
        self.assertEqual(code_silent, 0)
        self.assertEqual(len(notifier_silent.calls), 0)
        last_silent = json.loads((self.dir / "last.json").read_text(encoding="utf-8"))
        self.assertEqual(last_silent["notify_mode"], "none")
        self.assertEqual(last_silent["events"][0]["kind"], "author_head_push")
        self.assertTrue(log_path.exists())

        # 2. With --wake-on-review: sends agentapi wake notification
        control.unlink(missing_ok=True)
        store_graphql_pr(self.db, pr_rev1)
        state_wake = str(self.dir / "wake" / "state.json")
        control_wake = self.dir / "wake" / "control.jsonl"
        step_wake = 0

        def push_and_stop_wake() -> dict[str, Any]:
            nonlocal step_wake
            step_wake += 1
            if step_wake == 1:
                store_graphql_pr(self.db, pr_rev2)
                return change(21, "acme/widget#3339")
            control_wake.parent.mkdir(parents=True, exist_ok=True)
            control_wake.write_text('{"stop": true}\n', encoding="utf-8")
            return {"seq": 22, "changes": []}

        self.cli.changes.extend([push_and_stop_wake, push_and_stop_wake])
        notifier_wake = FakeNotifier()
        args_wake = parse_args(
            [
                "--reviews",
                "--wake-on-review",
                "--daemon",
                "--debounce",
                "0",
                "--conversation-id",
                "conv-rev-wake",
                "--source",
                "packrat",
                "--packrat-db",
                str(self.db),
                "--state",
                state_wake,
            ]
        )
        mon_wake = Monitor(
            runner=FakeGh([]),
            state_path=state_wake,
            clock=self.clk,
            sleep=self.clk.sleep,
            now=lambda: "2026-10-27T21:15:00Z",
            epoch_now=lambda: 1793135700.0,
            notifier=notifier_wake,
            packrat_runner=self.cli,
        )
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code_wake = mon_wake.run(args_wake)
        self.assertEqual(code_wake, 0)
        self.assertEqual(len(notifier_wake.calls), 1)
        self.assertEqual(notifier_wake.calls[0]["conversation_id"], "conv-rev-wake")


if __name__ == "__main__":
    unittest.main()
