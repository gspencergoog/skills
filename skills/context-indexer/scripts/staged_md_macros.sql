-- Relational Staged Markdown Macros for DuckDB (Layer 2)
-- Load with: duckdb -batch -dark-mode -init agents/skills/context-indexer/scripts/staged_md_macros.sql
-- Queries JSONL tables emitted by agents/skills/context-indexer/scripts/stage_markdown.py

-- 1. Read staged md_sections.jsonl emitted by stage_markdown.py
CREATE OR REPLACE MACRO staged_md_sections(md_dir) AS TABLE
SELECT *
FROM read_json(
  md_dir || '/md_sections.jsonl',
  columns={
    'file_path': 'VARCHAR',
    'section_id': 'INTEGER',
    'level': 'INTEGER',
    'heading': 'VARCHAR',
    'breadcrumb': 'VARCHAR',
    'anchor_slug': 'VARCHAR',
    'chunk_name': 'VARCHAR',
    'line_start': 'INTEGER',
    'line_end': 'INTEGER',
    'subtree_line_end': 'INTEGER',
    'own_lines': 'INTEGER',
    'subtree_lines': 'INTEGER',
    'own_code_blocks': 'INTEGER',
    'content': 'VARCHAR'
  },
  format='newline_delimited',
  ignore_errors=true
);

-- 2. Read staged md_code_blocks.jsonl emitted by stage_markdown.py
CREATE OR REPLACE MACRO staged_md_code_blocks(md_dir) AS TABLE
SELECT *
FROM read_json(
  md_dir || '/md_code_blocks.jsonl',
  columns={
    'file_path': 'VARCHAR',
    'block_id': 'INTEGER',
    'section_id': 'INTEGER',
    'section_heading': 'VARCHAR',
    'lang': 'VARCHAR',
    'line_start': 'INTEGER',
    'line_end': 'INTEGER',
    'code_lines': 'INTEGER',
    'code': 'VARCHAR'
  },
  format='newline_delimited',
  ignore_errors=true
);

-- 3. Read staged md_tables.jsonl emitted by stage_markdown.py
CREATE OR REPLACE MACRO staged_md_tables(md_dir) AS TABLE
SELECT *
FROM read_json(
  md_dir || '/md_tables.jsonl',
  columns={
    'file_path': 'VARCHAR',
    'table_id': 'INTEGER',
    'section_id': 'INTEGER',
    'section_heading': 'VARCHAR',
    'row_index': 'INTEGER',
    'line_num': 'INTEGER',
    'headers': 'VARCHAR[]',
    'cells': 'JSON'
  },
  format='newline_delimited',
  ignore_errors=true
);

-- 4. Read staged md_frontmatter.jsonl emitted by stage_markdown.py
CREATE OR REPLACE MACRO staged_md_frontmatter(md_dir) AS TABLE
SELECT *
FROM read_json(
  md_dir || '/md_frontmatter.jsonl',
  columns={
    'file_path': 'VARCHAR',
    'has_frontmatter': 'BOOLEAN',
    'line_end': 'INTEGER',
    'attributes': 'JSON',
    'tags': 'VARCHAR[]'
  },
  format='newline_delimited',
  ignore_errors=true
);

-- 5. Read staged md_links.jsonl emitted by stage_markdown.py
CREATE OR REPLACE MACRO staged_md_links(md_dir) AS TABLE
SELECT *
FROM read_json(
  md_dir || '/md_links.jsonl',
  columns={
    'file_path': 'VARCHAR',
    'line_num': 'INTEGER',
    'section_id': 'INTEGER',
    'section_heading': 'VARCHAR',
    'label': 'VARCHAR',
    'raw_target': 'VARCHAR',
    'target_path': 'VARCHAR',
    'anchor': 'VARCHAR',
    'is_external': 'BOOLEAN',
    'target_exists': 'BOOLEAN'
  },
  format='newline_delimited',
  ignore_errors=true
);

-- 6. Find broken relative file links and unresolved #anchor links across staged docs
CREATE OR REPLACE MACRO staged_md_broken_links(md_dir) AS TABLE
WITH staged_files AS (
  SELECT DISTINCT file_path
  FROM staged_md_sections(md_dir)
),
valid_anchors AS (
  SELECT DISTINCT file_path, anchor_slug
  FROM staged_md_sections(md_dir)
  WHERE anchor_slug != ''
)
SELECT
  l.file_path,
  l.line_num,
  l.section_id,
  l.section_heading,
  l.label,
  l.raw_target,
  l.target_path,
  l.anchor,
  CASE
    WHEN NOT l.target_exists THEN 'missing_file'
    ELSE 'missing_anchor'
  END AS reason
FROM staged_md_links(md_dir) l
WHERE NOT l.is_external
  AND (
    NOT l.target_exists
    OR (
      l.anchor != ''
      AND EXISTS (
        SELECT 1 FROM staged_files sf WHERE sf.file_path = l.target_path
      )
      AND NOT EXISTS (
        SELECT 1
        FROM valid_anchors va
        WHERE va.file_path = l.target_path
          AND va.anchor_slug = l.anchor
      )
    )
  )
ORDER BY l.file_path, l.line_num;

-- 7. Extract uppercase RFC 2119 normative requirements outside fenced code blocks
CREATE OR REPLACE MACRO staged_md_requirements(md_dir) AS TABLE
WITH split_sections AS (
  SELECT
    s.file_path,
    s.section_id,
    s.heading AS section_heading,
    s.breadcrumb,
    s.chunk_name,
    s.line_start,
    string_split(s.content, chr(10)) AS lines_arr
  FROM staged_md_sections(md_dir) s
),
section_lines AS (
  SELECT
    file_path,
    section_id,
    section_heading,
    breadcrumb,
    chunk_name,
    line_start + unnest(range(1, len(lines_arr) + 1)) - 1 AS line_num,
    unnest(lines_arr) AS line
  FROM split_sections
)
SELECT
  sl.file_path,
  sl.section_id,
  sl.section_heading,
  sl.breadcrumb,
  sl.chunk_name,
  sl.line_num,
  regexp_extract(
    sl.line,
    '\b(MUST NOT|SHALL NOT|SHOULD NOT|MUST|SHALL|SHOULD|REQUIRED|RECOMMENDED|MAY|OPTIONAL)\b',
    1
  ) AS keyword,
  trim(sl.line) AS statement
FROM section_lines sl
ANTI JOIN staged_md_code_blocks(md_dir) cb
  ON sl.file_path = cb.file_path
 AND sl.line_num BETWEEN cb.line_start AND cb.line_end
WHERE regexp_matches(
  sl.line,
  '\b(MUST NOT|SHALL NOT|SHOULD NOT|MUST|SHALL|SHOULD|REQUIRED|RECOMMENDED|MAY|OPTIONAL)\b'
)
ORDER BY sl.file_path, sl.line_num;
