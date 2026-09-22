---
name: code-merge
description: >-
  Merges Git branches, forks, and repositories by distilling 3-way engineering
  intents (base, ours, theirs), tracking file and symbol renames across
  directory restructures, detecting silent multi-language AST and schema
  contract conflicts, and resolving compatible changes autonomously while
  escalating high-level intent clashes via concise grill-me questions. Use when
  merging or rebasing branches, syncing long-lived release branches after
  squash merges, porting changes across repositories or subtrees, or resolving
  merge conflicts. Don't use for deleting stale merged branches (use
  branch-cleanup), routine single-branch commits without conflicts (use
  commit-changes), or Google3 Piper/CitC changelist operations (use vcs).
---

# Intent-Driven Code Merging (`code-merge`)

This skill elevates Git merging from line-based text comparison to **3-way intent synthesis**. Instead of comparing raw conflict markers between `OURS` and `THEIRS`, determine what invariant the `BASE` code maintained ($I_{\text{base}}$), why `OURS` changed ($\Delta I_{\text{ours}}$), and why `THEIRS` changed ($\Delta I_{\text{theirs}}$).

## Core Workflow

### Step 1: Pre-Flight Analysis & Squash-Base Recovery

Before running `git merge` or dirtying the working tree, run the bundled `merge_assistant.py analyze` script. It executes an in-memory 3-way merge (`git merge-tree --write-tree`), detects prior squash-merged sync commits, builds a file/symbol rename map, and scans for silent contract drift:

```bash
python3 /usr/local/google/home/gspencer/code/cheats/agents/skills/code-merge/scripts/merge_assistant.py analyze \
  --repo . \
  --ours HEAD \
  --theirs upstream/main
```

#### Why Check for Squash-Merged Syncs First?

When a previous sync PR from `main` into a release branch (`v1_0`) was squash-merged on GitHub, Git loses the second parent and falls back to an old merge base, re-triggering hundreds of stale conflicts. If `analyze` reports a `squash_merge_detected` SHA (or if `gh pr view` confirms the last merged commit on `THEIRS`), record the ancestry pointer before merging:

```bash
git merge -s ours <prior-theirs-sha> -m "chore: record ancestry for main@<prior-theirs-sha> (merged via squashed PR #<num>)"
git -c merge.directoryRenames=true -c merge.renameLimit=99999 merge --no-commit --no-ff -X find-renames=25% upstream/main
```

For cross-repository merges, subtree merges, and patch-series porting, see [Cross-Repo, Subtree, and Squash-Merged Branch Syncing](references/cross_repo_and_subtree_merging.md).

______________________________________________________________________

### Step 2: Distill 3-Way Intents & Check Silent AST Hazards

A merge that reports zero textual conflicts can still fail to compile or fail tests if one branch changed a constructor/method signature or renamed a class while the other branch added new callers.

1. **Inspect `Delta I_ours` and `Delta I_theirs`**:
   Use `git log --follow -L`, `sem diff`, `rg`, and `fd` to trace moved files and symbol signatures across all languages in the repository (TypeScript, Dart, Python, Swift, Kotlin, Rust, Go, JSON/YAML schemas).
2. **Audit Silent Contract Drift**:
   Check the `semantic_hazards` list from `merge_assistant.py analyze` for removed optional parameters (e.g., `validator=None`), renamed symbols, or new JSON Schema enum values requiring updates to language dispatch tables (`UNIMPLEMENTED_ACTIONS`).

For the multi-language AST inspection toolbox and silent-hazard checklists, read [Intent Discovery & Multi-Language AST Distillation](references/intent_discovery_and_distillation.md).

______________________________________________________________________

### Step 3: Autonomous Resolution (Tiers 1–3)

Resolve Tiers 1, 2, and 3 directly without prompting the user:

- **Tier 1 (Mechanical, Formatting, Imports, 1:1 File Moves & Symbol Renames)**:
  - **Modify/Delete (`DU`/`UD`) Directory Moves**: When a file moved across directories (`agent_sdks/python/` $\to$ `python/` or `renderers/web_core/src/v0_9/` $\to$ `typescript/web_core/src/`) and had enough churn to miss Git's similarity threshold, run `merge-moved-file` to 3-way merge `BASE:<old_path>` and `THEIRS:<old_path>` directly into `OURS:<new_path>` via `git merge-file`:
    ```bash
    python3 /usr/local/google/home/gspencer/code/cheats/agents/skills/code-merge/scripts/merge_assistant.py merge-moved-file \
      --old-path renderers/web_core/src/v0_9/rendering/generic-binder.ts \
      --new-path typescript/web_core/src/rendering/generic-binder.ts \
      --base <base-sha> \
      --theirs upstream/main \
      --stage-rm
    ```
  - **Unambiguous Symbol Renames**: When one branch renames a class or function (`A2uiValidator` $\to$ `PayloadValidator`) and the other branch adds call sites using the old name, use `rg -l "\bOldName\b"` and update the new call sites to the renamed symbol.
  - **Imports & Lockfiles**: Union non-conflicting imports, regenerate lockfiles or schema artifacts from source, and run the language formatter.
- **Tier 2 (Orthogonal / Composable Edits)**:
  - Extract clean `base`, `ours`, and `theirs` snapshots with `merge_assistant.py extract-blobs`, and apply both transformations sequentially (`f_theirs(f_ours(BASE))`).
- **Tier 3 (Non-1:1 Structural Splits)**:
  - When `OURS` extracted inline schema blocks into `$defs` or split one file into two (`validator_v0_9.yaml` + `validator_v1_0.yaml`), route `THEIRS`'s new fields into the corresponding target definitions.

______________________________________________________________________

### Step 4: Concise `/grill-me` Escalation (Tier 4 Intent Clashes Only)

Escalate to the user **only** when branches have mutually exclusive architectural intents or competing renames (`X -> Y` vs. `X -> Z`). Follow the [grill-me](../grill-me/SKILL.md) workflow with strict conciseness and clarity rules:

1. **Always Name Concrete Repos & Branches (Never Say "OURS" or "THEIRS")**:
   Users find abstract `OURS` / `THEIRS` labels disorienting. In all chat messages, `/grill-me` questions, and options, **always state the specific branch or repository name** (e.g., `v1_0` vs. `upstream/main`, or `main` vs. `feature/my-branch`). Confine `OURS`/`THEIRS` strictly to internal tool logic and diff flags.
2. **No Raw Conflict Markers**: Never paste raw `<<<<<<<` blocks into chat.
3. **Group by Root Cause**: Present at most 2–4 questions in a single `ask_question` call, grouping files by shared architectural cause (`tier4_clusters`).
4. **Use the 3-Bullet Micro-Brief**:
   - **Base**: 1 sentence on original behavior at the common ancestor.
   - **Discrepancy**: 1 sentence contrasting `<target-branch>` intent vs. `<incoming-branch>` intent.
   - **Blast Radius**: Number of affected files/callers.
5. **Provide Actionable Options Named with Concrete Branches + Deferral**:
   - `(Recommended) <Synthesized resolution naming specific branches with brief rationale>`
   - `<Keep <target-branch> (e.g. v1_0) design and adapt <incoming-branch> (e.g. main) callers>`
   - `<Adopt <incoming-branch> (e.g. main) design and migrate <target-branch> (e.g. v1_0) callers>`
   - `Let's come back to this` (Triggers the `/grill-me` Parking Lot deferral flow).

For detailed Tier 1–4 resolution recipes and `/grill-me` question templates, see [Conflict Resolution Strategies & Concise `/grill-me` Protocol](references/conflict_resolution_strategies.md).

______________________________________________________________________

### Step 5: Full-Tree Verification & Ancestry Bookkeeping

1. **Verify Clean Merge State**:
   Ensure zero conflict markers, zero unmerged index entries, and zero temporary remotes remain:
   ```bash
   python3 /usr/local/google/home/gspencer/code/cheats/agents/skills/code-merge/scripts/merge_assistant.py verify --repo .
   ```
2. **Run Static Analyzers & Tests Across the Entire Merged Tree**:
   Always run language analyzers (`dart analyze`, `tsc --noEmit`, `mypy .`, `pyink --check`) and unit/conformance test suites across the **entire repository**, not just the files that had textual conflicts.
3. **Document Merge Ancestry for the Next Cycle**:
   When committing ([commit-changes](../commit-changes/SKILL.md)) and drafting the PR description ([write-pr-description](../write-pr-description/SKILL.md)), explicitly record the merged `<theirs-tip-sha>` and note that if the PR is squash-merged, the next merge should run `git merge -s ours <theirs-tip-sha>` first.
