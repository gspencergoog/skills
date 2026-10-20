-- Relational Staged Diff Macros for DuckDB (Layer 2 & Layer 3)
-- Load with: duckdb -batch -dark-mode -init agents/skills/context-indexer/scripts/staged_diff_macros.sql
-- Queries JSONL tables emitted by agents/skills/context-indexer/scripts/stage_diffs.py --grouped

-- 1. Layer 2: Read staged diff_lines.jsonl emitted by stage_diffs.py --grouped
CREATE OR REPLACE MACRO staged_diff_lines(diff_dir) AS TABLE
SELECT *
FROM read_json(
  diff_dir || '/diff_lines.jsonl',
  columns={
    'file_path': 'VARCHAR',
    'chunk_name': 'VARCHAR',
    'hunk_id': 'INTEGER',
    'func_context': 'VARCHAR',
    'op': 'VARCHAR',
    'old_line': 'INTEGER',
    'new_line': 'INTEGER',
    'content': 'VARCHAR'
  },
  format='newline_delimited',
  ignore_errors=true
);

-- 2. Layer 2: Read staged diff_hunks.jsonl emitted by stage_diffs.py --grouped
CREATE OR REPLACE MACRO staged_diff_hunks(diff_dir) AS TABLE
SELECT *
FROM read_json(
  diff_dir || '/diff_hunks.jsonl',
  columns={
    'file_path': 'VARCHAR',
    'chunk_name': 'VARCHAR',
    'hunk_id': 'INTEGER',
    'old_start': 'INTEGER',
    'old_count': 'INTEGER',
    'new_start': 'INTEGER',
    'new_count': 'INTEGER',
    'func_context': 'VARCHAR',
    'added_lines': 'INTEGER',
    'deleted_lines': 'INTEGER'
  },
  format='newline_delimited',
  ignore_errors=true
);

-- 3. Layer 3: Read staged diff_entities.jsonl emitted by stage_diffs.py --grouped --with-entities
CREATE OR REPLACE MACRO staged_diff_entities(diff_dir) AS TABLE
SELECT *
FROM read_json(
  diff_dir || '/diff_entities.jsonl',
  columns={
    'file_path': 'VARCHAR',
    'side': 'VARCHAR',
    'kind': 'VARCHAR',
    'name': 'VARCHAR',
    'signature': 'VARCHAR',
    'line_start': 'INTEGER',
    'line_end': 'INTEGER'
  },
  format='newline_delimited',
  ignore_errors=true
);

-- 4. Layer 3: Range-join added diff lines to innermost enclosing AST symbols
CREATE OR REPLACE MACRO diff_entity_churn(diff_dir) AS TABLE
WITH innermost_matches AS (
  SELECT
    e.file_path,
    e.kind,
    e.name,
    e.signature,
    e.line_start,
    e.line_end,
    l.chunk_name,
    l.new_line
  FROM staged_diff_entities(diff_dir) e
  JOIN staged_diff_lines(diff_dir) l
    ON e.file_path = l.file_path
   AND l.new_line BETWEEN e.line_start AND e.line_end
  WHERE e.side = 'NEW'
    AND l.op = '+'
  QUALIFY row_number() OVER (
    PARTITION BY l.file_path, l.new_line
    ORDER BY (e.line_end - e.line_start) ASC, e.line_start DESC
  ) = 1
)
SELECT
  file_path,
  kind,
  name,
  signature,
  line_start,
  line_end,
  FIRST(chunk_name) AS chunk_name,
  COUNT(*) AS lines_added,
  (line_end - line_start + 1) AS total_entity_lines,
  ROUND(
    COUNT(*) * 100.0 / NULLIF(line_end - line_start + 1, 0),
    1
  ) AS pct_rewritten
FROM innermost_matches
GROUP BY file_path, kind, name, signature, line_start, line_end
ORDER BY lines_added DESC, file_path, line_start;

-- 5. Layer 3: Detect OLD -> NEW signature drift on modified symbols
CREATE OR REPLACE MACRO diff_signature_drift(diff_dir) AS TABLE
SELECT
  n.file_path,
  n.kind,
  n.name,
  o.signature AS old_signature,
  n.signature AS new_signature,
  o.line_start AS old_line_start,
  n.line_start AS new_line_start,
  n.line_end AS new_line_end
FROM staged_diff_entities(diff_dir) o
JOIN staged_diff_entities(diff_dir) n
  ON o.file_path = n.file_path
 AND o.kind = n.kind
 AND o.name = n.name
WHERE o.side = 'OLD'
  AND n.side = 'NEW'
  AND o.signature != n.signature
ORDER BY n.file_path, n.line_start;

-- 6. Layer 3: Find modified production functions/methods not referenced in test diffs
CREATE OR REPLACE MACRO diff_untested_functions(diff_dir) AS TABLE
WITH prod_mods AS (
  SELECT
    file_path,
    kind,
    name,
    signature,
    chunk_name,
    lines_added,
    pct_rewritten
  FROM diff_entity_churn(diff_dir)
  WHERE kind IN ('function', 'method')
    AND NOT regexp_matches(file_path, '(^|/)(test_|.*_test\.|.*\.test\.|.*\.spec\.|tests?/)')
    AND NOT starts_with(name, '_')
),
test_mentions AS (
  SELECT DISTINCT content
  FROM staged_diff_lines(diff_dir)
  WHERE op IN ('+', ' ')
    AND regexp_matches(file_path, '(^|/)(test_|.*_test\.|.*\.test\.|.*\.spec\.|tests?/)')
)
SELECT
  p.file_path,
  p.kind,
  p.name,
  p.signature,
  p.chunk_name,
  p.lines_added,
  p.pct_rewritten
FROM prod_mods p
WHERE NOT EXISTS (
  SELECT 1
  FROM test_mentions t
  WHERE contains(t.content, p.name)
)
ORDER BY p.lines_added DESC, p.file_path;
