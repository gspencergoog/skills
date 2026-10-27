"""Unit tests for monitor_prs.py."""

from __future__ import annotations

import io
import json
import signal
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from monitor_prs import Monitor, parse_args
from state import load_state
from testing import FakeGh, load_fixture


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
            self.assertTrue(Path(payload1["a2ui"]["dsl_file"]).exists())
            self.assertTrue((Path(store_dir) / "conversations" / "conv-test-1.jsonl").exists())
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
                {"review_decision", "review_requests", "human_threads", "merge_state", "ci_check"},
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
            conv_lines = (
                (Path(store_dir) / "conversations" / "conv-passive.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            )
            # 1 createSurface + 2 updateDataModel (passive tick) + 2 updateDataModel (actionable emit) = 5 lines
            self.assertEqual(len(conv_lines), 5)

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
                    ("notifications", 1, load_fixture("notifications_304.txt"), "gh: HTTP 304"),
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


if __name__ == "__main__":
    unittest.main()
