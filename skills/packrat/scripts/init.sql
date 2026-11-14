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

CREATE OR REPLACE VIEW v_indexed_files AS
SELECT r.full_name AS repo, r.full_name AS repo_full_name, idx.*
FROM kb.indexed_files idx
JOIN kb.repos r ON r.repo_id = idx.repo_id;

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
    LOWER(tp.login) AS participant,
    'inline_comment' AS role
  FROM kb.review_threads rt
  JOIN kb.pull_requests p ON p.repo_id = rt.repo_id AND p.number = rt.pr_number
  JOIN kb.repos r ON r.repo_id = p.repo_id
  CROSS JOIN UNNEST(
    from_json(
      COALESCE(NULLIF(rt.participants_json, ''), '[]'),
      '["VARCHAR"]'
    )
  ) AS tp(login)

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
  WHERE COALESCE(NULLIF(rt.participants_json, ''), '[]') = '[]'

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
SELECT r.full_name || '#' || q.pr_number AS item_key, q.reviewer, q.reviewer_kind,
  q.requested_at AS requested_at
FROM kb.review_requests q
JOIN kb.repos r ON r.repo_id = q.repo_id;

CREATE OR REPLACE VIEW v_pr_latest_reviews AS
SELECT r.full_name || '#' || o.pr_number AS item_key, o.author, o.author_type, o.state,
  o.submitted_at
FROM kb.latest_opinionated_reviews o
JOIN kb.repos r ON r.repo_id = o.repo_id;

CREATE OR REPLACE VIEW v_pr_latest_author_reviews AS
SELECT
  r.full_name || '#' || lr.pr_number AS item_key,
  r.full_name AS repo_full_name,
  lr.pr_number,
  lr.author,
  lr.author_type,
  lr.state,
  lr.submitted_at,
  lr.commit_oid
FROM kb.latest_reviews lr
JOIN kb.repos r ON r.repo_id = lr.repo_id;

CREATE OR REPLACE VIEW v_pr_threads AS
SELECT r.full_name || '#' || t.pr_number AS item_key, t.thread_id, t.is_resolved,
  t.is_outdated, t.last_author, t.last_author_type, t.last_at,
  t.participants_json AS participants_json
FROM kb.thread_activity t
JOIN kb.repos r ON r.repo_id = t.repo_id;

CREATE OR REPLACE VIEW v_viewer_items AS
SELECT
  v.set_name,
  r.full_name || '#' || v.item_number AS item_key,
  v.first_seen_at,
  v.last_seen_at
FROM kb.viewer_sets v
JOIN kb.repos r ON r.repo_id = v.repo_id;

CREATE OR REPLACE VIEW v_notifications AS
SELECT thread_id, reason, subject_type, item_key, updated_at, unread, seen_at
FROM kb.notifications;

CREATE OR REPLACE VIEW v_watches AS
SELECT
  w.consumer,
  w.consumer_id,
  r.full_name || '#' || w.item_number AS item_key,
  w.freshness_s,
  w.lease_expires_at,
  w.lease_expires_at > strftime(now() AT TIME ZONE 'UTC', '%Y-%m-%dT%H:%M:%SZ') AS active
FROM kb.watches w
JOIN kb.repos r ON r.repo_id = w.repo_id;

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
  (SELECT value FROM kb.service_state WHERE key = 'viewer_login') AS viewer_login,
  h.last_ok_at AS last_ok_at,
  COALESCE(h.consecutive_failed_ticks, 0) AS consecutive_failed_ticks
FROM (SELECT 1 AS one) AS anchor
LEFT JOIN kb.service_heartbeat h ON h.id = 1;

-- Timeline events (schema version 7). SQLite TEXT timestamps arrive as
-- VARCHAR, so every comparison and date_diff casts them with ::TIMESTAMP.

CREATE OR REPLACE VIEW v_timeline_events AS
SELECT
  r.full_name AS repo,
  r.full_name || '#' || t.item_number AS item_key,
  t.*
FROM kb.timeline_events t
JOIN kb.repos r ON r.repo_id = t.repo_id;

-- Every submitted review, from `kb.reviews` and timeline `PullRequestReview`
-- events, one row per review id (the timeline event_id is the review node id).
-- The timeline row wins for author type and state; otherwise the non-NULL
-- value is used. PENDING (unsubmitted) reviews are excluded. `is_human` drops
-- bots (by author type or a `[bot]` login) and the PR author's own reviews.
CREATE OR REPLACE VIEW v_pr_reviews_all AS
WITH tl AS (
  SELECT
    repo_id,
    event_id AS review_id,
    item_number AS pr_number,
    actor AS author,
    actor_type AS author_type,
    UPPER(COALESCE(NULLIF(subject, ''), json_extract_string(payload_json, '$.state'))) AS state,
    COALESCE(json_extract_string(payload_json, '$.submittedAt'), created_at) AS submitted_at
  FROM kb.timeline_events
  WHERE item_kind = 'pr' AND event_type = 'PullRequestReview'
),
merged AS (
  SELECT
    COALESCE(tl.repo_id, rv.repo_id) AS repo_id,
    COALESCE(tl.review_id, rv.review_id) AS review_id,
    COALESCE(tl.pr_number, rv.pr_number) AS pr_number,
    COALESCE(NULLIF(tl.author, ''), NULLIF(rv.author, '')) AS author,
    COALESCE(NULLIF(tl.author_type, ''), NULLIF(rv.author_type, '')) AS author_type,
    COALESCE(NULLIF(tl.state, ''), UPPER(NULLIF(rv.state, ''))) AS state,
    COALESCE(NULLIF(rv.submitted_at, ''), tl.submitted_at)::TIMESTAMP AS submitted_at,
    tl.review_id IS NOT NULL AS in_timeline,
    rv.review_id IS NOT NULL AS in_reviews
  FROM tl
  FULL OUTER JOIN kb.reviews rv
    ON rv.repo_id = tl.repo_id AND rv.review_id = tl.review_id
)
SELECT
  r.full_name AS repo,
  r.full_name || '#' || m.pr_number AS item_key,
  m.*,
  (COALESCE(m.author_type, '') = 'Bot' OR COALESCE(m.author, '') LIKE '%[bot]') AS is_bot,
  (
    COALESCE(m.author_type, '') <> 'Bot'
    AND m.author IS NOT NULL
    AND m.author NOT LIKE '%[bot]'
    AND LOWER(m.author) IS DISTINCT FROM LOWER(p.author)
  ) AS is_human
FROM merged m
JOIN kb.repos r ON r.repo_id = m.repo_id
LEFT JOIN kb.pull_requests p ON p.repo_id = m.repo_id AND p.number = m.pr_number
WHERE COALESCE(m.state, '') <> 'PENDING';

CREATE OR REPLACE VIEW v_pr_timeline AS
SELECT repo, pr_number, repo || '#' || pr_number AS item_key, "at", event_type, actor,
  actor_type, subject, subject_type
FROM (
  SELECT
    r.full_name AS repo,
    t.item_number AS pr_number,
    t.created_at::TIMESTAMP AS "at",
    t.event_type,
    t.actor,
    t.actor_type,
    t.subject,
    t.subject_type
  FROM kb.timeline_events t
  JOIN kb.repos r ON r.repo_id = t.repo_id
  WHERE t.item_kind = 'pr' AND t.event_type <> 'PullRequestReview'

  UNION ALL

  SELECT
    repo,
    pr_number,
    submitted_at AS "at",
    'PullRequestReview' AS event_type,
    author AS actor,
    author_type AS actor_type,
    state AS subject,
    CAST(NULL AS VARCHAR) AS subject_type
  FROM v_pr_reviews_all

  UNION ALL

  SELECT
    r.full_name AS repo,
    c.parent_number AS pr_number,
    c.created_at::TIMESTAMP AS "at",
    'IssueComment' AS event_type,
    c.author AS actor,
    c.author_type AS actor_type,
    CAST(NULL AS VARCHAR) AS subject,
    CAST(NULL AS VARCHAR) AS subject_type
  FROM kb.comments c
  JOIN kb.repos r ON r.repo_id = c.repo_id
  WHERE c.parent_kind = 'pr'
)
ORDER BY repo, pr_number, "at";

-- One row per PR. `first_ready_at` is the first ReadyForReviewEvent when the
-- PR was opened as a draft (no ConvertToDraftEvent before it), NULL while a PR
-- opened as a draft has never been readied, and `opened_at` otherwise.
-- Review columns count human reviews only (see v_pr_reviews_all.is_human).
CREATE OR REPLACE VIEW v_pr_lifecycle AS
WITH ev AS (
  SELECT repo_id, item_number AS pr_number, event_type, actor, created_at::TIMESTAMP AS "at"
  FROM kb.timeline_events
  WHERE item_kind = 'pr'
),
ev_agg AS (
  SELECT
    repo_id,
    pr_number,
    MIN("at") FILTER (WHERE event_type = 'ReadyForReviewEvent') AS first_ready_event_at,
    MIN("at") FILTER (WHERE event_type = 'ConvertToDraftEvent') AS first_draft_event_at,
    MIN("at") FILTER (WHERE event_type = 'ReviewRequestedEvent') AS first_review_requested_at,
    arg_min(actor, "at") FILTER (WHERE event_type = 'MergedEvent') AS merged_by,
    COUNT(*) FILTER (WHERE event_type = 'ConvertToDraftEvent') AS draft_cycles
  FROM ev
  GROUP BY repo_id, pr_number
),
human AS (
  SELECT
    repo_id,
    pr_number,
    MIN(submitted_at) AS first_review_at,
    MIN(submitted_at) FILTER (WHERE state = 'APPROVED') AS first_approval_at
  FROM v_pr_reviews_all
  WHERE is_human
  GROUP BY repo_id, pr_number
),
pushes AS (
  SELECT ev.repo_id, ev.pr_number, COUNT(*) AS force_pushes_after_first_review
  FROM ev
  JOIN human h ON h.repo_id = ev.repo_id AND h.pr_number = ev.pr_number
  WHERE ev.event_type = 'HeadRefForcePushedEvent' AND ev."at" > h.first_review_at
  GROUP BY ev.repo_id, ev.pr_number
),
base AS (
  SELECT
    r.full_name AS repo,
    p.number AS pr_number,
    r.full_name || '#' || p.number AS item_key,
    p.author,
    p.state,
    p.created_at::TIMESTAMP AS opened_at,
    CASE
      WHEN a.first_ready_event_at IS NOT NULL
        AND (a.first_draft_event_at IS NULL OR a.first_ready_event_at < a.first_draft_event_at)
        THEN a.first_ready_event_at
      WHEN COALESCE(p.is_draft, 0) <> 0 AND a.first_draft_event_at IS NULL THEN NULL
      ELSE p.created_at::TIMESTAMP
    END AS first_ready_at,
    a.first_review_requested_at,
    h.first_review_at,
    h.first_approval_at,
    p.merged_at::TIMESTAMP AS merged_at,
    a.merged_by,
    COALESCE(a.draft_cycles, 0) AS draft_cycles,
    COALESCE(fp.force_pushes_after_first_review, 0) AS force_pushes_after_first_review
  FROM kb.pull_requests p
  JOIN kb.repos r ON r.repo_id = p.repo_id
  LEFT JOIN ev_agg a ON a.repo_id = p.repo_id AND a.pr_number = p.number
  LEFT JOIN human h ON h.repo_id = p.repo_id AND h.pr_number = p.number
  LEFT JOIN pushes fp ON fp.repo_id = p.repo_id AND fp.pr_number = p.number
)
SELECT
  *,
  CASE
    WHEN first_review_at IS NULL THEN FALSE
    WHEN first_review_requested_at IS NULL THEN TRUE
    ELSE first_review_at < first_review_requested_at
  END AS reviewed_before_request,
  date_diff('minute', first_ready_at, first_review_at) AS ready_to_first_review_min,
  date_diff('minute', first_review_requested_at, first_review_at) AS request_to_first_review_min,
  date_diff('minute', first_ready_at, merged_at) AS ready_to_merge_min
FROM base;

-- One row per review request. `removed_at` is the first matching removal
-- before the same reviewer is requested again; `first_review_at` is that
-- reviewer's first submitted review after the request (never for teams).
CREATE OR REPLACE VIEW v_review_request_latency AS
WITH reqs AS (
  SELECT
    event_id,
    repo_id,
    item_number AS pr_number,
    subject AS reviewer,
    subject_type AS reviewer_type,
    actor AS requested_by,
    created_at::TIMESTAMP AS requested_at,
    LEAD(created_at::TIMESTAMP) OVER (
      PARTITION BY repo_id, item_number, LOWER(subject) ORDER BY created_at::TIMESTAMP, event_id
    ) AS next_requested_at
  FROM kb.timeline_events
  WHERE item_kind = 'pr' AND event_type = 'ReviewRequestedEvent'
),
removals AS (
  SELECT q.event_id, MIN(rm.created_at::TIMESTAMP) AS removed_at
  FROM reqs q
  JOIN kb.timeline_events rm
    ON rm.repo_id = q.repo_id
    AND rm.item_kind = 'pr'
    AND rm.item_number = q.pr_number
    AND rm.event_type = 'ReviewRequestRemovedEvent'
    AND LOWER(rm.subject) = LOWER(q.reviewer)
    AND rm.created_at::TIMESTAMP >= q.requested_at
    AND (q.next_requested_at IS NULL OR rm.created_at::TIMESTAMP < q.next_requested_at)
  GROUP BY q.event_id
),
first_reviews AS (
  SELECT q.event_id, MIN(rv.submitted_at) AS first_review_at
  FROM reqs q
  JOIN v_pr_reviews_all rv
    ON rv.repo_id = q.repo_id
    AND rv.pr_number = q.pr_number
    AND LOWER(rv.author) = LOWER(q.reviewer)
    AND rv.submitted_at >= q.requested_at
  WHERE COALESCE(q.reviewer_type, '') <> 'Team'
  GROUP BY q.event_id
)
SELECT
  r.full_name AS repo,
  q.pr_number,
  r.full_name || '#' || q.pr_number AS item_key,
  q.reviewer,
  q.reviewer_type,
  q.requested_at,
  q.requested_by,
  rm.removed_at,
  fr.first_review_at,
  date_diff('minute', q.requested_at, fr.first_review_at) AS latency_min
FROM reqs q
JOIN kb.repos r ON r.repo_id = q.repo_id
LEFT JOIN removals rm ON rm.event_id = q.event_id
LEFT JOIN first_reviews fr ON fr.event_id = q.event_id;

-- Strongest evidence per (repo, issue, PR): closed_by > closes > xref >
-- references. Adds closed_by for issues closed by a commit that belongs to a
-- PR, and drops pairs whose `issue_number` is actually a PR.
CREATE OR REPLACE VIEW v_issue_pr_links AS
WITH evidence AS (
  SELECT repo_id, issue_number, pr_number, link_type
  FROM kb.issue_pr_links

  UNION ALL

  SELECT e.repo_id, e.item_number AS issue_number, c.pr_number, 'closed_by' AS link_type
  FROM kb.timeline_events e
  JOIN kb.commits c ON c.repo_id = e.repo_id AND c.sha = e.subject
  WHERE e.item_kind = 'issue'
    AND e.event_type = 'ClosedEvent'
    AND e.subject_type = 'Commit'
    AND c.pr_number IS NOT NULL
),
ranked AS (
  SELECT
    *,
    CASE link_type
      WHEN 'closed_by' THEN 1
      WHEN 'closes' THEN 2
      WHEN 'xref' THEN 3
      WHEN 'references' THEN 4
      ELSE 5
    END AS strength
  FROM evidence
)
SELECT
  r.full_name AS repo,
  k.issue_number,
  k.pr_number,
  r.full_name || '#' || k.issue_number AS issue_key,
  r.full_name || '#' || k.pr_number AS pr_key,
  arg_min(k.link_type, k.strength) AS link_type,
  list_sort(list(DISTINCT k.link_type)) AS link_types
FROM ranked k
JOIN kb.repos r ON r.repo_id = k.repo_id
WHERE NOT EXISTS (
  SELECT 1 FROM kb.pull_requests p
  WHERE p.repo_id = k.repo_id AND p.number = k.issue_number
)
GROUP BY r.full_name, k.issue_number, k.pr_number;
