-- DuckDB v2.0 read-only analytical views and vector macros for packrat.
INSTALL sqlite;
LOAD sqlite;

ATTACH IF NOT EXISTS '{{DB_PATH}}'
  AS kb (TYPE SQLITE, READ_ONLY);

CREATE OR REPLACE MACRO kb_vec(v_json) AS
  from_json(v_json, '["FLOAT"]')::FLOAT[384];

CREATE OR REPLACE MACRO kb_cosine(v_json, q_vec) AS
  list_cosine_similarity(from_json(v_json, '["FLOAT"]')::FLOAT[384], q_vec::FLOAT[384]);

CREATE OR REPLACE VIEW v_pull_requests AS
SELECT r.full_name AS repo, p.*
FROM kb.pull_requests p
JOIN kb.repos r ON r.repo_id = p.repo_id;

CREATE OR REPLACE VIEW v_issues AS
SELECT r.full_name AS repo, i.*
FROM kb.issues i
JOIN kb.repos r ON r.repo_id = i.repo_id;

CREATE OR REPLACE VIEW v_review_threads AS
SELECT r.full_name AS repo, rt.*
FROM kb.review_threads rt
JOIN kb.repos r ON r.repo_id = rt.repo_id;

CREATE OR REPLACE VIEW v_code_symbols AS
SELECT r.full_name AS repo, cs.*
FROM kb.code_symbols cs
JOIN kb.repos r ON r.repo_id = cs.repo_id;

CREATE OR REPLACE VIEW v_doc_sections AS
SELECT r.full_name AS repo, ds.*
FROM kb.doc_sections ds
JOIN kb.repos r ON r.repo_id = ds.repo_id;

CREATE OR REPLACE VIEW v_chunk_vectors AS
SELECT
  r.full_name AS repo,
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
  ce.vector_json
FROM kb.chunks c
JOIN kb.repos r ON r.repo_id = c.repo_id
JOIN kb.chunk_embeddings ce ON ce.chunk_id = c.chunk_id;

CREATE OR REPLACE VIEW v_file_hotspots AS
SELECT
  r.full_name AS repo,
  cf.file_path,
  COUNT(DISTINCT cf.sha) AS commit_count,
  COUNT(DISTINCT rt.thread_id) AS review_thread_count,
  COUNT(DISTINCT CASE WHEN rt.is_resolved = 0 THEN rt.thread_id END) AS unresolved_thread_count
FROM kb.commit_files cf
JOIN kb.repos r ON r.repo_id = cf.repo_id
LEFT JOIN kb.review_threads rt
  ON rt.repo_id = cf.repo_id AND rt.file_path = cf.file_path
GROUP BY r.full_name, cf.file_path;
