"""SQLite WAL relational, FTS5 BM25, and vector store for packrat."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from .db_migrations import (
        CONNECTION_PRAGMAS,
        LATEST_SCHEMA_VERSION,
        apply_migrations,
        read_schema_version,
    )
    from .db_schema import ALL_BACKFILL_COLUMNS, BACKFILL_COLUMNS, SCHEMA_SQL
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
        ExportSummary,
        IssueRecord,
        PrRecord,
        RepoConfig,
        SearchFilters,
        StoredEmbeddingRecord,
        TextChunk,
        TimelineEventRecord,
        TimelineMode,
        TimelineTarget,
        dequantize_u8_vector,
        iso_at,
        parse_item_key,
        quantize_u8_matrix,
    )
    from .progress import (
        NULL_REPORTER,
        ProgressReporter,
        format_bytes,
        format_duration,
    )
except ImportError:
    from db_migrations import (  # type: ignore[no-redef]
        CONNECTION_PRAGMAS,
        LATEST_SCHEMA_VERSION,
        apply_migrations,
        read_schema_version,
    )
    from db_schema import (  # type: ignore[no-redef]
        ALL_BACKFILL_COLUMNS,
        BACKFILL_COLUMNS,
        SCHEMA_SQL,
    )
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
        ExportSummary,
        IssueRecord,
        PrRecord,
        RepoConfig,
        SearchFilters,
        StoredEmbeddingRecord,
        TextChunk,
        TimelineEventRecord,
        TimelineMode,
        TimelineTarget,
        dequantize_u8_vector,
        iso_at,
        parse_item_key,
        quantize_u8_matrix,
    )
    from progress import (  # type: ignore[no-redef]
        NULL_REPORTER,
        ProgressReporter,
        format_bytes,
        format_duration,
    )

__all__ = [
    "BACKFILL_COLUMNS",
    "DEFAULT_DB_PATH",
    "INIT_SQL_FILENAME",
    "SCHEMA_SQL",
    "ExportIntegrityError",
    "PackratStore",
    "render_init_sql",
]


_EXPORT_BACKUP_STEP_PAGES = 8192
"""Pages copied per `sqlite3.Connection.backup` step (~32 MiB at 4 KiB pages)."""

_EXPORT_VM_PROGRESS_OPS = 250_000
"""SQLite VM opcode interval between `set_progress_handler` ticks during export."""


class ExportIntegrityError(sqlite3.DatabaseError):
    """Raised when an exported database fails FTS5 or foreign-key verification."""


class _ExportProgress:
    """Bridges SQLite backup/VM callbacks to `ProgressReporter` and `on_progress`."""

    def __init__(
        self,
        reporter: ProgressReporter | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        self._reporter: ProgressReporter = (
            reporter if reporter is not None else NULL_REPORTER
        )
        self._on_progress = on_progress
        self._phase = ""
        self._started = 0.0
        self._detail = ""

    def begin(
        self,
        phase: str,
        *,
        total: int | None = None,
        detail: str = "",
    ) -> None:
        """Starts `phase`, notifies `on_progress`, and emits an initial update if due."""
        if self._on_progress is not None:
            self._on_progress(phase)
        self._phase = phase
        self._started = time.monotonic()
        self._detail = detail
        self._reporter.start_phase("export", phase, total=total)
        if detail:
            self._reporter.advance(0, total, detail=detail)

    def advance_pages(self, done_pages: int, total_pages: int, page_size: int) -> None:
        """Emits page and byte progress during `sqlite3.Connection.backup`."""
        detail = (
            f"{format_bytes(done_pages * page_size)} / "
            f"{format_bytes(total_pages * page_size)}"
        )
        self._reporter.advance(done_pages, total_pages, detail=detail)

    def step(self, detail: str) -> None:
        """Updates the active sub-step label and emits a throttled progress line."""
        self._detail = detail
        self._reporter.advance(0, None, detail=detail)

    def vm_tick(self) -> int:
        """SQLite `set_progress_handler` callback; returns 0 to continue execution."""
        self._reporter.advance(0, None, detail=self._detail)
        return 0

    def end(self, summary: str) -> None:
        """Finishes the active phase, printing a summary line if progress was shown."""
        elapsed = max(0.0, time.monotonic() - self._started)
        if getattr(self._reporter, "has_emitted_in_phase", False):
            self._reporter.finish_phase(
                f"{self._phase} done: {summary} ({format_duration(elapsed)})"
            )
        else:
            self._reporter.finish_phase()

    @contextlib.contextmanager
    def vm_heartbeat(self, conn: sqlite3.Connection) -> Iterator[None]:
        """Installs `vm_tick` on `conn` for the duration of long SQLite statements."""
        conn.set_progress_handler(self.vm_tick, _EXPORT_VM_PROGRESS_OPS)
        try:
            yield
        finally:
            conn.set_progress_handler(None, 0)


def _unlink_sqlite_sidecars(db_file: Path) -> None:
    """Removes `-wal`, `-shm`, and `-journal` sidecars adjacent to `db_file`."""
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{db_file}{suffix}").unlink(missing_ok=True)


def _unlink_temp_export_files(tmp_path: Path) -> None:
    """Removes a temporary export SQLite file and any sidecars."""
    tmp_path.unlink(missing_ok=True)
    _unlink_sqlite_sidecars(tmp_path)


def _select_kept_mirror_repos(
    mirror_by_id: dict[int, RepoConfig],
    repo_ids: Sequence[int] | None,
) -> list[RepoConfig]:
    """Filters `mirror_by_id` to the requested `repo_ids` in caller order."""
    if repo_ids is None:
        return list(mirror_by_id.values())
    requested = list(dict.fromkeys(int(rid) for rid in repo_ids))
    missing = [rid for rid in requested if rid not in mirror_by_id]
    if missing:
        raise ValueError(
            f"Requested export repo_id(s) are not registered mirror repositories: {missing}"
        )
    return [mirror_by_id[rid] for rid in requested]


def _estimate_conn_sizes(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """Returns `(page_size, total_bytes, live_bytes)` for `conn` via header PRAGMAs."""
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0] or 4096)
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0] or 0)
    free_count = int(conn.execute("PRAGMA freelist_count").fetchone()[0] or 0)
    total_bytes = page_count * page_size
    live_bytes = max(0, page_count - free_count) * page_size
    return page_size, total_bytes, live_bytes


def _compact_and_verify_export_conn(
    dest_conn: sqlite3.Connection,
    *,
    on_progress: Callable[[str], None] | None = None,
    tracker: _ExportProgress | None = None,
) -> tuple[int, int, int]:
    """Compacts `dest_conn`, switches to DELETE journal mode, and verifies integrity."""
    active = tracker if tracker is not None else _ExportProgress(None, on_progress)
    dest_conn.isolation_level = None
    _ps, pre_bytes, live_bytes = _estimate_conn_sizes(dest_conn)
    vac_detail = (
        f"compacting {format_bytes(pre_bytes)} -> ~{format_bytes(live_bytes)} live"
    )
    active.begin("vacuum", detail=vac_detail)
    with active.vm_heartbeat(dest_conn):
        dest_conn.execute("VACUUM")
        dest_conn.execute("PRAGMA journal_mode = DELETE")
    _, post_bytes, _ = _estimate_conn_sizes(dest_conn)
    active.end(f"{format_bytes(pre_bytes)} -> {format_bytes(post_bytes)}")

    active.begin("verify", detail="step 1/2, checking FTS5 index")
    with active.vm_heartbeat(dest_conn):
        try:
            dest_conn.execute(
                "INSERT INTO chunks_fts(chunks_fts, rank) VALUES('integrity-check', 1)"
            )
        except sqlite3.DatabaseError as exc:
            raise ExportIntegrityError(
                f"Exported chunks_fts failed integrity check: {exc}"
            ) from exc
        active.step("step 2/2, checking foreign keys")
        fk_violations = dest_conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_violations:
            raise ExportIntegrityError(
                f"Exported database failed foreign_key_check ({len(fk_violations)} violation(s))."
            )
        chunks_remaining = int(
            dest_conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        )
        embeddings_remaining = int(
            dest_conn.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0]
        )
    schema_version = read_schema_version(dest_conn) or LATEST_SCHEMA_VERSION
    active.end(f"{chunks_remaining:,} chunks, 0 FK errors")
    return chunks_remaining, embeddings_remaining, schema_version


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
        **{col: row[col] for col in ALL_BACKFILL_COLUMNS},
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
        self.conn = sqlite3.connect(str(self.db_path), timeout=5.0)
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

    def _bump_vector_cache_seq_in_tx(
        self,
        now: float | None = None,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """Increments the monotonic vector_cache_seq counter in service_state."""
        target_conn = conn if conn is not None else self.conn
        target_conn.execute(
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
        """Computes an O(log N) watermark over service_state, chunk_embeddings, chunks, and mirror repos."""
        row = self.conn.execute(
            """
            SELECT
              COALESCE((SELECT value FROM service_state WHERE key = 'vector_cache_seq'), '0') AS seq,
              COALESCE((SELECT MAX(rowid) FROM chunk_embeddings), 0) AS max_ce_rowid,
              COALESCE((SELECT MIN(rowid) FROM chunk_embeddings), 0) AS min_ce_rowid,
              COALESCE((SELECT MAX(rowid) FROM chunks), 0) AS max_ch_rowid,
              COALESCE((SELECT MIN(rowid) FROM chunks), 0) AS min_ch_rowid,
              COALESCE((SELECT COUNT(*) FROM repos WHERE mode = 'mirror'), 0) AS repo_cnt,
              COALESCE((SELECT GROUP_CONCAT(full_name, ',') FROM repos WHERE mode = 'mirror'), '') AS repo_names
            """
        ).fetchone()
        if not row:
            return ["0", 0, 0, 0, 0, 0, ""]
        return [
            str(row["seq"]),
            int(row["max_ce_rowid"]),
            int(row["min_ce_rowid"]),
            int(row["max_ch_rowid"]),
            int(row["min_ch_rowid"]),
            int(row["repo_cnt"]),
            str(row["repo_names"]),
        ]

    def remove_repo(self, full_name: str) -> bool:
        """Deletes a tracked repository and all of its synced and live rows.

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
            self._delete_repo_live_logs_in_tx(repo.full_name)
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
        live item keys, aliases, and timeline event subjects that link to this
        repository's issues and pull requests. The events ETag is cleared
        because the events URL changes with the name.

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
            self._rewrite_repo_identity_in_tx(
                repo.repo_id, old_prefix, repo.repo_id, new_full_name, now_iso
            )
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

    def set_commits_backfill_state(
        self,
        repo_id: int,
        *,
        cursor: str | None,
        done_at: str | None,
        now: float | None = None,
    ) -> None:
        """Sets the git commit history backfill cursor and completion timestamp."""
        with self.conn:
            self.conn.execute(
                """
                UPDATE repos SET commits_backfill_cursor = ?, commits_backfill_done_at = ?, updated_at = ?
                WHERE repo_id = ?
                """,
                (cursor, done_at, iso_at(now), repo_id),
            )

    def reset_backfill_state(self, repo_id: int, *, now: float | None = None) -> None:
        """Clears every stream's watermark and backfill state so the next sync refetches full history.

        Also clears per-item `timeline_synced_at` and `xref_synced_at`, so
        every stored item's timeline is fetched again from the start.
        """
        cleared = [f"{col} = NULL" for col in ALL_BACKFILL_COLUMNS]
        cleared += [f"{stream}_watermark = NULL" for stream in BACKFILL_STREAMS]
        with self.conn:
            self.conn.execute(
                f"UPDATE repos SET {', '.join(cleared)}, updated_at = ? WHERE repo_id = ?",
                (iso_at(now), repo_id),
            )
            self.conn.execute(
                "DELETE FROM service_state WHERE key LIKE ? OR key LIKE ?",
                (f"gap_cursor:{repo_id}:%", f"gap_floor:{repo_id}:%"),
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
        old = self._note_mirror_pr_change_in_tx(pr)
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
              ),
              live_fetched_at = NULL
            WHERE excluded.updated_at >= pull_requests.updated_at
               OR pull_requests.live_fetched_at IS NULL
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
        if old is not None and (
            (
                old["live_fetched_at"] is not None
                and str(old["updated_at"] or "") > pr.updated_at
            )
            or (
                old["live_fetched_at"] is None
                and old["updated_at"] == pr.updated_at
                and old["head_sha"] == pr.head_sha
            )
        ):
            return
        self._upsert_pr_files_and_reviews_in_tx(pr)
        self._upsert_pr_threads_and_links(pr)

    def _upsert_pr_files_and_reviews_in_tx(self, pr: PrRecord) -> None:
        """Replaces `pr_files` and upserts `reviews` for a pull request inside a transaction."""
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

    def update_pr_fork_owners(
        self,
        repo_id: int,
        rows: Sequence[tuple[int, str | None, bool | None]],
    ) -> int:
        """Updates `head_repo_owner` and `is_cross_repository` on stored PRs without touching children."""
        if not rows:
            return 0
        params = [
            (
                owner,
                None if is_cross is None else int(is_cross),
                repo_id,
                int(number),
            )
            for number, owner, is_cross in rows
        ]
        with self.conn:
            cur = self.conn.executemany(
                """
                UPDATE pull_requests
                SET head_repo_owner = COALESCE(?, head_repo_owner),
                    is_cross_repository = COALESCE(?, is_cross_repository)
                WHERE repo_id = ? AND number = ?
                """,
                params,
            )
        return cur.rowcount

    def _upsert_pr_threads_and_links(self, pr: PrRecord) -> None:
        """Upserts review threads, general comments, and issue links for a pull request."""
        if pr.review_threads:
            self.conn.executemany(
                """
                INSERT INTO review_threads (
                  thread_id, repo_id, pr_number, file_path, line, start_line,
                  is_resolved, is_outdated, reviewer, author_association,
                  created_at, updated_at, body, comments_json, participants_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                  is_resolved = excluded.is_resolved,
                  is_outdated = excluded.is_outdated,
                  updated_at = excluded.updated_at,
                  body = excluded.body,
                  comments_json = excluded.comments_json,
                  participants_json = excluded.participants_json
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
                        rt.participants_json,
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

    def mark_timelines_synced(
        self,
        repo_id: int,
        targets: Sequence[tuple[str, int]],
        *,
        synced_at: str,
        xref_only: bool,
    ) -> None:
        """Records completed timeline fetches for a batch of `(item_kind, item_number)` pairs."""
        if not targets:
            return
        by_kind: dict[str, list[int]] = {}
        all_numbers: list[int] = []
        for item_kind, item_number in targets:
            table = _item_table(item_kind)
            by_kind.setdefault(table, []).append(int(item_number))
            all_numbers.append(int(item_number))
        assignments = "xref_synced_at = ?"
        base_params: list[Any] = [synced_at]
        if not xref_only:
            assignments += ", timeline_synced_at = ?"
            base_params.append(synced_at)
        with self.conn:
            for table, numbers in by_kind.items():
                marks = ", ".join("?" for _ in numbers)
                self.conn.execute(
                    f"UPDATE {table} SET {assignments} WHERE repo_id = ? AND number IN ({marks})",
                    (*base_params, repo_id, *numbers),
                )
            marks = ", ".join("?" for _ in all_numbers)
            self.conn.execute(
                f"""
                DELETE FROM timeline_dirty
                WHERE repo_id = ? AND queued_at <= ? AND item_number IN ({marks})
                """,
                (repo_id, synced_at, *all_numbers),
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

    def get_indexed_blob_oids(self, repo_id: int) -> dict[str, str]:
        """Returns `{file_path: blob_oid}` recorded for `repo_id` in `indexed_files`."""
        rows = self.conn.execute(
            "SELECT file_path, blob_oid FROM indexed_files WHERE repo_id = ?",
            (repo_id,),
        ).fetchall()
        return {str(r["file_path"]): str(r["blob_oid"]) for r in rows}

    def upsert_indexed_files(
        self, repo_id: int, files: Sequence[tuple[str, str, str]]
    ) -> None:
        """Upserts `(file_path, blob_oid, sha)` entries in `indexed_files`."""
        if not files:
            return
        with self.conn:
            self.conn.executemany(
                """
                INSERT OR REPLACE INTO indexed_files (repo_id, file_path, blob_oid, sha)
                VALUES (?, ?, ?, ?)
                """,
                [(repo_id, path, blob_oid, sha) for path, blob_oid, sha in files],
            )

    def _delete_code_index_for_paths_in_tx(
        self,
        repo_id: int,
        paths: Sequence[str],
        keep_chunk_ids: Sequence[str] = (),
    ) -> bool:
        """Deletes symbols, doc sections, indexed_files, and stale chunks/FTS5 rows for file paths.

        Returns True when at least one `chunks` row was deleted.
        """
        if not paths:
            return False
        marks = ", ".join("?" for _ in paths)
        params: tuple[Any, ...] = (repo_id, *paths)
        self.conn.execute(
            f"DELETE FROM indexed_files WHERE repo_id = ? AND file_path IN ({marks})",
            params,
        )
        self.conn.execute(
            f"DELETE FROM code_symbols WHERE repo_id = ? AND file_path IN ({marks})",
            params,
        )
        self.conn.execute(
            f"DELETE FROM doc_sections WHERE repo_id = ? AND file_path IN ({marks})",
            params,
        )
        keep_sql = ""
        if keep_chunk_ids:
            keep_marks = ", ".join("?" for _ in keep_chunk_ids)
            keep_sql = f" AND chunk_id NOT IN ({keep_marks})"
            params = (*params, *keep_chunk_ids)
        self.conn.execute(
            f"""
            DELETE FROM chunks_fts WHERE rowid IN (
                SELECT rowid FROM chunks
                WHERE repo_id = ? AND file_path IN ({marks})
                  AND source_kind IN ('code_symbol', 'doc_section'){keep_sql}
            )
            """,
            params,
        )
        cur = self.conn.execute(
            f"""
            DELETE FROM chunks
            WHERE repo_id = ? AND file_path IN ({marks})
              AND source_kind IN ('code_symbol', 'doc_section'){keep_sql}
            """,
            params,
        )
        return cur.rowcount > 0

    def _insert_code_symbols_in_tx(self, symbols: Sequence[CodeSymbolRecord]) -> None:
        """Inserts or replaces a batch of CodeSymbolRecords inside a transaction."""
        if not symbols:
            return
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO code_symbols (
              repo_id, file_path, symbol_name, kind,
              line_start, line_end, signature, docstring, sha, blob_oid
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    s.blob_oid or None,
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
              anchor_slug, line_start, line_end, subtree_line_end, content, sha, blob_oid
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    d.blob_oid or None,
                )
                for d in doc_sections
            ],
        )

    def _delete_code_index_batches(
        self,
        repo_id: int,
        affected: Sequence[str],
        chunks: Sequence[TextChunk],
    ) -> bool:
        """Deletes stale code index rows in file batches and returns whether any chunk was deleted."""
        keep_by_path: dict[str, list[str]] = {}
        for ch in chunks:
            if ch.file_path:
                keep_by_path.setdefault(ch.file_path, []).append(ch.chunk_id)
        deleted_any = False
        for start in range(0, len(affected), self._FILE_BATCH_SIZE):
            batch_paths = affected[start : start + self._FILE_BATCH_SIZE]
            keep_ids = [
                cid for path in batch_paths for cid in keep_by_path.get(path, ())
            ]
            with self.conn:
                if self._delete_code_index_for_paths_in_tx(
                    repo_id, batch_paths, keep_ids
                ):
                    deleted_any = True
        return deleted_any

    def _replace_code_index_batched(
        self,
        repo_id: int,
        affected: list[str],
        symbols: Sequence[CodeSymbolRecord],
        doc_sections: Sequence[DocSectionRecord],
        chunks: Sequence[TextChunk],
        *,
        now: float | None,
        reporter: Any,
        repo_label: str,
    ) -> None:
        """Executes multi-batch deletion, symbol/section insertion, and chunk upserts."""
        total_chunks = len(chunks)
        report_progress = (
            reporter is not None and total_chunks >= self._WRITE_BATCH_SIZE
        )
        if report_progress:
            reporter.start_phase(
                repo_label or f"repo:{repo_id}", "git index", total=total_chunks
            )
        deleted_any = self._delete_code_index_batches(repo_id, affected, chunks)
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
        changed_any = False
        for start in range(0, total_chunks, self._WRITE_BATCH_SIZE):
            batch_chunks = chunks[start : start + self._WRITE_BATCH_SIZE]
            with self.conn:
                if self._upsert_chunks_in_tx(batch_chunks, now=now):
                    changed_any = True
            if report_progress:
                done = min(total_chunks, start + len(batch_chunks))
                reporter.advance(
                    done, total_chunks, detail=batch_chunks[-1].file_path or ""
                )
        if deleted_any and not changed_any:
            with self.conn:
                self._bump_vector_cache_seq_in_tx(now)
        if report_progress:
            reporter.finish_phase()

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
            keep_ids = [ch.chunk_id for ch in chunks]
            with self.conn:
                deleted_any = self._delete_code_index_for_paths_in_tx(
                    repo_id, affected, keep_ids
                )
                self._insert_code_symbols_in_tx(symbols)
                self._insert_doc_sections_in_tx(doc_sections)
                changed_any = self._upsert_chunks_in_tx(chunks, now=now)
                if deleted_any and not changed_any:
                    self._bump_vector_cache_seq_in_tx(now)
            return
        self._replace_code_index_batched(
            repo_id,
            affected,
            symbols,
            doc_sections,
            chunks,
            now=now,
            reporter=reporter,
            repo_label=repo_label,
        )

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

    @staticmethod
    def _classify_existing_chunk(
        old: sqlite3.Row, ch: TextChunk, ts: str
    ) -> tuple[bool, bool, tuple[Any, ...] | None]:
        """Returns `(fts_changed, meta_changed, update_row)` for an existing chunk row."""
        fts_changed = (
            old["content_sha256"] != ch.content_sha256
            or old["title"] != ch.title
            or old["text"] != ch.text
        )
        untrusted_int = 1 if ch.is_untrusted else 0
        meta_changed = (
            old["source_ref"] != ch.source_ref
            or old["source_kind"] != ch.source_kind
            or old["file_path"] != ch.file_path
            or old["item_number"] != ch.item_number
            or int(old["is_untrusted"]) != untrusted_int
            or (bool(ch.updated_at) and old["updated_at"] != ch.updated_at)
        )
        if not fts_changed and not meta_changed:
            return False, False, None
        update_row = (
            ch.source_kind,
            ch.source_ref,
            ch.file_path,
            ch.item_number,
            ch.title,
            ch.content_sha256,
            untrusted_int,
            ch.text,
            ts,
            int(old["rowid"]),
        )
        return fts_changed, meta_changed, update_row

    def _apply_chunk_mutations_in_tx(
        self,
        to_insert: Sequence[tuple[Any, ...]],
        inserted_ids: Sequence[str],
        to_update: Sequence[tuple[Any, ...]],
        fts_deletes: Sequence[tuple[Any, ...]],
        fts_inserts: Sequence[tuple[Any, ...]],
    ) -> None:
        """Executes batched chunk and external-content FTS5 inserts, updates, and deletes."""
        if to_insert:
            self.conn.executemany(
                """
                INSERT INTO chunks (
                  chunk_id, repo_id, source_kind, source_ref, file_path,
                  item_number, title, content_sha256, is_untrusted, text, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                to_insert,
            )
            ins_marks = ", ".join("?" for _ in inserted_ids)
            self.conn.execute(
                f"""
                INSERT INTO chunks_fts (rowid, chunk_id, source_kind, source_ref, title, text)
                SELECT rowid, chunk_id, source_kind, source_ref, title, text
                FROM chunks WHERE chunk_id IN ({ins_marks})
                """,
                inserted_ids,
            )
        if fts_deletes:
            self.conn.executemany(
                """
                INSERT INTO chunks_fts (
                  chunks_fts, rowid, chunk_id, source_kind, source_ref, title, text
                ) VALUES ('delete', ?, ?, ?, ?, ?, ?)
                """,
                fts_deletes,
            )
        if to_update:
            self.conn.executemany(
                """
                UPDATE chunks SET
                  source_kind = ?,
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
                to_update,
            )
        if fts_inserts:
            self.conn.executemany(
                """
                INSERT INTO chunks_fts (rowid, chunk_id, source_kind, source_ref, title, text)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                fts_inserts,
            )

    def _partition_chunk_upserts(
        self,
        deduped: Sequence[TextChunk],
        by_id: dict[str, sqlite3.Row],
        now_iso: str,
    ) -> tuple[
        list[tuple[Any, ...]],
        list[str],
        list[tuple[Any, ...]],
        list[tuple[Any, ...]],
        list[tuple[Any, ...]],
    ]:
        """Partitions deduplicated chunks into insert, update, and FTS mutation lists."""
        to_insert: list[tuple[Any, ...]] = []
        inserted_ids: list[str] = []
        to_update: list[tuple[Any, ...]] = []
        fts_deletes: list[tuple[Any, ...]] = []
        fts_inserts: list[tuple[Any, ...]] = []

        for ch in deduped:
            ts = ch.updated_at or now_iso
            old = by_id.get(ch.chunk_id)
            if old is None:
                inserted_ids.append(ch.chunk_id)
                to_insert.append(
                    (
                        ch.chunk_id,
                        ch.repo_id,
                        ch.source_kind,
                        ch.source_ref,
                        ch.file_path,
                        ch.item_number,
                        ch.title,
                        ch.content_sha256,
                        int(ch.is_untrusted),
                        ch.text,
                        ts,
                    )
                )
                continue
            fts_changed, _, update_row = self._classify_existing_chunk(old, ch, ts)
            if update_row is not None:
                to_update.append(update_row)
            if fts_changed:
                rowid = int(old["rowid"])
                fts_deletes.append(
                    (
                        rowid,
                        str(old["chunk_id"]),
                        str(old["source_kind"]),
                        str(old["source_ref"]),
                        str(old["title"]),
                        str(old["text"]),
                    )
                )
                fts_inserts.append(
                    (
                        rowid,
                        ch.chunk_id,
                        ch.source_kind,
                        ch.source_ref,
                        ch.title,
                        ch.text,
                    )
                )
        return to_insert, inserted_ids, to_update, fts_deletes, fts_inserts

    def _upsert_chunks_in_tx(
        self, chunks: Sequence[TextChunk], *, now: float | None = None
    ) -> bool:
        """Upserts chunks and keeps `chunks_fts` synchronized inside a transaction.

        Returns True when at least one chunk was inserted or had its searchable
        content changed (and `vector_cache_seq` was bumped).
        """
        if not chunks:
            return False
        now_iso = iso_at(now)
        deduped = list({ch.chunk_id: ch for ch in chunks}.values())
        marks = ", ".join("?" for _ in deduped)
        existing_rows = self.conn.execute(
            f"""
            SELECT rowid, chunk_id, source_kind, source_ref, file_path, item_number,
                   title, content_sha256, is_untrusted, text, updated_at
            FROM chunks WHERE chunk_id IN ({marks})
            """,
            [ch.chunk_id for ch in deduped],
        ).fetchall()
        by_id = {str(r["chunk_id"]): r for r in existing_rows}
        to_insert, inserted_ids, to_update, fts_deletes, fts_inserts = (
            self._partition_chunk_upserts(deduped, by_id, now_iso)
        )
        self._apply_chunk_mutations_in_tx(
            to_insert, inserted_ids, to_update, fts_deletes, fts_inserts
        )
        content_changed = bool(to_insert or fts_inserts)
        if content_changed:
            self._bump_vector_cache_seq_in_tx(now)
        return content_changed

    def has_any_chunks(self, repo_ids: Sequence[int] | None = None) -> bool:
        """Returns whether at least one chunk row exists (optionally restricted to `repo_ids`)."""
        if repo_ids is None:
            return (
                self.conn.execute("SELECT 1 FROM chunks LIMIT 1").fetchone() is not None
            )
        if not repo_ids:
            return False
        marks = ", ".join("?" for _ in repo_ids)
        return (
            self.conn.execute(
                f"SELECT 1 FROM chunks WHERE repo_id IN ({marks}) LIMIT 1",
                list(repo_ids),
            ).fetchone()
            is not None
        )

    def get_unembedded_chunks(
        self,
        repo_id: int | None,
        model_id: str,
        *,
        limit: int | None = None,
        after_chunk_id: str | None = None,
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
        if after_chunk_id is not None:
            sql += " AND c.chunk_id > ?"
            params.append(after_chunk_id)
        sql += " ORDER BY c.chunk_id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
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
        allow_seq_bump: bool = True,
    ) -> None:
        """Upserts 384-dimensional embeddings as per-vector scaled offset uint8 BLOBs."""
        if not embeddings:
            return
        now_iso = iso_at(now)
        quantized = quantize_u8_matrix([e.vector for e in embeddings])
        rows = [
            (
                e.chunk_id,
                e.model_id,
                EMBEDDING_DIM,
                scale,
                blob,
                e.content_sha256,
                e.updated_at or now_iso,
            )
            for e, (blob, scale) in zip(embeddings, quantized, strict=True)
        ]
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
            if allow_seq_bump:
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

    # ----- search engine read queries ------------------------------------------------

    @staticmethod
    def build_search_filter_sql(filters: SearchFilters) -> tuple[str, list[Any]]:
        """Builds SQL WHERE predicates and bind parameters for `SearchFilters`."""
        clauses: list[str] = []
        params: list[Any] = []

        if filters.repo:
            clauses.append(
                "(LOWER(r.full_name) = LOWER(?) OR LOWER(r.name) = LOWER(?))"
            )
            params.extend([filters.repo, filters.repo])

        if filters.kinds:
            placeholders = ",".join("?" for _ in filters.kinds)
            clauses.append(f"c.source_kind IN ({placeholders})")
            params.extend(filters.kinds)

        if filters.path_prefix:
            clauses.append("(c.file_path >= ? AND c.file_path < ?)")
            params.extend([filters.path_prefix, f"{filters.path_prefix}\uffff"])

        if filters.state:
            clauses.append(
                """(
                  (c.source_kind = 'issue' AND EXISTS (
                    SELECT 1 FROM issues i
                    WHERE i.repo_id = c.repo_id AND i.number = c.item_number
                      AND UPPER(i.state) = UPPER(?)
                  ))
                  OR
                  (c.source_kind = 'pr' AND EXISTS (
                    SELECT 1 FROM pull_requests p
                    WHERE p.repo_id = c.repo_id AND p.number = c.item_number
                      AND UPPER(p.state) = UPPER(?)
                  ))
                )"""
            )
            params.extend([filters.state, filters.state])

        if not clauses:
            return "", params
        return " AND " + " AND ".join(clauses), params

    def query_fts_candidates(
        self,
        fts_expr: str,
        filters: SearchFilters,
        limit: int = 50,
    ) -> list[tuple[str, float]]:
        """Executes an FTS5 BM25 lexical search and returns `(chunk_id, raw_bm25)` rows."""
        if not fts_expr:
            return []
        filter_sql, filter_params = self.build_search_filter_sql(filters)
        sql = f"""
        SELECT
          c.chunk_id,
          bm25(chunks_fts, 1.0, 1.0, 1.5, 5.0, 1.0) AS raw_bm25
        FROM chunks_fts
        JOIN chunks c ON c.rowid = chunks_fts.rowid
        JOIN repos r ON r.repo_id = c.repo_id
        WHERE chunks_fts MATCH ?
        {filter_sql}
        ORDER BY raw_bm25 ASC
        LIMIT ?
        """
        rows = self.conn.execute(sql, [fts_expr, *filter_params, int(limit)]).fetchall()
        return [(str(r["chunk_id"]), float(r["raw_bm25"])) for r in rows]

    def read_all_embeddings_for_mmap(self) -> list[tuple[int, float, bytes, str]]:
        """Reads all valid `(rowid, scale, vector_u8, model_id)` rows ordered by `ce.rowid`."""
        rows = self.conn.execute(
            """
            SELECT ce.rowid, ce.scale, ce.vector_u8, ce.model_id
            FROM chunk_embeddings ce
            JOIN chunks c ON c.chunk_id = ce.chunk_id
            WHERE ce.content_sha256 = c.content_sha256
            ORDER BY ce.rowid ASC
            """
        ).fetchall()
        return [(int(r[0]), float(r[1]), bytes(r[2]), str(r[3] or "")) for r in rows]

    @classmethod
    def _filtered_embeddings_sql(
        cls,
        select_cols: str,
        filters: SearchFilters,
        model_id: str | None = None,
        *,
        order_by_rowid: bool = False,
    ) -> tuple[str, list[Any]]:
        """Builds the filtered `chunk_embeddings` SQL query and bind parameters."""
        filter_sql, filter_params = cls.build_search_filter_sql(filters)
        model_sql = " AND ce.model_id = ?" if model_id else ""
        sql_params = [model_id, *filter_params] if model_id else filter_params
        has_filters = bool(
            filters.repo or filters.kinds or filters.path_prefix or filters.state
        )
        if has_filters:
            from_sql = (
                "FROM repos r "
                "CROSS JOIN chunks c ON c.repo_id = r.repo_id "
                "CROSS JOIN chunk_embeddings ce ON ce.chunk_id = c.chunk_id"
            )
        else:
            from_sql = (
                "FROM chunk_embeddings ce JOIN chunks c ON c.chunk_id = ce.chunk_id"
            )
        order_sql = " ORDER BY ce.rowid ASC" if order_by_rowid else ""
        sql = (
            f"SELECT {select_cols} {from_sql} "
            f"WHERE ce.content_sha256 = c.content_sha256{model_sql}{filter_sql}{order_sql}"
        )
        return sql, sql_params

    def resolve_filtered_chunk_ids(
        self,
        filters: SearchFilters,
        model_id: str | None = None,
    ) -> list[int]:
        """Returns sorted `chunk_embeddings.rowid` values matching `filters` and `model_id`."""
        sql, params = self._filtered_embeddings_sql(
            "ce.rowid", filters, model_id, order_by_rowid=True
        )
        rows = self.conn.execute(sql, params).fetchall()
        return [int(r[0]) for r in rows]

    def get_embedding_chunk_ids_by_rowids(
        self, rowids: Sequence[int]
    ) -> dict[int, str]:
        """Maps `chunk_embeddings.rowid` values to `chunk_id` strings."""
        if not rowids:
            return {}
        placeholders = ",".join("?" for _ in rowids)
        rows = self.conn.execute(
            f"SELECT rowid, chunk_id FROM chunk_embeddings WHERE rowid IN ({placeholders})",
            [int(r) for r in rowids],
        ).fetchall()
        return {int(r[0]): str(r[1]) for r in rows}

    def stream_filtered_embeddings(
        self,
        filters: SearchFilters,
        model_id: str | None = None,
    ) -> Iterable[tuple[str, float, bytes]]:
        """Streams `(chunk_id, scale, vector_u8)` rows matching `filters` and `model_id`."""
        sql, params = self._filtered_embeddings_sql(
            "ce.chunk_id, ce.scale, ce.vector_u8", filters, model_id
        )
        for row in self.conn.execute(sql, params):
            yield str(row[0]), float(row[1]), bytes(row[2])

    def _is_source_db_alias(self, resolved_dest: Path, raw_dest: Path) -> bool:
        """Returns whether `resolved_dest` or `raw_dest` aliases `self.db_path` or its sidecars."""
        if str(self.db_path) == ":memory:":
            return False
        src_resolved = self.db_path.expanduser().resolve()
        forbidden = (
            src_resolved,
            Path(f"{src_resolved}-wal"),
            Path(f"{src_resolved}-shm"),
            Path(f"{src_resolved}-journal"),
        )
        if resolved_dest in forbidden:
            return True
        if not raw_dest.exists():
            return False
        return any(
            candidate.exists() and os.path.samefile(raw_dest, candidate)
            for candidate in forbidden
        )

    def _validate_export_destination(
        self,
        dest_path: Path | str,
        *,
        force: bool,
    ) -> Path:
        """Validates and resolves `dest_path` for `export_database`."""
        raw_str = str(dest_path).strip()
        if not raw_str or raw_str == ":memory:" or raw_str.startswith("file:"):
            raise ValueError("Export destination must be a regular filesystem path.")
        raw_dest = Path(raw_str).expanduser()
        if raw_dest.is_dir():
            raise ValueError(
                f"Export destination '{raw_dest}' is an existing directory."
            )
        resolved_dest = raw_dest.resolve()
        if self._is_source_db_alias(resolved_dest, raw_dest):
            raise ValueError("Destination path cannot be the active source database.")
        if (raw_dest.exists() or raw_dest.is_symlink()) and not force:
            raise ValueError(
                f"Export destination '{raw_dest}' already exists; pass --force to overwrite."
            )
        return resolved_dest

    def _resolve_export_repos(
        self,
        repo_ids: Sequence[int] | None,
    ) -> tuple[list[RepoConfig], list[int], int, int]:
        """Resolves kept mirror repositories, excluded IDs, and removed repo counts."""
        all_repos = self.list_repos()
        mirror_by_id = {repo.repo_id: repo for repo in all_repos if repo.is_mirror}
        removed_items_count = sum(1 for repo in all_repos if not repo.is_mirror)
        kept = _select_kept_mirror_repos(mirror_by_id, repo_ids)
        if not kept:
            raise ValueError("No mirror repositories matched for export.")
        kept_id_set = {repo.repo_id for repo in kept}
        excluded_ids = [
            repo.repo_id for repo in all_repos if repo.repo_id not in kept_id_set
        ]
        removed_mirror_count = len(mirror_by_id) - len(kept)
        return kept, excluded_ids, removed_items_count, removed_mirror_count

    def _backup_snapshot_to_conn(
        self,
        dest_conn: sqlite3.Connection,
        tracker: _ExportProgress,
    ) -> None:
        """Copies `self.conn` into `dest_conn` under a pinned WAL read snapshot."""
        with self.read_snapshot():
            page_size, est_bytes, _ = _estimate_conn_sizes(self.conn)
            est_pages = est_bytes // max(1, page_size)
            # Pin the WAL read mark so concurrent WAL commits never restart stepped backup.
            self.conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            total_pages_seen = est_pages

            def _on_step(_status: int, remaining: int, total: int) -> None:
                nonlocal total_pages_seen
                total_pages_seen = total
                tracker.advance_pages(max(0, total - remaining), total, page_size)

            init_detail = f"0 B / {format_bytes(est_bytes)}" if est_pages > 0 else ""
            tracker.begin("backup", total=est_pages or None, detail=init_detail)
            self.conn.backup(
                dest_conn,
                pages=_EXPORT_BACKUP_STEP_PAGES,
                sleep=0.0,
                progress=_on_step,
            )
        copied_bytes = total_pages_seen * page_size
        tracker.end(f"{format_bytes(copied_bytes)} ({total_pages_seen:,} pages)")

    def _scrub_export_conn_in_tx(
        self,
        dest_conn: sqlite3.Connection,
        kept_ids: Sequence[int],
        excluded_ids: Sequence[int],
        *,
        now: float | None,
        tracker: _ExportProgress,
    ) -> None:
        """Scrubs personal tables, excluded repositories, and local paths inside one transaction."""
        kept_placeholders = ",".join("?" for _ in kept_ids)
        kept_params = [int(rid) for rid in kept_ids]
        dest_conn.execute("PRAGMA foreign_keys = ON")
        tracker.begin("scrub", detail="step 1/5, pruning excluded FTS rows")
        with tracker.vm_heartbeat(dest_conn), dest_conn:
            if excluded_ids:
                excl_placeholders = ",".join("?" for _ in excluded_ids)
                excl_params = [int(rid) for rid in excluded_ids]
                # FTS5 external-content tables read old text from `chunks` during delete.
                dest_conn.execute(
                    f"DELETE FROM chunks_fts WHERE rowid IN ("
                    f"SELECT rowid FROM chunks WHERE repo_id IN ({excl_placeholders})"
                    f")",
                    excl_params,
                )
                tracker.step("step 2/5, cascading excluded repo rows")
                dest_conn.execute(
                    f"DELETE FROM repos WHERE repo_id IN ({excl_placeholders})",
                    excl_params,
                )
            tracker.step("step 3/5, clearing personal and session tables")
            for table in (
                "notifications",
                "viewer_sets",
                "watches",
                "refresh_requests",
                "changes",
                "api_calls",
                "service_heartbeat",
            ):
                dest_conn.execute(f"DELETE FROM {table}")
            dest_conn.execute("DELETE FROM sqlite_sequence WHERE name = 'changes'")
            tracker.step("step 4/5, filtering service_state")
            dest_conn.execute(
                f"""
                DELETE FROM service_state
                WHERE key != 'vector_cache_seq'
                  AND NOT (
                    (
                      key LIKE 'signals_events:%'
                      OR key LIKE 'events_last_id:%'
                      OR key LIKE 'refetch_open_prs:%'
                    )
                    AND CAST(substr(key, instr(key, ':') + 1) AS INTEGER) IN ({kept_placeholders})
                  )
                  AND NOT (
                    (key LIKE 'gap_cursor:%:%' OR key LIKE 'gap_floor:%:%')
                    AND CAST(
                      substr(
                        key,
                        instr(key, ':') + 1,
                        instr(substr(key, instr(key, ':') + 1), ':') - 1
                      ) AS INTEGER
                    ) IN ({kept_placeholders})
                  )
                """,
                [*kept_params, *kept_params],
            )
            self._bump_vector_cache_seq_in_tx(now, conn=dest_conn)
            tracker.step("step 5/5, clearing local checkout paths")
            dest_conn.execute("UPDATE repos SET local_path = NULL")
        tracker.end(
            f"kept {len(kept_ids)} mirror repo(s), removed {len(excluded_ids)} repo(s)"
        )

    def export_database(
        self,
        dest_path: Path | str,
        *,
        repo_ids: Sequence[int] | None = None,
        force: bool = False,
        on_progress: Callable[[str], None] | None = None,
        reporter: ProgressReporter | None = None,
        now: float | None = None,
    ) -> ExportSummary:
        """Exports a scrubbed, compacted SQLite snapshot for sharing across users.

        Args:
            dest_path: Target filesystem path for the exported SQLite database.
            repo_ids: Optional allowlist of mirror `repo_id` values to retain.
                When `None`, all registered `mirror` repositories are retained.
            force: Whether to overwrite an existing file at `dest_path`.
            on_progress: Optional phase callback (`"backup"`, `"scrub"`,
                `"vacuum"`, `"verify"`).
            reporter: Optional `ProgressReporter` receiving live page and VM updates.
            now: Optional UNIX timestamp override for deterministic tests.

        Returns:
            An `ExportSummary` describing the exported repositories and file sizes.

        Raises:
            ValueError: If `dest_path` aliases `self.db_path` (or its WAL/SHM
                sidecars), points to an existing directory, exists without
                `force=True`, or if `repo_ids` matches zero mirror repositories.
            ExportIntegrityError: If post-VACUUM FTS5 or foreign-key checks fail.
            sqlite3.DatabaseError: If SQLite backup, scrub, or VACUUM fails.
            OSError: If temporary file creation or `os.replace` fails.
        """
        started = time.monotonic()
        resolved_dest = self._validate_export_destination(dest_path, force=force)
        kept_repos, excluded_ids, removed_items, removed_mirror = (
            self._resolve_export_repos(repo_ids)
        )
        resolved_dest.parent.mkdir(parents=True, exist_ok=True)
        source_is_memory = str(self.db_path) == ":memory:"
        source_db_str = (
            ":memory:" if source_is_memory else str(self.db_path.expanduser().resolve())
        )
        source_bytes = (
            0
            if source_is_memory or not self.db_path.exists()
            else self.db_path.stat().st_size
        )
        tracker = _ExportProgress(reporter=reporter, on_progress=on_progress)
        tmp_path = resolved_dest.parent / f"{resolved_dest.name}.tmp.{os.getpid()}"
        _unlink_temp_export_files(tmp_path)
        dest_conn: sqlite3.Connection | None = None
        try:
            dest_conn = sqlite3.connect(str(tmp_path), timeout=30.0)
            self._backup_snapshot_to_conn(dest_conn, tracker)
            self._scrub_export_conn_in_tx(
                dest_conn,
                [repo.repo_id for repo in kept_repos],
                excluded_ids,
                now=now,
                tracker=tracker,
            )
            chunks_rem, emb_rem, schema_ver = _compact_and_verify_export_conn(
                dest_conn,
                tracker=tracker,
            )
            dest_conn.close()
            dest_conn = None
            os.replace(tmp_path, resolved_dest)
            _unlink_sqlite_sidecars(resolved_dest)
        finally:
            if dest_conn is not None:
                dest_conn.close()
            _unlink_temp_export_files(tmp_path)
        dest_bytes = resolved_dest.stat().st_size
        duration_sec = round(time.monotonic() - started, 3)
        return ExportSummary(
            source_db_path=source_db_str,
            dest_db_path=str(resolved_dest),
            schema_version=schema_ver,
            exported_repos=tuple(repo.full_name for repo in kept_repos),
            removed_items_repos=removed_items,
            removed_mirror_repos=removed_mirror,
            chunks_remaining=chunks_rem,
            embeddings_remaining=emb_rem,
            source_bytes=source_bytes,
            dest_bytes=dest_bytes,
            duration_sec=duration_sec,
        )
