"""Signal poller: cheap GitHub change signals that tell the service what to refresh.

`SignalPoller` asks GitHub "did anything change?" as cheaply as possible and
returns a `DirtySet` naming the work the service should schedule:

- `poll_notifications` reads `/notifications` with `If-Modified-Since`.
- `poll_repo_events` reads `/repos/{o}/{r}/events` with ETags for `mirror` repos.
- `poll_check_runs` reads a leased PR head's `check-runs` and `status` with ETags.

Conditional requests that answer 304 cost no rate budget. Notification and
check-run requests run at `Priority.HOT`; events run at `Priority.MIRROR`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any

import github_sync
from db_store import PackratStore
from gh_transport import BudgetDeferred, GhApiError, GhResponse, GhRunner, GhTransport
from models import (
    NotificationRecord,
    RepoConfig,
    WatchLease,
    format_item_key,
    parse_item_key,
)
from rate_budget import Priority

WORKER = "signals"
"""Worker name recorded in `api_calls` for every signal request."""

NOTIFICATIONS_CURSOR_KEY = "notifications_last_modified"
"""`service_state` key holding the `Last-Modified` of the newest notifications response."""

NOTIFICATIONS_PATH = "notifications?per_page=50&all=true"
MAX_NOTIFICATION_PAGES = 10
"""Upper bound on `Link: rel="next"` pages followed in one notifications poll."""

DEFAULT_POLL_INTERVAL_S = 60
DEFAULT_CHECK_FRESHNESS_S = 120

EVENTS_STATE_KEY_PREFIX = "signals_events:"
"""`service_state` key prefix (plus repo_id) for the signal poller's own events ETag.

The poller keeps its own ETag so it never consumes the one that
`github_sync.sync_github_metadata()` uses to decide whether to walk a repo.
"""

_API_URL_PREFIX = "https://api.github.com/"
_NEXT_LINK_RE = re.compile(r'<([^>]+)>\s*;\s*rel="next"')
_SUBJECT_NUMBER_RE = re.compile(r"/(?:pulls|issues)/(\d+)$")


@dataclass
class DirtySet:
    """Work the service should schedule after a round of signals.

    Attributes:
        hot: Item keys (`owner/name#number`) that need a hot refresh.
        viewer: Whether the viewer's search sets may have changed.
        mirror_repos: repo_ids whose `/events` feed changed.
    """

    hot: set[str] = field(default_factory=set)
    viewer: bool = False
    mirror_repos: set[int] = field(default_factory=set)

    def merge(self, other: DirtySet) -> DirtySet:
        """Adds everything in `other` to this set in place and returns self."""
        self.hot |= other.hot
        self.viewer = self.viewer or other.viewer
        self.mirror_repos |= other.mirror_repos
        return self

    def is_empty(self) -> bool:
        """Returns True when no work is pending."""
        return not self.hot and not self.viewer and not self.mirror_repos


def _poll_interval(
    headers: Mapping[str, str], default: int = DEFAULT_POLL_INTERVAL_S
) -> int:
    """Returns `X-Poll-Interval` in seconds, or `default` when missing or malformed."""
    try:
        return max(1, int(headers.get("x-poll-interval", default)))
    except ValueError:
        return default


def _http_date_to_iso(value: str | None) -> str | None:
    """Converts an HTTP date such as `Last-Modified` to ISO-8601 UTC, or None."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _next_page_path(headers: Mapping[str, str]) -> str | None:
    """Returns the API path of the `Link: rel="next"` page, or None."""
    match = _NEXT_LINK_RE.search(headers.get("link", ""))
    if match is None:
        return None
    url = match.group(1)
    return url.removeprefix(_API_URL_PREFIX)


def _decode_list(body: str) -> list[dict[str, Any]]:
    """Decodes a JSON array of objects, dropping anything else."""
    try:
        parsed = json.loads(body) if body.strip() else []
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _reaches_cursor(
    entries: Iterable[Mapping[str, Any]], cursor_iso: str | None
) -> bool:
    """Returns True when a page holds an entry no newer than the stored cursor."""
    if cursor_iso is None:
        return True
    return any(str(e.get("updated_at") or "") < cursor_iso for e in entries)


def _subject_repo(entry: Mapping[str, Any]) -> tuple[str, str] | None:
    """Returns `(owner, name)` of a notification's repository, or None."""
    repo = entry.get("repository")
    full_name = repo.get("full_name") if isinstance(repo, dict) else None
    if not isinstance(full_name, str) or full_name.count("/") != 1:
        return None
    owner, name = full_name.split("/")
    return owner, name


def _subject_number(entry: Mapping[str, Any]) -> int | None:
    """Returns the issue or PR number from a notification's `subject.url`, or None."""
    subject = entry.get("subject")
    url = subject.get("url") if isinstance(subject, dict) else None
    match = _SUBJECT_NUMBER_RE.search(url) if isinstance(url, str) else None
    return int(match.group(1)) if match else None


def _subject_type(entry: Mapping[str, Any]) -> str | None:
    """Returns a notification's `subject.type`, such as "PullRequest"."""
    subject = entry.get("subject")
    value = subject.get("type") if isinstance(subject, dict) else None
    return str(value) if value else None


def _event_numbers(events: Iterable[Mapping[str, Any]]) -> set[int]:
    """Returns the issue and PR numbers referenced by repository event payloads."""
    numbers: set[int] = set()
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        for key in ("pull_request", "issue"):
            obj = payload.get(key)
            if isinstance(obj, dict) and isinstance(obj.get("number"), int):
                numbers.add(obj["number"])
        if isinstance(payload.get("number"), int):
            numbers.add(payload["number"])
    return numbers


def _lease_freshness(leases: Iterable[WatchLease]) -> dict[str, int]:
    """Returns the smallest positive `freshness_s` per leased item key."""
    out: dict[str, int] = {}
    for lease in leases:
        fresh = (
            lease.freshness_s if lease.freshness_s > 0 else DEFAULT_CHECK_FRESHNESS_S
        )
        out[lease.item_key] = min(fresh, out.get(lease.item_key, fresh))
    return out


def _response_changed(resp: GhResponse) -> bool:
    """Returns True for a 2xx response, which means the resource changed."""
    return 200 <= resp.status < 300


class SignalPoller:
    """Polls GitHub change signals and reports what needs refreshing.

    The poller is long-lived: it remembers `X-Poll-Interval` due times and the
    last check-run poll per item in memory. Cursors that must survive a
    restart (the notifications `Last-Modified` and the events ETags) live in
    `service_state`.

    When a request is deferred or rate limited mid-poll, the method re-raises
    `BudgetDeferred` and keeps the work it had already found; the next
    successful poll returns it.
    """

    def __init__(self, transport: GhTransport, store: PackratStore) -> None:
        """Initializes the poller.

        Args:
            transport: Budgeted GitHub transport.
            store: Packrat store for cursors, notifications, leases, and ETags.
        """
        self._transport = transport
        self._store = store
        self.notifications_poll_interval_s = DEFAULT_POLL_INTERVAL_S
        self._notifications_due_at = 0.0
        self._events_due_at: dict[int, float] = {}
        self._checks_polled_at: dict[str, float] = {}
        self._headless_marked: set[str] = set()
        self._carry = DirtySet()

    @property
    def poll_interval_s(self) -> int:
        """Seconds the service should wait between signal rounds.

        This is the last notifications `X-Poll-Interval` (default 60).
        """
        return self.notifications_poll_interval_s

    @contextlib.contextmanager
    def _collect(self) -> Iterator[DirtySet]:
        """Yields a DirtySet that is preserved across a `BudgetDeferred` raised inside."""
        dirty = DirtySet()
        try:
            yield dirty
        except BudgetDeferred:
            self._carry.merge(dirty)
            raise
        dirty.merge(self._carry)
        self._carry = DirtySet()

    # ----- notifications -------------------------------------------------------------

    def poll_notifications(self, now: float) -> DirtySet:
        """Polls `/notifications` once its `X-Poll-Interval` has elapsed.

        Sends the stored `Last-Modified` as `If-Modified-Since`. On a 200 it
        follows `Link: rel="next"` pages until a page reaches entries older
        than the cursor, upserts the notifications, registers unknown
        repositories in `items` mode, and saves the new cursor.

        Args:
            now: Current epoch seconds.

        Returns:
            Leased items with a new or updated notification in `hot`, and
            `viewer` set when any notification changed.

        Raises:
            BudgetDeferred: The request was deferred or rate limited.
            GhApiError: GitHub answered with another error.
        """
        with self._collect() as dirty:
            if now < self._notifications_due_at:
                return dirty
            self._notifications_due_at = now + self.notifications_poll_interval_s
            cursor = self._store.get_state(NOTIFICATIONS_CURSOR_KEY)
            first = self._transport.rest_get(
                NOTIFICATIONS_PATH,
                worker=WORKER,
                priority=Priority.HOT,
                if_modified_since=cursor,
            )
            self.notifications_poll_interval_s = _poll_interval(first.headers)
            self._notifications_due_at = now + self.notifications_poll_interval_s
            if first.status == 304:
                return dirty
            entries = self._read_notification_pages(first, _http_date_to_iso(cursor))
            changed = self._store_notifications(entries, now)
            new_cursor = first.headers.get("last-modified")
            if new_cursor:
                self._store.set_state(NOTIFICATIONS_CURSOR_KEY, new_cursor, now)
            dirty.viewer = bool(changed)
            dirty.hot |= self._leased(changed, now)
            return dirty

    def _read_notification_pages(
        self, first: GhResponse, cursor_iso: str | None
    ) -> list[dict[str, Any]]:
        """Returns entries from `first` plus `rel="next"` pages until the cursor is reached."""
        entries = _decode_list(first.body)
        resp = first
        pages = 1
        while pages < MAX_NOTIFICATION_PAGES and not _reaches_cursor(
            _decode_list(resp.body), cursor_iso
        ):
            next_path = _next_page_path(resp.headers)
            if next_path is None:
                break
            resp = self._transport.rest_get(
                next_path, worker=WORKER, priority=Priority.HOT
            )
            entries.extend(_decode_list(resp.body))
            pages += 1
        return entries

    def _store_notifications(
        self, entries: Sequence[Mapping[str, Any]], now: float
    ) -> list[str]:
        """Upserts notification entries and returns item keys that are new or updated."""
        records: list[NotificationRecord] = []
        known: dict[tuple[str, str], RepoConfig] = {}
        for entry in entries:
            thread_id = entry.get("id")
            if not thread_id:
                continue
            records.append(
                NotificationRecord(
                    thread_id=str(thread_id),
                    reason=entry.get("reason"),
                    subject_type=_subject_type(entry),
                    item_key=self._entry_item_key(entry, known),
                    updated_at=entry.get("updated_at"),
                    unread=bool(entry.get("unread")),
                )
            )
        if not records:
            return []
        return self._store.upsert_notifications(records, now)

    def _entry_item_key(
        self, entry: Mapping[str, Any], known: dict[tuple[str, str], RepoConfig]
    ) -> str | None:
        """Returns the canonical item key for an issue or PR subject.

        Registers the subject's repository in `items` mode the first time it
        is seen; `known` caches repositories already resolved in this poll.
        """
        repo_name = _subject_repo(entry)
        number = _subject_number(entry)
        if repo_name is None or number is None:
            return None
        repo = known.get(repo_name)
        if repo is None:
            repo = self._store.get_repo(f"{repo_name[0]}/{repo_name[1]}")
            if repo is None:
                repo = self._store.ensure_items_repo(*repo_name)
            known[repo_name] = repo
        return format_item_key(repo.owner, repo.name, number)

    def _leased_map(self, now: float) -> dict[str, str]:
        """Returns active leased item keys, keyed by their lowercase form."""
        return {
            lease.item_key.lower(): lease.item_key
            for lease in self._store.active_watches(now)
        }

    def _leased(self, item_keys: Iterable[str], now: float) -> set[str]:
        """Returns the subset of `item_keys` with an active lease (case-insensitive)."""
        leased = self._leased_map(now)
        return {leased[k.lower()] for k in item_keys if k.lower() in leased}

    # ----- repository events ---------------------------------------------------------

    def poll_repo_events(self, now: float) -> DirtySet:
        """Polls `/events` for every `mirror` repo whose `X-Poll-Interval` has elapsed.

        Calls go through `transport.as_runner("signals", Priority.MIRROR)`, so
        they are budgeted and logged in `api_calls`. A rate-limited or
        deferred poll stops the round; the remaining repos are polled next time.

        Args:
            now: Current epoch seconds.

        Returns:
            Repos whose feed changed in `mirror_repos`, and leased items
            referenced by the new events in `hot`.
        """
        with self._collect() as dirty:
            runner = self._transport.as_runner(WORKER, Priority.MIRROR)
            leased = self._leased_map(now)
            for repo in self._store.list_repos(mode="mirror"):
                if now < self._events_due_at.get(repo.repo_id, 0.0):
                    continue
                if not self._poll_one_repo(repo, runner, leased, now, dirty):
                    break
            return dirty

    def _poll_one_repo(
        self,
        repo: RepoConfig,
        runner: GhRunner,
        leased: Mapping[str, str],
        now: float,
        dirty: DirtySet,
    ) -> bool:
        """Polls one repo's `/events` into `dirty`; returns False when rate limited."""
        poll = github_sync.poll_repo_events(
            self._with_events_state(repo), runner=runner
        )
        self._events_due_at[repo.repo_id] = now + max(1, poll.poll_interval_sec)
        if poll.error:
            return not poll.rate_limited
        self._save_events_state(repo.repo_id, poll, now)
        if poll.not_modified:
            return True
        dirty.mirror_repos.add(repo.repo_id)
        for number in _event_numbers(poll.events):
            key = format_item_key(repo.owner, repo.name, number).lower()
            if key in leased:
                dirty.hot.add(leased[key])
        return True

    def _with_events_state(self, repo: RepoConfig) -> RepoConfig:
        """Returns `repo` carrying the poller's own events ETag and Last-Modified."""
        raw = self._store.get_state(f"{EVENTS_STATE_KEY_PREFIX}{repo.repo_id}")
        try:
            state = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            state = {}
        if not isinstance(state, dict):
            state = {}
        return dataclasses.replace(
            repo,
            events_etag=state.get("etag"),
            events_last_modified=state.get("last_modified"),
        )

    def _save_events_state(
        self, repo_id: int, poll: github_sync.EventsPollResult, now: float
    ) -> None:
        """Stores the poller's events ETag and Last-Modified for `repo_id`."""
        value = json.dumps({"etag": poll.etag, "last_modified": poll.last_modified})
        self._store.set_state(f"{EVENTS_STATE_KEY_PREFIX}{repo_id}", value, now)

    # ----- check runs ----------------------------------------------------------------

    def poll_check_runs(self, leases: Sequence[WatchLease], now: float) -> DirtySet:
        """Polls check runs and commit statuses for leases whose freshness has elapsed.

        Uses the ETags from `store.get_rollup_etags()` when they belong to the
        PR's current head, and saves the new ETags with `set_rollup_etags()`.
        An item whose head SHA is unknown is marked hot once, so the hot
        refresh can learn it.

        Args:
            leases: Active leases; the smallest `freshness_s` per item applies.
            now: Current epoch seconds.

        Returns:
            Items whose check runs or statuses changed, in `hot`.

        Raises:
            BudgetDeferred: A request was deferred or rate limited.
        """
        with self._collect() as dirty:
            for item_key, freshness in sorted(_lease_freshness(leases).items()):
                last = self._checks_polled_at.get(item_key)
                if last is not None and now - last < freshness:
                    continue
                if self._poll_item_checks(item_key, now):
                    dirty.hot.add(item_key)
            return dirty

    def _poll_item_checks(self, item_key: str, now: float) -> bool:
        """Polls one item's check runs and status; returns True when it needs a hot refresh."""
        pr_head = self._store.get_pr_head_sha(item_key)
        rollup_head, checks_etag, status_etag = self._store.get_rollup_etags(item_key)
        head = pr_head or rollup_head
        if head is None:
            first_time = item_key not in self._headless_marked
            self._headless_marked.add(item_key)
            self._checks_polled_at[item_key] = now
            return first_time
        self._headless_marked.discard(item_key)
        if rollup_head != head:
            checks_etag = status_etag = None
        owner, name, _number = parse_item_key(item_key)
        base = f"repos/{owner}/{name}/commits/{head}"
        try:
            checks = self._transport.rest_get(
                f"{base}/check-runs?per_page=100",
                worker=WORKER,
                priority=Priority.HOT,
                etag=checks_etag,
            )
            status = self._transport.rest_get(
                f"{base}/status", worker=WORKER, priority=Priority.HOT, etag=status_etag
            )
        except GhApiError:
            self._checks_polled_at[item_key] = now
            return False
        self._store.set_rollup_etags(
            item_key,
            head,
            checks.headers.get("etag") or checks_etag,
            status.headers.get("etag") or status_etag,
            now,
        )
        self._checks_polled_at[item_key] = now
        return _response_changed(checks) or _response_changed(status)
