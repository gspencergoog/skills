"""Daemon mode utilities for /monitor.

Provides liveness locking, heartbeat tracking, agentapi notification delivery,
control-file parsing, and acknowledgment gating.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONTROL_FILE = "control.jsonl"
ARMED_FILE = "armed.json"
PID_FILE = "daemon.pid"
LOCK_FILE = "daemon.lock"
HEARTBEAT_FILE = "heartbeat"
EXIT_ALREADY_RUNNING = 4

RunnerFn = Callable[..., subprocess.CompletedProcess[str]]


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _touch_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.utime(path, None)
    except OSError:
        path.write_bytes(b"")


@dataclass(frozen=True)
class Notifier:
    """Invokes agentapi to send a message into a conversation."""

    argv_prefix: tuple[str, ...]
    runner: RunnerFn = subprocess.run

    def send(
        self,
        conversation_id: str,
        title: str,
        body: str,
        timeout_s: float = 15.0,
    ) -> bool:
        """Sends ``body`` to ``conversation_id`` with ``title``.

        Strips ``ANTIGRAVITY_SOURCE_METADATA`` from the child environment so
        the language server does not reject the message with a project mismatch error.
        Returns ``True`` on success, or ``False`` if the process failed, timed out,
        or could not be executed.
        """
        env = dict(os.environ)
        env.pop("ANTIGRAVITY_SOURCE_METADATA", None)
        cmd = [*self.argv_prefix, f"--title={title}", conversation_id, body]
        try:
            res = self.runner(cmd, env=env, timeout=timeout_s, capture_output=True, text=True)
            if res.returncode != 0:
                err = (res.stderr or res.stdout or "").strip()
                sys.stderr.write(f"warning: agentapi failed (code {res.returncode}): {err}\n")
                return False
            return True
        except (subprocess.TimeoutExpired, OSError) as exc:
            sys.stderr.write(f"warning: agentapi invocation failed: {exc}\n")
            return False


def resolve_notifier(
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    runner: RunnerFn = subprocess.run,
) -> Notifier | None:
    """Finds the agentapi notifier executable from environment or PATH.

    Checks:
    1. ``ANTIGRAVITY_AGENTAPI_EXE``: If present and executable, wraps it as
       ``(<exe>, "agentapi", "send-message")``.
    2. ``agentapi`` on PATH: Wraps as ``("agentapi", "send-message")``.
    3. Returns ``None`` when neither is available.
    """
    lookup_env = os.environ if env is None else env
    exe = lookup_env.get("ANTIGRAVITY_AGENTAPI_EXE")
    if exe and os.path.isfile(exe) and os.access(exe, os.X_OK):
        return Notifier(argv_prefix=(exe, "agentapi", "send-message"), runner=runner)
    path_bin = which("agentapi")
    if path_bin:
        return Notifier(argv_prefix=(path_bin, "send-message"), runner=runner)
    return None


@dataclass
class ControlCommands:
    """Batch of control instructions parsed from control.jsonl."""

    acks: list[int] = field(default_factory=list)
    overrides: list[dict[str, Any]] = field(default_factory=list)
    mark_reruns: list[str] = field(default_factory=list)
    stop: bool = False


class ControlReader:
    """Incrementally reads control instructions from a JSONL file."""

    def __init__(self, path: Path, offset: int = 0) -> None:
        self.path = Path(path)
        self.offset = max(0, int(offset))

    def read_new(self) -> ControlCommands:
        """Reads newly appended full lines from the file and returns parsed commands."""
        cmds = ControlCommands()
        if not self.path.exists():
            return cmds
        try:
            with self.path.open("rb") as f:
                f.seek(self.offset)
                raw_bytes = f.read()
                if not raw_bytes:
                    return cmds

                # If the file does not end with a newline, the last line is incomplete.
                ends_with_newline = raw_bytes.endswith(b"\n")
                lines_bytes = raw_bytes.splitlines()
                if not ends_with_newline and lines_bytes:
                    incomplete_line = lines_bytes.pop()
                    consumed_bytes = len(raw_bytes) - len(incomplete_line)
                else:
                    consumed_bytes = len(raw_bytes)

                for line_b in lines_bytes:
                    raw = line_b.decode("utf-8", errors="replace").strip()
                    if not raw:
                        continue
                    try:
                        entry = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        sys.stderr.write(
                            f"warning: ignoring malformed control line: {raw!r} ({exc})\n"
                        )
                        continue
                    if not isinstance(entry, dict):
                        continue
                    self._parse_entry(entry, cmds)

                self.offset += consumed_bytes
        except OSError as exc:
            sys.stderr.write(f"warning: error reading control file {self.path}: {exc}\n")
        return cmds

    def _parse_entry(self, entry: dict[str, Any], cmds: ControlCommands) -> None:
        self._parse_ack(entry, cmds)
        if "stop" in entry and bool(entry["stop"]):
            cmds.stop = True
        self._parse_mark_rerun(entry, cmds)
        self._parse_overrides(entry, cmds)

    def _parse_ack(self, entry: dict[str, Any], cmds: ControlCommands) -> None:
        if "ack" not in entry:
            return
        try:
            cmds.acks.append(int(entry["ack"]))
        except (ValueError, TypeError):
            pass

    def _parse_mark_rerun(self, entry: dict[str, Any], cmds: ControlCommands) -> None:
        if "mark_rerun" not in entry:
            return
        val = entry["mark_rerun"]
        if isinstance(val, list):
            cmds.mark_reruns.extend(str(item) for item in val)
        elif isinstance(val, dict):
            for k, v in val.items():
                cmds.mark_reruns.append(f"{k}={v}")
        elif isinstance(val, str):
            cmds.mark_reruns.append(val)

    def _parse_overrides(self, entry: dict[str, Any], cmds: ControlCommands) -> None:
        for key in ("pushed_sha", "uploaded_snapshot"):
            val = entry.get(key)
            if isinstance(val, dict):
                cmds.overrides.append({key: val})


@dataclass
class AckGate:
    """Enforces that only one notification batch is in-flight at a time."""

    batch_seq: int = 0
    awaiting: int | None = None
    awaiting_since: float | None = None
    last_reminder: float | None = None

    def can_send(self) -> bool:
        """Returns True if no batch is currently awaiting acknowledgment."""
        return self.awaiting is None

    def mark_sent(self, now: float) -> int:
        """Records a new batch in-flight and returns its sequence number."""
        self.batch_seq += 1
        self.awaiting = self.batch_seq
        self.awaiting_since = now
        self.last_reminder = now
        return self.batch_seq

    def ack(self, batch_id: int) -> bool:
        """Acks ``batch_id``. Releases wait if ``batch_id >= self.awaiting``."""
        if self.awaiting is not None and batch_id >= self.awaiting:
            self.awaiting = None
            self.awaiting_since = None
            self.last_reminder = None
            return True
        return False

    def reminder_due(self, now: float, reminder_s: float) -> bool:
        """Returns True if an unacknowledged batch has exceeded the reminder threshold."""
        if self.awaiting is None or self.last_reminder is None:
            return False
        return (now - self.last_reminder) >= reminder_s

    def record_reminder(self, now: float) -> None:
        """Updates the reminder timestamp to prevent immediate re-triggering."""
        self.last_reminder = now

    def to_dict(self) -> dict[str, Any]:
        """Serializes gate state for persistence."""
        return {
            "batch_seq": self.batch_seq,
            "awaiting": self.awaiting,
            "awaiting_since": self.awaiting_since,
            "last_reminder": self.last_reminder,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> AckGate:
        """Deserializes gate state from a dictionary."""
        if not data:
            return cls()
        return cls(
            batch_seq=int(data.get("batch_seq", 0)),
            awaiting=int(data["awaiting"]) if data.get("awaiting") is not None else None,
            awaiting_since=float(data["awaiting_since"])
            if data.get("awaiting_since") is not None
            else None,
            last_reminder=float(data["last_reminder"])
            if data.get("last_reminder") is not None
            else None,
        )


class Liveness:
    """Manages process locking, PID persistence, and heartbeat tracking."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.pid_path = self.state_dir / PID_FILE
        self.lock_path = self.state_dir / LOCK_FILE
        self.armed_path = self.state_dir / ARMED_FILE
        self.heartbeat_path = self.state_dir / HEARTBEAT_FILE
        self._lock_fd: int | None = None

    def acquire(self) -> tuple[bool, int | None]:
        """Attempts to acquire an exclusive lock for this daemon.

        Returns ``(True, None)`` on success. If another process holds the lock,
        reads and returns ``(False, existing_pid)``.
        """
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_fd = fd
        except (BlockingIOError, OSError):
            existing_pid = None
            if self.pid_path.exists():
                try:
                    existing_pid = int(self.pid_path.read_text().strip())
                except (ValueError, OSError):
                    pass
            return False, existing_pid

        _atomic_write_text(self.pid_path, f"{os.getpid()}\n")
        self.beat()
        return True, None

    def arm(self, meta: Mapping[str, Any]) -> None:
        """Records metadata in armed.json and touches heartbeat."""
        payload = dict(meta)
        payload["pid"] = os.getpid()
        _atomic_write_text(self.armed_path, json.dumps(payload, indent=2) + "\n")
        self.beat()

    def disarm(self) -> None:
        """Removes armed.json to indicate clean shutdown."""
        try:
            self.armed_path.unlink(missing_ok=True)
        except OSError:
            pass

    def beat(self) -> None:
        """Updates the mtime of the heartbeat file."""
        _touch_file(self.heartbeat_path)

    def release(self) -> None:
        """Releases the lock and cleans up pid and lock files."""
        try:
            self.pid_path.unlink(missing_ok=True)
        except OSError:
            pass
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None


def format_batch_message(
    skill: str,
    batch_id: int,
    summary: str,
    events: Sequence[Mapping[str, Any]],
    last_json: Path,
    control: Path,
    max_lines: int = 8,
) -> tuple[str, str]:
    """Formats notification title and body for an actionable batch."""
    title = f"[{skill}] Batch {batch_id}: {summary}"
    lines: list[str] = [
        f"**{skill}** detected {len(events)} event(s) requiring action.",
        f"Details: `{last_json}`",
        "",
    ]
    for ev in events[:max_lines]:
        kind = ev.get("kind", "event")
        target = ev.get("pr") or ev.get("cl") or ""
        msg = ev.get("summary") or ev.get("message") or ""
        lines.append(f"- **{target}** ({kind}): {msg}")
    if len(events) > max_lines:
        lines.append(f"- ... and {len(events) - max_lines} more in `{last_json.name}`")

    lines.extend(
        [
            "",
            "To acknowledge after remediation:",
            f"```bash\necho '{{\"ack\": {batch_id}}}' >> {control}\n```",
        ]
    )
    return title, "\n".join(lines)
