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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import packrat_source
from a2ui_board import review_board_data_model
from daemon_support import ControlReader
from gh_client import GhError
from models import Event, Membership, ReviewRole, parse_iso_seconds
from monitor_prs import Monitor, _LoopConfig, _Timers, parse_args
from state import MonitorState, load_state, save_state
from test_packrat_source import (
    FakePackratCli,
    FakeReaderRunner,
    _db,
    _live_row,
    change,
    make_packrat_db,
    set_failed_ticks,
    set_heartbeat,
    set_synced_sets,
    store_fixture_prs,
    store_graphql_pr,
)
from testing import FakeGh, load_fixture, make_snapshot

_REAL_PACKRAT_DB = packrat_source.DEFAULT_PACKRAT_DB
_REAL_SUBPROCESS_PACKRAT = packrat_source.SubprocessPackrat


def _disabled_subprocess_packrat(_db_path: Path = _REAL_PACKRAT_DB) -> Any:
    def _fail(_args: list[str], _timeout: float = 30.0) -> tuple[int, str, str]:
        return 1, "", "packrat subprocess disabled in unit tests"

    return _fail


def setUpModule() -> None:
    # `--source auto` must never pick up a packrat service on the host running the tests.
    packrat_source.DEFAULT_PACKRAT_DB = (
        Path(tempfile.gettempdir()) / "no-such-packrat.sqlite"
    )
    packrat_source.SubprocessPackrat = _disabled_subprocess_packrat  # type: ignore[misc]


def tearDownModule() -> None:
    packrat_source.DEFAULT_PACKRAT_DB = _REAL_PACKRAT_DB
    packrat_source.SubprocessPackrat = _REAL_SUBPROCESS_PACKRAT  # type: ignore[misc]


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
            fake1 = FakeGh(
                [("graphql", 0, load_fixture("pr_open_ci_running.json"), "")]
            )
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
            board_file = (
                Path(store_dir) / "conversations" / "conv-test-1" / "monitor.jsonl"
            )
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
            fake_seed = FakeGh(
                [("graphql", 0, load_fixture("pr_open_ci_running.json"), "")]
            )
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
            board_lines = (
                (conv_dir / "monitor.jsonl").read_text(encoding="utf-8").splitlines()
            )
            # The board file is replaced in place: seed create, passive tick, actionable emit.
            self.assertEqual(len(board_lines), 1)
            self.assertEqual(json.loads(board_lines[0])["seq"], 3)
            self.assertEqual(
                (conv_dir / "_seq").read_text(encoding="utf-8").strip(), "3"
            )

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
            fake_seed = FakeGh(
                [("graphql", 0, load_fixture("pr_open_ci_running.json"), "")]
            )
            self._run_monitor(
                fake_seed, ["acme/widget#3339", "--state", state_path, "--once"]
            )

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
            fake_seed = FakeGh(
                [("graphql", 0, load_fixture("pr_open_ci_running.json"), "")]
            )
            self._run_monitor(
                fake_seed, ["acme/widget#3339", "--state", state_path, "--once"]
            )

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
            fake_seed = FakeGh(
                [("graphql", 0, load_fixture("pr_open_ci_running.json"), "")]
            )
            self._run_monitor(
                fake_seed, ["acme/widget#3339", "--state", state_path, "--once"]
            )

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
            self.assertEqual(
                list(payload["prs"].keys()), ["acme/widget#3338", "acme/widget#3339"]
            )

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
    return json.dumps(
        {"data": {"viewer": raw["data"]["viewer"], "pr0": raw["data"][alias]}}
    )


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
        notification_calls = [
            a for a, _ in fake.calls if "notifications?per_page=50&all=true" in a
        ]
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
        self._run(
            FakeGh([("graphql", 0, json.dumps(unknown), "")]), [*TWO_PRS, "--once"]
        )
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
        _, err = self._run(
            FakeGh([("graphql", 0, json.dumps(priced), "")]), [*TWO_PRS, "--once"]
        )
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
        self.assertEqual(
            self.mon._select_refresh_targets(1010.0, self._timers(), [], self.cfg), []
        )

    def test_unknown_mergeable_flags_only_its_key(self) -> None:
        self.mon.state.snapshots["acme/widget#2"].mergeable = "UNKNOWN"
        targets = self.mon._select_refresh_targets(1010.0, self._timers(), [], self.cfg)
        self.assertEqual(targets, ["acme/widget#2"])

    def test_pending_debounce_flags_only_its_keys(self) -> None:
        accumulated = [self._event("acme/widget#3"), self._event("other/repo#9")]
        targets = self.mon._select_refresh_targets(
            1010.0, self._timers(), accumulated, self.cfg
        )
        self.assertEqual(targets, ["acme/widget#3"])

    def test_pending_rollup_flagged_when_poll_due(self) -> None:
        self.mon.state.snapshots["acme/widget#1"].rollup_state = "PENDING"
        timers = self._timers(next_notify=2000.0)
        self.assertEqual(
            self.mon._select_refresh_targets(1010.0, timers, [], self.cfg), []
        )
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
        self.assertIsNone(
            self.mon._select_refresh_targets(1000.0, timers, [], self.cfg)
        )

    def test_backoff_suppresses_refresh(self) -> None:
        self.mon.state.snapshots["acme/widget#2"].mergeable = "UNKNOWN"
        timers = self._timers(next_sweep=1000.0, backoff_until=1030.0)
        self.assertEqual(
            self.mon._select_refresh_targets(1010.0, timers, [], self.cfg), []
        )

    def test_control_pushed_sha_flags_key_until_refreshed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp) / "control.jsonl"
            control.write_text(
                '{"pushed_sha": {"acme/widget#1": "c0ffee"}}\n', encoding="utf-8"
            )
            self.mon.control_reader = ControlReader(control)
            self.mon._process_control(None)
        timers = self._timers(next_notify=2000.0)
        targets = self.mon._select_refresh_targets(1010.0, timers, [], self.cfg)
        self.assertEqual(targets, ["acme/widget#1"])
        self.mon._pushed_keys.difference_update(targets)
        self.assertEqual(
            self.mon._select_refresh_targets(1011.0, timers, [], self.cfg), []
        )


class FakeNotifier:
    def __init__(self, succeed: bool = True) -> None:
        self.succeed = succeed
        self.calls: list[dict[str, Any]] = []
        self.argv_prefix = ("fake-agentapi", "send-message")

    def send(
        self, conversation_id: str, title: str, body: str, timeout_s: float = 15.0
    ) -> bool:
        self.calls.append(
            {"conversation_id": conversation_id, "title": title, "body": body}
        )
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
            mon = Monitor(
                runner=fake, state_path=self.state_path, notifier=FakeNotifier()
            )
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
        running_json = load_fixture("pr_open_ci_running.json").replace(
            '"alice"', '"bob"', 1
        )
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
        self.assertTrue(
            payload["prs"]["acme/widget#3339"]["mine"]
        )  # viewer from packrat
        self.assertIn(
            "via packrat", (self.dir / "board.a2ui").read_text(encoding="utf-8")
        )
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
        code, payload, _ = self._run(
            ["acme/widget#3339", "--debounce", "0", "--timeout", "120"]
        )
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
        code, payload, _ = self._run(
            ["acme/widget#3339", "--debounce", "0", "--timeout", "120"]
        )
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
        code, payload, _ = self._run(
            ["acme/widget#3339", "--stack", "--once"]
        )  # FakeGh()
        self.assertEqual(code, 0)
        self.assertEqual(
            list((payload or {})["prs"]), ["acme/widget#3338", "acme/widget#3339"]
        )

    def test_items_repo_stack_walks_with_gh(self) -> None:
        stack = json.loads(load_fixture("pr_stack.json"))
        stack["data"]["pr0"]["pullRequest"].update(state="OPEN", merged=False)
        for alias in ("pr0", "pr1"):
            store_graphql_pr(self.db, stack["data"][alias]["pullRequest"], mode="items")
        child_only = {
            "data": {"viewer": {"login": "alice"}, "pr0": stack["data"]["pr1"]}
        }
        parent = [
            {"number": 3338, "headRefName": "feat/store-writer", "baseRefName": "main"}
        ]
        fake = FakeGh(
            [
                ("graphql", 0, json.dumps(child_only), ""),
                ("--head feat/store-writer", 0, json.dumps(parent), ""),
                ("--base feat/decision-planner", 0, "[]", ""),
            ]
        )
        code, payload, _ = self._run(["acme/widget#3339", "--stack", "--once"], fake)
        self.assertEqual(code, 0)
        self.assertEqual(
            list((payload or {})["prs"]), ["acme/widget#3338", "acme/widget#3339"]
        )
        self.assertEqual(
            len(fake.calls), 3
        )  # stack walk only; snapshots come from packrat

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
            seen_armed.append(
                json.loads((self.dir / "armed.json").read_text(encoding="utf-8"))
            )
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
        self.assertEqual(
            len(self.cli.by_command("remove")), 1
        )  # lease released on exit

    def test_lease_renewed_every_five_minutes(self) -> None:
        store_fixture_prs(self.db, "pr_open_ci_running.json")
        code, _, _ = self._run(
            ["acme/widget#3339", "--events", "terminal", "--timeout", "650"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(len(self.cli.by_command("renew")), 2)  # at ~300 s and ~600 s

    def test_redirected_repo_slug_is_canonicalized_before_packrat_watch_add(
        self,
    ) -> None:
        store_fixture_prs(self.db, "pr_open_ci_running.json")
        fake = FakeGh([("graphql", 0, load_fixture("pr_open_ci_running.json"), "")])
        code, payload, _ = self._run(
            ["--repo", "legacy/Widget", "3339", "--once"], fake
        )
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
        json.loads(load_fixture("pr_open_ci_running.json"))["data"]["pr0"][
            "pullRequest"
        ]
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
            [
                "acme/widget#1",
                "--events",
                "new_review_request",
                "--state",
                self.state_path,
            ],
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
            [
                "--reviews",
                "--wake-on-review",
                "--repo",
                "acme/widget",
                "--state",
                self.state_path,
            ]
        )
        self.assertEqual(rev_repo.repo, "acme/widget")
        self.assertTrue(rev_repo.wake_on_review)

        author_args = parse_args(["acme/widget#1", "--state", self.state_path])
        self.assertEqual(author_args.role, ReviewRole.AUTHOR)
        self.assertEqual(author_args.until, "closed")

    def test_reviews_packrat_once_and_startup_pruning_with_zero_gh_calls(self) -> None:
        prior_state = MonitorState(
            snapshots={
                "acme/widget#5": make_snapshot(
                    number=5, state="CLOSED", author="bob", mine=False
                ),
                "acme/widget#6": make_snapshot(
                    number=6, state="OPEN", author="bob", mine=False
                ),
                "acme/widget#7": make_snapshot(
                    number=7, state="OPEN", author="alice", mine=True
                ),
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
        self.assertNotIn("acme/widget#7", payload["prs"])
        self.assertIn("acme/widget#3339", payload["prs"])
        self.assertNotIn("acme/widget#6", payload["prs"])
        self.assertEqual(
            [(e["pr"], e["state"]) for e in payload["recent_retired"]],
            [("acme/widget#6", "CLOSED")],
        )
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

    def test_reviews_mid_session_close_retires_snapshot_unleases_and_reopens(
        self,
    ) -> None:
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
        payload_close = json.loads(out_close.getvalue())
        self.assertNotIn("acme/widget#3339", mon_close.state.snapshots)
        self.assertEqual(
            [(e["pr"], e["state"]) for e in payload_close["recent_retired"]],
            [("acme/widget#3339", "CLOSED")],
        )
        self.assertEqual(
            mon_close.state.recent_retired, payload_close["recent_retired"]
        )
        self.assertNotIn("acme/widget#3339", mon_close.watched_keys)
        remove_calls = [
            c for c in self.cli.by_command("remove") if "acme/widget#3339" in c
        ]
        self.assertEqual(len(remove_calls), 1)
        dm_closed = review_board_data_model(
            mon_close.state.snapshots,
            viewer="alice",
            recent_retired=mon_close.state.recent_retired,
        )
        self.assertEqual(dm_closed["items"], [])
        self.assertEqual(
            dm_closed["subtitle"],
            "Review queue for @alice · closed today: PR 3339",
        )

        # Reopen recovery on a subsequent discovery sweep / viewer_changed clears recent_retired
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
        self.assertEqual(mon_close.state.recent_retired, [])
        dm_reopened = review_board_data_model(
            mon_close.state.snapshots,
            viewer="alice",
            recent_retired=mon_close.state.recent_retired,
        )
        self.assertTrue(dm_reopened["items"][0]["canReview"])
        self.assertNotIn("closed today", dm_reopened["subtitle"])

    def test_reviews_mid_session_merge_retires_and_expires_from_pane(self) -> None:
        pr1 = _review_pr_node(3339, repo="acme/widget", requested_viewer="alice")
        pr2 = _review_pr_node(3340, repo="acme/widget", requested_viewer="alice")
        store_graphql_pr(self.db, pr1)
        store_graphql_pr(self.db, pr2)
        self._add_viewer_item("review_requested", "acme/widget#3339")
        self._add_viewer_item("review_requested", "acme/widget#3340")

        store_dir = self.dir / "pane_store"
        store_dir.mkdir()
        current_epoch = 1793135700.0
        current_iso = "2026-10-27T21:15:00Z"

        def run_with_clock(argv: list[str]) -> tuple[int, dict[str, Any]]:
            args = parse_args(
                [
                    "--reviews",
                    "--source",
                    "packrat",
                    "--packrat-db",
                    str(self.db),
                    "--state",
                    self.state_path,
                    "--conversation-id",
                    "conv-rev-retire",
                    "--a2ui-store-dir",
                    str(store_dir),
                    *argv,
                ]
            )
            mon = Monitor(
                runner=FakeGh([]),
                state_path=self.state_path,
                clock=self.clk,
                sleep=self.clk.sleep,
                now=lambda: current_iso,
                epoch_now=lambda: current_epoch,
                packrat_runner=self.cli,
            )
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = mon.run(args)
            return code, json.loads(out.getvalue())

        code1, _ = run_with_clock(["--once"])
        self.assertEqual(code1, 0)

        pr1_merged = _review_pr_node(
            3339, repo="acme/widget", requested_viewer="alice", state="MERGED"
        )
        store_graphql_pr(self.db, pr1_merged)
        code2, payload2 = run_with_clock(["--once"])
        self.assertEqual(code2, 0)
        self.assertEqual(list(payload2["prs"]), ["acme/widget#3340"])
        self.assertEqual(
            [(e["pr"], e["state"]) for e in payload2["recent_retired"]],
            [("acme/widget#3339", "MERGED")],
        )
        pane_path = store_dir / "conversations" / "conv-rev-retire" / "monitor.jsonl"
        line_merged = json.loads(pane_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertIn(
            "merged today: PR 3339",
            line_merged["message"]["createSurface"]["dataModel"]["subtitle"],
        )

        # After 24h TTL expires with unchanged active rows, the pane updates its subtitle.
        current_epoch += 24 * 60 * 60 + 1
        current_iso = "2026-10-28T21:15:01Z"
        code3, payload3 = run_with_clock(["--once"])
        self.assertEqual(code3, 0)
        self.assertEqual(payload3["recent_retired"], [])
        line_expired = json.loads(pane_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertNotIn(
            "merged today",
            line_expired["message"]["createSurface"]["dataModel"]["subtitle"],
        )

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
            code_silent = self._monitor(FakeGh([]), notifier=notifier_silent).run(
                args_silent
            )
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

    def test_reviews_evicts_unassigned_open_pr_and_preserves_notify_flag_on_gh_error(
        self,
    ) -> None:
        # 1. H2: Unassigned open unreviewed PR is evicted from snapshots, watched_keys, and leases
        pr_req = _review_pr_node(3339, repo="acme/widget", requested_viewer="alice")
        store_graphql_pr(self.db, pr_req)
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

        # Unassign alice while bob remains requested on the still-OPEN PR
        pr_unassigned = _review_pr_node(
            3339, repo="acme/widget", requested_viewer="bob"
        )
        store_graphql_pr(self.db, pr_unassigned)
        with _db(self.db) as conn:
            conn.execute(
                "DELETE FROM v_viewer_items WHERE item_key = 'acme/widget#3339'"
            )

        mon_evict = self._monitor(FakeGh([]))
        mon_evict.role = ReviewRole.REVIEWER
        mon_evict.state = MonitorState.from_dict(
            json.loads(Path(self.state_path).read_text(encoding="utf-8"))
        )
        mon_evict.watched_keys = ["acme/widget#3339"]
        mon_evict.packrat = mon_evict._select_source(seed_args)
        assert mon_evict.packrat is not None
        mon_evict.packrat.lease(["acme/widget#3339"])
        added, missing = mon_evict._discover_packrat_review_delta()
        self.assertEqual((added, missing), ([], ["acme/widget#3339"]))
        mon_evict.refresh(missing)
        self.assertNotIn("acme/widget#3339", mon_evict.watched_keys)
        self.assertNotIn("acme/widget#3339", mon_evict.state.snapshots)
        remove_calls = [
            c for c in self.cli.by_command("remove") if "acme/widget#3339" in c
        ]
        self.assertEqual(len(remove_calls), 1)

        # 2. M4: Transient GhError in _consume_unwatched_review_request retains notification flag
        gh_fail = FakeGh([("graphql", 1, "", "HTTP 502 Bad Gateway")])
        mon_gh = self._monitor(gh_fail, state_path=str(self.dir / "m4_state.json"))
        mon_gh.role = ReviewRole.REVIEWER
        mon_gh.poller.review_requested_unwatched = True
        with self.assertRaises(GhError):
            mon_gh._consume_unwatched_review_request()
        self.assertTrue(mon_gh.poller.review_requested_unwatched)

    def test_reviews_cli_overrides_validation_and_mute_unmute_triage_flow(self) -> None:
        # 1. Override flags require --reviews and --stale-days >= 0.
        for bad_argv in (
            ["acme/widget#1", "--state", self.state_path, "--mute", "acme/widget#1"],
            ["acme/widget#1", "--state", self.state_path, "--unmute", "acme/widget#1"],
            [
                "acme/widget#1",
                "--state",
                self.state_path,
                "--record-triage",
                "acme/widget#1=likely_shelved",
            ],
            ["--reviews", "--state", self.state_path, "--stale-days", "-1"],
            [
                "--reviews",
                "--state",
                self.state_path,
                "--record-triage",
                "acme/widget#1=invalid_verdict",
            ],
        ):
            with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                parse_args(bad_argv)

        # 2. Seed three PRs in packrat:
        # - #3339: active PR with post-review comment watermark
        # - #3340: unleased PR muted via case-insensitive --mute Acme/Widget#3340
        # - #3341: 60-day-old unrequested Waiting PR (dormant under --stale-days 30)
        pr_active = _review_pr_node(
            3339,
            repo="acme/widget",
            requested_viewer=None,
            head_sha="1111111",
            updated_at="2026-10-27T21:10:00Z",
            reviews=[
                {
                    "author": {"login": "alice", "__typename": "User"},
                    "state": "COMMENTED",
                    "submittedAt": "2026-10-27T20:00:00Z",
                    "commit": {"oid": "1111111"},
                }
            ],
        )
        pr_active["comments"] = {
            "nodes": [
                {
                    "id": "IC_shelved_1",
                    "createdAt": "2026-10-27T21:05:00Z",
                    "author": {"login": "bob", "__typename": "User"},
                }
            ]
        }
        pr_unleased = _review_pr_node(
            3340,
            repo="acme/widget",
            requested_viewer="alice",
            head_sha="9999999",
            updated_at="2026-10-27T21:08:00Z",
        )
        pr_dormant = _review_pr_node(
            3341,
            repo="acme/widget",
            requested_viewer=None,
            head_sha="7777777",
            updated_at="2026-08-15T12:00:00Z",
            reviews=[
                {
                    "author": {"login": "alice", "__typename": "User"},
                    "state": "COMMENTED",
                    "submittedAt": "2026-08-15T12:00:00Z",
                    "commit": {"oid": "7777777"},
                }
            ],
        )
        pr_dormant["comments"] = {
            "nodes": [
                {
                    "id": "IC_old_1",
                    "createdAt": "2026-08-15T12:05:00Z",
                    "author": {"login": "carol", "__typename": "User"},
                }
            ]
        }
        for node in (pr_active, pr_unleased, pr_dormant):
            store_graphql_pr(self.db, node)
        self._add_viewer_item("reviewed", "acme/widget#3339")
        self._add_viewer_item("reviewed", "acme/widget#3341")

        ov_file = self.dir / "overrides.json"
        args_mute = parse_args(
            [
                "--reviews",
                "--once",
                "--stale-days",
                "30",
                "--overrides-file",
                str(ov_file),
                "--mute",
                "Acme/Widget#3340",
                "--source",
                "packrat",
                "--packrat-db",
                str(self.db),
                "--state",
                self.state_path,
            ]
        )
        out_mute = io.StringIO()
        with redirect_stdout(out_mute), redirect_stderr(io.StringIO()):
            code_mute = self._monitor(FakeGh([])).run(args_mute)
        self.assertEqual(code_mute, 0)
        payload_mute = json.loads(out_mute.getvalue())
        # Only active non-muted non-dormant #3339 appears in untriaged_discussions!
        self.assertEqual(payload_mute["untriaged_discussions"], ["acme/widget#3339"])
        board_dsl = (self.dir / "board.a2ui").read_text(encoding="utf-8")
        self.assertIn("Muted & Dormant (2)", board_dsl)
        saved_ov = json.loads(ov_file.read_text(encoding="utf-8"))
        self.assertEqual(saved_ov["mutes"]["acme/widget#3340"]["head_sha"], "9999999")
        self.assertTrue(saved_ov["mutes"]["acme/widget#3340"]["was_requested"])

        # 3. Record triage on #3339 and unmute/pin dormant #3341 -> #3341 moves back to active
        # items and #3339 drops out of untriaged_discussions with Likely shelved badge!
        args_unmute = parse_args(
            [
                "--reviews",
                "--once",
                "--stale-days",
                "30",
                "--overrides-file",
                str(ov_file),
                "--unmute",
                "acme/widget#3341",
                "--record-triage",
                "acme/widget#3339=likely_shelved",
                "--triage-summary",
                "acme/widget#3339=Participants agreed not to pursue this PR.",
                "--source",
                "packrat",
                "--packrat-db",
                str(self.db),
                "--state",
                self.state_path,
            ]
        )
        out_unmute = io.StringIO()
        with redirect_stdout(out_unmute), redirect_stderr(io.StringIO()):
            code_unmute = self._monitor(FakeGh([])).run(args_unmute)
        self.assertEqual(code_unmute, 0)
        payload_unmute = json.loads(out_unmute.getvalue())
        # Unhidden #3341 is now active (no longer dormant), so its untriaged comment surfaces!
        self.assertEqual(payload_unmute["untriaged_discussions"], ["acme/widget#3341"])
        board_dsl_after = (self.dir / "board.a2ui").read_text(encoding="utf-8")
        self.assertIn("Likely shelved", board_dsl_after)
        self.assertIn("Muted & Dormant (1)", board_dsl_after)

    def test_reviews_github_idle_tick_reloads_external_overrides_immediately(
        self,
    ) -> None:
        pr_node = _review_pr_node(
            3339,
            repo="acme/widget",
            requested_viewer=None,
            head_sha="1111111",
            reviews=[
                {
                    "author": {"login": "alice", "__typename": "User"},
                    "state": "COMMENTED",
                    "submittedAt": "2026-10-27T20:00:00Z",
                    "commit": {"oid": "1111111"},
                }
            ],
        )
        pr_node["mergeable"] = "MERGEABLE"
        pr_node["mergeStateStatus"] = "CLEAN"
        pr_node["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["state"] = (
            "SUCCESS"
        )
        fake_gh = FakeGh(
            [
                (
                    "graphql",
                    0,
                    _search_response("alice", [], [("acme/widget", 3339)]),
                    "",
                ),
                ("graphql", 0, _fetch_response("alice", pr_node), ""),
                ("notifications", 0, "HTTP/2.0 304 Not Modified\r\n\r\n", ""),
                ("notifications", 0, "HTTP/2.0 304 Not Modified\r\n\r\n", ""),
            ]
        )
        ov_file = self.dir / "idle_overrides.json"
        store_dir = self.dir / "idle_store"
        control = self.dir / "control.jsonl"
        args = parse_args(
            [
                "--reviews",
                "--daemon",
                "--poll",
                "120",
                "--notify-poll",
                "60",
                "--sweep",
                "600",
                "--overrides-file",
                str(ov_file),
                "--conversation-id",
                "conv-idle-ov",
                "--a2ui-store-dir",
                str(store_dir),
                "--source",
                "github",
                "--state",
                self.state_path,
            ]
        )
        sleeps = 0

        def sleep_and_mutate(seconds: float) -> None:
            nonlocal sleeps
            sleeps += 1
            self.clk.sleep(seconds)
            if sleeps == 1:
                # External CLI writes review_overrides.json while github targets == [] on next tick
                ov_file.write_text(
                    json.dumps(
                        {
                            "schema": 1,
                            "mutes": {
                                "acme/widget#3339": {
                                    "muted_at": "2026-10-27T21:20:00Z",
                                    "head_sha": "1111111",
                                    "updated_at": "2026-10-27T21:05:00Z",
                                    "was_requested": True,
                                    "reason": "",
                                }
                            },
                            "pins": {},
                            "triage": {},
                        }
                    ),
                    encoding="utf-8",
                )
            else:
                control.write_text('{"stop": true}\n', encoding="utf-8")

        mon = Monitor(
            runner=fake_gh,
            state_path=self.state_path,
            clock=self.clk,
            sleep=sleep_and_mutate,
            now=lambda: "2026-10-27T21:15:00Z",
            epoch_now=lambda: 1793135700.0,
            notifier=FakeNotifier(),
        )
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = mon.run(args)
        self.assertEqual(code, 0)
        pane_text = (
            store_dir / "conversations" / "conv-idle-ov" / "monitor.jsonl"
        ).read_text(encoding="utf-8")
        self.assertIn("Muted & Dormant (1)", pane_text)


class TestRemotePackratReaderMode(unittest.TestCase):
    """Tests zero-local-DB remote `packrat reader` execution and `auth_expired` fallback."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.state_path = str(self.dir / "state.json")
        self.clk = FakeClock()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_remote_reader_polls_health_once_per_minute(self) -> None:
        stack = json.loads(load_fixture("pr_stack.json"))
        pr0 = stack["data"]["pr0"]["pullRequest"]
        pr0.update(state="OPEN", merged=False)
        pr1 = stack["data"]["pr1"]["pullRequest"]

        from test_packrat_source import _live_row

        live0 = _live_row("acme/widget#3338", "acme/widget", "mirror", pr0, age_s=2.0)
        live1 = _live_row("acme/widget#3339", "acme/widget", "mirror", pr1, age_s=2.0)
        calls: list[list[str]] = []
        health_times: list[float] = []
        wait_step = 0

        def remote_runner(args: list[str], _timeout: float) -> tuple[int, str, str]:
            nonlocal wait_step
            calls.append(list(args))
            if args[0] == "reader":
                ops = json.loads(args[args.index("--ops-json") + 1])
                resp: dict[str, Any] = {}
                if ops.get("health"):
                    health_times.append(self.clk.now)
                    resp["health"] = {
                        "pid": 77,
                        "beat_age_s": 3,
                        "viewer_login": "alice",
                    }
                if ops.get("stack_neighbors"):
                    resp["stack_neighbors"] = {
                        "acme/widget#3339": ["acme/widget#3338", "acme/widget#3339"]
                    }
                if ops.get("known_keys"):
                    resp["known_keys"] = {
                        "acme/widget#3338": "acme/widget#3338",
                        "acme/widget#3339": "acme/widget#3339",
                    }
                if ops.get("items"):
                    resp["items"] = {
                        "pr_live": [live0, live1],
                        "checks": [],
                        "review_requests": [],
                        "latest_reviews": [],
                        "latest_author_reviews": [],
                        "threads": [],
                        "comments": [],
                    }
                return 0, json.dumps(resp), ""
            if args[0] == "changes":
                since = int(args[args.index("--since") + 1])
                wait = float(args[args.index("--wait") + 1])
                if since > 100:
                    return 0, json.dumps({"reset": True, "seq": 10}), ""
                wait_step += 1
                if wait > 0:
                    self.clk.sleep(wait)
                return 0, json.dumps({"seq": 10, "changes": []}), ""
            return 0, json.dumps({"ok": True}), ""

        # Zero local SQLite file exists at packrat_source.DEFAULT_PACKRAT_DB!
        self.assertFalse(packrat_source.DEFAULT_PACKRAT_DB.exists())
        args = parse_args(
            [
                "acme/widget#3339",
                "--stack",
                "--events",
                "terminal",
                "--timeout",
                "150",
                "--source",
                "auto",
                "--state",
                self.state_path,
                "--no-a2ui",
            ]
        )
        fake_gh = FakeGh([])
        mon = Monitor(
            runner=fake_gh,
            state_path=self.state_path,
            clock=self.clk,
            sleep=self.clk.sleep,
            now=lambda: "2026-10-27T21:15:00Z",
            epoch_now=lambda: 1793135700.0,
            packrat_runner=remote_runner,
        )
        start = self.clk.now
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = mon.run(args)
        self.assertEqual(code, 2)
        self.assertEqual(len(fake_gh.calls), 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["source"], "packrat")
        self.assertEqual(list(payload["prs"]), ["acme/widget#3338", "acme/widget#3339"])
        self.assertGreaterEqual(wait_step, 2)

        # Startup reads health at `start` (_select_source + viewer_login). The wait
        # loop reads it again no sooner than 60 s after startup and 60 s apart.
        elapsed = self.clk.now - start
        self.assertGreaterEqual(elapsed, 120)
        loop_health = [t for t in health_times if t > start]
        self.assertGreaterEqual(len(loop_health), 1)
        self.assertLessEqual(len(loop_health), elapsed // 60)
        for earlier, later in zip([start, *loop_health], loop_health):
            self.assertGreaterEqual(later - earlier, 60)
        self.assertEqual(len(health_times) - len(loop_health), 2)

    def _reader_monitor(self, fake_gh: FakeGh, runner: FakeReaderRunner) -> Monitor:
        return Monitor(
            runner=fake_gh,
            state_path=self.state_path,
            clock=self.clk,
            sleep=self.clk.sleep,
            now=lambda: "2026-10-27T21:15:00Z",
            epoch_now=lambda: 1793135700.0,
            packrat_runner=runner,
        )

    def _run_reader(
        self, extra: list[str], runner: FakeReaderRunner, fake_gh: FakeGh
    ) -> tuple[int, dict[str, Any] | None, str]:
        # No --packrat-db: on the default path every read is a `packrat reader` call.
        args = parse_args([*extra, "--state", self.state_path, "--source", "auto"])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = self._reader_monitor(fake_gh, runner).run(args)
        payload = json.loads(out.getvalue()) if out.getvalue().strip() else None
        return code, payload, err.getvalue()

    def _service_dies_on_first_wait(self, runner: FakeReaderRunner) -> None:
        def wait_then_die(seconds: float) -> None:
            self.clk.sleep(seconds)
            runner.beat_age_s = 999

        runner.on_wait = wait_then_die

    @staticmethod
    def _items_bundle(
        live: dict[str, Any], review_requests: list[dict[str, Any]] | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        return {
            "pr_live": [live],
            "checks": [],
            "review_requests": review_requests or [],
            "latest_reviews": [],
            "latest_author_reviews": [],
            "threads": [],
            "comments": [],
        }

    def test_reader_stale_heartbeat_falls_back_to_github_once(self) -> None:
        self.assertFalse(packrat_source.DEFAULT_PACKRAT_DB.exists())
        pr_open = json.loads(load_fixture("pr_open_ci_running.json"))["data"]["pr0"][
            "pullRequest"
        ]
        runner = FakeReaderRunner(beat_age_s=3)
        runner.known_keys_map = {"acme/widget#3339": "acme/widget#3339"}
        runner.items_bundle = self._items_bundle(
            _live_row("acme/widget#3339", "acme/widget", "mirror", pr_open, age_s=1.0)
        )
        self._run_reader(["acme/widget#3339", "--once"], runner, FakeGh())

        self._service_dies_on_first_wait(runner)
        fake = FakeGh([("graphql", 0, load_fixture("pr_ci_failed.json"), "")])
        code, payload, err = self._run_reader(
            ["acme/widget#3339", "--debounce", "0", "--timeout", "120"], runner, fake
        )
        self.assertEqual(code, 0)
        assert payload is not None
        self.assertEqual(payload["source"], "github")
        self.assertIn("ci_check", {e["kind"] for e in payload["events"]})
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(err.count("falling back to direct GitHub polling"), 1)
        self.assertIn("heartbeat is stale or missing", err)
        board = (self.dir / "board.a2ui").read_text(encoding="utf-8")
        self.assertIn("via GitHub (packrat unavailable)", board)

    def test_reviews_reader_stale_heartbeat_falls_back_to_github_once(self) -> None:
        self.assertFalse(packrat_source.DEFAULT_PACKRAT_DB.exists())
        pr_node = _review_pr_node(3339, repo="acme/widget", requested_viewer="alice")
        pr20 = _review_pr_node(20, repo="other/repo", requested_viewer="alice")
        runner = FakeReaderRunner(beat_age_s=3)
        runner.viewer_items = [
            {
                "set_name": "review_requested",
                "item_key": "acme/widget#3339",
                "state": "OPEN",
                "updated_at": "2026-10-27T21:00:00Z",
            }
        ]
        runner.items_bundle = self._items_bundle(
            _live_row("acme/widget#3339", "acme/widget", "mirror", pr_node, age_s=1.0),
            [
                {
                    "item_key": "acme/widget#3339",
                    "reviewer": "alice",
                    "reviewer_kind": "User",
                }
            ],
        )
        self._run_reader(["--reviews", "--once"], runner, FakeGh())

        self._service_dies_on_first_wait(runner)
        fake = FakeGh(
            [
                (
                    "graphql",
                    0,
                    _search_response(
                        "alice", [("acme/widget", 3339), ("other/repo", 20)]
                    ),
                    "",
                ),
                ("graphql", 0, _fetch_response("alice", pr_node, pr20), ""),
            ]
        )
        code, payload, err = self._run_reader(
            ["--reviews", "--debounce", "0", "--timeout", "120"], runner, fake
        )
        self.assertEqual(code, 0)
        assert payload is not None
        self.assertEqual(payload["source"], "github")
        self.assertIn(
            ("other/repo#20", "new_review_request"),
            {(e["pr"], e["kind"]) for e in payload["events"]},
        )
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(err.count("falling back to direct GitHub polling"), 1)
        self.assertIn("heartbeat is stale or missing", err)

    def test_auth_expired_in_select_source_and_packrat_wait_falls_back_to_gh(
        self,
    ) -> None:
        # 1. _select_source fallback when initial health probe hits [auth_expired]
        def expired_at_startup(
            _args: list[str], _timeout: float
        ) -> tuple[int, str, str]:
            return 4, "", "Error [auth_expired]: remote session expired"

        fake_gh1 = FakeGh([("graphql", 0, load_fixture("pr_open_ci_running.json"), "")])
        args1 = parse_args(
            [
                "acme/widget#3339",
                "--once",
                "--source",
                "auto",
                "--state",
                self.state_path,
                "--no-a2ui",
            ]
        )
        mon1 = Monitor(
            runner=fake_gh1,
            state_path=self.state_path,
            clock=self.clk,
            sleep=self.clk.sleep,
            now=lambda: "2026-10-27T21:15:00Z",
            epoch_now=lambda: 1793135700.0,
            packrat_runner=expired_at_startup,
        )
        out1 = io.StringIO()
        with redirect_stdout(out1), redirect_stderr(io.StringIO()):
            code1 = mon1.run(args1)
        self.assertEqual(code1, 0)
        self.assertEqual(json.loads(out1.getvalue())["source"], "github")
        self.assertEqual(len(fake_gh1.calls), 1)

        # 2. _packrat_wait fallback when mid-session wait_changes hits [auth_expired]
        pr_open = json.loads(load_fixture("pr_open_ci_running.json"))["data"]["pr0"][
            "pullRequest"
        ]
        from test_packrat_source import _live_row

        live_open = _live_row(
            "acme/widget#3339", "acme/widget", "mirror", pr_open, age_s=1.0
        )

        def expire_on_wait(args: list[str], _timeout: float) -> tuple[int, str, str]:
            if args[0] == "reader":
                ops = json.loads(args[args.index("--ops-json") + 1])
                resp: dict[str, Any] = {}
                if ops.get("health"):
                    resp["health"] = {
                        "pid": 77,
                        "beat_age_s": 2,
                        "viewer_login": "alice",
                    }
                if ops.get("known_keys"):
                    resp["known_keys"] = {"acme/widget#3339": "acme/widget#3339"}
                if ops.get("items"):
                    resp["items"] = {
                        "pr_live": [live_open],
                        "checks": [],
                        "review_requests": [],
                        "latest_reviews": [],
                        "latest_author_reviews": [],
                        "threads": [],
                        "comments": [],
                    }
                return 0, json.dumps(resp), ""
            if args[0] == "changes":
                since = int(args[args.index("--since") + 1])
                if since > 100:
                    return 0, json.dumps({"reset": True, "seq": 10}), ""
                return 4, "", "Error [auth_expired]: session token expired"
            return 0, json.dumps({"ok": True}), ""

        fake_gh2 = FakeGh([("graphql", 0, load_fixture("pr_ci_failed.json"), "")])
        args2 = parse_args(
            [
                "acme/widget#3339",
                "--events",
                "ci_check",
                "--debounce",
                "0",
                "--timeout",
                "60",
                "--source",
                "auto",
                "--state",
                self.state_path,
            ]
        )
        mon2 = Monitor(
            runner=fake_gh2,
            state_path=self.state_path,
            clock=self.clk,
            sleep=self.clk.sleep,
            now=lambda: "2026-10-27T21:15:00Z",
            epoch_now=lambda: 1793135700.0,
            packrat_runner=expire_on_wait,
        )
        out2, err2 = io.StringIO(), io.StringIO()
        with redirect_stdout(out2), redirect_stderr(err2):
            code2 = mon2.run(args2)
        self.assertEqual(code2, 0)
        payload2 = json.loads(out2.getvalue())
        self.assertEqual(payload2["source"], "github")
        self.assertIn("ci_check", {e["kind"] for e in payload2["events"]})
        self.assertIn("auth_expired", err2.getvalue())
        self.assertIn("falling back to direct GitHub polling", err2.getvalue())


_MINE_SYNCED = {"authored_open": "2026-11-17T01:19:05Z"}
_MINE_EPOCH = 1793135700.0  # 2026-10-27T21:15:00Z, when the fake clock reads 1000


def _fetched_ago(seconds: float) -> str:
    """Returns a ``live_fetched_at`` value that many real seconds in the past.

    The value keeps milliseconds, so a row stored "now" does not read as up to a
    second old.
    """
    at = datetime.now(UTC) - timedelta(seconds=seconds)
    return at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"


def _viewer_change(seq: int, key: str) -> dict[str, Any]:
    row = {"seq": seq, "item_key": key, "kind": "viewer", "fields": ["authored_open"]}
    return {"seq": seq, "changes": [{**row, "at": "x"}]}


class _NoGh:
    """A ``gh`` runner that fails on any call: ``--mine`` reads only packrat."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self, args: list[str], *, input_text: str | None = None
    ) -> tuple[int, str, str]:
        self.calls.append(list(args))
        raise AssertionError(f"--mine must not call gh: gh {' '.join(args)}")


class MineCase(unittest.TestCase):
    """Shared setup for ``--mine`` tests: a fake clock, a failing ``gh``, and a log."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.state_path = str(self.dir / "state.json")
        self.log_path = self.dir / "events.jsonl"
        self.control = self.dir / "control.jsonl"
        self.clk = FakeClock()
        self.gh = _NoGh()
        self.notifier = FakeNotifier()
        self.sleep: Any = self.clk.sleep

    def tearDown(self) -> None:
        self.assertEqual(self.gh.calls, [], "--mine called gh")
        self.tmp.cleanup()

    def _iso_now(self) -> str:
        at = datetime.fromtimestamp(self._epoch_now(), UTC)
        return at.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _epoch_now(self) -> float:
        return _MINE_EPOCH + (self.clk.now - 1000.0)

    def _monitor(self, runner: Any, state_path: str | None = None) -> Monitor:
        self.mon = Monitor(
            runner=self.gh,
            state_path=state_path or self.state_path,
            clock=self.clk,
            sleep=lambda seconds: self.sleep(seconds),
            now=self._iso_now,
            epoch_now=self._epoch_now,
            notifier=self.notifier,
            packrat_runner=runner,
        )
        return self.mon

    def _argv(self, extra: list[str], state_path: str | None = None) -> list[str]:
        common = ["--state", state_path or self.state_path, "--log", str(self.log_path)]
        return ["--mine", *extra, *common, "--a2ui-store-dir", str(self.dir / "store")]

    def _daemon(self, *extra: str) -> list[str]:
        control = ["--control", str(self.control)]
        return ["--daemon", "--conversation-id", "conv-mine", *control, *extra]

    def _run_monitor(self, mon: Monitor, argv: list[str]) -> tuple[int, str]:
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = mon.run(parse_args(argv))
        return code, err.getvalue()

    def _logged(self) -> list[dict[str, Any]]:
        """Returns every payload the run emitted, oldest first."""
        if not self.log_path.exists():
            return []
        lines = self.log_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def _logged_events(self) -> list[tuple[str, str]]:
        return [(e["pr"], e["kind"]) for p in self._logged() for e in p["events"]]

    def _stop(self) -> dict[str, Any]:
        """A change-feed answer that also asks the daemon to stop."""
        with self.control.open("a", encoding="utf-8") as handle:
            handle.write('{"stop": true}\n')
        return {"seq": 99, "changes": []}


class TestMineMode(MineCase):
    """``--mine`` on a packrat database file: startup, row lifecycle, wake rules, gates."""

    def setUp(self) -> None:
        super().setUp()
        self.db = make_packrat_db(
            self.dir / "repos.sqlite", viewer="alice", synced_sets=_MINE_SYNCED
        )
        self.cli = FakePackratCli(on_wait=self.clk.sleep)

    def _pr(
        self, number: int, repo: str = "acme/widget", **kwargs: Any
    ) -> dict[str, Any]:
        return _review_pr_node(
            number, repo=repo, author="alice", requested_viewer=None, **kwargs
        )

    def _store(self, pr: dict[str, Any], age_s: float = 0.0) -> str:
        """Stores ``pr`` as a live row that packrat fetched ``age_s`` seconds ago.

        Row ages are real time and gate times are fake-clock time. An old row is
        given an age far beyond any fake-clock span in its test.
        """
        return store_graphql_pr(self.db, pr, live_fetched_at=_fetched_ago(age_s))

    def _refetch(self, pr: dict[str, Any]) -> str:
        """Stores ``pr`` as packrat refetching it now, a few seconds after any gate."""
        self.clk.now += 2
        return self._store(pr)

    def _in_set(self, *keys: str) -> None:
        with _db(self.db) as conn:
            conn.executemany(
                "INSERT INTO v_viewer_items VALUES ('authored_open', ?, 'x', 'x')",
                [(key,) for key in keys],
            )

    def _leave_set(self, key: str) -> None:
        with _db(self.db) as conn:
            conn.execute("DELETE FROM v_viewer_items WHERE item_key = ?", (key,))

    def _open_pr(self, number: int = 3339, age_s: float = 0.0, **kwargs: Any) -> str:
        key = self._store(self._pr(number, **kwargs), age_s)
        self._in_set(key)
        return key

    def _run(
        self, extra: list[str], state_path: str | None = None
    ) -> tuple[int, dict[str, Any] | None, str, Monitor]:
        mon = self._monitor(self.cli, state_path)
        argv = [*self._argv(extra, state_path), "--packrat-db", str(self.db)]
        code, err = self._run_monitor(mon, argv)
        logged = self._logged()
        return code, (logged[-1] if logged else None), err, mon

    def _waits(self) -> list[str]:
        """Returns the ``--wait`` value of each change-feed call, in order."""
        calls = self.cli.by_command("changes")
        return [c[c.index("--wait") + 1] for c in calls]

    def _refreshed(self) -> list[list[str]]:
        """Returns the keys of each ``packrat refresh`` call, in order."""
        return [c[c.index("--") + 1 :] for c in self.cli.by_command("refresh")]

    # ----- arguments ---------------------------------------------------------------

    def test_mine_argument_validation_and_defaults(self) -> None:
        state = ["--state", self.state_path]
        for invalid in (
            ["--mine", "acme/widget#1"],
            ["--mine", "--reviews"],
            ["--mine", "--stack"],
            ["--mine", "--source", "github"],
            ["--mine", "--until", "closed"],
            ["--mine", "--until", "merged"],
            ["--mine", "--until", "all-green"],
            ["--mine", "--events", "new_review_request"],
            ["--mine", "--mute", "acme/widget#1"],
            ["--wake", "acme/widget#1"],
            ["--wake", "--reviews"],
        ):
            with (
                self.subTest(argv=invalid),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                parse_args([*invalid, *state])

        args = parse_args(["--mine", *state])
        self.assertEqual(args.membership, Membership.MINE)
        self.assertEqual(args.role, ReviewRole.AUTHOR)
        self.assertEqual(args.until, "none")
        self.assertEqual(args.source, "packrat")  # --source auto behaves as packrat
        self.assertFalse(args.wake)

        full = parse_args(
            ["--mine", "--wake", "--repo", "acme/widget", "--until", "none", "--source",
             "packrat", "--events", "pr_added,pr_removed,terminal", *state]
        )  # fmt: skip
        self.assertTrue(full.wake)
        self.assertEqual(full.repo, "acme/widget")

        # The other modes keep their membership and their --until defaults.
        static = parse_args(["acme/widget#1", *state])
        self.assertEqual(
            (static.membership, static.until), (Membership.STATIC, "closed")
        )
        reviews = parse_args(["--reviews", *state])
        self.assertEqual(
            (reviews.membership, reviews.until), (Membership.REVIEW_QUEUE, "none")
        )

    # ----- startup -----------------------------------------------------------------

    def test_never_synced_set_exits_1_with_and_without_repo(self) -> None:
        self._open_pr()
        for synced in ({}, {"authored": "2026-11-17T01:19:05Z"}):
            set_synced_sets(self.db, synced)
            for extra in ([], ["--repo", "acme/widget"]):
                with self.subTest(synced=synced, extra=extra):
                    code, payload, err, _ = self._run(["--once", *extra])
                    self.assertEqual((code, payload), (1, None))
                    self.assertIn("authored_open", err)
                    self.assertIn("packrat auto-sync on", err)

    def test_packrat_down_at_startup_exits_1_with_the_source_packrat_error(
        self,
    ) -> None:
        self._open_pr()
        set_heartbeat(self.db, 999)
        code, payload, err, _ = self._run(["--once"])
        self.assertEqual((code, payload), (1, None))
        self.assertIn("--source packrat: packrat serve is not running", err)

    def test_once_lists_open_prs_and_key_without_a_live_row_does_not_raise(
        self,
    ) -> None:
        shown = self._open_pr(3339, updated_at="2026-10-27T20:00:00Z")
        draft = self._store(
            self._pr(7, repo="other/repo", updated_at="2026-10-27T22:00:00Z")
        )
        self._in_set(draft, "acme/widget#404")  # 404 has no live row yet

        code, payload, err, mon = self._run(["--once"])
        self.assertEqual(code, 0, err)
        assert payload is not None
        self.assertEqual(sorted(payload["prs"]), sorted([draft, shown]))
        # Watched newest update first, and the key without a row last.
        self.assertEqual(mon.watched_keys, [draft, shown, "acme/widget#404"])
        self.assertTrue(all(pr["mine"] for pr in payload["prs"].values()))
        self.assertEqual(payload["events"], [])  # a fresh state announces nothing
        self.assertEqual(payload["exit_reason"], "snapshot")
        self.assertEqual(payload["membership"], "mine")
        self.assertEqual(payload["source"], "packrat")
        self.assertEqual(payload["source_state"], "live")
        self.assertIsNone(payload["stale_since"])
        self.assertEqual(payload["total_open"], 3)
        self.assertEqual(payload["pending"], 1)
        self.assertEqual(payload["recent_retired"], [])
        # Every member is leased, the row-less one included, and nothing is gated.
        add_calls = self.cli.by_command("add")
        self.assertEqual(len(add_calls), 1)
        self.assertEqual(
            sorted(add_calls[0][add_calls[0].index("--") + 1 :]),
            sorted([shown, draft, "acme/widget#404"]),
        )
        self.assertEqual(self._refreshed(), [])
        self.assertNotIn("acme/widget#404", load_state(self.state_path).snapshots)
        self.assertIn("via packrat", (self.dir / "board.a2ui").read_text("utf-8"))

    def test_repo_scopes_the_watched_set(self) -> None:
        mine = self._open_pr(3339)
        self._in_set(self._store(self._pr(7, repo="other/repo")))
        code, payload, _, _ = self._run(["--once", "--repo", "ACME/Widget"])
        self.assertEqual(code, 0)
        assert payload is not None
        self.assertEqual(list(payload["prs"]), [mine])
        self.assertEqual(payload["total_open"], 1)

    def test_empty_set_runs_with_no_rows(self) -> None:
        code, payload, _, _ = self._run(["--timeout", "30"])
        self.assertEqual(code, 2)  # the timeout, never the terminal exit 3
        assert payload is not None
        self.assertEqual((payload["prs"], payload["total_open"]), ({}, 0))

    def test_once_uses_a_stale_startup_row_with_a_warning_after_the_wait(self) -> None:
        key = self._open_pr(age_s=2000)  # older than the 900 s startup limit
        start = self.clk.now
        code, payload, err, _ = self._run(["--once"])
        self.assertEqual(code, 0)
        assert payload is not None
        self.assertEqual(self._refreshed(), [[key]])  # the startup gate asks for it
        self.assertGreaterEqual(self.clk.now - start, 30)  # waited for the refetch
        self.assertIn(f"did not refetch {key} within 30s; using the stored row", err)
        self.assertEqual(list(payload["prs"]), [key])

    def test_startup_gate_clears_without_a_warning_when_the_row_is_refetched(
        self,
    ) -> None:
        key = self._open_pr(age_s=2000)

        def refetched() -> dict[str, Any]:
            self._refetch(self._pr(3339))
            return {"seq": 10, "changes": []}  # nothing changed: no change row

        self.cli.changes.append(refetched)
        start = self.clk.now
        code, payload, err, _ = self._run(["--once"])
        self.assertEqual(code, 0)
        assert payload is not None
        self.assertEqual(self._refreshed(), [[key]])
        self.assertLess(self.clk.now - start, 30)
        self.assertNotIn("did not refetch", err)
        self.assertEqual(list(payload["prs"]), [key])

    def test_daemon_carries_a_startup_gate_and_rereads_the_key_after_each_wait(
        self,
    ) -> None:
        key = self._open_pr(age_s=2000)
        shown_before_refetch: list[list[str]] = []

        def on_wait(seconds: float) -> None:
            self.clk.sleep(seconds)
            if seconds == 5.0 and not shown_before_refetch:
                # packrat refetches during the first gated wait and finds nothing
                # new, so it writes no change row.
                shown_before_refetch.append(list(load_state(self.state_path).snapshots))
                self._store(self._pr(3339))

        self.cli.on_wait = on_wait
        code, payload, err, _ = self._run(self._daemon("--timeout", "60", "--no-a2ui"))
        self.assertEqual(code, 2)
        assert payload is not None
        self.assertEqual(shown_before_refetch, [[]])  # the old row was not shown
        self.assertEqual(list(payload["prs"]), [key])
        self.assertNotIn("did not refetch", err)
        self.assertEqual(self._refreshed(), [[key]])
        # The feed head, the 30 s startup wait, one gated wait, then the normal wait.
        self.assertEqual(self._waits()[:18], ["0", *["2"] * 15, "5", "20"])

    def test_gated_pending_key_refetched_as_merged_clears_its_gate(self) -> None:
        key = self._open_pr(age_s=2000)  # pending, with a startup gate
        refetched: list[str] = []

        def on_wait(seconds: float) -> None:
            self.clk.sleep(seconds)
            if seconds == 5.0 and not refetched:
                # packrat refetches the key during the first gated wait and finds
                # it merged. The set change has not landed yet.
                refetched.append(self._store(self._pr(3339, state="MERGED")))

        self.cli.on_wait = on_wait
        code, payload, _, mon = self._run(self._daemon("--timeout", "60", "--no-a2ui"))
        self.assertEqual(code, 2)
        assert payload is not None
        self.assertEqual(refetched, [key])
        # The refetch satisfied the gate, so the wait after it is no longer capped
        # at 5 s. The key stays pending: a merged row never shows.
        self.assertEqual(mon.gates.keys(), [])
        self.assertEqual(self._waits()[:18], ["0", *["2"] * 15, "5", "20"])
        self.assertEqual((payload["prs"], mon.rows.pending), ({}, {key}))
        self.assertEqual(self._logged_events(), [])

    def test_restart_prunes_saved_snapshots_and_once_keeps_an_unconfirmed_exit(
        self,
    ) -> None:
        kept = self._open_pr(3339)
        left = self._store(self._pr(8), age_s=300)  # open row, no longer in the set
        save_state(
            Path(self.state_path),
            MonitorState(
                snapshots={
                    kept: make_snapshot(number=3339),
                    left: make_snapshot(number=8),
                    "acme/widget#5": make_snapshot(number=5, state="CLOSED"),
                    "acme/widget#6": make_snapshot(number=6, mine=False, author="bob"),
                    "other/repo#7": make_snapshot(number=7, repo="other/repo"),
                }
            ),
        )
        code, payload, _, mon = self._run(["--once", "--repo", "acme/widget"])
        self.assertEqual(code, 0)
        assert payload is not None
        # The exit is gated and requested. No fresh row came, and --once never
        # retires an unconfirmed exit, so the row stays.
        self.assertEqual(sorted(payload["prs"]), sorted([kept, left]))
        self.assertEqual(self._refreshed(), [[left]])
        kinds = {e["kind"] for e in payload["events"]}
        self.assertFalse(kinds & {"pr_removed", "terminal", "pr_added"})
        self.assertEqual(payload["recent_retired"], [])
        self.assertEqual(
            sorted(load_state(self.state_path).snapshots), sorted([kept, left])
        )
        self.assertEqual(mon.rows.pending_exit.keys(), {left})

        # The next run gates the row again. This time packrat refetches it during
        # the startup wait and the fresh row says it merged.
        def refetched() -> dict[str, Any]:
            self._refetch(self._pr(8, state="MERGED"))
            return change(11, left)

        self.cli.calls.clear()
        self.cli.changes.append(refetched)
        code, payload, _, _ = self._run(["--once", "--repo", "acme/widget"])
        self.assertEqual(code, 0)
        assert payload is not None
        self.assertEqual(self._refreshed(), [[left]])
        self.assertEqual(list(payload["prs"]), [kept])
        self.assertIn(
            (left, "terminal"), {(e["pr"], e["kind"]) for e in payload["events"]}
        )
        self.assertEqual(
            [(e["pr"], e["state"]) for e in payload["recent_retired"]],
            [(left, "MERGED")],
        )

    def test_saved_state_with_only_closed_snapshots_announces_nothing(self) -> None:
        key = self._open_pr(3339)
        save_state(
            Path(self.state_path),
            MonitorState(
                snapshots={"acme/widget#5": make_snapshot(number=5, state="CLOSED")}
            ),
        )
        code, payload, _, _ = self._run(["--once"])
        self.assertEqual(code, 0)
        assert payload is not None
        # Pruning leaves nothing, so the first sync starts from an empty board.
        self.assertEqual(list(payload["prs"]), [key])
        self.assertEqual(payload["events"], [])
        self.assertEqual(payload["exit_reason"], "snapshot")

    # ----- row lifecycle -----------------------------------------------------------

    def test_merge_whose_set_exit_lands_first_logs_terminal(self) -> None:
        key = self._open_pr(age_s=300)
        self._run(["--once"])

        def exits_set() -> dict[str, Any]:
            # packrat writes the set change before it refetches the PR, so the
            # stored row still says OPEN.
            self._leave_set(key)
            return _viewer_change(11, key)

        def refetched() -> dict[str, Any]:
            self._refetch(self._pr(3339, state="MERGED"))
            return change(12, key)

        self.cli.calls.clear()
        self.cli.changes.extend([exits_set, refetched])
        code, payload, _, mon = self._run(
            ["--wake", "--debounce", "0", "--timeout", "200"]
        )
        self.assertEqual(code, 0)
        assert payload is not None
        kinds = [e["kind"] for e in payload["events"] if e["pr"] == key]
        self.assertIn("terminal", kinds)
        self.assertNotIn("pr_removed", kinds)
        terminal = next(e for e in payload["events"] if e["kind"] == "terminal")
        self.assertEqual((terminal["from"], terminal["to"]), ("OPEN", "MERGED"))
        self.assertEqual(payload["prs"], {})
        self.assertEqual(
            payload["recent_retired"],
            [
                {
                    "pr": key,
                    "title": "feat: add decision planner format",
                    "state": "MERGED",
                    "at": "2026-10-27T21:15:02Z",
                }
            ],
        )
        # The set-exit gate asked packrat for the key once, and shortened the wait.
        self.assertEqual(self._refreshed(), [[key]])
        self.assertEqual(self._waits(), ["0", "20", "5"])
        # Retirement dropped the snapshot, the watched key, and the lease.
        self.assertEqual(load_state(self.state_path).snapshots, {})
        self.assertEqual(mon.watched_keys, [])
        removed = [c for c in self.cli.by_command("remove") if key in c]
        self.assertEqual(len(removed), 1)
        self.assertEqual(mon.gates.keys(), [])

    def test_set_exit_that_stays_open_logs_pr_removed_without_waking(self) -> None:
        key = self._open_pr(age_s=300)
        self._run(["--once"])

        def exits_set() -> dict[str, Any]:
            self._leave_set(key)
            return _viewer_change(11, key)

        def refetched_unchanged() -> dict[str, Any]:
            # The PR is still open (its repository was archived). The refetch
            # changes nothing, so the change feed is silent.
            self._refetch(self._pr(3339))
            return {"seq": 11, "changes": []}

        self.log_path.unlink()
        self.cli.calls.clear()
        self.cli.changes.extend([exits_set, refetched_unchanged, self._stop])
        code, _, _, mon = self._run(
            self._daemon("--wake", "--debounce", "0", "--no-a2ui")
        )
        self.assertEqual(code, 0)
        silent, stopped = self._logged()
        self.assertEqual(silent["notify_mode"], "none")
        self.assertEqual(len(silent["events"]), 1)
        event = silent["events"][0]
        self.assertEqual((event["pr"], event["kind"]), (key, "pr_removed"))
        self.assertEqual((event["from"], event["to"]), ("OPEN", None))
        self.assertFalse(event["actionable"])
        self.assertEqual(event["detail"], {"reason": "left_set"})
        self.assertEqual(silent["prs"], {})
        self.assertEqual(
            [(e["pr"], e["state"]) for e in silent["recent_retired"]],
            [(key, "REMOVED")],
        )
        self.assertEqual(stopped["exit_reason"], "stopped")
        self.assertEqual(self.notifier.calls, [])  # a lone passive event never wakes
        self.assertEqual(self._refreshed(), [[key]])
        self.assertEqual(mon.gates.keys(), [])
        last = json.loads((self.dir / "last.json").read_text(encoding="utf-8"))
        self.assertEqual(last["events"][0]["kind"], "pr_removed")

    def test_set_exit_gate_lapses_into_an_unconfirmed_removal_after_600_seconds(
        self,
    ) -> None:
        key = self._open_pr(age_s=800)  # under the startup limit, older than the gate
        self._run(["--once"])
        gated_at: list[float] = []

        def exits_set() -> dict[str, Any]:
            self._leave_set(key)
            gated_at.append(self.clk.now)
            return _viewer_change(11, key)

        self.log_path.unlink()
        self.cli.calls.clear()
        self.cli.changes.append(exits_set)
        code, payload, _, _ = self._run(
            self._daemon("--timeout", "700", "--sweep", "100000", "--no-a2ui")
        )
        self.assertEqual(code, 2)
        assert payload is not None
        removed = [p for p in self._logged() if p["events"]]
        self.assertEqual(len(removed), 1)
        event = removed[0]["events"][0]
        self.assertEqual((event["pr"], event["kind"]), (key, "pr_removed"))
        self.assertEqual(event["detail"], {"reason": "left_set_unconfirmed"})
        retired = payload["recent_retired"]
        self.assertEqual([(e["pr"], e["state"]) for e in retired], [(key, "REMOVED")])
        # The row stayed for the whole 600 s and no longer.
        waited = parse_iso_seconds(retired[0]["at"]) - (
            _MINE_EPOCH + gated_at[0] - 1000
        )
        self.assertGreaterEqual(waited, 600)
        self.assertLess(waited, 610)
        self.assertEqual(payload["prs"], {})

    def test_sixty_first_pr_displaces_the_oldest_with_no_event(self) -> None:
        keys = [
            self._store(self._pr(n, updated_at=f"2026-10-27T20:{n:02d}:00Z"))
            for n in range(1, 61)
        ]
        self._in_set(*keys)
        _, seeded, _, _ = self._run(["--once", "--no-a2ui"])
        assert seeded is not None
        self.assertEqual(len(seeded["prs"]), 60)
        oldest = keys[0]

        def sixty_first() -> dict[str, Any]:
            newest = self._store(self._pr(61, updated_at="2026-10-27T21:10:00Z"))
            self._in_set(newest)
            return _viewer_change(11, newest)

        self.log_path.unlink()
        self.cli.calls.clear()
        self.cli.changes.append(sixty_first)
        code, payload, _, mon = self._run(self._daemon("--timeout", "30", "--no-a2ui"))
        self.assertEqual(code, 2)
        assert payload is not None
        self.assertEqual(len(payload["prs"]), 60)
        self.assertIn("acme/widget#61", payload["prs"])
        self.assertNotIn(oldest, payload["prs"])
        self.assertEqual(payload["total_open"], 61)
        self.assertEqual(payload["recent_retired"], [])  # displaced, not removed
        self.assertEqual(self._logged_events(), [("acme/widget#61", "pr_added")])
        self.assertNotIn(oldest, mon.watched_keys)
        self.assertNotIn(oldest, load_state(self.state_path).snapshots)
        dropped = [c for c in self.cli.by_command("remove") if oldest in c]
        self.assertEqual(len(dropped), 1)
        self.assertEqual(self._refreshed(), [])  # no gate: nothing to refetch

    def test_new_pr_shows_when_its_row_arrives_and_logs_pr_added_without_waking(
        self,
    ) -> None:
        first = self._open_pr(3339)
        self._run(["--once"])

        def enters_set() -> dict[str, Any]:
            # The key enters the set before packrat has a live row for it.
            self._in_set("acme/widget#12")
            return _viewer_change(11, "acme/widget#12")

        def row_arrives() -> dict[str, Any]:
            self._store(self._pr(12, updated_at="2026-10-27T22:00:00Z"))
            return change(12, "acme/widget#12")

        pending_before_row: list[int] = []

        def row_arrives_after_check() -> dict[str, Any]:
            pending_before_row.append(len(self.mon.rows.pending))
            return row_arrives()

        self.log_path.unlink()
        self.cli.calls.clear()
        self.cli.changes.extend([enters_set, row_arrives_after_check, self._stop])
        code, _, _, mon = self._run(
            self._daemon("--wake", "--debounce", "0", "--no-a2ui")
        )
        self.assertEqual(code, 0)
        self.assertEqual(pending_before_row, [1])  # held without a board row
        added, stopped = self._logged()
        self.assertEqual(added["notify_mode"], "none")
        event = added["events"][0]
        self.assertEqual((event["pr"], event["kind"]), ("acme/widget#12", "pr_added"))
        self.assertEqual((event["from"], event["to"]), (None, "OPEN"))
        self.assertFalse(event["actionable"])
        self.assertEqual(event["detail"], {"draft": False})
        self.assertEqual(sorted(stopped["prs"]), sorted([first, "acme/widget#12"]))
        self.assertEqual(stopped["pending"], 0)
        # The new key was leased when it entered the set.
        leased = [c for c in self.cli.by_command("add") if "acme/widget#12" in c]
        self.assertEqual(len(leased), 1)
        self.assertEqual(self.notifier.calls, [])
        self.assertEqual(mon.rows.pending, set())

    def test_pending_key_that_leaves_the_set_is_released_with_no_row_or_event(
        self,
    ) -> None:
        shown = self._open_pr(3339)
        self._run(["--once"])
        new = "acme/widget#12"
        pending_while_in_set: list[set[str]] = []

        def enters_set() -> dict[str, Any]:
            self._in_set(new)  # packrat has no live row for it yet
            return _viewer_change(11, new)

        def leaves_set() -> dict[str, Any]:
            pending_while_in_set.append(set(self.mon.rows.pending))
            self._leave_set(new)  # closed before its row ever arrived
            return _viewer_change(12, new)

        self.log_path.unlink()
        self.cli.calls.clear()
        self.cli.changes.extend([enters_set, leaves_set, self._stop])
        code, payload, _, mon = self._run(self._daemon("--debounce", "0", "--no-a2ui"))
        self.assertEqual(code, 0)
        assert payload is not None
        self.assertEqual(pending_while_in_set, [{new}])
        # Leased on entry, released on exit, and never gated or refetched.
        self.assertEqual(len([c for c in self.cli.by_command("add") if new in c]), 1)
        self.assertEqual(len([c for c in self.cli.by_command("remove") if new in c]), 1)
        self.assertEqual(self._refreshed(), [])
        self.assertEqual(self._logged_events(), [])
        self.assertEqual(list(payload["prs"]), [shown])
        self.assertEqual((payload["pending"], payload["total_open"]), (0, 1))
        self.assertEqual(payload["recent_retired"], [])
        self.assertEqual(mon.watched_keys, [shown])
        self.assertEqual((mon.rows.pending, mon.gates.keys()), (set(), []))

    def test_exiting_key_that_reenters_the_set_keeps_its_row_and_clears_the_gate(
        self,
    ) -> None:
        key = self._open_pr(age_s=300)
        self._run(["--once"])
        while_exiting: list[tuple[list[str], list[str]]] = []

        def exits_set() -> dict[str, Any]:
            self._leave_set(key)
            return _viewer_change(11, key)

        def reenters_set() -> dict[str, Any]:
            while_exiting.append((mon_gates(), list(self.mon.rows.pending_exit)))
            self._in_set(key)
            return _viewer_change(12, key)

        def mon_gates() -> list[str]:
            return self.mon.gates.keys()

        self.log_path.unlink()
        self.cli.calls.clear()
        self.cli.changes.extend([exits_set, reenters_set, self._stop])
        code, payload, _, mon = self._run(self._daemon("--debounce", "0", "--no-a2ui"))
        self.assertEqual(code, 0)
        assert payload is not None
        self.assertEqual(while_exiting, [([key], [key])])  # gated while it was out
        self.assertEqual(self._refreshed(), [[key]])
        # Back in the set: the gate is gone, so the wait returns to normal.
        self.assertEqual((mon.gates.keys(), mon.rows.pending_exit), ([], {}))
        self.assertEqual(self._waits(), ["0", "20", "5", "20"])
        # The row never left, and nothing was retired, released, or logged.
        self.assertEqual(list(payload["prs"]), [key])
        self.assertEqual(payload["recent_retired"], [])
        self.assertEqual(self._logged_events(), [])
        self.assertEqual([c for c in self.cli.by_command("remove") if key in c], [])
        self.assertEqual(mon.watched_keys, [key])

    def test_daemon_startup_gate_lapses_into_the_stored_row_after_600_seconds(
        self,
    ) -> None:
        key = self._open_pr(age_s=2000)  # packrat never refetches it
        gated_at = self.clk.now
        code, payload, err, mon = self._run(
            self._daemon("--timeout", "700", "--sweep", "100000", "--no-a2ui")
        )
        self.assertEqual(code, 2)
        assert payload is not None
        warning = f"did not refetch {key} within 600s of its startup gate; using the"
        self.assertEqual(err.count(warning), 1)
        # The stored row showed when the gate lapsed, and not before.
        shown = [p for p in self._logged() if p["events"]]
        self.assertEqual(len(shown), 1)
        self.assertEqual(
            [(e["pr"], e["kind"]) for e in shown[0]["events"]], [(key, "pr_added")]
        )
        waited = parse_iso_seconds(shown[0]["observed_at"]) - _MINE_EPOCH
        self.assertGreaterEqual(waited, 600 - (1000 - gated_at))
        self.assertLess(waited, 610)
        self.assertEqual(list(payload["prs"]), [key])
        self.assertEqual(payload["recent_retired"], [])  # a lapse retires nothing
        self.assertEqual(mon.gates.keys(), [])
        self.assertEqual(self._refreshed(), [[key]])

    def test_sweep_advances_its_timer_and_requests_pending_keys(self) -> None:
        self._open_pr(3339)
        self._in_set("acme/widget#404")  # stays without a live row
        code, payload, _, _ = self._run(
            ["--timeout", "100", "--sweep", "30", "--debounce", "0", "--no-a2ui"]
        )
        self.assertEqual(code, 2)
        assert payload is not None
        self.assertEqual(payload["pending"], 1)
        # Each sweep (at 30, 60, and 90 s) asks packrat for the row-less key.
        self.assertEqual(self._refreshed(), [["acme/widget#404"]] * 3)
        # A handled sweep moves its deadline on. A wait of 1 s would mean the loop
        # is spinning on a sweep that stays due.
        waits = self._waits()[1:]
        self.assertNotIn("1", waits)
        self.assertEqual(waits[:4], ["20", "10", "20", "10"])

    # ----- wake rules --------------------------------------------------------------

    def test_restarted_wake_daemon_logs_pr_added_for_a_new_key_without_waking(
        self,
    ) -> None:
        self._open_pr(3339)
        self._run(["--once"])
        new = self._open_pr(12, updated_at="2026-10-27T22:00:00Z")
        seeded = Path(self.state_path).read_text(encoding="utf-8")
        # --actionable-only has no effect under --mine.
        for index, extra in enumerate(([], ["--actionable-only"])):
            with self.subTest(extra=extra):
                state_path = self.dir / f"restart{index}" / "state.json"
                state_path.parent.mkdir()
                state_path.write_text(seeded, encoding="utf-8")
                self.log_path.unlink()
                self.cli.changes.append(self._stop)
                code, _, _, _ = self._run(
                    self._daemon("--wake", "--no-a2ui", *extra), str(state_path)
                )
                self.assertEqual(code, 0)
                startup = self._logged()[0]
                self.assertEqual(
                    [(e["pr"], e["kind"]) for e in startup["events"]],
                    [(new, "pr_added")],
                )
                self.assertEqual(startup["notify_mode"], "none")
                self.assertNotIn("batch_id", startup)
                self.assertEqual(self.notifier.calls, [])

    def test_passive_event_inside_an_actionable_window_joins_the_wake_batch(
        self,
    ) -> None:
        key = self._open_pr(3339)
        self._run(["--once"])

        def conflicts() -> dict[str, Any]:
            self._store({**self._pr(3339), "mergeable": "CONFLICTING"})
            return change(11, key)

        def new_pr_inside_the_window() -> dict[str, Any]:
            return _viewer_change(12, self._open_pr(12))

        def window_ends() -> dict[str, Any]:
            self.clk.now += 25
            return {"seq": 12, "changes": []}

        self.log_path.unlink()
        self.cli.changes.extend(
            [conflicts, new_pr_inside_the_window, window_ends, self._stop]
        )
        # --actionable-only would drop the passive event in the other modes.
        code, _, _, _ = self._run(
            self._daemon("--wake", "--actionable-only", "--debounce", "20", "--no-a2ui")
        )
        self.assertEqual(code, 0)
        batch = self._logged()[0]
        self.assertEqual(batch["notify_mode"], "agentapi")
        self.assertEqual(batch["batch_id"], 1)
        events = [(e["pr"], e["kind"], e["actionable"]) for e in batch["events"]]
        self.assertIn((key, "conflicts", True), events)
        self.assertIn(("acme/widget#12", "pr_added", False), events)
        self.assertEqual(len(self.notifier.calls), 1)
        self.assertEqual(self.notifier.calls[0]["conversation_id"], "conv-mine")

    def test_retired_row_leaves_the_board_before_the_debounce_ends(self) -> None:
        key = self._open_pr(3339)
        other = self._open_pr(12)
        self._run(["--once"])
        board = self.dir / "board.a2ui"
        self.assertIn("pull/3339", board.read_text(encoding="utf-8"))
        during_window: list[dict[str, Any]] = []

        def merged() -> dict[str, Any]:
            self._store(self._pr(3339, state="MERGED"))
            return change(11, key)

        def look_then_stop() -> dict[str, Any]:
            text = board.read_text(encoding="utf-8")
            during_window.append(
                {
                    "snapshots": list(load_state(self.state_path).snapshots),
                    "merged_on_board": "pull/3339" in text,
                    "other_on_board": "pull/12" in text,
                    "wakes": len(self.notifier.calls),
                    "window_open": self.clk.now < 1000 + 60,
                }
            )
            return self._stop()

        self.cli.changes.extend([merged, look_then_stop])
        code, _, _, _ = self._run(self._daemon("--wake", "--debounce", "60"))
        self.assertEqual(code, 0)
        self.assertEqual(
            during_window,
            [
                {
                    "snapshots": [other],
                    "merged_on_board": False,
                    "other_on_board": True,
                    "wakes": 0,
                    "window_open": True,
                }
            ],
        )

    def test_passive_event_before_the_ack_keeps_the_woken_batch_in_last_json(
        self,
    ) -> None:
        key = self._open_pr(3339)
        self._run(["--once"])
        last_json = self.dir / "last.json"
        board = self.dir / "board.a2ui"
        seen: list[dict[str, Any]] = []

        def conflicts() -> dict[str, Any]:
            self._store({**self._pr(3339), "mergeable": "CONFLICTING"})
            return change(11, key)

        def look() -> None:
            seen.append(
                {
                    "last": json.loads(last_json.read_text(encoding="utf-8")),
                    "board": board.read_text(encoding="utf-8"),
                    "saved": sorted(load_state(self.state_path).snapshots),
                }
            )

        def new_pr_before_the_ack() -> dict[str, Any]:
            return _viewer_change(12, self._open_pr(12))

        def look_then_ack() -> dict[str, Any]:
            look()
            with self.control.open("a", encoding="utf-8") as handle:
                handle.write('{"ack": 1}\n')
            return {"seq": 12, "changes": []}

        def new_pr_after_the_ack() -> dict[str, Any]:
            return _viewer_change(13, self._open_pr(13))

        def look_then_stop() -> dict[str, Any]:
            look()
            return self._stop()

        self.log_path.unlink()
        self.cli.changes.extend(
            [conflicts, new_pr_before_the_ack, look_then_ack, new_pr_after_the_ack]
        )
        self.cli.changes.append(look_then_stop)
        code, _, _, _ = self._run(
            self._daemon("--wake", "--debounce", "0", "--output", str(last_json))
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(self.notifier.calls), 1)
        # Before the ack, last.json keeps the woken batch, while the passive
        # event still reaches events.jsonl, the board, and state.json.
        before, after = seen
        self.assertEqual(before["last"]["batch_id"], 1)
        self.assertEqual(before["last"]["notify_mode"], "agentapi")
        self.assertEqual(
            [(e["pr"], e["kind"]) for e in before["last"]["events"]],
            [(key, "conflicts")],
        )
        self.assertIn("pull/12", before["board"])
        self.assertEqual(before["saved"], sorted([key, "acme/widget#12"]))
        batch, held, silent, _ = self._logged()
        self.assertEqual(batch["batch_id"], 1)
        self.assertEqual(held["notify_mode"], "none")
        self.assertNotIn("batch_id", held)
        self.assertEqual(
            [(e["pr"], e["kind"]) for e in held["events"]],
            [("acme/widget#12", "pr_added")],
        )
        # After the ack, a silent batch writes last.json again.
        self.assertEqual(after["last"]["notify_mode"], "none")
        self.assertEqual(
            [(e["pr"], e["kind"]) for e in after["last"]["events"]],
            [("acme/widget#13", "pr_added")],
        )
        self.assertEqual(after["last"], silent)

    # ----- source state ------------------------------------------------------------

    def test_failed_ticks_turn_a_database_file_stale_then_recovery_catches_up(
        self,
    ) -> None:
        key = self._open_pr(age_s=300)
        self._run(["--once"])
        while_stale: list[tuple[str, str]] = []

        def ticks_fail() -> dict[str, Any]:
            set_failed_ticks(self.db, 3)  # the heartbeat stays fresh
            return {"seq": 10, "changes": []}

        def stale_sleep(seconds: float) -> None:
            self.clk.sleep(seconds)
            armed = json.loads((self.dir / "armed.json").read_text(encoding="utf-8"))
            while_stale.append((self.mon.source_state.value, armed["source"]))
            if self.clk.now >= 1150:
                set_failed_ticks(self.db, 0)

        self.sleep = stale_sleep
        self.log_path.unlink()
        self.cli.calls.clear()
        self.cli.changes.append(ticks_fail)
        code, payload, err, mon = self._run(
            self._daemon("--timeout", "250", "--debounce", "0", "--no-a2ui")
        )
        self.assertEqual(code, 2)
        assert payload is not None
        # Health is read 60 s after startup, not on every wait.
        self.assertEqual(payload["stale_since"], "2026-10-27T21:16:00Z")
        self.assertTrue(while_stale)
        self.assertEqual(set(while_stale), {("stale", "packrat")})
        self.assertEqual(err.count("packrat unavailable"), 1)
        self.assertIn("3 or more ticks have failed", err)
        self.assertNotIn("falling back", err)
        # Recovery leases the key again, gates it, and asks packrat for it. The
        # stored row predates the recovery, so the state stays catching_up.
        self.assertEqual(err.count("catching up"), 1)
        self.assertEqual(payload["source_state"], "catching_up")
        self.assertEqual(payload["source"], "packrat")
        self.assertEqual(self._refreshed(), [[key]])
        self.assertEqual(len([c for c in self.cli.by_command("add") if key in c]), 2)
        self.assertEqual(mon.gates.keys(), [key])
        self.assertEqual(self._logged_events(), [])  # nothing fired from the old row
        self.assertEqual(list(payload["prs"]), [key])

    def _fail_ticks_then_heal(self, heal_at: list[float]) -> None:
        """Makes the service fail its ticks on the first wait and heal at ``heal_at[0]``.

        The heal is checked while the monitor sleeps in the ``stale`` state.
        """

        def ticks_fail() -> dict[str, Any]:
            set_failed_ticks(self.db, 3)
            return {"seq": 10, "changes": []}

        def stale_sleep(seconds: float) -> None:
            self.clk.sleep(seconds)
            if self.clk.now >= heal_at[0]:
                set_failed_ticks(self.db, 0)

        self.sleep = stale_sleep
        self.cli.changes.append(ticks_fail)

    def test_recovery_gate_lapses_into_the_stored_row_and_returns_to_live(self) -> None:
        key = self._open_pr(age_s=800)  # packrat never refetches it after the outage
        self._run(["--once"])
        states: list[tuple[str, float]] = []

        def on_wait(seconds: float) -> None:
            self.clk.sleep(seconds)
            states.append((self.mon.source_state.value, self.clk.now))

        self.cli.on_wait = on_wait
        self.log_path.unlink()
        self.cli.calls.clear()
        self._fail_ticks_then_heal([1100.0])
        code, payload, err, mon = self._run(
            self._daemon("--timeout", "800", "--sweep", "100000", "--no-a2ui")
        )
        self.assertEqual(code, 2)
        assert payload is not None
        changes = [
            (state, at)
            for index, (state, at) in enumerate(states)
            if index == 0 or states[index - 1][0] != state
        ]
        self.assertEqual(
            [state for state, _ in changes], ["live", "catching_up", "live"]
        )
        # Recovery came at 1120. The gate held for 600 s, then the stored row was
        # accepted with a warning and the state returned to live. Each state is
        # recorded during the wait that follows it.
        still_catching_up = max(at for state, at in states if state == "catching_up")
        self.assertEqual(still_catching_up, 1120 + 600)
        self.assertLessEqual(changes[2][1], 1120 + 600 + 20)
        warning = f"did not refetch {key} within 600s of its recovery gate; using the"
        self.assertEqual(err.count(warning), 1)
        self.assertEqual(payload["source_state"], "live")
        self.assertIsNone(payload["stale_since"])
        # The lapse retired nothing and fired nothing.
        self.assertEqual(list(payload["prs"]), [key])
        self.assertEqual(payload["recent_retired"], [])
        self.assertEqual(self._logged_events(), [])
        self.assertEqual(mon.gates.keys(), [])
        self.assertEqual(self._refreshed(), [[key]])

    def test_set_exit_during_an_outage_keeps_the_recovery_gate_until_the_refetch(
        self,
    ) -> None:
        key = self._open_pr(age_s=300)
        self._run(["--once"])
        seen: list[tuple[str, str, str | None]] = []

        def on_wait(seconds: float) -> None:
            self.clk.sleep(seconds)
            if not self._refreshed():
                return  # not recovered yet
            reason = self.mon.gates.reason(key)
            phase = ("recovered", "bounced", "refetched", "done")[min(len(seen), 3)]
            seen.append((phase, self.mon.source_state.value, reason and reason.value))
            if phase == "recovered":
                # Back in the set before packrat has refetched it.
                self._in_set(key)
                self.cli.changes.append(_viewer_change(12, key))
            elif phase == "bounced":
                self._store(self._pr(3339))  # the refetch that recovery asked for

        self.cli.on_wait = on_wait
        self.log_path.unlink()
        self.cli.calls.clear()
        self._fail_ticks_then_heal([1100.0])
        heal = self.sleep
        left: list[float] = []

        def outage_sleep(seconds: float) -> None:
            if not left:
                # During the outage the PR leaves the set, and packrat holds a
                # row from before the outage that the monitor never read.
                left.append(self.clk.now)
                self._leave_set(key)
                self._store({**self._pr(3339), "mergeable": "CONFLICTING"}, 300)
            heal(seconds)

        self.sleep = outage_sleep
        code, payload, err, mon = self._run(
            self._daemon("--timeout", "300", "--sweep", "100000", "--no-a2ui")
        )
        self.assertEqual(code, 2)
        assert payload is not None
        self.assertEqual(err.count("catching up"), 1)
        # The set exit kept the recovery gate, so the state stayed catching_up
        # and the return to the set did not accept the pre-outage row.
        self.assertEqual(
            seen[:3],
            [
                ("recovered", "catching_up", "recovery"),
                ("bounced", "catching_up", "recovery"),
                ("refetched", "live", None),
            ],
        )
        self.assertEqual(self._logged_events(), [])  # no conflicts from the old row
        self.assertEqual(payload["source_state"], "live")
        self.assertEqual(list(payload["prs"]), [key])
        self.assertEqual(payload["prs"][key]["mergeable"], "MERGEABLE")
        self.assertEqual(payload["recent_retired"], [])
        self.assertEqual((mon.gates.keys(), mon.rows.pending_exit), ([], {}))

    def test_second_outage_while_catching_up_turns_stale_and_gates_again(self) -> None:
        key = self._open_pr(age_s=300)
        self._run(["--once"])
        heal_at = [1100.0]
        failed_again: list[float] = []

        def on_wait(seconds: float) -> None:
            self.clk.sleep(seconds)
            if self.mon.source_state.value == "catching_up" and not failed_again:
                failed_again.append(self.clk.now)
                set_failed_ticks(self.db, 3)  # the service fails again
                heal_at[0] = 1200.0

        self.cli.on_wait = on_wait
        self.log_path.unlink()
        self.cli.calls.clear()
        self._fail_ticks_then_heal(heal_at)
        code, payload, err, mon = self._run(
            self._daemon("--timeout", "300", "--debounce", "0", "--no-a2ui")
        )
        self.assertEqual(code, 2)
        assert payload is not None
        self.assertEqual(failed_again, [1125.0])  # during the first catch-up
        # live -> stale (1060) -> catching_up (1120) -> stale (1180) -> catching_up
        # (1240). Each recovery leased the key again and sent one refresh.
        self.assertEqual(err.count("packrat unavailable"), 2)
        self.assertEqual(err.count("catching up"), 2)
        self.assertEqual(self._refreshed(), [[key], [key]])
        self.assertEqual(len([c for c in self.cli.by_command("add") if key in c]), 3)
        self.assertEqual(payload["source_state"], "catching_up")
        self.assertEqual(payload["stale_since"], "2026-10-27T21:18:00Z")  # 1180
        # Nothing retired or fired through both outages.
        self.assertEqual(list(payload["prs"]), [key])
        self.assertEqual(payload["recent_retired"], [])
        self.assertEqual(self._logged_events(), [])
        self.assertEqual([c for c in self.cli.by_command("remove") if key in c], [])
        # The key is gated at the second recovery (1240), not the first (1120): a
        # row fetched between the two is still rejected.
        self.assertEqual(mon.gates.keys(), [key])
        since_second = self.clk.now - 1240
        self.assertTrue(mon.gates.accepts(key, since_second, self.clk.now))
        self.assertFalse(mon.gates.accepts(key, since_second + 5, self.clk.now))

    def test_failed_packrat_command_turns_stale_and_a_refetch_returns_to_live(
        self,
    ) -> None:
        key = self._open_pr(age_s=300)
        self._run(["--once"])
        states: list[str] = []

        def next_command_fails() -> dict[str, Any]:
            self.cli.fail_next = "packrat: database is locked"
            return {"seq": 10, "changes": []}

        def on_wait(seconds: float) -> None:
            self.clk.sleep(seconds)
            states.append(self.mon.source_state.value)
            if self.mon.source_state.value == "catching_up":
                self._store(self._pr(3339))  # the refetch that recovery asked for

        self.cli.on_wait = on_wait
        self.cli.calls.clear()
        self.cli.changes.append(next_command_fails)
        code, payload, err, mon = self._run(["--timeout", "150", "--debounce", "0"])
        self.assertEqual(code, 2)
        assert payload is not None
        self.assertIn("packrat unavailable (packrat changes failed", err)
        self.assertEqual(err.count("catching up"), 1)
        self.assertEqual(states[:2], ["catching_up", "live"])
        self.assertEqual(payload["source_state"], "live")
        self.assertIsNone(payload["stale_since"])
        self.assertEqual(self._refreshed(), [[key]])
        self.assertEqual(mon.gates.keys(), [])

    def test_daemon_needs_the_notifier_only_with_wake(self) -> None:
        self._open_pr()
        self.notifier = None  # type: ignore[assignment]
        with unittest.mock.patch("monitor_prs.resolve_notifier", return_value=None):
            self.cli.changes.append(self._stop)
            code, payload, err, _ = self._run(self._daemon("--no-a2ui"))
            self.assertEqual(code, 0)
            assert payload is not None
            self.assertEqual(payload["exit_reason"], "stopped")
            self.assertEqual(payload["notify_mode"], "exit")
            self.assertNotIn("agentapi notifier not found", err)

            # With --wake there is nobody to wake, so it runs as exit-on-change.
            other = str(self.dir / "wake" / "state.json")
            code, _, err, _ = self._run(
                self._daemon("--wake", "--timeout", "30", "--no-a2ui"), other
            )
            self.assertEqual(code, 2)
            self.assertIn("agentapi notifier not found", err)
        self.notifier = FakeNotifier()


class TestMineReaderMode(MineCase):
    """``--mine`` on the default path, where every read is a ``packrat reader`` call."""

    KEY = "acme/widget#3339"

    def setUp(self) -> None:
        super().setUp()
        self.assertFalse(packrat_source.DEFAULT_PACKRAT_DB.exists())
        self.runner = FakeReaderRunner(beat_age_s=3, on_wait=self.clk.sleep)
        self.runner.health_extra = {"viewer_sets_synced_at": dict(_MINE_SYNCED)}
        self.runner.viewer_items = [
            {
                "set_name": "authored_open",
                "item_key": self.KEY,
                "state": "OPEN",
                "updated_at": "2026-10-27T21:00:00Z",
            }
        ]
        self._serve(age_s=1.0)

    def _serve(self, age_s: float, **changes: Any) -> None:
        """Makes packrat answer with a row for the PR that it fetched ``age_s`` ago."""
        pr = {**_review_pr_node(3339, author="alice", requested_viewer=None), **changes}
        live = _live_row(self.KEY, "acme/widget", "mirror", pr, age_s=age_s)
        self.runner.items_bundle = TestRemotePackratReaderMode._items_bundle(live)

    def _run(self, extra: list[str]) -> tuple[int, dict[str, Any] | None, str]:
        code, err = self._run_monitor(self._monitor(self.runner), self._argv(extra))
        logged = self._logged()
        return code, (logged[-1] if logged else None), err

    def _health_reads(self) -> int:
        count = 0
        for call in self.runner.calls:
            if call[0] == "reader":
                count += "health" in json.loads(call[call.index("--ops-json") + 1])
        return count

    def _assert_turns_stale(self, break_service: Any, reason: str) -> None:
        def service_breaks(seconds: float) -> None:
            self.clk.sleep(seconds)
            break_service()

        self.runner.on_wait = service_breaks
        code, payload, err = self._run(["--timeout", "200", "--debounce", "0"])
        self.assertEqual(code, 2)
        assert payload is not None
        self.assertEqual(payload["source_state"], "stale")
        self.assertEqual(
            payload["stale_since"], "2026-10-27T21:16:00Z"
        )  # the 60 s read
        self.assertEqual(payload["source"], "packrat")
        self.assertEqual(payload["membership"], "mine")
        self.assertEqual(list(payload["prs"]), [self.KEY])  # the last board stays
        self.assertEqual(payload["events"], [])
        self.assertEqual(err.count("packrat unavailable"), 1)
        self.assertIn(reason, err)
        self.assertNotIn("falling back", err)
        board = (self.dir / "board.a2ui").read_text(encoding="utf-8")
        self.assertIn("via packrat", board)
        self.assertNotIn("via GitHub", board)
        # Startup reads health three times (source check, viewer, discovery). The
        # loop then reads it once a minute: at 60 s, and at 120 and 180 s while stale.
        self.assertEqual(self._health_reads(), 3 + 3)

    def test_dead_service_on_the_default_path_turns_stale(self) -> None:
        def dies() -> None:
            self.runner.beat_age_s = 999

        self._assert_turns_stale(dies, "heartbeat is stale or missing")

    def test_three_failed_ticks_on_the_default_path_turn_stale(self) -> None:
        def ticks_fail() -> None:
            self.runner.health_extra["consecutive_failed_ticks"] = 3

        self._assert_turns_stale(ticks_fail, "3 or more ticks have failed")

    def test_never_synced_set_exits_1_on_the_default_path(self) -> None:
        self.runner.health_extra = {}  # a service that predates the sync times
        code, payload, err = self._run(["--once"])
        self.assertEqual((code, payload), (1, None))
        self.assertIn("packrat auto-sync on", err)

    def test_row_from_before_an_outage_emits_nothing_until_its_refetch(self) -> None:
        self._run(["--once"])
        states: list[str] = []
        revive_at, refetch_at = 1100.0, 1140.0

        def feed_wait(seconds: float) -> None:
            self.clk.sleep(seconds)
            states.append(self.mon.source_state.value)
            if self.clk.now < revive_at:
                # The service dies. Its last fetch found a conflict, and the
                # monitor had not read that row yet.
                self.runner.beat_age_s = 999
                self._serve(age_s=500.0, mergeable="CONFLICTING")
            elif self.clk.now >= refetch_at:
                self._serve(age_s=2.0, mergeable="CONFLICTING")

        def stale_sleep(seconds: float) -> None:
            self.clk.sleep(seconds)
            states.append(self.mon.source_state.value)
            if self.clk.now >= revive_at:
                self.runner.beat_age_s = 3

        self.runner.on_wait = feed_wait
        self.sleep = stale_sleep
        self.runner.calls.clear()
        self.log_path.unlink()
        code, payload, err = self._run(
            ["--wake", "--debounce", "0", "--timeout", "400"]
        )
        self.assertEqual(code, 0)
        assert payload is not None
        self.assertEqual(list(dict.fromkeys(states)), ["live", "stale", "catching_up"])
        # The conflict was readable from the recovery at 1120 on, but its row
        # predated the recovery. The event waited for the refetch at 1140.
        self.assertEqual(self.clk.now, refetch_at)
        self.assertIn(
            (self.KEY, "conflicts"), {(e["pr"], e["kind"]) for e in payload["events"]}
        )
        self.assertEqual(payload["source_state"], "live")
        self.assertIsNone(payload["stale_since"])
        self.assertEqual(err.count("catching up"), 1)
        calls = self.runner.calls
        # Recovery leased the key again and sent one refresh for its gate.
        self.assertEqual(len([c for c in calls if c[:2] == ["watch", "add"]]), 2)
        self.assertEqual(
            [c for c in calls if c[0] == "refresh"],
            [["refresh", "--json", "--", self.KEY]],
        )

    # ----- board -------------------------------------------------------------------

    def _headings(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Returns the title, subtitle, and badge of the board file and of the pane store.

        The ``· HH:MM UTC`` suffix of a badge is dropped: the pane store skips a
        write that changes nothing but that time.
        """
        fields = ("title", "subtitle", "status", "statusTone", "statusPulse")
        board_file: dict[str, Any] = {}
        for line in (self.dir / "board.a2ui").read_text(encoding="utf-8").splitlines():
            name, value = line.split(" = ", 1)
            board_file[name.removeprefix("$/")] = json.loads(value)
        pane = self.dir / "store" / "conversations" / "conv-mine" / "monitor.jsonl"
        record = json.loads(pane.read_text(encoding="utf-8"))
        stored = record["message"]["createSurface"]["dataModel"]
        headings = []
        for model in (board_file, stored):
            heading = {k: model[k] for k in fields}
            heading["status"] = heading["status"].split(" · ")[0]
            headings.append(heading)
        return headings[0], headings[1]

    def test_board_file_and_pane_store_show_the_stale_badge_and_subtitle(self) -> None:
        old_sync = {
            "authored_open": "2026-10-27T20:00:00Z"
        }  # 75 minutes before startup
        self.runner.health_extra = {"viewer_sets_synced_at": old_sync}

        def service_dies(seconds: float) -> None:
            self.clk.sleep(seconds)
            self.runner.beat_age_s = 999

        self.runner.on_wait = service_dies
        code, payload, _ = self._run(
            ["--conversation-id", "conv-mine", "--timeout", "200", "--debounce", "0"]
        )
        self.assertEqual(code, 2)
        assert payload is not None
        self.assertEqual(payload["source_state"], "stale")
        board_file, pane = self._headings()
        # The rows did not change when the source turned stale, and the pane still
        # got the new badge.
        self.assertEqual(board_file, pane)
        self.assertEqual(board_file["title"], "### My Open PRs (1)")
        self.assertEqual(
            (board_file["status"], board_file["statusTone"], board_file["statusPulse"]),
            ("packrat stale since 21:16 UTC", "warning", False),
        )
        self.assertEqual(
            board_file["subtitle"],
            "1 open in `acme/widget` · membership as of 20:00 UTC · via packrat",
        )
        self.assertEqual(board_file["subtitle"].count("via packrat"), 1)
        self.assertEqual(payload["a2ui"]["store_status"], "unchanged")

    def test_board_file_and_pane_store_show_catching_up_then_live(self) -> None:
        self._run(["--once", "--conversation-id", "conv-mine"])
        seen: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        revive_at, refetch_at = 1100.0, 1140.0

        def feed_wait(seconds: float) -> None:
            self.clk.sleep(seconds)
            seen.append((self.mon.source_state.value, *self._headings()))
            if self.clk.now < revive_at:
                self.runner.beat_age_s = 999
                self._serve(age_s=500.0)
            elif self.clk.now >= refetch_at:
                self._serve(age_s=2.0)

        def stale_sleep(seconds: float) -> None:
            self.clk.sleep(seconds)
            if self.clk.now >= revive_at:
                self.runner.beat_age_s = 3

        self.runner.on_wait = feed_wait
        self.sleep = stale_sleep
        code, payload, _ = self._run(
            ["--conversation-id", "conv-mine", "--debounce", "0", "--timeout", "200"]
        )
        self.assertEqual(code, 2)
        assert payload is not None
        self.assertEqual(payload["source_state"], "live")
        for state, board_file, pane in seen:
            self.assertEqual(board_file, pane, state)
            self.assertEqual(board_file["subtitle"].count("via packrat"), 1)
        catching_up = [file for state, file, _ in seen if state == "catching_up"]
        self.assertTrue(catching_up)
        self.assertEqual(catching_up[0]["status"], "catching up")
        board_file, pane = self._headings()
        self.assertEqual(board_file, pane)
        self.assertEqual(board_file["status"], "CI running")


if __name__ == "__main__":
    unittest.main()
