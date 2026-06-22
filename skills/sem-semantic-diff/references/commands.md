# `sem` CLI Command Reference

Follow this comprehensive reference for all `sem` subcommands, options, and flags.

## Table of Contents
- [1. Semantic Diff (sem diff)](#1-semantic-diff-sem-diff)
- [2. Impact Analysis (sem impact)](#2-impact-analysis-sem-impact)
- [3. Dependency Graph (sem graph)](#3-dependency-graph-sem-graph)
- [4. Semantic Blame (sem blame)](#4-semantic-blame-sem-blame)
- [5. Semantic Log (sem log)](#5-semantic-log-sem-log)
- [6. Entity Context (sem context)](#6-entity-context-sem-context)
- [7. List Entities (sem entities)](#7-list-entities-sem-entities)

## 1. Semantic Diff (`sem diff`)
Show added, modified, deleted, or renamed entities in the working tree or between commits.

```bash
# View semantic changes in the working directory
sem diff

# View only staged changes
sem diff --staged

# Show changes from a specific commit
sem diff --commit <COMMIT>

# View diff between two commits
sem diff --from <COMMIT_1> --to <COMMIT_2>

# Get verbose inline content diffs for modified entities
sem diff -v

# Output in JSON format
sem diff --format json
```
*Additional options:* `-C, --cwd <DIR>` (Run as if started in directory), `--file-exts <EXTS>...` (Filter by extensions).

## 2. Impact Analysis (`sem impact`)
Analyze the transitive impact of changing an entity (BFS traversal).

```bash
# See what else is affected if you change an entity
sem impact <entity_name>

# Look up entity by fully qualified ID
sem impact --entity-id "src/utils.ts::function::setup"

# Disambiguate by specifying the file containing the entity
sem impact setup --file src/test_utils.ts

# Output as JSON
sem impact <entity_name> --format json

# Show direct dependencies only
sem impact <entity_name> --deps

# Show direct dependents only
sem impact <entity_name> --dependents

# Show only affected tests
sem impact <entity_name> --tests
```
*Additional options:* `--depth <DEPTH>` (Max traversal depth, default 2, 0 = unlimited), `--file-exts <EXTS>...`, `--no-cache`.

## 3. Dependency Graph (`sem graph`)
View the full entity dependency graph for the codebase.

```bash
# View graph for current directory
sem graph

# View graph for specific path in JSON format
sem graph src/ --format json
```
*Additional options:* `--format <FORMAT>` (terminal, json), `--file-exts <EXTS>...`, `--no-cache`.

## 4. Semantic Blame (`sem blame`)
Identify who last modified each function or class within a file.

```bash
sem blame <file_path>
```

## 5. Semantic Log (`sem log`)
Show the evolution of an entity through git history.

```bash
sem log <entity_name>
```

## 6. Entity Context (`sem context`)
Show token-budgeted context for an entity. This is intended for providing code snippets directly to an LLM's context window.

```bash
# Show context with token budget (default 8000)
sem context <entity_name> --budget 8000

# Output in JSON
sem context <entity_name> --format json
```
*Additional options:* `--entity-id <ID>`, `--file <FILE>`, `--file-exts <EXTS>...`, `--no-cache`.

## 7. List Entities (`sem entities`)
List entities under a file or directory path.

```bash
# List entities in current directory or specific path
sem entities src/

# Output in JSON format
sem entities src/ --format json
```
