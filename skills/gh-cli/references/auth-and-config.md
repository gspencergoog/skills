# GitHub CLI Authentication & Configuration Reference (`gh auth`, `gh config`)

Reference guide and troubleshooting for authentication, configuration, environment variables, web browsing, shell integration, and raw API access in GitHub CLI (`gh`).

______________________________________________________________________

## Table of Contents

- [Authentication (`gh auth`)](#authentication-gh-auth)
  - [gh auth login](#gh-auth-login)
  - [gh auth status](#gh-auth-status)
  - [gh auth switch](#gh-auth-switch)
  - [gh auth token](#gh-auth-token)
  - [gh auth refresh](#gh-auth-refresh)
  - [gh auth logout](#gh-auth-logout)
  - [gh auth setup-git](#gh-auth-setup-git)
- [Configuration (`gh config`)](#configuration-gh-config)
  - [Configuration Commands](#configuration-commands)
  - [Configurable Keys](#configurable-keys)
- [Environment Variables](#environment-variables)
- [Web Browser Navigation (`gh browse`)](#web-browser-navigation-gh-browse)
- [Shell Setup, Aliases & Git Credential Helper](#shell-setup-aliases--git-credential-helper)
- [Direct API Requests (`gh api`)](#direct-api-requests-gh-api)
- [Installation Quick Reference](#installation-quick-reference)
- [Troubleshooting & Common Pitfalls](#troubleshooting--common-pitfalls)

______________________________________________________________________

## Authentication (`gh auth`)

### `gh auth login`

Authenticate with a GitHub host.

```bash
# Interactive login (prompts for host, protocol, and auth method)
gh auth login

# Web-based browser login
gh auth login --web

# Web-based login copying one-time verification code to clipboard
gh auth login --web --clipboard

# Log in using a specific git protocol
gh auth login --git-protocol ssh
gh auth login --git-protocol https

# Log in to a GitHub Enterprise Server instance
gh auth login --hostname enterprise.internal

# Non-interactive login supplying a personal access token from standard input or file
gh auth login --with-token < token.txt
echo "$GH_TOKEN" | gh auth login --with-token

# Request additional initial permission scopes
gh auth login --scopes write:org,read:public_key,repo

# Store token in plain text rather than the system keychain (headless/container environments)
gh auth login --insecure-storage
```

Flags:

- `-h, --hostname <string>`: The hostname of the GitHub instance (default: `github.com`).
- `-p, --git-protocol <string>`: Preferred protocol for git operations: `ssh` or `https`.
- `-s, --scopes <list>`: Additional authentication scopes to request.
- `-w, --web`: Open browser to authenticate.
- `-c, --clipboard`: Copy one-time authentication code to clipboard (used with `--web`).
- `--with-token`: Read personal access token from standard input.
- `--insecure-storage`: Store authentication token in plaintext rather than credential manager/keychain.

### `gh auth status`

Display authentication status for all known hosts and accounts.

```bash
# Show authentication status for all accounts
gh auth status

# Check active account only
gh auth status --active

# Check status for a specific hostname
gh auth status --hostname github.com
gh auth status --hostname enterprise.internal

# Display the authentication token in the terminal output
gh auth status --show-token

# Output authentication metadata as JSON
gh auth status --json hosts

# Query host tokens or user details using jq
gh auth status --json hosts --jq '.hosts | keys[]'
```

Flags:

- `-a, --active`: Check only the active account.
- `-h, --hostname <string>`: Target a specific GitHub host.
- `-t, --show-token`: Display authentication token in terminal output.
- `--json <fields>`: Output JSON with specified fields (`hosts`).

### `gh auth switch`

Switch active account for a GitHub host.

```bash
# Interactive account switch prompt
gh auth switch

# Switch to a specific user on github.com
gh auth switch --hostname github.com --user monalisa

# Switch user on an Enterprise host
gh auth switch --hostname enterprise.internal --user octocat
```

Flags:

- `-h, --hostname <string>`: The GitHub host for the account.
- `-u, --user <string>`: The GitHub username to activate.

### `gh auth token`

Print the authentication token used for requests.

```bash
# Print active token for default host
gh auth token

# Print token for specific host
gh auth token --hostname github.com
gh auth token --hostname enterprise.internal

# Print token for a specific user on a host
gh auth token --hostname github.com --user monalisa
```

Flags:

- `-h, --hostname <string>`: The GitHub host.
- `-u, --user <string>`: The username.

### `gh auth refresh`

Expand or reset granted permission scopes on an existing account.

```bash
# Refresh existing credentials
gh auth refresh

# Add specific scopes
gh auth refresh --scopes write:org,read:public_key,workflow

# Remove specific scopes
gh auth refresh --remove-scopes delete_repo

# Reset granted scopes back to the default minimal set
gh auth refresh --reset-scopes

# Authenticate via browser with clipboard code
gh auth refresh --web --clipboard
```

Flags:

- `-h, --hostname <string>`: The GitHub host.
- `-u, --user <string>`: The account to refresh.
- `-s, --scopes <list>`: Additional scopes to add.
- `--remove-scopes <list>`: Scopes to revoke.
- `--reset-scopes`: Reset to the default set of scopes.
- `-w, --web`: Open browser for authentication.
- `-c, --clipboard`: Copy code to clipboard.

### `gh auth logout`

Log out of a GitHub host and remove credentials.

```bash
# Interactive logout
gh auth logout

# Logout from a specific host and user without prompt
gh auth logout --hostname github.com --user monalisa
```

Flags:

- `-h, --hostname <string>`: The GitHub host.
- `-u, --user <string>`: Account username.

### `gh auth setup-git`

Configure git to use GitHub CLI as a credential helper for HTTPS repository operations.

```bash
# Configure credential helper for default host
gh auth setup-git

# Configure credential helper for Enterprise host
gh auth setup-git --hostname enterprise.internal

# Force overwrite of existing git credential helper configuration
gh auth setup-git --hostname enterprise.internal --force
```

Flags:

- `-h, --hostname <string>`: The GitHub host.
- `-f, --force`: Force re-configuration of git credentials.

______________________________________________________________________

## Configuration (`gh config`)

### Configuration Commands

```bash
# List all active configuration options
gh config list

# Display value for a specific setting
gh config get editor
gh config get git_protocol

# Set configuration values
gh config set editor vim
gh config set git_protocol ssh
gh config set prompt disabled
gh config set pager "less -R"
gh config set browser open

# Clear cached HTTP responses and GraphQL schemas
gh config clear-cache
```

### Configurable Keys

| Key                | Values                                      | Description                                                     |
| ------------------ | ------------------------------------------- | --------------------------------------------------------------- |
| `git_protocol`     | `https`, `ssh`                              | Protocol used when cloning or pushing repositories              |
| `editor`           | string (e.g. `vim`, `nano`, `code -w`)      | Text editor used for composing commit messages, PRs, and issues |
| `prompt`           | `enabled`, `disabled`                       | Toggle interactive terminal prompts                             |
| `pager`            | string (e.g. `less -R`, `cat`)              | Terminal pager program for long output                          |
| `browser`          | string (e.g. `open`, `xdg-open`, `firefox`) | Web browser executable used by `gh browse` and `--web`          |
| `http_unix_socket` | path                                        | Path to Unix domain socket for HTTP proxying                    |

______________________________________________________________________

## Environment Variables

GitHub CLI respects environment variables for automation, CI/CD runners, and environment overrides:

| Variable                    | Description                                                                             |
| --------------------------- | --------------------------------------------------------------------------------------- |
| `GH_TOKEN` / `GITHUB_TOKEN` | Authentication token. Overrides tokens stored in the system keychain                    |
| `GH_HOST`                   | Target GitHub host (default: `github.com`)                                              |
| `GH_REPO`                   | Default target repository in `[HOST/]OWNER/REPO` format, overriding current git remote  |
| `GH_PROMPT_DISABLED`        | Set to `1` or `true` to disable all interactive terminal prompts                        |
| `GH_EDITOR`                 | Editor command used for authoring text, overriding `gh config get editor` and `$EDITOR` |
| `GH_PAGER`                  | Pager command used for output, overriding `gh config get pager` and `$PAGER`            |
| `GH_BROWSER`                | Browser executable to use, overriding `gh config get browser` and `$BROWSER`            |
| `GH_TIMEOUT`                | HTTP request timeout in seconds (default: 30)                                           |
| `GH_ENTERPRISE_HOSTNAME`    | Hostname for GitHub Enterprise Server instances                                         |
| `GH_NO_UPDATE_NOTIFIER`     | Set to `1` to suppress notices when a new version of `gh` is available                  |
| `DEBUG`                     | Set to `1` or `api` to output verbose HTTP and GraphQL debug traces to stderr           |

______________________________________________________________________

## Web Browser Navigation (`gh browse`)

Open repository pages, code, issues, and pull requests in the default web browser.

```bash
# Open repository root in browser
gh browse

# Open specific file or directory
gh browse src/index.ts
gh browse lib/

# Open specific line or line range in a file
gh browse src/app.ts:42
gh browse src/app.ts:42-55

# Open issue or pull request by number
gh browse 123

# Open commit by hash
gh browse 77507cd94ccafcf568f8560cfecde965fcfa63

# Open file on a specific branch or tag
gh browse src/main.go --branch bug-fix
gh browse README.md --branch v1.2.0

# Open specific repository pages
gh browse --actions       # GitHub Actions tab
gh browse --projects      # Projects tab
gh browse --releases      # Releases tab
gh browse --settings      # Repository settings
gh browse --wiki          # Wiki pages

# Target another repository
gh browse --repo owner/repo

# Print destination URL to stdout instead of opening browser
gh browse --no-browser
gh browse 123 --no-browser
```

Flags:

- `-b, --branch <string>`: Select branch or tag.
- `-n, --no-browser`: Output the generated URL rather than launching a browser.
- `-R, --repo [HOST/]OWNER/REPO`: Select repository.
- `--actions`: Open Actions tab.
- `--projects`: Open Projects tab.
- `--releases`: Open Releases tab.
- `--settings`: Open Settings tab.
- `--wiki`: Open Wiki tab.

______________________________________________________________________

## Shell Setup, Aliases & Git Credential Helper

### Shell Completion

```bash
# Bash
eval "$(gh completion -s bash)"

# Zsh
eval "$(gh completion -s zsh)"

# Fish
gh completion -s fish | source
```

### Command Aliases (`gh alias`)

Create convenient shortcuts for complex or frequently used `gh` invocations:

```bash
# Create aliases
gh alias set co "pr checkout"
gh alias set pv "pr view --web"
gh alias set bugs "issue list --label bug --state open"

# Create shell alias executing arbitrary shell commands (prefix with !)
gh alias set pr-clean '!gh pr list --state merged --json headRefName --jq ".[].headRefName" | xargs -n 1 git branch -d'

# List active aliases
gh alias list

# Delete an alias
gh alias delete co
```

### Git Credential Integration

Configure git to use `gh` for credential authentication over HTTPS:

```bash
# Automatic helper setup
gh auth setup-git

# Equivalent manual git configuration
git config --global credential.helper '!gh auth git-credential'
```

______________________________________________________________________

## Direct API Requests (`gh api`)

Make authenticated REST or GraphQL calls directly to the GitHub API, handling authentication, pagination, and caching automatically.

```bash
# REST GET request
gh api /user
gh api /repos/owner/repo/pulls

# REST POST request with string fields (-f) and raw JSON/boolean fields (-F)
gh api -X POST /repos/owner/repo/issues \
  -f title="Automated bug report" \
  -f body="Details on crash" \
  -F milestone=3

# Pagination across all pages
gh api --paginate /repos/owner/repo/issues --jq '.[].title'

# GraphQL request
gh api graphql -f query='
  query($owner: String!, $repo: String!) {
    repository(owner: $owner, name: $repo) {
      stargazerCount
      openIssues: issues(states: OPEN) { totalCount }
    }
  }' -F owner='cli' -F repo='cli'

# Custom request headers
gh api -H "Accept: application/vnd.github.v3.diff" /repos/owner/repo/pulls/123

# HTTP response caching
gh api /user --cache force      # Read from cache if available
gh api /user --cache bypass     # Bypass cache and refresh
```

Flags:

- `-X, --method <METHOD>`: HTTP method: `GET`, `POST`, `PUT`, `DELETE`, `PATCH` (default: `GET`).
- `-f, --field <key=value>`: Add string parameter.
- `-F, --raw-field <key=value>`: Add parameter with type conversion (boolean, number, null).
- `-H, --header <key:value>`: Add custom HTTP request header.
- `--paginate`: Fetch all pages of a paginated resource.
- `--cache <mode>`: Cache control: `default`, `force`, `bypass`.
- `--jq <expression>`: Filter response JSON using jq syntax.

______________________________________________________________________

## Installation Quick Reference

```bash
# macOS (Homebrew)
brew install gh

# Ubuntu / Debian
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
sudo apt update && sudo apt install gh

# Fedora / RHEL
sudo dnf install gh

# Windows (winget)
winget install --id GitHub.cli

# Verify version
gh --version
```

______________________________________________________________________

## Troubleshooting & Common Pitfalls

1. **Dummy Environment Variable Override**:
   - In environments that inject a dummy or read-only `GITHUB_TOKEN`, `gh` commands will prioritize it over logged-in credentials. Prepend `env -u GITHUB_TOKEN` to unset the variable for that execution:
     ```bash
     env -u GITHUB_TOKEN gh auth status
     ```
2. **Missing Token Scopes**:
   - If an operation returns HTTP 403 / 404 due to insufficient token permissions (e.g. modifying workflow files or organization teams), expand scopes without re-logging in:
     ```bash
     gh auth refresh --scopes repo,workflow,write:org
     ```
3. **Headless Execution & Interactive Prompts**:
   - If `gh` freezes waiting for input in background scripts or automated agent sessions, set `GH_PROMPT_DISABLED=1` or run `gh config set prompt disabled`.
4. **Git Credential Helper Conflicts**:
   - If git continues prompting for HTTPS credentials after `gh auth setup-git`, check for competing credential helpers in `git config --show-origin --get-all credential.helper`.
