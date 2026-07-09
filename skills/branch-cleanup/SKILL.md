---
name: branch-cleanup
description: Clean up local and remote tracking git branches that have already been merged to the main branch. Use this when a repository has stale local or remote branches that have been merged (either normally or via squash-and-merge) and need to be deleted safely.
---

# Git Branch Cleanup Skill

This skill provides a helper script to identify and delete merged git branches. It handles both standard merges and squash merges (using `git cherry` analysis), and safely deletes matching tracking branches from their respective remotes (e.g. `origin`).

## Core Features
1. **Squash-Merge Detection**: Uses `git cherry` to check if a branch's changes exist in the main branch, even if the commit history was squashed.
2. **Worktree Cleanup**: Automatically detects and deletes linked git worktrees associated with merged branches before deleting the branches.
3. **Safety First**: Prevents removing the main worktree, and protects uncommitted work by aborting if a linked worktree has uncommitted changes.
4. **Remote Deletion**: Automatically identifies and deletes the corresponding remote tracking branch (e.g. `origin/my-branch`) using branch tracking config, avoiding coincidental name matches.
5. **Dry-Run by Default**: Safe exploration of branches before executing any deletion commands.

## Usage

Run the bundled script in dry-run mode to preview changes:

```bash
python3 scripts/cleanup_branches.py --repo-dir /path/to/repo
```

To execute the deletions:

```bash
python3 scripts/cleanup_branches.py --repo-dir /path/to/repo --delete
```

### Options:
- `--repo-dir <path>`: Path to the target git repository (defaults to the current working directory).
- `--main-branch <name>`: Name of the main/production branch to check against (defaults to `main`).
- `--delete`: Executes the deletion of both local and tracking remote branches.
