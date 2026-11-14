# `packrat` Schema and DuckDB SQL Recipes

## SQLite WAL Schema (`~/.local/share/packrat/repos.sqlite`)

Content tables carry a `repo_id` column (foreign key to `repos.repo_id`, deleted with the repository). Embedding, change-feed, and notification tables are keyed by `chunk_id`, `seq`, or `thread_id` instead and reach a repository only through a join. Schema migrations are recorded in `schema_version`; `packrat` refuses to open a database whose version is newer than the code knows.

| Table | Primary Key | Columns |
| :--- | :--- | :--- |
| `repos` | `repo_id` | `owner`, `name`, `full_name`, `mode` (`mirror` or `items`), `local_path`, `git_source` (`none`, `path`, or `managed`), `git_url`, `default_branch`, `events_etag`, `events_last_modified`, `poll_interval_sec`, `last_events_check_at`, `issues_watermark`, `prs_watermark`, `discussions_watermark`, `last_full_sweep_at`, `last_indexed_sha`, `parser_fingerprint`, `created_at`, `updated_at`, `issues_backfill_cursor`, `issues_backfill_done_at`, `prs_backfill_cursor`, `prs_backfill_done_at`, `discussions_backfill_cursor`, `discussions_backfill_done_at`, `commits_backfill_cursor`, `commits_backfill_done_at` (older commits still to index when a repository has more than one run's worth) |
| `repo_aliases` | `alias_full_name` | `repo_id`, `created_at`. Former names of renamed or transferred repositories, so old item keys still resolve |
| `schema_version` | `version` | `applied_at` |
| `views_meta` | `id` (always `1`) | `views_sha256`, `views_version`. Identifies the installed reader views |
| `issues` | `(repo_id, number)` | `node_id`, `state`, `author`, `author_association`, `title`, `body`, `labels_json`, `assignees_json`, `created_at`, `updated_at`, `closed_at`, `url`, `timeline_synced_at`, `xref_synced_at` |
| `pull_requests` | `(repo_id, number)` | `node_id`, `state`, `is_draft`, `author`, `author_association`, `title`, `body`, `labels_json`, `base_ref`, `head_ref`, `head_sha`, `merge_commit_sha`, `review_decision`, `mergeable`, `additions`, `deletions`, `changed_files`, `created_at`, `updated_at`, `merged_at`, `closed_at`, `url`, plus the live columns `merge_state_status`, `auto_merge_enabled_at`, `head_committed_date`, `head_repo_owner`, `is_cross_repository`, `author_type`, `live_fetched_at`, plus `timeline_synced_at` and `xref_synced_at` |
| `pr_files` | `(repo_id, pr_number, file_path)` | `change_type`, `additions`, `deletions` |
| `reviews` | `(repo_id, review_id)` | `pr_number`, `author`, `author_association`, `author_type` (`User`, `Bot`, `Mannequin`, or `Organization`; filled by mirror syncs and the live service), `state`, `body`, `submitted_at` |
| `review_threads` | `thread_id` | `repo_id`, `pr_number`, `file_path`, `line`, `start_line`, `is_resolved`, `is_outdated`, `reviewer`, `author_association`, `created_at`, `updated_at`, `body`, `comments_json`, `participants_json` (logins of everyone who commented in the thread) |
| `comments` | `(repo_id, comment_id)` | `parent_kind` (`issue` or `pr`), `parent_number`, `author`, `author_association`, `author_type`, `body`, `created_at`, `updated_at`, `url` |
| `issue_pr_links` | `(repo_id, issue_number, pr_number, link_type)` | `link_type`: `closes` or `references` from PR text, `closed_by` from an issue's `ClosedEvent` closer, `xref` from cross-reference and connect events. Re-saving a PR replaces only its text-derived rows |
| `discussions` | `(repo_id, number)` | `node_id`, `category`, `author`, `title`, `body`, `answer_body`, `created_at`, `updated_at`, `url` |
| `commits` | `(repo_id, sha)` | `author_name`, `author_email`, `committed_at` (UTC, `Z` suffix), `title`, `message`, `pr_number` |
| `commit_files` | `(repo_id, sha, file_path)` | `change_type`, `old_path`, `additions`, `deletions` |
| `timeline_events` | `event_id` (GraphQL node id) | `repo_id`, `item_kind` (`pr` or `issue`), `item_number`, `event_type` (GraphQL `__typename`, such as `ReviewRequestedEvent`), `created_at`, `actor`, `actor_type`, `subject`, `subject_type`, `payload_json` |
| `timeline_dirty` | `(repo_id, item_number)` | `reason` (`mention` or `commit`), `queued_at`. Items another item or commit mentioned, waiting for a cross-reference fetch |
| `indexed_files` | `(repo_id, file_path)` | `blob_oid`, `sha`, `parser_id`. The git blob and symbol parser recorded per indexed file so unchanged blobs with current parsers are skipped on the next index run |
| `code_symbols` | `(repo_id, file_path, kind, qualified_name, line_start)` | `symbol_name`, `parent`, `line_end`, `signature`, `docstring`, `visibility`, `sha`, `blob_oid` |
| `doc_sections` | `(repo_id, file_path, section_id)` | `level`, `heading`, `breadcrumb`, `anchor_slug`, `line_start`, `line_end`, `subtree_line_end`, `content`, `sha`, `blob_oid` |
| `chunks` | `chunk_id` | `repo_id`, `source_kind`, `source_ref`, `file_path`, `item_number`, `title`, `text`, `content_sha256`, `is_untrusted`, `updated_at` |
| `chunks_fts` | FTS5 virtual table | `chunk_id`, `source_kind`, `source_ref`, `title`, `text` (tokenized with `porter unicode61`) |
| `chunk_embeddings` | `chunk_id` | `model_id`, `dim` (`384`), `scale` (`REAL`), `vector_u8` (`BLOB`), `content_sha256`, `updated_at` |

### Timeline Events

`timeline_events` holds GitHub timeline items for stored PRs and issues. `subject` depends on `event_type`:

| `event_type` | `actor` | `subject` (`subject_type`) |
| :--- | :--- | :--- |
| `PullRequestReview` | review author | review state, such as `APPROVED` (`NULL`). `event_id` equals `reviews.review_id` |
| `ReviewRequestedEvent`, `ReviewRequestRemovedEvent` | requester | reviewer login, or `org/slug` for a team (`User`, `Bot`, `Mannequin`, or `Team`) |
| `ClosedEvent` | closer's actor | closing PR as `owner/name#N` (`PullRequest`) or commit SHA (`Commit`) |
| `MergedEvent` | merger | merge commit SHA (`Commit`) |
| `CrossReferencedEvent`, `ConnectedEvent`, `DisconnectedEvent` | actor | linked item as `owner/name#N` (`Issue` or `PullRequest`) |
| `LabeledEvent`, `AssignedEvent`, and similar | actor | label name or assignee login |

`payload_json` keeps the raw GraphQL node with GitHub's camelCase keys. `timeline_synced_at` and `xref_synced_at` on `pull_requests` and `issues` record the last full and cross-reference-only timeline fetch; `timeline_synced_at IS NULL` means the item has no timeline yet.

Known limits:

- Mention queueing reads only the text the mirror query returns, so mentions past its nested caps (for example, older comments on a long thread) don't queue their targets.
- An item with more than 20 follow-up pages of events is marked synced anyway, with a warning, so it isn't refetched on every run. Some of its events may be missing.
- Timeline events don't write change-feed rows.

### Live GitHub Tables

The `packrat serve` service writes these tables. Migrations are tracked in `schema_version`. `packrat` refuses to open a database whose schema version is newer than the code knows.

| Table | Primary Key | Key Columns |
| :--- | :--- | :--- |
| `pr_rollup` | `(repo_id, pr_number)` | `head_oid`, `state`, `total`, `checks_etag`, `status_etag`, `fetched_at`. A new head clears the state and ETags |
| `check_runs` | `(repo_id, pr_number, head_oid, run_key)` | `run_key` (`CheckRun:<databaseId>` or `StatusContext:<context>`), `name`, `kind` (`CheckRun` or `StatusContext`), `status`, `conclusion`, `details_url`, `started_at`, `completed_at`, `is_required`, `workflow_run_id` |
| `review_requests` | `(repo_id, pr_number, reviewer)` | `reviewer_kind` (`User`, `Team`, `Bot`, `Mannequin`), `requested_at` |
| `latest_opinionated_reviews` | `(repo_id, pr_number, author)` | `author_type`, `state`, `submitted_at` |
| `latest_reviews` | `(repo_id, pr_number, author)` | `author_type`, `state` (includes `COMMENTED`), `submitted_at`, `commit_oid` |
| `thread_activity` | `thread_id` | `repo_id`, `pr_number`, `is_resolved`, `is_outdated`, `last_author`, `last_author_type`, `last_at`, `participants_json` |
| `notifications` | `thread_id` | `reason`, `subject_type`, `item_key`, `updated_at`, `unread`, `seen_at` |
| `viewer_sets` | `(set_name, repo_id, item_number)` | `set_name` (`authored`, `review_requested`, `reviewed`, `issues`), `first_seen_at`, `last_seen_at`. `v_viewer_items` adds the item key text |
| `watches` | `(consumer, consumer_id, repo_id, item_number)` | `freshness_s`, `lease_expires_at`. `v_watches` adds the item key text |
| `refresh_requests` | `(repo_id, item_number)` | `requested_at` |
| `changes` | `seq` (autoincrement) | `item_key`, `kind`, `fields` (JSON array), `at` |
| `api_calls` | none (append-only) | `at`, `worker`, `resource` (`graphql` or `core`), `cost`, `remaining`, `reset_at`, `status` |
| `service_heartbeat` | `id` (always `1`) | `pid`, `started_at`, `beat_at`, `version` (`packrat-serve/<n>+<scripts sha>`), `last_ok_at`, `consecutive_failed_ticks` |
| `service_state` | `key` | `value`, `updated_at`. Holds cursors such as `notifications_last_modified` |

`watches`, `viewer_sets`, and `refresh_requests` reference `(repo_id, item_number)` with `ON DELETE CASCADE`, so removing or merging a repository drops its leases and sets; the reader views add the `item_key` text. Item keys are `owner/name#number` with the repository's registered casing. Timestamps are ISO-8601 UTC text.

### SQLite Reader Views

Tools that read live data (`/monitor`, `inflight`, the watchdog) open `repos.sqlite` read-only and query only these views: `v_pr_basic`, `v_issue_basic`, `v_item_comments`, `v_pr_live`, `v_pr_checks`, `v_pr_review_requests`, `v_pr_latest_reviews`, `v_pr_latest_author_reviews`, `v_pr_threads`, `v_viewer_items`, `v_notifications`, `v_service_health`, `v_watches`, `v_changes`, and `v_api_calls`. [reader_contract.md](reader_contract.md) documents every column, along with the lease and change-feed rules. The views are versioned by `views_meta.views_version`; `packrat` recreates them when `views.sql` changes.

`v_pr_checks` has one row per check run or status context on the current head, keyed by `run_key`. Several rows can share a `name` (re-runs, or one job name in two workflows), so judge every row rather than one per name. `v_item_comments` holds the 10 newest conversation comments per PR and issue, with `author_type` (`User` or `Bot`).

### Database Portability (packrat export and packrat import)

`packrat export <dest-sqlite-path> [--repo <owner>/<name> ...]` copies the active WAL database into `<dest>.tmp.<pid>` in `32 MiB` steps (`8,192` pages) inside a pinned `read_snapshot`. Concurrent `packrat serve` writes neither block nor restart the copy. Next, it scrubs non-portable state under `PRAGMA foreign_keys = ON`, runs `VACUUM`, switches to `PRAGMA journal_mode = DELETE`, and streams live `stderr` progress. Before replacing `<dest-sqlite-path>` and unlinking `<dest>-wal`, `<dest>-shm`, and `<dest>-journal`, it checks `INSERT INTO chunks_fts(chunks_fts, rank) VALUES('integrity-check', 1)` and `PRAGMA foreign_key_check`.

| Category | Tables / Keys | Export Disposition |
| :--- | :--- | :--- |
| Repository registry & schema metadata | `repos`, `repo_aliases`, `schema_version`, `views_meta` | Kept for selected `mirror` repositories (`mode = 'mirror'`). All `items`-mode repositories and unselected `mirror` repositories are dropped. Every exported row clears `local_path = NULL` and `git_url = NULL`, and maps `git_source = 'path'` to `'managed'` (`'none'` stays `'none'`) while preserving watermarks, backfill cursors, `events_etag`, `events_last_modified`, and `last_indexed_sha`. |
| Historical mirror content & git index | `issues`, `pull_requests`, `pr_files`, `reviews`, `review_threads`, `comments`, `issue_pr_links`, `discussions`, `commits`, `commit_files`, `timeline_events`, `timeline_dirty`, `indexed_files`, `code_symbols`, `doc_sections`, `chunks`, `chunks_fts`, `chunk_embeddings` | Kept for exported `mirror` repositories. For dropped repositories, external-content `chunks_fts` rows are cleared before `DELETE FROM repos WHERE repo_id NOT IN (...)` cascades across all 19 child tables. |
| Live PR status tables | `pr_rollup`, `check_runs`, `review_requests`, `latest_opinionated_reviews`, `latest_reviews`, `thread_activity` | Kept for exported `mirror` repositories so `v_pr_live`, `v_pr_checks`, `v_pr_review_requests`, `v_pr_latest_reviews`, `v_pr_latest_author_reviews`, and `v_pr_threads` remain populated. Rows for `items` and excluded `mirror` repositories cascade-delete with `repos`. |
| Personal, viewer, & runtime tables | `notifications`, `viewer_sets`, `watches`, `refresh_requests`, `changes`, `api_calls`, `service_heartbeat` | Scrubbed completely (`0` rows), along with `sqlite_sequence WHERE name = 'changes'` so the recipient change feed resets to `seq = 1`. |
| Kept `service_state` keys | `signals_events:<id>`, `events_last_id:<id>`, `refetch_open_prs:<id>`, `gap_cursor:<id>:*`, `gap_floor:<id>:*`, `vector_cache_seq` | Preserved only when `<id>` matches an exported `mirror` `repo_id`. `vector_cache_seq` is incremented on the exported copy so recipient `vectors.*.mmap` sidecars rebuild on the next `packrat search`. |
| Scrubbed `service_state` keys | `viewer_login`, `notifications_last_modified`, `viewer_set:*`, `node_id:*`, `git_poll:<id>`, and keys for dropped `<id>` values | Cleared so the recipient's `packrat serve` polls unread notifications without a sender `If-Modified-Since` cutoff, keeps clone state machine-local, and populates `viewer_sets` for the recipient's GitHub login. |

Every exported `repos` row has `local_path = NULL` and `git_url = NULL`. Rows with git history export as `git_source = 'managed'`, so the recipient creates its own bare clone on the next sync (or right after import with `--clone-now`). Binding a local checkout with `packrat repo set-path <owner>/<name> <checkout>` switches that row back to `path` and resumes incremental `git diff-tree` indexing from `last_indexed_sha`.

`packrat import <src-sqlite-path> [--repo ...] [--path ...] [--allow-older] [--clone-now] [--dry-run]` checks the source, schema (`11`..`15`), `--repo`, `--path`, alias collisions, and free disk (`2x` selected data) before `sync.lock`. Under `sync.lock`, it attaches the source read-only (`?mode=ro` as `src_db`) and merges each `mirror` repository inside `BEGIN IMMEDIATE`. Each repository checks `PRAGMA foreign_key_check` and `chunks_fts` `integrity-check` before `COMMIT`, runs `PRAGMA wal_checkpoint(PASSIVE)` afterward, and ends the batch with `PRAGMA wal_checkpoint(TRUNCATE)`. Failures roll back only the active repository, while `--dry-run` inspects an existing destination via `?mode=ro` (or treats missing paths as empty).

Destination matching resolves `full_name` and `repo_aliases` case-insensitively (split matches exit `2`). Missing repositories are `added`, and local `items` stubs become `mirror` in place (`promoted`, keeping `dest_repo_id`, `watches`, and `viewer_sets`). Existing `mirror` rows compare `FreshnessKey(done_backfills, sync_watermark, has_git_index)`: completed issue/PR/discussion backfills (`0..3`), newest UTC item watermark (or `last_full_sweep_at`, never `last_events_check_at`), then `has_git_index`. Newer sources are `replaced`, while older or equal sources return `skipped_older` (exit `0`) unless `--allow-older` is set.

| Category | Tables / Keys | Import Disposition |
| :--- | :--- | :--- |
| Repository registry & checkout paths | `repos`, `repo_aliases` | Upserted as `mode = 'mirror'` for `added`, `promoted`, and `replaced` repositories; source `items` rows and unselected repositories are ignored. Source `local_path` and `git_url` are never copied; existing destination `path` checkouts and `managed` clones win over imported sources, `--path` overrides to `path` on single-repo imports even when `skipped_older`, and `none` or new rows take the source `git_source` (or infer `managed` from `last_indexed_sha` for pre-v15 snapshots). Source aliases merge into `repo_aliases` without shadowing local `repos.full_name` rows; `events_etag` and `events_last_modified` clear if the slug changed. |
| GitHub mirror & live PR tables | `issues`, `pull_requests`, `pr_files`, `reviews`, `review_threads`, `comments`, `discussions`, `issue_pr_links`, `timeline_events`, `timeline_dirty`, `pr_rollup`, `check_runs`, `review_requests`, `latest_opinionated_reviews`, `latest_reviews`, `thread_activity` | Replaced as a unit (`WHERE repo_id = :dest_id`) for each `added`, `promoted`, or `replaced` repository. Untouched local repositories stay intact. |
| Local git index tables | `commits`, `commit_files`, `indexed_files`, `code_symbols`, `doc_sections` (plus `last_indexed_sha`, `commits_backfill_cursor`, `commits_backfill_done_at` on `repos`) | Replaced when the source has `last_indexed_sha` set or `--allow-older` is passed. Kept intact (`preserved_local_git = true`) when importing a GitHub-only source (`last_indexed_sha IS NULL`) over a locally git-indexed repository without `--allow-older`. |
| Chunks, FTS5, & embeddings | `chunks`, `chunks_fts`, `chunk_embeddings` | Replaced for all repository chunks on a full copy, or only non-git chunks (`source_kind NOT IN ('commit', 'code_symbol', 'doc_section')`) when keeping the local git index. External-content `chunks_fts` entries are deleted before `chunks` and rebuilt from the new `rowid`s. When source and destination slugs differ, `#`, `@`, and `:` prefixes on `chunk_embeddings`, `chunks` (`chunk_id`, `source_ref`), and `timeline_events` subjects are rewritten under deferred foreign keys. |
| Personal, viewer, & runtime tables | `notifications`, `viewer_sets`, `watches`, `refresh_requests`, `changes`, `api_calls`, `service_heartbeat` | Never copied from the source; destination rows stay intact. |
| `service_state` & reader handoff | `signals_events:<id>`, `events_last_id:<id>`, `refetch_open_prs:<id>`, `gap_cursor:<id>:*`, `gap_floor:<id>:*`, `vector_cache_seq`, `refresh_requests`, `changes` | Per-repository sync keys for `<dest_id>` are replaced from `<src_id>`, while machine-local (`git_poll:<id>`) and global keys (`viewer_login`, `notifications_last_modified`, `viewer_set:*`, `node_id:*`) stay untouched and `vector_cache_seq` increments so `vectors.*.mmap` caches rebuild on the next `packrat search`. On `promoted` and `replaced` repositories, `import` queues `refresh_requests` for leased items and non-issue `viewer_sets` entries, and appends `changes` rows (`kind = 'pr'`, `fields = ["pr", "checks", "reviews", "threads"]`) for matching pull requests. |

Sources at schema versions `11` through `15` copy via `PRAGMA table_info` column intersection; schemas below `11` or above `15` exit `2`. Schema v11 sources missing `indexed_files` or `commits_backfill_*` columns default cleanly without schema errors. Pre-v12 sources run repo-scoped Migration 12 fixups before `COMMIT`: UTC `Z` commit timestamps, `review_threads.participants_json` backfill, and stale `check_runs` cleanup. Pre-v14 sources also run repo-scoped Migration 14 fixups. They backfill `code_symbols.qualified_name`, mark `doc_section` and documented `code_symbol` chunks `is_untrusted = 1`, and clear `repos.parser_fingerprint`.

## DuckDB v2.0 Views and Macros (`~/.local/share/packrat/init.sql`)

When running `packrat sql "<query>"` (or `duckdb -batch -dark-mode -init ~/.local/share/packrat/init.sql`), the SQLite database is attached read-only as `kb` and the following views and macros are pre-loaded:

- `v_pull_requests`: `kb.pull_requests` joined with `repo` (`r.full_name`).
- `v_issues`: `kb.issues` joined with `repo` (`r.full_name`).
- `v_review_threads`: `kb.review_threads` joined with `repo` (`r.full_name`).
- `v_code_symbols`: `kb.code_symbols` joined with `repo` (`r.full_name`), exposing `(repo, file_path, symbol_name, qualified_name, kind, parent, line_start, line_end, signature, docstring, visibility, sha, blob_oid)`.
- `v_indexed_files`: `kb.indexed_files` joined with `repo` (`r.full_name`), exposing `(repo, file_path, blob_oid, sha, parser_id)` (`py-ast@1`, `treesitter@1`, `dart-analyzer@1`, `swift-syntax@1`, `md-sections@1`, or `regex@1`).
- `v_doc_sections`: `kb.doc_sections` joined with `repo` (`r.full_name`).
- `v_chunk_vectors`: `kb.chunks` joined with `kb.chunk_embeddings` and `repo` (`r.full_name`).
- `v_file_hotspots`: per-file `commit_count`, `review_thread_count`, and `unresolved_thread_count`.
- `v_item_participants`: unified lowercased `participant` logins and `role` values across PRs, issues, and discussions. Each row exposes `repo`, `kind`, `number`, `title`, `state`, `url`, `item_author`, `created_at`, `updated_at`, `merged_at`, `participant`, and `role`. Roles use `author`, `review:<STATE>`, `comment`, and `inline_comment`. PRs and issues mirror full multi-user comments and inline threads, while discussions mirror `discussions.author` only.
- `v_timeline_events`: `kb.timeline_events` with `repo` and `item_key`.
- `v_pr_reviews_all`: every submitted review, from `kb.reviews` and timeline `PullRequestReview` events, one row per `review_id`. `PENDING` reviews are left out. `is_bot` is true for `author_type = 'Bot'` or a `[bot]` login. `is_human` is true for reviews that are not from a bot and not from the PR author.
- `v_pr_timeline`: one stream per PR (`repo`, `pr_number`, `item_key`, `at`, `event_type`, `actor`, `actor_type`, `subject`, `subject_type`) combining timeline events, reviews (`event_type = 'PullRequestReview'`, `subject` = state), and conversation comments (`IssueComment`).
- `v_pr_lifecycle`: one row per PR with `opened_at`, `first_ready_at`, `first_review_requested_at`, `first_review_at`, `first_approval_at`, `merged_at`, `merged_by`, `draft_cycles`, `force_pushes_after_first_review`, `reviewed_before_request`, and the minute latencies `ready_to_first_review_min`, `request_to_first_review_min`, and `ready_to_merge_min`. Review columns count human reviews only. `first_ready_at` is the first ready-for-review event for a PR opened as a draft, `NULL` while such a PR is still a draft, and `opened_at` otherwise. `reviewed_before_request` is true when a human review exists and no review request came before it. `request_to_first_review_min` is negative when the review came first.
- `v_review_request_latency`: one row per review request with `reviewer`, `reviewer_type`, `requested_at`, `requested_by`, `removed_at` (the first removal before the same reviewer is requested again), `first_review_at` (that reviewer's first submitted review after the request), and `latency_min`. Team requests have no `first_review_at`.
- `v_issue_pr_links`: one row per `(repo, issue_number, pr_number)` with the strongest `link_type` (`closed_by` > `closes` > `xref` > `references`), every type seen in `link_types`, and `issue_key` / `pr_key`. It adds `closed_by` for issues closed by a commit whose `commits.pr_number` is known, and drops pairs whose `issue_number` is a PR.
- Live GitHub views with the same names and columns as the SQLite reader views: `v_pr_basic`, `v_issue_basic`, `v_item_comments`, `v_pr_live`, `v_pr_checks`, `v_pr_review_requests`, `v_pr_latest_reviews`, `v_pr_latest_author_reviews`, `v_pr_threads`, `v_viewer_items`, `v_notifications`, and `v_service_health`. DuckDB can't evaluate the SQLite views, so `init.sql` defines DuckDB copies of them.
- `v_watches`: every lease, with an `active` column that is true while `lease_expires_at` is in the future.
- `v_changes`: the change feed (`seq`, `item_key`, `kind`, `fields`, `at`).
- `v_api_calls`: the GitHub API call log (`at`, `worker`, `resource`, `cost`, `remaining`, `reset_at`, `status`).
- `kb_vec(v_u8, scale)`: decodes the 384-byte offset `uint8` `vector_u8` `BLOB` and `scale` into `FLOAT[384]`.
- `kb_cosine(v_u8, scale, q_vec)`: computes `list_cosine_similarity(kb_vec(v_u8, scale), q_vec::FLOAT[384])`.

## Analytical SQL Recipes

### 1. Files with the Most Review Contention and Commit Churn

```sql
SELECT
  repo,
  file_path,
  commit_count,
  review_thread_count,
  unresolved_thread_count
FROM v_file_hotspots
WHERE repo = '<owner>/<name>'
ORDER BY review_thread_count DESC, commit_count DESC
LIMIT 15;
```

### 2. Open Issues Linked to Pull Requests

`v_issue_pr_links` keeps the strongest evidence per issue and PR pair, so each pair appears once.

```sql
SELECT
  l.repo,
  l.issue_number,
  i.title AS issue_title,
  l.link_type,
  l.pr_number,
  p.state AS pr_state,
  p.title AS pr_title
FROM v_issue_pr_links l
JOIN v_issues i ON i.repo = l.repo AND i.number = l.issue_number
JOIN v_pull_requests p ON p.repo = l.repo AND p.number = l.pr_number
WHERE i.state = 'OPEN'
ORDER BY i.updated_at DESC;
```

### 3. Semantic Nearest Neighbors to a Specific Chunk in DuckDB

```sql
WITH target AS (
  SELECT kb_vec(vector_u8, scale) AS q_vec
  FROM v_chunk_vectors
  WHERE chunk_id = '<owner>/<name>#issue:42'
)
SELECT
  v.repo,
  v.source_kind,
  v.source_ref,
  v.title,
  ROUND(kb_cosine(v.vector_u8, v.scale, target.q_vec), 4) AS cosine_sim
FROM v_chunk_vectors v, target
WHERE v.chunk_id <> '<owner>/<name>#issue:42'
ORDER BY cosine_sim DESC
LIMIT 10;
```

### 4. Map Review Threads to AST Symbols in the Same File

```sql
SELECT
  rt.repo,
  rt.pr_number,
  rt.file_path,
  rt.line AS comment_line,
  cs.symbol_name,
  cs.kind,
  rt.reviewer,
  LEFT(rt.body, 120) AS review_excerpt
FROM v_review_threads rt
JOIN v_code_symbols cs
  ON cs.repo = rt.repo
 AND cs.file_path = rt.file_path
 AND rt.line BETWEEN cs.line_start AND cs.line_end
ORDER BY rt.pr_number DESC;
```

### 5. Collaboration History & Peer Feedback Between Engineers

If a repository is not yet mirrored locally, register it and run `packrat sync --repo octo-org/widgets --github-only` first. Run `packrat status` to confirm that `prs` and `issues` backfills are complete before summarizing long date windows.

Find every pull request and issue where both `@alice` and `@bob` participated (including co-reviewed third-party PRs and inline threads):

```bash
packrat sql --format markdown "
SELECT
  repo,
  kind,
  number,
  state,
  title,
  item_author,
  LIST(DISTINCT role ORDER BY role) FILTER (WHERE participant = 'alice') AS alice_roles,
  LIST(DISTINCT role ORDER BY role) FILTER (WHERE participant = 'bob') AS bob_roles,
  updated_at,
  url
FROM v_item_participants
WHERE repo = 'octo-org/widgets'
  AND participant IN ('alice', 'bob')
  AND updated_at >= '2026-01-01T00:00:00Z'
  AND updated_at <= '2026-06-30T23:59:59Z'
GROUP BY repo, kind, number, state, title, item_author, updated_at, url
HAVING COUNT(DISTINCT participant) = 2
ORDER BY updated_at DESC;
"
```

Summarize a single engineer's authored and reviewed activity across repositories over a review cycle:

```bash
packrat sql --format markdown "
SELECT
  repo,
  kind,
  role,
  COUNT(DISTINCT number) AS item_count,
  COUNT(DISTINCT CASE WHEN state = 'MERGED' THEN number END) AS merged_count
FROM v_item_participants
WHERE participant = 'alice'
  AND updated_at >= '2026-01-01T00:00:00Z'
  AND updated_at <= '2026-06-30T23:59:59Z'
GROUP BY repo, kind, role
ORDER BY repo, kind, item_count DESC;
"
```

Fetch full review bodies, timeline comments, and inline review threads for selected PRs to prepare peer feedback:

```bash
packrat sql --format json "
SELECT * FROM (
  SELECT
    rv.pr_number,
    'review:' || UPPER(COALESCE(NULLIF(rv.state, ''), 'COMMENTED')) AS source,
    LOWER(rv.author) AS author,
    CAST(NULL AS VARCHAR) AS location,
    rv.submitted_at AS ts,
    rv.body
  FROM kb.reviews rv
  JOIN kb.repos r ON r.repo_id = rv.repo_id
  WHERE r.full_name = 'octo-org/widgets'
    AND rv.pr_number IN (101, 102)
    AND rv.body <> ''

  UNION ALL

  SELECT
    c.parent_number AS pr_number,
    'comment' AS source,
    LOWER(c.author) AS author,
    CAST(NULL AS VARCHAR) AS location,
    c.created_at AS ts,
    c.body
  FROM kb.comments c
  JOIN kb.repos r ON r.repo_id = c.repo_id
  WHERE r.full_name = 'octo-org/widgets'
    AND c.parent_kind = 'pr'
    AND c.parent_number IN (101, 102)
    AND c.body <> ''

  UNION ALL

  SELECT
    rt.pr_number,
    'inline_thread' AS source,
    LOWER(rt.reviewer) AS author,
    COALESCE(rt.file_path, 'general') || COALESCE(':' || CAST(rt.line AS VARCHAR), '') AS location,
    rt.updated_at AS ts,
    rt.body
  FROM kb.review_threads rt
  JOIN kb.repos r ON r.repo_id = rt.repo_id
  WHERE r.full_name = 'octo-org/widgets'
    AND rt.pr_number IN (101, 102)
    AND rt.body <> ''
)
ORDER BY pr_number, ts;
"
```

### 6. Watched PRs With Failing Required Checks

In `packrat sql` (DuckDB), join the active leases to the current head's checks. For status contexts, `conclusion` holds the context's state.

```sql
SELECT
  w.consumer,
  c.item_key,
  c.kind,
  c.name,
  c.conclusion,
  c.details_url
FROM v_watches w
JOIN v_pr_checks c ON c.item_key = w.item_key
WHERE w.active
  AND c.is_required = 1
  AND c.conclusion IN ('FAILURE', 'ERROR', 'TIMED_OUT', 'CANCELLED', 'ACTION_REQUIRED', 'STARTUP_FAILURE')
ORDER BY c.item_key, c.name;
```

The same query works against the SQLite `v_watches` view. A reader that already knows its own keys can skip the join:

```sql
SELECT item_key, kind, name, conclusion, details_url
FROM v_pr_checks
WHERE item_key IN ('octo-org/widgets#101', 'octo-org/widgets#102')
  AND is_required = 1
  AND conclusion IN ('FAILURE', 'ERROR', 'TIMED_OUT', 'CANCELLED', 'ACTION_REQUIRED', 'STARTUP_FAILURE');
```

### 7. GraphQL Cost per Worker in the Last Hour

```sql
SELECT
  worker,
  COUNT(*) AS calls,
  SUM(cost) AS cost,
  MIN(remaining) AS lowest_remaining
FROM v_api_calls
WHERE resource = 'graphql'
  AND "at" >= strftime(now() AT TIME ZONE 'UTC' - INTERVAL 1 HOUR, '%Y-%m-%dT%H:%M:%SZ')
GROUP BY worker
ORDER BY cost DESC NULLS LAST;
```

`cost` comes from the query's `rateLimit { cost }` field and is `NULL` for REST calls and for queries that don't select it.

### 8. Stale Service Check

```sql
SELECT
  pid,
  beat_at,
  beat_age_s,
  beat_age_s IS NULL OR beat_age_s > 180 AS stale,
  graphql_remaining,
  core_remaining,
  active_leases
FROM v_service_health;
```

The view always returns one row. `pid` and `beat_age_s` are `NULL` when `packrat serve` has never run. The same query works against the SQLite view, where `stale` is `0` or `1`.

### 9. The Viewer's Open PRs With Live State

```sql
SELECT
  v.set_name,
  p.item_key,
  p.title,
  p.review_decision,
  p.mergeable,
  p.rollup_state,
  p.live_fetched_at
FROM v_viewer_items v
JOIN v_pr_live p ON p.item_key = v.item_key
WHERE v.set_name IN ('authored', 'review_requested', 'reviewed')
ORDER BY v.set_name, p.updated_at DESC;
```

### 10. Recent Changes for Selected Items

```sql
SELECT seq, item_key, kind, fields, "at"
FROM v_changes
WHERE item_key IN ('octo-org/widgets#101', 'octo-org/widgets#102')
  AND "at" >= strftime(now() AT TIME ZONE 'UTC' - INTERVAL 10 MINUTE, '%Y-%m-%dT%H:%M:%SZ')
ORDER BY seq;
```

Readers that follow the feed continuously use `packrat changes --since SEQ --wait SECONDS --json` instead, which also reports when pruned rows require a full snapshot.

### 11. Reviews Before Any Review Request

PRs that got a human review before anyone requested a reviewer, including PRs that never had a request. Bot reviews, `PENDING` reviews, and the author's own reviews don't count. Check `packrat status` for a pending-timeline warning first: PRs without timeline events have no request times, so any of them with a human review would match.

```sql
SELECT
  repo,
  COUNT(*) AS prs,
  COUNT(*) FILTER (WHERE reviewed_before_request) AS reviewed_first,
  COUNT(*) FILTER (WHERE reviewed_before_request AND first_review_requested_at IS NULL)
    AS never_requested
FROM v_pr_lifecycle
WHERE repo = '<owner>/<name>'
  AND opened_at >= TIMESTAMP '2026-01-01'
GROUP BY repo;
```

List the PRs themselves:

```sql
SELECT item_key, author, opened_at, first_review_at, first_review_requested_at
FROM v_pr_lifecycle
WHERE repo = '<owner>/<name>' AND reviewed_before_request
ORDER BY opened_at DESC;
```

### 12. Review Request Latency per Reviewer

Median and 90th percentile minutes from a review request to that reviewer's first review. An unanswered request has a `NULL` latency, which the percentiles ignore.

```sql
SELECT
  reviewer,
  COUNT(*) AS requests,
  COUNT(latency_min) AS answered,
  quantile_cont(latency_min, 0.5) AS median_min,
  quantile_cont(latency_min, 0.9) AS p90_min
FROM v_review_request_latency
WHERE repo = '<owner>/<name>'
  AND reviewer_type <> 'Team'
  AND requested_at >= TIMESTAMP '2026-01-01'
GROUP BY reviewer
HAVING COUNT(latency_min) >= 3
ORDER BY median_min;
```

### 13. Ready-to-Merge Time by Author

```sql
SELECT
  author,
  COUNT(*) AS merged_prs,
  ROUND(quantile_cont(ready_to_merge_min, 0.5) / 60.0, 1) AS median_hours,
  ROUND(quantile_cont(ready_to_merge_min, 0.9) / 60.0, 1) AS p90_hours,
  ROUND(AVG(draft_cycles), 2) AS avg_draft_cycles
FROM v_pr_lifecycle
WHERE repo = '<owner>/<name>'
  AND merged_at >= TIMESTAMP '2026-01-01'
  AND ready_to_merge_min IS NOT NULL
GROUP BY author
ORDER BY merged_prs DESC;
```

### 14. Issues and the PRs That Closed Them

`closed_by` comes from the issue's own close event: either the PR that closed it, or a commit whose `commits.pr_number` is known from the local git index.

```sql
SELECT
  l.issue_key,
  i.title AS issue_title,
  l.pr_key,
  p.title AS pr_title,
  p.merged_at,
  l.link_types
FROM v_issue_pr_links l
JOIN v_issues i ON i.repo = l.repo AND i.number = l.issue_number
LEFT JOIN v_pull_requests p ON p.repo = l.repo AND p.number = l.pr_number
WHERE l.repo = '<owner>/<name>' AND l.link_type = 'closed_by'
ORDER BY i.closed_at DESC;
```

### 15. Query AST Symbol Docstrings and Parser Coverage

Inspect which AST parser indexed the files in a repository:

```sql
SELECT
  parser_id,
  COUNT(*) AS files
FROM v_indexed_files
WHERE repo = '<owner>/<name>'
GROUP BY parser_id
ORDER BY files DESC;
```

Query public symbols that have docstrings in DuckDB:

```sql
SELECT
  file_path,
  kind,
  qualified_name,
  visibility,
  signature,
  LEFT(docstring, 160) AS docstring_excerpt
FROM v_code_symbols
WHERE repo = '<owner>/<name>'
  AND visibility = 'public'
  AND docstring <> ''
ORDER BY file_path, line_start
LIMIT 25;
```

Run hybrid BM25 and vector search restricted to `code_symbol` chunks (which embed both the declaration signature and the stripped docstring):

```bash
packrat search "lexical token stream lookahead" --repo <owner>/<name> --kind code_symbol
```
