# Conflict Resolution Strategies & Concise `/grill-me` Protocol

This reference defines the 4-Tier Conflict Taxonomy, autonomous synthesis patterns for Tiers 1–3, and the escalation protocol for Tier 4 intent clashes using [grill-me](../../grill-me/SKILL.md).

## Table of Contents

1. [The 4-Tier Conflict Taxonomy](#1-the-4-tier-conflict-taxonomy)
2. [Tier 1 Recipes: Mechanical, File Moves & Symbol Renames](#2-tier-1-recipes-mechanical-file-moves--symbol-renames)
3. [Tier 2 Recipes: Composing Orthogonal Intents](#3-tier-2-recipes-composing-orthogonal-intents)
4. [Tier 3 Recipes: Routing 1-to-N Structural Splits](#4-tier-3-recipes-routing-1-to-n-structural-splits)
5. [Tier 4 Protocol: Concise `/grill-me` Alignment](#5-tier-4-protocol-concise-grill-me-alignment)

______________________________________________________________________

## 1. The 4-Tier Conflict Taxonomy

Classify every conflict and semantic hazard into one of four tiers before editing files:

| Tier | Category | Examples | Resolution Authority |
| :--- | :--- | :--- | :--- |
| **Tier 1** | Mechanical, Formatting, Imports, 1:1 File Moves & Symbol Renames | Lockfiles, import sorting, `DU`/`UD` directory moves (`agent_sdks/python/` $\to$ `python/`), unambiguous symbol renames (`A2uiValidator` $\to$ `PayloadValidator`). | **100% Autonomous** |
| **Tier 2** | Orthogonal / Composable Intents | `OURS` adds recursion depth tracking (`MAX_DYNAMIC_VALUE_DEPTH`); `THEIRS` adds `catalogId` forwarding in the same method. | **Autonomous Synthesis** (`f_theirs(f_ours(BASE))`) |
| **Tier 3** | Non-1:1 Structural Splits & Consolidations | `OURS` extracts inline schema properties into `$defs/TestCase`; `THEIRS` adds 10 new enum actions to the old inline block. | **Lineage-Guided Synthesis** (Ask user only if target is ambiguous) |
| **Tier 4** | Mutually Exclusive Intents & Competing Renames | `OURS` renames `X -> Y` while `THEIRS` renames `X -> Z`; or `OURS` deletes a feature that `THEIRS` expands. | **Escalate via `/grill-me`** (`ask_question` batch) |

______________________________________________________________________

## 2. Tier 1 Recipes: Mechanical, File Moves & Symbol Renames

Resolve Tier 1 conflicts deterministically without prompting the user.

### 2.1. Modify/Delete (`DU` / `UD`) Conflicts from Directory Moves

When one branch moves a directory or file and edits it heavily, Git's similarity check may miss the rename and leave a `DU` (deleted by us, modified by them) or `UD` conflict at `<old_path>`:

1. Locate the new file path on the target branch using `fd` or `merge_assistant.py analyze`.
2. Run `merge-moved-file` to perform a 3-way `git merge-file` directly between `OURS:<new_path>`, `BASE:<old_path>`, and `THEIRS:<old_path>`, and remove `<old_path>`:
   ```bash
   python3 scripts/merge_assistant.py merge-moved-file \
     --old-path agent_sdks/python/a2ui_core/CHANGELOG.md \
     --new-path python/a2ui_core/CHANGELOG.md \
     --base <base-sha> \
     --theirs <theirs-ref> \
     --stage-rm
   ```

### 2.2. Unambiguous Symbol Renames

When `OURS` renames a class, method, or function (`OldName` $\to$ `NewName`) and `THEIRS` keeps `OldName` or adds new call sites to `OldName`:

1. Confirm that `THEIRS` did not independently rename `OldName` to a third name.
2. Find all remaining references to `OldName` introduced by `THEIRS`:
   ```bash
   rg -l "\bOldName\b"
   ```
3. Update those call sites and imports to `NewName`.

### 2.3. Imports, Lockfiles, and Generated Files

- **Import blocks**: Take the set union of imports from `OURS` and `THEIRS`, remove any symbols deleted by either branch's intent, and run the language formatter (`dart format`, `pyink`, `prettier`).
- **Generated code / schemas**: Never hand-merge generated files. Resolve the source specification or template first, then re-run the generator script.

______________________________________________________________________

## 3. Tier 2 Recipes: Composing Orthogonal Intents

When both branches edit the same lines of a function for compatible reasons:

1. Extract clean `base`, `ours`, and `theirs` snapshots to a scratch directory:
   ```bash
   python3 scripts/merge_assistant.py extract-blobs \
     --file typescript/web_core/src/rendering/data-context.ts \
     --output-dir /tmp/merge_blobs
   ```
2. Diff `base` against `ours` to isolate transformation $f_{\text{ours}}$, and diff `base` against `theirs` to isolate transformation $f_{\text{theirs}}$.
3. Apply $f_{\text{theirs}}$ onto the `ours` version so both behaviors are preserved (for example, keeping `OURS`'s multi-catalog lookup while adopting `THEIRS`'s depth-limit parameter and check).

______________________________________________________________________

## 4. Tier 3 Recipes: Routing 1-to-N Structural Splits

When `OURS` splits a single file or class (`validator.yaml` or `Service`) into multiple files or definitions (`validator_v0_9.yaml` + `validator_v1_0.yaml`), while `THEIRS` modifies the original file:

1. Inspect `THEIRS`'s delta against `BASE`:
   ```bash
   git diff <base>..<theirs> -- conformance/core/validator.yaml | cat
   ```
2. For each added or modified block in `THEIRS`, determine which split target on `OURS` owns that protocol version or responsibility.
3. Apply the relevant delta to one or both split targets on `OURS`, then `git rm` the obsolete monolithic path.

______________________________________________________________________

## 5. Tier 4 Protocol: Concise `/grill-me` Alignment

When `merge_assistant.py analyze` or manual inspection identifies a **Tier 4 Intent Clash**, use the [grill-me](../../grill-me/SKILL.md) workflow with strict conciseness rules.

### 5.1. Anti-Chattiness & Clarity Rules

1. **Always Name Specific Branches & Repositories (Never Say "OURS" or "THEIRS")**:
   Users find abstract labels like `OURS` and `THEIRS` confusing when comparing diffs. In all questions, summaries, and option choices, **always state the concrete branch, ref, or repository name** (e.g. `v1_0` vs. `upstream/main`).
2. **Never paste raw `<<<<<<<` / `=======` / `>>>>>>>` conflict blocks in chat.**
3. **Cluster by Root Cause**: If one architectural decision affects 12 files, ask **one** question for the cluster, not 12 per-file questions.
4. **Cap Batch Size at 2–4 Questions**: Present all Tier 4 questions in a single `ask_question` tool call.
5. **Use the 3-Bullet Intent Discrepancy Brief**: Keep each question title/stem to three short lines:
   - **Base**: 1 sentence stating original behavior at the common ancestor.
   - **Discrepancy**: 1 sentence contrasting `<target-branch>` (e.g. `v1_0`) intent vs. `<incoming-branch>` (e.g. `main`) intent.
   - **Impact**: Number of affected files or tests.

### 5.2. Required Option Structure (`ask_question`)

Format options as direct user rulings naming the specific branches, placing your recommended synthesis first and always including the `/grill-me` Parking Lot deferral option:

- `(Recommended) <Synthesize both / Adopt specific winning intent with 1-clause rationale>`
- `<Keep <target-branch> (e.g. v1_0) behavior and adapt <incoming-branch> (e.g. main) callers>`
- `<Adopt <incoming-branch> (e.g. main) behavior and migrate <target-branch> (e.g. v1_0) callers>`
- `Let's come back to this`

If the user selects `Let's come back to this`, record a `[DEFERRED: <Topic>]` placeholder in your working plan, resolve all non-dependent files first, and perform the dependency-ordered Repercussion Analysis from [grill-me](../../grill-me/SKILL.md) for the remaining deferred items.
