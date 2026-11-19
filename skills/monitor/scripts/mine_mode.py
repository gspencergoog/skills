"""Decision logic for ``/monitor --mine``: fresh-fetch gates, row lifecycle, removals.

``--mine`` follows the user's open pull requests from ``packrat`` alone. ``packrat``
writes a viewer-set change before it refetches the affected pull requests, and
after an outage its rows predate the outage, so a stored row can say ``OPEN`` for
a pull request that has merged. The types here decide when a stored row can be
trusted and when a board row is added, kept, or retired.

Nothing in this module does I/O. ``monitor_prs.py`` reads ``packrat``, passes in
what it read, and acts on the answers.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from models import parse_iso_seconds

GATE_LAPSE_S = 600
"""A gate that no newer row has cleared lapses after this many seconds."""
GATE_SLACK_S = 1.0
"""Allowance for ``packrat`` storing fetch times in whole seconds."""
GATED_WAIT_S = 5.0
"""Longest wait between reads of gated keys while any gate holds."""
RECENT_RETIRED_MAX = 20
"""Most entries kept in the recent removals list."""
RECENT_RETIRED_TTL_S = 24 * 60 * 60
"""A recent removal older than this is dropped."""
RETIRED_REMOVED = "REMOVED"
"""The ``state`` of a recent removal whose pull request left the set while open."""

_TERMINAL_STATES = frozenset({"MERGED", "CLOSED"})


class SourceState(StrEnum):
    """How far ``--mine`` trusts ``packrat`` right now."""

    LIVE = "live"
    STALE = "stale"
    CATCHING_UP = "catching_up"


class GateReason(StrEnum):
    """Why a stored row stopped being trusted."""

    SET_EXIT = "set_exit"
    RECOVERY = "recovery"
    STARTUP = "startup"


class Resolution(StrEnum):
    """What ``MineRows.resolve()`` decides for one board row."""

    KEEP = "keep"
    RETIRE_TERMINAL = "retire_terminal"
    RETIRE_LEFT_SET = "retire_left_set"
    RETIRE_UNCONFIRMED = "retire_unconfirmed"


REMOVAL_REASONS: Mapping[Resolution, str] = {
    Resolution.RETIRE_LEFT_SET: "left_set",
    Resolution.RETIRE_UNCONFIRMED: "left_set_unconfirmed",
}
"""The ``pr_removed`` event reason for each resolution that retires an open row."""


@dataclass
class GateBook:
    """Per-key gate times: a stored row is trusted only when fetched after its gate.

    Times are readings of one monotonic clock. A row's age comes from ``packrat``
    (``age_s``, seconds since the row was fetched), so no wall-clock time is
    compared with a monotonic one.
    """

    _gates: dict[str, tuple[float, GateReason]] = field(default_factory=dict)

    def set(self, key: str, at: float, reason: GateReason) -> None:
        """Gates ``key`` at time ``at``, replacing any gate it already holds."""
        self._gates[key] = (at, reason)

    def holds(self, key: str) -> bool:
        """Whether ``key`` has a gate."""
        return key in self._gates

    def reason(self, key: str) -> GateReason | None:
        """Returns why ``key`` is gated, or None when it has no gate."""
        gate = self._gates.get(key)
        return gate[1] if gate is not None else None

    def keys(self) -> list[str]:
        """Returns every gated key, in the order the gates were first set."""
        return list(self._gates)

    def holds_reason(self, reason: GateReason) -> bool:
        """Whether any key is gated for ``reason``."""
        return any(held == reason for _, held in self._gates.values())

    def accepts(self, key: str, age_s: float | None, now: float) -> bool:
        """Whether a row of ``key`` fetched ``age_s`` seconds ago is trusted at ``now``.

        True when no gate holds, or ``age_s <= now - gate_at + GATE_SLACK_S``. A
        gated key whose row has no known age is not accepted.
        """
        gate = self._gates.get(key)
        if gate is None:
            return True
        if age_s is None:
            return False
        return age_s <= now - gate[0] + GATE_SLACK_S

    def clear(self, key: str) -> None:
        """Removes the gate on ``key``, if any."""
        self._gates.pop(key, None)

    def lapsed(self, now: float) -> list[tuple[str, GateReason]]:
        """Returns the gates that have held for ``GATE_LAPSE_S`` seconds or more.

        The gates stay in place. The caller clears each one once it has acted.
        """
        return [
            (key, reason)
            for key, (at, reason) in self._gates.items()
            if now - at >= GATE_LAPSE_S
        ]


@dataclass(frozen=True)
class Discovery:
    """What one read of the ``authored_open`` set changes.

    Attributes:
        watched: The keys to watch from now on: the newest set members that fit
            under the cap, then the rows that are waiting to leave.
        lease: Keys that are new to the watch list. They have no board row yet.
        gate: Shown keys that left the set while their row was open. Each needs
            a gate at the discovery time and a refresh request.
        drop: Keys to stop watching with no event: pending keys that left the
            set, and keys pushed past the cap.
        regained: Keys that were waiting to leave and are in the set again.
    """

    watched: list[str]
    lease: list[str]
    gate: list[str]
    drop: list[str]
    regained: list[str]


@dataclass
class MineRows:
    """The lifecycle of each watched key under ``--mine``.

    A key is in one of three states. ``Pending`` keys are in the set and leased,
    with no board row until ``packrat`` has an open row for them. ``Shown`` keys
    have a board row. ``PendingExit`` keys have a board row and have left the set
    while that row was open, so they wait for a fresh row to say why.

    Attributes:
        cap: The most keys watched at once.
        once: Whether this is a ``--once`` run, which never retires an exit that
            no fresh row has confirmed.
        pending: Keys in the ``Pending`` state.
        pending_exit: Keys in the ``PendingExit`` state, with the time each exit
            was seen.
        total_open: The size of the set at the last discovery, before the cap.
    """

    cap: int = 60
    once: bool = False
    pending: set[str] = field(default_factory=set)
    pending_exit: dict[str, float] = field(default_factory=dict)
    total_open: int = 0
    _quiet: set[str] = field(default_factory=set)

    def apply_discovery(
        self, set_keys: Sequence[str], shown_keys: Iterable[str], now: float
    ) -> Discovery:
        """Updates the row states from a new read of the set.

        ``set_keys`` is every key in the set, most recently updated first.
        ``shown_keys`` is every key that has a board row. ``now`` is the time of the
        read, which becomes the exit time of each key that left the set.

        Rows that are waiting to leave keep their slot under the cap, and the
        newest set members fill the rest. A key pushed past the cap is dropped
        with no event, and shows again with no event if it returns.
        """
        in_set = set(set_keys)
        shown = list(dict.fromkeys(shown_keys))
        self.total_open = len(in_set)
        regained = [key for key in self.pending_exit if key in in_set]
        for key in regained:
            del self.pending_exit[key]
        gate = [
            key for key in shown if key not in in_set and key not in self.pending_exit
        ]
        for key in gate:
            self.pending_exit[key] = now
        exits = [key for key in shown if key in self.pending_exit]
        self.pending_exit = {key: self.pending_exit[key] for key in exits}

        kept = list(dict.fromkeys(set_keys))[: max(0, self.cap - len(exits))]
        kept_set = set(kept)
        tracked = set(shown) | self.pending
        drop = sorted(tracked - kept_set - set(exits))
        lease = [key for key in kept if key not in tracked]
        self._quiet = (self._quiet | (set(drop) & set(shown))) & in_set
        self.pending = (self.pending & kept_set) | set(lease)
        return Discovery(
            watched=[*kept, *exits],
            lease=lease,
            gate=gate,
            drop=drop,
            regained=regained,
        )

    def mark_shown(self, key: str) -> bool:
        """Moves a pending ``key`` to ``Shown`` once its first open row has arrived.

        Returns whether the row is new to the board and should be announced with
        a ``pr_added`` event. That is false for a key that already had a row, and
        for a key that the cap displaced and that is now back.
        """
        if key not in self.pending:
            return False
        self.pending.discard(key)
        if key in self._quiet:
            self._quiet.discard(key)
            return False
        return True

    def resolve(
        self, key: str, state: str | None, in_set: bool, gate_lapsed: bool
    ) -> Resolution:
        """Decides whether the board row of ``key`` stays or is retired.

        ``state`` is the state of a row that passed the key's gate on this read,
        or None when no such row arrived. ``in_set`` is whether the key was in the
        set at the last discovery. ``gate_lapsed`` is whether the key's gate has
        held for ``GATE_LAPSE_S`` seconds.

        A retired key is forgotten, so a pull request that is reopened starts
        again as a new key.
        """
        resolution = self._decide(key, state, in_set, gate_lapsed)
        if resolution != Resolution.KEEP:
            self.forget(key)
        return resolution

    def _decide(
        self, key: str, state: str | None, in_set: bool, gate_lapsed: bool
    ) -> Resolution:
        if state in _TERMINAL_STATES:
            return Resolution.RETIRE_TERMINAL
        if key not in self.pending_exit:
            return Resolution.KEEP
        if in_set:
            del self.pending_exit[key]
            return Resolution.KEEP
        if state is not None:
            return Resolution.RETIRE_LEFT_SET
        if gate_lapsed and not self.once:
            return Resolution.RETIRE_UNCONFIRMED
        return Resolution.KEEP

    def forget(self, key: str) -> None:
        """Drops every record of ``key``."""
        self.pending.discard(key)
        self.pending_exit.pop(key, None)
        self._quiet.discard(key)


class RecentRetired:
    """The rows that recently left the board, oldest first.

    Holds at most ``RECENT_RETIRED_MAX`` entries, none older than
    ``RECENT_RETIRED_TTL_S`` seconds. Each entry is a ``{"pr", "title", "state",
    "at"}`` dictionary, the shape stored in ``state.json``. ``state`` is ``MERGED`` or
    ``CLOSED`` for a pull request that finished, and ``RETIRED_REMOVED`` for one
    that left the set while open.
    """

    def __init__(self, entries: Iterable[Mapping[str, Any]] = ()) -> None:
        self._entries: list[dict[str, Any]] = [
            dict(entry) for entry in entries if isinstance(entry, Mapping)
        ]

    def remove(self, pr: str) -> None:
        """Drops any recorded removal entry for ``pr``."""
        self._entries = [entry for entry in self._entries if entry.get("pr") != pr]

    def add(self, pr: str, title: str, state: str, at: str) -> None:
        """Records that ``pr`` left the board at ISO-8601 time ``at``.

        An earlier entry for the same pull request is replaced.
        """
        self.remove(pr)
        self._entries.append({"pr": pr, "title": title, "state": state, "at": at})
        del self._entries[:-RECENT_RETIRED_MAX]

    def prune(self, now_epoch: float) -> None:
        """Drops entries older than the limit, or whose time cannot be read."""
        cutoff = now_epoch - RECENT_RETIRED_TTL_S
        kept: list[dict[str, Any]] = []
        for entry in self._entries:
            at = parse_iso_seconds(entry.get("at"))
            if at is not None and at >= cutoff:
                kept.append(entry)
        self._entries = kept[-RECENT_RETIRED_MAX:]

    def to_list(self) -> list[dict[str, Any]]:
        """Returns a copy of the entries, oldest first."""
        return [dict(entry) for entry in self._entries]
