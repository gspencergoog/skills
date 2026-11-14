"""Fresh-install baseline DDL for migration-1 tables in the packrat database.

`SCHEMA_SQL` defines only the core mirror tables that existed in migration 1
(keeping migration-2+ tables in `db_migrations.py`), while including the current
column and index layout so fresh installs avoid redundant column additions. The
frozen historical pre-migration DDL lives in `fixtures/schema_v1.sql`.
"""

from __future__ import annotations

try:
    from .models import BACKFILL_STREAMS, GIT_BACKFILL_COLUMNS
except ImportError:
    from models import BACKFILL_STREAMS, GIT_BACKFILL_COLUMNS  # type: ignore[no-redef]

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS repos (
  repo_id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner TEXT NOT NULL,
  name TEXT NOT NULL,
  full_name TEXT NOT NULL UNIQUE,
  local_path TEXT,
  default_branch TEXT NOT NULL DEFAULT 'main',
  events_etag TEXT,
  events_last_modified TEXT,
  poll_interval_sec INTEGER NOT NULL DEFAULT 60,
  last_events_check_at TEXT,
  issues_watermark TEXT,
  prs_watermark TEXT,
  discussions_watermark TEXT,
  last_full_sweep_at TEXT,
  last_indexed_sha TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  issues_backfill_cursor TEXT,
  issues_backfill_done_at TEXT,
  prs_backfill_cursor TEXT,
  prs_backfill_done_at TEXT,
  discussions_backfill_cursor TEXT,
  discussions_backfill_done_at TEXT
);

CREATE TABLE IF NOT EXISTS issues (
  repo_id INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  number INTEGER NOT NULL,
  node_id TEXT,
  state TEXT NOT NULL,
  author TEXT,
  author_association TEXT,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  labels_json TEXT NOT NULL DEFAULT '[]',
  assignees_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT,
  updated_at TEXT NOT NULL,
  closed_at TEXT,
  url TEXT,
  PRIMARY KEY (repo_id, number)
);
CREATE INDEX IF NOT EXISTS idx_issues_updated ON issues(repo_id, updated_at);

CREATE TABLE IF NOT EXISTS pull_requests (
  repo_id INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  number INTEGER NOT NULL,
  node_id TEXT,
  state TEXT NOT NULL,
  is_draft INTEGER NOT NULL DEFAULT 0,
  author TEXT,
  author_association TEXT,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  labels_json TEXT NOT NULL DEFAULT '[]',
  base_ref TEXT,
  head_ref TEXT,
  head_sha TEXT,
  merge_commit_sha TEXT,
  review_decision TEXT,
  mergeable TEXT,
  additions INTEGER NOT NULL DEFAULT 0,
  deletions INTEGER NOT NULL DEFAULT 0,
  changed_files INTEGER NOT NULL DEFAULT 0,
  created_at TEXT,
  updated_at TEXT NOT NULL,
  merged_at TEXT,
  closed_at TEXT,
  url TEXT,
  PRIMARY KEY (repo_id, number)
);
CREATE INDEX IF NOT EXISTS idx_prs_updated ON pull_requests(repo_id, updated_at);

CREATE TABLE IF NOT EXISTS pr_files (
  repo_id INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  pr_number INTEGER NOT NULL,
  file_path TEXT NOT NULL,
  change_type TEXT NOT NULL DEFAULT 'MODIFIED',
  additions INTEGER NOT NULL DEFAULT 0,
  deletions INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (repo_id, pr_number, file_path)
);
CREATE INDEX IF NOT EXISTS idx_pr_files_path ON pr_files(repo_id, file_path);

CREATE TABLE IF NOT EXISTS reviews (
  repo_id INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  review_id TEXT NOT NULL,
  pr_number INTEGER NOT NULL,
  author TEXT,
  author_association TEXT,
  state TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  submitted_at TEXT,
  PRIMARY KEY (repo_id, review_id)
);
CREATE INDEX IF NOT EXISTS idx_reviews_pr ON reviews(repo_id, pr_number);

CREATE TABLE IF NOT EXISTS review_threads (
  thread_id TEXT PRIMARY KEY,
  repo_id INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  pr_number INTEGER NOT NULL,
  file_path TEXT,
  line INTEGER,
  start_line INTEGER,
  is_resolved INTEGER NOT NULL DEFAULT 0,
  is_outdated INTEGER NOT NULL DEFAULT 0,
  reviewer TEXT,
  author_association TEXT,
  created_at TEXT,
  updated_at TEXT,
  body TEXT NOT NULL DEFAULT '',
  comments_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_review_threads_pr ON review_threads(repo_id, pr_number);
CREATE INDEX IF NOT EXISTS idx_review_threads_path ON review_threads(repo_id, file_path);

CREATE TABLE IF NOT EXISTS comments (
  repo_id INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  comment_id TEXT NOT NULL,
  parent_kind TEXT NOT NULL,
  parent_number INTEGER NOT NULL,
  author TEXT,
  author_association TEXT,
  body TEXT NOT NULL,
  created_at TEXT,
  updated_at TEXT,
  url TEXT,
  PRIMARY KEY (repo_id, comment_id)
);
CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments(repo_id, parent_kind, parent_number);

CREATE TABLE IF NOT EXISTS discussions (
  repo_id INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  number INTEGER NOT NULL,
  node_id TEXT,
  category TEXT,
  author TEXT,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  answer_body TEXT,
  created_at TEXT,
  updated_at TEXT NOT NULL,
  url TEXT,
  PRIMARY KEY (repo_id, number)
);

CREATE TABLE IF NOT EXISTS issue_pr_links (
  repo_id INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  issue_number INTEGER NOT NULL,
  pr_number INTEGER NOT NULL,
  link_type TEXT NOT NULL DEFAULT 'references',
  PRIMARY KEY (repo_id, issue_number, pr_number, link_type)
);

CREATE TABLE IF NOT EXISTS commits (
  repo_id INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  sha TEXT NOT NULL,
  author_name TEXT,
  author_email TEXT,
  committed_at TEXT NOT NULL,
  title TEXT NOT NULL,
  message TEXT NOT NULL DEFAULT '',
  pr_number INTEGER,
  PRIMARY KEY (repo_id, sha)
);
CREATE INDEX IF NOT EXISTS idx_commits_date ON commits(repo_id, committed_at);

CREATE TABLE IF NOT EXISTS commit_files (
  repo_id INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  sha TEXT NOT NULL,
  file_path TEXT NOT NULL,
  change_type TEXT NOT NULL DEFAULT 'M',
  old_path TEXT,
  additions INTEGER NOT NULL DEFAULT 0,
  deletions INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (repo_id, sha, file_path)
);
CREATE INDEX IF NOT EXISTS idx_commit_files_path ON commit_files(repo_id, file_path);

CREATE TABLE IF NOT EXISTS code_symbols (
  repo_id INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  file_path TEXT NOT NULL,
  symbol_name TEXT NOT NULL,
  qualified_name TEXT NOT NULL DEFAULT '',
  parent TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL,
  line_start INTEGER NOT NULL,
  line_end INTEGER NOT NULL,
  signature TEXT NOT NULL,
  docstring TEXT NOT NULL DEFAULT '',
  visibility TEXT NOT NULL DEFAULT '',
  sha TEXT NOT NULL,
  blob_oid TEXT,
  PRIMARY KEY (repo_id, file_path, kind, qualified_name, line_start)
);
CREATE INDEX IF NOT EXISTS idx_code_symbols_name ON code_symbols(repo_id, symbol_name);

CREATE TABLE IF NOT EXISTS doc_sections (
  repo_id INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  file_path TEXT NOT NULL,
  section_id INTEGER NOT NULL,
  level INTEGER NOT NULL,
  heading TEXT NOT NULL,
  breadcrumb TEXT NOT NULL,
  anchor_slug TEXT NOT NULL,
  line_start INTEGER NOT NULL,
  line_end INTEGER NOT NULL,
  subtree_line_end INTEGER NOT NULL,
  content TEXT NOT NULL,
  sha TEXT NOT NULL,
  PRIMARY KEY (repo_id, file_path, section_id)
);

CREATE TABLE IF NOT EXISTS chunks (
  chunk_id TEXT PRIMARY KEY,
  repo_id INTEGER NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
  source_kind TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  file_path TEXT,
  item_number INTEGER,
  title TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  is_untrusted INTEGER NOT NULL DEFAULT 1,
  text TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_repo_kind ON chunks(repo_id, source_kind);
CREATE INDEX IF NOT EXISTS idx_chunks_repo_file ON chunks(repo_id, file_path);
CREATE INDEX IF NOT EXISTS idx_chunks_sha ON chunks(content_sha256);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  chunk_id UNINDEXED,
  source_kind UNINDEXED,
  source_ref UNINDEXED,
  title,
  text,
  content='chunks',
  content_rowid='rowid',
  tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS chunk_embeddings (
  chunk_id TEXT PRIMARY KEY REFERENCES chunks(chunk_id) ON DELETE CASCADE,
  model_id TEXT NOT NULL,
  dim INTEGER NOT NULL DEFAULT 384,
  scale REAL NOT NULL DEFAULT 1.0,
  vector_u8 BLOB NOT NULL,
  content_sha256 TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""

BACKFILL_COLUMNS: tuple[str, ...] = tuple(
    f"{stream}_backfill_{suffix}"
    for stream in BACKFILL_STREAMS
    for suffix in ("cursor", "done_at")
)

ALL_BACKFILL_COLUMNS: tuple[str, ...] = (*BACKFILL_COLUMNS, *GIT_BACKFILL_COLUMNS)

BACKFILL_COLUMN_DECLS: tuple[tuple[str, str, str], ...] = tuple(
    ("repos", col, "TEXT") for col in BACKFILL_COLUMNS
)
"""Backfill columns that `repos` tables created by older packrat versions may lack."""

REPO_GITHUB_TABLES: tuple[str, ...] = (
    "issues",
    "pull_requests",
    "pr_files",
    "reviews",
    "review_threads",
    "comments",
    "discussions",
    "issue_pr_links",
    "timeline_events",
    "timeline_dirty",
    "pr_rollup",
    "check_runs",
    "review_requests",
    "latest_opinionated_reviews",
    "latest_reviews",
    "thread_activity",
)
"""`repo_id`-keyed GitHub mirror and live PR tables replaced as a unit by `packrat import`."""

REPO_GIT_TABLES: tuple[str, ...] = (
    "commits",
    "commit_files",
    "indexed_files",
    "code_symbols",
    "doc_sections",
)
"""`repo_id`-keyed local git index tables that `packrat import` may keep on GitHub-only sources."""

GIT_SOURCE_KINDS: tuple[str, ...] = ("commit", "code_symbol", "doc_section")
"""`chunks.source_kind` values produced by the local git indexer."""

PERSONAL_TABLES: tuple[str, ...] = (
    "notifications",
    "viewer_sets",
    "watches",
    "refresh_requests",
    "changes",
    "api_calls",
    "service_heartbeat",
)
"""Per-user and per-machine runtime tables scrubbed by export and never imported."""

REPO_STATE_KEY_PATTERNS: tuple[str, ...] = (
    "signals_events:",
    "events_last_id:",
    "refetch_open_prs:",
    "gap_cursor:",
    "gap_floor:",
)
"""`service_state` key prefixes followed by a `repo_id` (and optionally `:<stream>`)."""
