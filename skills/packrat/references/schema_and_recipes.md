# `packrat` Schema and DuckDB SQL Recipes

## SQLite WAL Schema (`~/.local/share/packrat/repos.sqlite`)

All tables are partitioned by `repo_id` (foreign key to `repos.repo_id`).

| Table | Primary Key | Key Columns |
| :--- | :--- | :--- |
| `repos` | `repo_id` | `owner`, `name`, `full_name`, `mode` (`mirror` or `items`), `local_path`, `default_branch`, `events_etag`, `issues_watermark`, `prs_watermark`, `discussions_watermark`, `last_indexed_sha`, `last_full_sweep_at`, `issues_backfill_cursor`, `issues_backfill_done_at`, `prs_backfill_cursor`, `prs_backfill_done_at`, `discussions_backfill_cursor`, `discussions_backfill_done_at` |
| `issues` | `(repo_id, number)` | `state`, `author`, `author_association`, `title`, `body`, `labels_json`, `assignees_json`, `created_at`, `updated_at`, `closed_at`, `timeline_synced_at`, `xref_synced_at` |
| `pull_requests` | `(repo_id, number)` | `state`, `is_draft`, `author`, `title`, `body`, `labels_json`, `base_ref`, `head_ref`, `head_sha`, `merge_commit_sha`, `review_decision`, `additions`, `deletions`, `changed_files`, `updated_at`, `merged_at`, plus the live columns `node_id`, `author_type`, `mergeable`, `merge_state_status`, `auto_merge_enabled_at`, `head_committed_date`, `head_repo_owner`, `is_cross_repository`, `live_fetched_at`, plus `timeline_synced_at` and `xref_synced_at` |
| `pr_files` | `(repo_id, pr_number, file_path)` | `change_type`, `additions`, `deletions` |
| `reviews` | `(repo_id, review_id)` | `pr_number`, `author`, `author_association`, `author_type` (`User`, `Bot`, `Mannequin`, or `Organization`; filled by mirror syncs and the live service), `state`, `body`, `submitted_at` |
| `review_threads` | `thread_id` | `repo_id`, `pr_number`, `file_path`, `line`, `start_line`, `is_resolved`, `is_outdated`, `reviewer`, `body`, `comments_json` |
| `comments` | `(repo_id, comment_id)` | `parent_kind` (`issue` or `pr`), `parent_number`, `author`, `author_type`, `body`, `created_at`, `updated_at` |
| `issue_pr_links` | `(repo_id, issue_number, pr_number, link_type)` | `link_type`: `closes` or `references` from PR text, `closed_by` from an issue's `ClosedEvent` closer, `xref` from cross-reference and connect events. Re-saving a PR replaces only its text-derived rows |
| `discussions` | `(repo_id, number)` | `category`, `author`, `title`, `body`, `answer_body`, `updated_at` |
| `commits` | `(repo_id, sha)` | `author_name`, `author_email`, `committed_at`, `title`, `message`, `pr_number` |
| `timeline_events` | `event_id` (GraphQL node id) | `repo_id`, `item_kind` (`pr` or `issue`), `item_number`, `event_type` (GraphQL `__typename`, such as `ReviewRequestedEvent`), `created_at`, `actor`, `actor_type`, `subject`, `subject_type`, `payload_json` |
| `timeline_dirty` | `(repo_id, item_number)` | `reason` (`mention` or `commit`), `queued_at`. Items another item or commit mentioned, waiting for a cross-reference fetch |
| `commit_files` | `(repo_id, sha, file_path)` | `change_type`, `old_path`, `additions`, `deletions` |
| `code_symbols` | `(repo_id, file_path, kind, symbol_name, line_start)` | `kind`, `line_end`, `signature`, `docstring`, `sha` |
| `doc_sections` | `(repo_id, file_path, section_id)` | `level`, `heading`, `breadcrumb`, `anchor_slug`, `line_start`, `line_end`, `subtree_line_end`, `content`, `sha` |
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
| `check_runs` | `(repo_id, pr_number, head_oid, run_key)` | `run_key` (`CheckRun:<databaseId>` or `StatusContext:<context>`; schema v3), `name`, `kind` (`CheckRun` or `StatusContext`), `status`, `conclusion`, `details_url`, `started_at`, `completed_at`, `is_required`, `workflow_run_id` |
| `review_requests` | `(repo_id, pr_number, reviewer)` | `reviewer_kind` (`User`, `Team`, `Bot`, `Mannequin`) |
| `latest_opinionated_reviews` | `(repo_id, pr_number, author)` | `author_type`, `state`, `submitted_at` |
| `thread_activity` | `thread_id` | `repo_id`, `pr_number`, `is_resolved`, `is_outdated`, `last_author`, `last_author_type`, `last_at` |
| `notifications` | `thread_id` | `reason`, `subject_type`, `item_key`, `updated_at`, `unread`, `seen_at` |
| `viewer_sets` | `(set_name, item_key)` | `set_name` (`authored`, `review_requested`, `issues`), `first_seen_at`, `last_seen_at` |
| `watches` | `(consumer, consumer_id, item_key)` | `freshness_s`, `lease_expires_at` |
| `refresh_requests` | `item_key` | `requested_at` |
| `changes` | `seq` (autoincrement) | `item_key`, `kind`, `fields` (JSON array), `at` |
| `api_calls` | none (append-only) | `at`, `worker`, `resource` (`graphql` or `core`), `cost`, `remaining`, `reset_at`, `status` |
| `service_heartbeat` | `id` (always `1`) | `pid`, `started_at`, `beat_at`, `version` |
| `service_state` | `key` | `value`, `updated_at`. Holds cursors such as `notifications_last_modified` |

Item keys are `owner/name#number` with the repository's registered casing. Timestamps are ISO-8601 UTC text.

### SQLite Reader Views

Tools that read live data (`/monitor`, `inflight`, the watchdog) open `repos.sqlite` read-only and query only these views: `v_pr_basic`, `v_issue_basic`, `v_item_comments`, `v_pr_live`, `v_pr_checks`, `v_pr_review_requests`, `v_pr_latest_reviews`, `v_pr_threads`, `v_viewer_items`, `v_notifications`, and `v_service_health`. [reader_contract.md](reader_contract.md) documents every column, along with the lease and change-feed rules.

`v_pr_checks` has one row per check run or status context on the current head, keyed by `run_key`. Several rows can share a `name` (re-runs, or one job name in two workflows), so judge every row rather than one per name. `v_item_comments` holds the 10 newest conversation comments per PR and issue, with `author_type` (`User` or `Bot`).

## DuckDB v2.0 Views and Macros (`~/.local/share/packrat/init.sql`)

When running `packrat sql "<query>"` (or `duckdb -batch -dark-mode -init ~/.local/share/packrat/init.sql`), the SQLite database is attached read-only as `kb` and the following views and macros are pre-loaded:

- `v_pull_requests`: `kb.pull_requests` joined with `repo` (`r.full_name`).
- `v_issues`: `kb.issues` joined with `repo` (`r.full_name`).
- `v_review_threads`: `kb.review_threads` joined with `repo` (`r.full_name`).
- `v_code_symbols`: `kb.code_symbols` joined with `repo` (`r.full_name`).
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
- Live GitHub views with the same names and columns as the SQLite reader views: `v_pr_basic`, `v_issue_basic`, `v_item_comments`, `v_pr_live`, `v_pr_checks`, `v_pr_review_requests`, `v_pr_latest_reviews`, `v_pr_threads`, `v_viewer_items`, `v_notifications`, and `v_service_health`. DuckDB can't evaluate the SQLite views, so `init.sql` defines DuckDB copies of them.
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

A reader using the SQLite views has no leases view. It passes the keys it leased instead:

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
WHERE v.set_name IN ('authored', 'review_requested')
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
