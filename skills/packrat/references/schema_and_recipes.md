# `packrat` Schema and DuckDB SQL Recipes

## SQLite WAL Schema (`~/.local/share/packrat/repos.sqlite`)

All tables are partitioned by `repo_id` (foreign key to `repos.repo_id`).

| Table | Primary Key | Key Columns |
| :--- | :--- | :--- |
| `repos` | `repo_id` | `owner`, `name`, `full_name`, `local_path`, `default_branch`, `events_etag`, `issues_watermark`, `prs_watermark`, `discussions_watermark`, `last_indexed_sha`, `last_full_sweep_at`, `issues_backfill_cursor`, `issues_backfill_done_at`, `prs_backfill_cursor`, `prs_backfill_done_at`, `discussions_backfill_cursor`, `discussions_backfill_done_at` |
| `issues` | `(repo_id, number)` | `state`, `author`, `author_association`, `title`, `body`, `labels_json`, `assignees_json`, `created_at`, `updated_at`, `closed_at` |
| `pull_requests` | `(repo_id, number)` | `state`, `is_draft`, `author`, `title`, `body`, `labels_json`, `base_ref`, `head_ref`, `head_sha`, `merge_commit_sha`, `review_decision`, `additions`, `deletions`, `changed_files`, `updated_at`, `merged_at` |
| `pr_files` | `(repo_id, pr_number, file_path)` | `change_type`, `additions`, `deletions` |
| `reviews` | `(repo_id, review_id)` | `pr_number`, `author`, `author_association`, `state`, `body`, `submitted_at` |
| `review_threads` | `thread_id` | `repo_id`, `pr_number`, `file_path`, `line`, `start_line`, `is_resolved`, `is_outdated`, `reviewer`, `body`, `comments_json` |
| `comments` | `(repo_id, comment_id)` | `parent_kind` (`issue` or `pr`), `parent_number`, `author`, `body`, `created_at`, `updated_at` |
| `issue_pr_links` | `(repo_id, issue_number, pr_number, link_type)` | `link_type` (`closes` or `references`) |
| `discussions` | `(repo_id, number)` | `category`, `author`, `title`, `body`, `answer_body`, `updated_at` |
| `commits` | `(repo_id, sha)` | `author_name`, `author_email`, `committed_at`, `title`, `message`, `pr_number` |
| `commit_files` | `(repo_id, sha, file_path)` | `change_type`, `old_path`, `additions`, `deletions` |
| `code_symbols` | `(repo_id, file_path, kind, symbol_name, line_start)` | `kind`, `line_end`, `signature`, `docstring`, `sha` |
| `doc_sections` | `(repo_id, file_path, section_id)` | `level`, `heading`, `breadcrumb`, `anchor_slug`, `line_start`, `line_end`, `subtree_line_end`, `content`, `sha` |
| `chunks` | `chunk_id` | `repo_id`, `source_kind`, `source_ref`, `file_path`, `item_number`, `title`, `text`, `content_sha256`, `is_untrusted`, `updated_at` |
| `chunks_fts` | FTS5 virtual table | `chunk_id`, `source_kind`, `source_ref`, `title`, `text` (tokenized with `porter unicode61`) |
| `chunk_embeddings` | `chunk_id` | `model_id`, `dim` (`384`), `vector_f32` (`BLOB`), `vector_json` (`TEXT`), `content_sha256`, `updated_at` |

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
- `kb_vec(v_json)`: parses `vector_json` into `FLOAT[384]`.
- `kb_cosine(v_json, q_vec)`: computes `list_cosine_similarity(from_json(v_json, '["FLOAT"]')::FLOAT[384], q_vec::FLOAT[384])`.

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

```sql
SELECT
  r.full_name AS repo,
  i.number AS issue_number,
  i.title AS issue_title,
  l.link_type,
  p.number AS pr_number,
  p.state AS pr_state,
  p.title AS pr_title
FROM kb.issue_pr_links l
JOIN kb.repos r ON r.repo_id = l.repo_id
JOIN kb.issues i ON i.repo_id = l.repo_id AND i.number = l.issue_number
JOIN kb.pull_requests p ON p.repo_id = l.repo_id AND p.number = l.pr_number
WHERE i.state = 'OPEN'
ORDER BY i.updated_at DESC;
```

### 3. Semantic Nearest Neighbors to a Specific Chunk in DuckDB

```sql
WITH target AS (
  SELECT kb_vec(vector_json) AS q_vec
  FROM v_chunk_vectors
  WHERE chunk_id = '<owner>/<name>#issue:42'
)
SELECT
  v.repo,
  v.source_kind,
  v.source_ref,
  v.title,
  ROUND(kb_cosine(v.vector_json, target.q_vec), 4) AS cosine_sim
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
