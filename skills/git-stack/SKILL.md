---
name: git-stack
description: >-
  Inspects, validates ancestry, cascade-rebases, and synchronizes GitHub pull request stacks across git worktrees and forks without manual merge-base calculations. Use when working with stacked PRs, updating branch chains, rebasing multi-PR features, or diagnosing stack ancestry drift.
---

# Git Stack Skill (`git-stack`)

This skill provides automated inspection, validation, cascade rebasing, and remote synchronization for stacked pull requests on GitHub, specifically handling multi-worktree checkouts and branch hierarchies.

[TOC]

---

## Capabilities & Problem Context

In modern Git workflows with stacked PRs (such as GitHub stack chains or dependent branch series):
1. **Ancestry Stagnation**: Whenever a lower branch in the stack is modified, all branches above it drift (`⚠ needs rebase`) because their git merge-base no longer contains the updated parent.
2. **Worktree Isolation**: In multi-worktree setups, running native `gh stack rebase` fails because the branches are checked out in separate directories.
3. **Manual Rebase Overhead**: Re-calculating `OLD_BASE=$(git merge-base <parent_pre_rebase_sha> <child_sha>)` and running `git rebase --onto <parent> $OLD_BASE` for every branch in the chain is error-prone.
4. **Fork Limitations**: The GitHub CLI `gh stack` extension rejects PRs opened from forks; `git-stack` supports both native upstream stacks and fallback `baseRefName` chains.

`git-stack` automates discovery, ancestry checks, worktree navigation, cascade rebasing, and GitHub PR retargeting.

---

## Canonical CLI Invocation

The skill CLI is located at `scripts/git_stack.py` within this skill directory:

```bash
GIT_STACK="python3 scripts/git_stack.py"
```

### 1. View Stack Hierarchy (`view`)

Inspects the stack containing the current branch or specified PR/branch:

```bash
# View active stack from current directory
$GIT_STACK view

# View specific PR's stack in a repository
$GIT_STACK view --repo-dir /path/to/repo --target 123

# Output as machine-readable JSON
$GIT_STACK view --json
```

Output highlights:
- `✓ UP TO DATE`: Head commit contains base branch tip.
- `⚠ NEEDS REBASE`: Base branch was updated; child branch must be rebased onto parent.
- `⊘ DIRTY WORKTREE`: The worktree containing this branch has uncommitted changes.

### 2. Verify Ancestry & Clean Status (`check`)

Validates whether all branches in the stack are up to date and clean:

```bash
$GIT_STACK check --repo-dir /path/to/repo
```

- Returns exit code `0` if all links are up to date and clean.
- Returns exit code `1` if any link requires a rebase or worktree is dirty.
- Returns exit code `2` if stack discovery failed.

### 3. Cascade Rebase Bottom-Up (`rebase`)

Performs a bottom-up cascade rebase across the stack:

```bash
# Dry-run to preview commands without modifying git state
$GIT_STACK rebase --dry-run

# Execute cascade rebase
$GIT_STACK rebase
```

**Worktree Handling**:
- If a target branch is checked out in an existing worktree, the rebase runs inside that worktree.
- If a branch is not checked out in any worktree, `git-stack` creates a temporary detached worktree at `.git/git-stack-temp-worktree`, rebases the branch there, and cleans up the temporary worktree upon completion.
- If a conflict occurs, execution halts immediately with instructions to resolve the conflict and continue (`git rebase --continue`).

### 4. Synchronize Remote Branches & PR Bases (`sync`)

Pushes rebased stack branches to the remote and aligns PR base branches on GitHub:

```bash
# Preview push and edit commands
$GIT_STACK sync --dry-run

# Push rebased branches and align base branches
$GIT_STACK sync --remote upstream
```

- Pushes with `--force-with-lease` to prevent overwriting unexpected remote changes.
- Retargets GitHub PR `baseRefName` if a parent branch was renamed or changed.

---

## Agent Guidelines & Workflows

When working with stacked pull requests:

1. **Before Modifying Code**:
   Run `$GIT_STACK check` to ensure your stack ancestry is healthy before adding new commits.
2. **After Landing or Updating a Parent Branch**:
   - First run `$GIT_STACK rebase --dry-run` to inspect the planned rebase commands.
   - Run `$GIT_STACK rebase` to cascade the changes up the stack.
   - If a conflict occurs, open the conflicting files in the designated worktree, resolve conflicts, and run `git rebase --continue`.
3. **Before Requesting Review**:
   Run `$GIT_STACK sync` to push rebased branches and verify that the GitHub UI displays isolated per-PR diffs rather than cumulative stack diffs.
