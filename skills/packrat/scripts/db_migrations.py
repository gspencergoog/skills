"""Versioned schema migrations and reader-view maintenance for the packrat database.

`apply_migrations()` brings a database to `LATEST_SCHEMA_VERSION`, running each
pending migration in its own `BEGIN IMMEDIATE` transaction, and then recreates
the reader views from `views.sql` only when a migration ran or the file's
SHA-256 changed. An up-to-date database is opened without any DDL, so
short-lived CLI calls take no write lock and don't invalidate prepared
statements held by read-only readers.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

try:
    from .db_schema import BACKFILL_COLUMN_DECLS, SCHEMA_SQL
    from .models import quantize_u8_vector, unpack_f32_vector
except ImportError:
    from db_schema import BACKFILL_COLUMN_DECLS, SCHEMA_SQL  # type: ignore[no-redef]
    from models import quantize_u8_vector, unpack_f32_vector  # type: ignore[no-redef]

VIEWS_SQL_PATH = Path(__file__).resolve().parent / "views.sql"

CONNECTION_PRAGMAS = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
"""

LIVE_GITHUB_SQL = """
CREATE TABLE IF NOT EXISTS pr_rollup (
  repo_id INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  pr_number INTEGER NOT NULL,
  head_oid TEXT NOT NULL,
  state TEXT,
  total INTEGER,
  checks_etag TEXT,
  status_etag TEXT,
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (repo_id, pr_number)
);
CREATE TABLE IF NOT EXISTS check_runs (
  repo_id INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  pr_number INTEGER NOT NULL,
  head_oid TEXT NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT,
  conclusion TEXT,
  details_url TEXT,
  started_at TEXT,
  completed_at TEXT,
  is_required INTEGER,
  workflow_run_id INTEGER,
  PRIMARY KEY (repo_id, pr_number, head_oid, kind, name)
);
CREATE TABLE IF NOT EXISTS review_requests (
  repo_id INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  pr_number INTEGER NOT NULL,
  reviewer TEXT NOT NULL,
  reviewer_kind TEXT NOT NULL,
  PRIMARY KEY (repo_id, pr_number, reviewer)
);
CREATE TABLE IF NOT EXISTS latest_opinionated_reviews (
  repo_id INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  pr_number INTEGER NOT NULL,
  author TEXT NOT NULL,
  author_type TEXT,
  state TEXT NOT NULL,
  submitted_at TEXT,
  PRIMARY KEY (repo_id, pr_number, author)
);
CREATE TABLE IF NOT EXISTS thread_activity (
  thread_id TEXT PRIMARY KEY,
  repo_id INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  pr_number INTEGER NOT NULL,
  is_resolved INTEGER,
  is_outdated INTEGER,
  last_author TEXT,
  last_author_type TEXT,
  last_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_thread_activity_pr ON thread_activity(repo_id, pr_number);
CREATE TABLE IF NOT EXISTS notifications (
  thread_id TEXT PRIMARY KEY,
  reason TEXT,
  subject_type TEXT,
  item_key TEXT,
  updated_at TEXT,
  unread INTEGER,
  seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS viewer_sets (
  set_name TEXT NOT NULL,
  item_key TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  PRIMARY KEY (set_name, item_key)
);
CREATE TABLE IF NOT EXISTS watches (
  consumer TEXT NOT NULL,
  consumer_id TEXT NOT NULL,
  item_key TEXT NOT NULL,
  freshness_s INTEGER NOT NULL,
  lease_expires_at TEXT NOT NULL,
  PRIMARY KEY (consumer, consumer_id, item_key)
);
CREATE INDEX IF NOT EXISTS idx_watches_expiry ON watches(lease_expires_at);
CREATE TABLE IF NOT EXISTS refresh_requests (
  item_key TEXT PRIMARY KEY,
  requested_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS changes (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  item_key TEXT NOT NULL,
  kind TEXT NOT NULL,
  fields TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_calls (
  at TEXT NOT NULL,
  worker TEXT NOT NULL,
  resource TEXT NOT NULL,
  cost INTEGER,
  remaining INTEGER,
  reset_at INTEGER,
  status INTEGER
);
CREATE INDEX IF NOT EXISTS idx_api_calls_at ON api_calls(at);
CREATE TABLE IF NOT EXISTS service_heartbeat (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  pid INTEGER,
  started_at TEXT,
  beat_at TEXT,
  version TEXT
);
CREATE TABLE IF NOT EXISTS service_state (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS views_meta (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  views_sha256 TEXT NOT NULL
);
"""

LIVE_GITHUB_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("repos", "mode", "TEXT NOT NULL DEFAULT 'mirror'"),
    ("pull_requests", "merge_state_status", "TEXT"),
    ("pull_requests", "auto_merge_enabled_at", "TEXT"),
    ("pull_requests", "head_committed_date", "TEXT"),
    ("pull_requests", "head_repo_owner", "TEXT"),
    ("pull_requests", "is_cross_repository", "INTEGER"),
    ("pull_requests", "author_type", "TEXT"),
    ("pull_requests", "live_fetched_at", "TEXT"),
    ("reviews", "author_type", "TEXT"),
    ("comments", "author_type", "TEXT"),
)
"""Columns added by migration 2. `pull_requests.node_id` already exists in the baseline."""

REFETCH_OPEN_PRS_KEY_PREFIX = "refetch_open_prs:"
"""`service_state` key prefix (plus repo_id) marking a pending one-time open-PR refetch."""


class SchemaTooNewError(RuntimeError):
    """Raised when a database was migrated by a newer packrat than this one."""


def _iter_statements(sql: str) -> Iterator[str]:
    """Splits a SQL script into complete statements."""
    buf: list[str] = []
    for line in sql.splitlines(keepends=True):
        buf.append(line)
        candidate = "".join(buf)
        if sqlite3.complete_statement(candidate):
            if candidate.strip():
                yield candidate
            buf = []
    tail = "".join(buf).strip()
    if tail and not all(
        ln.strip().startswith("--") for ln in tail.splitlines() if ln.strip()
    ):
        yield tail


def _execute_script(conn: sqlite3.Connection, sql: str) -> None:
    """Executes every statement in `sql` inside the caller's open transaction."""
    for stmt in _iter_statements(sql):
        conn.execute(stmt)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Returns the column names of `table`."""
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_missing_columns(
    conn: sqlite3.Connection, columns: tuple[tuple[str, str, str], ...]
) -> None:
    """Adds each `(table, column, decl)` that the table doesn't have yet."""
    for table, column, decl in columns:
        if column not in _table_columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def migrate_0001_baseline(conn: sqlite3.Connection) -> None:
    """Creates the pre-migration schema and adds backfill columns old databases lack."""
    _execute_script(conn, SCHEMA_SQL)
    _add_missing_columns(conn, BACKFILL_COLUMN_DECLS)


def migrate_0002_live_github(conn: sqlite3.Connection) -> None:
    """Adds live GitHub tables and columns, and queues the open-PR fork-field refetch."""
    _add_missing_columns(conn, LIVE_GITHUB_COLUMNS)
    _execute_script(conn, LIVE_GITHUB_SQL)
    conn.execute(
        """
        INSERT OR REPLACE INTO service_state (key, value, updated_at)
        SELECT ? || repo_id, '1', strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
        FROM repos WHERE mode = 'mirror'
        """,
        (REFETCH_OPEN_PRS_KEY_PREFIX,),
    )


CHECK_RUNS_V3_SQL = """
CREATE TABLE check_runs_v3 (
  repo_id INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  pr_number INTEGER NOT NULL,
  head_oid TEXT NOT NULL,
  run_key TEXT NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT,
  conclusion TEXT,
  details_url TEXT,
  started_at TEXT,
  completed_at TEXT,
  is_required INTEGER,
  workflow_run_id INTEGER,
  PRIMARY KEY (repo_id, pr_number, head_oid, run_key)
);
INSERT INTO check_runs_v3 (
  repo_id, pr_number, head_oid, run_key, name, kind, status, conclusion, details_url,
  started_at, completed_at, is_required, workflow_run_id
)
SELECT repo_id, pr_number, head_oid, kind || ':' || name, name, kind, status, conclusion,
  details_url, started_at, completed_at, is_required, workflow_run_id
FROM check_runs;
DROP VIEW IF EXISTS v_pr_checks;
DROP TABLE check_runs;
ALTER TABLE check_runs_v3 RENAME TO check_runs;
"""


def migrate_0003_check_run_keys(conn: sqlite3.Connection) -> None:
    """Re-keys `check_runs` by `run_key` so same-named check runs are all kept.

    Version 2 keyed rows by `(head_oid, kind, name)`, so re-runs or the same
    job name in two workflows collapsed into one row and a failing duplicate
    could hide behind a passing one. Existing rows get `run_key = kind:name`;
    the hot worker writes `CheckRun:<databaseId>` from then on. The reader view
    is dropped here and recreated from views.sql after the migration.
    """
    _execute_script(conn, CHECK_RUNS_V3_SQL)


READER_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_prs_live ON pull_requests(repo_id, number)
  WHERE live_fetched_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_changes_at ON changes(at);
"""


def migrate_0004_reader_indexes(conn: sqlite3.Connection) -> None:
    """Indexes live PRs (for `v_pr_live`) and `changes.at` (for `prune()`)."""
    _execute_script(conn, READER_INDEXES_SQL)


CHUNK_EMBEDDINGS_V5_DDL = """
CREATE TABLE chunk_embeddings_v5 (
  chunk_id TEXT PRIMARY KEY REFERENCES chunks(chunk_id) ON DELETE CASCADE,
  model_id TEXT NOT NULL,
  dim INTEGER NOT NULL DEFAULT 384,
  scale REAL NOT NULL DEFAULT 1.0,
  vector_u8 BLOB NOT NULL,
  content_sha256 TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


def _quantize_legacy_f32_row(
    row: sqlite3.Row | tuple[object, ...],
) -> tuple[str, str, int, float, bytes, str, str] | None:
    """Converts one legacy vector_f32 row into a quantized v5 tuple, or None if corrupt."""
    try:
        vec = unpack_f32_vector(bytes(row[3]))  # type: ignore[arg-type]
        u8_blob, scale = quantize_u8_vector(vec)
    except (ValueError, TypeError):
        return None
    return (
        str(row[0]),
        str(row[1]),
        int(row[2]),  # type: ignore[arg-type]
        scale,
        u8_blob,
        str(row[4]),
        str(row[5]),
    )


def _copy_legacy_f32_batches(conn: sqlite3.Connection) -> None:
    """Converts legacy vector_f32 rows into chunk_embeddings_v5 in 2,000-row batches."""
    cursor = conn.execute(
        """
        SELECT chunk_id, model_id, dim, vector_f32, content_sha256, updated_at
        FROM chunk_embeddings
        ORDER BY rowid ASC
        """
    )
    while True:
        batch = cursor.fetchmany(2000)
        if not batch:
            break
        converted = [
            item for row in batch if (item := _quantize_legacy_f32_row(row)) is not None
        ]
        if converted:
            conn.executemany(
                """
                INSERT INTO chunk_embeddings_v5 (
                  chunk_id, model_id, dim, scale, vector_u8, content_sha256, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                converted,
            )


def migrate_0005_quantized_embeddings(conn: sqlite3.Connection) -> None:
    """Rebuilds `chunk_embeddings` with per-vector scaled offset `uint8` blobs (`388 B/row`).

    Converts existing `vector_f32` rows in place in batches of 2,000 and drops
    `vector_f32` and `vector_json`. Fresh databases created with the v5
    `SCHEMA_SQL` already have `vector_u8` and `scale`, so they return early.
    """
    cols = _table_columns(conn, "chunk_embeddings")
    if "vector_u8" in cols and "vector_f32" not in cols:
        return
    conn.execute("DROP TABLE IF EXISTS chunk_embeddings_v5")
    conn.execute(CHUNK_EMBEDDINGS_V5_DDL)
    if "vector_f32" in cols:
        _copy_legacy_f32_batches(conn)
    conn.execute("DROP TABLE chunk_embeddings")
    conn.execute("ALTER TABLE chunk_embeddings_v5 RENAME TO chunk_embeddings")
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'service_state'"
    ).fetchone():
        conn.execute(
            """
            INSERT INTO service_state (key, value, updated_at)
            VALUES ('vector_cache_seq', '1', '')
            ON CONFLICT(key) DO UPDATE SET
              value = CAST(CAST(service_state.value AS INTEGER) + 1 AS TEXT)
            """
        )


REPO_ALIASES_SQL = """
CREATE TABLE IF NOT EXISTS repo_aliases (
  alias_full_name TEXT PRIMARY KEY COLLATE NOCASE,
  repo_id INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_repo_aliases_repo_id ON repo_aliases(repo_id);
"""


def migrate_0006_repo_aliases(conn: sqlite3.Connection) -> None:
    """Adds `repo_aliases` for repository rename and redirect resolution."""
    _execute_script(conn, REPO_ALIASES_SQL)


TIMELINE_SQL = """
CREATE TABLE IF NOT EXISTS timeline_events (
  event_id TEXT PRIMARY KEY,
  repo_id INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  item_kind TEXT NOT NULL,
  item_number INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  created_at TEXT NOT NULL,
  actor TEXT,
  actor_type TEXT,
  subject TEXT,
  subject_type TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_timeline_item
  ON timeline_events(repo_id, item_kind, item_number, created_at);
CREATE INDEX IF NOT EXISTS idx_timeline_type ON timeline_events(repo_id, event_type, created_at);
CREATE INDEX IF NOT EXISTS idx_timeline_subject ON timeline_events(subject);
CREATE TABLE IF NOT EXISTS timeline_dirty (
  repo_id INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  item_number INTEGER NOT NULL,
  reason TEXT NOT NULL,
  queued_at TEXT NOT NULL,
  PRIMARY KEY (repo_id, item_number)
);
CREATE INDEX IF NOT EXISTS idx_prs_timeline_pending ON pull_requests(repo_id)
  WHERE timeline_synced_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_issues_timeline_pending ON issues(repo_id)
  WHERE timeline_synced_at IS NULL;
"""

TIMELINE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("pull_requests", "timeline_synced_at", "TEXT"),
    ("pull_requests", "xref_synced_at", "TEXT"),
    ("issues", "timeline_synced_at", "TEXT"),
    ("issues", "xref_synced_at", "TEXT"),
)
"""Per-item timeline progress columns added by migration 7."""


def migrate_0007_timeline_events(conn: sqlite3.Connection) -> None:
    """Adds `timeline_events`, the `timeline_dirty` queue, and per-item timeline progress.

    `timeline_synced_at IS NULL` means an item's timeline has never been
    fetched, so the partial indexes keep backfill candidate scans cheap.
    """
    _add_missing_columns(conn, TIMELINE_COLUMNS)
    _execute_script(conn, TIMELINE_SQL)


LATEST_REVIEWS_SQL = """
CREATE TABLE IF NOT EXISTS latest_reviews (
  repo_id INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  pr_number INTEGER NOT NULL,
  author TEXT NOT NULL,
  author_type TEXT,
  state TEXT NOT NULL,
  submitted_at TEXT,
  commit_oid TEXT,
  PRIMARY KEY (repo_id, pr_number, author)
);
"""


def migrate_0008_latest_reviews(conn: sqlite3.Connection) -> None:
    """Adds `latest_reviews` storing the latest non-pending review per author and commit OID."""
    _execute_script(conn, LATEST_REVIEWS_SQL)


Migration = Callable[[sqlite3.Connection], None]

MIGRATIONS: tuple[tuple[int, Migration], ...] = (
    (1, migrate_0001_baseline),
    (2, migrate_0002_live_github),
    (3, migrate_0003_check_run_keys),
    (4, migrate_0004_reader_indexes),
    (5, migrate_0005_quantized_embeddings),
    (6, migrate_0006_repo_aliases),
    (7, migrate_0007_timeline_events),
    (8, migrate_0008_latest_reviews),
)

LATEST_SCHEMA_VERSION = MIGRATIONS[-1][0]


def read_schema_version(conn: sqlite3.Connection) -> int:
    """Returns the applied schema version, or 0 when `schema_version` doesn't exist."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if exists is None:
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def views_sha256(path: Path = VIEWS_SQL_PATH) -> str:
    """Returns the SHA-256 hex digest of the reader views script."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stored_views_sha256(conn: sqlite3.Connection) -> str | None:
    """Returns the views hash recorded in `views_meta`, or None before migration 2."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'views_meta'"
    ).fetchone()
    if exists is None:
        return None
    row = conn.execute("SELECT views_sha256 FROM views_meta WHERE id = 1").fetchone()
    return str(row[0]) if row else None


def _run_migration(conn: sqlite3.Connection, version: int, migrate: Migration) -> bool:
    """Runs one migration in its own write transaction unless another process already did.

    Returns:
        True when this call applied the migration.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        if read_schema_version(conn) >= version:
            conn.execute("ROLLBACK")
            return False
        migrate(conn)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version "
            "(version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) "
            "VALUES (?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))",
            (version,),
        )
        conn.execute("COMMIT")
        return True
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def refresh_views(conn: sqlite3.Connection, path: Path = VIEWS_SQL_PATH) -> None:
    """Recreates every reader view from `path` and records its hash, in one transaction."""
    digest = views_sha256(path)
    conn.execute("BEGIN IMMEDIATE")
    try:
        _execute_script(conn, path.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT OR REPLACE INTO views_meta (id, views_sha256) VALUES (1, ?)",
            (digest,),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def apply_migrations(
    conn: sqlite3.Connection, views_path: Path = VIEWS_SQL_PATH
) -> list[int]:
    """Brings `conn`'s database to `LATEST_SCHEMA_VERSION` and refreshes stale views.

    Args:
        conn: Connection in autocommit-compatible state (no open transaction).
        views_path: Reader views script, overridable for tests.

    Returns:
        The migration versions this call applied (empty when already current).

    Raises:
        SchemaTooNewError: If the database's schema version is newer than
            `LATEST_SCHEMA_VERSION`.
    """
    current = read_schema_version(conn)
    if current > LATEST_SCHEMA_VERSION:
        raise SchemaTooNewError(
            f"packrat database schema version {current} is newer than this packrat supports "
            f"({LATEST_SCHEMA_VERSION}); update packrat (git pull) before opening it."
        )
    applied = [
        version
        for version, migrate in MIGRATIONS
        if version > current and _run_migration(conn, version, migrate)
    ]
    if applied or _stored_views_sha256(conn) != views_sha256(views_path):
        refresh_views(conn, views_path)
    return applied
