"""Adaptive stderr progress and diagnostic reporter for packrat."""

from __future__ import annotations

import contextlib
import shutil
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator
from typing import Literal, Protocol, TextIO

ProgressMode = Literal["auto", "plain", "off"]

DEFAULT_SQLITE_VM_PROGRESS_OPS = 250_000
"""SQLite VM opcode interval between `set_progress_handler` ticks in long statements."""


def format_duration(seconds: float) -> str:
    """Formats a duration in seconds as a compact human string (e.g., 3.1s, 4m18s, 1h02m)."""
    sec = max(0.0, float(seconds))
    if sec < 60.0:
        return f"{sec:.1f}s" if sec < 10.0 else f"{round(sec)}s"
    total_sec = round(sec)
    minutes, rem_sec = divmod(total_sec, 60)
    if minutes < 60:
        return f"{minutes}m{rem_sec:02d}s"
    hours, rem_min = divmod(minutes, 60)
    return f"{hours}h{rem_min:02d}m"


def format_bytes(num_bytes: int) -> str:
    """Formats a byte count as a compact human-readable string (B, KiB, MiB, GiB)."""
    size = max(0, int(num_bytes))
    if size >= 1024**3:
        return f"{size / (1024**3):.2f} GiB"
    if size >= 1024**2:
        return f"{size / (1024**2):.2f} MiB"
    if size >= 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size} B"


def format_eta(elapsed_sec: float, completed: int, total: int | None) -> str:
    """Computes a remaining time estimate string when a positive rate and total are known."""
    if (
        total is None
        or total <= 0
        or completed <= 0
        or completed >= total
        or elapsed_sec <= 0.0
    ):
        return ""
    rate = completed / elapsed_sec
    if rate <= 0.0:
        return ""
    remaining_sec = (total - completed) / rate
    return f"ETA {format_duration(remaining_sec)}"


def _format_counts(completed: int, total: int | None) -> str:
    """Formats item progress with thousands separators and percentage when total is known."""
    if total is not None and total > 0:
        pct = min(100, round((completed * 100.0) / total))
        return f"{completed:,}/{total:,} ({pct}%)"
    return f"{completed:,}"


def format_status_line(
    repo: str,
    phase: str,
    completed: int,
    total: int | None,
    elapsed_sec: float,
    detail: str = "",
) -> str:
    """Builds a single-line status summary for either TTY or plain heartbeat output."""
    if total is None and completed <= 0 and detail:
        return f"[{repo}] {phase}: {detail} ({format_duration(elapsed_sec)})"
    counts = _format_counts(completed, total)
    parts = [f"[{repo}] {phase}: {counts}"]
    if detail:
        parts.append(detail)
    eta = format_eta(elapsed_sec, completed, total)
    if eta:
        parts.append(eta)
    return " | ".join(parts)


class ProgressReporter(Protocol):
    """Protocol for receiving progress updates, warnings, and errors on stderr."""

    def start_phase(self, repo: str, phase: str, total: int | None = None) -> None:
        """Begins tracking a named phase for a repository."""
        ...

    def advance(
        self,
        completed: int,
        total: int | None = None,
        *,
        detail: str = "",
    ) -> None:
        """Records progress within the active phase and emits a throttled update if due."""
        ...

    def finish_phase(self, summary: str = "") -> None:
        """Finishes the active phase and clears any transient TTY status line."""
        ...

    def warn(self, repo: str, message: str) -> None:
        """Emits a persistent warning line to stderr unless quiet mode is active."""
        ...

    def error(self, repo: str, message: str) -> None:
        """Emits a persistent error line to stderr even when quiet mode is active."""
        ...

    def clear_active_line(self) -> None:
        """Clears any transient carriage-return status line before stdout/stderr output."""
        ...


class NullReporter:
    """No-op reporter used as the default when callers do not supply a reporter."""

    def start_phase(self, repo: str, phase: str, total: int | None = None) -> None:
        """No-op phase start."""

    def advance(
        self,
        completed: int,
        total: int | None = None,
        *,
        detail: str = "",
    ) -> None:
        """No-op progress update."""

    def finish_phase(self, summary: str = "") -> None:
        """No-op phase completion."""

    def warn(self, repo: str, message: str) -> None:
        """No-op warning handler."""

    def error(self, repo: str, message: str) -> None:
        """No-op error handler."""

    def clear_active_line(self) -> None:
        """No-op line clear."""


NULL_REPORTER = NullReporter()


class SqlitePhaseProgress:
    """Bridges SQLite backup and VM progress callbacks to a `ProgressReporter`.

    Shared by `packrat export` (`group="export"`) and `packrat import`
    (`group="import"`), which label every phase line with that group.
    """

    def __init__(
        self,
        reporter: ProgressReporter | None = None,
        on_progress: Callable[[str], None] | None = None,
        *,
        group: str = "export",
        vm_ops: int = DEFAULT_SQLITE_VM_PROGRESS_OPS,
    ) -> None:
        self._reporter: ProgressReporter = (
            reporter if reporter is not None else NULL_REPORTER
        )
        self._on_progress = on_progress
        self._group = group
        self._vm_ops = max(1, int(vm_ops))
        self._phase = ""
        self._started = 0.0
        self._detail = ""

    def begin(
        self,
        phase: str,
        *,
        total: int | None = None,
        detail: str = "",
    ) -> None:
        """Starts `phase`, notifies `on_progress`, and emits an initial update if due."""
        if self._on_progress is not None:
            self._on_progress(phase)
        self._phase = phase
        self._started = time.monotonic()
        self._detail = detail
        self._reporter.start_phase(self._group, phase, total=total)
        if detail:
            self._reporter.advance(0, total, detail=detail)

    def advance_pages(self, done_pages: int, total_pages: int, page_size: int) -> None:
        """Emits page and byte progress during `sqlite3.Connection.backup`."""
        detail = (
            f"{format_bytes(done_pages * page_size)} / "
            f"{format_bytes(total_pages * page_size)}"
        )
        self._reporter.advance(done_pages, total_pages, detail=detail)

    def step(self, detail: str) -> None:
        """Updates the active sub-step label and emits a throttled progress line."""
        self._detail = detail
        self._reporter.advance(0, None, detail=detail)

    def vm_tick(self) -> int:
        """SQLite `set_progress_handler` callback; returns 0 to continue execution."""
        self._reporter.advance(0, None, detail=self._detail)
        return 0

    def end(self, summary: str) -> None:
        """Finishes the active phase, printing a summary line if progress was shown."""
        elapsed = max(0.0, time.monotonic() - self._started)
        if getattr(self._reporter, "has_emitted_in_phase", False):
            self._reporter.finish_phase(
                f"{self._phase} done: {summary} ({format_duration(elapsed)})"
            )
        else:
            self._reporter.finish_phase()

    @contextlib.contextmanager
    def vm_heartbeat(self, conn: sqlite3.Connection) -> Iterator[None]:
        """Installs `vm_tick` on `conn` for the duration of long SQLite statements."""
        conn.set_progress_handler(self.vm_tick, self._vm_ops)
        try:
            yield
        finally:
            conn.set_progress_handler(None, 0)


def _resolve_effective_mode(
    mode: ProgressMode,
    quiet: bool,
    stream: TextIO,
    is_tty: bool | None,
) -> Literal["tty", "plain", "off"]:
    """Resolves user flags and stream TTY detection into tty, plain, or off."""
    if quiet or mode == "off":
        return "off"
    if mode == "plain":
        return "plain"
    tty_flag = (
        is_tty
        if is_tty is not None
        else bool(getattr(stream, "isatty", lambda: False)())
    )
    return "tty" if tty_flag else "plain"


class StderrProgressReporter:
    """Adaptive stderr reporter with a 2.0s quiet grace window, 1.0s TTY throttle, and 15.0s plain throttle."""

    def __init__(
        self,
        mode: ProgressMode = "auto",
        *,
        quiet: bool = False,
        stream: TextIO | None = None,
        is_tty: bool | None = None,
        clock: Callable[[], float] = time.monotonic,
        grace_sec: float = 2.0,
        tty_interval_sec: float = 1.0,
        plain_interval_sec: float = 15.0,
        max_width: int | None = None,
    ) -> None:
        self._stream: TextIO = stream if stream is not None else sys.stderr
        self._quiet = quiet
        self._effective_mode = _resolve_effective_mode(
            mode, quiet, self._stream, is_tty
        )
        self._clock = clock
        self._grace_sec = grace_sec
        self._tty_interval_sec = tty_interval_sec
        self._plain_interval_sec = plain_interval_sec
        self._max_width = max_width

        self._repo = ""
        self._phase = ""
        self._total: int | None = None
        self._phase_started_at = 0.0
        self._last_emit_at: float | None = None
        self._has_active_tty_line = False

    @property
    def effective_mode(self) -> Literal["tty", "plain", "off"]:
        """Returns the resolved output mode ('tty', 'plain', or 'off')."""
        return self._effective_mode

    @property
    def has_emitted_in_phase(self) -> bool:
        """Whether at least one progress update was written during the current phase."""
        return self._last_emit_at is not None

    def _terminal_limit(self) -> int:
        """Returns the maximum character width for a single-line TTY status update."""
        if self._max_width is not None and self._max_width > 10:
            return self._max_width
        cols = shutil.get_terminal_size((100, 20)).columns
        return max(40, cols - 1)

    def clear_active_line(self) -> None:
        """Clears any active transient TTY line so subsequent lines do not collide."""
        if self._has_active_tty_line:
            self._stream.write("\r\x1b[K")
            self._stream.flush()
            self._has_active_tty_line = False

    def start_phase(self, repo: str, phase: str, total: int | None = None) -> None:
        """Starts tracking a new phase and resets per-phase timers."""
        self.clear_active_line()
        self._repo = repo
        self._phase = phase
        self._total = total
        self._phase_started_at = self._clock()
        self._last_emit_at = None

    def _is_tick_due(self, now: float, elapsed: float) -> bool:
        """Returns True when the grace window and mode-specific throttle interval have elapsed."""
        if (
            self._effective_mode == "off"
            or not self._phase
            or elapsed < self._grace_sec
        ):
            return False
        if self._last_emit_at is None:
            return True
        interval = (
            self._tty_interval_sec
            if self._effective_mode == "tty"
            else self._plain_interval_sec
        )
        return (now - self._last_emit_at) >= interval

    def advance(
        self,
        completed: int,
        total: int | None = None,
        *,
        detail: str = "",
    ) -> None:
        """Emits a throttled progress update to stderr when due."""
        if total is not None:
            self._total = total
        now = self._clock()
        elapsed = max(0.0, now - self._phase_started_at)
        if not self._is_tick_due(now, elapsed):
            return

        line = format_status_line(
            self._repo, self._phase, completed, self._total, elapsed, detail
        )
        self._last_emit_at = now
        if self._effective_mode == "tty":
            clipped = line[: self._terminal_limit()]
            self._stream.write(f"\r\x1b[K{clipped}")
            self._stream.flush()
            self._has_active_tty_line = True
            return

        self._stream.write(f"{line}\n")
        self._stream.flush()

    def finish_phase(self, summary: str = "") -> None:
        """Clears any transient TTY line and optionally emits a completion summary."""
        self.clear_active_line()
        if summary and self._effective_mode != "off":
            self._stream.write(f"[{self._repo}] {summary}\n")
            self._stream.flush()
        self._phase = ""
        self._total = None
        self._last_emit_at = None

    def warn(self, repo: str, message: str) -> None:
        """Writes a persistent warning line to stderr unless quiet mode is enabled."""
        if self._quiet:
            return
        self.clear_active_line()
        self._stream.write(f"[{repo}] warning: {message}\n")
        self._stream.flush()

    def error(self, repo: str, message: str) -> None:
        """Writes a persistent error line to stderr regardless of quiet mode."""
        self.clear_active_line()
        self._stream.write(f"[{repo}] error: {message}\n")
        self._stream.flush()
