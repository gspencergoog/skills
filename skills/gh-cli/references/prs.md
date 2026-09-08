# GitHub CLI Pull Requests Reference (`gh pr`)

Command reference and troubleshooting guide for pull request operations in GitHub CLI (`gh pr`).

For stacked pull request workflows, see [stacks.md](stacks.md).

> [!TIP]
> A pull request can be specified by number (e.g. `123`), URL (`https://github.com/owner/repo/pull/123`), or head branch (`feat/login` or `owner:feat/login`). If omitted, targeted commands select the PR corresponding to the current git branch.

______________________________________________________________________

## Commands

### 1. Create & Draft PRs (`create`, `ready`)

#### `gh pr create`

Create a pull request on GitHub. If the current branch is not pushed, prompts to push or fork (unless `--head` is provided).

```bash
# Create a standard pull request with title and body
gh pr create --title "Feature description" --body "Detailed explanation of changes."

# Create a draft pull request with reviewers and assignees
env -u GITHUB_TOKEN gh pr create --title "Feature description" --body "Details..." --draft --reviewer user1 --assignee @me

# Auto-fill title and description from commit messages (ideal for automation)
gh pr create --fill

# Use first commit for title/body, or use commit bodies for verbose description
gh pr create --fill-first
gh pr create --fill-verbose

# Read body from a markdown file or standard input
gh pr create --title "Release v1.2" --body-file ./release-notes.md
echo "Automated PR summary" | gh pr create --title "Automated update" --body-file -

# Specify base branch and head branch explicitly
gh pr create --base main --head feature-branch

# Target fork head branch
gh pr create --base develop --head username:feature-branch

# Attach screenshots or screen recordings with optional alt text
gh pr create --title "Fix UI alignment" --body "See attached" --attach './login.png#Login screen preview'

# Preview PR creation without modifying remote state
gh pr create --dry-run
```

Flags:

- `-B, --base <branch>`: The branch into which you want your code merged.
- `-H, --head <branch>`: The branch that contains commits for your pull request (default: current branch).
- `-t, --title <string>`: Title for the pull request.
- `-b, --body <string>`: Body text for the pull request.
- `-F, --body-file <file>`: Read body from file (use `-` for stdin).
- `-d, --draft`: Mark pull request as a draft.
- `-f, --fill`: Use commit info for title and body.
- `--fill-first`: Use first commit info for title and body.
- `--fill-verbose`: Include commit messages and bodies in PR description.
- `-r, --reviewer <handles>`: Request reviews from people or teams (comma-separated or repeated).
- `-a, --assignee <logins>`: Assign people by login (use `@me` to self-assign).
- `-l, --label <names>`: Add labels by name.
- `-m, --milestone <name>`: Add PR to a milestone by name.
- `-p, --project <title>`: Add PR to a GitHub project by title.
- `-T, --template <file>`: Template file to use as starting body text.
- `--attach <file#alt>`: Attach images or videos (up to 50 files).
- `--no-maintainer-edit`: Prevent upstream maintainers from pushing commits to the PR branch.
- `--dry-run`: Print details instead of creating the PR.
- `-w, --web`: Open PR creation form in the browser.

#### `gh pr ready`

Mark a draft pull request as ready for review.

```bash
# Mark PR for current branch as ready for review
gh pr ready

# Mark a specific PR as ready
gh pr ready 123
```

Flags:

- `--undo`: Convert a ready PR back to draft status.

______________________________________________________________________

### 2. View, List & Diff PRs (`list`, `view`, `diff`, `status`)

#### `gh pr list`

List pull requests in a repository with rich filtering options.

```bash
# List open pull requests authored by current user
gh pr list --state open --author @me

# Filter by state, review status, and labels
gh pr list --state all --label bug,core --search "review:required"

# Filter by base branch and assignee
gh pr list --base main --assignee user1

# Output structured JSON with jq filtering
gh pr list --json number,title,headRefName,statusCheckRollup --jq '.[].title'
```

Flags:

- `-s, --state <string>`: Filter by state: `{open|closed|merged|all}` (default: `open`).
- `-A, --author <string>`: Filter by author (`@me` or username).
- `-a, --assignee <string>`: Filter by assignee (`@me` or username).
- `-B, --base <string>`: Filter by base branch.
- `-H, --head <string>`: Filter by head branch.
- `-l, --label <strings>`: Filter by labels (comma-separated).
- `-S, --search <query>`: Search PRs using GitHub search syntax (e.g. `review:approved`, `draft:false`).
- `-L, --limit <int>`: Maximum number of items to fetch (default: 30).
- `--json <fields>`: Output JSON with specified fields.
- `-q, --jq <expression>`: Filter JSON output with jq.
- `-t, --template <string>`: Format output with a Go template.
- `-w, --web`: Open PR list in the browser.

#### `gh pr view`

Display overview, comments, and metadata for a pull request.

```bash
# View PR summary in terminal
gh pr view 123

# View PR along with conversation comments
gh pr view 123 --comments

# View a PR with comments in the browser
env -u GITHUB_TOKEN gh pr view 123 --comments --web

# Extract specific JSON metadata fields
gh pr view 123 --json title,body,state,baseRefName,headRefName,reviews
```

Flags:

- `-c, --comments`: View pull request comments.
- `-w, --web`: Open PR in web browser.
- `--json <fields>`: Output JSON with specified fields.
- `-q, --jq <expr>`: Filter JSON output.

#### `gh pr diff`

Display the changes introduced by a pull request.

```bash
# View diff of changes in a PR
gh pr diff 123

# View diff as patch format
gh pr diff 123 --patch

# Show only changed filenames
gh pr diff 123 --name-only
```

Flags:

- `--name-only`: Only display names of changed files.
- `--patch`: Display diff in patch format.
- `--color <always|never|auto>`: Colorize terminal output.

#### `gh pr status`

Show status of relevant pull requests in the repository (created by you, requesting your review, or assigned to you).

```bash
# Show status of relevant PRs
gh pr status

# Check conflict status with the base branch
gh pr status --conflict-status
```

Flags:

- `-c, --conflict-status`: Display whether PRs have merge conflicts with their base branch.

______________________________________________________________________

### 3. Checkout & Update Branches (`checkout`, `update-branch`)

#### `gh pr checkout`

Check out a pull request branch locally.

```bash
# Checkout a PR branch locally by PR number
gh pr checkout 123

# Checkout into a custom local branch name
gh pr checkout 123 -b local-feature-test

# Force checkout, discarding uncommitted local changes
gh pr checkout 123 --force

# Recurse submodules when checking out
gh pr checkout 123 --recurse-submodules
```

Flags:

- `-b, --branch <string>`: Local branch name to use.
- `-f, --force`: Reset local branch to match PR head even if uncommitted changes exist.
- `--detach`: Check out PR head commit in detached HEAD state.
- `--recurse-submodules`: Update submodules after checkout.

#### `gh pr update-branch`

Update a pull request head branch with the latest changes from its base branch.

```bash
# Update PR head branch by merging the base branch
gh pr update-branch 123

# Update PR head branch by rebasing onto the base branch
gh pr update-branch 123 --rebase
```

Flags:

- `-r, --rebase`: Update using rebase instead of merge commit.

______________________________________________________________________

### 4. Review & Comment (`review`, `comment`)

#### `gh pr review`

Submit an approval, change request, or review comment on a pull request.

```bash
# Approve a pull request with a comment
gh pr review 123 --approve --body "LGTM!"

# Request changes on a pull request with feedback
gh pr review 123 --request-changes --body "Please fix..."

# Leave an overall review comment
gh pr review 123 --comment -b "Looks good, left non-blocking questions."

# Read review comment from a file
gh pr review 123 -r -F ./review-feedback.md
```

Flags:

- `-a, --approve`: Approve pull request.
- `-r, --request-changes`: Request changes on pull request.
- `-c, --comment`: Submit a general review comment without approval/rejection.
- `-b, --body <string>`: Specify review comment body.
- `-F, --body-file <file>`: Read review comment body from file (`-` for stdin).

#### `gh pr comment`

Add a regular comment to a pull request conversation thread.

```bash
# Add a comment to a PR
gh pr comment 123 --body "CI checks re-triggered."

# Add comment from file
gh pr comment 123 --body-file release-notes.md

# Edit your previous comment on the PR
gh pr comment 123 --edit-last --body "Updated test results."
```

Flags:

- `-b, --body <string>`: Comment body text.
- `-F, --body-file <file>`: Read body from file.
- `--edit-last`: Edit your last comment instead of posting a new one.

______________________________________________________________________

### 5. Checks & CI Monitoring (`checks`)

#### `gh pr checks`

Show CI status checks for a pull request.

```bash
# View CI status checks for a PR
gh pr checks 123

# Watch checks until they complete
gh pr checks 123 --watch

# Exit immediately on the first failed check during watch
gh pr checks 123 --watch --fail-fast

# Only show required checks
gh pr checks 123 --required

# Inspect checks as JSON with bucket status (pass, fail, pending, skipping, cancel)
gh pr checks 123 --json bucket,state,name,link
```

Flags:

- `--watch`: Watch checks until completion.
- `--fail-fast`: Exit watch mode immediately if any check fails.
- `--required`: Only show checks marked as required by branch protection rules.
- `-i, --interval <int>`: Refresh interval in seconds during watch (default: 10).
- `--json <fields>`: Output JSON fields (`bucket`, `completedAt`, `description`, `event`, `link`, `name`, `startedAt`, `state`, `workflow`).
- `-w, --web`: View check runs in the browser.

Exit Codes:

- `0`: All checks passed.
- `1`: One or more checks failed.
- `8`: Checks pending / in progress.

______________________________________________________________________

### 6. Merge, Close & Reopen (`merge`, `close`, `reopen`, `revert`)

#### `gh pr merge`

Merge a pull request into its base branch.

```bash
# Squash and merge a PR, delete branch, and enable auto-merge if checks pending
gh pr merge 123 --squash --delete-branch --auto

# Merge immediately with a merge commit and custom commit message
gh pr merge 123 --merge --subject "Merge feature #123" --body "Closes #123"

# Rebase and merge
gh pr merge 123 --rebase --delete-branch

# Disable an already-enabled auto-merge
gh pr merge 123 --disable-auto

# Bypass merge requirements using administrator privileges
gh pr merge 123 --squash --admin
```

Flags:

- `-s, --squash`: Squash and merge.
- `-r, --rebase`: Rebase and merge.
- `-m, --merge`: Merge using a merge commit.
- `-d, --delete-branch`: Delete head branch locally and remotely after merge.
- `--auto`: Automatically merge after required checks and reviews pass.
- `--disable-auto`: Cancel auto-merge.
- `-t, --subject <text>`: Subject line for merge commit.
- `-b, --body <text>`: Body text for merge commit.
- `-F, --body-file <file>`: Read merge commit body from file.
- `--admin`: Use administrator privileges to bypass requirements.
- `--match-head-commit <SHA>`: Ensure head branch commit SHA matches before allowing merge.

#### `gh pr close` & `gh pr reopen`

```bash
# Close a PR with an explanation
gh pr close 123 --comment "Closing in favor of PR #125"

# Close and delete the head branch
gh pr close 123 --delete-branch

# Reopen a previously closed PR
gh pr reopen 123 --comment "Reopening after rebasing against main"
```

#### `gh pr revert`

Create a revert pull request on GitHub.

```bash
# Create a PR that reverts PR #123
gh pr revert 123
```

______________________________________________________________________

### 7. Edit PRs (`edit`, `lock`, `unlock`)

#### `gh pr edit`

Edit metadata on an existing pull request.

```bash
# Change PR base branch
gh pr edit 123 --base develop

# Update title and body
gh pr edit 123 --title "New title" --body "Updated description"

# Manage labels, reviewers, and assignees
gh pr edit 123 --add-label "ready-to-merge" --remove-label "needs-work"
gh pr edit 123 --add-reviewer octocat --add-assignee @me
```

Flags:

- `-B, --base <branch>`: Change base branch.
- `-t, --title <string>`: Update PR title.
- `-b, --body <string>`: Update PR body.
- `-F, --body-file <file>`: Update body from file.
- `--add-label <names>` / `--remove-label <names>`: Modify labels.
- `--add-reviewer <logins>` / `--remove-reviewer <logins>`: Modify reviewers.
- `--add-assignee <logins>` / `--remove-assignee <logins>`: Modify assignees.
- `--milestone <name>` / `--remove-milestone`: Modify milestone.

#### `gh pr lock` & `gh pr unlock`

```bash
# Lock PR conversation
gh pr lock 123 --reason "resolved"

# Unlock PR conversation
gh pr unlock 123
```

______________________________________________________________________

## Troubleshooting & Common Pitfalls

1. **Non-Interactive Automation**:
   - In automated agent sessions or CI pipelines, commands like `gh pr create` without `--fill` or `--title`/`--body` will trigger an interactive TUI editor that blocks execution. Always pass `--fill` or `--title` and `--body`/`--body-file`.
2. **Checks Watch Exit Code 8**:
   - When running `gh pr checks --watch`, an exit code of `8` indicates that checks are still in progress or pending. Scripts should handle exit code 8 as pending rather than fatal error.
3. **Merge Queues vs Auto-Merge**:
   - If a target repository uses GitHub Merge Queues, `--auto` adds the pull request to the merge queue once all pre-merge conditions pass, rather than merging immediately.
4. **Bulk PR Modifications**:
   - Query PR numbers with `--json number --jq '.[].number'` and pipe into `xargs` to batch modify labels or reviewers:
     ```bash
     # Add label to all PRs requiring review
     gh pr list --search "review:required" --json number --jq '.[].number' | \
       xargs -I {} gh pr edit {} --add-label needs-review
     ```
