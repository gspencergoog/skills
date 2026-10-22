# Read-Only Codebase Discovery Guide

This guide defines read-only procedures for scouting a target codebase, specification corpus, or branch diff before writing an implementation plan. Every plan must cite real file paths, existing abstractions, and runnable verification commands while keeping context token usage low.

______________________________________________________________________

## 1. Size-Routed Investigation Matrix & Slice-Before-Reading Rule

> [!IMPORTANT]
> **Slice Before Reading (`>= 350` lines)**: Never call `read_file` (`view_file`) on a whole file of 350 or more lines. Index its section spans (`duckdb` or `stage_markdown.py` for Markdown) or symbol lines (`rg -n` or `sem` for code) first, then read only `StartLine`..`EndLine`.

Choose the lightest discovery tool that fits the target format and size:

| Target Artifact & Size | Tool to Use | Why It Saves Tokens & Time |
| :--- | :--- | :--- |
| Prior conversation decisions, related sessions, or past `*plan*.md` artifacts | [duckdb](../../duckdb/SKILL.md) (`duckdb -batch -dark-mode` over `conversation_summaries.db` + `read_md_sections('brain/*/*plan*.md')`) | Recovers settled architectural rulings, non-goals, and file inventories in one 200 ms SQL query instead of re-auditing the repository. |
| Fast point lookups and source files `< 350` lines (`< 15 KB`) | `rg -n` and `fd` ([unix-cli-best-practices](../../unix-cli-best-practices/SKILL.md)), then `read_file` with `StartLine`..`EndLine` | Finds exact symbol definitions and call sites in under 5 ms without loading whole modules. |
| Markdown specs, blueprints, or audits `>= 350` lines or `10+` files | `duckdb` with [markdown_macros.sql](../../duckdb/scripts/markdown_macros.sql) (`read_md_sections`, `read_md_code_blocks`) | Extracts ATX heading hierarchies, breadcrumbs, and `line_start..subtree_line_end` spans in 70 to 300 ms with zero disk staging. |
| Specs `> 500` lines, pipe tables, YAML frontmatter, `#anchor` links, or RFC 2119 rules | [stage_markdown.py](../../context-indexer/scripts/stage_markdown.py) + [staged_md_macros.sql](../../context-indexer/scripts/staged_md_macros.sql) ([context-indexer](../../context-indexer/SKILL.md)) | Queries pipe table cells as JSON (`staged_md_tables`), extracts `MUST`/`SHOULD` rules (`staged_md_requirements`), and emits size-bounded `md_chunk_NN.md` chunks. |
| Raw branch or PR diff `<= 500` lines (hunk and line filtering) | `duckdb` with [diff_macros.sql](../../duckdb/scripts/diff_macros.sql) (`read_diff_lines`, `read_diff_hunks`) | Streams `git diff` over `/dev/stdin` into SQL without writing temp files. |
| Branch or PR diff `> 150` lines (chunking) or `> 500` lines (AST churn & signature drift) | [stage_diffs.py](../../context-indexer/scripts/stage_diffs.py) + [staged_diff_macros.sql](../../context-indexer/scripts/staged_diff_macros.sql) ([context-indexer](../../context-indexer/SKILL.md)) | Maps modified lines to enclosing AST symbols (`diff_entity_churn`, `diff_signature_drift`, `diff_untested_functions`). |
| Worktree symbol graphs, call graphs, and transitive blast radius | `sem impact <file>` ([sem-semantic-info](../../sem-semantic-info/SKILL.md)) and language MCP tools (`find_definition`, `analyze_files`) | Resolves callers, callees, and analyzer diagnostics without running mutating builds. |
| Local JSON, JSONL, CSV, Parquet, or SQLite test fixtures and schemas | [duckdb](../../duckdb/SKILL.md) (`duckdb -batch -dark-mode` with `DESCRIBE`, `SUMMARIZE`, `read_json`) | Inspects schema shapes and sample rows without loading multi-megabyte data files into the prompt. |

### Copy-Paste Discovery Recipes

#### A. Outline a Large Specification or Audit (`>= 350` lines)

```bash
duckdb -batch -dark-mode -box \
  -init <skills-dir>/duckdb/scripts/markdown_macros.sql -c "
SELECT section_id, level, heading, line_start, subtree_line_end, own_lines
FROM read_md_sections('docs/architecture_spec.md')
WHERE level <= 3;
"
```

#### B. Query Normative Requirements and Tables in a Complex Spec (`> 500` lines)

```bash
md_dir=$(mktemp -d -t plan-md-XXXXXX)
python3 <skills-dir>/context-indexer/scripts/stage_markdown.py \
  --root . --split-chunks --output-dir "$md_dir" "docs/spec.md"

duckdb -batch -dark-mode -box \
  -init <skills-dir>/context-indexer/scripts/staged_md_macros.sql -c "
SELECT section_heading, line_num, keyword, statement
FROM staged_md_requirements('$md_dir');

SELECT section_heading, line_num, cells
FROM staged_md_tables('$md_dir')
LIMIT 20;
"
rm -rf "$md_dir"
```

#### C. Inspect an In-Flight Branch Diff (`> 500` lines)

```bash
diff_dir=$(mktemp -d -t plan-diff-XXXXXX)
git diff main...HEAD | python3 <skills-dir>/context-indexer/scripts/stage_diffs.py \
  --grouped --repo-dir . --base-ref main --output-dir "$diff_dir"

duckdb -batch -dark-mode -box \
  -init <skills-dir>/context-indexer/scripts/staged_diff_macros.sql -c "
SELECT file_path, kind, name, lines_added, pct_rewritten
FROM diff_entity_churn('$diff_dir')
LIMIT 15;

SELECT file_path, kind, name, old_signature, new_signature
FROM diff_signature_drift('$diff_dir');
"
rm -rf "$diff_dir"
```

______________________________________________________________________

## 2. Project Ecosystem & Tooling Detection

Inspect root manifest files to identify the language runtime, dependency manager, test runner, and linters:

| File Pattern | Ecosystem | Test Runner | Linter / Typechecker |
| :--- | :--- | :--- | :--- |
| `pubspec.yaml` | Dart / Flutter | Dart MCP `run_tests` / `flutter test` / `dart test` | Dart MCP `analyze_files` / `dart analyze` |
| `pyproject.toml`, `setup.cfg`, `requirements.txt` | Python | `pytest`, `python3 -m unittest` | `ruff`, `flake8`, `mypy`, `pylint` |
| `package.json`, `tsconfig.json` | TypeScript / Node | `npm test`, `pnpm test`, `yarn test`, `bun test` | `eslint`, `tsc --noEmit`, `biome` |
| `Cargo.toml` | Rust | `cargo test` | `cargo clippy`, `cargo check` |
| `go.mod` | Go | `go test ./...` | `golangci-lint`, `go vet` |
| `pom.xml`, `build.gradle`, `build.gradle.kts` | Java / Kotlin | `mvn test`, `./gradlew test` | `checkstyle`, `ktlint`, `detekt` |
| `CMakeLists.txt`, `Makefile` | C / C++ | `ctest`, `make test` | `clang-tidy`, `cppcheck` |

### Checking Manifests

Read the manifest with `read_file`:

- In `package.json`, inspect the `"scripts"` block for exact test and build commands (`"test"`, `"lint"`, `"typecheck"`).
- In `pyproject.toml`, inspect `[tool.pytest]`, `[tool.ruff]`, and `[tool.mypy]`.
- In `pubspec.yaml`, inspect `dependencies`, `dev_dependencies`, and workspace resolution settings.

______________________________________________________________________

## 3. Locating Existing Code & Exemplars

Before planning any new function, class, or module, verify whether an existing abstraction or helper can be reused:

1. Locate files matching the domain pattern (`*service*`, `*handler*`, `*validator*`) with `fd` or `find_by_name`.
2. Search for literal strings or regex patterns with `rg -n` or `grep_search` to find shared base classes, error types, and validators.
3. Read only the relevant line ranges (`StartLine`..`EndLine`) of candidate files with `read_file` to match naming conventions, error handling, and directory placement.

______________________________________________________________________

## 4. Blast Radius, Dependency Mapping & Parallel Partitioning

When modifying or deleting existing files and symbols, map their dependencies so downstream call sites are covered and parallel tracks do not collide:

1. Search for symbol imports and call sites across the repository with `rg -l "\bSymbolName\b"`.
2. Run `sem impact <path/to/target_file>` when `sem` is available to inspect affected entities and transitive callers.
3. Identify shared chokepoint files that multiple feature slices touch, such as barrel exports (`lib/pkg.dart`, `src/index.ts`, `__init__.py`), central registries, or `CHANGELOG.md`. Isolate those chokepoint files into sequential foundation or integration waves so parallel implementation tracks own strictly disjoint file sets.
4. Locate existing unit and integration test files (`tests/`, `test/`, `*.test.ts`, `*_test.dart`, `test_*.py`) so each parallel track gets a fast, track-local verification command.

______________________________________________________________________

## 5. Strict Read-Only Rules

During planning, the target repository is strictly read-only:

- Never call file modification tools (`edit_file`, `write_file`, `replace_file_content`, `write_to_file`) on codebase files. Never run shell commands that alter workspace files (`sed -i`, `rm`, `touch`).
- Never run mutating Git commands (`git checkout`, `git switch`, `git stash`, `git reset`, `git commit`, `git rm`, `git pull`, or `git worktree add`). Safe read-only commands include `git status`, `git log -n 10`, `git diff`, `git branch`, and `git worktree list`.
- Never run package installation or mutating build commands (`npm install`, `pub get`, `pip install`, `cargo build`) unless explicitly instructed by the user.
- If you stage Markdown or diffs into a temporary directory created via `mktemp -d` for `context-indexer` queries, always remove that temporary directory (`rm -rf "$md_dir" "$diff_dir"`) before finishing discovery.
