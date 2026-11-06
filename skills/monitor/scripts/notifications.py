"""Polls GitHub REST notifications with If-Modified-Since for free 304 checks."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from gh_client import GhError, GhRunner
from models import pr_key

DEFAULT_POLL_INTERVAL_S = 60
_SUBJECT_URL_RE = re.compile(r"/repos/([^/]+/[^/]+)/(?:pulls|issues)/(\d+)$")
_STATUS_LINE_RE = re.compile(r"^HTTP/[\d.]+\s+(\d{3})")


class NotificationsUnavailable(GhError):
    """Raised when the ``/notifications`` endpoint rejects the token or is disabled."""


@dataclass
class PollResult:
    """Outcome of a single ``GET /notifications`` poll."""

    not_modified: bool
    last_modified: str | None = None
    poll_interval_s: int = DEFAULT_POLL_INTERVAL_S
    changed_keys: set[str] = field(default_factory=set)
    review_requested_unwatched: bool = False


def subject_to_pr_key(url: str | None) -> str | None:
    """Maps a GitHub notification subject API URL to ``owner/repo#number``."""
    if not url:
        return None
    match = _SUBJECT_URL_RE.search(url)
    if not match:
        return None
    return pr_key(match.group(1), int(match.group(2)))


def parse_http_response(text: str) -> tuple[int, dict[str, str], str]:
    """Parses ``gh api -i`` output into ``(status_code, lowercase_headers, body)``."""
    normalized = text.replace("\r\n", "\n")
    split_idx = normalized.find("\n\n")
    head = normalized if split_idx == -1 else normalized[:split_idx]
    body = "" if split_idx == -1 else normalized[split_idx + 2 :]
    lines = head.split("\n")
    status_line = lines[0] if lines else ""
    match = _STATUS_LINE_RE.match(status_line)
    status = int(match.group(1)) if match else 0
    headers: dict[str, str] = {}
    for line in lines[1:]:
        colon = line.find(":")
        if colon > 0:
            headers[line[:colon].strip().lower()] = line[colon + 1 :].strip()
    return status, headers, body


def _parse_poll_interval(headers: dict[str, str]) -> int:
    raw = headers.get("x-poll-interval")
    if raw and raw.isdigit():
        return max(DEFAULT_POLL_INTERVAL_S, int(raw))
    return DEFAULT_POLL_INTERVAL_S


def _classify_item(item: Any, watched: set[str]) -> tuple[str | None, bool]:
    if not isinstance(item, dict):
        return None, False
    subj: dict[str, Any] = item.get("subject") if isinstance(item.get("subject"), dict) else {}
    key = subject_to_pr_key(subj.get("url"))
    if not key:
        return None, False
    if key in watched:
        return key, False
    return None, item.get("reason") == "review_requested"


def _extract_changed_keys(body: str, watched: set[str]) -> set[str]:
    NotificationPoller.review_requested_unwatched = False
    try:
        items = json.loads(body) if body.strip() else []
    except json.JSONDecodeError as exc:
        raise GhError(f"Invalid JSON from /notifications: {exc}", code=1) from exc
    if not isinstance(items, list):
        return set()
    changed: set[str] = set()
    for item in items:
        matched_key, unwatched_req = _classify_item(item, watched)
        if matched_key:
            changed.add(matched_key)
        elif unwatched_req:
            NotificationPoller.review_requested_unwatched = True
    return changed


def poll(runner: GhRunner, since: str | None, watched: set[str]) -> PollResult:
    """Polls ``GET /notifications`` with ``If-Modified-Since``."""
    NotificationPoller.review_requested_unwatched = False
    args = ["api", "-i", "notifications?per_page=50&all=true"]
    if since:
        args.extend(["-H", f"If-Modified-Since: {since}"])
    code, stdout, stderr = runner(args)
    status, headers, body = parse_http_response(stdout)
    interval = _parse_poll_interval(headers)
    last_mod = headers.get("last-modified") or since

    if status == 304:
        return PollResult(
            not_modified=True,
            last_modified=last_mod,
            poll_interval_s=interval,
            changed_keys=set(),
            review_requested_unwatched=False,
        )
    if status in (401, 403):
        raise NotificationsUnavailable(
            f"/notifications returned HTTP {status}: {stderr.strip() or body.strip()}",
            code=code or 1,
            stderr=stderr,
            status=status,
            headers=headers,
        )
    if status != 200 or code != 0:
        raise GhError(
            f"/notifications failed (HTTP {status}, exit {code}): {stderr.strip()}",
            code=code or 1,
            stderr=stderr,
            status=status or None,
            headers=headers,
        )
    changed = _extract_changed_keys(body, watched)
    return PollResult(
        not_modified=False,
        last_modified=last_mod,
        poll_interval_s=interval,
        changed_keys=changed,
        review_requested_unwatched=NotificationPoller.review_requested_unwatched,
    )


_UNSET = object()


def _coerce_watched(watched: set[str] | None, fallback: Any) -> set[str]:
    if watched is not None:
        return watched
    return fallback if isinstance(fallback, set) else set()


def _coerce_since(first: Any, second: Any, default_since: str | None) -> str | None:
    if isinstance(first, str):
        return first
    if isinstance(second, str):
        return second
    if first is None and second is not _UNSET:
        return None
    if second is None:
        return None
    return default_since


class NotificationPoller:
    """Polls GitHub REST notifications and tracks unwatched review requests."""

    review_requested_unwatched: bool = False

    def __init__(self, runner: GhRunner | None = None) -> None:
        self.runner = runner
        self.last_modified: str | None = None
        self.poll_interval_s: int = DEFAULT_POLL_INTERVAL_S
        self.review_requested_unwatched: bool = False

    def _extract_changed_keys(self, body: str, watched: set[str]) -> set[str]:
        self.review_requested_unwatched = False
        changed = _extract_changed_keys(body, watched)
        self.review_requested_unwatched = NotificationPoller.review_requested_unwatched
        return changed

    def _resolve_poll_args(
        self,
        runner: Any,
        since: Any,
        watched: set[str] | None,
    ) -> tuple[GhRunner, str | None, set[str]]:
        if callable(runner):
            actual_since = since if isinstance(since, str) else None
            return runner, actual_since, _coerce_watched(watched, since)
        if self.runner is None:
            raise TypeError("NotificationPoller.poll requires a GhRunner")
        if isinstance(runner, set):
            return self.runner, self.last_modified, runner
        actual_since = _coerce_since(runner, since, self.last_modified)
        return self.runner, actual_since, _coerce_watched(watched, since)

    def poll(
        self,
        runner: Any = _UNSET,
        since: Any = _UNSET,
        watched: set[str] | None = None,
    ) -> PollResult:
        """Polls ``GET /notifications`` and updates ``review_requested_unwatched``."""
        self.review_requested_unwatched = False
        NotificationPoller.review_requested_unwatched = False
        first = None if runner is _UNSET else runner
        actual_runner, actual_since, actual_watched = self._resolve_poll_args(first, since, watched)
        result = poll(actual_runner, since=actual_since, watched=actual_watched)
        self.last_modified = result.last_modified
        self.poll_interval_s = result.poll_interval_s
        self.review_requested_unwatched = result.review_requested_unwatched
        return result
