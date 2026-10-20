---
name: context-indexer
description: >-
  Stages large code diffs and Markdown documents into size-bounded chunk files
  and relational JSONL tables for DuckDB SQL: diff lines, hunks, and AST symbol
  spans (stage_diffs.py and staged_diff_macros.sql), plus Markdown sections,
  code blocks, pipe tables, frontmatter, and links (stage_markdown.py and
  staged_md_macros.sql). Use when splitting a diff (> 150 lines) or spec (> 500
  lines) into chunks, ranking modified functions by churn, detecting signature
  drift or untested changes, querying Markdown tables, frontmatter, or sections
  across docs and conversation artifacts (brain/*/*.md), extracting RFC 2119
  statements, or checking broken file and #anchor links. Flags: --grouped,
  --with-entities, --split-chunks. Don't use for single-pattern text searches
  (use unix-cli-best-practices).
---

# Context Indexer (Relational Diff & Markdown Staging)

Stages unified code diffs and Markdown documents into size-bounded chunk files
and newline-delimited JSON (`.jsonl`) tables for DuckDB SQL queries. All scripts
use the Python 3 stdlib with zero third-party dependencies.

## 1. Tool Selection Thresholds

Choose the lightest tool that fits the file size and query structure:

| Artifact & Size | Recommended Tool | Why |
| :--- | :--- | :--- |
| Single-line pattern in working tree | `rg -n` and `fd` | Runs in under 5 ms when code-fence state, section hierarchy, and hunk line numbers do not matter. |
| Searching past conversations (`transcript.jsonl` + `conversation_summaries.db`) | `skills/duckdb` (`ATTACH ... TYPE SQLITE` + `read_json`) | Joins `transcript.jsonl` matches (`content` and `tool_calls`) with top-level conversation titles in a single SQL query without staging files. |
| Single Markdown file `< 350` lines (`< 15 KB`) | `read_file` (`view_file`) | One call loads the entire document in 1,500 to 3,500 tokens. |
| Markdown file `>= 350` lines or `10+` files (section outlines & code blocks across docs or `brain/*/*.md` artifacts) | Pure-SQL `skills/duckdb/scripts/markdown_macros.sql` (`read_md_sections`, `read_md_code_blocks`) | Indexes ATX headings and 3/4/5-backtick code fences in 70 to 320 ms without staging files. |
| Markdown spec `> 500` lines, pipe tables, YAML frontmatter, Setext headings, `#anchor` link checks, or `brain/*/*.md` artifact tables | [scripts/stage_markdown.py](scripts/stage_markdown.py) + [scripts/staged_md_macros.sql](scripts/staged_md_macros.sql) | Extracts keyed JSON table cells, multi-line YAML frontmatter, RFC 2119 statements, broken `#anchor` links, and size-bounded `md_chunk_NN.md` chunks. |
| Raw unified diff (`<= 500` lines, line/hunk filtering only) | Pure-SQL `skills/duckdb/scripts/diff_macros.sql` (`read_diff_lines`, `read_diff_hunks`) | Streams `git diff` over `/dev/stdin` with zero disk staging. |
| Diff `> 150` lines (chunking) or `> 500` lines (AST entity churn, signature drift, untested functions) | [scripts/stage_diffs.py](scripts/stage_diffs.py) + [scripts/staged_diff_macros.sql](scripts/staged_diff_macros.sql) | Packs hunks into `diff_chunk_NN.diff` files (`<= 35 KB`, `<= 700` lines) and range-joins modified lines to enclosing AST functions and classes. |

Always pass `-batch -dark-mode` when invoking `duckdb` in non-interactive
subshells to avoid the 5-second OSC terminal probe timeout.

---

## 2. Staging & Querying Code Diffs (`stage_diffs.py`)

Run [scripts/stage_diffs.py](scripts/stage_diffs.py) with `--grouped` to pack a
diff into size-bounded chunk files (`diff_chunk_01.diff`, `diff_chunk_02.diff`,
…) and emit relational JSONL tables into `--output-dir`:

```bash
diff_dir=$(mktemp -d -t staged-diffs-XXXXXX)
git diff main...HEAD | python3 <skills-dir>/context-indexer/scripts/stage_diffs.py \
  --grouped --repo-dir . --base-ref main \
  --max-bytes 35000 --max-lines 700 --output-dir "$diff_dir"
```

### Emitted Files in `$diff_dir`

- `manifest.json`: Lists every `diff_chunk_NN.diff` file and the source files it
  covers.
- `diff_lines.jsonl`: Every added (`+`), deleted (`-`), and context (` `) line
  with monotonic 1-indexed `old_line` and `new_line` numbers and `chunk_name`.
- `diff_hunks.jsonl`: Every `@@` hunk header with `old_start`, `new_start`,
  `func_context`, `added_lines`, and `deleted_lines`.
- `diff_entities.jsonl`: Enclosing `OLD` and `NEW` AST symbol spans (`function`,
  `method`, `class`, `schema_def`) with `signature`, `line_start`, and
  `line_end` when `--repo-dir` or `--with-entities` is supplied.

### Querying Staged Diffs (`staged_diff_macros.sql`)

Load [scripts/staged_diff_macros.sql](scripts/staged_diff_macros.sql) into
`duckdb`:

```bash
duckdb -batch -dark-mode -box \
  -init <skills-dir>/context-indexer/scripts/staged_diff_macros.sql -c "
-- 1. Rank modified functions and methods by added lines and rewrite percentage
SELECT file_path, kind, name, chunk_name, lines_added, pct_rewritten
FROM diff_entity_churn('$diff_dir')
LIMIT 15;

-- 2. Detect signature changes between OLD and NEW
SELECT file_path, kind, name, old_signature, new_signature
FROM diff_signature_drift('$diff_dir');

-- 3. Identify modified production functions not mentioned in any test diff
SELECT file_path, kind, name, chunk_name, lines_added
FROM diff_untested_functions('$diff_dir');

-- 4. Find symbols deleted from OLD without a matching replacement in NEW
SELECT o.file_path, o.kind, o.name, o.signature, o.line_start AS old_line
FROM staged_diff_entities('$diff_dir') o
ANTI JOIN staged_diff_entities('$diff_dir') n
  ON o.file_path = n.file_path
 AND o.kind = n.kind
 AND o.name = n.name
 AND n.side = 'NEW'
WHERE o.side = 'OLD'
ORDER BY o.file_path, o.line_start;
"
```

Clean up `$diff_dir` (`rm -rf "$diff_dir"`) when the task completes.

---

## 3. Staging & Querying Markdown Documents (`stage_markdown.py`)

Run [scripts/stage_markdown.py](scripts/stage_markdown.py) on one or more
Markdown files or globs (or pass `-` to read file paths from stdin). Add
`--split-chunks` when a spec exceeds 500 lines and needs size-bounded
`md_chunk_NN.md` files for subagents:

```bash
md_dir=$(mktemp -d -t staged-md-XXXXXX)
python3 <skills-dir>/context-indexer/scripts/stage_markdown.py \
  --root . --split-chunks --max-bytes 35000 --max-lines 700 \
  --output-dir "$md_dir" "docs/**/*.md"
```

### Emitted Files in `$md_dir`

- `md_sections.jsonl`: Every ATX and Setext section with `section_id`, `level`,
  `heading`, hierarchical `breadcrumb`, GitHub/g3doc `anchor_slug`,
  `chunk_name`, `line_start`, `line_end`, `subtree_line_end`, `own_lines`,
  `subtree_lines`, `own_code_blocks`, and `content`.
- `md_code_blocks.jsonl`: Every fenced code block (including 3/4/5-backtick,
  tilde, and blockquote-indented fences) with `section_heading`, `lang`,
  `line_start`, `line_end`, and clean `code`.
- `md_tables.jsonl`: Every GFM pipe table row with `headers` and keyed JSON
  `cells` (preserving pipes inside inline code spans).
- `md_frontmatter.jsonl`: Parsed YAML frontmatter `attributes` (including
  multi-line `>-` and `|` block scalars and lists) and inline `#tags`.
- `md_links.jsonl`: Every inline `[label](target)` and reference `[label][id]`
  link outside code spans with resolved `target_path`, `anchor`, `is_external`,
  and `target_exists`.
- `manifest.json` and `md_chunk_NN.md` (when `--split-chunks` is passed).

### Querying Staged Markdown (`staged_md_macros.sql`)

Load [scripts/staged_md_macros.sql](scripts/staged_md_macros.sql) into `duckdb`:

```bash
duckdb -batch -dark-mode -box \
  -init <skills-dir>/context-indexer/scripts/staged_md_macros.sql -c "
-- 1. Outline top-level sections with subtree line ranges and chunk assignments
SELECT file_path, section_id, level, heading, chunk_name, line_start, subtree_line_end
FROM staged_md_sections('$md_dir')
WHERE level <= 2;

-- 2. Check broken relative file links and unresolved #anchor slugs
SELECT file_path, line_num, label, raw_target, reason
FROM staged_md_broken_links('$md_dir');

-- 3. Extract uppercase RFC 2119 normative statements outside code blocks
SELECT file_path, section_heading, line_num, keyword, statement
FROM staged_md_requirements('$md_dir');

-- 4. Query pipe table cells as JSON objects
SELECT file_path, section_heading, line_num, cells
FROM staged_md_tables('$md_dir')
LIMIT 20;
"
```

Clean up `$md_dir` (`rm -rf "$md_dir"`) when the task completes.
