---
name: duckdb
description: >-
  Queries, inspects, transforms, and converts local structured data files
  (JSON, JSONL, NDJSON, CSV, TSV, Parquet, SQLite), unified code diffs, and
  Markdown documents on Linux and macOS with DuckDB SQL (duckdb -batch
  -dark-mode). Use when querying local JSON, CSV, or SQLite files, benchmark
  artifacts, large diffs, or Markdown corpora, inspecting schemas and row
  counts, joining JSONL logs with SQLite indexes (such as transcript.jsonl and
  conversation_summaries.db), joining across formats, converting to Parquet, or
  filtering stdin pipelines. Prefer it over ad hoc Python or jq. Flags: -batch,
  -dark-mode, -agent, -no-stdin, -box, -json, -csv, -markdown, -manual,
  -format, -init, -c. Don't use for remote cloud data warehouses (BigQuery,
  Snowflake, Spanner) or PLx dashboards.
---

# DuckDB CLI Analytics

Fast analytical SQL engine for querying and converting local data files (JSON,
JSONL, CSV, TSV, Parquet, SQLite), code diffs, and Markdown files from the
shell. It runs without database servers or external imports.

## Quick Start & Execution Setup

DuckDB runs as a standalone CLI binary. Always pass **`-dark-mode`** on every
invocation (including `duckdb -dark-mode --version` and `--help`), and pass
**`-batch -dark-mode`** on all queries and `-manual` lookups.

### Installation & Subshell PATH Setup (Linux & macOS)

To prevent `exit code 127: duckdb: command not found` in non-interactive agent
subshells (where `mise` or `asdf` shims are not sourced):

- Pin `"github:duckdb/duckdb" = "2"` in `~/.config/mise/config.toml` to track
  stable `2.x` releases with SLSA provenance verification, and put
  `~/.local/share/mise/shims` on `PATH` in the startup files that
  non-interactive shells read (`~/.zshenv`, `BASH_ENV`, or
  `~/.config/environment.d/*.conf`) (Recommended). The shims are symlinks to
  `mise` itself, so they resolve even when `mise` is off `PATH` and cover every
  managed tool. Keep `~/.local/bin` (and any user command directory such as
  `~/cmd`) before the shims directory so custom wrappers still win.
- Make the binary directly accessible at `~/.local/bin/duckdb` (or
  `/opt/homebrew/bin/duckdb` on Apple Silicon macOS) when shims are unavailable.
  Note that a per-tool symlink collides with any dotfiles repository that tracks
  the same path:

  ```bash
  # Option A: If installed via mise ("github:duckdb/duckdb" = "2"), symlink into ~/.local/bin/
  ln -sf ~/.local/share/mise/installs/github-duckdb-duckdb/2/duckdb ~/.local/bin/duckdb

  # Option B: macOS Homebrew
  brew install duckdb

  # Option C: Direct download from GitHub Releases (v2.0+ uses .tar.gz archives;
  # replace linux-amd64 with linux-arm64 or osx-arm64 as needed)
  curl -fsSL https://github.com/duckdb/duckdb/releases/latest/download/duckdb-cli-linux-amd64.tar.gz \
    | tar -xzf - -C ~/.local/bin/ duckdb
  ```

### Critical Flags & DuckDB `v2.0+` Agent Mode

- Always pass `-batch -dark-mode` together in subshells:
  - Pass `-dark-mode` (or `-light-mode`) on every invocation. DuckDB probes the
    terminal background color via OSC sequences whenever `stdout` is a TTY, even
    before handling `--version`, `--help`, `-manual`, or `-format`. In
    PTY-allocated agent subshells this probe times out after 5 seconds
    (`Timeout trying to read terminal background color (> 5s elapsed)`), and
    neither `-batch`, `-agent`, nor `DUCKDB_AGENT_MODE=1` disables it.
  - Pass `-batch` to disable interactive prompts, progress bars, and `less`
    paging. It is also required with `v2.0+` `-manual <fn>`, which otherwise
    retains interactive detection and triggers both the 5-second color probe and
    the pager.
  - Pass `-no-stdin` to exit after processing CLI options instead of reading
    `stdin` when spawned from child-process runners whose `stdin` pipe stays
    open (omit when streaming data from `/dev/stdin`).
- Use Agent Mode on `v2.0+` (`-agent`, `-no-agent`, `AI_AGENT=1`,
  `DUCKDB_AGENT_MODE=1`):
  - Agent Mode renders compact unpadded Markdown tables with inline column types
    (`| col:TYPE |`), loud head and tail caps (`1,000` rows, `10,000` bytes,
    `500`-char cells via `.maxrows` / `.maxbytes`), a row-count and
    order-independent hash footer, JSON errors and `>1M`-row progress on
    `stderr`, and 1-line-per-operator `EXPLAIN` plans. Because row and byte caps
    remain active even under `-batch` and `> file` redirection, use `-csv` or
    `-json` for untruncated exports.
  - Omit `-box` on exploratory queries. `duckdb -batch -dark-mode` defaults to
    `box` on `v1.x`, and on `v2.0+` built-in Agent Mode auto-detection
    (`CLAUDECODE`, `GEMINI_CLI`, `CURSOR_AGENT`, `CODEX_*`, `COPILOT_*`,
    `AI_AGENT`, `AGENT`) activates when `stdout` is not a TTY and no output
    flag is set. Passing `-box` disables auto-detection and overrides `-agent`
    (`3x-5x` whitespace padding and silent `>40`-row truncation); pass `-agent`
    explicitly in PTY subshells where `stdout` is a TTY.
  - In `PATH` wrappers, pair `export AI_AGENT=1; unset DUCKDB_AGENT_MODE`
    because `DUCKDB_AGENT_MODE=1` keeps `agent_mode_active = true` on `stderr`
    even with `-json`, `-csv`, or `-markdown`, and inspect `"$@"` before
    prepending `-dark-mode` or `-batch` so `-light-mode` and `-interactive`
    still win.
- Output formatting and utility flags:
  - Use `-json` or `-jsonlines` to output structured JSON arrays or NDJSON rows
    (`0` bytes on `stderr`).
  - Use `-csv` to output uncapped comma-separated rows with headers (use over
    `-agent` for full-table exports or large `read_md_sections` scans).
  - Use `-markdown` to print column-padded GFM pipe tables without `:TYPE` or
    hash footers.
  - Use `-box` or `-no-agent` for human-oriented padded Unicode boxed tables.
  - Use `-manual <function>` (`v2.0+`) to print offline function signatures,
    descriptions, and examples (`duckdb -batch -dark-mode -manual regexp_extract`).
  - Use `-format` or `-format-file <path.sql>` (`v2.0+`) to format SQL to
    `stdout` (emits ANSI color codes on a TTY; pass `-dark-mode` and pipe
    `| cat` or redirect `> out.sql` for plain SQL text).
  - Use `-bail` to stop execution on the first error.

```bash
# Exploratory agent query (v2.0+ compact typed Markdown + hash; omit -box)
duckdb -batch -dark-mode -agent -c "SELECT 42 AS answer;"

# Version-neutral exploratory query (box on v1.x; Agent Mode when piped or wrapped on v2.0+)
duckdb -batch -dark-mode -c "SELECT 42 AS answer;"

# Programmatic JSON output (add -no-stdin when spawned from a child-process runner)
duckdb -batch -no-stdin -dark-mode -json -c "SELECT 42 AS answer;"

# Offline SQL function manual lookup & plain-text SQL formatting (v2.0+)
duckdb -batch -dark-mode -manual regexp_extract
printf "select a, b from tbl where a = 1;\n" | duckdb -dark-mode -format | cat
```

---

## Core Gotchas & Invariants

1. Passing `-batch`, `-agent`, `DUCKDB_AGENT_MODE=1`, `--version`, `--help`,
   `-manual`, or `-format` without `-dark-mode` does not disable DuckDB's
   5-second OSC probe for the terminal color on a TTY, and `-manual` without
   `-batch` retains interactive paging. Always pass `-batch -dark-mode`.
2. DuckDB follows ANSI SQL 1-based array indexing: `arr[1]` is the first
   element, and `arr[0]` produces an out-of-bounds error.
3. When reading JSONL with varying keys across rows, DuckDB defaults to a shared
   schema and may drop extra keys. Pass `union_by_name = true` inside
   `read_json` or `read_json_auto`.
4. In log files or interrupted processes, malformed JSON lines abort the query.
   Pass `ignore_errors = true` to skip broken lines.
5. Files ending in `.gz` or `.zst` are read automatically without manual
   decompression.
6. When scanning thousands of JSONL files (such as `transcript.jsonl`), passing
   an explicit `columns={...}` map skips schema inference and speeds up scans by
   3x. Typing a nested object or array column as `'JSON'` or `'JSON[]'` returns
   raw JSON-encoded strings with embedded quotes, which breaks equality checks.
   - Type nested objects as `STRUCT(...)[]` (for example,
     `columns={'tool_calls': 'STRUCT(name VARCHAR, args JSON)[]'}`), or extract
     strings with `->>` (`tc->>'name' = 'run_command'`).
7. Both native DuckDB databases and SQLite databases often use `.db` extensions.
   Running `ATTACH 'path/to/file.db' AS db (TYPE SQLITE)` on a DuckDB file fails
   with `file is not a database`. Run `file path/to/file.db` first when the
   format is unknown.
8. DuckDB `v2.0+` writes storage version `69` (`v2.0.0+`) on new `.duckdb`
   files, which `v1.5.x` and earlier cannot open (while `v2.0+` reads existing
   `v1.x` files transparently). Pass `-storage-version v1.5.0` for `v1.5.x`
   readers (or `v1.2.0` for `v1.2+`; check via
   `SELECT tags['storage_version'] FROM duckdb_databases()`).

---

## Quick Format & Streaming Patterns

### 1. JSON / JSONL & Nested Unnesting

```bash
# Preview JSONL with irregular schema / interrupted lines tolerance
duckdb -batch -dark-mode -c "SELECT * FROM read_json('logs/*.jsonl', union_by_name=true, ignore_errors=true) LIMIT 10;"

# Dot access, 1-based array indexing, and unnest()
duckdb -batch -dark-mode -c "SELECT id, user.email, tags[1] AS primary_tag, unnest(tags) AS tag FROM 'users.jsonl';"
```

### 2. CSV, TSV & Parquet

```bash
# Auto-detect CSV/TSV or read compressed .csv.gz directly
duckdb -batch -dark-mode -c "SELECT * FROM 'export.csv.gz' LIMIT 10;"

# Read Parquet globs and inspect schema without scanning rows
duckdb -batch -dark-mode -c "SELECT * FROM 'metrics/*.parquet' WHERE value > 100 LIMIT 10; SELECT * FROM parquet_schema('metrics/part-00.parquet');"
```

### 3. Standard Input Streaming (`/dev/stdin`)

```bash
curl -s "https://api.example.com/items" | \
  duckdb -batch -dark-mode -c "SELECT * FROM read_json('/dev/stdin') LIMIT 10;"
```

### 4. Local Databases (`.db`, `.duckdb`, `.sqlite`)

Always check `file path/to/database.db` first:

```bash
# Native DuckDB database file
duckdb -batch -dark-mode path/to/database.db -c "SHOW TABLES;"

# SQLite 3.x database file
duckdb -batch -dark-mode -c "
ATTACH 'path/to/database.db' AS db (TYPE SQLITE, READ_ONLY);
SELECT * FROM db.my_table LIMIT 10;
"
```

---

## SQL Cookbook & Advanced Recipes

Read [`references/recipes.md`](references/recipes.md),
[`scripts/diff_macros.sql`](scripts/diff_macros.sql), and
[`scripts/markdown_macros.sql`](scripts/markdown_macros.sql) for detailed SQL
patterns:

- Agent conversation transcript mining across
  `~/.gemini/*/brain/*/.system_generated/logs/transcript.jsonl` and
  `~/.claude/projects/**/*.jsonl`, searching and joining JSONL log globs with
  SQLite index databases (`ATTACH ... (TYPE SQLITE, READ_ONLY)`, such as joining
  `transcript.jsonl` with `conversation_summaries.db`), tool usage and error
  audits, and stripping `EPHEMERAL_MESSAGE` `<memory>` echoes.
- Format conversions to ZSTD Parquet (`COPY ... TO`), Hive-partitioned reads and
  writes, and cross-format SQL joins (JSON + CSV + Parquet + SQLite).
- Benchmark JSON and log extraction (`<<<BENCH_PRESS_JSON_START>>>`), pivoting
  multi-target (`aot` / `jit` / `wasm`) benchmark results, and tuning memory and
  thread limits for `>10GB` datasets.
- Relational code diff querying with
  [`scripts/diff_macros.sql`](scripts/diff_macros.sql) across unified code diffs
  (`read_diff_lines`, `read_diff_hunks`).
- Relational Markdown indexing with
  [`scripts/markdown_macros.sql`](scripts/markdown_macros.sql) (`read_md_sections`,
  `read_md_code_blocks`) for large documents (`>= 350` lines) and multi-file
  Markdown corpora (`10+` files).
- When a query produces a quantitative series, distribution, or `bench_press`
  target comparison with 4 or more data points (or 2+ series across 3+
  categories) sharing a single unit axis and the `a2ui-pane` plugin is available
  (`render_a2ui` is reachable as a native or lazy MCP tool), read the `genui`
  skill and pair the Markdown table with an inline `Chart` (`mode: "inline"`
  when compact under 500px) or `metrics_dashboard` in the pane (`mode: "pane"`).
  Keep 1 to 3 scalar numbers, mixed-unit rows, and string-heavy lookup tables in
  plain Markdown.
