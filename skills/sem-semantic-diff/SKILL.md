---
name: sem-semantic-diff
description: Use the `sem` CLI to view semantic codebase diffs, evaluate dependency graphs, perform impact analysis, and investigate code history without formatting noise. Explicitly supports subcommands: `sem diff` (semantic diffs), `sem impact` (impact analysis), `sem graph` (dependency graph), `sem blame` (semantic blame), `sem log` (entity history), `sem context` (entity context), and `sem entities` (list entities). Use instead of standard git diff/log when analyzing structural code changes.
---

# `sem` Semantic Diff Skill

This skill provides instructions on how to use `sem`, a semantic version control tool that tracks functions, classes, and types rather than just lines of text.

## Capabilities & Limitations (What `sem` Does Well and Does Not Do)

### What `sem` Does Well
- **Local Codebase Navigation:** Builds a precise semantic dependency graph of all classes, functions, methods, and properties defined within the local repository.
- **Structural Diffs & History:** Shows added, modified, renamed, or deleted entities across commits without formatting or whitespace noise.
- **Internal Impact Analysis:** Tracing the transitive impact (`sem impact`) or callers/callees (`sem graph`) of local entities across the workspace.

### What `sem` Does Not Do (Important Limitations)
- **External Dependencies:** `sem` only indexes entities defined within the local repository's source files. It **does not** parse or track external packages or transitive library dependencies (e.g., from `pubspec.yaml`, `node_modules`, `Cargo.toml`, etc.).
- **External Impact Analysis:** Running `sem impact` on an external type or class (e.g., `DartType` or `ClassElement` from an external package) will fail with `error: Entity '...' not found`.
- **Workflow for External Packages:** If tasked with evaluating how an external package is used across a codebase, **do not start with `sem`**. Use standard `grep` or `ripgrep` to find `import` statements and locate local wrapper classes or helper functions. Once local wrapper entities are identified, use `sem impact` on those local entities to trace their usage across the codebase.

## Finding Entities (`<entity_name>`)

Many `sem` commands require an `<entity_name>`. You can discover the exact names or IDs of entities in the codebase using the following methods:

1. **List entities in a file or directory:**
   Use `sem entities [PATH]` to see all parsed functions, classes, and types in a specific file or directory.
   ```bash
   sem entities src/utils.ts
   
   # For agentic/programmatic parsing:
   sem entities src/utils.ts --format json
   ```

2. **From semantic diffs:**
   When you run `sem diff`, the output will list the names of the entities that have been added, modified, or deleted.

3. **Entity IDs:**
   If a name is ambiguous (e.g., multiple files have a `setup()` function), you can use the fully qualified `entity_id` provided in the JSON output of `sem diff` or `sem entities` (e.g., `--entity-id "src/utils.ts::function::setup"`).

## Core Commands

The `sem` tool provides several subcommands for semantic codebase analysis. For a comprehensive list of all flags, options, and advanced command usages, see the [Commands Reference](references/commands.md).

- **[sem diff](references/commands.md#1-semantic-diff-sem-diff)**: View semantic changes in the working tree or between commits.
- **[sem impact](references/commands.md#2-impact-analysis-sem-impact)**: Trace the transitive impact of changing an entity.
- **[sem graph](references/commands.md#3-dependency-graph-sem-graph)**: Generate a semantic dependency graph of the codebase.
- **[sem blame](references/commands.md#4-semantic-blame-sem-blame)**: Identify who last modified each function or class.
- **[sem log](references/commands.md#5-semantic-log-sem-log)**: View the evolution of an entity through git history.
- **[sem context](references/commands.md#6-entity-context-sem-context)**: Retrieve token-budgeted context for an entity.
- **[sem entities](references/commands.md#7-list-entities-sem-entities)**: List all entities parsed within a file or directory.

## Best Practices
- **JSON Output for Processing**: Always use `--format json` when you need to parse the output programmatically. This ensures 100% consistency across all `sem` commands (as `sem diff` requires `--format json` and does not support a `--json` shorthand).
- **Pipe to cat to Avoid Pagers**: `sem` commands that output large amounts of text (such as `sem diff`, `sem log`, or `sem graph`) may automatically launch an interactive pager like `less`. **Always pipe interactive `sem` commands to `| cat`** (e.g., `sem diff | cat` or `sem log <entity> | cat`) to prevent the terminal from pausing.
- **File Extensions**: Use `--file-exts .ts .js` to filter large codebases.
- **Handling Ambiguity**: If multiple entities have the same name (e.g., a `setup` function in multiple test files), use `--file <FILE>` or `--entity-id <ENTITY_ID>` to disambiguate:
  ```bash
  sem impact setup --file src/test_utils.ts
  # OR
  sem impact --entity-id "src/test_utils.ts::function::setup"
  ```