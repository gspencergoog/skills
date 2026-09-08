# GitHub CLI Stacks Reference (`gh stack`)

Command reference and troubleshooting guide for stacked pull requests in GitHub CLI (`gh stack`).

## Commands

### 1. Stack Initialization & Management (`init`, `add`, `modify`, `unstack`, `view`)

#### `gh stack init`

Initialize a stack of branches targeting the repository trunk (default branch or specified `--base`).

- Adopts existing branches or creates missing ones.
- When passing multiple branches, order them from bottom (base) to top (head).

```bash
# Create a stack with a new branch targeting default branch
gh stack init feat/step-1

# Adopt existing branches into a stack (ordered bottom to top)
gh stack init feat/step-1 feat/step-2 feat/step-3

# Specify a custom trunk/base branch
gh stack init --base develop feat/step-1
gh stack init --base develop feat/step-1 feat/step-2
```

Flags:

- `-b, --base <string>`: Trunk branch for stack (defaults to repository default branch).

#### `gh stack add`

Add a new branch on top of the current stack.

```bash
# Add a new named branch
gh stack add feat/step-4

# Stage all changes and commit with message
gh stack add -A -m "Add user authentication" feat/step-4

# Stage tracked changes only and commit
gh stack add -u -m "Update API endpoints" feat/step-4

# Auto-generate branch name from commit message (omit branch argument)
gh stack add -m "Fix login bug"

# Open editor for commit message
gh stack add -A feat/step-4
```

Flags:

- `-A, --all`: Stage all changes including untracked files.
- `-u, --update`: Stage changes to tracked files only.
- `-m, --message <string>`: Create a commit with this message (auto-generates branch name if branch argument is omitted).

#### `gh stack modify`

Open an interactive TUI to restructure, reorder, drop, fold, insert, or rename stack branches. All changes stage in the TUI and apply on `Ctrl+S`.

```bash
# Open interactive TUI
gh stack modify

# Continue after resolving rebase conflicts encountered during modify
gh stack modify --continue

# Abort modify session and restore stack to pre-modify state
gh stack modify --abort
```

Flags:

- `--continue`: Continue after resolving conflicts.
- `--abort`: Abort session and restore stack to previous state.

#### `gh stack unstack` (alias: `delete`)

Remove stack tracking locally and/or on GitHub.

- With no arguments, unstacks the currently checked-out stack.
- PRs queued for merge or with auto-merge enabled are kept stacked by GitHub.

```bash
# Unstack the active stack both locally and on GitHub
gh stack unstack

# Unstack a specific stack by its GitHub stack number (works without local checkout)
gh stack unstack 7

# Remove local tracking only (keeps stack on GitHub intact)
gh stack unstack --local
```

Flags:

- `--local`: Only delete local tracking metadata, preserving the stack on GitHub.

#### `gh stack view`

Display stack branches, status, and PR links.

```bash
# Interactive view with commit logs and status icons
gh stack view

# Compact one-line-per-branch view
gh stack view --short

# Output full stack structure as JSON
gh stack view --json
```

Status Icons:

| Icon | Description  |
| :--: | :----------- |
| `✓`  | PR merged    |
| `◎`  | PR queued    |
| `○`  | PR open      |
| `⚠`  | Needs rebase |

______________________________________________________________________

### 2. Navigation & Checkout (`checkout`, `switch`, `up`, `down`, `top`, `bottom`, `trunk`)

```bash
# Check out a stack by stack number, PR number, PR URL, or branch name
gh stack checkout 7
gh stack checkout 123
gh stack checkout https://github.com/owner/repo/pull/123
gh stack checkout feat/step-2

# Interactive stack picker (lists local and remote stacks)
gh stack checkout

# Interactive branch picker for current stack (j/k or arrows to navigate)
gh stack switch

# Move one branch up (away from trunk) or down (toward trunk)
gh stack up
gh stack down

# Jump to endpoints of the current stack
gh stack top       # Top branch (furthest from trunk)
gh stack bottom    # Bottom branch (closest to trunk)
gh stack trunk     # Base trunk branch
```

______________________________________________________________________

### 3. Remote Operations & Synchronization (`push`, `submit`, `sync`, `link`, `rebase`, `merge`)

#### `gh stack push`

Push active branches in the current stack to the remote using per-branch `--force-with-lease`.

- Updates are not atomic across branches: each branch updates individually.
- Merged and queued branches are automatically skipped.
- Does **not** create or update PRs or stack objects on GitHub (use `submit` for PR management).

```bash
# Push active stack branches to default remote
gh stack push

# Push to a specific remote
gh stack push --remote upstream
```

Flags:

- `--remote <string>`: Remote to push to (defaults to auto-detected remote).

#### `gh stack submit`

Push branches, create or update PRs on GitHub, update PR base branches, and build the GitHub stack object.

- In interactive terminal: opens a single-screen editor to customize PR titles, descriptions, and draft states. Save with `Ctrl+S`.
- In non-interactive terminals or automated scripts: pass `--auto` to skip editor and generate PR titles automatically.

```bash
# Open interactive PR editor and submit stack
gh stack submit

# Automated submission: skip editor and auto-generate PR titles (creates as drafts)
gh stack submit --auto

# Mark new and existing PRs as ready for review (not draft)
gh stack submit --open
gh stack submit --auto --open

# Push to a specific remote
gh stack submit --remote upstream
```

Flags:

- `--auto`: Use auto-generated PR titles without prompting (creates as drafts unless `--open` is specified).
- `--open`: Mark new and existing PRs as ready for review.
- `--remote <string>`: Remote to push to.

#### `gh stack sync`

Pull changes, cascade-rebase stack branches, atomically push to remote, and sync PR state.

- Steps performed:
  1. Fetches latest remote changes.
  2. Reconciles local and remote stack state.
  3. Fast-forwards trunk branch.
  4. Cascade-rebases branches onto their updated parents.
  5. Pushes all branches atomically (`--force-with-lease --atomic`).
  6. Syncs PR state from GitHub.
  7. Links open PRs into GitHub stack object (when 2+ PRs exist).
- Does **not** create new PRs (use `submit` to create PRs).

```bash
# Sync and cascade-rebase current stack
gh stack sync

# Sync and delete local branches for already-merged PRs
gh stack sync --prune

# Sync with a specific remote
gh stack sync --remote upstream
```

Flags:

- `--prune`: Delete local branches for merged PRs (moves checkout to first active branch or trunk).
- `--remote <string>`: Remote to fetch from and push to.

#### `gh stack link`

Create or update a stack on GitHub from branch names, PR numbers, or PR URLs **without local stack tracking**.

- Designed for workflows using external tools or multiple worktrees.
- Automatically pushes branch arguments to remote before creating or linking PRs.
- Order arguments bottom to top.
- If first argument is an existing stack number, appends remaining arguments to the top of that stack.

```bash
# Link branches into a stack (bottom to top)
gh stack link feat/step-1 feat/step-2 feat/step-3

# Link existing PR numbers
gh stack link 101 102 103

# Link by PR URLs
gh stack link https://github.com/owner/repo/pull/101 https://github.com/owner/repo/pull/102

# Append branches/PRs to an existing GitHub stack (stack number 7)
gh stack link 7 104 feat/step-5

# Specify custom base branch for the bottom of the stack
gh stack link --base develop feat/step-1 feat/step-2

# Mark PRs ready for review
gh stack link --open 101 102
```

Flags:

- `--base <string>`: Base branch for bottom of stack.
- `--open`: Mark PRs ready for review.
- `--remote <string>`: Remote to push to.

#### `gh stack rebase`

Cascade-rebase branches across the stack to ensure each layer includes the tip of its parent.

```bash
# Rebase the entire stack from trunk to top
gh stack rebase

# Only rebase from trunk to the current branch
gh stack rebase --downstack

# Only rebase from current branch to the top
gh stack rebase --upstack

# Rebase stack branches onto each other without pulling/rebasing trunk
gh stack rebase --no-trunk

# Continue rebase after resolving merge conflicts
gh stack rebase --continue

# Abort rebase and restore branches
gh stack rebase --abort
```

Flags:

- `--continue`: Continue rebase after resolving conflicts.
- `--abort`: Abort rebase and restore all branches.
- `--downstack`: Only rebase branches between trunk and current branch.
- `--upstack`: Only rebase branches between current branch and top.
- `--no-trunk`: Skip trunk branch.
- `--committer-date-is-author-date` / `--preserve-dates`: Preserve commit dates.
- `--remote <string>`: Remote to fetch from.

#### `gh stack merge`

Atomically merge all or part of a stack into the base trunk branch (all-or-nothing merge).

- Merges all PRs up to and including the target PR.
- If the base branch uses a merge queue, the stack enters the queue together.

```bash
# Interactive merge wizard for current stack
gh stack merge

# Merge an unchecked-out stack by stack number
gh stack merge 7

# Merge up to and including PR #42
gh stack merge 42

# Non-interactive merge of entire stack with squash merge
gh stack merge --yes --squash
gh stack merge 123 --squash --yes

# Specific merge methods
gh stack merge --squash
gh stack merge --rebase
gh stack merge --merge
```

Flags:

- `-y, --yes`: Merge without prompting.
- `--squash`: Squash and merge.
- `--rebase`: Rebase and merge.
- `--merge`: Create merge commit.
- `--merge-method <string>`: `squash`, `rebase`, or `merge`.

______________________________________________________________________

### 4. Utilities (`alias`)

```bash
# Install 'gs' alias for 'gh stack' in ~/.local/bin/
gh stack alias

# Install custom alias name
gh stack alias gst

# Remove alias
gh stack alias --remove
```

______________________________________________________________________

## Troubleshooting & Common Pitfalls

### 1. `git worktree` Collisions During `init`

**Error**:
`✗ switching to branch <branch>: failed to run git: fatal: '<branch>' is already used by worktree at '<path>'`

**Cause**: `gh stack init` validates and tracks each branch by checking it out sequentially. If any branch in the stack is currently checked out in another `git worktree`, Git refuses to checkout the branch in two places simultaneously.

**Solutions**:

- **Use `gh stack link`**: If branches are already pushed, use `gh stack link` with branch names or PR numbers. `gh stack link` operates via the GitHub API and does not need to switch local branches.
- **Copy stack metadata**: `gh stack` stores its tracking state in `.git/gh-stack` (or `.git/worktrees/<name>/gh-stack`). Initializing from one worktree writes the state file there; you can copy it to sibling worktrees if needed.

### 2. Branch Already Part of a Stack

**Error**:
`✗ current branch "<branch>" is already part of a stack`

**Cause**: A previous stack session already registered this branch in local metadata.

**Solution**:
Run `gh stack unstack --local` to clear local stack tracking without touching remote PRs on GitHub:

```bash
gh stack unstack --local
```

Then re-run `gh stack init`.

### 3. Non-Interactive / Script Execution Hanging

**Issue**: Running `gh stack submit` in agent sessions or non-interactive CI environments hangs waiting for the TUI PR editor.

**Solution**: Always pass `--auto` (and `--open` if desired):

```bash
gh stack submit --auto --open
```

### 4. `push` vs. `submit` vs. `sync`

| Command               | What it Does                                                                            |    Creates PRs?     | Updates Remote Stack? |
| :-------------------- | :-------------------------------------------------------------------------------------- | :-----------------: | :-------------------: |
| **`gh stack push`**   | Force-pushes active branches to remote                                                  |         No          |          No           |
| **`gh stack submit`** | Pushes branches, creates/updates PRs, updates base branches                             |         Yes         |          Yes          |
| **`gh stack sync`**   | Fetches, cascade-rebases, atomically pushes, links existing PRs, prunes merged branches | No (links existing) |          Yes          |
