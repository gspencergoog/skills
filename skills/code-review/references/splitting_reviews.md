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

Splitting the same changes by concern is what `--panel` does: two reviewers get the full diff, each with its own lane. Lane A covers correctness, concurrency and failure modes, edge cases, tests, and the Implementer and Tester lenses. Lane B covers security, maintainability, efficiency, API soundness, and the Consumer and Operator lenses. The orchestrator assigns its seeded hypotheses by lane as well: correctness, state, and test hypotheses go to lane A; wiring, security, API, performance, and dependency hypotheses go to lane B. See SKILL.md Step 3, "Panel Review", for the lane blocks.

When a split by component and `--panel` both apply, each partition gets its own A/B pair. State the total subagent count in chat before launching.

### 4. Deletion-Heavy Diffs

When a diff mostly deletes, moves, or consolidates files, seed hypotheses of the form "deleted symbol `X` is still referenced in the tree" and "deleted test coverage for `Y` does not exist elsewhere in `HEAD`", and assign them to lane A (or the single reviewer). The reviewer checks them with `rg` against `HEAD` and records disproved ones in `## Checked and Found Clean`.

## Tooling Support

To assist with splitting large diffs, use the provided Python script:
`scripts/split_diff.py` (inside the directory the SKILL.md is in)

This script can:

- Read a diff from stdin or a file.
- Extract a diff from a JSON file (useful if the diff is wrapped in JSON).
- Pack per-file diffs (and split oversized files along hunk boundaries) into size-bounded chunk files (`diff_chunk_01.diff`, `diff_chunk_02.diff`, …) with `--grouped` so each chunk fits in a single `view_file` call ($\le 35\text{ KB}$ and $\le 700$ lines by default) and write a `manifest.json`.
- Split the diff into separate files per changed file in a specified output directory (when `--grouped` is omitted).

**Grouped Staging Example (Recommended for Subagent Prompts):**

```bash
diff_dir=$(mktemp -d -t pr-review-diffs-XXXXXX)
git diff main...HEAD | python3 <skills-directory>/code-review/scripts/split_diff.py \
  --grouped --max-bytes 35000 --max-lines 700 --output-dir "$diff_dir"
```

When `split_diff.py --grouped` produces 1–3 chunks, you can pass all chunk paths to a single reviewer (or a single `--panel` A/B pair) so the reviewer reads each chunk via `view_file`. When it produces 4 or more chunks across distinct components, partition the chunks across multiple reviewer invocations by component or artifact type.

**Per-File Split Example:**

```bash
python3 <skills-directory>/code-review/scripts/split_diff.py --output-dir scratch/diff_chunks < diff.txt
```

For JSON inputs:

```bash
python3 <skills-directory>/code-review/scripts/split_diff.py --json --json-key diff --output-dir scratch/diff_chunks < input.json
```

## How to Combine Subdivided Reviews

After performing subdivided reviews, use the **Synthesis** step to create the final output:

1. **Deduplicate**: Ensure that the same issue found in multiple passes or files is not reported multiple times unless it manifests differently.
2. **Prioritize**: Group comments by severity. Ensure critical and high-severity issues are highlighted at the top.
3. **Cohesiveness**: Ensure the tone and style of all comments are consistent, following the [write-prose](../../write-prose/SKILL.md) skill.
