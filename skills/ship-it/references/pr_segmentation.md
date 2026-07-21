# PR Segmentation & Stacking Guide

Use this guide during Phase 1 of the `ship-it` workflow to determine whether a change should be submitted as a single Pull Request or segmented into stacked PRs.

## 1. Pre-flight Working Tree Check

Before evaluating diff scope, check working tree cleanliness:

1. Run `git status --porcelain` to check for uncommitted edits or untracked files.
2. **If uncommitted or untracked changes are found**:
   - Use `ask_question` to explicitly ask the user how they want to handle them:
     - Option 1: Stage and commit active work into the current branch before diff evaluation.
     - Option 2: Include uncommitted working tree changes in the diff scope calculation.
     - Option 3: Stash uncommitted changes (`git stash`) before proceeding.

---

## 2. When to Segment a Change

Segment a branch into multiple stacked PRs if any of the following conditions are met:

1. **Diff Size Threshold**: Total changes exceed **500 lines of code** (excluding auto-generated code, lockfiles, or test fixtures).
2. **Logical Separation**:
   - Refactoring existing code vs. adding a new feature.
   - Core API/data model updates vs. consumer/UI updates.
   - Foundation/utility infrastructure vs. feature usage.
3. **Reviewer Specialization**: Different components require domain experts (e.g. backend schema change vs frontend widget change).

---

## 3. Generating a PR Stacking Plan Artifact

Before executing any branch splitting, create a markdown artifact titled `pr_stacking_plan.md` in the conversation artifact directory to present to the user for approval.

### Stacking Plan Artifact Template

```markdown
# PR Stacking Plan Proposal

## Overview
Briefly summarize the total change and why segmentation is recommended.

## Proposed Stack Hierarchy

1. **PR 1 (Base Layer)**: `branch-1-refactor`
   - **Scope**: Internal refactoring of X component without API breaking changes.
   - **Files touched**: [list key files]
   - **Estimated Diff**: ~150 lines.

2. **PR 2 (Middle Layer)**: `branch-2-api-extension` (stacks on `branch-1-refactor`)
   - **Scope**: Adding new API interfaces and data models.
   - **Files touched**: [list key files]
   - **Estimated Diff**: ~200 lines.

3. **PR 3 (Top Layer)**: `branch-3-feature-implementation` (stacks on `branch-2-api-extension`)
   - **Scope**: End-to-end integration and CLI tool updates.
   - **Files touched**: [list key files]
   - **Estimated Diff**: ~250 lines.

## Next Steps / Git Worktree Commands
Provide exact bash commands for creating dedicated worktrees for each stacked branch when approved.
```

---

## 4. Creating Stacked Worktrees

Always use **separate `git worktree` directories** for each stacked branch. This provides isolated working directories so multiple subagents can work simultaneously without state collision or dirty tree conflicts.

```bash
# Worktree 1 (Base Layer)
git worktree add -b feature/part1-base ../worktrees/part1-base main

# Worktree 2 (Middle Layer - stacked on Base Layer)
git worktree add -b feature/part2-api ../worktrees/part2-api feature/part1-base

# Worktree 3 (Top Layer - stacked on Middle Layer)
git worktree add -b feature/part3-impl ../worktrees/part3-impl feature/part2-api
```
