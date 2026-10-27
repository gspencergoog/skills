"""State persistence, check duration history, ETA calculation, and JSONL event log."""

from __future__ import annotations

import json
import os
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from models import CheckRun, MonitorState, PrSnapshot

MAX_DURATION_SAMPLES = 5


class StateError(Exception):
    """Raised when the monitor state file cannot be read or parsed."""


def load_state(path: str | os.PathLike[str]) -> MonitorState:
    """Loads ``MonitorState`` from ``path``, or returns ``MonitorState.empty()`` if absent."""
    file_path = Path(path)
    if not file_path.exists():
        return MonitorState.empty()
    try:
        raw = file_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise StateError(
                f"Corrupt state file {file_path}: top-level JSON value must be an object"
            )
        return MonitorState.from_dict(data)
    except (OSError, json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, StateError):
            raise
        raise StateError(f"Corrupt state file {file_path}: {exc}") from exc


def save_state(path: str | os.PathLike[str], state: MonitorState) -> None:
    """Atomically writes ``state`` to ``path``, creating parent directories if needed."""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = file_path.with_name(f".{file_path.name}.tmp.{os.getpid()}")
    payload = json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n"
    try:
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, file_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def duration_key(repo: str, check_name: str) -> str:
    return f"{repo}:{check_name}"


def parse_iso8601(ts: str | None) -> float | None:
    """Parses a GitHub ISO-8601 timestamp into POSIX seconds, or None."""
    if not ts:
        return None
    cleaned = ts.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def check_duration_seconds(check: CheckRun) -> int | None:
    """Computes a completed check's wall duration in seconds when timestamps are valid."""
    if not check.is_completed():
        return None
    start = parse_iso8601(check.started_at)
    end = parse_iso8601(check.completed_at)
    if start is None or end is None or end < start:
        return None
    seconds = round(end - start)
    return seconds if seconds > 0 else None


def record_duration(state: MonitorState, repo: str, check_name: str, seconds: int) -> None:
    """Records a check duration sample, keeping at most ``MAX_DURATION_SAMPLES`` entries."""
    if seconds <= 0:
        return
    key = duration_key(repo, check_name)
    samples = list(state.check_durations.get(key) or [])
    samples.append(int(seconds))
    state.check_durations[key] = samples[-MAX_DURATION_SAMPLES:]


def record_completed_durations(
    state: MonitorState, prev: PrSnapshot | None, cur: PrSnapshot
) -> None:
    """Records durations for checks that newly completed in ``cur``."""
    prev_completed = {c.name for c in (prev.checks if prev else []) if c.is_completed()}
    for check in cur.checks:
        if not check.is_completed():
            continue
        if prev is not None and prev.head_sha == cur.head_sha and check.name in prev_completed:
            continue
        dur = check_duration_seconds(check)
        if dur is not None:
            record_duration(state, cur.repo, check.name, dur)


def median_duration(state: MonitorState, repo: str, check_name: str) -> int | None:
    """Returns the median historical duration in seconds for ``(repo, check_name)``."""
    samples = state.check_durations.get(duration_key(repo, check_name))
    if not samples:
        return None
    return round(statistics.median(samples))


def eta_seconds(
    state: MonitorState, snap: PrSnapshot, now_epoch: float | None = None
) -> int | None:
    """Estimates remaining seconds for running required checks using historical medians."""
    relevant = [c for c in snap.effective_required_checks() if not c.is_completed()]
    if not relevant:
        return None
    best: int | None = None
    for check in relevant:
        med = median_duration(state, snap.repo, check.name)
        if med is None:
            continue
        elapsed = 0
        start = parse_iso8601(check.started_at)
        if start is not None and now_epoch is not None and now_epoch > start:
            elapsed = int(now_epoch - start)
        remaining = max(5, med - elapsed)
        if best is None or remaining > best:
            best = remaining
    return best


def append_event_log(path: str | os.PathLike[str], payload: dict[str, Any]) -> None:
    """Appends one JSON line to ``path``, creating parent directories if needed."""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, sort_keys=True) + "\n"
    with file_path.open("a", encoding="utf-8") as handle:
        handle.write(line)
