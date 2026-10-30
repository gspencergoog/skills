# Splitting Reviews

This reference document provides guidance on how to subdivide a large or complex code review into smaller, manageable chunks to maintain high quality and avoid context overload.

## When to Split a Review

Consider splitting a review when:

- The diff is large (e.g., > 500 lines or > 10 files).
- The changes span multiple distinct components or layers (e.g., frontend, backend, database).
- The PR contains multiple unrelated features or bug fixes (though ideally these should be separate PRs, sometimes they are combined).
- You notice that your review comments are becoming superficial or missing details in later files.

## Strategies for Splitting

### 1. By Artifact Type

Start from the classifier partitions (SKILL.md Step 1b): code files in one group, document files in another.

- A mixed diff under 500 lines stays in one prompt, with the code mandate first and the Document Review Mandate second.
- A large spec, design doc, or blueprint (over 500 changed lines) splits by top-level section. Give each part the document's section outline so the reviewer can check cross-references.
- The downstream impact pass looks at the change as a whole, so the orchestrator runs it once during synthesis, not once per part.

### 2. By File or Component

The most common approach is to review files in logical groups:

- **By Directory**: Review files folder by folder if the project is well-organized by feature or component.
- **By Layer**: Review database changes first, then backend logic, then frontend UI, then tests. This helps build context sequentially.
- **By File Type**: Review core logic files (.ts, .java, .go) separately from configuration files or documentation.

### 3. By Concern (`--panel`)

Splitting the same changes by concern is what `--panel` does: two reviewers get the full diff, each with its own lane. The lane table, the Lane Assignment blocks, and the hypothesis lane labels are in [lanes.md](lanes.md#lane-specs).

When a component split and `--panel` both apply, each partition gets an A/B pair (`rev-a-1` / `rev-b-1` on Tier 1, or subagents on Tier 2). State the total reviewer count in chat before launching.

#### By Theme (focus reviewers)

A focus reviewer splits by mechanism instead: it reads the whole diff through one cross-cutting theme ([lanes.md](lanes.md#focus-blocks)). When a large diff carries three strong themes in different components, consider a component split where each partition carries its own theme, so each focus reviewer reads only the chunks where its mechanism lives.

### 4. Deletion-Heavy Diffs

Treat a diff that mostly deletes, moves, or consolidates files as the `deletion-and-consolidation` focus theme. Its playbook, [deletion-and-consolidation.md](focus_areas/deletion-and-consolidation.md), lists the failure modes and hypothesis templates, such as "deleted symbol `X` is still referenced in the tree" and "deleted test coverage for `Y` does not exist elsewhere in `HEAD`". Assign those hypotheses to lane A (or the single reviewer), checked with `rg` against `HEAD`.

## Tooling Support

To assist with splitting large diffs, use the [`context-indexer`](../../context-indexer/SKILL.md) staging script:
[`../../context-indexer/scripts/stage_diffs.py`](../../context-indexer/scripts/stage_diffs.py)

This script can:

- Read a diff from stdin or a file.
- Extract a diff from a JSON file (useful if the diff is wrapped in JSON).
- Pack per-file diffs (and split oversized files along hunk boundaries) into size-bounded chunk files (`diff_chunk_01.diff`, `diff_chunk_02.diff`, …) with `--grouped` so each chunk fits in a single `view_file` call ($\le 35\text{ KB}$ and $\le 700$ lines by default). It writes `manifest.json`, `diff_lines.jsonl`, `diff_hunks.jsonl`, and `diff_entities.jsonl` (populated when `--repo-dir` or `--with-entities` is supplied).
- Split the diff into separate files per changed file in a specified output directory (when `--grouped` is omitted).

**Grouped Staging Example (Recommended for Swarm Briefs and Subagent Prompts):**

```bash
diff_dir=$(mktemp -d -t pr-review-diffs-XXXXXX)
git diff main...HEAD | python3 <skills-directory>/context-indexer/scripts/stage_diffs.py \
  --grouped --repo-dir . --base-ref main --max-bytes 35000 --max-lines 700 --output-dir "$diff_dir"
```

When `stage_diffs.py --grouped` produces 1 to 3 chunks, pass all chunk paths to one reviewer (or one `--panel` A/B pair) to read via `view_file`. For 4 or more chunks across components, partition them across swarm worker lanes or subagent calls by component or artifact type.

**Relational Diff Triage with DuckDB (`staged_diff_macros.sql`):**

On large diffs (> 500 lines), query the staged JSONL files in `$diff_dir` with [../../context-indexer/scripts/staged_diff_macros.sql](../../context-indexer/scripts/staged_diff_macros.sql) before assigning chunks or seeding hypotheses:

```bash
duckdb -batch -dark-mode -box \
  -init <skills-directory>/context-indexer/scripts/staged_diff_macros.sql -c "
-- 1. Rank modified functions and methods by added lines and rewrite percentage
SELECT file_path, kind, name, chunk_name, lines_added, pct_rewritten
FROM diff_entity_churn('$diff_dir')
LIMIT 15;

-- 2. Detect public or internal signature changes between OLD and NEW
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

**Per-File Split Example:**

```bash
python3 <skills-directory>/context-indexer/scripts/stage_diffs.py --output-dir scratch/diff_chunks < diff.txt
```

For JSON inputs:

```bash
python3 <skills-directory>/context-indexer/scripts/stage_diffs.py --json --json-key diff --output-dir scratch/diff_chunks < input.json
```

**Large Document Staging & Section Splitting (`stage_markdown.py` & `staged_md_macros.sql`):**

For specs, RFCs, or blueprints (`> 500` lines), run [../../context-indexer/scripts/stage_markdown.py](../../context-indexer/scripts/stage_markdown.py) with `--split-chunks`. It writes size-bounded `md_chunk_NN.md` files (`<= 35 KB` and `<= 700` lines) alongside relational JSONL tables (`md_sections.jsonl`, `md_code_blocks.jsonl`, `md_tables.jsonl`, `md_frontmatter.jsonl`, `md_links.jsonl`):

```bash
md_dir=$(mktemp -d -t doc-review-chunks-XXXXXX)
python3 <skills-directory>/context-indexer/scripts/stage_markdown.py \
  --root . --split-chunks --max-bytes 35000 --max-lines 700 \
  --output-dir "$md_dir" path/to/spec.md

duckdb -batch -dark-mode -box \
  -init <skills-directory>/context-indexer/scripts/staged_md_macros.sql -c "
SELECT section_id, level, heading, chunk_name, line_start, subtree_line_end
FROM staged_md_sections('$md_dir')
WHERE level <= 2;
"
```

For quick ad hoc section outlines on a single Markdown file (`>= 350` lines) without staging JSONL files, run `duckdb -batch -dark-mode -box -init <skills-directory>/duckdb/scripts/markdown_macros.sql` and query `read_md_sections('path/to/spec.md')`.

## How to Combine Subdivided Reviews

After performing subdivided reviews, use the **Synthesis** step to create the final output:

1. **Deduplicate**: Ensure that the same issue found in multiple passes or files is not reported multiple times unless it manifests differently.
2. **Prioritize**: Group comments by severity. Ensure critical and high-severity issues are highlighted at the top.
3. **Cohesiveness**: Ensure the tone and style of all comments are consistent, following the [write-prose](../../write-prose/SKILL.md) skill.
