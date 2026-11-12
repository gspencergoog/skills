"""Live GitHub state methods for `PackratStore`: leases, change feed, checks, and service state.

`LiveStoreMixin` is mixed into `db_store.PackratStore` and works on its
`self.conn`. Every `now` argument is epoch seconds (defaulting to the current
time) and is stored as ISO-8601 UTC text, so stored timestamps compare as
strings. Item keys are `owner/name#number`; methods that take keys resolve the
repository case-insensitively and use its registered `full_name`.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import time
from collections.abc import Iterable, Iterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

try:
    from .db_migrations import read_schema_version, read_stored_views_version
    from .models import (
        REPO_MODE_ITEMS,
        ChangeRecord,
        CheckRunRecord,
        ItemCommentRecord,
        LatestReviewRecord,
        NotificationRecord,
        OpinionatedReviewRecord,
        PrLiveRecord,
        RepoConfig,
        ReviewRequestRecord,
        ServiceHeartbeat,
        ThreadActivityRecord,
        WatchLease,
        format_item_key,
        iso_at,
        parse_item_key,
    )
except ImportError:
    from db_migrations import (  # type: ignore[no-redef]
        read_schema_version,
        read_stored_views_version,
    )
    from models import (  # type: ignore[no-redef]
        REPO_MODE_ITEMS,
        ChangeRecord,
        CheckRunRecord,
        ItemCommentRecord,
        LatestReviewRecord,
        NotificationRecord,
        OpinionatedReviewRecord,
        PrLiveRecord,
        RepoConfig,
        ReviewRequestRecord,
        ServiceHeartbeat,
        ThreadActivityRecord,
        WatchLease,
        format_item_key,
        iso_at,
        parse_item_key,
    )

DAY_S = 86400

PR_LIVE_COLUMNS: tuple[str, ...] = (
    "node_id",
    "title",
    "state",
    "is_draft",
    "url",
    "author",
    "author_type",
    "base_ref",
    "head_ref",
    "head_sha",
    "head_committed_date",
    "head_repo_owner",
    "is_cross_repository",
    "mergeable",
    "merge_state_status",
    "review_decision",
    "auto_merge_enabled_at",
    "created_at",
    "updated_at",
    "merged_at",
    "closed_at",
)
"""`pull_requests` columns owned by live refreshes (compared for change detection)."""

_ROLLUP_FIELDS: tuple[str, ...] = ("rollup_state", "rollup_total")

MIRROR_LIVE_FIELDS: tuple[str, ...] = (
    "node_id",
    "title",
    "state",
    "is_draft",
    "url",
    "author",
    "base_ref",
    "head_ref",
    "head_sha",
    "head_repo_owner",
    "is_cross_repository",
    "mergeable",
    "review_decision",
    "updated_at",
    "merged_at",
    "closed_at",
    "body",
)
"""Live-visible `pull_requests` columns that the mirror's PR upsert also writes."""

VIEWER_SET_STATE_PREFIX = "viewer_set:"
NODE_ID_STATE_PREFIX = "node_id:"


def _db_value(value: Any) -> Any:
    """Converts a Python value to its SQLite storage form (bools become 0/1)."""
    if isinstance(value, bool):
        return int(value)
    return value


def _opt_bool(value: Any) -> bool | None:
    """Converts a nullable SQLite integer to a nullable bool."""
    return None if value is None else bool(value)


class LiveStoreMixin:
    """Store methods for live GitHub data, mixed into `PackratStore`."""

    conn: sqlite3.Connection

    def get_repo_by_id(self, repo_id: int) -> RepoConfig | None:  # pragma: no cover
        """Looks up a repository by repo_id (implemented by `PackratStore`)."""
        raise NotImplementedError

    def _bump_vector_cache_seq_in_tx(
        self, now: float | None = None
    ) -> None:  # pragma: no cover
        """Increments `vector_cache_seq` in `service_state` (implemented by `PackratStore`)."""
        raise NotImplementedError

    # ----- repositories and item keys -------------------------------------------------

    def _find_repo_row(self, owner: str, name: str) -> sqlite3.Row | None:
        """Returns the repos row for `owner/name` (case-insensitive, including aliases), if registered."""
        full_name = f"{owner}/{name}"
        alias = self.conn.execute(
            """
            SELECT r.* FROM repo_aliases a
            JOIN repos r ON r.repo_id = a.repo_id
            WHERE a.alias_full_name = ?
            """,
            (full_name,),
        ).fetchone()
        if alias is not None:
            return alias
        return self.conn.execute(
            "SELECT * FROM repos WHERE lower(full_name) = lower(?)",
            (full_name,),
        ).fetchone()

    def _ensure_items_repo_in_tx(
        self, owner: str, name: str, now_iso: str
    ) -> RepoConfig:
        """Finds or inserts an `items` repository inside the caller's transaction."""
        row = self._find_repo_row(owner, name)
        if row is None:
            self.conn.execute(
                """
                INSERT INTO repos (owner, name, full_name, created_at, updated_at, mode)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(full_name) DO NOTHING
                """,
                (owner, name, f"{owner}/{name}", now_iso, now_iso, REPO_MODE_ITEMS),
            )
            row = self._find_repo_row(owner, name)
        repo = self.get_repo_by_id(int(row["repo_id"])) if row is not None else None
        if repo is None:
            raise RuntimeError(f"Failed to register repository {owner}/{name}")
        return repo

    def ensure_items_repo(
        self, owner: str, name: str, now: float | None = None
    ) -> RepoConfig:
        """Returns the repository for `owner/name`, registering it in `items` mode if new.

        An `items` repository gets no backfill, `/events` polling, or code
        indexing; only items that are watched, notified, or in a viewer set
        are stored. `register_repo()` promotes it to `mirror` later.
        """
        row = self._find_repo_row(owner, name)
        if row is not None:
            repo = self.get_repo_by_id(int(row["repo_id"]))
            if repo is not None:
                return repo
        with self.conn:
            return self._ensure_items_repo_in_tx(owner, name, iso_at(now))

    def find_repo_by_node_id(self, node_id: str) -> RepoConfig | None:
        """Returns the stored repository for a PR or issue GraphQL `node_id`, if any."""
        row = self.conn.execute(
            """
            SELECT repo_id FROM pull_requests WHERE node_id = ?
            UNION ALL
            SELECT repo_id FROM issues WHERE node_id = ?
            LIMIT 1
            """,
            (node_id, node_id),
        ).fetchone()
        if row is not None:
            return self.get_repo_by_id(int(row["repo_id"]))
        state_row = self.conn.execute(
            "SELECT key FROM service_state WHERE key LIKE ? AND value = ? LIMIT 1",
            (f"{NODE_ID_STATE_PREFIX}%", node_id),
        ).fetchone()
        if state_row is None:
            return None
        item_key = str(state_row["key"])[len(NODE_ID_STATE_PREFIX) :]
        owner, name, _ = parse_item_key(item_key)
        found = self._find_repo_row(owner, name)
        return self.get_repo_by_id(int(found["repo_id"])) if found is not None else None

    def _migrate_normalized_live_rows_in_tx(
        self, alias_repo_id: int, canonical_repo_id: int
    ) -> None:
        """Moves normalized `(repo_id, item_number)` rows from `alias_repo_id` to `canonical_repo_id`."""
        if alias_repo_id == canonical_repo_id:
            return
        self.conn.execute(
            """
            INSERT INTO watches (
              consumer, consumer_id, repo_id, item_number, freshness_s, lease_expires_at
            )
            SELECT consumer, consumer_id, ?, item_number, freshness_s, lease_expires_at
            FROM watches WHERE repo_id = ?
            ON CONFLICT(consumer, consumer_id, repo_id, item_number) DO UPDATE SET
              freshness_s = MIN(watches.freshness_s, excluded.freshness_s),
              lease_expires_at = MAX(watches.lease_expires_at, excluded.lease_expires_at)
            """,
            (canonical_repo_id, alias_repo_id),
        )
        self.conn.execute("DELETE FROM watches WHERE repo_id = ?", (alias_repo_id,))
        self.conn.execute(
            """
            INSERT INTO viewer_sets (
              set_name, repo_id, item_number, first_seen_at, last_seen_at
            )
            SELECT set_name, ?, item_number, first_seen_at, last_seen_at
            FROM viewer_sets WHERE repo_id = ?
            ON CONFLICT(set_name, repo_id, item_number) DO UPDATE SET
              first_seen_at = MIN(viewer_sets.first_seen_at, excluded.first_seen_at),
              last_seen_at = MAX(viewer_sets.last_seen_at, excluded.last_seen_at)
            """,
            (canonical_repo_id, alias_repo_id),
        )
        self.conn.execute("DELETE FROM viewer_sets WHERE repo_id = ?", (alias_repo_id,))
        self.conn.execute(
            """
            INSERT INTO refresh_requests (repo_id, item_number, requested_at)
            SELECT ?, item_number, requested_at
            FROM refresh_requests WHERE repo_id = ?
            ON CONFLICT(repo_id, item_number) DO UPDATE SET
              requested_at = MIN(refresh_requests.requested_at, excluded.requested_at)
            """,
            (canonical_repo_id, alias_repo_id),
        )
        self.conn.execute(
            "DELETE FROM refresh_requests WHERE repo_id = ?", (alias_repo_id,)
        )

    def _rewrite_alias_item_keys_in_tx(
        self,
        alias_repo_id: int,
        alias_full_name: str,
        canonical_repo_id: int,
        canonical_full_name: str,
    ) -> None:
        """Rewrites live state and log item keys from `alias_full_name` to `canonical_full_name`."""
        self._migrate_normalized_live_rows_in_tx(alias_repo_id, canonical_repo_id)
        prefix = f"{alias_full_name}#"
        plen = len(prefix)
        rewrite_args = (canonical_full_name, plen, plen, prefix)
        self.conn.execute(
            """
            UPDATE notifications SET item_key = ? || substr(item_key, ?)
            WHERE item_key IS NOT NULL AND lower(substr(item_key, 1, ?)) = lower(?)
            """,
            rewrite_args,
        )
        self.conn.execute(
            """
            UPDATE changes SET item_key = ? || substr(item_key, ?)
            WHERE lower(substr(item_key, 1, ?)) = lower(?)
            """,
            rewrite_args,
        )
        state_prefix = f"{NODE_ID_STATE_PREFIX}{alias_full_name}#"
        new_state_prefix = f"{NODE_ID_STATE_PREFIX}{canonical_full_name}#"
        slen = len(state_prefix)
        if state_prefix != new_state_prefix:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO service_state (key, value, updated_at)
                SELECT ? || substr(key, ?), value, updated_at
                FROM service_state WHERE lower(substr(key, 1, ?)) = lower(?)
                """,
                (
                    f"{NODE_ID_STATE_PREFIX}{canonical_full_name}",
                    slen,
                    slen,
                    state_prefix,
                ),
            )
            self.conn.execute(
                """
                DELETE FROM service_state
                WHERE lower(substr(key, 1, ?)) = lower(?) AND substr(key, 1, ?) <> ?
                """,
                (slen, state_prefix, len(new_state_prefix), new_state_prefix),
            )

    def _delete_repo_live_logs_in_tx(self, full_name: str) -> None:
        """Deletes matching `full_name#` rows from `notifications`, `changes`, and `service_state`."""
        prefix = f"{full_name}#"
        plen = len(prefix)
        self.conn.execute(
            """
            DELETE FROM notifications
            WHERE item_key IS NOT NULL AND lower(substr(item_key, 1, ?)) = lower(?)
            """,
            (plen, prefix),
        )
        self.conn.execute(
            "DELETE FROM changes WHERE lower(substr(item_key, 1, ?)) = lower(?)",
            (plen, prefix),
        )
        state_prefix = f"{NODE_ID_STATE_PREFIX}{full_name}#"
        slen = len(state_prefix)
        self.conn.execute(
            "DELETE FROM service_state WHERE lower(substr(key, 1, ?)) = lower(?)",
            (slen, state_prefix),
        )

    def _rewrite_timeline_subjects_in_tx(
        self, old_full_name: str, new_full_name: str
    ) -> None:
        """Rewrites linked-item timeline subjects from `old_full_name#` to `new_full_name#`.

        Only `Issue` and `PullRequest` subjects hold item keys; labels, refs,
        and other subject types that happen to contain `#` are left alone.
        """
        prefix = f"{old_full_name}#"
        plen = len(prefix)
        self.conn.execute(
            """
            UPDATE timeline_events SET subject = ? || substr(subject, ?)
            WHERE subject_type IN ('Issue', 'PullRequest')
              AND lower(substr(subject, 1, ?)) = lower(?)
            """,
            (f"{new_full_name}#", plen + 1, plen, prefix),
        )

    def _rewrite_repo_identity_in_tx(
        self,
        old_repo_id: int,
        old_full_name: str,
        canonical_repo_id: int,
        new_full_name: str,
        now_iso: str,
    ) -> None:
        """Records `old_full_name` in `repo_aliases` and rewrites live state, logs, and timeline subjects."""
        self.conn.execute(
            "DELETE FROM repo_aliases WHERE alias_full_name = ?",
            (new_full_name,),
        )
        if old_repo_id != canonical_repo_id:
            self.conn.execute(
                "UPDATE repo_aliases SET repo_id = ? WHERE repo_id = ?",
                (canonical_repo_id, old_repo_id),
            )
        if old_full_name.lower() != new_full_name.lower():
            self.conn.execute(
                """
                INSERT OR REPLACE INTO repo_aliases (alias_full_name, repo_id, created_at)
                VALUES (?, ?, ?)
                """,
                (old_full_name, canonical_repo_id, now_iso),
            )
        self._rewrite_alias_item_keys_in_tx(
            old_repo_id, old_full_name, canonical_repo_id, new_full_name
        )
        self._rewrite_timeline_subjects_in_tx(old_full_name, new_full_name)

    def merge_repo_alias(
        self,
        alias_repo_id: int,
        canonical_full_name: str,
        now: float | None = None,
    ) -> RepoConfig:
        """Resolves a renamed or redirected repository to `canonical_full_name` in one transaction.

        Creates or finds the canonical repository, records `alias_full_name` in
        `repo_aliases`, rewrites item keys in `watches`, `notifications`,
        `viewer_sets`, `refresh_requests`, and `changes` (and linked-item
        subjects in `timeline_events`), and deletes the alias
        repository row when it is in `items` mode. A `mirror`-mode alias
        repository is kept and logged instead of deleted.
        """
        if "/" not in canonical_full_name:
            raise ValueError(
                f"Invalid repository name '{canonical_full_name}'; expected owner/name."
            )
        owner, name = canonical_full_name.split("/", 1)
        now_iso = iso_at(now)
        with self.conn:
            alias_row = self.conn.execute(
                "SELECT * FROM repos WHERE repo_id = ?", (alias_repo_id,)
            ).fetchone()
            if alias_row is None:
                return self._ensure_items_repo_in_tx(owner, name, now_iso)
            alias_full_name = str(alias_row["full_name"])
            alias_mode = str(alias_row["mode"] or "mirror")
            if alias_full_name.lower() == canonical_full_name.lower():
                same = self.get_repo_by_id(alias_repo_id)
                if same is not None:
                    return same
            canonical = self._ensure_items_repo_in_tx(owner, name, now_iso)
            self._rewrite_repo_identity_in_tx(
                alias_repo_id,
                alias_full_name,
                canonical.repo_id,
                canonical.full_name,
                now_iso,
            )
            if alias_mode == REPO_MODE_ITEMS and alias_repo_id != canonical.repo_id:
                self.conn.execute(
                    "DELETE FROM chunks_fts WHERE rowid IN "
                    "(SELECT rowid FROM chunks WHERE repo_id = ?)",
                    (alias_repo_id,),
                )
                self.conn.execute(
                    "DELETE FROM repos WHERE repo_id = ?", (alias_repo_id,)
                )
                self._bump_vector_cache_seq_in_tx(now)
            elif alias_mode != REPO_MODE_ITEMS:
                LOGGER.warning(
                    "Repository %s is in mirror mode; added alias to %s without deleting mirror repo",
                    alias_full_name,
                    canonical.full_name,
                )
            return canonical

    def _resolve_key(
        self, item_key: str, *, create: bool
    ) -> tuple[int, str, int] | None:
        """Maps an item key to `(repo_id, canonical_key, number)`.

        Args:
            item_key: `owner/name#number`.
            create: Register an unknown repository in `items` mode instead of
                returning None.
        """
        owner, name, number = parse_item_key(item_key)
        row = self._find_repo_row(owner, name)
        if row is None:
            if not create:
                return None
            repo = self.ensure_items_repo(owner, name)
            return repo.repo_id, format_item_key(repo.owner, repo.name, number), number
        return int(row["repo_id"]), f"{row['full_name']}#{number}", number

    def canonical_item_key(self, item_key: str) -> str:
        """Returns `item_key` with the registered repository's casing, or unchanged."""
        resolved = self._resolve_key(item_key, create=False)
        return resolved[1] if resolved else item_key

    def _item_key_for(self, repo_id: int, number: int) -> str:
        """Builds the canonical item key for a repo_id and number."""
        row = self.conn.execute(
            "SELECT full_name FROM repos WHERE repo_id = ?", (repo_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown repo_id {repo_id}")
        return f"{row['full_name']}#{number}"

    # ----- change feed ---------------------------------------------------------------

    def _append_change_in_tx(
        self, item_key: str, kind: str, fields: Sequence[str], now: float | None
    ) -> int:
        """Inserts one change row inside the caller's transaction and returns its seq."""
        cur = self.conn.execute(
            "INSERT INTO changes (item_key, kind, fields, at) VALUES (?, ?, ?, ?)",
            (item_key, kind, json.dumps(list(fields)), iso_at(now)),
        )
        return int(cur.lastrowid or 0)

    def append_change(
        self, item_key: str, kind: str, fields: Sequence[str], now: float | None = None
    ) -> int:
        """Appends a change-feed row and returns its sequence number.

        Args:
            item_key: Item that changed.
            kind: "pr", "checks", "review_requests", "reviews", "threads",
                "notification", or "viewer".
            fields: Names of the fields that changed.
            now: Change time in epoch seconds.
        """
        with self.conn:
            return self._append_change_in_tx(item_key, kind, fields, now)

    def changes_since(self, seq: int, limit: int = 500) -> list[ChangeRecord]:
        """Returns up to `limit` change rows with a sequence number above `seq`, oldest first."""
        rows = self.conn.execute(
            "SELECT seq, item_key, kind, fields, at FROM changes WHERE seq > ? "
            "ORDER BY seq ASC LIMIT ?",
            (seq, limit),
        ).fetchall()
        return [
            ChangeRecord(
                seq=int(r["seq"]),
                item_key=str(r["item_key"]),
                kind=str(r["kind"]),
                fields=tuple(json.loads(r["fields"] or "[]")),
                at=str(r["at"]),
            )
            for r in rows
        ]

    def max_change_seq(self) -> int:
        """Returns the highest sequence number ever assigned (0 when none), even if pruned."""
        row = self.conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'changes'"
        ).fetchone()
        return int(row["seq"]) if row and row["seq"] is not None else 0

    def min_change_seq(self) -> int:
        """Returns the lowest `since` value a reader may pass without missing pruned rows.

        This is one less than the oldest retained row's seq, or
        `max_change_seq()` when the feed is empty. A reader whose `since` is
        below this value missed pruned rows and must take a full snapshot.
        """
        row = self.conn.execute("SELECT MIN(seq) AS s FROM changes").fetchone()
        if row is None or row["s"] is None:
            return self.max_change_seq()
        return int(row["s"]) - 1

    @contextlib.contextmanager
    def read_snapshot(self) -> Iterator[None]:
        """Holds a consistent read snapshot (`BEGIN` .. `COMMIT`) across multiple queries."""
        if self.conn.in_transaction:
            yield
            return
        self.conn.execute("BEGIN")
        try:
            yield
        finally:
            if self.conn.in_transaction:
                self.conn.execute("COMMIT")

    # ----- live PR state -------------------------------------------------------------

    def _pr_live_row(self, repo_id: int, number: int) -> sqlite3.Row | None:
        """Returns the stored live columns and rollup for one PR."""
        cols = ", ".join(f"p.{c}" for c in PR_LIVE_COLUMNS)
        return self.conn.execute(
            f"""
            SELECT {cols}, p.body AS body, ro.state AS rollup_state, ro.total AS rollup_total,
                   ro.head_oid AS rollup_head_oid
            FROM pull_requests p
            LEFT JOIN pr_rollup ro ON ro.repo_id = p.repo_id AND ro.pr_number = p.number
            WHERE p.repo_id = ? AND p.number = ?
            """,
            (repo_id, number),
        ).fetchone()

    @staticmethod
    def _changed_live_fields(old: sqlite3.Row | None, rec: PrLiveRecord) -> list[str]:
        """Lists the live fields whose stored value differs from `rec`."""
        if old is None:
            return [*PR_LIVE_COLUMNS, *_ROLLUP_FIELDS]
        changed = [c for c in PR_LIVE_COLUMNS if old[c] != _db_value(getattr(rec, c))]
        # Without a head SHA no rollup row is written, so there is nothing to
        # compare; reporting it would wake readers on every idle refresh (L5).
        same_head = old["rollup_head_oid"] == rec.head_sha
        for f in _ROLLUP_FIELDS if rec.head_sha else ():
            if not same_head or old[f] != getattr(rec, f):
                changed.append(f)
        if rec.body is not None and old["body"] != rec.body:
            changed.append("body")
        return changed

    def _write_pr_live_in_tx(self, rec: PrLiveRecord, now_iso: str) -> None:
        """Inserts or updates the PR's live columns and rollup inside a transaction."""
        values = [_db_value(getattr(rec, c)) for c in PR_LIVE_COLUMNS]
        cols = ", ".join(PR_LIVE_COLUMNS)
        marks = ", ".join("?" for _ in PR_LIVE_COLUMNS)
        updates = ", ".join(f"{c} = excluded.{c}" for c in PR_LIVE_COLUMNS)
        self.conn.execute(
            f"""
            INSERT INTO pull_requests (repo_id, number, {cols}, live_fetched_at)
            VALUES (?, ?, {marks}, ?)
            ON CONFLICT(repo_id, number) DO UPDATE SET {updates},
              live_fetched_at = excluded.live_fetched_at
            """,
            (rec.repo_id, rec.number, *values, now_iso),
        )
        if rec.body is not None:
            self.conn.execute(
                "UPDATE pull_requests SET body = ? WHERE repo_id = ? AND number = ?",
                (rec.body, rec.repo_id, rec.number),
            )
        if rec.head_sha:
            self.conn.execute(
                """
                INSERT INTO pr_rollup (repo_id, pr_number, head_oid, state, total, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo_id, pr_number) DO UPDATE SET
                  checks_etag = CASE WHEN pr_rollup.head_oid = excluded.head_oid
                                     THEN pr_rollup.checks_etag END,
                  status_etag = CASE WHEN pr_rollup.head_oid = excluded.head_oid
                                     THEN pr_rollup.status_etag END,
                  head_oid = excluded.head_oid, state = excluded.state,
                  total = excluded.total, fetched_at = excluded.fetched_at
                """,
                (
                    rec.repo_id,
                    rec.number,
                    rec.head_sha,
                    rec.rollup_state,
                    rec.rollup_total,
                    now_iso,
                ),
            )

    def _note_mirror_pr_change_in_tx(self, pr: Any) -> sqlite3.Row | None:
        """Appends a "pr" change row when a mirror upsert changes a live PR's fields.

        Called by `PackratStore._upsert_single_pr` before it writes `pr` (a
        `PrRecord`). Only PRs that live refreshes track (`live_fetched_at` set)
        get change rows, so readers learn about changes the mirror saw first
        (M2). Fields the mirror leaves unset (None) are not compared.
        """
        cols = ", ".join(MIRROR_LIVE_FIELDS)
        old = self.conn.execute(
            f"SELECT live_fetched_at, {cols} FROM pull_requests WHERE repo_id = ? AND number = ?",
            (pr.repo_id, pr.number),
        ).fetchone()
        if old is None or old["live_fetched_at"] is None:
            return old
        if str(old["updated_at"] or "") > pr.updated_at:
            return old
        changed = [
            f
            for f in MIRROR_LIVE_FIELDS
            if getattr(pr, f) is not None and old[f] != _db_value(getattr(pr, f))
        ]
        if changed:
            key = self._item_key_for(pr.repo_id, pr.number)
            self._append_change_in_tx(key, "pr", changed, None)
        return old

    def upsert_pr_live(
        self, record: PrLiveRecord, now: float | None = None
    ) -> list[str]:
        """Stores a live PR refresh and returns the names of the fields that changed.

        Creates the `pull_requests` row when it is missing; otherwise updates
        only the live columns. Always stamps `live_fetched_at`. Appends a "pr"
        change row only when at least one field changed.
        """
        changed = self._changed_live_fields(
            self._pr_live_row(record.repo_id, record.number), record
        )
        with self.conn:
            self._write_pr_live_in_tx(record, iso_at(now))
            if changed:
                key = self._item_key_for(record.repo_id, record.number)
                self._append_change_in_tx(key, "pr", changed, now)
        return changed

    def _replace_pr_rows(
        self,
        repo_id: int,
        pr_number: int,
        spec: tuple[str, str, Sequence[str]],
        rows: Sequence[tuple[Any, ...]],
        now: float | None,
    ) -> bool:
        """Replaces a PR's rows in a child table when they differ, appending one change row.

        Args:
            spec: `(table, change_kind, columns)`; `columns` exclude repo_id and
                pr_number.
            rows: New rows as tuples in `columns` order.

        Returns:
            True when the stored rows changed.
        """
        table, kind, columns = spec
        col_sql = ", ".join(columns)
        old = {
            tuple(r)
            for r in self.conn.execute(
                f"SELECT {col_sql} FROM {table} WHERE repo_id = ? AND pr_number = ?",
                (repo_id, pr_number),
            )
        }
        new = {tuple(_db_value(v) for v in row) for row in rows}
        if old == new:
            return False
        marks = ", ".join("?" for _ in columns)
        with self.conn:
            self.conn.execute(
                f"DELETE FROM {table} WHERE repo_id = ? AND pr_number = ?",
                (repo_id, pr_number),
            )
            self.conn.executemany(
                f"INSERT OR REPLACE INTO {table} (repo_id, pr_number, {col_sql}) "
                f"VALUES (?, ?, {marks})",
                [(repo_id, pr_number, *row) for row in sorted(new, key=repr)],
            )
            key = self._item_key_for(repo_id, pr_number)
            self._append_change_in_tx(key, kind, [table], now)
        return True

    def replace_check_runs(
        self,
        repo_id: int,
        pr_number: int,
        head_oid: str,
        runs: Sequence[CheckRunRecord],
        now: float | None = None,
    ) -> bool:
        """Replaces a PR's check runs with `runs` for `head_oid`, dropping other heads' rows.

        Returns:
            True (and appends a "checks" change) when the stored set changed.
        """
        columns = (
            "head_oid",
            "run_key",
            "name",
            "kind",
            "status",
            "conclusion",
            "details_url",
            "started_at",
            "completed_at",
            "is_required",
            "workflow_run_id",
        )
        deduped = {r.key: r for r in runs}
        rows = [
            (
                head_oid,
                r.key,
                r.name,
                r.kind,
                r.status,
                r.conclusion,
                r.details_url,
                r.started_at,
                r.completed_at,
                r.is_required,
                r.workflow_run_id,
            )
            for r in deduped.values()
        ]
        return self._replace_pr_rows(
            repo_id, pr_number, ("check_runs", "checks", columns), rows, now
        )

    def replace_review_requests(
        self,
        repo_id: int,
        pr_number: int,
        requests: Sequence[ReviewRequestRecord],
        now: float | None = None,
    ) -> bool:
        """Replaces a PR's pending review requests; True (plus a change row) when changed."""
        deduped = {r.reviewer: r for r in requests}
        rows = [(r.reviewer, r.reviewer_kind, r.requested_at) for r in deduped.values()]
        spec = (
            "review_requests",
            "review_requests",
            ("reviewer", "reviewer_kind", "requested_at"),
        )
        return self._replace_pr_rows(repo_id, pr_number, spec, rows, now)

    def replace_latest_opinionated_reviews(
        self,
        repo_id: int,
        pr_number: int,
        reviews: Sequence[OpinionatedReviewRecord],
        now: float | None = None,
    ) -> bool:
        """Replaces a PR's latest review per author; True (plus a change row) when changed."""
        deduped = {r.author: r for r in reviews}
        rows = [
            (r.author, r.author_type, r.state, r.submitted_at) for r in deduped.values()
        ]
        spec = (
            "latest_opinionated_reviews",
            "reviews",
            ("author", "author_type", "state", "submitted_at"),
        )
        return self._replace_pr_rows(repo_id, pr_number, spec, rows, now)

    def replace_latest_reviews(
        self,
        repo_id: int,
        pr_number: int,
        reviews: Sequence[LatestReviewRecord],
        now: float | None = None,
    ) -> bool:
        """Replaces a PR's latest non-pending review per author; True when changed."""
        deduped = {r.author: r for r in reviews}
        rows = [
            (r.author, r.author_type, r.state, r.submitted_at, r.commit_oid)
            for r in deduped.values()
        ]
        spec = (
            "latest_reviews",
            "reviews",
            ("author", "author_type", "state", "submitted_at", "commit_oid"),
        )
        return self._replace_pr_rows(repo_id, pr_number, spec, rows, now)

    def replace_thread_activity(
        self,
        repo_id: int,
        pr_number: int,
        threads: Sequence[ThreadActivityRecord],
        now: float | None = None,
    ) -> bool:
        """Replaces a PR's review-thread activity; True (plus a change row) when changed."""
        deduped = {t.thread_id: t for t in threads}
        rows = [
            (
                t.thread_id,
                t.is_resolved,
                t.is_outdated,
                t.last_author,
                t.last_author_type,
                t.last_at,
                json.dumps(list(t.participants)),
            )
            for t in deduped.values()
        ]
        spec = (
            "thread_activity",
            "threads",
            (
                "thread_id",
                "is_resolved",
                "is_outdated",
                "last_author",
                "last_author_type",
                "last_at",
                "participants_json",
            ),
        )
        return self._replace_pr_rows(repo_id, pr_number, spec, rows, now)

    upsert_thread_activity = replace_thread_activity

    def upsert_item_comments(
        self,
        repo_id: int,
        parent_kind: str,
        number: int,
        comments: Sequence[ItemCommentRecord],
        now: float | None = None,
    ) -> bool:
        """Stores an item's latest conversation comments in `comments`.

        New comments are inserted with an empty body (the mirror fills bodies
        for `mirror` repositories); existing rows only get their
        `author_type` updated, so mirrored bodies are never blanked.

        Args:
            parent_kind: "pr" or "issue".

        Returns:
            True (and appends a "comments" change row) when a new comment appeared.
        """
        if not comments:
            return False
        ids = [c.comment_id for c in comments]
        marks = ", ".join("?" for _ in ids)
        # Rows the mirror stored (author_type NULL) haven't been seen by a live
        # refresh yet, so they still count as new for the change feed (M2).
        existing_rows = self.conn.execute(
            f"SELECT comment_id, author_type FROM comments WHERE repo_id = ? AND comment_id IN ({marks})",
            (repo_id, *ids),
        ).fetchall()
        existing_types = {str(r["comment_id"]): r["author_type"] for r in existing_rows}
        existing_seen = {
            cid for cid, atype in existing_types.items() if atype is not None
        }
        added = set(ids) - existing_seen
        needs_write = bool(added) or any(
            existing_types.get(c.comment_id) != c.author_type for c in comments
        )
        if not needs_write:
            return False
        rows = [
            (
                repo_id,
                c.comment_id,
                parent_kind,
                number,
                c.author,
                c.created_at,
                c.author_type,
            )
            for c in comments
        ]
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO comments (repo_id, comment_id, parent_kind, parent_number, author,
                  body, created_at, updated_at, author_type)
                VALUES (?1, ?2, ?3, ?4, ?5, '', ?6, ?6, ?7)
                ON CONFLICT(repo_id, comment_id) DO UPDATE SET author_type = excluded.author_type
                """,
                rows,
            )
            if added:
                key = self._item_key_for(repo_id, number)
                self._append_change_in_tx(key, "comments", ["comments"], now)
        return bool(added)

    # ----- notifications and viewer sets ---------------------------------------------

    def upsert_notifications(
        self, records: Sequence[NotificationRecord], now: float | None = None
    ) -> list[str]:
        """Stores notification threads and returns item keys whose notification is new or updated.

        Each new or updated notification with an item key also appends a
        "notification" change row.
        """
        if not records:
            return []
        now_iso = iso_at(now)
        tids = [rec.thread_id for rec in records]
        marks = ", ".join("?" for _ in tids)
        existing = {
            str(r["thread_id"]): (r["updated_at"], int(r["unread"]))
            for r in self.conn.execute(
                f"SELECT thread_id, updated_at, unread FROM notifications WHERE thread_id IN ({marks})",
                tids,
            ).fetchall()
        }
        changed: list[str] = []
        rows: list[tuple[Any, ...]] = []
        for rec in records:
            state = (rec.updated_at, int(rec.unread))
            is_new = existing.get(rec.thread_id) != state
            existing[rec.thread_id] = state
            rows.append(
                (
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
        with self.conn:
            self.conn.executemany(
                """
                INSERT OR REPLACE INTO notifications
                  (thread_id, reason, subject_type, item_key, updated_at, unread, seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            for key in changed:
                self._append_change_in_tx(key, "notification", ["reason"], now)
        return changed

    def replace_viewer_set(
        self, set_name: str, item_keys: Iterable[str], now: float | None = None
    ) -> tuple[list[str], list[str]]:
        """Replaces the members of a viewer set (such as "authored").

        Members that stay keep their `first_seen_at`; members that left are
        deleted. The refresh time is also stored in `service_state` under
        `viewer_set:<set_name>`.

        Returns:
            `(added, removed)` item keys; each appends a "viewer" change row.
        """
        now_iso = iso_at(now)
        resolved = [self._resolve_key(k, create=True) for k in item_keys]
        current_map = {r[1]: (r[0], r[2]) for r in resolved if r is not None}
        prev_rows = self.conn.execute(
            """
            SELECT r.full_name || '#' || v.item_number AS item_key, v.repo_id, v.item_number
            FROM viewer_sets v
            JOIN repos r ON r.repo_id = v.repo_id
            WHERE v.set_name = ?
            """,
            (set_name,),
        ).fetchall()
        previous_map = {
            str(r["item_key"]): (int(r["repo_id"]), int(r["item_number"]))
            for r in prev_rows
        }
        added = sorted(set(current_map) - set(previous_map))
        removed = sorted(set(previous_map) - set(current_map))
        with self.conn:
            if removed:
                self.conn.executemany(
                    "DELETE FROM viewer_sets WHERE set_name = ? AND repo_id = ? AND item_number = ?",
                    [(set_name, *previous_map[key]) for key in removed],
                )
            if current_map:
                self.conn.executemany(
                    """
                    INSERT INTO viewer_sets (
                      set_name, repo_id, item_number, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(set_name, repo_id, item_number) DO UPDATE SET
                      last_seen_at = excluded.last_seen_at
                    """,
                    [
                        (set_name, *current_map[key], now_iso, now_iso)
                        for key in sorted(current_map)
                    ],
                )
            self._set_state_in_tx(
                f"{VIEWER_SET_STATE_PREFIX}{set_name}", now_iso, now_iso
            )
            for key in [*added, *removed]:
                self._append_change_in_tx(key, "viewer", [set_name], now)
        return added, removed

    # ----- watches and refresh requests ----------------------------------------------

    def add_watch(
        self,
        consumer: str,
        consumer_id: str,
        item_keys: Iterable[str],
        freshness_s: int,
        ttl_s: int,
        now: float | None = None,
    ) -> list[str]:
        """Leases items for a consumer, registering unknown repositories in `items` mode.

        Returns:
            The canonical item keys that were leased.
        """
        moment = time.time() if now is None else now
        expires = iso_at(moment + ttl_s)
        resolved = [
            r for k in item_keys if (r := self._resolve_key(k, create=True)) is not None
        ]
        canonical = [r[1] for r in resolved]
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO watches (
                  consumer, consumer_id, repo_id, item_number, freshness_s, lease_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(consumer, consumer_id, repo_id, item_number) DO UPDATE SET
                  freshness_s = excluded.freshness_s, lease_expires_at = excluded.lease_expires_at
                """,
                [
                    (consumer, consumer_id, r[0], r[2], int(freshness_s), expires)
                    for r in resolved
                ],
            )
        return canonical

    def _watch_scope(
        self, consumer: str, consumer_id: str, item_keys: Iterable[str] | None
    ) -> tuple[str, list[Any]]:
        """Builds a WHERE clause selecting a consumer's leases, optionally limited to keys."""
        where = "consumer = ? AND consumer_id = ?"
        params: list[Any] = [consumer, consumer_id]
        if item_keys is not None:
            pairs = [
                (r[0], r[2])
                for k in item_keys
                if (r := self._resolve_key(k, create=False)) is not None
            ]
            if not pairs:
                return "1 = 0", []
            values_sql = ", ".join("(?, ?)" for _ in pairs)
            where += f" AND (repo_id, item_number) IN (VALUES {values_sql})"
            for repo_id, number in pairs:
                params.extend((repo_id, number))
        return where, params

    def renew_watch(
        self,
        consumer: str,
        consumer_id: str,
        ttl_s: int,
        item_keys: Iterable[str] | None = None,
        now: float | None = None,
    ) -> int:
        """Extends a consumer's leases (all of them, or only `item_keys`); returns rows renewed."""
        moment = time.time() if now is None else now
        where, params = self._watch_scope(consumer, consumer_id, item_keys)
        with self.conn:
            cur = self.conn.execute(
                f"UPDATE watches SET lease_expires_at = ? WHERE {where}",
                [iso_at(moment + ttl_s), *params],
            )
        return cur.rowcount

    def remove_watch(
        self, consumer: str, consumer_id: str, item_keys: Iterable[str] | None = None
    ) -> int:
        """Drops a consumer's leases (all of them, or only `item_keys`); returns rows removed."""
        where, params = self._watch_scope(consumer, consumer_id, item_keys)
        with self.conn:
            cur = self.conn.execute(f"DELETE FROM watches WHERE {where}", params)
        return cur.rowcount

    def active_watches(self, now: float | None = None) -> list[WatchLease]:
        """Returns leases that have not expired, ordered by item key."""
        rows = self.conn.execute(
            """
            SELECT w.consumer, w.consumer_id, r.full_name || '#' || w.item_number AS item_key,
                   w.freshness_s, w.lease_expires_at
            FROM watches w
            JOIN repos r ON r.repo_id = w.repo_id
            WHERE w.lease_expires_at > ?
            ORDER BY item_key, w.consumer
            """,
            (iso_at(now),),
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
        """Deletes expired leases and returns how many were removed."""
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM watches WHERE lease_expires_at <= ?", (iso_at(now),)
            )
        return cur.rowcount

    def request_refresh(
        self, item_keys: Iterable[str], now: float | None = None
    ) -> list[str]:
        """Queues items for an immediate refresh by the service; returns the canonical keys."""
        resolved = [
            r for k in item_keys if (r := self._resolve_key(k, create=True)) is not None
        ]
        now_iso = iso_at(now)
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO refresh_requests (repo_id, item_number, requested_at) "
                "VALUES (?, ?, ?)",
                [(r[0], r[2], now_iso) for r in resolved],
            )
        return [r[1] for r in resolved]

    def peek_refresh_requests(self) -> list[str]:
        """Returns every queued refresh request without removing it, oldest first."""
        rows = self.conn.execute(
            """
            SELECT r.full_name || '#' || rr.item_number AS item_key
            FROM refresh_requests rr
            JOIN repos r ON r.repo_id = rr.repo_id
            ORDER BY rr.requested_at, item_key
            """
        ).fetchall()
        return [str(r["item_key"]) for r in rows]

    def take_refresh_requests(self) -> list[str]:
        """Atomically removes and returns every queued refresh request, oldest first."""
        if (
            self.conn.execute("SELECT 1 FROM refresh_requests LIMIT 1").fetchone()
            is None
        ):
            return []
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self.conn.execute(
                """
                SELECT rr.repo_id, rr.item_number,
                       r.full_name || '#' || rr.item_number AS item_key
                FROM refresh_requests rr
                JOIN repos r ON r.repo_id = rr.repo_id
                ORDER BY rr.requested_at, item_key
                """
            ).fetchall()
            if rows:
                self.conn.executemany(
                    "DELETE FROM refresh_requests WHERE repo_id = ? AND item_number = ?",
                    [(int(r["repo_id"]), int(r["item_number"])) for r in rows],
                )
            self.conn.execute("COMMIT")
            return [str(r["item_key"]) for r in rows]
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    # ----- api calls, heartbeat, service state ---------------------------------------

    def record_api_call(
        self,
        *,
        worker: str,
        resource: str,
        cost: int | None,
        remaining: int | None,
        reset_at: int | None,
        status: int,
        now: float | None = None,
    ) -> None:
        """Appends one row to the `api_calls` audit log."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO api_calls (at, worker, resource, cost, remaining, reset_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (iso_at(now), worker, resource, cost, remaining, reset_at, status),
            )

    def write_heartbeat(
        self,
        pid: int,
        version: str,
        now: float | None = None,
        *,
        last_ok_at: str | None = None,
        consecutive_failed_ticks: int | None = None,
    ) -> None:
        """Updates the service heartbeat, resetting `started_at` when the PID changes."""
        now_iso = iso_at(now)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO service_heartbeat (
                  id, pid, started_at, beat_at, version, last_ok_at, consecutive_failed_ticks
                ) VALUES (1, ?, ?, ?, ?, ?, COALESCE(?, 0))
                ON CONFLICT(id) DO UPDATE SET
                  started_at = CASE WHEN service_heartbeat.pid = excluded.pid
                                    THEN service_heartbeat.started_at
                                    ELSE excluded.started_at END,
                  last_ok_at = COALESCE(?, service_heartbeat.last_ok_at),
                  consecutive_failed_ticks = CASE
                    WHEN ? IS NOT NULL THEN ?
                    WHEN service_heartbeat.pid = excluded.pid
                      THEN service_heartbeat.consecutive_failed_ticks
                    ELSE 0
                  END,
                  pid = excluded.pid,
                  beat_at = excluded.beat_at,
                  version = excluded.version
                """,
                (
                    pid,
                    now_iso,
                    now_iso,
                    version,
                    last_ok_at,
                    consecutive_failed_ticks,
                    last_ok_at,
                    consecutive_failed_ticks,
                    consecutive_failed_ticks,
                ),
            )

    def read_heartbeat(self) -> ServiceHeartbeat | None:
        """Returns the service heartbeat, or None if the service never ran."""
        row = self.conn.execute(
            "SELECT * FROM service_heartbeat WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return ServiceHeartbeat(
            pid=row["pid"],
            started_at=row["started_at"],
            beat_at=row["beat_at"],
            version=row["version"],
            last_ok_at=row["last_ok_at"],
            consecutive_failed_ticks=int(row["consecutive_failed_ticks"] or 0),
        )

    def read_schema_and_views_version(self) -> tuple[int, int]:
        """Returns `(schema_version, views_version)` currently recorded in the database."""
        return read_schema_version(self.conn), read_stored_views_version(self.conn)

    def wal_checkpoint_truncate(self) -> tuple[int, int, int]:
        """Runs `PRAGMA wal_checkpoint(TRUNCATE)` and returns `(busy, log, checkpointed)`."""
        row = self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row is None:
            return 0, 0, 0
        return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)

    def get_gap_state(self, repo_id: int, stream: str) -> tuple[str | None, str | None]:
        """Returns `(cursor, floor_watermark)` for an interrupted incremental GitHub walk."""
        cursor = self.get_state(f"gap_cursor:{repo_id}:{stream}")
        floor = self.get_state(f"gap_floor:{repo_id}:{stream}")
        return cursor, floor

    def set_gap_state(
        self,
        repo_id: int,
        stream: str,
        *,
        cursor: str | None,
        floor: str | None,
        now: float | None = None,
    ) -> None:
        """Persists or clears `(cursor, floor_watermark)` for an interrupted incremental walk."""
        cursor_key = f"gap_cursor:{repo_id}:{stream}"
        floor_key = f"gap_floor:{repo_id}:{stream}"
        now_iso = iso_at(now)
        with self.conn:
            if cursor is None:
                self.conn.execute(
                    "DELETE FROM service_state WHERE key IN (?, ?)",
                    (cursor_key, floor_key),
                )
            else:
                self._set_state_in_tx(cursor_key, cursor, now_iso)
                if floor is None:
                    self.conn.execute(
                        "DELETE FROM service_state WHERE key = ?", (floor_key,)
                    )
                else:
                    self._set_state_in_tx(floor_key, floor, now_iso)

    def _set_state_in_tx(self, key: str, value: str | None, now_iso: str) -> None:
        """Writes one `service_state` row inside the caller's transaction."""
        self.conn.execute(
            "INSERT OR REPLACE INTO service_state (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, now_iso),
        )

    def get_state(self, key: str) -> str | None:
        """Returns a `service_state` value, or None when unset."""
        row = self.conn.execute(
            "SELECT value FROM service_state WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None or row["value"] is None else str(row["value"])

    def set_state(self, key: str, value: str | None, now: float | None = None) -> None:
        """Sets a `service_state` value; None deletes the key."""
        with self.conn:
            if value is None:
                self.conn.execute("DELETE FROM service_state WHERE key = ?", (key,))
            else:
                self._set_state_in_tx(key, value, iso_at(now))

    # ----- node ids and rollup ETags -------------------------------------------------

    def _resolve_keys_batch(
        self, item_keys: Iterable[str]
    ) -> list[tuple[str, str, tuple[int, int] | None]]:
        """Resolves a batch of item keys without creating repos, caching repo lookups per batch."""
        repo_cache: dict[tuple[str, str], sqlite3.Row | None] = {}
        out: list[tuple[str, str, tuple[int, int] | None]] = []
        for key in item_keys:
            owner, name, number = parse_item_key(key)
            cache_key = (owner.lower(), name.lower())
            if cache_key not in repo_cache:
                repo_cache[cache_key] = self._find_repo_row(owner, name)
            row = repo_cache[cache_key]
            if row is None:
                out.append((key, key, None))
            else:
                out.append(
                    (key, f"{row['full_name']}#{number}", (int(row["repo_id"]), number))
                )
        return out

    def _query_pr_node_ids(
        self, pairs: Sequence[tuple[int, int]]
    ) -> dict[tuple[int, int] | None, str]:
        """Batch-loads known `pull_requests.node_id` values for `(repo_id, number)` pairs."""
        unique_pairs = list(dict.fromkeys(pairs))
        if not unique_pairs:
            return {}
        values_sql = ", ".join("(?, ?)" for _ in unique_pairs)
        params = [v for pair in unique_pairs for v in pair]
        rows = self.conn.execute(
            f"""
            SELECT repo_id, number, node_id FROM pull_requests
            WHERE node_id IS NOT NULL AND (repo_id, number) IN (VALUES {values_sql})
            """,
            params,
        ).fetchall()
        return {
            (int(r["repo_id"]), int(r["number"])): str(r["node_id"])
            for r in rows
            if r["node_id"]
        }

    def _query_state_node_ids(self, state_keys: Sequence[str]) -> dict[str, str]:
        """Batch-loads fallback `node_id:<item_key>` entries from `service_state`."""
        unique_keys = list(dict.fromkeys(state_keys))
        if not unique_keys:
            return {}
        marks = ", ".join("?" for _ in unique_keys)
        rows = self.conn.execute(
            f"SELECT key, value FROM service_state WHERE value IS NOT NULL AND key IN ({marks})",
            unique_keys,
        ).fetchall()
        return {str(r["key"]): str(r["value"]) for r in rows if r["value"]}

    def get_node_ids(self, item_keys: Iterable[str]) -> dict[str, str]:
        """Returns known GraphQL node ids keyed by the item keys passed in."""
        batch = self._resolve_keys_batch(item_keys)
        if not batch:
            return {}
        pr_nodes = self._query_pr_node_ids(
            [pair for _, _, pair in batch if pair is not None]
        )
        state_nodes = self._query_state_node_ids(
            [
                f"{NODE_ID_STATE_PREFIX}{canon}"
                for _, canon, pair in batch
                if pair not in pr_nodes
            ]
        )
        out: dict[str, str] = {}
        for key, canon, pair in batch:
            node_id = pr_nodes.get(pair) or state_nodes.get(
                f"{NODE_ID_STATE_PREFIX}{canon}"
            )
            if node_id:
                out[key] = node_id
        return out

    def set_node_id(self, item_key: str, node_id: str) -> None:
        """Remembers a PR's GraphQL node id, even before its `pull_requests` row exists."""
        resolved = self._resolve_key(item_key, create=False)
        if resolved is not None:
            with self.conn:
                cur = self.conn.execute(
                    "UPDATE pull_requests SET node_id = ? WHERE repo_id = ? AND number = ?",
                    (node_id, resolved[0], resolved[2]),
                )
            if cur.rowcount:
                return
        self.set_state(
            f"{NODE_ID_STATE_PREFIX}{self.canonical_item_key(item_key)}", node_id
        )

    def get_live_fetched_at(self, item_keys: Iterable[str]) -> dict[str, float]:
        """Returns `live_fetched_at` (epoch seconds) for the PRs in `item_keys` that have one.

        Keys are canonicalized against registered repository casing before matching.
        """
        key_by_pair = {
            pair: canon
            for _, canon, pair in self._resolve_keys_batch(item_keys)
            if pair is not None
        }
        if not key_by_pair:
            return {}
        pairs = list(key_by_pair)
        values_sql = ", ".join("(?, ?)" for _ in pairs)
        params = [v for pair in pairs for v in pair]
        rows = self.conn.execute(
            f"""
            SELECT p.repo_id, p.number, p.live_fetched_at
            FROM pull_requests p
            WHERE p.live_fetched_at IS NOT NULL
              AND (p.repo_id, p.number) IN (VALUES {values_sql})
            """,
            params,
        ).fetchall()
        return {
            key_by_pair[(int(r["repo_id"]), int(r["number"]))]: datetime.fromisoformat(
                str(r["live_fetched_at"])
            ).timestamp()
            for r in rows
            if (int(r["repo_id"]), int(r["number"])) in key_by_pair
        }

    def get_live_pr_fingerprints(
        self, item_keys: Iterable[str]
    ) -> dict[str, tuple[str | None, str | None, str | None, int | None]]:
        """Returns `{canonical_key: (updated_at, head_sha, rollup_state, rollup_total)}` for live PRs."""
        resolved = [
            r
            for k in item_keys
            if (r := self._resolve_key(k, create=False)) is not None
        ]
        if not resolved:
            return {}
        pairs = list(dict.fromkeys((r[0], r[2]) for r in resolved))
        key_by_pair = {(r[0], r[2]): r[1] for r in resolved}
        values_sql = ", ".join("(?, ?)" for _ in pairs)
        params: list[Any] = []
        for repo_id, number in pairs:
            params.extend((repo_id, number))
        rows = self.conn.execute(
            f"""
            SELECT p.repo_id, p.number, p.updated_at, p.head_sha,
                   ro.state AS rollup_state, ro.total AS rollup_total
            FROM pull_requests p
            LEFT JOIN pr_rollup ro
              ON ro.repo_id = p.repo_id AND ro.pr_number = p.number AND ro.head_oid = p.head_sha
            WHERE p.live_fetched_at IS NOT NULL
              AND (p.repo_id, p.number) IN (VALUES {values_sql})
            """,
            params,
        ).fetchall()
        out: dict[str, tuple[str | None, str | None, str | None, int | None]] = {}
        for row in rows:
            pair = (int(row["repo_id"]), int(row["number"]))
            key = key_by_pair.get(pair)
            if key is not None:
                out[key] = (
                    row["updated_at"],
                    row["head_sha"],
                    row["rollup_state"],
                    int(row["rollup_total"])
                    if row["rollup_total"] is not None
                    else None,
                )
        return out

    def touch_live_fetched_at(
        self, item_keys: Iterable[str], now: float | None = None
    ) -> None:
        """Stamps `live_fetched_at` (and `pr_rollup.fetched_at`) for unchanged hot refreshes."""
        pairs = list(
            dict.fromkeys(
                (r[0], r[2])
                for k in item_keys
                if (r := self._resolve_key(k, create=False)) is not None
            )
        )
        if not pairs:
            return
        now_iso = iso_at(now)
        with self.conn:
            self.conn.executemany(
                "UPDATE pull_requests SET live_fetched_at = ? WHERE repo_id = ? AND number = ?",
                [(now_iso, repo_id, number) for repo_id, number in pairs],
            )
            self.conn.executemany(
                "UPDATE pr_rollup SET fetched_at = ? WHERE repo_id = ? AND pr_number = ?",
                [(now_iso, repo_id, number) for repo_id, number in pairs],
            )

    def get_pr_head_sha(self, item_key: str) -> str | None:
        """Returns the stored `pull_requests.head_sha` for a PR, or None if unknown."""
        resolved = self._resolve_key(item_key, create=False)
        if resolved is None:
            return None
        row = self.conn.execute(
            "SELECT head_sha FROM pull_requests WHERE repo_id = ? AND number = ?",
            (resolved[0], resolved[2]),
        ).fetchone()
        return str(row["head_sha"]) if row and row["head_sha"] else None

    def get_rollup_etags(
        self, item_key: str
    ) -> tuple[str | None, str | None, str | None]:
        """Returns `(head_oid, checks_etag, status_etag)` for a PR, or Nones if unknown."""
        resolved = self._resolve_key(item_key, create=False)
        if resolved is None:
            return None, None, None
        row = self.conn.execute(
            "SELECT head_oid, checks_etag, status_etag FROM pr_rollup "
            "WHERE repo_id = ? AND pr_number = ?",
            (resolved[0], resolved[2]),
        ).fetchone()
        if row is None:
            return None, None, None
        return row["head_oid"], row["checks_etag"], row["status_etag"]

    def set_rollup_etags(
        self,
        item_key: str,
        head_oid: str,
        checks_etag: str | None,
        status_etag: str | None,
        now: float | None = None,
    ) -> None:
        """Stores the check-runs and status ETags for a PR's head commit.

        A different `head_oid` than the stored one replaces the rollup row and
        clears its state, since the old state described another commit.
        """
        resolved = self._resolve_key(item_key, create=True)
        if resolved is None:  # pragma: no cover - create=True always resolves
            raise ValueError(f"Cannot resolve item key {item_key}")
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO pr_rollup
                  (repo_id, pr_number, head_oid, checks_etag, status_etag, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo_id, pr_number) DO UPDATE SET
                  state = CASE WHEN pr_rollup.head_oid = excluded.head_oid
                               THEN pr_rollup.state END,
                  total = CASE WHEN pr_rollup.head_oid = excluded.head_oid
                               THEN pr_rollup.total END,
                  head_oid = excluded.head_oid, checks_etag = excluded.checks_etag,
                  status_etag = excluded.status_etag, fetched_at = excluded.fetched_at
                """,
                (
                    resolved[0],
                    resolved[2],
                    head_oid,
                    checks_etag,
                    status_etag,
                    iso_at(now),
                ),
            )

    # ----- retention -----------------------------------------------------------------

    def _prune_idle_items_repos_in_tx(self, now_iso: str, cutoff_iso: str) -> int:
        """Deletes idle `items` repositories older than `cutoff_iso` inside a transaction."""
        rows = self.conn.execute(
            """
            SELECT r.repo_id, r.full_name FROM repos r
            WHERE r.mode = ?
              AND r.created_at < ?
              AND NOT EXISTS (
                SELECT 1 FROM watches w
                WHERE w.repo_id = r.repo_id AND w.lease_expires_at > ?
              )
              AND NOT EXISTS (
                SELECT 1 FROM notifications n
                WHERE n.item_key IS NOT NULL
                  AND n.seen_at >= ?
                  AND lower(substr(n.item_key, 1, length(r.full_name) + 1))
                      = lower(r.full_name || '#')
              )
              AND NOT EXISTS (
                SELECT 1 FROM viewer_sets v
                WHERE v.repo_id = r.repo_id AND v.last_seen_at >= ?
              )
              AND NOT EXISTS (
                SELECT 1 FROM changes c
                WHERE c.at >= ?
                  AND lower(substr(c.item_key, 1, length(r.full_name) + 1))
                      = lower(r.full_name || '#')
              )
            """,
            (REPO_MODE_ITEMS, cutoff_iso, now_iso, cutoff_iso, cutoff_iso, cutoff_iso),
        ).fetchall()
        repo_ids = [int(r["repo_id"]) for r in rows]
        if not repo_ids:
            return 0
        for row in rows:
            self._delete_repo_live_logs_in_tx(str(row["full_name"]))
        marks = ", ".join("?" for _ in repo_ids)
        fts_cur = self.conn.execute(
            f"""
            DELETE FROM chunks_fts
            WHERE rowid IN (SELECT rowid FROM chunks WHERE repo_id IN ({marks}))
            """,
            repo_ids,
        )
        cur = self.conn.execute(
            f"DELETE FROM repos WHERE repo_id IN ({marks})", repo_ids
        )
        if fts_cur.rowcount:
            self._bump_vector_cache_seq_in_tx()
        return cur.rowcount

    def prune(
        self,
        now: float | None = None,
        keep_changes_s: int = 7 * DAY_S,
        keep_api_calls_s: int = 7 * DAY_S,
        keep_notifications_s: int = 30 * DAY_S,
        keep_viewer_s: int = 30 * DAY_S,
        keep_idle_items_repo_s: int = 30 * DAY_S,
        keep_timeline_dirty_s: int = 7 * DAY_S,
    ) -> dict[str, int]:
        """Deletes old change, api_calls, notification, viewer-set, expired lease, and idle `items` repo rows.

        Viewer-set rows are removed when their set has not been refreshed for
        `keep_viewer_s` (for example a set the service stopped tracking). Idle
        `items` repositories older than `keep_idle_items_repo_s` with no active
        watch or recent notification or viewer-set row are removed along with
        their FTS chunks and cascaded child/alias rows. `timeline_dirty` rows
        older than `keep_timeline_dirty_s` (targets that were never stored) are
        removed too, as are superseded `check_runs` rows whose `head_oid`
        differs from the stored PR's `head_sha`.

        Returns:
            Rows deleted per table.
        """
        moment = time.time() if now is None else now
        plan = (
            ("changes", "at", keep_changes_s),
            ("api_calls", "at", keep_api_calls_s),
            ("notifications", "seen_at", keep_notifications_s),
            ("viewer_sets", "last_seen_at", keep_viewer_s),
            ("timeline_dirty", "queued_at", keep_timeline_dirty_s),
        )
        deleted: dict[str, int] = {}
        with self.conn:
            for table, column, keep_s in plan:
                cur = self.conn.execute(
                    f"DELETE FROM {table} WHERE {column} < ?",
                    (iso_at(moment - keep_s),),
                )
                deleted[table] = cur.rowcount
            self.conn.execute(
                """
                DELETE FROM check_runs
                WHERE EXISTS (
                  SELECT 1 FROM pull_requests p
                  WHERE p.repo_id = check_runs.repo_id
                    AND p.number = check_runs.pr_number
                    AND p.head_sha IS NOT NULL
                    AND p.head_sha <> check_runs.head_oid
                )
                """
            )
        deleted["watches"] = self.prune_expired_watches(moment)
        with self.conn:
            deleted["items_repos"] = self._prune_idle_items_repos_in_tx(
                iso_at(moment), iso_at(moment - keep_idle_items_repo_s)
            )
        return deleted


def read_heartbeat_version(db_path: Path) -> str | None:
    """Reads `service_heartbeat.version` from `db_path` without migrating or creating the DB."""
    if not db_path.is_file():
        return None
    try:
        with contextlib.closing(sqlite3.connect(db_path, timeout=2.0)) as conn:
            row = conn.execute(
                "SELECT version FROM service_heartbeat WHERE id = 1"
            ).fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row and row[0] is not None else None
