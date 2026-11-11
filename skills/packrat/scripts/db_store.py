"""SQLite WAL relational, FTS5 BM25, and vector store for packrat."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from .db_migrations import CONNECTION_PRAGMAS, apply_migrations
    from .db_schema import BACKFILL_COLUMNS, SCHEMA_SQL
    from .live_store import LiveStoreMixin
    from .models import (
        BACKFILL_STREAMS,
        EMBEDDING_DIM,
        TIMELINE_LINK_TYPES,
        CodeSymbolRecord,
        CommentRecord,
        CommitRecord,
        DiscussionRecord,
        DocSectionRecord,
        IssueRecord,
        PrRecord,
        RepoConfig,
        StoredEmbeddingRecord,
        TextChunk,
        TimelineEventRecord,
        TimelineMode,
        TimelineTarget,
        dequantize_u8_vector,
        iso_at,
        parse_item_key,
        quantize_u8_vector,
    )
except ImportError:
    from db_migrations import CONNECTION_PRAGMAS, apply_migrations  # type: ignore[no-redef]
    from db_schema import BACKFILL_COLUMNS, SCHEMA_SQL  # type: ignore[no-redef]
    from live_store import LiveStoreMixin  # type: ignore[no-redef]
    from models import (  # type: ignore[no-redef]
        BACKFILL_STREAMS,
        EMBEDDING_DIM,
        TIMELINE_LINK_TYPES,
        CodeSymbolRecord,
        CommentRecord,
        CommitRecord,
        DiscussionRecord,
        DocSectionRecord,
        IssueRecord,
        PrRecord,
        RepoConfig,
        StoredEmbeddingRecord,
        TextChunk,
        TimelineEventRecord,
        TimelineMode,
        TimelineTarget,
        dequantize_u8_vector,
        iso_at,
        parse_item_key,
        quantize_u8_vector,
    )

__all__ = [
    "BACKFILL_COLUMNS",
    "DEFAULT_DB_PATH",
    "INIT_SQL_FILENAME",
    "SCHEMA_SQL",
    "PackratStore",
    "render_init_sql",
]

INIT_SQL_FILENAME = "init.sql"
DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "packrat"
DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "repos.sqlite"
DEFAULT_INIT_SQL_PATH = DEFAULT_DATA_DIR / INIT_SQL_FILENAME
_TEMPLATE_SQL_PATH = Path(__file__).resolve().parent / INIT_SQL_FILENAME


def render_init_sql(db_path: Path = DEFAULT_DB_PATH) -> str:
    """Renders the DuckDB init.sql script with the resolved SQLite database path."""
    template = _TEMPLATE_SQL_PATH.read_text(encoding="utf-8")
    escaped_path = str(db_path.resolve()).replace("'", "''")
    return template.replace("{{DB_PATH}}", escaped_path)


def _row_to_repo(row: sqlite3.Row) -> RepoConfig:
    """Converts a sqlite3.Row from the repos table into a RepoConfig."""
    return RepoConfig(
        repo_id=int(row["repo_id"]),
        owner=str(row["owner"]),
        name=str(row["name"]),
        full_name=str(row["full_name"]),
        local_path=row["local_path"],
        default_branch=str(row["default_branch"] or "main"),
        events_etag=row["events_etag"],
        events_last_modified=row["events_last_modified"],
        poll_interval_sec=int(row["poll_interval_sec"] or 60),
        last_events_check_at=row["last_events_check_at"],
        issues_watermark=row["issues_watermark"],
        prs_watermark=row["prs_watermark"],
        discussions_watermark=row["discussions_watermark"],
        last_full_sweep_at=row["last_full_sweep_at"],
        last_indexed_sha=row["last_indexed_sha"],
        created_at=str(row["created_at"] or ""),
        updated_at=str(row["updated_at"] or ""),
        **{col: row[col] for col in BACKFILL_COLUMNS},
        mode=str(row["mode"] or "mirror"),
    )


def _row_to_chunk(row: sqlite3.Row) -> TextChunk:
    """Converts a sqlite3.Row from the chunks table into a TextChunk."""
    return TextChunk(
        chunk_id=str(row["chunk_id"]),
        repo_id=int(row["repo_id"]),
        source_kind=str(row["source_kind"]),
        source_ref=str(row["source_ref"]),
        title=str(row["title"]),
        text=str(row["text"]),
        content_sha256=str(row["content_sha256"]),
        is_untrusted=bool(row["is_untrusted"]),
        file_path=row["file_path"],
        item_number=int(row["item_number"]) if row["item_number"] is not None else None,
        updated_at=str(row["updated_at"] or ""),
    )


TIMELINE_SINCE_OVERLAP_S = 300
"""Seconds subtracted from a stored sync time to form `timelineItems(since:)`."""

_ITEM_TABLES: dict[str, str] = {"pr": "pull_requests", "issue": "issues"}
_SUBJECT_ITEM_KINDS: dict[str, str] = {"Issue": "issue", "PullRequest": "pr"}
_XREF_EVENT_TYPES = frozenset(
    {"CrossReferencedEvent", "ConnectedEvent", "DisconnectedEvent"}
)
_CLOSED_BY_LINK, _XREF_LINK = TIMELINE_LINK_TYPES
_TIMELINE_LINK_MARKS = ", ".join("?" for _ in TIMELINE_LINK_TYPES)

_TIMELINE_MODE_FILTERS: dict[TimelineMode, str] = {
    TimelineMode.INCREMENTAL: "timeline_synced_at IS NOT NULL AND updated_at > timeline_synced_at",
    TimelineMode.BACKFILL: "timeline_synced_at IS NULL",
    TimelineMode.ALL: "(timeline_synced_at IS NULL OR updated_at > timeline_synced_at)",
}


def _timeline_since(synced_at: str | None) -> str | None:
    """Returns `synced_at` minus `TIMELINE_SINCE_OVERLAP_S` in `iso_at` format, or None."""
    if not synced_at:
        return None
    try:
        moment = datetime.strptime(synced_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        moment = datetime.fromisoformat(synced_at)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
    return iso_at((moment - timedelta(seconds=TIMELINE_SINCE_OVERLAP_S)).timestamp())


def _item_table(item_kind: str) -> str:
    """Returns the table holding `item_kind` ("pr" or "issue") items.

    Raises:
        ValueError: If `item_kind` is neither "pr" nor "issue".
    """
    table = _ITEM_TABLES.get(item_kind)
    if table is None:
        raise ValueError(f"Unknown item kind '{item_kind}'; expected 'pr' or 'issue'.")
    return table


def _derived_link_type(event: TimelineEventRecord, other_kind: str) -> str | None:
    """Returns the `issue_pr_links` type an event implies, before the same-repo check."""
    if event.event_type == "ClosedEvent":
        is_issue_closed_by_pr = event.item_kind == "issue" and other_kind == "pr"
        return _CLOSED_BY_LINK if is_issue_closed_by_pr else None
    if event.event_type in _XREF_EVENT_TYPES and {event.item_kind, other_kind} == {
        "issue",
        "pr",
    }:
        return _XREF_LINK
    return None


def _derived_issue_pr_link(
    event: TimelineEventRecord, is_same_repo: Callable[[int, str], bool]
) -> tuple[int, int, int, str] | None:
    """Maps a timeline event to an `(repo_id, issue, pr, link_type)` row, or None.

    A `ClosedEvent` on an issue whose closer is a pull request yields
    `closed_by`. A cross-reference, connect, or disconnect event pairing one
    issue with one pull request yields `xref`. Pairs whose linked item is in
    another repository, and issue-to-issue or PR-to-PR pairs, yield None.
    """
    other_kind = _SUBJECT_ITEM_KINDS.get(event.subject_type or "")
    if other_kind is None or not event.subject:
        return None
    link_type = _derived_link_type(event, other_kind)
    if link_type is None:
        return None
    try:
        owner, name, number = parse_item_key(event.subject)
    except ValueError:
        return None
    if not is_same_repo(event.repo_id, f"{owner}/{name}"):
        return None
    if event.item_kind == "issue":
        return event.repo_id, event.item_number, number, link_type
    return event.repo_id, number, event.item_number, link_type


def _row_to_timeline_target(row: sqlite3.Row, synced_column: str) -> TimelineTarget:
    """Converts a candidate or dirty-queue row into a TimelineTarget."""
    return TimelineTarget(
        repo_id=int(row["repo_id"]),
        item_kind=str(row["item_kind"]),
        item_number=int(row["item_number"]),
        node_id=str(row["node_id"]),
        since=_timeline_since(row[synced_column]),
    )


class PackratStore(LiveStoreMixin):
    """Manages the SQLite WAL relational, FTS5, and vector database.

    Live GitHub state (leases, change feed, checks, notifications, heartbeat)
    is handled by the methods inherited from `LiveStoreMixin`.
    """

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        """Opens the database, applying pending schema migrations.

        Raises:
            SchemaTooNewError: If the database was migrated by a newer packrat.
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.executescript(CONNECTION_PRAGMAS)
            self.applied_migrations = apply_migrations(self.conn)
            if (
                self.applied_migrations
                and self.default_cache_dir is not None
                and self.init_sql_path.exists()
            ):
                self.write_init_sql()
        except BaseException:
            self.conn.close()
            raise

    @property
    def default_cache_dir(self) -> Path | None:
        """Returns the directory containing `db_path`, or None for `:memory:` stores."""
        return self.db_path.parent if str(self.db_path) != ":memory:" else None

    @property
    def init_sql_path(self) -> Path:
        """Returns the default `init.sql` path adjacent to `db_path`."""
        return self.db_path.parent / INIT_SQL_FILENAME

    def close(self) -> None:
        """Closes the underlying SQLite connection."""
        self.conn.close()

    def write_init_sql(self, init_sql_path: Path | None = None) -> Path:
        """Writes the rendered DuckDB init.sql file to disk."""
        target = (
            Path(init_sql_path) if init_sql_path is not None else self.init_sql_path
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_init_sql(self.db_path), encoding="utf-8")
        return target

    def _bump_vector_cache_seq_in_tx(self, now: float | None = None) -> None:
        """Increments the monotonic vector_cache_seq counter in service_state."""
        self.conn.execute(
            """
            INSERT INTO service_state (key, value, updated_at)
            VALUES ('vector_cache_seq', '1', ?)
            ON CONFLICT(key) DO UPDATE SET
              value = CAST(CAST(service_state.value AS INTEGER) + 1 AS TEXT),
              updated_at = excluded.updated_at
            """,
            (iso_at(now),),
        )

    def vector_cache_watermark(self) -> list[Any]:
        """Computes an O(log N) watermark over service_state, chunk_embeddings, chunks, and repos."""
        row = self.conn.execute(
            """
            SELECT
              COALESCE((SELECT value FROM service_state WHERE key = 'vector_cache_seq'), '0') AS seq,
              COALESCE((SELECT COUNT(*) FROM chunk_embeddings), 0) AS ce_cnt,
              COALESCE((SELECT MAX(rowid) FROM chunk_embeddings), 0) AS max_ce_rowid,
              COALESCE((SELECT MIN(rowid) FROM chunk_embeddings), 0) AS min_ce_rowid,
              COALESCE((SELECT COUNT(*) FROM chunks), 0) AS ch_cnt,
              COALESCE((SELECT MAX(rowid) FROM chunks), 0) AS max_ch_rowid,
              COALESCE((SELECT MIN(rowid) FROM chunks), 0) AS min_ch_rowid,
              COALESCE((SELECT COUNT(*) FROM repos), 0) AS repo_cnt,
              COALESCE((SELECT GROUP_CONCAT(full_name, ',') FROM repos), '') AS repo_names
            """
        ).fetchone()
        if not row:
            return ["0", 0, 0, 0, 0, 0, 0, 0, ""]
        return [
            str(row["seq"]),
            int(row["ce_cnt"]),
            int(row["max_ce_rowid"]),
            int(row["min_ce_rowid"]),
            int(row["ch_cnt"]),
            int(row["max_ch_rowid"]),
            int(row["min_ch_rowid"]),
            int(row["repo_cnt"]),
            str(row["repo_names"]),
        ]

    def remove_repo(self, full_name: str) -> bool:
        """Deletes a tracked repository and all of its synced rows.

        Returns False when no repository named `full_name` is registered.
        """
        repo = self.get_repo(full_name)
        if repo is None:
            return False
        with self.conn:
            # FTS5 virtual tables do not participate in foreign key cascades.
            self.conn.execute(
                "DELETE FROM chunks_fts WHERE rowid IN (SELECT rowid FROM chunks WHERE repo_id = ?)",
                (repo.repo_id,),
            )
            self.conn.execute("DELETE FROM repos WHERE repo_id = ?", (repo.repo_id,))
            self._bump_vector_cache_seq_in_tx()
        return True

    def rename_repo(
        self,
        old_full_name: str,
        new_owner_or_full_name: str,
        new_name: str | None = None,
        *,
        now: float | None = None,
    ) -> RepoConfig:
        """Renames a tracked repository in place, keeping its synced rows and embeddings.

        Accepts either `rename_repo("old/repo", "new/repo")` or
        `rename_repo("old/repo", "new", "repo")`. Chunk IDs and source references
        embed the full name, so their prefixes are rewritten too, as are
        timeline event subjects that link to this repository's issues and pull
        requests. The events ETag is cleared because the events URL changes
        with the name.

        Raises:
            ValueError: if `old_full_name` is not registered, the new name is
                malformed, or the new name is already taken.
        """
        if new_name is None:
            if "/" not in new_owner_or_full_name:
                raise ValueError(
                    f"Invalid repository name '{new_owner_or_full_name}'; expected owner/name."
                )
            new_owner, resolved_name = new_owner_or_full_name.split("/", 1)
        else:
            new_owner, resolved_name = new_owner_or_full_name, new_name
        repo = self.get_repo(old_full_name)
        if repo is None:
            raise ValueError(
                f"Repository '{old_full_name}' is not registered in packrat."
            )
        new_full_name = f"{new_owner}/{resolved_name}"
        existing = self.get_repo(new_full_name)
        if existing is not None and existing.repo_id != repo.repo_id:
            raise ValueError(
                f"Repository '{new_full_name}' is already registered in packrat."
            )
        old_prefix = repo.full_name
        start = len(old_prefix) + 1
        now_iso = iso_at(now)
        with self.conn:
            # chunk_embeddings.chunk_id references chunks.chunk_id without ON UPDATE
            # CASCADE, so defer the check until both tables have been rewritten.
            self.conn.execute("PRAGMA defer_foreign_keys = ON")
            self.conn.execute(
                """
                UPDATE repos SET owner = ?, name = ?, full_name = ?,
                  events_etag = NULL, events_last_modified = NULL, updated_at = ?
                WHERE repo_id = ?
                """,
                (new_owner, resolved_name, new_full_name, now_iso, repo.repo_id),
            )
            # Rewrite dependent tables first, while chunks still identifies this
            # repo's rows by their old IDs.
            repo_chunk_ids = "SELECT chunk_id FROM chunks WHERE repo_id = ?"
            self.conn.execute(
                f"UPDATE chunk_embeddings SET chunk_id = ? || substr(chunk_id, ?) WHERE chunk_id IN ({repo_chunk_ids})",
                (new_full_name, start, repo.repo_id),
            )
            for column in ("chunk_id", "source_ref"):
                self.conn.execute(
                    f"""
                    UPDATE chunks SET {column} = ? || substr({column}, ?)
                    WHERE repo_id = ? AND substr({column}, 1, ?) = ?
                    """,
                    (new_full_name, start, repo.repo_id, len(old_prefix), old_prefix),
                )
            self._rewrite_timeline_subjects_in_tx(old_prefix, new_full_name)
            self._bump_vector_cache_seq_in_tx(now)
        renamed = self.get_repo(new_full_name)
        if renamed is None:
            raise RuntimeError(f"Failed to load repository {new_full_name}")
        return renamed

    def register_repo(
        self,
        owner: str,
        name: str,
        local_path: str | None = None,
        default_branch: str = "main",
        *,
        now: float | None = None,
    ) -> RepoConfig:
        """Inserts or updates a tracked repository configuration.

        Registering an `items` repository promotes it to `mirror` mode, so the
        next sync backfills its history and polls its events. An existing row
        is matched case-insensitively (like every other lookup) and keeps its
        stored casing, so a repo auto-created from a notification in GitHub's
        canonical case is promoted rather than duplicated.
        """
        now_iso = iso_at(now)
        full_name = f"{owner}/{name}"
        existing = self.get_repo(full_name)
        with self.conn:
            if existing is not None:
                self.conn.execute(
                    """
                    UPDATE repos SET
                      local_path = COALESCE(?, local_path),
                      default_branch = COALESCE(?, default_branch),
                      mode = 'mirror',
                      updated_at = ?
                    WHERE repo_id = ?
                    """,
                    (local_path, default_branch, now_iso, existing.repo_id),
                )
            else:
                self.conn.execute(
                    """
                    INSERT INTO repos (
                      owner, name, full_name, local_path, default_branch, created_at, updated_at,
                      mode
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'mirror')
                    """,
                    (
                        owner,
                        name,
                        full_name,
                        local_path,
                        default_branch,
                        now_iso,
                        now_iso,
                    ),
                )
        repo = self.get_repo(full_name)
        if repo is None:
            raise RuntimeError(f"Failed to load repository {full_name}")
        return repo

    def update_repo_sync_state(
        self,
        repo_id: int,
        *,
        events_etag: str | None = None,
        events_last_modified: str | None = None,
        poll_interval_sec: int | None = None,
        last_events_check_at: str | None = None,
        issues_watermark: str | None = None,
        prs_watermark: str | None = None,
        discussions_watermark: str | None = None,
        last_full_sweep_at: str | None = None,
        last_indexed_sha: str | None = None,
        now: float | None = None,
    ) -> None:
        """Updates non-None watermark and ETag columns for repo_id."""
        now_iso = iso_at(now)
        with self.conn:
            self.conn.execute(
                """
                UPDATE repos SET
                  events_etag = COALESCE(?, events_etag),
                  events_last_modified = COALESCE(?, events_last_modified),
                  poll_interval_sec = COALESCE(?, poll_interval_sec),
                  last_events_check_at = COALESCE(?, last_events_check_at),
                  issues_watermark = COALESCE(?, issues_watermark),
                  prs_watermark = COALESCE(?, prs_watermark),
                  discussions_watermark = COALESCE(?, discussions_watermark),
                  last_full_sweep_at = COALESCE(?, last_full_sweep_at),
                  last_indexed_sha = COALESCE(?, last_indexed_sha),
                  updated_at = ?
                WHERE repo_id = ?
                """,
                (
                    events_etag,
                    events_last_modified,
                    poll_interval_sec,
                    last_events_check_at,
                    issues_watermark,
                    prs_watermark,
                    discussions_watermark,
                    last_full_sweep_at,
                    last_indexed_sha,
                    now_iso,
                    repo_id,
                ),
            )

    def set_backfill_state(
        self,
        repo_id: int,
        stream: str,
        *,
        cursor: str | None,
        done_at: str | None,
        now: float | None = None,
    ) -> None:
        """Sets the backfill cursor and completion time for one stream, including clearing them to NULL.

        Raises:
            ValueError: if `stream` is not one of BACKFILL_STREAMS.
        """
        if stream not in BACKFILL_STREAMS:
            raise ValueError(
                f"Unknown GitHub stream '{stream}'; expected one of {BACKFILL_STREAMS}."
            )
        with self.conn:
            self.conn.execute(
                f"""
                UPDATE repos SET {stream}_backfill_cursor = ?, {stream}_backfill_done_at = ?, updated_at = ?
                WHERE repo_id = ?
                """,
                (cursor, done_at, iso_at(now), repo_id),
            )

    def reset_backfill_state(self, repo_id: int, *, now: float | None = None) -> None:
        """Clears every stream's watermark and backfill state so the next sync refetches full history.

        Also clears per-item `timeline_synced_at` and `xref_synced_at`, so
        every stored item's timeline is fetched again from the start.
        """
        cleared = [f"{col} = NULL" for col in BACKFILL_COLUMNS]
        cleared += [f"{stream}_watermark = NULL" for stream in BACKFILL_STREAMS]
        with self.conn:
            self.conn.execute(
                f"UPDATE repos SET {', '.join(cleared)}, updated_at = ? WHERE repo_id = ?",
                (iso_at(now), repo_id),
            )
            for table in _ITEM_TABLES.values():
                self.conn.execute(
                    f"""
                    UPDATE {table} SET timeline_synced_at = NULL, xref_synced_at = NULL
                    WHERE repo_id = ?
                    """,
                    (repo_id,),
                )

    def get_repo(self, full_name: str) -> RepoConfig | None:
        """Looks up a repository by full_name (case-insensitively, including aliases)."""
        if "/" in full_name:
            owner, name = full_name.split("/", 1)
            row = self._find_repo_row(owner, name)
        else:
            row = self.conn.execute(
                "SELECT * FROM repos WHERE lower(full_name) = lower(?)",
                (full_name,),
            ).fetchone()
        return _row_to_repo(row) if row else None

    def get_repo_by_id(self, repo_id: int) -> RepoConfig | None:
        """Looks up a repository by integer repo_id."""
        row = self.conn.execute(
            "SELECT * FROM repos WHERE repo_id = ?",
            (repo_id,),
        ).fetchone()
        return _row_to_repo(row) if row else None

    def list_repos(self, mode: str | None = None) -> list[RepoConfig]:
        """Returns registered repositories ordered by repo_id.

        Args:
            mode: When set ("mirror" or "items"), only repositories in that mode.
        """
        if mode is None:
            rows = self.conn.execute(
                "SELECT * FROM repos ORDER BY repo_id ASC"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM repos WHERE mode = ? ORDER BY repo_id ASC", (mode,)
            ).fetchall()
        return [_row_to_repo(r) for r in rows]

    def upsert_issues(
        self,
        issues: Sequence[IssueRecord],
        comments: Sequence[CommentRecord] = (),
    ) -> None:
        """Upserts issue rows and their associated comments in a single transaction."""
        if not issues and not comments:
            return
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO issues (
                  repo_id, number, node_id, state, author, author_association,
                  title, body, labels_json, assignees_json, created_at, updated_at, closed_at, url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo_id, number) DO UPDATE SET
                  node_id = excluded.node_id,
                  state = excluded.state,
                  author = excluded.author,
                  author_association = excluded.author_association,
                  title = excluded.title,
                  body = excluded.body,
                  labels_json = excluded.labels_json,
                  assignees_json = excluded.assignees_json,
                  updated_at = excluded.updated_at,
                  closed_at = excluded.closed_at,
                  url = excluded.url
                """,
                [
                    (
                        i.repo_id,
                        i.number,
                        i.node_id,
                        i.state,
                        i.author,
                        i.author_association,
                        i.title,
                        i.body,
                        json.dumps(list(i.labels)),
                        json.dumps(list(i.assignees)),
                        i.created_at,
                        i.updated_at,
                        i.closed_at,
                        i.url,
                    )
                    for i in issues
                ],
            )
            self._upsert_comments_in_tx(comments)

    def _upsert_comments_in_tx(self, comments: Sequence[CommentRecord]) -> None:
        """Upserts comment rows inside an active transaction."""
        if not comments:
            return
        self.conn.executemany(
            """
            INSERT INTO comments (
              repo_id, comment_id, parent_kind, parent_number, author,
              author_association, body, created_at, updated_at, url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo_id, comment_id) DO UPDATE SET
              body = excluded.body,
              updated_at = excluded.updated_at,
              url = excluded.url
            """,
            [
                (
                    c.repo_id,
                    c.comment_id,
                    c.parent_kind,
                    c.parent_number,
                    c.author,
                    c.author_association,
                    c.body,
                    c.created_at,
                    c.updated_at,
                    c.url,
                )
                for c in comments
            ],
        )

    def upsert_pull_requests(self, prs: Sequence[PrRecord]) -> None:
        """Upserts pull requests along with files, reviews, threads, comments, and links."""
        if not prs:
            return
        with self.conn:
            for pr in prs:
                self._upsert_single_pr(pr)

    def _upsert_single_pr(self, pr: PrRecord) -> None:
        """Upserts one pull request and its child records inside an active transaction."""
        self._note_mirror_pr_change_in_tx(pr)
        self.conn.execute(
            """
            INSERT INTO pull_requests (
              repo_id, number, node_id, state, is_draft, author, author_association,
              title, body, labels_json, base_ref, head_ref, head_sha, merge_commit_sha,
              review_decision, mergeable, additions, deletions, changed_files,
              created_at, updated_at, merged_at, closed_at, url,
              head_repo_owner, is_cross_repository
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo_id, number) DO UPDATE SET
              node_id = excluded.node_id,
              state = excluded.state,
              is_draft = excluded.is_draft,
              author = excluded.author,
              author_association = excluded.author_association,
              title = excluded.title,
              body = excluded.body,
              labels_json = excluded.labels_json,
              base_ref = excluded.base_ref,
              head_ref = excluded.head_ref,
              head_sha = excluded.head_sha,
              merge_commit_sha = excluded.merge_commit_sha,
              review_decision = excluded.review_decision,
              mergeable = excluded.mergeable,
              additions = excluded.additions,
              deletions = excluded.deletions,
              changed_files = excluded.changed_files,
              updated_at = excluded.updated_at,
              merged_at = excluded.merged_at,
              closed_at = excluded.closed_at,
              url = excluded.url,
              head_repo_owner = COALESCE(excluded.head_repo_owner, pull_requests.head_repo_owner),
              is_cross_repository = COALESCE(
                excluded.is_cross_repository, pull_requests.is_cross_repository
              )
            """,
            (
                pr.repo_id,
                pr.number,
                pr.node_id,
                pr.state,
                1 if pr.is_draft else 0,
                pr.author,
                pr.author_association,
                pr.title,
                pr.body,
                json.dumps(list(pr.labels)),
                pr.base_ref,
                pr.head_ref,
                pr.head_sha,
                pr.merge_commit_sha,
                pr.review_decision,
                pr.mergeable,
                pr.additions,
                pr.deletions,
                pr.changed_files,
                pr.created_at,
                pr.updated_at,
                pr.merged_at,
                pr.closed_at,
                pr.url,
                pr.head_repo_owner,
                None if pr.is_cross_repository is None else int(pr.is_cross_repository),
            ),
        )
        self.conn.execute(
            "DELETE FROM pr_files WHERE repo_id = ? AND pr_number = ?",
            (pr.repo_id, pr.number),
        )
        if pr.files:
            self.conn.executemany(
                """
                INSERT INTO pr_files (repo_id, pr_number, file_path, change_type, additions, deletions)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        f.repo_id,
                        f.pr_number,
                        f.file_path,
                        f.change_type,
                        f.additions,
                        f.deletions,
                    )
                    for f in pr.files
                ],
            )
        if pr.reviews:
            self.conn.executemany(
                """
                INSERT INTO reviews (
                  repo_id, review_id, pr_number, author, author_association, state, body,
                  submitted_at, author_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo_id, review_id) DO UPDATE SET
                  state = excluded.state,
                  body = excluded.body,
                  submitted_at = excluded.submitted_at,
                  author_type = COALESCE(excluded.author_type, reviews.author_type)
                """,
                [
                    (
                        r.repo_id,
                        r.review_id,
                        r.pr_number,
                        r.author,
                        r.author_association,
                        r.state,
                        r.body,
                        r.submitted_at,
                        r.author_type,
                    )
                    for r in pr.reviews
                ],
            )
        self._upsert_pr_threads_and_links(pr)

    def _upsert_pr_threads_and_links(self, pr: PrRecord) -> None:
        """Upserts review threads, general comments, and issue links for a pull request."""
        if pr.review_threads:
            self.conn.executemany(
                """
                INSERT INTO review_threads (
                  thread_id, repo_id, pr_number, file_path, line, start_line,
                  is_resolved, is_outdated, reviewer, author_association,
                  created_at, updated_at, body, comments_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                  is_resolved = excluded.is_resolved,
                  is_outdated = excluded.is_outdated,
                  updated_at = excluded.updated_at,
                  body = excluded.body,
                  comments_json = excluded.comments_json
                """,
                [
                    (
                        rt.thread_id,
                        rt.repo_id,
                        rt.pr_number,
                        rt.file_path,
                        rt.line,
                        rt.start_line,
                        1 if rt.is_resolved else 0,
                        1 if rt.is_outdated else 0,
                        rt.reviewer,
                        rt.author_association,
                        rt.created_at,
                        rt.updated_at,
                        rt.body,
                        rt.comments_json,
                    )
                    for rt in pr.review_threads
                ],
            )
        self._upsert_comments_in_tx(pr.comments)
        # Only text-derived links (such as `closes` and `references`) are rebuilt
        # here; timeline-derived `closed_by` and `xref` rows come from
        # `upsert_timeline_events` and must survive a PR re-save.
        self.conn.execute(
            f"""
            DELETE FROM issue_pr_links
            WHERE repo_id = ? AND pr_number = ? AND link_type NOT IN ({_TIMELINE_LINK_MARKS})
            """,
            (pr.repo_id, pr.number, *TIMELINE_LINK_TYPES),
        )
        if pr.issue_links:
            self.conn.executemany(
                """
                INSERT OR IGNORE INTO issue_pr_links (repo_id, issue_number, pr_number, link_type)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (lk.repo_id, lk.issue_number, lk.pr_number, lk.link_type)
                    for lk in pr.issue_links
                ],
            )

    def upsert_discussions(self, discussions: Sequence[DiscussionRecord]) -> None:
        """Upserts GitHub discussion records."""
        if not discussions:
            return
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO discussions (
                  repo_id, number, node_id, category, author, title, body,
                  answer_body, created_at, updated_at, url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo_id, number) DO UPDATE SET
                  category = excluded.category,
                  title = excluded.title,
                  body = excluded.body,
                  answer_body = excluded.answer_body,
                  updated_at = excluded.updated_at,
                  url = excluded.url
                """,
                [
                    (
                        d.repo_id,
                        d.number,
                        d.node_id,
                        d.category,
                        d.author,
                        d.title,
                        d.body,
                        d.answer_body,
                        d.created_at,
                        d.updated_at,
                        d.url,
                    )
                    for d in discussions
                ],
            )

    def upsert_commits(self, commits: Sequence[CommitRecord]) -> None:
        """Upserts git commit metadata and commit-to-file edges."""
        if not commits:
            return
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO commits (
                  repo_id, sha, author_name, author_email, committed_at, title, message, pr_number
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo_id, sha) DO UPDATE SET
                  title = excluded.title,
                  message = excluded.message,
                  pr_number = COALESCE(excluded.pr_number, commits.pr_number)
                """,
                [
                    (
                        c.repo_id,
                        c.sha,
                        c.author_name,
                        c.author_email,
                        c.committed_at,
                        c.title,
                        c.message,
                        c.pr_number,
                    )
                    for c in commits
                ],
            )
            all_files = [f for c in commits for f in c.files]
            if all_files:
                self.conn.executemany(
                    """
                    INSERT INTO commit_files (
                      repo_id, sha, file_path, change_type, old_path, additions, deletions
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(repo_id, sha, file_path) DO UPDATE SET
                      change_type = excluded.change_type,
                      old_path = excluded.old_path,
                      additions = excluded.additions,
                      deletions = excluded.deletions
                    """,
                    [
                        (
                            f.repo_id,
                            f.sha,
                            f.file_path,
                            f.change_type,
                            f.old_path,
                            f.additions,
                            f.deletions,
                        )
                        for f in all_files
                    ],
                )

    # ----- timeline events -----------------------------------------------------------

    def timeline_candidates(
        self, repo_id: int, mode: TimelineMode, limit: int
    ) -> list[TimelineTarget]:
        """Returns stored PRs and issues whose timeline should be fetched.

        Only items with a `node_id` are eligible. `BACKFILL` selects items never
        fetched (`timeline_synced_at IS NULL`), `INCREMENTAL` selects fetched
        items whose `updated_at` is newer than `timeline_synced_at`, and `ALL`
        selects both. Both sides of that comparison use the `iso_at` format,
        which keeps the text comparison valid.

        Args:
            repo_id: Repository to scan.
            mode: Which items to select.
            limit: Maximum number of targets.

        Returns:
            Targets ordered open items first, then by `updated_at` descending,
            with `since` set to `timeline_synced_at` minus
            `TIMELINE_SINCE_OVERLAP_S` (None for never-fetched items).
        """
        where = _TIMELINE_MODE_FILTERS[TimelineMode(mode)]
        selects = [
            f"""
            SELECT repo_id, '{kind}' AS item_kind, number AS item_number, node_id,
              timeline_synced_at, state, updated_at
            FROM {table}
            WHERE repo_id = ? AND node_id IS NOT NULL AND {where}
            """
            for kind, table in _ITEM_TABLES.items()
        ]
        rows = self.conn.execute(
            f"""
            SELECT * FROM ({" UNION ALL ".join(selects)})
            ORDER BY CASE WHEN upper(state) = 'OPEN' THEN 0 ELSE 1 END,
              updated_at DESC, item_kind ASC, item_number DESC
            LIMIT ?
            """,
            (repo_id, repo_id, int(limit)),
        ).fetchall()
        return [_row_to_timeline_target(r, "timeline_synced_at") for r in rows]

    def get_item_updated_at(
        self, repo_id: int, item_kind: str, item_number: int
    ) -> str | None:
        """Returns the stored `updated_at` of one PR or issue, or None when it isn't stored.

        Raises:
            ValueError: If `item_kind` is neither "pr" nor "issue".
        """
        table = _item_table(item_kind)
        row = self.conn.execute(
            f"SELECT updated_at FROM {table} WHERE repo_id = ? AND number = ?",
            (repo_id, item_number),
        ).fetchone()
        return str(row["updated_at"]) if row and row["updated_at"] is not None else None

    def count_timeline_pending(self, repo_id: int) -> int:
        """Returns how many stored PRs and issues with a `node_id` still need a first timeline fetch."""
        row = self.conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM pull_requests
               WHERE repo_id = ? AND node_id IS NOT NULL AND timeline_synced_at IS NULL)
              + (SELECT COUNT(*) FROM issues
                 WHERE repo_id = ? AND node_id IS NOT NULL AND timeline_synced_at IS NULL)
              AS n
            """,
            (repo_id, repo_id),
        ).fetchone()
        return int(row["n"]) if row else 0

    def _same_repo_checker(self, repo_full_name: str) -> Callable[[int, str], bool]:
        """Returns a cached `(repo_id, owner/name) -> bool` check for same-repo links.

        `owner/name` matches when it equals `repo_full_name` case-insensitively
        or resolves (including through `repo_aliases`) to `repo_id`.
        """
        cache: dict[tuple[int, str], bool] = {}

        def is_same_repo(repo_id: int, full_name: str) -> bool:
            key = (repo_id, full_name.lower())
            if key not in cache:
                if key[1] == repo_full_name.lower():
                    cache[key] = True
                else:
                    owner, name = full_name.split("/", 1)
                    row = self._find_repo_row(owner, name)
                    cache[key] = row is not None and int(row["repo_id"]) == repo_id
            return cache[key]

        return is_same_repo

    def upsert_timeline_events(
        self, events: Sequence[TimelineEventRecord], *, repo_full_name: str
    ) -> int:
        """Upserts timeline events by `event_id` and derives same-repo issue/PR links.

        Runs in one transaction. A `ClosedEvent` on an issue whose closer is a
        pull request in the same repository inserts a `closed_by` link; a
        `CrossReferencedEvent`, `ConnectedEvent`, or `DisconnectedEvent` that
        pairs one issue with one pull request in the same repository inserts
        an `xref` link. Existing links are kept (`INSERT OR IGNORE`).

        Args:
            events: Events to write.
            repo_full_name: `owner/name` of the repository the events belong
                to, used to recognize same-repo linked item keys.

        Returns:
            The number of event rows written.
        """
        if not events:
            return 0
        is_same_repo = self._same_repo_checker(repo_full_name)
        links = {
            link
            for e in events
            if (link := _derived_issue_pr_link(e, is_same_repo)) is not None
        }
        with self.conn:
            self.conn.executemany(
                """
                INSERT OR REPLACE INTO timeline_events (
                  event_id, repo_id, item_kind, item_number, event_type, created_at,
                  actor, actor_type, subject, subject_type, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        e.event_id,
                        e.repo_id,
                        e.item_kind,
                        e.item_number,
                        e.event_type,
                        e.created_at,
                        e.actor,
                        e.actor_type,
                        e.subject,
                        e.subject_type,
                        e.payload_json,
                    )
                    for e in events
                ],
            )
            if links:
                self.conn.executemany(
                    """
                    INSERT OR IGNORE INTO issue_pr_links (repo_id, issue_number, pr_number, link_type)
                    VALUES (?, ?, ?, ?)
                    """,
                    sorted(links),
                )
        return len(events)

    def mark_timeline_synced(
        self,
        repo_id: int,
        item_kind: str,
        item_number: int,
        *,
        synced_at: str,
        xref_only: bool,
    ) -> None:
        """Records a completed timeline fetch for one item and clears its dirty-queue row.

        The `timeline_dirty` row is deleted only when it was queued at or
        before `synced_at`, so a mention queued during the run survives.

        Args:
            repo_id: Repository of the item.
            item_kind: "pr" or "issue".
            item_number: Item number.
            synced_at: The run's start time in `iso_at` format.
            xref_only: Whether only cross-reference types were fetched, in
                which case only `xref_synced_at` is set and
                `timeline_synced_at` is left unchanged.

        Raises:
            ValueError: If `item_kind` is neither "pr" nor "issue".
        """
        table = _item_table(item_kind)
        assignments = "xref_synced_at = ?"
        params: list[Any] = [synced_at]
        if not xref_only:
            assignments += ", timeline_synced_at = ?"
            params.append(synced_at)
        with self.conn:
            self.conn.execute(
                f"UPDATE {table} SET {assignments} WHERE repo_id = ? AND number = ?",
                (*params, repo_id, item_number),
            )
            self.conn.execute(
                """
                DELETE FROM timeline_dirty
                WHERE repo_id = ? AND item_number = ? AND queued_at <= ?
                """,
                (repo_id, item_number, synced_at),
            )

    def enqueue_timeline_dirty(
        self,
        targets: Iterable[tuple[int, int]],
        *,
        reason: str,
        now: float | None = None,
        source: tuple[int, int] | None = None,
    ) -> int:
        """Queues items for a cross-reference-only timeline refresh.

        Args:
            targets: `(repo_id, item_number)` pairs mentioned by another item
                or commit. Duplicates are ignored, as are items already queued.
            reason: Why the items were queued, such as "mention" or "commit".
            now: Queue time in epoch seconds (default: now).
            source: `(repo_id, item_number)` of the mentioning item, which is
                skipped so an item never queues itself.

        Returns:
            The number of rows newly queued.
        """
        skip = {source} if source is not None else set()
        rows = sorted({(int(r), int(n)) for r, n in targets} - skip)
        if not rows:
            return 0
        queued_at = iso_at(now)
        written = 0
        with self.conn:
            for repo_id, number in rows:
                cur = self.conn.execute(
                    """
                    INSERT OR IGNORE INTO timeline_dirty (repo_id, item_number, reason, queued_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (repo_id, number, reason, queued_at),
                )
                written += cur.rowcount
        return written

    def take_timeline_dirty(self, repo_id: int, limit: int) -> list[TimelineTarget]:
        """Returns queued items ready for a cross-reference-only timeline refresh.

        Each queued number resolves against `pull_requests` first, then
        `issues`. Rows whose item has never had its timeline fetched
        (`timeline_synced_at IS NULL`) are deleted instead of returned, since
        that item's first full fetch already includes the cross-reference
        types. Numbers not stored yet (or without a `node_id`) stay queued.
        Returned rows also stay queued until `mark_timeline_synced` clears them.

        Args:
            repo_id: Repository whose queue to read.
            limit: Maximum number of targets.

        Returns:
            Targets in queue order, with `since` set to `xref_synced_at` (or
            `timeline_synced_at` when no xref-only fetch has run) minus
            `TIMELINE_SINCE_OVERLAP_S`.
        """
        resolved = """
            SELECT d.repo_id, d.item_number, d.queued_at,
              CASE WHEN p.number IS NOT NULL THEN 'pr' ELSE 'issue' END AS item_kind,
              CASE WHEN p.number IS NOT NULL THEN p.node_id ELSE i.node_id END AS node_id,
              CASE WHEN p.number IS NOT NULL THEN p.timeline_synced_at
                ELSE i.timeline_synced_at END AS timeline_synced_at,
              CASE WHEN p.number IS NOT NULL THEN COALESCE(p.xref_synced_at, p.timeline_synced_at)
                ELSE COALESCE(i.xref_synced_at, i.timeline_synced_at) END AS xref_since
            FROM timeline_dirty d
            LEFT JOIN pull_requests p ON p.repo_id = d.repo_id AND p.number = d.item_number
            LEFT JOIN issues i ON i.repo_id = d.repo_id AND i.number = d.item_number
            WHERE d.repo_id = ? AND (p.number IS NOT NULL OR i.number IS NOT NULL)
        """
        with self.conn:
            self.conn.execute(
                f"""
                DELETE FROM timeline_dirty WHERE repo_id = ? AND item_number IN (
                  SELECT item_number FROM ({resolved}) WHERE timeline_synced_at IS NULL
                )
                """,
                (repo_id, repo_id),
            )
        rows = self.conn.execute(
            f"""
            SELECT * FROM ({resolved})
            WHERE node_id IS NOT NULL
            ORDER BY queued_at ASC, item_number ASC
            LIMIT ?
            """,
            (repo_id, int(limit)),
        ).fetchall()
        return [_row_to_timeline_target(r, "xref_since") for r in rows]

    def list_indexed_file_paths(self, repo_id: int) -> set[str]:
        """Returns all file paths with indexed symbols, doc sections, or file chunks."""
        rows = self.conn.execute(
            """
            SELECT file_path FROM code_symbols WHERE repo_id = ?
            UNION
            SELECT file_path FROM doc_sections WHERE repo_id = ?
            UNION
            SELECT file_path FROM chunks
            WHERE repo_id = ?
              AND source_kind IN ('code_symbol', 'doc_section')
              AND file_path IS NOT NULL
            """,
            (repo_id, repo_id, repo_id),
        ).fetchall()
        return {str(r["file_path"]) for r in rows if r["file_path"]}

    _FILE_BATCH_SIZE = 400
    _WRITE_BATCH_SIZE = 1000

    def _delete_code_index_for_paths_in_tx(
        self, repo_id: int, paths: Sequence[str]
    ) -> None:
        """Deletes symbols, doc sections, chunks, and FTS5 rows for a batch of file paths."""
        if not paths:
            return
        marks = ", ".join("?" for _ in paths)
        params = (repo_id, *paths)
        self.conn.execute(
            f"DELETE FROM code_symbols WHERE repo_id = ? AND file_path IN ({marks})",
            params,
        )
        self.conn.execute(
            f"DELETE FROM doc_sections WHERE repo_id = ? AND file_path IN ({marks})",
            params,
        )
        self.conn.execute(
            f"""
            DELETE FROM chunks_fts WHERE rowid IN (
                SELECT rowid FROM chunks
                WHERE repo_id = ? AND file_path IN ({marks})
                  AND source_kind IN ('code_symbol', 'doc_section')
            )
            """,
            params,
        )
        self.conn.execute(
            f"""
            DELETE FROM chunks
            WHERE repo_id = ? AND file_path IN ({marks})
              AND source_kind IN ('code_symbol', 'doc_section')
            """,
            params,
        )

    def _insert_code_symbols_in_tx(self, symbols: Sequence[CodeSymbolRecord]) -> None:
        """Inserts or replaces a batch of CodeSymbolRecords inside a transaction."""
        if not symbols:
            return
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO code_symbols (
              repo_id, file_path, symbol_name, kind,
              line_start, line_end, signature, docstring, sha
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    s.repo_id,
                    s.file_path,
                    s.symbol_name,
                    s.kind,
                    s.line_start,
                    s.line_end,
                    s.signature,
                    s.docstring,
                    s.sha,
                )
                for s in symbols
            ],
        )

    def _insert_doc_sections_in_tx(
        self, doc_sections: Sequence[DocSectionRecord]
    ) -> None:
        """Inserts or replaces a batch of DocSectionRecords inside a transaction."""
        if not doc_sections:
            return
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO doc_sections (
              repo_id, file_path, section_id, level, heading, breadcrumb,
              anchor_slug, line_start, line_end, subtree_line_end, content, sha
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    d.repo_id,
                    d.file_path,
                    d.section_id,
                    d.level,
                    d.heading,
                    d.breadcrumb,
                    d.anchor_slug,
                    d.line_start,
                    d.line_end,
                    d.subtree_line_end,
                    d.content,
                    d.sha,
                )
                for d in doc_sections
            ],
        )

    def replace_code_index_for_files(
        self,
        repo_id: int,
        changed_paths: Sequence[str],
        deleted_paths: Sequence[str],
        symbols: Sequence[CodeSymbolRecord],
        doc_sections: Sequence[DocSectionRecord],
        chunks: Sequence[TextChunk],
        *,
        now: float | None = None,
        reporter: Any = None,
        repo_label: str = "",
    ) -> None:
        """Replaces code symbols, doc sections, and chunks for changed or deleted files."""
        affected = list(dict.fromkeys([*changed_paths, *deleted_paths]))
        if (
            len(affected) <= self._FILE_BATCH_SIZE
            and len(symbols) <= self._WRITE_BATCH_SIZE
            and len(doc_sections) <= self._WRITE_BATCH_SIZE
            and len(chunks) <= self._WRITE_BATCH_SIZE
        ):
            with self.conn:
                self._delete_code_index_for_paths_in_tx(repo_id, affected)
                self._insert_code_symbols_in_tx(symbols)
                self._insert_doc_sections_in_tx(doc_sections)
                self._upsert_chunks_in_tx(chunks, now=now)
                if affected and not chunks:
                    self._bump_vector_cache_seq_in_tx(now)
            return

        total_chunks = len(chunks)
        report_progress = (
            reporter is not None and total_chunks >= self._WRITE_BATCH_SIZE
        )
        if report_progress:
            reporter.start_phase(
                repo_label or f"repo:{repo_id}", "git index", total=total_chunks
            )

        for start in range(0, len(affected), self._FILE_BATCH_SIZE):
            batch_paths = affected[start : start + self._FILE_BATCH_SIZE]
            with self.conn:
                self._delete_code_index_for_paths_in_tx(repo_id, batch_paths)
                if not chunks and start + self._FILE_BATCH_SIZE >= len(affected):
                    self._bump_vector_cache_seq_in_tx(now)

        for start in range(0, len(symbols), self._WRITE_BATCH_SIZE):
            with self.conn:
                self._insert_code_symbols_in_tx(
                    symbols[start : start + self._WRITE_BATCH_SIZE]
                )

        for start in range(0, len(doc_sections), self._WRITE_BATCH_SIZE):
            with self.conn:
                self._insert_doc_sections_in_tx(
                    doc_sections[start : start + self._WRITE_BATCH_SIZE]
                )

        for start in range(0, total_chunks, self._WRITE_BATCH_SIZE):
            batch_chunks = chunks[start : start + self._WRITE_BATCH_SIZE]
            with self.conn:
                self._upsert_chunks_in_tx(batch_chunks, now=now)
            if report_progress:
                done = min(total_chunks, start + len(batch_chunks))
                reporter.advance(
                    done, total_chunks, detail=batch_chunks[-1].file_path or ""
                )

        if report_progress:
            reporter.finish_phase()

    def upsert_chunks(
        self, chunks: Sequence[TextChunk], *, now: float | None = None
    ) -> None:
        """Upserts searchable text chunks and synchronizes the FTS5 index."""
        if not chunks:
            return
        for start in range(0, len(chunks), self._WRITE_BATCH_SIZE):
            with self.conn:
                self._upsert_chunks_in_tx(
                    chunks[start : start + self._WRITE_BATCH_SIZE], now=now
                )

    def _upsert_single_chunk_in_tx(self, ch: TextChunk, now_iso: str) -> None:
        """Upserts one chunk and updates `chunks_fts` by `rowid` when FTS columns change."""
        ts = ch.updated_at or now_iso
        existing = self.conn.execute(
            """
            SELECT rowid, title, content_sha256, text
            FROM chunks WHERE chunk_id = ?
            """,
            (ch.chunk_id,),
        ).fetchone()
        if existing is None:
            cur = self.conn.execute(
                """
                INSERT INTO chunks (
                  chunk_id, repo_id, source_kind, source_ref, file_path,
                  item_number, title, content_sha256, is_untrusted, text, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ch.chunk_id,
                    ch.repo_id,
                    ch.source_kind,
                    ch.source_ref,
                    ch.file_path,
                    ch.item_number,
                    ch.title,
                    ch.content_sha256,
                    1 if ch.is_untrusted else 0,
                    ch.text,
                    ts,
                ),
            )
            rowid = int(cur.lastrowid)
            self.conn.execute(
                """
                INSERT INTO chunks_fts (rowid, chunk_id, source_kind, source_ref, title, text)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (rowid, ch.chunk_id, ch.source_kind, ch.source_ref, ch.title, ch.text),
            )
            return

        rowid = int(existing["rowid"])
        fts_changed = (
            existing["content_sha256"] != ch.content_sha256
            or existing["title"] != ch.title
            or existing["text"] != ch.text
        )
        if fts_changed:
            self.conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (rowid,))
        self.conn.execute(
            """
            UPDATE chunks SET
              source_ref = ?,
              file_path = ?,
              item_number = ?,
              title = ?,
              content_sha256 = ?,
              is_untrusted = ?,
              text = ?,
              updated_at = ?
            WHERE rowid = ?
            """,
            (
                ch.source_ref,
                ch.file_path,
                ch.item_number,
                ch.title,
                ch.content_sha256,
                1 if ch.is_untrusted else 0,
                ch.text,
                ts,
                rowid,
            ),
        )
        if fts_changed:
            self.conn.execute(
                """
                INSERT INTO chunks_fts (rowid, chunk_id, source_kind, source_ref, title, text)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (rowid, ch.chunk_id, ch.source_kind, ch.source_ref, ch.title, ch.text),
            )

    def _upsert_chunks_in_tx(
        self, chunks: Sequence[TextChunk], *, now: float | None = None
    ) -> None:
        """Upserts chunks and keeps chunks_fts synchronized inside a transaction."""
        if not chunks:
            return
        now_iso = iso_at(now)
        for ch in chunks:
            self._upsert_single_chunk_in_tx(ch, now_iso)
        self._bump_vector_cache_seq_in_tx(now)

    def get_unembedded_chunks(
        self,
        repo_id: int | None,
        model_id: str,
    ) -> list[TextChunk]:
        """Returns chunks whose content_sha256 or model_id differs from chunk_embeddings."""
        sql = """
        SELECT c.*
        FROM chunks c
        LEFT JOIN chunk_embeddings ce ON ce.chunk_id = c.chunk_id
        WHERE (ce.chunk_id IS NULL OR ce.content_sha256 <> c.content_sha256 OR ce.model_id <> ?)
        """
        params: list[Any] = [model_id]
        if repo_id is not None:
            sql += " AND c.repo_id = ?"
            params.append(repo_id)
        sql += " ORDER BY c.chunk_id ASC"
        rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_chunk(r) for r in rows]

    def get_chunks_by_ids(self, chunk_ids: Sequence[str]) -> dict[str, TextChunk]:
        """Loads a mapping of chunk_id to TextChunk for the requested IDs."""
        if not chunk_ids:
            return {}
        placeholders = ",".join("?" for _ in chunk_ids)
        rows = self.conn.execute(
            f"SELECT * FROM chunks WHERE chunk_id IN ({placeholders})",
            list(chunk_ids),
        ).fetchall()
        return {str(r["chunk_id"]): _row_to_chunk(r) for r in rows}

    def upsert_embeddings(
        self,
        embeddings: Sequence[StoredEmbeddingRecord],
        *,
        now: float | None = None,
    ) -> None:
        """Upserts 384-dimensional embeddings as per-vector scaled offset uint8 BLOBs."""
        if not embeddings:
            return
        now_iso = iso_at(now)
        rows = []
        for e in embeddings:
            blob, scale = quantize_u8_vector(e.vector)
            rows.append(
                (
                    e.chunk_id,
                    e.model_id,
                    EMBEDDING_DIM,
                    scale,
                    blob,
                    e.content_sha256,
                    e.updated_at or now_iso,
                )
            )
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO chunk_embeddings (
                  chunk_id, model_id, dim, scale, vector_u8, content_sha256, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                  model_id = excluded.model_id,
                  dim = excluded.dim,
                  scale = excluded.scale,
                  vector_u8 = excluded.vector_u8,
                  content_sha256 = excluded.content_sha256,
                  updated_at = excluded.updated_at
                """,
                rows,
            )
            self._bump_vector_cache_seq_in_tx(now)

    def get_embedding(self, chunk_id: str) -> StoredEmbeddingRecord | None:
        """Fetches a single StoredEmbeddingRecord by chunk_id."""
        row = self.conn.execute(
            "SELECT * FROM chunk_embeddings WHERE chunk_id = ?",
            (chunk_id,),
        ).fetchone()
        if not row:
            return None
        return StoredEmbeddingRecord(
            chunk_id=str(row["chunk_id"]),
            model_id=str(row["model_id"]),
            content_sha256=str(row["content_sha256"]),
            vector=dequantize_u8_vector(bytes(row["vector_u8"]), float(row["scale"])),
            updated_at=str(row["updated_at"] or ""),
        )

    def table_counts(self, repo_id: int | None = None) -> dict[str, int]:
        """Returns row counts for core relational and vector tables."""
        tables = (
            "issues",
            "pull_requests",
            "review_threads",
            "comments",
            "discussions",
            "commits",
            "code_symbols",
            "doc_sections",
            "chunks",
            "timeline_events",
            "timeline_dirty",
        )
        counts: dict[str, int] = {}
        for tbl in tables:
            if repo_id is None:
                row = self.conn.execute(f"SELECT COUNT(*) AS n FROM {tbl}").fetchone()
            else:
                row = self.conn.execute(
                    f"SELECT COUNT(*) AS n FROM {tbl} WHERE repo_id = ?",
                    (repo_id,),
                ).fetchone()
            counts[tbl] = int(row["n"]) if row else 0

        if repo_id is None:
            emb_row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM chunk_embeddings"
            ).fetchone()
        else:
            emb_row = self.conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM chunk_embeddings ce
                JOIN chunks c ON c.chunk_id = ce.chunk_id
                WHERE c.repo_id = ?
                """,
                (repo_id,),
            ).fetchone()
        counts["chunk_embeddings"] = int(emb_row["n"]) if emb_row else 0
        return counts
