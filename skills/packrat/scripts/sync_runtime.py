"""Subprocess execution, single-flight file locking, and auto-sync run state helpers."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

SYNC_LOCK_FILENAME = "sync.lock"
SERVE_LOCK_FILENAME = "serve.lock"
AUTO_SYNC_STATE_FILENAME = "auto-sync-state.json"
_LOCK_POLL_INTERVAL_SEC = 0.2

LockMode = Literal["wait", "skip", "fail"]


@dataclass(frozen=True)
class BoundedCmdResult:
    """Captured output and exit status from a bounded subprocess execution.

    Attributes:
        returncode: Process exit code, 124 on timeout, or 127 when launch fails.
        stdout: Captured standard output decoded with replacement.
        stderr: Captured standard error or diagnostic failure message.
        timed_out: Whether execution exceeded the configured timeout.
    """

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass(frozen=True)
class SyncLockInfo:
    """Outcome and holder metadata from a single-flight sync lock attempt.

    Attributes:
        acquired: Whether the caller acquired the exclusive sync lock.
        lock_path: Path to the lock file on disk.
        holder_pid: Process ID currently holding the lock, when known.
        holder_started_at: UTC ISO-8601 timestamp when the holder acquired the lock.
    """

    acquired: bool
    lock_path: Path
    holder_pid: int | None = None
    holder_started_at: str | None = None


@dataclass(frozen=True)
class AutoSyncRunRecord:
    """Persisted summary of the most recent background auto-sync run.

    Attributes:
        ran_at: UTC ISO-8601 timestamp when the run completed.
        duration_sec: Wall-clock duration of the run in seconds.
        outcome: Terminal outcome code for the run.
        repos_total: Number of tracked repositories evaluated.
        git_commits_upserted: Total git commits indexed across repositories.
        github_items_upserted: Total GitHub issues, PRs, and discussions upserted.
        detail: Optional diagnostic message describing warnings or skip reasons.
    """

    ran_at: str
    duration_sec: float
    outcome: str
    repos_total: int = 0
    git_commits_upserted: int = 0
    github_items_upserted: int = 0
    detail: str = ""


def _utc_now_iso() -> str:
    """Returns the current UTC timestamp in ISO-8601 format."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    """Terminates the subprocess session group with SIGKILL, falling back to proc.kill()."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


def _close_process_pipes(proc: subprocess.Popen[str]) -> None:
    """Closes stdout and stderr pipes on proc so inherited handles cannot block."""
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            with contextlib.suppress(OSError):
                pipe.close()


def run_bounded_subprocess(
    cmd: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 20,
) -> BoundedCmdResult:
    """Runs a command in an isolated process group and kills the group on timeout.

    Starts the child with `start_new_session=True` and `stdin=DEVNULL` so hung
    credential helpers or SSH children cannot prompt on the terminal or keep
    inherited pipes open past `timeout`.

    Args:
        cmd: Argument vector to execute.
        cwd: Optional working directory for the child process.
        env: Optional environment mapping replacing `os.environ` for the child.
        timeout: Maximum wall-clock runtime in seconds before killing the group.

    Returns:
        A `BoundedCmdResult` containing exit status, captured streams, and timeout state.
    """
    try:
        proc = subprocess.Popen(
            list(cmd),
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=True,
        )
    except OSError as exc:
        return BoundedCmdResult(
            returncode=127,
            stdout="",
            stderr=str(exc),
            timed_out=False,
        )

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        _close_process_pipes(proc)
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            proc.wait(timeout=2)
        prog = cmd[0] if cmd else "command"
        return BoundedCmdResult(
            returncode=124,
            stdout="",
            stderr=f"{prog} timed out after {timeout}s",
            timed_out=True,
        )
    except BaseException:
        _kill_process_group(proc)
        _close_process_pipes(proc)
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            proc.wait(timeout=2)
        raise

    return BoundedCmdResult(
        returncode=proc.returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=False,
    )


def _try_flock_nonblocking(fd: int) -> bool:
    """Attempts a non-blocking exclusive flock on fd."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
            return False
        raise


def _acquire_flock_with_mode(fd: int, mode: LockMode, wait_timeout_sec: float) -> bool:
    """Acquires an exclusive flock according to the requested contention mode."""
    if mode not in ("wait", "skip", "fail"):
        msg = f"Unsupported lock mode '{mode}'; expected 'wait', 'skip', or 'fail'."
        raise ValueError(msg)
    if mode != "wait":
        return _try_flock_nonblocking(fd)

    deadline = time.monotonic() + max(0.0, wait_timeout_sec)
    while True:
        if _try_flock_nonblocking(fd):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            return False
        time.sleep(min(_LOCK_POLL_INTERVAL_SEC, remaining))


def _write_lock_holder(fd: int) -> tuple[int, str]:
    """Rewinds, truncates, and writes current process holder metadata to fd."""
    pid = os.getpid()
    started_at = _utc_now_iso()
    payload = json.dumps({"pid": pid, "started_at": started_at}).encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    view = memoryview(payload)
    written = 0
    while written < len(view):
        written += os.write(fd, view[written:])
    os.fsync(fd)
    return pid, started_at


def _read_lock_holder(lock_path: Path) -> tuple[int | None, str | None]:
    """Reads active holder metadata from lock_path, tolerating empty or corrupt JSON."""
    try:
        raw = lock_path.read_text(encoding="utf-8", errors="replace").strip()
        data = json.loads(raw) if raw else None
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    raw_pid = data.get("pid")
    is_valid_pid = isinstance(raw_pid, int) and not isinstance(raw_pid, bool)
    pid = raw_pid if is_valid_pid else None
    raw_started = data.get("started_at")
    started_at = raw_started if isinstance(raw_started, str) and raw_started else None
    return pid, started_at


@contextlib.contextmanager
def _acquire_file_lock(
    db_path: Path,
    filename: str,
    mode: LockMode,
    wait_timeout_sec: float,
) -> Iterator[SyncLockInfo]:
    """Acquires an advisory flock on `<db_path.parent>/<filename>`.

    Opens the lock file without truncation so contended callers never erase
    the active holder's metadata. Truncates and writes current holder metadata
    only after `flock` succeeds, and keeps the lock file on disk upon release.

    Args:
        db_path: Path to the target SQLite database file.
        filename: Name of the lock file inside the database directory.
        mode: Contention behavior; see `acquire_sync_lock`.
        wait_timeout_sec: Maximum seconds to poll when `mode` is "wait".

    Yields:
        A `SyncLockInfo` describing whether the lock was acquired and its holder.

    Raises:
        ValueError: If `mode` is not one of "wait", "skip", or "fail".
        OSError: If opening or locking the file fails for a non-contention reason.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = db_path.parent / filename
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    fd_open = True
    try:
        acquired = _acquire_flock_with_mode(fd, mode, wait_timeout_sec)
        if not acquired:
            os.close(fd)
            fd_open = False
            holder_pid, holder_started_at = _read_lock_holder(lock_path)
            yield SyncLockInfo(
                acquired=False,
                lock_path=lock_path,
                holder_pid=holder_pid,
                holder_started_at=holder_started_at,
            )
            return

        pid, started_at = _write_lock_holder(fd)
        yield SyncLockInfo(
            acquired=True,
            lock_path=lock_path,
            holder_pid=pid,
            holder_started_at=started_at,
        )
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        if fd_open:
            with contextlib.suppress(OSError):
                os.close(fd)


@contextlib.contextmanager
def acquire_sync_lock(
    db_path: Path,
    *,
    mode: LockMode = "wait",
    wait_timeout_sec: float = 30.0,
) -> Iterator[SyncLockInfo]:
    """Acquires an advisory single-flight lock for a packrat database directory.

    Opens `<db_path.parent>/sync.lock` without truncation so contended callers
    never erase the active holder's metadata. Truncates and writes current
    holder metadata only after `flock` succeeds, and keeps the lock file on
    disk upon release.

    Args:
        db_path: Path to the target SQLite database file.
        mode: Contention behavior ("wait" polls up to `wait_timeout_sec`;
            "skip" and "fail" return `acquired=False` immediately).
        wait_timeout_sec: Maximum seconds to poll when `mode` is "wait".

    Yields:
        A `SyncLockInfo` describing whether the lock was acquired and its holder.

    Raises:
        ValueError: If `mode` is not one of "wait", "skip", or "fail".
        OSError: If opening or locking the file fails for a non-contention reason.
    """
    with _acquire_file_lock(
        db_path, SYNC_LOCK_FILENAME, mode, wait_timeout_sec
    ) as info:
        yield info


@contextlib.contextmanager
def acquire_serve_lock(db_path: Path) -> Iterator[SyncLockInfo]:
    """Acquires the non-blocking single-instance lock for `packrat serve`.

    Uses `<db_path.parent>/serve.lock`, which is independent of `sync.lock`:
    the service holds `serve.lock` for its whole life and takes `sync.lock`
    only around individual sync runs. Holder metadata is recorded exactly as
    `acquire_sync_lock` does.

    Args:
        db_path: Path to the target SQLite database file.

    Yields:
        A `SyncLockInfo`; `acquired` is False immediately when another process
        holds the lock, with that holder's PID and start time when readable.

    Raises:
        OSError: If opening or locking the file fails for a non-contention reason.
    """
    with _acquire_file_lock(db_path, SERVE_LOCK_FILENAME, "fail", 0.0) as info:
        yield info


def write_auto_sync_run_state(db_path: Path, record: AutoSyncRunRecord) -> Path:
    """Writes the latest auto-sync run summary atomically next to `db_path`.

    Args:
        db_path: Path to the target SQLite database file.
        record: Completed auto-sync run summary to persist.

    Returns:
        Path to the written `auto-sync-state.json` file.
    """
    state_dir = db_path.parent
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / AUTO_SYNC_STATE_FILENAME
    payload = json.dumps(asdict(record), indent=2) + "\n"
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=state_dir,
        prefix=".auto-sync-state.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, state_path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise
    return state_path


def _parse_int_field(data: dict[str, Any], key: str, default: int = 0) -> int | None:
    """Extracts an integer field from decoded JSON, rejecting booleans."""
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _build_run_record(data: dict[str, Any]) -> AutoSyncRunRecord | None:
    """Validates a decoded JSON dictionary into an AutoSyncRunRecord."""
    ran_at = data.get("ran_at")
    duration = data.get("duration_sec")
    outcome = data.get("outcome")
    detail = data.get("detail", "")
    valid_scalars = (
        isinstance(ran_at, str)
        and isinstance(outcome, str)
        and isinstance(detail, str)
        and isinstance(duration, int | float)
        and not isinstance(duration, bool)
    )
    if not valid_scalars:
        return None

    repos_total = _parse_int_field(data, "repos_total")
    git_commits = _parse_int_field(data, "git_commits_upserted")
    github_items = _parse_int_field(data, "github_items_upserted")
    if repos_total is None or git_commits is None or github_items is None:
        return None

    return AutoSyncRunRecord(
        ran_at=ran_at,
        duration_sec=float(duration),
        outcome=outcome,
        repos_total=repos_total,
        git_commits_upserted=git_commits,
        github_items_upserted=github_items,
        detail=detail,
    )


def read_auto_sync_run_state(db_path: Path) -> AutoSyncRunRecord | None:
    """Reads the persisted auto-sync run summary next to `db_path`, if valid.

    Args:
        db_path: Path to the target SQLite database file.

    Returns:
        The parsed `AutoSyncRunRecord`, or `None` when missing or malformed.
    """
    state_path = db_path.parent / AUTO_SYNC_STATE_FILENAME
    try:
        raw = state_path.read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return _build_run_record(data)
