"""SQLite WAL relational, FTS5 BM25, and vector store for packrat."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

try:
    from .models import (
        EMBEDDING_DIM,
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
        format_vector_json,
        pack_f32_vector,
        unpack_f32_vector,
        utc_now_iso,
    )
except ImportError:
    from models import (  # type: ignore[no-redef]
        EMBEDDING_DIM,
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
        format_vector_json,
        pack_f32_vector,
        unpack_f32_vector,
        utc_now_iso,
    )

DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "packrat"
DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "repos.sqlite"
DEFAULT_INIT_SQL_PATH = DEFAULT_DATA_DIR / "init.sql"
_TEMPLATE_SQL_PATH = Path(__file__).resolve().parent / "init.sql"

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

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
  updated_at TEXT NOT NULL
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
  kind TEXT NOT NULL,
  line_start INTEGER NOT NULL,
  line_end INTEGER NOT NULL,
  signature TEXT NOT NULL,
  docstring TEXT NOT NULL DEFAULT '',
  sha TEXT NOT NULL,
  PRIMARY KEY (repo_id, file_path, kind, symbol_name, line_start)
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
CREATE INDEX IF NOT EXISTS idx_chunks_sha ON chunks(content_sha256);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  chunk_id UNINDEXED,
  source_kind UNINDEXED,
  source_ref UNINDEXED,
  title,
  text,
  tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS chunk_embeddings (
  chunk_id TEXT PRIMARY KEY REFERENCES chunks(chunk_id) ON DELETE CASCADE,
  model_id TEXT NOT NULL,
  dim INTEGER NOT NULL DEFAULT 384,
  vector_f32 BLOB NOT NULL,
  vector_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


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


class PackratStore:
    """Manages the SQLite WAL relational, FTS5, and vector database."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)

    def close(self) -> None:
        """Closes the underlying SQLite connection."""
        self.conn.close()

    def write_init_sql(self, init_sql_path: Path | None = None) -> Path:
        """Writes the rendered DuckDB init.sql file to disk."""
        target = Path(init_sql_path) if init_sql_path is not None else (self.db_path.parent / "init.sql")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_init_sql(self.db_path), encoding="utf-8")
        return target

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
                "DELETE FROM chunks_fts WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE repo_id = ?)",
                (repo.repo_id,),
            )
            self.conn.execute("DELETE FROM repos WHERE repo_id = ?", (repo.repo_id,))
        return True

    def rename_repo(self, old_full_name: str, new_owner: str, new_name: str) -> RepoConfig:
        """Renames a tracked repository in place, keeping its synced rows and embeddings.

        Use this after a GitHub repository is transferred or renamed. Chunk IDs and
        source references embed the full name, so their prefixes are rewritten too.
        The events ETag is cleared because the events URL changes with the name.

        Raises:
            ValueError: if `old_full_name` is not registered or the new name is taken.
        """
        repo = self.get_repo(old_full_name)
        if repo is None:
            raise ValueError(f"Repository '{old_full_name}' is not registered in packrat.")
        new_full_name = f"{new_owner}/{new_name}"
        existing = self.get_repo(new_full_name)
        if existing is not None and existing.repo_id != repo.repo_id:
            raise ValueError(f"Repository '{new_full_name}' is already registered in packrat.")
        old_prefix = repo.full_name
        start = len(old_prefix) + 1
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
                (new_owner, new_name, new_full_name, utc_now_iso(), repo.repo_id),
            )
            # Rewrite dependent tables first, while chunks still identifies this
            # repo's rows by their old IDs.
            repo_chunk_ids = "SELECT chunk_id FROM chunks WHERE repo_id = ?"
            self.conn.execute(
                f"UPDATE chunk_embeddings SET chunk_id = ? || substr(chunk_id, ?) WHERE chunk_id IN ({repo_chunk_ids})",
                (new_full_name, start, repo.repo_id),
            )
            self.conn.execute(
                f"""
                UPDATE chunks_fts SET
                  chunk_id = ? || substr(chunk_id, ?),
                  source_ref = CASE WHEN substr(source_ref, 1, ?) = ? THEN ? || substr(source_ref, ?) ELSE source_ref END
                WHERE chunk_id IN ({repo_chunk_ids})
                """,
                (new_full_name, start, len(old_prefix), old_prefix, new_full_name, start, repo.repo_id),
            )
            for column in ("chunk_id", "source_ref"):
                self.conn.execute(
                    f"""
                    UPDATE chunks SET {column} = ? || substr({column}, ?)
                    WHERE repo_id = ? AND substr({column}, 1, ?) = ?
                    """,
                    (new_full_name, start, repo.repo_id, len(old_prefix), old_prefix),
                )
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
    ) -> RepoConfig:
        """Inserts or updates a tracked repository configuration."""
        now = utc_now_iso()
        full_name = f"{owner}/{name}"
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO repos (owner, name, full_name, local_path, default_branch, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(full_name) DO UPDATE SET
                  local_path = COALESCE(excluded.local_path, repos.local_path),
                  default_branch = COALESCE(excluded.default_branch, repos.default_branch),
                  updated_at = excluded.updated_at
                """,
                (owner, name, full_name, local_path, default_branch, now, now),
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
    ) -> None:
        """Updates non-None watermark and ETag columns for repo_id."""
        now = utc_now_iso()
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
                    now,
                    repo_id,
                ),
            )

    def get_repo(self, full_name: str) -> RepoConfig | None:
        """Looks up a repository by full_name (case-insensitively)."""
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

    def list_repos(self) -> list[RepoConfig]:
        """Returns all registered repositories ordered by repo_id."""
        rows = self.conn.execute("SELECT * FROM repos ORDER BY repo_id ASC").fetchall()
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
        self.conn.execute(
            """
            INSERT INTO pull_requests (
              repo_id, number, node_id, state, is_draft, author, author_association,
              title, body, labels_json, base_ref, head_ref, head_sha, merge_commit_sha,
              review_decision, mergeable, additions, deletions, changed_files,
              created_at, updated_at, merged_at, closed_at, url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
              url = excluded.url
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
                    (f.repo_id, f.pr_number, f.file_path, f.change_type, f.additions, f.deletions)
                    for f in pr.files
                ],
            )
        if pr.reviews:
            self.conn.executemany(
                """
                INSERT INTO reviews (
                  repo_id, review_id, pr_number, author, author_association, state, body, submitted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo_id, review_id) DO UPDATE SET
                  state = excluded.state,
                  body = excluded.body,
                  submitted_at = excluded.submitted_at
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
        self.conn.execute(
            "DELETE FROM issue_pr_links WHERE repo_id = ? AND pr_number = ?",
            (pr.repo_id, pr.number),
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

    def replace_code_index_for_files(
        self,
        repo_id: int,
        changed_paths: Sequence[str],
        deleted_paths: Sequence[str],
        symbols: Sequence[CodeSymbolRecord],
        doc_sections: Sequence[DocSectionRecord],
        chunks: Sequence[TextChunk],
    ) -> None:
        """Replaces code symbols, doc sections, and chunks for changed or deleted files."""
        affected = list(dict.fromkeys([*changed_paths, *deleted_paths]))
        with self.conn:
            for rel_path in affected:
                self.conn.execute(
                    "DELETE FROM code_symbols WHERE repo_id = ? AND file_path = ?",
                    (repo_id, rel_path),
                )
                self.conn.execute(
                    "DELETE FROM doc_sections WHERE repo_id = ? AND file_path = ?",
                    (repo_id, rel_path),
                )
                old_chunk_rows = self.conn.execute(
                    """
                    SELECT chunk_id FROM chunks
                    WHERE repo_id = ? AND file_path = ?
                      AND source_kind IN ('code_symbol', 'doc_section')
                    """,
                    (repo_id, rel_path),
                ).fetchall()
                for row in old_chunk_rows:
                    cid = str(row["chunk_id"])
                    self.conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (cid,))
                    self.conn.execute("DELETE FROM chunks WHERE chunk_id = ?", (cid,))

            if symbols:
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
            if doc_sections:
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
            self._upsert_chunks_in_tx(chunks)

    def upsert_chunks(self, chunks: Sequence[TextChunk]) -> None:
        """Upserts searchable text chunks and synchronizes the FTS5 index."""
        if not chunks:
            return
        with self.conn:
            self._upsert_chunks_in_tx(chunks)

    def _upsert_chunks_in_tx(self, chunks: Sequence[TextChunk]) -> None:
        """Upserts chunks and keeps chunks_fts synchronized inside a transaction."""
        if not chunks:
            return
        now = utc_now_iso()
        for ch in chunks:
            ts = ch.updated_at or now
            self.conn.execute(
                """
                INSERT INTO chunks (
                  chunk_id, repo_id, source_kind, source_ref, file_path,
                  item_number, title, content_sha256, is_untrusted, text, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                  source_ref = excluded.source_ref,
                  file_path = excluded.file_path,
                  item_number = excluded.item_number,
                  title = excluded.title,
                  content_sha256 = excluded.content_sha256,
                  is_untrusted = excluded.is_untrusted,
                  text = excluded.text,
                  updated_at = excluded.updated_at
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
            self.conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (ch.chunk_id,))
            self.conn.execute(
                """
                INSERT INTO chunks_fts (chunk_id, source_kind, source_ref, title, text)
                VALUES (?, ?, ?, ?, ?)
                """,
                (ch.chunk_id, ch.source_kind, ch.source_ref, ch.title, ch.text),
            )

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

    def upsert_embeddings(self, embeddings: Sequence[StoredEmbeddingRecord]) -> None:
        """Upserts 384-dimensional float32 BLOB and JSON embeddings for chunks."""
        if not embeddings:
            return
        now = utc_now_iso()
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO chunk_embeddings (
                  chunk_id, model_id, dim, vector_f32, vector_json, content_sha256, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                  model_id = excluded.model_id,
                  dim = excluded.dim,
                  vector_f32 = excluded.vector_f32,
                  vector_json = excluded.vector_json,
                  content_sha256 = excluded.content_sha256,
                  updated_at = excluded.updated_at
                """,
                [
                    (
                        e.chunk_id,
                        e.model_id,
                        EMBEDDING_DIM,
                        pack_f32_vector(e.vector),
                        format_vector_json(e.vector),
                        e.content_sha256,
                        e.updated_at or now,
                    )
                    for e in embeddings
                ],
            )

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
            vector=unpack_f32_vector(bytes(row["vector_f32"])),
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
            emb_row = self.conn.execute("SELECT COUNT(*) AS n FROM chunk_embeddings").fetchone()
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
