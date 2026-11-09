"""Persistent cross-session reviewer overrides (mutes, pins, and triage watermarks)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from models import MuteEntry, PrSnapshot, TriageEntry, TriageVerdict, parse_pr_ref, pr_key

OVERRIDES_SCHEMA_VERSION = 1
DEFAULT_OVERRIDES_REL = Path("monitor") / "review_overrides.json"


def utc_now_iso() -> str:
    """Formats the current UTC wall-clock time as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_override_key(raw_key: str, default_repo: str | None = None) -> str:
    """Normalizes a pull request reference to lowercase ``owner/repo#number``."""
    repo, number = parse_pr_ref(raw_key, default_repo=default_repo)
    return pr_key(repo.lower(), number)


def default_overrides_path(env: Mapping[str, str] | None = None) -> Path:
    """Resolves ``$XDG_CONFIG_HOME/monitor/review_overrides.json`` (or ``~/.config/...``)."""
    source_env = os.environ if env is None else env
    xdg = (source_env.get("XDG_CONFIG_HOME") or "").strip()
    base = Path(xdg) if xdg else (Path.home() / ".config")
    return base / DEFAULT_OVERRIDES_REL


@dataclass
class ReviewOverrides:
    """Cross-session mute, dormant-pin, and discussion-triage state for ``--reviews``."""

    schema: int = OVERRIDES_SCHEMA_VERSION
    mutes: dict[str, MuteEntry] = field(default_factory=dict)
    pins: dict[str, str] = field(default_factory=dict)
    triage: dict[str, TriageEntry] = field(default_factory=dict)

    def get_mute(self, key: str) -> MuteEntry | None:
        """Looks up a ``MuteEntry`` case-insensitively by PR key."""
        return self.mutes.get(key.lower())

    def get_pin(self, key: str) -> str | None:
        """Looks up an ``Unhide`` pin timestamp case-insensitively by PR key."""
        return self.pins.get(key.lower())

    def get_triage(self, key: str) -> TriageEntry | None:
        """Looks up a cached ``TriageEntry`` case-insensitively by PR key."""
        return self.triage.get(key.lower())

    def to_dict(self) -> dict[str, Any]:
        """Serializes overrides into a JSON-compatible dictionary."""
        return {
            "schema": self.schema,
            "mutes": {k: v.to_dict() for k, v in sorted(self.mutes.items())},
            "pins": dict(sorted(self.pins.items())),
            "triage": {k: v.to_dict() for k, v in sorted(self.triage.items())},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReviewOverrides:
        """Deserializes ``ReviewOverrides`` from JSON, normalizing keys to lowercase."""
        schema = int(data.get("schema", OVERRIDES_SCHEMA_VERSION))
        mutes = {
            str(k).lower(): MuteEntry.from_dict(v)
            for k, v in (data.get("mutes") or {}).items()
            if isinstance(v, dict)
        }
        pins = {
            str(k).lower(): str(v)
            for k, v in (data.get("pins") or {}).items()
            if isinstance(v, str) and v
        }
        triage = {
            str(k).lower(): TriageEntry.from_dict(v)
            for k, v in (data.get("triage") or {}).items()
            if isinstance(v, dict)
        }
        return cls(schema=schema, mutes=mutes, pins=pins, triage=triage)


def load_overrides(path: Path) -> ReviewOverrides:
    """Loads ``ReviewOverrides`` from ``path``, returning empty overrides if missing or corrupt."""
    if not path.exists():
        return ReviewOverrides()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise TypeError("top-level JSON value must be an object")
        return ReviewOverrides.from_dict(raw)
    except (OSError, ValueError, TypeError) as exc:
        print(
            f"monitor: ignoring unreadable review overrides at {path}: {exc}",
            file=sys.stderr,
        )
        return ReviewOverrides()


def save_overrides(path: Path, overrides: ReviewOverrides) -> None:
    """Atomically writes ``overrides`` to ``path`` with ``0o600`` permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(overrides.to_dict(), indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        os.chmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def mute_pr(
    overrides: ReviewOverrides,
    key: str,
    snap: PrSnapshot | None,
    viewer: str,
    *,
    now_iso: str | None = None,
    reason: str = "",
) -> MuteEntry:
    """Records a ``MuteEntry`` for ``key`` using ``snap`` metadata when available."""
    norm_key = normalize_override_key(key)
    viewer_norm = viewer.strip().lower()
    was_requested = bool(
        snap is not None
        and viewer_norm
        and any(r.lower() == viewer_norm for r in snap.requested_reviewers)
    )
    entry = MuteEntry(
        muted_at=now_iso or utc_now_iso(),
        head_sha=snap.head_sha if snap is not None else "",
        updated_at=snap.updated_at if snap is not None else "",
        was_requested=was_requested,
        reason=reason,
    )
    overrides.mutes[norm_key] = entry
    overrides.pins.pop(norm_key, None)
    return entry


def unmute_pr(
    overrides: ReviewOverrides,
    key: str,
    *,
    now_iso: str | None = None,
) -> None:
    """Clears any active mute on ``key`` and pins it active for a fresh dormancy window."""
    norm_key = normalize_override_key(key)
    overrides.mutes.pop(norm_key, None)
    overrides.pins[norm_key] = now_iso or utc_now_iso()


def record_triage_entry(
    overrides: ReviewOverrides,
    key: str,
    *,
    watermark: str,
    verdict: TriageVerdict | str,
    summary: str = "",
    evaluated_at: str | None = None,
) -> TriageEntry:
    """Stores a watermarked discussion ``TriageEntry`` for ``key``."""
    norm_key = normalize_override_key(key)
    entry = TriageEntry(
        watermark=watermark,
        verdict=TriageVerdict(verdict),
        summary=summary.strip(),
        evaluated_at=evaluated_at or utc_now_iso(),
    )
    overrides.triage[norm_key] = entry
    return entry


def _file_signature(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


class OverridesWatcher:
    """Reloads ``review_overrides.json`` only when ``(st_mtime_ns, st_size)`` changes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._sig: tuple[int, int] | None = _file_signature(path)
        self._cached: ReviewOverrides = load_overrides(path)

    def changed(self) -> bool:
        """Returns whether the underlying overrides file changed since the last ``current()``."""
        return _file_signature(self.path) != self._sig

    def current(self) -> ReviewOverrides:
        """Returns up-to-date ``ReviewOverrides``, reloading from disk only on file change."""
        sig = _file_signature(self.path)
        if sig != self._sig:
            self._sig = sig
            self._cached = load_overrides(self.path)
        return self._cached
