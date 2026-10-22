---
name: merge-code
description: >-
  Merges Git branches, forks, and repositories by reconstructing the intent of
  the base, ours, and theirs sides instead of comparing conflict markers.
  Defaults to merging upstream/main into the current branch and resolving
  conflicts when invoked with no arguments. Tracks file and symbol renames
  across restructures, detects silent AST and schema contract conflicts,
  resolves compatible changes on its own, and escalates real intent clashes as
  short questions. Use when merging or rebasing branches, syncing a long-lived
  release branch after squash merges, porting changes across repositories or
  subtrees, or resolving merge conflicts. Don't use for deleting merged
  branches (use branch-cleanup), single-branch commits (use commit-changes), or
  monorepo changelist tooling outside Git.
---

# Intent-Driven Code Merging (`merge-code`)

This skill elevates Git merging from line-based text comparison to **3-way intent synthesis**. Instead of comparing raw conflict markers between `OURS` and `THEIRS`, determine what invariant the `BASE` code maintained ($I_{\text{base}}$), why `OURS` changed ($\Delta I_{\text{ours}}$), and why `THEIRS` changed ($\Delta I_{\text{theirs}}$).

## Default Behavior (No Arguments)

When invoked as `/merge-code` with no arguments or extra guidance, merge `upstream/main` into the active branch and resolve all conflicts. Do not stop to ask which branch to merge when none is given:

1. Resolve the repository root (`git rev-parse --show-toplevel`) for the current working directory or active worktree, using `HEAD` as `--ours`.
2. Fetch the latest upstream commits with `git fetch upstream main` (falling back to `origin` only when no `upstream` remote exists).
3. Default the incoming ref (`--theirs`) to `upstream/main`.
4. Run pre-flight analysis, record squash-merge ancestry if needed, start the working-tree merge, and resolve all conflicts through Steps 1 to 5.

## Core Workflow

### Step 1: Pre-Flight Analysis, Squash-Base Recovery & Merge Execution

Before running `git merge` or dirtying the working tree, fetch the target remote (`git fetch upstream main` by default). Next, run the bundled `merge_assistant.py analyze` script (`--ours` defaults to `HEAD` and `--theirs` defaults to `upstream/main`). It runs an in-memory 3-way merge (`git merge-tree --write-tree`), detects prior squash-merged sync commits, builds a rename map, and scans for silent contract drift:

```bash
python3 /usr/local/google/home/gspencer/code/cheats/agents/skills/merge-code/scripts/merge_assistant.py analyze \
  --repo . \
  --ours HEAD \
  --theirs upstream/main
```

#### Why Check for Squash-Merged Syncs First?

When a previous sync PR from `main` into a release branch (`v1_0`) was squash-merged on GitHub, Git loses the second parent. It then falls back to an old merge base and re-triggers hundreds of stale conflicts. If `analyze` reports a `squash_merge_detected` SHA (or `gh pr view` shows the last merged commit on `THEIRS`), record the ancestry pointer first:

```bash
git merge -s ours <prior-theirs-sha> -m "chore: record ancestry for main@<prior-theirs-sha> (merged via squashed PR #<num>)"
```

#### Start the Working-Tree Merge

Once ancestry is current (and if `.git/MERGE_HEAD` is not already active), start the merge with rename detection enabled. Then proceed directly to resolve conflicts in Steps 2 to 4:

```bash
git -c merge.directoryRenames=true -c merge.renameLimit=99999 merge --no-commit --no-ff -X find-renames=25% upstream/main
```

For cross-repository merges, subtree merges, and patch-series porting, see the [cross-repo, subtree, and squash-merged branch syncing guide](references/cross_repo_and_subtree_merging.md).

______________________________________________________________________

### Step 2: Distill 3-Way Intents & Check Silent AST Hazards

A merge can report zero textual conflicts and still fail to compile or pass tests. This happens when one branch changes a signature or renames a class while the other branch adds new callers.

1. Use `git log --follow -L`, `sem diff`, `rg`, and `fd` to trace moved files and symbol signatures across all repository languages.
2. Check the `semantic_hazards` list from `merge_assistant.py analyze` for removed optional parameters (`validator=None`), renamed symbols, or new schema enum values needing dispatch updates (`UNIMPLEMENTED_ACTIONS`).

For the multi-language AST inspection toolbox and silent-hazard checklists, read the [intent discovery and AST distillation guide](references/intent_discovery_and_distillation.md).

______________________________________________________________________

### Step 3: Autonomous Resolution (Tiers 1–3)

Resolve Tiers 1, 2, and 3 directly without prompting the user.

#### Tier 1: Mechanical Edits, Imports, 1:1 File Moves & Symbol Renames

- When a file moved across directories (`agent_sdks/python/` $\to$ `python/`) with too much churn for Git's similarity check, run `merge-moved-file`. This merges `BASE:<old_path>` and `THEIRS:<old_path>` directly into `OURS:<new_path>` via `git merge-file`:
  ```bash
  python3 /usr/local/google/home/gspencer/code/cheats/agents/skills/merge-code/scripts/merge_assistant.py merge-moved-file \
    --old-path renderers/web_core/src/v0_9/rendering/generic-binder.ts \
    --new-path typescript/web_core/src/rendering/generic-binder.ts \
    --base <base-sha> \
    --theirs upstream/main \
    --stage-rm
  ```
- When one branch renames a class or function (`A2uiValidator` $\to$ `PayloadValidator`), the other branch might still add callers to the old name. Find those call sites with `rg -l "\bOldName\b"` and update them to the new symbol.
- Union non-conflicting imports, regenerate lockfiles or schema artifacts from source, and run the language formatter.

#### Tier 2: Orthogonal / Composable Edits

Extract clean `base`, `ours`, and `theirs` snapshots with `merge_assistant.py extract-blobs`, and apply both transformations sequentially (`f_theirs(f_ours(BASE))`).

#### Tier 3: Non-1:1 Structural Splits

When `OURS` extracts inline schema blocks into `$defs` or splits a file (`validator_v0_9.yaml` + `validator_v1_0.yaml`), route new fields from `THEIRS` into the matching targets.

______________________________________________________________________

### Step 4: Concise `/grill-me` Escalation (Tier 4 Intent Clashes Only)

Escalate to the user **only** when branches have mutually exclusive architectural intents or competing renames (`X -> Y` vs. `X -> Z`). Follow the [grill-me](../grill-me/SKILL.md) workflow with these rules:

1. Always state the concrete branch or repository name (such as `v1_0` vs. `upstream/main`) in chat messages, questions, and options. Never use abstract `OURS` or `THEIRS` labels in user-facing text; keep them inside internal tool flags.
2. Never paste raw `<<<<<<<` conflict blocks into chat.
3. Group conflicting files by shared architectural cause (`tier4_clusters`), and render each cluster with `genui` `consequence_cards` when the `a2ui-pane` plugin is installed. Use `mode: "pane"` with `stack: true` for 2 to 4 clusters (including `[A2UI Pane](sidecar://a2ui-pane/a2ui-pane/)` in chat), or `mode: "inline"` for one clash. Fall back to your host's question tool or a numbered chat prompt when `a2ui-pane` is unavailable.
4. Keep each decision brief to three parts:
   - One sentence on original behavior at the common ancestor with clickable `[file](file:///...)` links in chat.
   - Side-by-side branch consequences in `$/options` (or a Markdown table) contrasting `<target-branch>` intent against `<incoming-branch>` intent, including `impact` and `tradeoff`.
   - Your recommended synthesis pre-populated in `$/choice` (for `consequence_cards`) or listed first with `(Recommended)` in the fallback.
5. Format options with concrete branch names and include deferral:
   - `(Recommended) <Synthesized resolution naming specific branches with brief rationale>`
   - `<Keep <target-branch> (e.g. v1_0) design and adapt <incoming-branch> (e.g. main) callers>`
   - `<Adopt <incoming-branch> (e.g. main) design and migrate <target-branch> (e.g. v1_0) callers>`
   - Support `"Explain further"` (`$/moreInfo` in `consequence_cards`) and `"Let's come back to this"` (`$/defer` in `consequence_cards` or as a fallback option).

For detailed Tier 1–4 resolution recipes and `/grill-me` question templates, see the [guide on conflict-resolution strategies](references/conflict_resolution_strategies.md).

______________________________________________________________________

### Step 5: Full-Tree Check & Ancestry Bookkeeping

1. Check that zero conflict markers, zero unmerged index entries, and zero temporary remotes remain:
   ```bash
   python3 /usr/local/google/home/gspencer/code/cheats/agents/skills/merge-code/scripts/merge_assistant.py verify --repo .
   ```
2. Run language analyzers (`dart analyze`, `tsc --noEmit`, `mypy .`, `pyink --check`) and unit or conformance test suites across the entire repository, not only on files that had textual conflicts.
3. When committing ([commit-changes](../commit-changes/SKILL.md)) and drafting the PR description ([write-pr-description](../write-pr-description/SKILL.md)), record the merged `<theirs-tip-sha>`. Note that if the PR is squash-merged, the next merge should run `git merge -s ours <theirs-tip-sha>` first.
