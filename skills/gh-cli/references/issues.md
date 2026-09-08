# GitHub CLI Issues Reference (`gh issue`)

Command reference and troubleshooting guide for issue operations in GitHub CLI (`gh issue`).

> [!TIP]
> An issue can be supplied by number (e.g. `123`) or full URL (`https://github.com/owner/repo/issues/123`).

______________________________________________________________________

## Commands

### 1. Create & View Issues (`create`, `view`, `status`)

#### `gh issue create`

Create a new issue on GitHub.

```bash
# Create an issue with title, body, label, and assignee
gh issue create --title "Bug description" --body "Details..." --labels bug --assignee @me

# Create issue reading body from file or standard input
gh issue create --title "Crash report" --body-file ./crash.log
echo "Auto-generated issue text" | gh issue create --title "Telemetry Alert" --body-file -

# Create issue with milestone, project, and template
gh issue create --title "New Feature" --body "Spec..." --milestone "v1.0" --project "Roadmap" --template "feature_request.md"

# Recover input from a previously interrupted create attempt
gh issue create --recover ~/.config/gh/state.yml
```

Flags:

- `-t, --title <string>`: Issue title.
- `-b, --body <string>`: Issue body text.
- `-F, --body-file <file>`: Read body from file (use `-` for stdin).
- `-a, --assignee <logins>`: Assign users by login (use `@me` to self-assign).
- `-l, --label <names>`: Add labels by name.
- `-m, --milestone <name>`: Add issue to a milestone.
- `-p, --project <title>`: Add issue to projects by title.
- `-T, --template <file>`: Use a specific issue template file.
- `--recover <file>`: Recover input from a failed run of create.
- `-w, --web`: Open issue creation in the browser.

#### `gh issue view`

Display details, discussion comments, and metadata for an issue.

```bash
# View issue summary in terminal
gh issue view 123

# View an issue and its comments
gh issue view 123 --comments

# Open issue in the browser
gh issue view 123 --web

# Extract specific JSON fields
gh issue view 123 --json title,body,state,labels,assignees,comments
```

Flags:

- `-c, --comments`: View conversation comments.
- `-w, --web`: Open in web browser.
- `--json <fields>`: Output JSON with specified fields.
- `-q, --jq <expr>`: Filter output using jq.

#### `gh issue status`

Show status of issues relevant to the current user (assigned to you, mentioned, or created by you).

```bash
# Show status of relevant issues
gh issue status
```

______________________________________________________________________

### 2. List & Search Issues (`list`)

#### `gh issue list`

List and search issues in a repository.

```bash
# List open issues assigned to current user with a label
gh issue list --state open --assignee @me --labels bug

# Search open bug issues and output JSON fields
gh issue list --search "is:open is:issue label:bug" --json number,title

# Filter by milestone and author
gh issue list --milestone "v1.0" --author "@me"

# Output structured data filtered with jq
gh issue list --json number,title,labels --jq '.[] | {number, title, labels: [.labels[].name]}'
```

Flags:

- `-s, --state <string>`: Filter by state: `{open|closed|all}` (default: `open`).
- `-a, --assignee <string>`: Filter by assignee (`@me` or username).
- `-A, --author <string>`: Filter by author (`@me` or username).
- `-l, --label <strings>`: Filter by label names (comma-separated).
- `-m, --milestone <string>`: Filter by milestone name.
- `-S, --search <query>`: Search issues with GitHub search query syntax.
- `-L, --limit <int>`: Maximum number of items to fetch (default: 30).
- `--json <fields>`: Output JSON with specified fields.
- `-q, --jq <expression>`: Filter JSON output with jq.
- `-w, --web`: Open issues list in browser.

______________________________________________________________________

### 3. Edit, Close & Reopen (`edit`, `close`, `reopen`, `delete`)

#### `gh issue edit`

Update issue title, description, labels, milestone, and assignments.

```bash
# Edit issue to add a label and milestone
gh issue edit 123 --add-label high-priority --milestone "v1.0"

# Update title and body text
gh issue edit 123 --title "Updated Title" --body "Updated description"

# Remove labels and assignees
gh issue edit 123 --remove-label "needs-triage" --remove-assignee user1
```

Flags:

- `-t, --title <string>`: Set new title.
- `-b, --body <string>`: Set new body.
- `-F, --body-file <file>`: Update body from file.
- `--add-label <names>` / `--remove-label <names>`: Add or remove labels.
- `--add-assignee <logins>` / `--remove-assignee <logins>`: Add or remove assignees.
- `--milestone <name>` / `--remove-milestone`: Set or remove milestone.
- `--add-project <title>` / `--remove-project <title>`: Add or remove project.

#### `gh issue close`

Close an issue with an optional comment and structured resolution reason.

```bash
# Close an issue with a comment
gh issue close 123 --comment "Fixed in PR #456"

# Close as completed or not planned
gh issue close 123 --reason "completed"
gh issue close 123 --reason "not planned"

# Close as duplicate of another issue
gh issue close 123 --duplicate-of 456
```

Flags:

- `-c, --comment <string>`: Leave a closing comment.
- `-r, --reason <string>`: Reason for closing: `{completed|not planned|duplicate}`.
- `--duplicate-of <number|url>`: Mark as duplicate of another issue.

#### `gh issue reopen`

Reopen a closed issue.

```bash
# Reopen a closed issue
gh issue reopen 123

# Reopen with an explanatory comment
gh issue reopen 123 --comment "Reopening: bug reproduced on v1.2"
```

Flags:

- `-c, --comment <string>`: Add comment when reopening.

#### `gh issue delete`

Permanently delete an issue.

```bash
# Delete issue without prompting
gh issue delete 123 --yes
```

Flags:

- `--yes`: Confirm deletion without interactive prompt.

______________________________________________________________________

### 4. Branch Integration & Comments (`develop`, `comment`, `transfer`)

#### `gh issue develop`

Create or manage linked development branches for an issue.

```bash
# Create a branch to start developing a fix for an issue
gh issue develop 123 --branch fix/issue-123

# Create a branch and checkout immediately
gh issue develop 123 --name fix/auth-bug --checkout

# Create a branch based on a custom base branch
gh issue develop 123 --base develop --checkout

# Create a branch and check it out directly into a dedicated git worktree
gh issue develop 123 --checkout --worktree ../worktree-issue-123

# List linked branches for an issue
gh issue develop --list 123
```

Flags:

- `-n, --name <string>` / `--branch <string>`: Name of the branch to create.
- `-b, --base <string>`: Remote base branch to branch off of.
- `-c, --checkout`: Check out the branch locally after creating it.
- `--worktree <path>`: Check out the branch into a dedicated git worktree at the given path.
- `-l, --list`: List linked branches for the issue.
- `--branch-repo <string>`: Target repository for the new branch.

#### `gh issue comment`

Add comments to an issue conversation.

```bash
# Add a comment to an issue
gh issue comment 123 --body "Investigating this..."

# Add comment from a file
gh issue comment 123 --body-file reproduction-steps.md

# Edit your previous comment on the issue
gh issue comment 123 --edit-last --body "Updated root cause analysis."
```

Flags:

- `-b, --body <string>`: Comment body text.
- `-F, --body-file <file>`: Read comment body from file.
- `--edit-last`: Edit your last comment instead of adding a new one.

#### `gh issue transfer`

Transfer an issue to another repository.

```bash
# Transfer an issue to another repository
gh issue transfer 123 --repo owner/new-repo
```

______________________________________________________________________

### 5. Locking & Pinning (`pin`, `unpin`, `lock`, `unlock`)

```bash
# Pin an issue to the repository
gh issue pin 123

# Unpin an issue
gh issue unpin 123

# Lock issue conversation with reason
gh issue lock 123 --reason "resolved"

# Unlock issue conversation
gh issue unlock 123
```

Reasons for lock: `{off-topic|too heated|resolved|spam}`.

______________________________________________________________________

## Troubleshooting & Automation Tips

1. **Non-Interactive Execution**:
   - Always supply `--title` and `--body` (or `--body-file`) when calling `gh issue create` in automation or scripts; omitting them invokes an interactive prompt/editor that halts automated agents.
2. **Linked Branch PR Closure**:
   - Creating a branch with `gh issue develop` links the branch to the issue. When a PR is created from that branch and merged, GitHub automatically closes the linked issue.
3. **Bulk Issue Operations**:
   - Query issue numbers with `--json number --jq '.[].number'` and pipe into `xargs` for batch modifications:
     ```bash
     # Bulk close stale issues
     gh issue list --search "label:stale" --json number --jq '.[].number' | \
       xargs -I {} gh issue close {} --comment "Closing as stale"
     ```
