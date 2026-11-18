"""Multi-user `personal.sqlite` store, waiter condition, and server config loader.

Separates per-caller identities, viewer sets, watch leases, notifications,
refresh requests, and change-feed rows from shared `repos.sqlite` so schema v15
stays untouched and `/api/v1/sql` DuckDB workers never attach personal tables.
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any, Literal

try:
    from .db_migrations import CONNECTION_PRAGMAS
    from .managed_git import validate_branch, validate_slug, validate_url
    from .models import (
        ChangeKind,
        ChangeRecord,
        NotificationRecord,
        SeedRepoSpec,
        ServerConfig,
        UserIdentity,
        WatchLease,
        format_item_key,
        iso_at,
        parse_item_key,
    )
except ImportError:
    from db_migrations import CONNECTION_PRAGMAS  # type: ignore[no-redef]
    from managed_git import (  # type: ignore[no-redef]
        validate_branch,
        validate_slug,
        validate_url,
    )
    from models import (  # type: ignore[no-redef]
        ChangeKind,
        ChangeRecord,
        NotificationRecord,
        SeedRepoSpec,
        ServerConfig,
        UserIdentity,
        WatchLease,
        format_item_key,
        iso_at,
        parse_item_key,
    )

DAY_S = 86400
_GITHUB_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")

PERSONAL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS user_identities (
  owner_id TEXT PRIMARY KEY,
  github_login TEXT NOT NULL,
  login_verified INTEGER NOT NULL DEFAULT 0,
  notifications_last_modified TEXT,
  notifications_poll_interval_s INTEGER NOT NULL DEFAULT 60,
  last_active_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_viewer_sets (
  viewer_login TEXT NOT NULL,
  set_name TEXT NOT NULL,
  repo_full_name TEXT NOT NULL,
  item_number INTEGER NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  PRIMARY KEY (viewer_login, set_name, repo_full_name, item_number)
);
CREATE TABLE IF NOT EXISTS user_viewer_set_sync (
  viewer_login TEXT NOT NULL,
  set_name TEXT NOT NULL,
  synced_at TEXT NOT NULL,
  PRIMARY KEY (viewer_login, set_name)
);
CREATE TABLE IF NOT EXISTS user_watches (
  owner_id TEXT NOT NULL,
  consumer TEXT NOT NULL,
  consumer_id TEXT NOT NULL,
  repo_full_name TEXT NOT NULL,
  item_number INTEGER NOT NULL,
  freshness_s INTEGER NOT NULL,
  lease_expires_at TEXT NOT NULL,
  PRIMARY KEY (owner_id, consumer, consumer_id, repo_full_name, item_number)
);
CREATE TABLE IF NOT EXISTS user_notifications (
  owner_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  reason TEXT,
  subject_type TEXT,
  item_key TEXT,
  updated_at TEXT,
  unread INTEGER NOT NULL DEFAULT 1,
  seen_at TEXT NOT NULL,
  PRIMARY KEY (owner_id, thread_id)
);
CREATE TABLE IF NOT EXISTS user_refresh_requests (
  owner_id TEXT NOT NULL,
  repo_full_name TEXT NOT NULL,
  item_number INTEGER NOT NULL,
  requested_at TEXT NOT NULL,
  PRIMARY KEY (owner_id, repo_full_name, item_number)
);
CREATE TABLE IF NOT EXISTS user_changes (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  viewer_login TEXT,
  owner_id TEXT,
  item_key TEXT NOT NULL,
  kind TEXT NOT NULL,
  fields TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_changes_seq
  ON user_changes(seq, viewer_login, owner_id);
"""


class TooManyWaitersError(RuntimeError):
    """Raised when concurrent `GET /api/v1/changes` waiters exceed `max_waiters`."""


class IdentityConflictError(ValueError):
    """Raised when an unverified link attempts to claim an active verified handle."""


class ChangeNotifier:
    """In-memory wakeup condition with a bounded waiter cap for `GET /changes`."""

    def __init__(self, max_waiters: int = 64) -> None:
        """Initializes the condition with a maximum concurrent waiter count."""
        self.max_waiters = int(max_waiters)
        self._cond = threading.Condition()
        self._waiters = 0

    @property
    def active_waiters(self) -> int:
        """Current count of threads blocked in `wait_until`."""
        with self._cond:
            return self._waiters

    def notify_all(self) -> None:
        """Wakes every waiting `/changes` handler after a committed mutation."""
        with self._cond:
            self._cond.notify_all()

    def wait_until(self, deadline_mono: float) -> bool:
        """Waits until notified or `deadline_mono` elapses.

        Raises:
            TooManyWaitersError: If `max_waiters` threads are already waiting.
        """
        with self._cond:
            remaining = deadline_mono - time.monotonic()
            if remaining <= 0:
                return False
            if self._waiters >= self.max_waiters:
                raise TooManyWaitersError(
                    f"Too many concurrent change waiters (max {self.max_waiters})."
                )
            self._waiters += 1
            try:
                return bool(self._cond.wait(timeout=remaining))
            finally:
                self._waiters -= 1


def normalize_github_login(raw: str) -> str:
    """Validates and normalizes a GitHub login to lowercase."""
    cleaned = raw.strip().lstrip("@")
    if not _GITHUB_LOGIN_RE.match(cleaned):
        raise ValueError(f"Invalid GitHub login {raw!r}.")
    return cleaned.lower()


def _parse_seed_repo(
    raw: Any,
    default_source: Literal["managed", "none"],
    allowed_protocols: Sequence[str],
) -> SeedRepoSpec:
    if not isinstance(raw, dict):
        raise TypeError("Each server config `repos` entry must be an object.")
    full_name = str(raw.get("full_name") or "").strip()
    if "/" not in full_name:
        raise ValueError(f"Invalid seed repository full_name {full_name!r}.")
    owner, name = full_name.split("/", 1)
    validate_slug(owner, "owner")
    validate_slug(name, "name")
    branch_raw = str(raw.get("default_branch") or "").strip()
    branch = validate_branch(branch_raw) if branch_raw else ""
    src_raw = str(raw.get("git_source") or default_source).strip()
    if src_raw not in ("managed", "none"):
        raise ValueError(f"Unsupported server repo git_source {src_raw!r}.")
    git_source: Literal["managed", "none"] = (
        "managed" if src_raw == "managed" else "none"
    )
    git_url_raw = raw.get("git_url")
    git_url = (
        validate_url(str(git_url_raw).strip(), allowed_schemes=allowed_protocols)
        if git_url_raw is not None and str(git_url_raw).strip()
        else None
    )
    return SeedRepoSpec(
        full_name=f"{owner}/{name}",
        default_branch=branch,
        git_source=git_source,
        git_url=git_url,
    )


def load_server_config(path: Path) -> ServerConfig:
    """Loads and validates a JSON `ServerConfig` file from `path`."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("Server config root must be a JSON object.")
    orgs_raw = payload.get("allowlisted_orgs", ("a2ui-project", "flutter", "dart-lang"))
    allowlisted_orgs = tuple(
        validate_slug(str(o).strip(), "org").lower() for o in orgs_raw
    )
    admins_raw = payload.get("admin_users", ())
    admin_users = tuple(str(u).strip().lower() for u in admins_raw if str(u).strip())
    def_src_raw = str(payload.get("default_git_source", "managed")).strip()
    if def_src_raw not in ("managed", "none"):
        raise ValueError(f"Invalid default_git_source {def_src_raw!r}.")
    default_git_source: Literal["managed", "none"] = (
        "managed" if def_src_raw == "managed" else "none"
    )
    protos_raw = payload.get("allowed_git_protocols", ("https",))
    protos = tuple(
        str(p).strip().lower()
        for p in protos_raw
        if str(p).strip().lower() in ("https", "ssh")
    )
    if not protos:
        raise ValueError("allowed_git_protocols must include 'https' or 'ssh'.")
    disk_pct = float(payload.get("disk_high_water_pct", 85.0))
    hmac_env = str(
        payload.get("hmac_secret_env", "PACKRAT_EDGE_HMAC_KEYS_JSON")
    ).strip()
    repos = tuple(
        _parse_seed_repo(entry, default_git_source, protos)
        for entry in payload.get("repos", ())
    )
    return ServerConfig(
        allowlisted_orgs=allowlisted_orgs,
        admin_users=admin_users,
        default_git_source=default_git_source,
        allowed_git_protocols=protos,
        disk_high_water_pct=disk_pct,
        hmac_secret_env=hmac_env,
        repos=repos,
    )


def _row_to_identity(row: sqlite3.Row) -> UserIdentity:
    return UserIdentity(
        owner_id=str(row["owner_id"]),
        github_login=str(row["github_login"]),
        login_verified=bool(row["login_verified"]),
        notifications_last_modified=row["notifications_last_modified"],
        notifications_poll_interval_s=int(row["notifications_poll_interval_s"] or 60),
        last_active_at=str(row["last_active_at"]),
        updated_at=str(row["updated_at"]),
    )


def _split_item_keys(item_keys: Iterable[str]) -> list[tuple[str, int, str]]:
    """Parses `owner/name#number` keys into deduplicated `(repo_full_name, number, key)` tuples."""
    seen: dict[tuple[str, int], tuple[str, int, str]] = {}
    for raw in item_keys:
        owner, name, number = parse_item_key(raw)
        repo_name = f"{owner}/{name}"
        dedup_key = (repo_name.lower(), number)
        if dedup_key not in seen:
            seen[dedup_key] = (repo_name, number, format_item_key(owner, name, number))
    return list(seen.values())


class PersonalStore:
    """Thread-isolated SQLite store for server-only multi-user tables."""

    def __init__(
        self,
        db_path: Path,
        *,
        notifier: ChangeNotifier | None = None,
        read_only: bool = False,
    ) -> None:
        """Opens `personal.sqlite` for the current thread."""
        self.db_path = Path(db_path)
        self.notifier = notifier
        self.read_only = read_only
        if read_only:
            uri = f"{self.db_path.expanduser().resolve().as_uri()}?mode=ro"
            self.conn = sqlite3.connect(
                uri, uri=True, timeout=5.0, check_same_thread=True
            )
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA busy_timeout = 5000")
            return
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.conn = sqlite3.connect(
            str(self.db_path), timeout=5.0, check_same_thread=True
        )
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.executescript(CONNECTION_PRAGMAS)
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                for stmt in PERSONAL_SCHEMA_SQL.split(";"):
                    if stmt.strip():
                        self.conn.execute(stmt)
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
        except BaseException:
            self.conn.close()
            raise

    def close(self) -> None:
        """Closes the underlying SQLite connection."""
        self.conn.close()

    @contextlib.contextmanager
    def _immediate_tx(self) -> Iterator[None]:
        """Runs a short `BEGIN IMMEDIATE` transaction."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def _notify(self) -> None:
        if self.notifier is not None:
            self.notifier.notify_all()

    # ----- identities ---------------------------------------------------------------

    def get_identity(self, owner_id: str) -> UserIdentity | None:
        """Returns the stored `UserIdentity` for `owner_id`, or None."""
        row = self.conn.execute(
            "SELECT * FROM user_identities WHERE owner_id = ?", (owner_id,)
        ).fetchone()
        return _row_to_identity(row) if row is not None else None

    def _check_unverified_claim(
        self, owner_id: str, login: str, cutoff_iso: str
    ) -> None:
        conflict = self.conn.execute(
            """
            SELECT owner_id FROM user_identities
            WHERE github_login = ? AND login_verified = 1
              AND owner_id != ? AND last_active_at >= ?
            LIMIT 1
            """,
            (login, owner_id, cutoff_iso),
        ).fetchone()
        if conflict is not None:
            raise IdentityConflictError(
                f"GitHub login '{login}' is verified by another active user; "
                "authenticate with a GitHub token to claim it."
            )

    def link_identity(
        self,
        owner_id: str,
        github_login: str,
        *,
        verified: bool,
        now: float | None = None,
    ) -> UserIdentity:
        """Binds `owner_id` to `github_login`, enforcing `login_verified` protections."""
        moment = time.time() if now is None else now
        now_iso = iso_at(moment)
        login = normalize_github_login(github_login)
        cutoff_30d = iso_at(moment - 30 * DAY_S)
        with self._immediate_tx():
            if not verified:
                self._check_unverified_claim(owner_id, login, cutoff_30d)
            prev = self.conn.execute(
                "SELECT github_login, login_verified FROM user_identities WHERE owner_id = ?",
                (owner_id,),
            ).fetchone()
            keep_verified = bool(
                verified
                or (
                    prev is not None
                    and prev["github_login"] == login
                    and bool(prev["login_verified"])
                )
            )
            login_changed = prev is not None and str(prev["github_login"]) != login
            if not keep_verified or login_changed:
                self.conn.execute(
                    "DELETE FROM user_notifications WHERE owner_id = ?", (owner_id,)
                )
            self.conn.execute(
                """
                INSERT INTO user_identities (
                  owner_id, github_login, login_verified, notifications_last_modified,
                  notifications_poll_interval_s, last_active_at, updated_at
                ) VALUES (?, ?, ?, NULL, 60, ?, ?)
                ON CONFLICT(owner_id) DO UPDATE SET
                  github_login = excluded.github_login,
                  login_verified = excluded.login_verified,
                  notifications_last_modified = CASE
                    WHEN user_identities.github_login = excluded.github_login
                         AND excluded.login_verified = 1
                    THEN user_identities.notifications_last_modified
                    ELSE NULL
                  END,
                  last_active_at = excluded.last_active_at,
                  updated_at = excluded.updated_at
                """,
                (owner_id, login, int(keep_verified), now_iso, now_iso),
            )
        ident = self.get_identity(owner_id)
        if ident is None:  # pragma: no cover
            raise RuntimeError(f"Failed to persist identity for {owner_id}")
        return ident

    def touch_identity(self, owner_id: str, now: float | None = None) -> None:
        """Refreshes `last_active_at` when `owner_id` has a linked identity."""
        with self._immediate_tx():
            self.conn.execute(
                "UPDATE user_identities SET last_active_at = ? WHERE owner_id = ?",
                (iso_at(now), owner_id),
            )

    def active_identities(
        self, within_days: int = 7, now: float | None = None
    ) -> list[UserIdentity]:
        """Returns identities active within `within_days`, ordered by `owner_id`."""
        moment = time.time() if now is None else now
        cutoff = iso_at(moment - within_days * DAY_S)
        rows = self.conn.execute(
            """
            SELECT * FROM user_identities
            WHERE last_active_at >= ?
            ORDER BY owner_id ASC
            """,
            (cutoff,),
        ).fetchall()
        return [_row_to_identity(r) for r in rows]

    def update_notification_poll_state(
        self,
        owner_id: str,
        *,
        last_modified: str | None,
        poll_interval_s: int,
        now: float | None = None,
    ) -> None:
        """Persists per-user `/notifications` `Last-Modified` and poll interval."""
        with self._immediate_tx():
            self.conn.execute(
                """
                UPDATE user_identities
                SET notifications_last_modified = COALESCE(?, notifications_last_modified),
                    notifications_poll_interval_s = ?,
                    updated_at = ?
                WHERE owner_id = ?
                """,
                (last_modified, max(10, int(poll_interval_s)), iso_at(now), owner_id),
            )

    # ----- viewer sets --------------------------------------------------------------

    def replace_viewer_set(
        self,
        viewer_login: str,
        set_name: str,
        item_keys: Sequence[str],
        now: float | None = None,
    ) -> tuple[list[str], list[str]]:
        """Replaces `(viewer_login, set_name)` members and appends scoped viewer changes."""
        login = normalize_github_login(viewer_login)
        now_iso = iso_at(now)
        parsed = _split_item_keys(item_keys)
        current_map = {canon: (repo_name, num) for repo_name, num, canon in parsed}
        prev_rows = self.conn.execute(
            """
            SELECT repo_full_name || '#' || item_number AS item_key,
                   repo_full_name, item_number
            FROM user_viewer_sets
            WHERE viewer_login = ? AND set_name = ?
            """,
            (login, set_name),
        ).fetchall()
        prev_map = {
            str(r["item_key"]): (str(r["repo_full_name"]), int(r["item_number"]))
            for r in prev_rows
        }
        added = sorted(set(current_map) - set(prev_map))
        removed = sorted(set(prev_map) - set(current_map))
        with self._immediate_tx():
            if removed:
                self.conn.executemany(
                    """
                    DELETE FROM user_viewer_sets
                    WHERE viewer_login = ? AND set_name = ?
                      AND repo_full_name = ? AND item_number = ?
                    """,
                    [(login, set_name, *prev_map[k]) for k in removed],
                )
            if current_map:
                self.conn.executemany(
                    """
                    INSERT INTO user_viewer_sets (
                      viewer_login, set_name, repo_full_name, item_number,
                      first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(viewer_login, set_name, repo_full_name, item_number)
                    DO UPDATE SET last_seen_at = excluded.last_seen_at
                    """,
                    [
                        (login, set_name, *current_map[k], now_iso, now_iso)
                        for k in sorted(current_map)
                    ],
                )
            self.conn.execute(
                """
                INSERT INTO user_viewer_set_sync (viewer_login, set_name, synced_at)
                VALUES (?, ?, ?)
                ON CONFLICT(viewer_login, set_name)
                DO UPDATE SET synced_at = excluded.synced_at
                """,
                (login, set_name, now_iso),
            )
            for key in [*added, *removed]:
                self._insert_change_row(
                    key,
                    "viewer",
                    [set_name],
                    now_iso,
                    viewer_login=login,
                    owner_id=None,
                )
        if added or removed:
            self._notify()
        return added, removed

    def viewer_set_sync_times(self, viewer_login: str) -> dict[str, str]:
        """Returns `{set_name: synced_at}` for `viewer_login`."""
        cleaned_login = viewer_login.strip().lstrip("@").lower()
        if not cleaned_login:
            return {}
        rows = self.conn.execute(
            "SELECT set_name, synced_at FROM user_viewer_set_sync "
            "WHERE viewer_login = ? ORDER BY set_name ASC",
            (cleaned_login,),
        ).fetchall()
        return {str(r["set_name"]): str(r["synced_at"]) for r in rows}

    def viewer_items_for_login(
        self,
        viewer_login: str,
        *,
        sets: Sequence[str] | None = None,
        repo: str | None = None,
    ) -> list[dict[str, Any]]:
        """Returns `user_viewer_sets` rows for `viewer_login`."""
        cleaned_login = viewer_login.strip().lstrip("@").lower()
        if not cleaned_login:
            return []
        clauses = ["viewer_login = ?"]
        params: list[Any] = [cleaned_login]
        if sets:
            marks = ",".join("?" for _ in sets)
            clauses.append(f"set_name IN ({marks})")
            params.extend(sets)
        if repo and repo.strip():
            clauses.append("lower(repo_full_name) = lower(?)")
            params.append(repo.strip())
        sql = (
            "SELECT set_name, repo_full_name, item_number, "
            "repo_full_name || '#' || item_number AS item_key, "
            "first_seen_at, last_seen_at "
            f"FROM user_viewer_sets WHERE {' AND '.join(clauses)} "
            "ORDER BY set_name ASC, repo_full_name ASC, item_number ASC"
        )
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # ----- watches & refresh requests -----------------------------------------------

    def add_watch(
        self,
        owner_id: str,
        consumer: str,
        consumer_id: str,
        item_keys: Sequence[str],
        freshness_s: int,
        ttl_s: int,
        now: float | None = None,
    ) -> list[str]:
        """Leases `item_keys` for `(owner_id, consumer, consumer_id)`."""
        moment = time.time() if now is None else now
        expires = iso_at(moment + ttl_s)
        parsed = _split_item_keys(item_keys)
        if not parsed:
            return []
        with self._immediate_tx():
            self.conn.executemany(
                """
                INSERT INTO user_watches (
                  owner_id, consumer, consumer_id, repo_full_name, item_number,
                  freshness_s, lease_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_id, consumer, consumer_id, repo_full_name, item_number)
                DO UPDATE SET
                  freshness_s = excluded.freshness_s,
                  lease_expires_at = excluded.lease_expires_at
                """,
                [
                    (
                        owner_id,
                        consumer,
                        consumer_id,
                        repo_name,
                        num,
                        int(freshness_s),
                        expires,
                    )
                    for repo_name, num, _ in parsed
                ],
            )
        return [canon for _, _, canon in parsed]

    def _watch_where(
        self,
        owner_id: str,
        consumer: str,
        consumer_id: str,
        item_keys: Sequence[str] | None,
    ) -> tuple[str, list[Any]]:
        where = "owner_id = ? AND consumer = ? AND consumer_id = ?"
        params: list[Any] = [owner_id, consumer, consumer_id]
        if item_keys is None:
            return where, params
        parsed = _split_item_keys(item_keys)
        if not parsed:
            return "1 = 0", []
        values_sql = ", ".join("(?, ?)" for _ in parsed)
        where += f" AND (lower(repo_full_name), item_number) IN (VALUES {values_sql})"
        for repo_name, num, _ in parsed:
            params.extend((repo_name.lower(), num))
        return where, params

    def renew_watch(
        self,
        owner_id: str,
        consumer: str,
        consumer_id: str,
        ttl_s: int,
        item_keys: Sequence[str] | None = None,
        now: float | None = None,
    ) -> int:
        """Extends leases owned by `(owner_id, consumer, consumer_id)`."""
        moment = time.time() if now is None else now
        where, params = self._watch_where(owner_id, consumer, consumer_id, item_keys)
        if where == "1 = 0":
            return 0
        with self._immediate_tx():
            cur = self.conn.execute(
                f"UPDATE user_watches SET lease_expires_at = ? WHERE {where}",
                [iso_at(moment + ttl_s), *params],
            )
        return cur.rowcount

    def remove_watch(
        self,
        owner_id: str,
        consumer: str,
        consumer_id: str,
        item_keys: Sequence[str] | None = None,
    ) -> int:
        """Removes leases owned by `(owner_id, consumer, consumer_id)`."""
        where, params = self._watch_where(owner_id, consumer, consumer_id, item_keys)
        if where == "1 = 0":
            return 0
        with self._immediate_tx():
            cur = self.conn.execute(f"DELETE FROM user_watches WHERE {where}", params)
        return cur.rowcount

    def active_watches(
        self, now: float | None = None, *, owner_id: str | None = None
    ) -> list[WatchLease]:
        """Returns unexpired leases, optionally filtered to `owner_id`."""
        now_iso = iso_at(now)
        if owner_id is not None:
            rows = self.conn.execute(
                """
                SELECT consumer, consumer_id,
                       repo_full_name || '#' || item_number AS item_key,
                       freshness_s, lease_expires_at
                FROM user_watches
                WHERE owner_id = ? AND lease_expires_at > ?
                ORDER BY item_key, consumer
                """,
                (owner_id, now_iso),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT MIN(consumer) AS consumer, MIN(consumer_id) AS consumer_id,
                       MIN(repo_full_name) || '#' || item_number AS item_key,
                       MIN(freshness_s) AS freshness_s,
                       MAX(lease_expires_at) AS lease_expires_at
                FROM user_watches
                WHERE lease_expires_at > ?
                GROUP BY lower(repo_full_name), item_number
                ORDER BY item_key
                """,
                (now_iso,),
            ).fetchall()
        return [
            WatchLease(
                consumer=str(r["consumer"]),
                consumer_id=str(r["consumer_id"]),
                item_key=str(r["item_key"]),
                freshness_s=int(r["freshness_s"]),
                lease_expires_at=str(r["lease_expires_at"]),
            )
            for r in rows
        ]

    def prune_expired_watches(self, now: float | None = None) -> int:
        """Deletes expired leases across all callers."""
        with self._immediate_tx():
            cur = self.conn.execute(
                "DELETE FROM user_watches WHERE lease_expires_at <= ?", (iso_at(now),)
            )
        return cur.rowcount

    def request_refresh(
        self, owner_id: str, item_keys: Sequence[str], now: float | None = None
    ) -> list[str]:
        """Queues `item_keys` for immediate refresh tagged with `owner_id`."""
        parsed = _split_item_keys(item_keys)
        if not parsed:
            return []
        now_iso = iso_at(now)
        with self._immediate_tx():
            self.conn.executemany(
                """
                INSERT OR REPLACE INTO user_refresh_requests
                  (owner_id, repo_full_name, item_number, requested_at)
                VALUES (?, ?, ?, ?)
                """,
                [(owner_id, repo_name, num, now_iso) for repo_name, num, _ in parsed],
            )
        return [canon for _, _, canon in parsed]

    def take_user_refresh_requests(self) -> list[tuple[str, str]]:
        """Atomically removes and returns `(owner_id, item_key)` refresh pairs."""
        if (
            self.conn.execute("SELECT 1 FROM user_refresh_requests LIMIT 1").fetchone()
            is None
        ):
            return []
        with self._immediate_tx():
            rows = self.conn.execute(
                """
                SELECT owner_id, repo_full_name, item_number,
                       repo_full_name || '#' || item_number AS item_key
                FROM user_refresh_requests
                ORDER BY requested_at ASC, item_key ASC
                """
            ).fetchall()
            if rows:
                self.conn.executemany(
                    """
                    DELETE FROM user_refresh_requests
                    WHERE owner_id = ? AND repo_full_name = ? AND item_number = ?
                    """,
                    [
                        (
                            str(r["owner_id"]),
                            str(r["repo_full_name"]),
                            int(r["item_number"]),
                        )
                        for r in rows
                    ],
                )
        return [(str(r["owner_id"]), str(r["item_key"])) for r in rows]

    def requeue_user_refresh_requests(
        self, pairs: Sequence[tuple[str, str]], now: float | None = None
    ) -> None:
        """Re-inserts failed `(owner_id, item_key)` refresh pairs preserving ownership."""
        if not pairs:
            return
        now_iso = iso_at(now)
        rows: list[tuple[str, str, int, str]] = []
        for owner_id, key in pairs:
            owner, name, num = parse_item_key(key)
            rows.append((owner_id, f"{owner}/{name}", num, now_iso))
        with self._immediate_tx():
            self.conn.executemany(
                """
                INSERT OR REPLACE INTO user_refresh_requests
                  (owner_id, repo_full_name, item_number, requested_at)
                VALUES (?, ?, ?, ?)
                """,
                rows,
            )

    # ----- notifications ------------------------------------------------------------

    def upsert_notifications(
        self,
        owner_id: str,
        rows: Sequence[NotificationRecord],
        now: float | None = None,
    ) -> list[str]:
        """Stores notification threads for `owner_id` and emits owner-private changes."""
        if not rows:
            return []
        now_iso = iso_at(now)
        tids = [rec.thread_id for rec in rows]
        marks = ",".join("?" for _ in tids)
        existing = {
            str(r["thread_id"]): (r["updated_at"], int(r["unread"]))
            for r in self.conn.execute(
                f"SELECT thread_id, updated_at, unread FROM user_notifications "
                f"WHERE owner_id = ? AND thread_id IN ({marks})",
                [owner_id, *tids],
            ).fetchall()
        }
        changed: list[str] = []
        values: list[tuple[Any, ...]] = []
        for rec in rows:
            state = (rec.updated_at, int(rec.unread))
            is_new = existing.get(rec.thread_id) != state
            existing[rec.thread_id] = state
            values.append(
                (
                    owner_id,
                    rec.thread_id,
                    rec.reason,
                    rec.subject_type,
                    rec.item_key,
                    rec.updated_at,
                    int(rec.unread),
                    now_iso,
                )
            )
            if is_new and rec.item_key:
                changed.append(rec.item_key)
        with self._immediate_tx():
            self.conn.executemany(
                """
                INSERT OR REPLACE INTO user_notifications (
                  owner_id, thread_id, reason, subject_type, item_key,
                  updated_at, unread, seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            for key in changed:
                self._insert_change_row(
                    key,
                    "notification",
                    ["reason"],
                    now_iso,
                    viewer_login=None,
                    owner_id=owner_id,
                )
        if changed:
            self._notify()
        return changed

    def notifications_for_owner(
        self,
        owner_id: str,
        *,
        item_keys: Sequence[str] | None = None,
        require_verified: bool = True,
    ) -> list[dict[str, Any]]:
        """Returns notification rows for `owner_id` (empty when `login_verified == 0`)."""
        if require_verified and owner_id != "local":
            ident = self.get_identity(owner_id)
            if ident is None or not ident.login_verified:
                return []
        if item_keys is not None:
            if not item_keys:
                return []
            lowered = [k.lower() for k in item_keys]
            marks = ",".join("?" for _ in lowered)
            rows = self.conn.execute(
                f"SELECT thread_id, reason, subject_type, item_key, updated_at, unread, seen_at "
                f"FROM user_notifications WHERE owner_id = ? AND lower(item_key) IN ({marks})",
                [owner_id, *lowered],
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT thread_id, reason, subject_type, item_key, updated_at, unread, seen_at "
                "FROM user_notifications WHERE owner_id = ? ORDER BY updated_at DESC",
                (owner_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ----- changes feed -------------------------------------------------------------

    def _insert_change_row(
        self,
        item_key: str,
        kind: str,
        fields: Sequence[str] | str,
        now_iso: str,
        *,
        viewer_login: str | None,
        owner_id: str | None,
    ) -> int:
        encoded = fields if isinstance(fields, str) else json.dumps(list(fields))
        cur = self.conn.execute(
            """
            INSERT INTO user_changes (viewer_login, owner_id, item_key, kind, fields, at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                viewer_login.lower() if viewer_login else None,
                owner_id,
                item_key,
                kind,
                encoded,
                now_iso,
            ),
        )
        return int(cur.lastrowid or 0)

    def append_change(
        self,
        item_key: str,
        kind: str,
        fields: Sequence[str] | str,
        *,
        viewer_login: str | None = None,
        owner_id: str | None = None,
        now: float | None = None,
    ) -> int:
        """Appends a change row in `user_changes` and wakes waiting `/changes` clients."""
        with self._immediate_tx():
            seq = self._insert_change_row(
                item_key,
                kind,
                fields,
                iso_at(now),
                viewer_login=viewer_login,
                owner_id=owner_id,
            )
        self._notify()
        return seq

    def max_change_seq(self) -> int:
        """Returns the highest sequence number ever assigned in `user_changes`."""
        row = self.conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'user_changes'"
        ).fetchone()
        if row is not None and row["seq"] is not None:
            return int(row["seq"])
        fallback = self.conn.execute(
            "SELECT MAX(seq) AS s FROM user_changes"
        ).fetchone()
        return int(fallback["s"]) if fallback and fallback["s"] is not None else 0

    def min_change_seq(self) -> int:
        """Returns the lowest `since` cursor that has not missed pruned rows."""
        row = self.conn.execute("SELECT MIN(seq) AS s FROM user_changes").fetchone()
        if row is None or row["s"] is None:
            return self.max_change_seq()
        return int(row["s"]) - 1

    def changes_since_for_caller(
        self,
        since: int,
        *,
        viewer_login: str,
        owner_id: str,
        limit: int = 500,
    ) -> tuple[int, bool, list[ChangeRecord]]:
        """Returns `(next_seq, truncated, changes)` visible to `(owner_id, viewer_login)`."""
        clean_login = viewer_login.strip().lstrip("@").lower()
        max_seq = self.max_change_seq()
        if clean_login:
            viewer_sql = "(viewer_login IS NULL OR viewer_login = ?)"
            params: list[Any] = [int(since), clean_login, owner_id, int(limit) + 1]
        else:
            viewer_sql = "viewer_login IS NULL"
            params = [int(since), owner_id, int(limit) + 1]
        rows = self.conn.execute(
            f"""
            SELECT seq, item_key, kind, fields, at
            FROM user_changes
            WHERE seq > ?
              AND {viewer_sql}
              AND (owner_id IS NULL OR owner_id = ?)
            ORDER BY seq ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        truncated = len(rows) > limit
        visible = rows[:limit]
        changes: list[ChangeRecord] = []
        for r in visible:
            kind_val: ChangeKind = str(r["kind"])  # type: ignore[assignment]
            changes.append(
                ChangeRecord(
                    seq=int(r["seq"]),
                    item_key=str(r["item_key"]),
                    kind=kind_val,
                    fields=tuple(json.loads(r["fields"] or "[]")),
                    at=str(r["at"]),
                )
            )
        if truncated and changes:
            next_seq = changes[-1].seq
        else:
            next_seq = max(int(since), max_seq)
        return next_seq, truncated, changes

    def caller_temp_rows(
        self, *, owner_id: str, viewer_login: str, now: float | None = None
    ) -> dict[str, list[tuple[Any, ...]]]:
        """Extracts caller-scoped personal rows for `DuckDbSqlSandbox` temp tables."""
        clean_login = viewer_login.strip().lstrip("@").lower()
        viewer_rows = (
            [
                (r["set_name"], r["item_key"], r["first_seen_at"], r["last_seen_at"])
                for r in self.viewer_items_for_login(clean_login)
            ]
            if clean_login
            else []
        )
        now_iso = iso_at(now)
        watch_rows = [
            (
                str(r["consumer"]),
                str(r["consumer_id"]),
                f"{r['repo_full_name']}#{r['item_number']}",
                int(r["freshness_s"]),
                str(r["lease_expires_at"]),
                bool(str(r["lease_expires_at"]) > now_iso),
            )
            for r in self.conn.execute(
                "SELECT * FROM user_watches WHERE owner_id = ?", (owner_id,)
            ).fetchall()
        ]
        notif_rows = [
            (
                r["thread_id"],
                r["reason"],
                r["subject_type"],
                r["item_key"],
                r["updated_at"],
                int(r["unread"]),
                r["seen_at"],
            )
            for r in self.notifications_for_owner(owner_id)
        ]
        _, _, chg = self.changes_since_for_caller(
            0, viewer_login=clean_login, owner_id=owner_id, limit=5000
        )
        change_rows = [
            (c.seq, c.item_key, c.kind, json.dumps(list(c.fields)), c.at) for c in chg
        ]
        return {
            "viewer_items": viewer_rows,
            "watches": watch_rows,
            "notifications": notif_rows,
            "changes": change_rows,
        }

    # ----- repository lifecycle & retention -----------------------------------------

    def rewrite_repo_full_name(self, old_full_name: str, new_full_name: str) -> None:
        """Rewrites `old_full_name` references across all personal tables."""
        old_prefix = f"{old_full_name}#"
        plen = len(old_prefix)
        with self._immediate_tx():
            for table, pk_cols in (
                (
                    "user_viewer_sets",
                    "viewer_login, set_name, repo_full_name, item_number",
                ),
                (
                    "user_watches",
                    "owner_id, consumer, consumer_id, repo_full_name, item_number",
                ),
                ("user_refresh_requests", "owner_id, repo_full_name, item_number"),
            ):
                del pk_cols
                self.conn.execute(
                    f"DELETE FROM {table} WHERE lower(repo_full_name) = lower(?) "
                    f"AND lower(?) != lower(?)",
                    (new_full_name, old_full_name, new_full_name),
                )
                self.conn.execute(
                    f"UPDATE OR REPLACE {table} SET repo_full_name = ? "
                    f"WHERE lower(repo_full_name) = lower(?)",
                    (new_full_name, old_full_name),
                )
            self.conn.execute(
                """
                UPDATE user_notifications
                SET item_key = ? || substr(item_key, ?)
                WHERE item_key IS NOT NULL AND lower(substr(item_key, 1, ?)) = lower(?)
                """,
                (new_full_name, plen, plen, old_prefix),
            )
            self.conn.execute(
                """
                UPDATE user_changes
                SET item_key = ? || substr(item_key, ?)
                WHERE lower(substr(item_key, 1, ?)) = lower(?)
                """,
                (new_full_name, plen, plen, old_prefix),
            )

    def delete_repo_rows(self, full_name: str) -> None:
        """Deletes all rows referencing `full_name` across `personal.sqlite`."""
        prefix = f"{full_name}#"
        plen = len(prefix)
        with self._immediate_tx():
            for table in ("user_viewer_sets", "user_watches", "user_refresh_requests"):
                self.conn.execute(
                    f"DELETE FROM {table} WHERE lower(repo_full_name) = lower(?)",
                    (full_name,),
                )
            self.conn.execute(
                """
                DELETE FROM user_notifications
                WHERE item_key IS NOT NULL AND lower(substr(item_key, 1, ?)) = lower(?)
                """,
                (plen, prefix),
            )
            self.conn.execute(
                "DELETE FROM user_changes WHERE lower(substr(item_key, 1, ?)) = lower(?)",
                (plen, prefix),
            )

    def referenced_repo_names(
        self, cutoff_iso: str, *, now_iso: str | None = None
    ) -> set[str]:
        """Returns lowercased `owner/name` slugs actively referenced in `personal.sqlite`."""
        active_now = now_iso or iso_at()
        names: set[str] = set()
        for sql, param in (
            (
                "SELECT DISTINCT lower(repo_full_name) FROM user_watches WHERE lease_expires_at > ?",
                active_now,
            ),
            (
                "SELECT DISTINCT lower(repo_full_name) FROM user_viewer_sets WHERE last_seen_at >= ?",
                cutoff_iso,
            ),
        ):
            names.update(
                str(r[0]) for r in self.conn.execute(sql, (param,)).fetchall() if r[0]
            )
        for sql in (
            "SELECT DISTINCT item_key FROM user_notifications WHERE item_key IS NOT NULL AND seen_at >= ?",
            "SELECT DISTINCT item_key FROM user_changes WHERE at >= ?",
        ):
            for (raw_key,) in self.conn.execute(sql, (cutoff_iso,)).fetchall():
                if isinstance(raw_key, str) and "#" in raw_key:
                    names.add(raw_key.split("#", 1)[0].lower())
        return names

    def prune(
        self,
        now: float | None = None,
        *,
        keep_changes_s: int = 7 * DAY_S,
        keep_notifications_s: int = 30 * DAY_S,
        keep_viewer_s: int = 30 * DAY_S,
    ) -> dict[str, int]:
        """Prunes expired leases and aged personal rows."""
        moment = time.time() if now is None else now
        deleted: dict[str, int] = {}
        with self._immediate_tx():
            for table, col, keep_s in (
                ("user_changes", "at", keep_changes_s),
                ("user_notifications", "seen_at", keep_notifications_s),
                ("user_viewer_sets", "last_seen_at", keep_viewer_s),
            ):
                cur = self.conn.execute(
                    f"DELETE FROM {table} WHERE {col} < ?",
                    (iso_at(moment - keep_s),),
                )
                deleted[table] = cur.rowcount
            cur_w = self.conn.execute(
                "DELETE FROM user_watches WHERE lease_expires_at <= ?",
                (iso_at(moment),),
            )
            deleted["user_watches"] = cur_w.rowcount
        return deleted
