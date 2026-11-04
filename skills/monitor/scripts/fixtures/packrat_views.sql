-- Test double for the packrat reader contract (agents/skills/packrat/references/reader_contract.md).
-- Each contract view is a plain table with the same name and columns, so /monitor's reader can
-- be tested without importing packrat.

CREATE TABLE v_pr_basic (
  item_key TEXT PRIMARY KEY, repo_full_name TEXT, repo_mode TEXT, number INTEGER, title TEXT,
  state TEXT, is_draft INTEGER, url TEXT, author TEXT, created_at TEXT, updated_at TEXT,
  merged_at TEXT, closed_at TEXT, base_ref TEXT, head_ref TEXT, head_sha TEXT,
  head_repo_owner TEXT, is_cross_repository INTEGER, body TEXT DEFAULT ''
);

CREATE TABLE v_pr_live (
  item_key TEXT PRIMARY KEY, repo_full_name TEXT, repo_mode TEXT, number INTEGER, node_id TEXT,
  title TEXT, url TEXT, state TEXT, is_draft INTEGER, author TEXT, author_type TEXT,
  base_ref TEXT, head_ref TEXT, head_sha TEXT, head_committed_date TEXT, head_repo_owner TEXT,
  is_cross_repository INTEGER, mergeable TEXT, merge_state_status TEXT, review_decision TEXT,
  auto_merge_enabled_at TEXT, created_at TEXT, updated_at TEXT, merged_at TEXT, closed_at TEXT,
  live_fetched_at TEXT, rollup_state TEXT, rollup_total INTEGER, rollup_fetched_at TEXT
);

CREATE TABLE v_pr_checks (
  item_key TEXT, head_oid TEXT, run_key TEXT, kind TEXT, name TEXT, status TEXT,
  conclusion TEXT, details_url TEXT, started_at TEXT, completed_at TEXT,
  is_required INTEGER, workflow_run_id INTEGER
);

CREATE TABLE v_pr_review_requests (item_key TEXT, reviewer TEXT, reviewer_kind TEXT);

CREATE TABLE v_pr_latest_reviews (
  item_key TEXT, author TEXT, author_type TEXT, state TEXT, submitted_at TEXT
);

CREATE TABLE v_pr_threads (
  item_key TEXT, thread_id TEXT, is_resolved INTEGER, is_outdated INTEGER, last_author TEXT,
  last_author_type TEXT, last_at TEXT
);

CREATE TABLE v_viewer_items (
  set_name TEXT, item_key TEXT, first_seen_at TEXT, last_seen_at TEXT
);

CREATE TABLE v_service_health (
  pid INTEGER, started_at TEXT, beat_at TEXT, version TEXT, beat_age_s INTEGER,
  graphql_remaining INTEGER, graphql_reset_at INTEGER, core_remaining INTEGER,
  core_reset_at INTEGER, active_leases INTEGER, viewer_login TEXT
);
