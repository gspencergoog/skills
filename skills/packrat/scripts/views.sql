-- Read-only reader views for packrat's live GitHub data.
--
-- Readers (/monitor, inflight, the watchdog) query only these views, never the
-- tables behind them. The columns are a stable contract documented in
-- references/reader_contract.md. PackratStore recreates the views only when a
-- migration ran or when the SHA-256 of this file changed.
--
-- `item_key` is always `owner/name#number`. Timestamps are ISO-8601 UTC text.

DROP VIEW IF EXISTS v_pr_basic;
CREATE VIEW v_pr_basic AS
SELECT
  r.full_name || '#' || p.number AS item_key,
  r.full_name AS repo_full_name,
  r.mode AS repo_mode,
  p.number AS number,
  p.title AS title,
  p.state AS state,
  p.is_draft AS is_draft,
  p.url AS url,
  p.author AS author,
  p.created_at AS created_at,
  p.updated_at AS updated_at,
  p.merged_at AS merged_at,
  p.closed_at AS closed_at,
  p.base_ref AS base_ref,
  p.head_ref AS head_ref,
  p.head_sha AS head_sha,
  p.head_repo_owner AS head_repo_owner,
  p.is_cross_repository AS is_cross_repository,
  p.body AS body
FROM pull_requests p
JOIN repos r ON r.repo_id = p.repo_id;

DROP VIEW IF EXISTS v_issue_basic;
CREATE VIEW v_issue_basic AS
SELECT
  r.full_name || '#' || i.number AS item_key,
  r.full_name AS repo_full_name,
  r.mode AS repo_mode,
  i.number AS number,
  i.title AS title,
  i.state AS state,
  i.url AS url,
  i.author AS author,
  i.labels_json AS labels_json,
  i.assignees_json AS assignees_json,
  i.created_at AS created_at,
  i.updated_at AS updated_at,
  i.closed_at AS closed_at,
  i.body AS body
FROM issues i
JOIN repos r ON r.repo_id = i.repo_id;

DROP VIEW IF EXISTS v_item_comments;
CREATE VIEW v_item_comments AS
SELECT item_key, kind, comment_id, author, author_type, created_at
FROM (
  SELECT
    r.full_name || '#' || c.parent_number AS item_key,
    c.parent_kind AS kind,
    c.comment_id AS comment_id,
    c.author AS author,
    c.author_type AS author_type,
    c.created_at AS created_at,
    -- Partitioning by the item_key expression lets SQLite push a reader's
    -- `WHERE item_key IN (...)` into this subquery instead of sorting every comment.
    ROW_NUMBER() OVER (
      PARTITION BY r.full_name || '#' || c.parent_number, c.parent_kind
      ORDER BY c.created_at DESC, c.comment_id DESC
    ) AS rn
  FROM comments c
  JOIN repos r ON r.repo_id = c.repo_id
  WHERE c.parent_kind IN ('pr', 'issue')
)
WHERE rn <= 10;

DROP VIEW IF EXISTS v_pr_live;
CREATE VIEW v_pr_live AS
SELECT
  r.full_name || '#' || p.number AS item_key,
  r.full_name AS repo_full_name,
  r.mode AS repo_mode,
  p.number AS number,
  p.node_id AS node_id,
  p.title AS title,
  p.url AS url,
  p.state AS state,
  p.is_draft AS is_draft,
  p.author AS author,
  p.author_type AS author_type,
  p.base_ref AS base_ref,
  p.head_ref AS head_ref,
  p.head_sha AS head_sha,
  p.head_committed_date AS head_committed_date,
  p.head_repo_owner AS head_repo_owner,
  p.is_cross_repository AS is_cross_repository,
  p.mergeable AS mergeable,
  p.merge_state_status AS merge_state_status,
  p.review_decision AS review_decision,
  p.auto_merge_enabled_at AS auto_merge_enabled_at,
  p.created_at AS created_at,
  p.updated_at AS updated_at,
  p.merged_at AS merged_at,
  p.closed_at AS closed_at,
  p.live_fetched_at AS live_fetched_at,
  ro.state AS rollup_state,
  ro.total AS rollup_total,
  ro.fetched_at AS rollup_fetched_at
FROM pull_requests p
JOIN repos r ON r.repo_id = p.repo_id
LEFT JOIN pr_rollup ro
  ON ro.repo_id = p.repo_id AND ro.pr_number = p.number AND ro.head_oid = p.head_sha
WHERE p.live_fetched_at IS NOT NULL;

DROP VIEW IF EXISTS v_pr_checks;
CREATE VIEW v_pr_checks AS
SELECT
  r.full_name || '#' || p.number AS item_key,
  c.head_oid AS head_oid,
  c.run_key AS run_key,
  c.kind AS kind,
  c.name AS name,
  c.status AS status,
  c.conclusion AS conclusion,
  c.details_url AS details_url,
  c.started_at AS started_at,
  c.completed_at AS completed_at,
  c.is_required AS is_required,
  c.workflow_run_id AS workflow_run_id
FROM check_runs c
JOIN pull_requests p
  ON p.repo_id = c.repo_id AND p.number = c.pr_number AND p.head_sha = c.head_oid
JOIN repos r ON r.repo_id = c.repo_id;

DROP VIEW IF EXISTS v_pr_review_requests;
CREATE VIEW v_pr_review_requests AS
SELECT
  r.full_name || '#' || q.pr_number AS item_key,
  q.reviewer AS reviewer,
  q.reviewer_kind AS reviewer_kind
FROM review_requests q
JOIN repos r ON r.repo_id = q.repo_id;

DROP VIEW IF EXISTS v_pr_latest_reviews;
CREATE VIEW v_pr_latest_reviews AS
SELECT
  r.full_name || '#' || o.pr_number AS item_key,
  o.author AS author,
  o.author_type AS author_type,
  o.state AS state,
  o.submitted_at AS submitted_at
FROM latest_opinionated_reviews o
JOIN repos r ON r.repo_id = o.repo_id;

DROP VIEW IF EXISTS v_pr_latest_author_reviews;
CREATE VIEW v_pr_latest_author_reviews AS
SELECT
  r.full_name || '#' || lr.pr_number AS item_key,
  r.full_name AS repo_full_name,
  lr.pr_number,
  lr.author,
  lr.author_type,
  lr.state,
  lr.submitted_at,
  lr.commit_oid
FROM latest_reviews lr
JOIN repos r ON r.repo_id = lr.repo_id;

DROP VIEW IF EXISTS v_pr_threads;
CREATE VIEW v_pr_threads AS
SELECT
  r.full_name || '#' || t.pr_number AS item_key,
  t.thread_id AS thread_id,
  t.is_resolved AS is_resolved,
  t.is_outdated AS is_outdated,
  t.last_author AS last_author,
  t.last_author_type AS last_author_type,
  t.last_at AS last_at
FROM thread_activity t
JOIN repos r ON r.repo_id = t.repo_id;

DROP VIEW IF EXISTS v_viewer_items;
CREATE VIEW v_viewer_items AS
SELECT
  v.set_name AS set_name,
  v.item_key AS item_key,
  v.first_seen_at AS first_seen_at,
  v.last_seen_at AS last_seen_at
FROM viewer_sets v;

DROP VIEW IF EXISTS v_notifications;
CREATE VIEW v_notifications AS
SELECT
  thread_id,
  reason,
  subject_type,
  item_key,
  updated_at,
  unread,
  seen_at
FROM notifications;

DROP VIEW IF EXISTS v_watches;
CREATE VIEW v_watches AS
SELECT
  consumer,
  consumer_id,
  item_key,
  freshness_s,
  lease_expires_at,
  lease_expires_at > strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AS active
FROM watches;

DROP VIEW IF EXISTS v_changes;
CREATE VIEW v_changes AS
SELECT
  seq,
  item_key,
  kind,
  fields,
  "at"
FROM changes;

DROP VIEW IF EXISTS v_api_calls;
CREATE VIEW v_api_calls AS
SELECT
  "at",
  worker,
  resource,
  cost,
  remaining,
  reset_at,
  status
FROM api_calls;

DROP VIEW IF EXISTS v_service_health;
CREATE VIEW v_service_health AS
SELECT
  h.pid AS pid,
  h.started_at AS started_at,
  h.beat_at AS beat_at,
  h.version AS version,
  CAST(ROUND((julianday('now') - julianday(h.beat_at)) * 86400) AS INTEGER) AS beat_age_s,
  (SELECT remaining FROM api_calls WHERE resource = 'graphql' AND remaining IS NOT NULL
     ORDER BY rowid DESC LIMIT 1) AS graphql_remaining,
  (SELECT reset_at FROM api_calls WHERE resource = 'graphql' AND remaining IS NOT NULL
     ORDER BY rowid DESC LIMIT 1) AS graphql_reset_at,
  (SELECT remaining FROM api_calls WHERE resource = 'core' AND remaining IS NOT NULL
     ORDER BY rowid DESC LIMIT 1) AS core_remaining,
  (SELECT reset_at FROM api_calls WHERE resource = 'core' AND remaining IS NOT NULL
     ORDER BY rowid DESC LIMIT 1) AS core_reset_at,
  (SELECT COUNT(*) FROM watches WHERE lease_expires_at > strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    AS active_leases,
  (SELECT value FROM service_state WHERE key = 'viewer_login') AS viewer_login
FROM (SELECT 1 AS one) AS anchor
LEFT JOIN service_heartbeat h ON h.id = 1;
