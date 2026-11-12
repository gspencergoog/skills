"""Shared GitHub rate budget derived from `X-RateLimit-*` response headers.

Every `gh api -i` response carries the caller's remaining budget for the
resource it consumed. `RateBudget` records the newest value per resource in
`<data_dir>/rate_budget.json` so that concurrent packrat processes share one
view of the budget without spending requests on `GET /rate_limit`.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict, dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any

RATE_BUDGET_FILENAME = "rate_budget.json"
RATE_BUDGET_LOCK_FILENAME = "rate_budget.lock"

RESERVE_FRACTION = 0.20
"""Below this fraction of the limit, `MIRROR` and `BACKFILL` work is deferred."""

FLOOR_FRACTION = 0.05
"""Below this fraction of the limit, only `HOT` work runs."""

_HEADER_PREFIX = "x-ratelimit-"


class Priority(IntEnum):
    """Scheduling priority of a GitHub request; lower values are more urgent."""

    HOT = 0
    VIEWER = 1
    MIRROR = 2
    BACKFILL = 3


@dataclass(frozen=True)
class RateSnapshot:
    """Budget for one GitHub rate-limit resource as reported by a response.

    Attributes:
        resource: Rate-limit resource name, such as "graphql" or "core".
        limit: Total requests or points allowed per window.
        remaining: Requests or points left in the current window.
        reset_at: Epoch seconds when the current window resets.
        observed_at: Epoch seconds when the response was recorded.
    """

    resource: str
    limit: int
    remaining: int
    reset_at: int
    observed_at: float

    def fraction_remaining(self) -> float:
        """Returns `remaining / limit`, or 1.0 when the limit is not positive."""
        if self.limit <= 0:
            return 1.0
        return max(0.0, self.remaining / self.limit)

    def expired(self, now: float) -> bool:
        """Returns True once the window this snapshot describes has reset."""
        return now >= self.reset_at


def parse_rate_headers(headers: Mapping[str, str], now: float) -> RateSnapshot | None:
    """Builds a snapshot from `X-RateLimit-*` headers, matching names case-insensitively.

    Args:
        headers: Response headers.
        now: Epoch seconds to store as `observed_at`.

    Returns:
        The parsed snapshot, or None when any of the resource, limit, remaining,
        or reset headers is missing or malformed.
    """
    lowered = {str(k).strip().lower(): str(v).strip() for k, v in headers.items()}
    resource = lowered.get(f"{_HEADER_PREFIX}resource", "")
    try:
        limit = int(lowered[f"{_HEADER_PREFIX}limit"])
        remaining = int(lowered[f"{_HEADER_PREFIX}remaining"])
        reset_at = int(lowered[f"{_HEADER_PREFIX}reset"])
    except (KeyError, ValueError):
        return None
    if not resource:
        return None
    return RateSnapshot(
        resource=resource,
        limit=limit,
        remaining=remaining,
        reset_at=reset_at,
        observed_at=now,
    )


def _supersedes(new: RateSnapshot, old: RateSnapshot | None) -> bool:
    """Returns True when `new` should replace `old` for the same resource.

    A later window wins. Within one window the budget only shrinks, so a
    strictly lower `remaining` (or a changed `limit`) wins; equal snapshots
    return False to avoid disk writes on unchanged 304 responses.
    """
    if old is None or new.reset_at > old.reset_at:
        return True
    if new.reset_at < old.reset_at:
        return False
    return new.remaining < old.remaining or (
        new.remaining == old.remaining and new.limit != old.limit
    )


def _snapshot_from_json(data: Any) -> RateSnapshot | None:
    """Validates one decoded JSON object into a RateSnapshot."""
    if not isinstance(data, dict):
        return None
    try:
        return RateSnapshot(
            resource=str(data["resource"]),
            limit=int(data["limit"]),
            remaining=int(data["remaining"]),
            reset_at=int(data["reset_at"]),
            observed_at=float(data["observed_at"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


class RateBudget:
    """File-backed, process-shared record of the latest GitHub rate snapshots.

    The budget trusts response headers only. A resource with no snapshot, or
    whose snapshot is past its reset time, counts as unknown, and unknown
    budgets allow every request.
    """

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        """Initializes the budget.

        Args:
            path: Location of `rate_budget.json`. The lock file
                `rate_budget.lock` lives next to it.
            clock: Returns the current time in epoch seconds.
        """
        self.path = Path(path)
        self.lock_path = self.path.parent / RATE_BUDGET_LOCK_FILENAME
        self._clock = clock

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Holds an exclusive `flock` on the lock file for a read-modify-write."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read_all(self) -> dict[str, RateSnapshot]:
        """Reads every stored snapshot, ignoring a missing or corrupt file."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        resources = data.get("resources") if isinstance(data, dict) else None
        if not isinstance(resources, dict):
            return {}
        out: dict[str, RateSnapshot] = {}
        for name, raw in resources.items():
            snap = _snapshot_from_json(raw)
            if snap is not None and snap.resource == name:
                out[name] = snap
        return out

    def _write_all(self, snapshots: Mapping[str, RateSnapshot]) -> None:
        """Atomically replaces the budget file with `snapshots`."""
        payload = {
            "resources": {name: asdict(snap) for name, snap in snapshots.items()}
        }
        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=self.path.parent, prefix=".rate_budget.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            raise

    def record(
        self,
        headers: Mapping[str, str],
        worker: str = "",
        now: float | None = None,
    ) -> RateSnapshot | None:
        """Records the budget reported by one response's headers.

        Args:
            headers: Response headers; names are matched case-insensitively.
            worker: Name of the caller, for diagnostics.
            now: Epoch seconds to record as `observed_at`; defaults to the clock.

        Returns:
            The parsed snapshot, or None when the response carried no complete
            `X-RateLimit-*` header set (nothing is written in that case).
        """
        del worker  # Reserved for the api_calls audit log added with gh_transport.
        moment = self._clock() if now is None else now
        snap = parse_rate_headers(headers, moment)
        if snap is None:
            return None
        with self._locked():
            snapshots = self._read_all()
            if _supersedes(snap, snapshots.get(snap.resource)):
                snapshots[snap.resource] = snap
                self._write_all(snapshots)
        return snap

    def current(self, resource: str, now: float | None = None) -> RateSnapshot | None:
        """Returns the stored snapshot for `resource`, or None if unknown or reset.

        Args:
            resource: Rate-limit resource name, such as "graphql".
            now: Epoch seconds to compare against `reset_at`; defaults to now.
        """
        snap = self._read_all().get(resource)
        moment = self._clock() if now is None else now
        if snap is None or snap.expired(moment):
            return None
        return snap

    def last_snapshots(self) -> dict[str, RateSnapshot]:
        """Returns every stored snapshot, including ones past their reset time."""
        return self._read_all()

    def allows(
        self, resource: str, priority: Priority, now: float | None = None
    ) -> bool:
        """Decides whether a request at `priority` may spend `resource` budget now.

        Args:
            resource: Rate-limit resource name, such as "graphql".
            priority: Priority of the work that wants to make the request.
            now: Epoch seconds used for expiry checks; defaults to now.

        Returns:
            False for `MIRROR` and `BACKFILL` below the 20% reserve and for
            everything except `HOT` below the 5% floor; True otherwise,
            including when the budget is unknown.
        """
        if priority == Priority.HOT:
            return True
        moment = self._clock() if now is None else now
        snap = self.current(resource, moment)
        if snap is None:
            return True
        fraction = snap.fraction_remaining()
        if fraction < FLOOR_FRACTION:
            return False
        if fraction < RESERVE_FRACTION:
            return priority < Priority.MIRROR
        return True
