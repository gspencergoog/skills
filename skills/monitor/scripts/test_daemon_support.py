"""Unit tests for daemon_support.py in /monitor."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from daemon_support import (
    ARMED_FILE,
    CONTROL_FILE,
    HEARTBEAT_FILE,
    PID_FILE,
    AckGate,
    ControlReader,
    Liveness,
    Notifier,
    format_batch_message,
    resolve_notifier,
)


class TestNotifier(unittest.TestCase):
    def test_resolve_prefers_antigravity_agentapi_exe(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            exe = Path(td) / "hub-server"
            exe.write_text("#!/bin/sh\nexit 0\n")
            exe.chmod(0o755)
            env = {"ANTIGRAVITY_AGENTAPI_EXE": str(exe)}
            notifier = resolve_notifier(env=env, which=lambda _: "/usr/bin/agentapi")
            self.assertIsNotNone(notifier)
            assert notifier is not None
            self.assertEqual(notifier.argv_prefix, (str(exe), "agentapi", "send-message"))

    def test_resolve_falls_back_to_path(self) -> None:
        notifier = resolve_notifier(env={}, which=lambda _: "/usr/local/bin/agentapi")
        self.assertIsNotNone(notifier)
        assert notifier is not None
        self.assertEqual(notifier.argv_prefix, ("/usr/local/bin/agentapi", "send-message"))

    def test_resolve_returns_none_when_unavailable(self) -> None:
        self.assertIsNone(resolve_notifier(env={}, which=lambda _: None))

    def test_send_strips_antigravity_source_metadata(self) -> None:
        calls: list[dict[str, Any]] = []

        def fake_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append({"cmd": cmd, "env": kwargs.get("env")})
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

        notifier = Notifier(argv_prefix=("agentapi", "send-message"), runner=fake_runner)
        with mock.patch.dict(
            os.environ, {"ANTIGRAVITY_SOURCE_METADATA": "secret", "OTHER": "keep"}
        ):
            success = notifier.send("conv-123", "Title", "Body")

        self.assertTrue(success)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0]["cmd"],
            ["agentapi", "send-message", "--title=Title", "conv-123", "Body"],
        )
        self.assertNotIn("ANTIGRAVITY_SOURCE_METADATA", calls[0]["env"])
        self.assertEqual(calls[0]["env"].get("OTHER"), "keep")

    def test_send_handles_nonzero_exit_and_exceptions(self) -> None:
        def failing_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="permission denied")

        def raising_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd, 15.0)

        n1 = Notifier(argv_prefix=("agentapi", "send-message"), runner=failing_runner)
        self.assertFalse(n1.send("conv-1", "T", "B"))

        n2 = Notifier(argv_prefix=("agentapi", "send-message"), runner=raising_runner)
        self.assertFalse(n2.send("conv-1", "T", "B"))


class TestControlReader(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.control_path = Path(self.td.name) / CONTROL_FILE

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_reads_valid_commands_and_advances_offset(self) -> None:
        reader = ControlReader(self.control_path)
        cmds0 = reader.read_new()
        self.assertEqual(cmds0.acks, [])
        self.assertFalse(cmds0.stop)

        self.control_path.write_text(
            '{"ack": 1}\n'
            '{"pushed_sha": {"owner/repo#1": "sha1"}}\n'
            '{"mark_rerun": ["owner/repo#1=ci@sha1"]}\n'
            '{"mark_rerun": {"owner/repo#2": "ci@sha2"}}\n'
            '{"stop": true}\n'
        )
        cmds1 = reader.read_new()
        self.assertEqual(cmds1.acks, [1])
        self.assertTrue(cmds1.stop)
        self.assertEqual(
            cmds1.mark_reruns, ["owner/repo#1=ci@sha1", "owner/repo#2=ci@sha2"]
        )
        self.assertEqual(cmds1.overrides, [{"pushed_sha": {"owner/repo#1": "sha1"}}])
        self.assertGreater(reader.offset, 0)

        # Subsequent read with no additions returns empty commands
        cmds2 = reader.read_new()
        self.assertEqual(cmds2.acks, [])
        self.assertFalse(cmds2.stop)

    def test_handles_partial_line_and_invalid_json(self) -> None:
        reader = ControlReader(self.control_path)
        # Write one full line and one partial line (no trailing newline)
        self.control_path.write_text('{"ack": 2}\n{"ack": 3')
        cmds1 = reader.read_new()
        self.assertEqual(cmds1.acks, [2])

        # Complete the second line and add invalid json
        self.control_path.write_text('{"ack": 2}\n{"ack": 3}\nnot json\n{"ack": 4}\n')
        cmds2 = reader.read_new()
        self.assertEqual(cmds2.acks, [3, 4])


class TestAckGate(unittest.TestCase):
    def test_transitions_and_acking(self) -> None:
        gate = AckGate()
        self.assertTrue(gate.can_send())
        self.assertFalse(gate.reminder_due(now=100.0, reminder_s=60.0))

        b1 = gate.mark_sent(now=10.0)
        self.assertEqual(b1, 1)
        self.assertEqual(gate.awaiting, 1)
        self.assertFalse(gate.can_send())

        # Acking an older batch does not clear awaiting
        self.assertFalse(gate.ack(0))
        self.assertFalse(gate.can_send())

        # Reminders
        self.assertFalse(gate.reminder_due(now=50.0, reminder_s=60.0))
        self.assertTrue(gate.reminder_due(now=80.0, reminder_s=60.0))
        gate.record_reminder(now=80.0)
        self.assertFalse(gate.reminder_due(now=90.0, reminder_s=60.0))

        # Acking clears awaiting
        self.assertTrue(gate.ack(1))
        self.assertTrue(gate.can_send())
        self.assertIsNone(gate.awaiting)

    def test_to_and_from_dict(self) -> None:
        gate = AckGate(batch_seq=5, awaiting=5, awaiting_since=123.4, last_reminder=130.0)
        d = gate.to_dict()
        restored = AckGate.from_dict(d)
        self.assertEqual(restored.batch_seq, 5)
        self.assertEqual(restored.awaiting, 5)
        self.assertEqual(restored.awaiting_since, 123.4)
        self.assertEqual(restored.last_reminder, 130.0)


class TestLiveness(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)
        self.liveness = Liveness(self.dir)

    def tearDown(self) -> None:
        self.liveness.release()
        self.td.cleanup()

    def test_acquire_and_release(self) -> None:
        ok, existing = self.liveness.acquire()
        self.assertTrue(ok)
        self.assertIsNone(existing)
        self.assertTrue((self.dir / PID_FILE).exists())
        self.assertTrue((self.dir / HEARTBEAT_FILE).exists())
        self.assertEqual(int((self.dir / PID_FILE).read_text().strip()), os.getpid())

        # Second acquire in a subprocess fails with our PID
        script = f"""
import sys
from pathlib import Path
from daemon_support import Liveness
l = Liveness(Path({str(self.dir)!r}))
ok, pid = l.acquire()
sys.exit(0 if not ok and pid == {os.getpid()} else 1)
"""
        res = subprocess.run([sys.executable, "-c", script], check=False, cwd=os.path.dirname(__file__))
        self.assertEqual(res.returncode, 0)

        # Release cleans up PID file and unlocks
        self.liveness.release()
        self.assertFalse((self.dir / PID_FILE).exists())

    def test_arm_and_disarm(self) -> None:
        self.liveness.acquire()
        self.liveness.arm({"argv": ["monitor_prs.py", "--daemon"], "poll_s": 120})
        armed = self.dir / ARMED_FILE
        self.assertTrue(armed.exists())
        data = json.loads(armed.read_text())
        self.assertEqual(data["argv"], ["monitor_prs.py", "--daemon"])
        self.assertEqual(data["pid"], os.getpid())

        self.liveness.disarm()
        self.assertFalse(armed.exists())


class TestFormatBatchMessage(unittest.TestCase):
    def test_formats_message_with_cutoff(self) -> None:
        events = [
            {"kind": "ci", "pr": f"repo#{i}", "summary": f"failed check {i}"} for i in range(12)
        ]
        last_json = Path("/tmp/last.json")
        control = Path("/tmp/control.jsonl")
        title, body = format_batch_message(
            skill="PR Monitor",
            batch_id=3,
            summary="12 checks failed",
            events=events,
            last_json=last_json,
            control=control,
            max_lines=5,
        )
        self.assertEqual(title, "[PR Monitor] Batch 3: 12 checks failed")
        self.assertIn("detected 12 event(s)", body)
        self.assertIn("- **repo#0** (ci): failed check 0", body)
        self.assertIn("- **repo#4** (ci): failed check 4", body)
        self.assertIn("- ... and 7 more in `last.json`", body)
        self.assertIn("echo '{\"ack\": 3}' >> /tmp/control.jsonl", body)


if __name__ == "__main__":
    unittest.main()
