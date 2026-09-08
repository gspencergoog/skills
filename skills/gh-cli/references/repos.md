# GitHub CLI Repositories Reference (`gh repo`)

Command reference and troubleshooting guide for repository operations in GitHub CLI (`gh repo`).

> [!TIP]
> Repositories can be specified as `OWNER/REPO`, by remote URL (`https://github.com/OWNER/REPO`), or omitted when executed inside a tracked git repository directory.

______________________________________________________________________

## Commands

### 1. Create & Clone Repositories (`create`, `clone`)

#### `gh repo create`

Create a new GitHub repository remotely, optionally cloning it or initializing from a local folder.

```bash
# Create a public repository with a description
gh repo create my-repo --public --description "My project"

# Create a private repository under an organization
gh repo create org/my-repo --private

# Create a repository and clone it to the current directory
gh repo create my-repo --public --clone

# Create a repository from an existing local folder and push initial commits
gh repo create my-project --private --source=. --remote=origin --push

# Create from a template repository
gh repo create my-service --template owner/service-template --private

# Initialize with README, gitignore, and open-source license
gh repo create my-app --public --add-readme --gitignore Node --license MIT
```

Flags:

- `--public`: Make the repository public.
- `--private`: Make the repository private.
- `--internal`: Make the repository internal (Enterprise organizations only).
- `-d, --description <string>`: Description of the repository.
- `-h, --homepage <URL>`: Repository homepage URL.
- `-c, --clone`: Clone the new repository to the current directory.
- `-s, --source <path>`: Specify local directory to use as source.
- `-r, --remote <string>`: Specify remote name for the new repository (default: `origin`).
- `--push`: Push local commits to the new repository.
- `--add-readme`: Add a README file to the new repository.
- `-g, --gitignore <template>`: Specify a `.gitignore` template (e.g. `Python`, `Node`, `Go`).
- `-l, --license <keyword>`: Specify an Open Source License (e.g. `MIT`, `Apache-2.0`).
- `-p, --template <owner/repo>`: Base repository on a template repository.
- `-t, --team <name>`: Organization team to be granted access.
- `--disable-issues`: Disable issues in the repository.
- `--disable-wiki`: Disable wiki in the repository.

#### `gh repo clone`

Clone a repository locally, automatically configuring remotes and protocol settings.

```bash
# Clone a repository
gh repo clone owner/repo

# Clone a specific branch into a custom directory
gh repo clone owner/repo my-directory --branch develop

# Pass git flags directly through to git clone
gh repo clone owner/repo -- --depth 1
```

Flags:

- `-b, --branch <name>`: Clone a specific branch.
- `-u, --upstream-remote-name <string>`: Upstream remote name when cloning a fork (default: `upstream`).
- Any trailing arguments after `--` are passed directly to `git clone`.

______________________________________________________________________

### 2. View, List & Search Repositories (`list`, `view`)

#### `gh repo list`

List repositories owned by a user or organization.

```bash
# List public repositories for an owner (up to limit)
gh repo list owner --limit 50 --public

# Output repository names as filtered JSON
gh repo list --json name,visibility --jq '.[].name'

# Filter by visibility and forks
gh repo list my-org --private --no-archived --limit 100

# Filter by programming language
gh repo list owner --language typescript
```

Flags:

- `-L, --limit <int>`: Maximum number of repositories to fetch (default: 30).
- `--public` / `--private` / `--internal`: Filter by visibility.
- `--fork` / `--source`: Filter for forks or source repositories only.
- `--archived` / `--no-archived`: Filter by archived state.
- `-l, --language <string>`: Filter by primary language.
- `--topic <string>`: Filter by repository topic.
- `--json <fields>`: Output JSON with specified fields.
- `-q, --jq <expression>`: Filter JSON output with jq.

#### `gh repo view`

View repository description, README, or metadata.

```bash
# View README of current repository in terminal
gh repo view

# View repository metadata as JSON
gh repo view owner/repo --json name,description

# Open repository in the web browser
gh repo view --web

# View a specific branch of a repository
gh repo view owner/repo --branch develop
```

Flags:

- `-b, --branch <name>`: View specific branch.
- `-w, --web`: Open repository in the browser.
- `--json <fields>`: Output JSON fields (`name`, `owner`, `description`, `visibility`, `defaultBranchRef`, `isFork`, `isPrivate`, `parent`).
- `-q, --jq <expr>`: Filter output using jq.

______________________________________________________________________

### 3. Edit & Configure Repositories (`edit`, `set-default`)

#### `gh repo edit`

Edit repository settings, features, topics, and visibility.

```bash
# Update repository description
gh repo edit --description "New description"

# Change visibility to private
gh repo edit --visibility private --accept-visibility-change-consequences

# Enable repository issues and wiki
gh repo edit --enable-issues --enable-wiki

# Disable projects or wiki (use --<flag>=false syntax)
gh repo edit --enable-projects=false --enable-wiki=false

# Enable automated features: auto-merge and auto-delete branch on merge
gh repo edit --enable-auto-merge --delete-branch-on-merge

# Change squash merge commit message style
gh repo edit --enable-squash-merge --squash-merge-commit-message pr-title

# Add and remove repository topics
gh repo edit --add-topic "cli,tooling,automation" --remove-topic "deprecated"

# Make repository available as a template
gh repo edit --template
```

Flags:

- `-d, --description <string>`: Set description.
- `-h, --homepage <URL>`: Set homepage URL.
- `--visibility <public|private|internal>`: Change repository visibility.
- `--accept-visibility-change-consequences`: Required when changing repository visibility.
- `--default-branch <name>`: Set the default branch name.
- `--enable-auto-merge`: Enable auto-merge capability.
- `--delete-branch-on-merge`: Automatically delete head branches upon PR merge.
- `--allow-update-branch`: Allow updating PR branches from base via UI.
- `--allow-forking`: Allow forking of an organization repository.
- `--enable-issues` / `--enable-wiki` / `--enable-projects` / `--enable-discussions`: Toggle features.
- `--enable-squash-merge` / `--enable-rebase-merge` / `--enable-merge-commit`: Toggle merge types.
- `--squash-merge-commit-message <style>`: `{default|pr-title|pr-title-commits|pr-title-description}`.
- `--add-topic <strings>` / `--remove-topic <strings>`: Manage topics.
- `--template`: Make repository a template.

#### `gh repo set-default`

Configure the default remote repository to use for GitHub API operations in the current local folder.

```bash
# Interactively choose default repository
gh repo set-default

# Set default repository explicitly
gh repo set-default owner/repo

# Set default repository from an existing git remote name
gh repo set-default origin

# View the current default repository
gh repo set-default --view

# Unset default repository mapping
gh repo set-default --unset
```

Flags:

- `-v, --view`: View current default repository.
- `-u, --unset`: Unset default repository mapping.

______________________________________________________________________

### 4. Fork, Sync, Rename & Delete (`fork`, `sync`, `rename`, `delete`, `archive`)

#### `gh repo fork`

Fork a repository to your personal account or an organization.

```bash
# Fork a repository, clone it locally, and configure upstream remote
gh repo fork owner/repo --clone --remote-name upstream

# Fork without cloning
gh repo fork owner/repo --clone=false

# Fork into a specific organization
gh repo fork owner/repo --org my-org
```

Flags:

- `--clone`: Clone the fork locally (default: prompts in interactive mode).
- `--remote`: Configure remote for fork in existing clone.
- `--remote-name <name>`: Remote name for upstream repository (default: `upstream`).
- `--org <name>`: Fork to an organization.

#### `gh repo sync`

Sync a local clone or remote fork branch with the upstream source repository.

```bash
# Sync local or remote branch with upstream
gh repo sync owner/repo --branch main

# Sync current local clone directly from remote parent
gh repo sync

# Hard reset branch to match upstream exactly (force sync)
gh repo sync owner/repo --branch main --force

# Sync from a custom source repository
gh repo sync owner/repo --source upstream-org/repo
```

Flags:

- `-b, --branch <name>`: Branch to sync (default: default branch).
- `-s, --source <owner/repo>`: Specify custom source repository.
- `--force`: Hard reset destination branch to match source.

#### `gh repo rename` & `gh repo archive`

```bash
# Rename the current repository
gh repo rename new-repo-name --yes

# Archive a repository (makes it read-only)
gh repo archive owner/repo --yes

# Unarchive a repository
gh repo unarchive owner/repo --yes
```

#### `gh repo delete`

Permanently delete a repository on GitHub.

```bash
# Delete a repository without prompting
gh repo delete owner/repo --yes
```

Flags:

- `--yes`: Confirm deletion without interactive prompt.

______________________________________________________________________

### 5. Deploy Keys, Autolinks & Templates (`deploy-key`, `autolink`, `license`, `gitignore`)

#### `gh repo deploy-key`

Manage read or write deploy keys for a repository.

```bash
# List deploy keys for a repository
gh repo deploy-key list

# Add a read-only deploy key with a title
gh repo deploy-key add ~/.ssh/id_rsa.pub --title "Production" --read-only

# Add a read/write deploy key
gh repo deploy-key add ~/.ssh/id_rsa.pub --title "CI Deployment" --allow-write

# Delete a deploy key by ID
gh repo deploy-key delete 12345 --yes
```

#### `gh repo autolink`

Manage external autolink references (e.g. JIRA, Zendesk).

```bash
# List autolinks
gh repo autolink list

# Add an external autolink reference
gh repo autolink add --key-prefix JIRA- --url-template https://jira.example.com/browse/<num>

# View autolink details
gh repo autolink view 12345

# Delete autolink
gh repo autolink delete 12345 --yes
```

#### `gh repo license` & `gh repo gitignore`

Explore available licenses and gitignore templates.

```bash
# List standard open-source licenses
gh repo license list

# View license text
gh repo license view MIT

# List available .gitignore templates
gh repo gitignore list

# View a .gitignore template
gh repo gitignore view Python
```

______________________________________________________________________

### 6. Repository Labels (`gh label`)

Manage issue and pull request labels on a repository.

```bash
# List repository labels
gh label list
gh label list --limit 50 --sort name --order asc

# Search for labels
gh label list --search "bug"

# Create a new label
gh label create bug --color "d73a4a" --description "Bug report"
gh label create enhancement --color "a2eeef" --description "Feature request"
gh label create documentation --color "0075ca" --description "Documentation"

# Edit an existing label
gh label edit bug --name defect --color "b60205" --description "Software defect"

# Clone all labels from a template repository
gh label clone owner/template-repo
gh label clone owner/template-repo --force

# Delete a label
gh label delete stale --yes
```

Flags:

- `-c, --color <hex>`: Color code (e.g. `d73a4a` or `#d73a4a`).
- `-d, --description <string>`: Label description.
- `-n, --name <string>`: New name when editing.
- `-f, --force`: Overwrite existing labels when cloning.
- `-l, --limit <int>`: Maximum number of labels to list (default: 30).
- `-s, --sort <created|name>`: Field to sort by (default: `created`).
- `-o, --order <asc|desc>`: Order to sort by: `asc` or `desc`.
- `--search <query>`: Search label names and descriptions.
- `--yes`: Confirm deletion without prompting.

______________________________________________________________________

## Troubleshooting & Common Pitfalls

1. **Default Repository Ambiguity**:
   - In directories with multiple git remotes (e.g. `origin` pointing to your fork, `upstream` pointing to upstream repo), `gh` commands may target the fork by default. Run `gh repo set-default --view` to check, and `gh repo set-default upstream` to configure operations against the central upstream repo.
2. **Changing Visibility Requires Confirmation Flag**:
   - When running `gh repo edit --visibility private`, `gh` will error unless `--accept-visibility-change-consequences` is explicitly passed.
3. **Toggling Boolean Settings Off**:
   - In `gh repo edit`, settings cannot be toggled off by omitting them. You must pass `--<flag>=false`, e.g. `--enable-issues=false` or `--enable-wiki=false`.
