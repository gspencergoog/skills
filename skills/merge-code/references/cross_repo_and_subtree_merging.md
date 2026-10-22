# Cross-Repo, Subtree, and Squash-Merged Branch Syncing

This reference covers merging between branches whose previous syncs were squash-merged on GitHub, merging across directory restructures, and syncing code between separate Git repositories.

## Table of Contents

1. [Handling Squash-Merged Branch Syncs (`git merge -s ours`)](#1-handling-squash-merged-branch-syncs-git-merge--s-ours)
2. [Native Directory & File Move Merging (`ort` + `git merge-file`)](#2-native-directory--file-move-merging-ort--git-merge-file)
3. [Cross-Repository Merging via Ephemeral Remotes](#3-cross-repository-merging-via-ephemeral-remotes)
4. [Subtree Merging (`-X subtree`) & Patch-Series Fallback (`port-patches`)](#4-subtree-merging--x-subtree--patch-series-fallback-port-patches)

______________________________________________________________________

## 1. Handling Squash-Merged Branch Syncs (`git merge -s ours`)

### Why Squash-Merged Sync PRs Cause Conflict Floods

When a long-lived feature or release branch (such as `v1_0`) periodically merges `main` via a GitHub Pull Request that gets **squash-merged**, GitHub creates a single-parent commit on `v1_0` and discards the second parent pointing to `main`.

On the *next* merge from `main` into `v1_0`, `git merge-base v1_0 main` falls back to an older ancestor from weeks or months ago. Git then tries to re-apply every commit from `main` that was already squash-merged, producing hundreds of false conflicts.

### Step-by-Step Recovery Procedure

1. **Detect the Prior Squash-Merged Commit**:
   Run `merge_assistant.py analyze` to scan `v1_0` commit messages and GitHub PR metadata (`gh pr view <n> --json title,body,commits`) for the last `main` commit SHA that was incorporated:
   ```bash
   python3 scripts/merge_assistant.py analyze --ours HEAD --theirs upstream/main
   ```
2. **Record the Missing Ancestry Pointer (`-s ours`)**:
   Before merging `upstream/main`, record a no-op `-s ours` merge commit pointing to the previously merged `main` SHA (`<prior-main-sha>`), or pass `--record-ancestry` to `analyze`:
   ```bash
   git merge -s ours <prior-main-sha> -m "chore: record ancestry for main@<prior-main-sha> (merged via squashed PR #<num>)"
   ```
   This updates Git's commit graph without changing a single file in the working tree.
3. **Execute the Real Merge**:
   Now run `git merge --no-commit --no-ff upstream/main`. Git uses `<prior-main-sha>` as the merge base and evaluates **only** the new commits landed on `main` since that SHA.
4. **Bookmark the New Merge SHA in the PR Description**:
   When creating the merge commit and PR description, explicitly state the new `main` tip SHA (`f7ef57a16`) and include the `git merge -s ours f7ef57a16` instruction so the next merge cycle can recover it immediately.

______________________________________________________________________

## 2. Native Directory & File Move Merging (`ort` + `git merge-file`)

You do **not** need `git format-patch` when a repository moves directories around. Git's default `ort` merge strategy (`git merge -s ort`) includes native directory rename detection (`merge.directoryRenames=true`) and file rename detection.

1. **Tune Rename Thresholds for Large Restructures**:
   When merging branches after a major directory move (such as `renderers/web_core/src/v0_9/` $\to$ `typescript/web_core/src/`), lower the rename similarity threshold so Git pairs heavily edited moved files:
   ```bash
   git -c merge.directoryRenames=true -c merge.renameLimit=99999 merge --no-commit -X find-renames=25% upstream/main
   ```
2. **Resolve High-Churn `DU`/`UD` Stragglers with `merge-moved-file`**:
   If a file changed by more than 75% after being moved, Git leaves a Modify/Delete (`DU` or `UD`) conflict at `<old_path>`. Pair it with `<new_path>` using `fd` and merge the 3-way blobs directly with `git merge-file`:
   ```bash
   python3 scripts/merge_assistant.py merge-moved-file \
     --old-path renderers/web_core/src/v0_9/rendering/generic-binder.ts \
     --new-path typescript/web_core/src/rendering/generic-binder.ts \
     --base <base-sha> \
     --theirs upstream/main \
     --stage-rm
   ```

______________________________________________________________________

## 3. Cross-Repository Merging via Ephemeral Remotes

When merging changes from a local sibling checkout or an external repository URL:

1. **Prepare the Temporary Remote**:
   ```bash
   python3 scripts/merge_assistant.py prepare-cross-repo \
     --source-repo /path/to/other/repo \
     --source-ref main
   ```
   This registers `_merge_source`, fetches the target ref without tags, and reports the common or squash-merged base SHA.
2. **Run Pre-Flight Analysis & Merge**:
   ```bash
   python3 scripts/merge_assistant.py analyze --ours HEAD --theirs _merge_source/main
   git merge --no-commit -X find-renames=25% _merge_source/main
   ```
3. **Clean Up the Temporary Remote Before Committing**:
   ```bash
   git remote remove _merge_source
   python3 scripts/merge_assistant.py verify
   ```

______________________________________________________________________

## 4. Subtree Merging (`-X subtree`) & Patch-Series Fallback (`port-patches`)

### 4.1. Subtree Merging (`-X subtree=<prefix>`)

When an entire standalone repository is nested inside a subdirectory of a monorepo:

```bash
git merge -s ort -X subtree=packages/sub_repo -X find-renames=25% _merge_source/main
```

### 4.2. Patch-Series Porting (`port-patches`)

Use `port-patches` only when repositories cannot share commit ancestry directly or when cherry-picking an isolated commit range across remapped subdirectories:

```bash
# 1. Dry-run 3-way check (uses temporary GIT_INDEX_FILE and GIT_ALTERNATE_OBJECT_DIRECTORIES)
python3 scripts/merge_assistant.py port-patches \
  --source-repo /path/to/source_repo \
  --range v1.0..v1.2 \
  --source-subdir pkg/foo \
  --target-dir lib/foo

# 2. Apply with git am --3way once dry run passes
python3 scripts/merge_assistant.py port-patches \
  --source-repo /path/to/source_repo \
  --range v1.0..v1.2 \
  --source-subdir pkg/foo \
  --target-dir lib/foo \
  --apply
```
