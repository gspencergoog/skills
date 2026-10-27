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


def _extract_changed_keys(body: str, watched: set[str]) -> set[str]:
    try:
        items = json.loads(body) if body.strip() else []
    except json.JSONDecodeError as exc:
        raise GhError(f"Invalid JSON from /notifications: {exc}", code=1) from exc
    if not isinstance(items, list):
        return set()
    changed: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        subj: dict[str, Any] = item.get("subject") if isinstance(item.get("subject"), dict) else {}
        key = subject_to_pr_key(subj.get("url"))
        if key and key in watched:
            changed.add(key)
    return changed


def poll(runner: GhRunner, since: str | None, watched: set[str]) -> PollResult:
    """Polls ``GET /notifications`` with ``If-Modified-Since``."""
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
    return PollResult(
        not_modified=False,
        last_modified=last_mod,
        poll_interval_s=interval,
        changed_keys=_extract_changed_keys(body, watched),
    )
