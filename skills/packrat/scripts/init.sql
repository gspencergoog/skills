-- DuckDB v2.0 read-only analytical views and vector macros for packrat.
INSTALL sqlite;
LOAD sqlite;

ATTACH IF NOT EXISTS '{{DB_PATH}}'
  AS kb (TYPE SQLITE, READ_ONLY);

CREATE OR REPLACE MACRO kb_vec(v_u8, scale) AS
  list_transform(
    range(0, 384),
    lambda i: ((('0x' || substr(to_hex(v_u8), i * 2 + 1, 2))::INTEGER - 128)::FLOAT) * scale::FLOAT
  )::FLOAT[384];

CREATE OR REPLACE MACRO kb_cosine(v_u8, scale, q_vec) AS
  list_cosine_similarity(kb_vec(v_u8, scale), q_vec::FLOAT[384]);

CREATE OR REPLACE VIEW v_pull_requests AS
SELECT r.full_name AS repo, r.full_name AS repo_full_name, p.*
FROM kb.pull_requests p
JOIN kb.repos r ON r.repo_id = p.repo_id;

CREATE OR REPLACE VIEW v_issues AS
SELECT r.full_name AS repo, r.full_name AS repo_full_name, i.*
FROM kb.issues i
JOIN kb.repos r ON r.repo_id = i.repo_id;

CREATE OR REPLACE VIEW v_review_threads AS
SELECT r.full_name AS repo, r.full_name AS repo_full_name, rt.*
FROM kb.review_threads rt
JOIN kb.repos r ON r.repo_id = rt.repo_id;

CREATE OR REPLACE VIEW v_code_symbols AS
SELECT r.full_name AS repo, r.full_name AS repo_full_name, cs.*
FROM kb.code_symbols cs
JOIN kb.repos r ON r.repo_id = cs.repo_id;

CREATE OR REPLACE VIEW v_doc_sections AS
SELECT r.full_name AS repo, r.full_name AS repo_full_name, ds.*
FROM kb.doc_sections ds
JOIN kb.repos r ON r.repo_id = ds.repo_id;

CREATE OR REPLACE VIEW v_chunk_vectors AS
SELECT
  r.full_name AS repo,
  r.full_name AS repo_full_name,
  c.chunk_id,
  c.repo_id,
  c.source_kind,
  c.source_ref,
  c.file_path,
  c.item_number,
  c.title,
  c.text,
  c.is_untrusted,
  ce.model_id,
  ce.scale,
  ce.vector_u8
FROM kb.chunks c
JOIN kb.repos r ON r.repo_id = c.repo_id
JOIN kb.chunk_embeddings ce ON ce.chunk_id = c.chunk_id;

CREATE OR REPLACE VIEW v_file_hotspots AS
SELECT
  r.full_name AS repo,
  r.full_name AS repo_full_name,
  cf.file_path,
  COUNT(DISTINCT cf.sha) AS commit_count,
  COUNT(DISTINCT rt.thread_id) AS review_thread_count,
  COUNT(DISTINCT CASE WHEN rt.is_resolved = 0 THEN rt.thread_id END) AS unresolved_thread_count
FROM kb.commit_files cf
JOIN kb.repos r ON r.repo_id = cf.repo_id
LEFT JOIN kb.review_threads rt
  ON rt.repo_id = cf.repo_id AND rt.file_path = cf.file_path
GROUP BY r.full_name, cf.file_path;

CREATE OR REPLACE VIEW v_item_participants AS
SELECT DISTINCT
  repo,
  repo AS repo_full_name,
  kind,
  number,
  title,
  state,
  url,
  item_author,
  created_at,
  updated_at,
  merged_at,
  participant,
  role
FROM (
  SELECT
    r.full_name AS repo,
    'pr' AS kind,
    p.number AS number,
    p.title AS title,
    p.state AS state,
    p.url AS url,
    LOWER(NULLIF(p.author, '')) AS item_author,
    p.created_at AS created_at,
    p.updated_at AS updated_at,
    p.merged_at AS merged_at,
    LOWER(p.author) AS participant,
    'author' AS role
  FROM kb.pull_requests p
  JOIN kb.repos r ON r.repo_id = p.repo_id

  UNION ALL

  SELECT
    r.full_name AS repo,
    'pr' AS kind,
    p.number AS number,
    p.title AS title,
    p.state AS state,
    p.url AS url,
    LOWER(NULLIF(p.author, '')) AS item_author,
    p.created_at AS created_at,
    p.updated_at AS updated_at,
    p.merged_at AS merged_at,
    LOWER(rv.author) AS participant,
    'review:' || UPPER(COALESCE(NULLIF(rv.state, ''), 'COMMENTED')) AS role
  FROM kb.reviews rv
  JOIN kb.pull_requests p ON p.repo_id = rv.repo_id AND p.number = rv.pr_number
  JOIN kb.repos r ON r.repo_id = p.repo_id

  UNION ALL

  SELECT
    r.full_name AS repo,
    'pr' AS kind,
    p.number AS number,
    p.title AS title,
    p.state AS state,
    p.url AS url,
    LOWER(NULLIF(p.author, '')) AS item_author,
    p.created_at AS created_at,
    p.updated_at AS updated_at,
    p.merged_at AS merged_at,
    LOWER(c.author) AS participant,
    'comment' AS role
  FROM kb.comments c
  JOIN kb.pull_requests p
    ON p.repo_id = c.repo_id AND p.number = c.parent_number AND c.parent_kind = 'pr'
  JOIN kb.repos r ON r.repo_id = p.repo_id

  UNION ALL

  SELECT
    r.full_name AS repo,
    'issue' AS kind,
    i.number AS number,
    i.title AS title,
    i.state AS state,
    i.url AS url,
    LOWER(NULLIF(i.author, '')) AS item_author,
    i.created_at AS created_at,
    i.updated_at AS updated_at,
    CAST(NULL AS VARCHAR) AS merged_at,
    LOWER(c.author) AS participant,
    'comment' AS role
  FROM kb.comments c
  JOIN kb.issues i
    ON i.repo_id = c.repo_id AND i.number = c.parent_number AND c.parent_kind = 'issue'
  JOIN kb.repos r ON r.repo_id = i.repo_id

  UNION ALL

  SELECT
    r.full_name AS repo,
    'pr' AS kind,
    p.number AS number,
    p.title AS title,
    p.state AS state,
    p.url AS url,
    LOWER(NULLIF(p.author, '')) AS item_author,
    p.created_at AS created_at,
    p.updated_at AS updated_at,
    p.merged_at AS merged_at,
    LOWER(rt.reviewer) AS participant,
    'inline_comment' AS role
  FROM kb.review_threads rt
  JOIN kb.pull_requests p ON p.repo_id = rt.repo_id AND p.number = rt.pr_number
  JOIN kb.repos r ON r.repo_id = p.repo_id

  UNION ALL

  SELECT
    r.full_name AS repo,
    'pr' AS kind,
    p.number AS number,
    p.title AS title,
    p.state AS state,
    p.url AS url,
    LOWER(NULLIF(p.author, '')) AS item_author,
    p.created_at AS created_at,
    p.updated_at AS updated_at,
    p.merged_at AS merged_at,
    LOWER(tc.comment.author.login) AS participant,
    'inline_comment' AS role
  FROM kb.review_threads rt
  JOIN kb.pull_requests p ON p.repo_id = rt.repo_id AND p.number = rt.pr_number
  JOIN kb.repos r ON r.repo_id = p.repo_id
  CROSS JOIN UNNEST(
    from_json(
      COALESCE(NULLIF(rt.comments_json, ''), '[]'),
      '[{"author":{"login":"VARCHAR"}}]'
    )
  ) AS tc(comment)

  UNION ALL

  SELECT
    r.full_name AS repo,
    'issue' AS kind,
    i.number AS number,
    i.title AS title,
    i.state AS state,
    i.url AS url,
    LOWER(NULLIF(i.author, '')) AS item_author,
    i.created_at AS created_at,
    i.updated_at AS updated_at,
    CAST(NULL AS VARCHAR) AS merged_at,
    LOWER(i.author) AS participant,
    'author' AS role
  FROM kb.issues i
  JOIN kb.repos r ON r.repo_id = i.repo_id

  UNION ALL

  SELECT
    r.full_name AS repo,
    'discussion' AS kind,
    d.number AS number,
    d.title AS title,
    CASE
      WHEN d.answer_body IS NOT NULL AND d.answer_body <> '' THEN 'ANSWERED'
      ELSE 'OPEN'
    END AS state,
    d.url AS url,
    LOWER(NULLIF(d.author, '')) AS item_author,
    d.created_at AS created_at,
    d.updated_at AS updated_at,
    CAST(NULL AS VARCHAR) AS merged_at,
    LOWER(d.author) AS participant,
    'author' AS role
  FROM kb.discussions d
  JOIN kb.repos r ON r.repo_id = d.repo_id
)
WHERE participant IS NOT NULL AND participant <> '';

-- Live GitHub data (schema version 2). These mirror the SQLite reader views in
-- views.sql, rewritten in DuckDB SQL: DuckDB cannot evaluate the SQLite views
-- (kb.v_*) themselves, so query these unprefixed names from `packrat sql`.

CREATE OR REPLACE VIEW v_pr_basic AS
SELECT
  r.full_name || '#' || p.number AS item_key,
  r.full_name AS repo_full_name,
  r.mode AS repo_mode,
  p.number, p.title, p.state, p.is_draft, p.url, p.author,
  p.created_at, p.updated_at, p.merged_at, p.closed_at,
  p.base_ref, p.head_ref, p.head_sha, p.head_repo_owner, p.is_cross_repository, p.body
FROM kb.pull_requests p
JOIN kb.repos r ON r.repo_id = p.repo_id;

CREATE OR REPLACE VIEW v_issue_basic AS
SELECT
  r.full_name || '#' || i.number AS item_key,
  r.full_name AS repo_full_name,
  r.mode AS repo_mode,
  i.number, i.title, i.state, i.url, i.author, i.labels_json, i.assignees_json,
  i.created_at, i.updated_at, i.closed_at, i.body
FROM kb.issues i
JOIN kb.repos r ON r.repo_id = i.repo_id;

CREATE OR REPLACE VIEW v_item_comments AS
SELECT item_key, kind, comment_id, author, author_type, created_at
FROM (
  SELECT
    r.full_name || '#' || c.parent_number AS item_key,
    c.parent_kind AS kind, c.comment_id, c.author, c.author_type, c.created_at,
    row_number() OVER (
      PARTITION BY c.repo_id, c.parent_kind, c.parent_number
      ORDER BY c.created_at DESC, c.comment_id DESC
    ) AS rn
  FROM kb.comments c
  JOIN kb.repos r ON r.repo_id = c.repo_id
  WHERE c.parent_kind IN ('pr', 'issue')
)
WHERE rn <= 10;

CREATE OR REPLACE VIEW v_pr_live AS
SELECT
  r.full_name || '#' || p.number AS item_key,
  r.full_name AS repo_full_name,
  r.mode AS repo_mode,
  p.number, p.node_id, p.title, p.url, p.state, p.is_draft, p.author, p.author_type,
  p.base_ref, p.head_ref, p.head_sha, p.head_committed_date, p.head_repo_owner,
  p.is_cross_repository, p.mergeable, p.merge_state_status, p.review_decision,
  p.auto_merge_enabled_at, p.created_at, p.updated_at, p.merged_at, p.closed_at,
  p.live_fetched_at,
  ro.state AS rollup_state,
  ro.total AS rollup_total,
  ro.fetched_at AS rollup_fetched_at
FROM kb.pull_requests p
JOIN kb.repos r ON r.repo_id = p.repo_id
LEFT JOIN kb.pr_rollup ro
  ON ro.repo_id = p.repo_id AND ro.pr_number = p.number AND ro.head_oid = p.head_sha
WHERE p.live_fetched_at IS NOT NULL;

CREATE OR REPLACE VIEW v_pr_checks AS
SELECT
  r.full_name || '#' || p.number AS item_key,
  c.head_oid, c.run_key, c.kind, c.name, c.status, c.conclusion, c.details_url,
  c.started_at, c.completed_at, c.is_required, c.workflow_run_id
FROM kb.check_runs c
JOIN kb.pull_requests p
  ON p.repo_id = c.repo_id AND p.number = c.pr_number AND p.head_sha = c.head_oid
JOIN kb.repos r ON r.repo_id = c.repo_id;

CREATE OR REPLACE VIEW v_pr_review_requests AS
SELECT r.full_name || '#' || q.pr_number AS item_key, q.reviewer, q.reviewer_kind
FROM kb.review_requests q
JOIN kb.repos r ON r.repo_id = q.repo_id;

CREATE OR REPLACE VIEW v_pr_latest_reviews AS
SELECT r.full_name || '#' || o.pr_number AS item_key, o.author, o.author_type, o.state,
  o.submitted_at
FROM kb.latest_opinionated_reviews o
JOIN kb.repos r ON r.repo_id = o.repo_id;

CREATE OR REPLACE VIEW v_pr_threads AS
SELECT r.full_name || '#' || t.pr_number AS item_key, t.thread_id, t.is_resolved,
  t.is_outdated, t.last_author, t.last_author_type, t.last_at
FROM kb.thread_activity t
JOIN kb.repos r ON r.repo_id = t.repo_id;

CREATE OR REPLACE VIEW v_viewer_items AS
SELECT set_name, item_key, first_seen_at, last_seen_at FROM kb.viewer_sets;

CREATE OR REPLACE VIEW v_notifications AS
SELECT thread_id, reason, subject_type, item_key, updated_at, unread, seen_at
FROM kb.notifications;

CREATE OR REPLACE VIEW v_watches AS
SELECT consumer, consumer_id, item_key, freshness_s, lease_expires_at,
  lease_expires_at > strftime(now() AT TIME ZONE 'UTC', '%Y-%m-%dT%H:%M:%SZ') AS active
FROM kb.watches;

CREATE OR REPLACE VIEW v_changes AS
SELECT seq, item_key, kind, fields, "at" FROM kb.changes;

CREATE OR REPLACE VIEW v_api_calls AS
SELECT "at", worker, resource, cost, remaining, reset_at, status FROM kb.api_calls;

CREATE OR REPLACE VIEW v_service_health AS
SELECT
  h.pid, h.started_at, h.beat_at, h.version,
  CAST(round(epoch(now()) - epoch(CAST(h.beat_at AS TIMESTAMPTZ))) AS INTEGER) AS beat_age_s,
  (SELECT remaining FROM kb.api_calls
     WHERE resource = 'graphql' AND remaining IS NOT NULL
     ORDER BY "at" DESC, reset_at DESC, remaining ASC LIMIT 1)
    AS graphql_remaining,
  (SELECT reset_at FROM kb.api_calls
     WHERE resource = 'graphql' AND remaining IS NOT NULL
     ORDER BY "at" DESC, reset_at DESC, remaining ASC LIMIT 1)
    AS graphql_reset_at,
  (SELECT remaining FROM kb.api_calls
     WHERE resource = 'core' AND remaining IS NOT NULL
     ORDER BY "at" DESC, reset_at DESC, remaining ASC LIMIT 1)
    AS core_remaining,
  (SELECT reset_at FROM kb.api_calls
     WHERE resource = 'core' AND remaining IS NOT NULL
     ORDER BY "at" DESC, reset_at DESC, remaining ASC LIMIT 1)
    AS core_reset_at,
  (SELECT count(*) FROM kb.watches
     WHERE lease_expires_at > strftime(now() AT TIME ZONE 'UTC', '%Y-%m-%dT%H:%M:%SZ'))
    AS active_leases,
  (SELECT value FROM kb.service_state WHERE key = 'viewer_login') AS viewer_login
FROM (SELECT 1 AS one) AS anchor
LEFT JOIN kb.service_heartbeat h ON h.id = 1;
