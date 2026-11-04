"""Unit tests for bounded subprocess execution, sync file locks, and run-state JSON."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

try:
    from .sync_runtime import (
        AUTO_SYNC_STATE_FILENAME,
        SERVE_LOCK_FILENAME,
        SYNC_LOCK_FILENAME,
        AutoSyncRunRecord,
        acquire_serve_lock,
        acquire_sync_lock,
        read_auto_sync_run_state,
        run_bounded_subprocess,
        write_auto_sync_run_state,
    )
except ImportError:
    from sync_runtime import (  # type: ignore[no-redef]
        AUTO_SYNC_STATE_FILENAME,
        SERVE_LOCK_FILENAME,
        SYNC_LOCK_FILENAME,
        AutoSyncRunRecord,
        acquire_serve_lock,
        acquire_sync_lock,
        read_auto_sync_run_state,
        run_bounded_subprocess,
        write_auto_sync_run_state,
    )

_SCRIPTS_DIR = Path(__file__).resolve().parent

_CHILD_LOCK_HOLDER_CODE = """\
import sys
from pathlib import Path
from sync_runtime import acquire_sync_lock

db_path = Path(sys.argv[1])
with acquire_sync_lock(db_path, mode="fail") as info:
    if not info.acquired:
        sys.exit(2)
    sys.stdout.write("READY\\n")
    sys.stdout.flush()
    sys.stdin.read()
"""

_CHILD_SERVE_LOCK_HOLDER_CODE = """\
import sys
from pathlib import Path
from sync_runtime import acquire_serve_lock

db_path = Path(sys.argv[1])
with acquire_serve_lock(db_path) as info:
    if not info.acquired:
        sys.exit(2)
    sys.stdout.write("READY\\n")
    sys.stdout.flush()
    sys.stdin.read()
"""

_CHILD_RAW_FLOCK_CODE = """\
import fcntl
import os
import sys

lock_path = sys.argv[1]
payload = sys.argv[2].encode("utf-8")
fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
os.ftruncate(fd, 0)
if payload:
    os.write(fd, payload)
os.fsync(fd)
sys.stdout.write("READY\\n")
sys.stdout.flush()
sys.stdin.read()
os.close(fd)
"""


def _is_pid_alive(pid: int) -> bool:
    """Checks whether pid is an active non-zombie OS process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.exists():
        with contextlib.suppress(OSError):
            stat_text = stat_path.read_text(encoding="utf-8", errors="replace")
            fields = stat_text.rsplit(")", 1)
            if len(fields) == 2 and fields[1].strip().startswith("Z"):
                return False
    return True


def _wait_until_pid_dead(pid: int, timeout_sec: float = 1.5) -> bool:
    """Polls briefly until pid exits or is reaped."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if not _is_pid_alive(pid):
            return True
        time.sleep(0.02)
    return not _is_pid_alive(pid)


@contextlib.contextmanager
def _spawn_lock_holder_process(db_path: Path, code: str = _CHILD_LOCK_HOLDER_CODE):
    """Spawns a child process running `code` (a lock holder) until context exit."""
    env = {**os.environ, "PYTHONPATH": str(_SCRIPTS_DIR)}
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(db_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        ready = proc.stdout.readline().strip() if proc.stdout is not None else ""
        if ready != "READY":
            stderr = proc.stderr.read() if proc.stderr is not None else ""
            raise RuntimeError(f"Lock holder child failed to start: {stderr}")
        yield proc
    finally:
        if proc.stdin is not None and not proc.stdin.closed:
            proc.stdin.close()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=3)
        if proc.returncode is None:
            proc.kill()
            proc.wait(timeout=2)
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()


class TestBoundedSubprocess(unittest.TestCase):
    """Tests stdout/stderr capture, missing binaries, and process-group timeout reaping."""

    def test_captures_stdout_stderr_exit_code_cwd_and_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            code = (
                "import os, sys; "
                "print(os.getcwd()); "
                "print(os.environ.get('PACKRAT_TEST_VAR', '')); "
                "sys.stderr.write('warn-line\\n'); "
                "sys.exit(3)"
            )
            env = {**os.environ, "PACKRAT_TEST_VAR": "custom-val"}
            res = run_bounded_subprocess(
                [sys.executable, "-c", code],
                cwd=tmp_dir,
                env=env,
                timeout=10,
            )
            self.assertFalse(res.timed_out)
            self.assertEqual(res.returncode, 3)
            lines = res.stdout.strip().splitlines()
            self.assertEqual(Path(lines[0]).resolve(), tmp_dir.resolve())
            self.assertEqual(lines[1], "custom-val")
            self.assertEqual(res.stderr, "warn-line\n")

    def test_missing_binary_returns_127(self) -> None:
        res = run_bounded_subprocess(["/nonexistent/packrat-missing-bin"], timeout=5)
        self.assertFalse(res.timed_out)
        self.assertEqual(res.returncode, 127)
        self.assertTrue(res.stderr)

    def test_timeout_kills_grandchild_holding_stdout_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "grandchild.pid"
            child_script = (
                "import os, subprocess, sys, time\n"
                "pid_path = sys.argv[1]\n"
                "gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "tmp_path = pid_path + '.tmp'\n"
                "with open(tmp_path, 'w', encoding='utf-8') as fh:\n"
                "    fh.write(str(gc.pid))\n"
                "os.replace(tmp_path, pid_path)\n"
                "time.sleep(60)\n"
            )
            started = time.monotonic()
            res = run_bounded_subprocess(
                [sys.executable, "-c", child_script, str(pid_file)],
                timeout=1,
            )
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 3.0)
            self.assertTrue(res.timed_out)
            self.assertEqual(res.returncode, 124)
            self.assertEqual(res.stdout, "")
            self.assertIn("timed out", res.stderr)

            grandchild_pid = int(pid_file.read_text(encoding="utf-8").strip())
            self.assertTrue(
                _wait_until_pid_dead(grandchild_pid),
                f"Grandchild PID {grandchild_pid} survived process-group kill",
            )

    def test_timeout_reaps_orphan_grandchild_after_child_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "orphan_grandchild.pid"
            child_script = (
                "import os, subprocess, sys\n"
                "pid_path = sys.argv[1]\n"
                "gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "with open(pid_path, 'w', encoding='utf-8') as fh:\n"
                "    fh.write(str(gc.pid))\n"
            )
            started = time.monotonic()
            res = run_bounded_subprocess(
                [sys.executable, "-c", child_script, str(pid_file)],
                timeout=1,
            )
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 3.0)
            self.assertTrue(res.timed_out)
            self.assertEqual(res.returncode, 124)

            grandchild_pid = int(pid_file.read_text(encoding="utf-8").strip())
            self.assertTrue(_wait_until_pid_dead(grandchild_pid))


class TestAcquireSyncLock(unittest.TestCase):
    """Tests single-flight flock acquisition, metadata preservation, and wait polling."""

    def test_uncontended_lock_writes_metadata_and_keeps_file_on_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "nested" / "repos.sqlite"
            with acquire_sync_lock(db_path) as info:
                self.assertTrue(info.acquired)
                self.assertEqual(info.lock_path, db_path.parent / SYNC_LOCK_FILENAME)
                self.assertEqual(info.holder_pid, os.getpid())
                self.assertIsNotNone(info.holder_started_at)
                on_disk = json.loads(info.lock_path.read_text(encoding="utf-8"))
                self.assertEqual(on_disk["pid"], os.getpid())
                self.assertEqual(on_disk["started_at"], info.holder_started_at)

            self.assertTrue((db_path.parent / SYNC_LOCK_FILENAME).is_file())

    def test_contended_skip_and_fail_preserve_holder_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "repos.sqlite"
            lock_path = db_path.parent / SYNC_LOCK_FILENAME
            with _spawn_lock_holder_process(db_path) as child:
                raw_before = lock_path.read_bytes()
                holder_meta = json.loads(raw_before.decode("utf-8"))
                self.assertEqual(holder_meta["pid"], child.pid)

                holder_started = holder_meta["started_at"]

                for mode in ("skip", "fail"):
                    with acquire_sync_lock(db_path, mode=mode) as contended:
                        self.assertFalse(contended.acquired)
                        self.assertEqual(contended.lock_path, lock_path)
                        self.assertEqual(contended.holder_pid, child.pid)
                        self.assertEqual(contended.holder_started_at, holder_started)
                        self.assertEqual(lock_path.read_bytes(), raw_before)

                with acquire_sync_lock(
                    db_path,
                    mode="wait",
                    wait_timeout_sec=0.25,
                ) as timed_out:
                    self.assertFalse(timed_out.acquired)
                    self.assertEqual(timed_out.holder_pid, child.pid)
                    self.assertEqual(lock_path.read_bytes(), raw_before)

    def test_wait_mode_acquires_after_child_releases_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "repos.sqlite"
            with _spawn_lock_holder_process(db_path) as child:

                def _release_soon() -> None:
                    time.sleep(0.3)
                    if child.stdin is not None and not child.stdin.closed:
                        child.stdin.close()

                releaser = threading.Thread(target=_release_soon, daemon=True)
                releaser.start()

                with acquire_sync_lock(
                    db_path,
                    mode="wait",
                    wait_timeout_sec=5.0,
                ) as info:
                    self.assertTrue(info.acquired)
                    self.assertEqual(info.holder_pid, os.getpid())
                    on_disk = json.loads(info.lock_path.read_text(encoding="utf-8"))
                    self.assertEqual(on_disk["pid"], os.getpid())
                releaser.join(timeout=2.0)

    def test_contended_lock_tolerates_corrupt_or_empty_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "repos.sqlite"
            lock_path = db_path.parent / SYNC_LOCK_FILENAME
            for payload in ("", "{not-valid-json"):
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        _CHILD_RAW_FLOCK_CODE,
                        str(lock_path),
                        payload,
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    self.assertEqual(
                        proc.stdout.readline().strip() if proc.stdout else "",
                        "READY",
                    )
                    with acquire_sync_lock(db_path, mode="skip") as info:
                        self.assertFalse(info.acquired)
                        self.assertIsNone(info.holder_pid)
                        self.assertIsNone(info.holder_started_at)
                        self.assertEqual(lock_path.read_text(encoding="utf-8"), payload)
                finally:
                    if proc.stdin is not None:
                        proc.stdin.close()
                    proc.wait(timeout=3)
                    for pipe in (proc.stdout, proc.stderr):
                        if pipe is not None:
                            pipe.close()

    def test_invalid_lock_mode_raises_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "repos.sqlite"
            with (
                self.assertRaises(ValueError),
                acquire_sync_lock(db_path, mode="bogus"),
            ):  # type: ignore[arg-type]
                pass


class TestAcquireServeLock(unittest.TestCase):
    """Tests the single-instance serve.lock and its independence from sync.lock."""

    def test_uncontended_serve_lock_writes_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "nested" / "repos.sqlite"
            with acquire_serve_lock(db_path) as info:
                self.assertTrue(info.acquired)
                self.assertEqual(info.lock_path, db_path.parent / SERVE_LOCK_FILENAME)
                self.assertEqual(info.holder_pid, os.getpid())
                on_disk = json.loads(info.lock_path.read_text(encoding="utf-8"))
                self.assertEqual(on_disk["pid"], os.getpid())
                self.assertEqual(on_disk["started_at"], info.holder_started_at)
            self.assertTrue((db_path.parent / SERVE_LOCK_FILENAME).is_file())

    def test_serve_lock_is_exclusive_across_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "repos.sqlite"
            lock_path = db_path.parent / SERVE_LOCK_FILENAME
            with _spawn_lock_holder_process(
                db_path, _CHILD_SERVE_LOCK_HOLDER_CODE
            ) as child:
                raw_before = lock_path.read_bytes()
                started = time.monotonic()
                with acquire_serve_lock(db_path) as contended:
                    self.assertLess(time.monotonic() - started, 1.0)
                    self.assertFalse(contended.acquired)
                    self.assertEqual(contended.holder_pid, child.pid)
                    self.assertIsNotNone(contended.holder_started_at)
                    self.assertEqual(lock_path.read_bytes(), raw_before)
            with acquire_serve_lock(db_path) as after:
                self.assertTrue(after.acquired)

    def test_serve_lock_is_independent_of_sync_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "repos.sqlite"
            with (
                _spawn_lock_holder_process(db_path, _CHILD_SERVE_LOCK_HOLDER_CODE),
                acquire_sync_lock(db_path, mode="fail") as sync_info,
            ):
                self.assertTrue(sync_info.acquired)
            with (
                _spawn_lock_holder_process(db_path),
                acquire_serve_lock(db_path) as serve_info,
            ):
                self.assertTrue(serve_info.acquired)
                self.assertNotEqual(
                    serve_info.lock_path, db_path.parent / SYNC_LOCK_FILENAME
                )


class TestAutoSyncRunState(unittest.TestCase):
    """Tests atomic persistence and corrupt-file recovery for auto-sync-state.json."""

    def test_round_trip_and_atomic_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state_dir" / "repos.sqlite"
            expected = AutoSyncRunRecord(
                ran_at="2026-11-03T02:55:00Z",
                duration_sec=4.25,
                outcome="degraded_keyring_locked",
                repos_total=3,
                git_commits_upserted=14,
                github_items_upserted=0,
                detail="gh auth token timed out; indexed local git only",
            )
            written_path = write_auto_sync_run_state(db_path, expected)
            self.assertEqual(written_path, db_path.parent / AUTO_SYNC_STATE_FILENAME)
            self.assertTrue(written_path.is_file())
            leftover_tmps = list(db_path.parent.glob(".auto-sync-state.*.tmp"))
            self.assertEqual(leftover_tmps, [])

            loaded = read_auto_sync_run_state(db_path)
            self.assertEqual(loaded, expected)

    def test_round_trips_deferred_rate_limit_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "repos.sqlite"
            expected = AutoSyncRunRecord(
                ran_at="2026-11-03T22:40:00Z",
                duration_sec=1.5,
                outcome="deferred_rate_limit",
                repos_total=2,
                git_commits_upserted=3,
                github_items_upserted=4,
                detail="deferred by GitHub rate budget (octo-org/widgets: issues, prs)",
            )
            write_auto_sync_run_state(db_path, expected)
            self.assertEqual(read_auto_sync_run_state(db_path), expected)

    def test_missing_or_corrupt_state_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "repos.sqlite"
            self.assertIsNone(read_auto_sync_run_state(db_path))

            state_path = db_path.parent / AUTO_SYNC_STATE_FILENAME
            for bad_content in (
                "{corrupt-json",
                "[1, 2, 3]",
                "{}",
                json.dumps({"ran_at": 123, "duration_sec": 1.0, "outcome": "ok"}),
                json.dumps(
                    {
                        "ran_at": "2026-11-03T00:00:00Z",
                        "duration_sec": True,
                        "outcome": "ok",
                    }
                ),
                json.dumps(
                    {
                        "ran_at": "2026-11-03T00:00:00Z",
                        "duration_sec": 1.0,
                        "outcome": "ok",
                        "repos_total": "three",
                    }
                ),
            ):
                state_path.write_text(bad_content, encoding="utf-8")
                self.assertIsNone(read_auto_sync_run_state(db_path))


if __name__ == "__main__":
    unittest.main()
