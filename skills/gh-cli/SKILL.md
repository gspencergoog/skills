---
name: gh-cli
description: GitHub CLI (gh) reference for repositories, issues, pull requests, Actions, stacks, authentication, and CLI configuration.
---

# GitHub CLI (`gh`)

Work with GitHub from the command line across repositories, issues, pull requests, stacked workflows, and GitHub Actions.

**Version:** 2.100.0 (current as of September 2026)

> [!IMPORTANT]
> In environments that define a dummy or read-only `GITHUB_TOKEN` environment variable, run `gh` with `env -u GITHUB_TOKEN` to allow the CLI to use your authenticated user credentials (e.g. `env -u GITHUB_TOKEN gh auth status`).

______________________________________________________________________

## Command & Domain References

To avoid unnecessary context consumption, consult the dedicated reference guide for the specific domain you need:

| Domain                    | Reference Guide                                                | When to Read                                                                                                                                    |
| ------------------------- | -------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| **Pull Requests**         | [references/prs.md](references/prs.md)                         | Creating, viewing, diffing, reviewing, approving, checking out, and merging pull requests (`gh pr`).                                            |
| **Stacked Pull Requests** | [references/stacks.md](references/stacks.md)                   | Initializing, modifying, syncing, and navigating stacked PR chains (`gh stack`), and resolving worktree collisions.                             |
| **Issues & Discussions**  | [references/issues.md](references/issues.md)                   | Creating, filtering, closing, assigning, viewing comments, and branching from issues (`gh issue develop`).                                      |
| **Repositories & Labels** | [references/repos.md](references/repos.md)                     | Creating, cloning, forking, syncing, archiving repositories, and managing repository labels (`gh repo`, `gh label`).                            |
| **GitHub Actions**        | [references/actions.md](references/actions.md)                 | Viewing workflow run statuses, watching runs, rerunning jobs, downloading artifacts, and managing caches (`gh run`, `gh workflow`, `gh cache`). |
| **Auth, Config & API**    | [references/auth-and-config.md](references/auth-and-config.md) | Logging in/out, switching accounts, scopes, environment variables, browser navigation (`gh browse`), aliases, and raw API queries (`gh api`).   |

______________________________________________________________________

## Quickstart

### Verify Authentication

```bash
# Check logged-in accounts and active host status
env -u GITHUB_TOKEN gh auth status

# Log in interactively (if unauthenticated)
env -u GITHUB_TOKEN gh auth login
```

______________________________________________________________________

## Global Flags

These flags are available across `gh` commands:

| Flag                           | Description                                                       |
| ------------------------------ | ----------------------------------------------------------------- |
| `-R, --repo [HOST/]OWNER/REPO` | Target a specific repository instead of inferring from git remote |
| `--json <fields>`              | Output JSON containing specified fields (comma-separated)         |
| `-q, --jq <expression>`        | Filter and format JSON output using jq expression syntax          |
| `-t, --template <string>`      | Format JSON output using Go text/template syntax                  |
| `-w, --web`                    | Open the target item (PR, issue, repo) in the default web browser |
| `--paginate`                   | Fetch all pages when querying paginated endpoints                 |
| `--help` / `-h`                | Display command help and available flags                          |
| `--version`                    | Display the installed version of `gh`                             |

______________________________________________________________________

## Output Formatting & Scripting

### JSON Output & jq Filtering

Use `--json` to select desired fields and `--jq` to filter results without external dependencies:

```bash
# Extract scalar fields
gh repo view --json name,description

# Join fields with custom string formatting
gh repo view --json owner,name --jq '.owner.login + "/" + .name'

# Filter list output
gh pr list --json number,title --jq '.[] | select(.number > 100)'

# Map list objects into a custom JSON structure
gh issue list --json number,title,labels \
  --jq '.[] | {number, title: .title, tags: [.labels[].name]}'

# Extract list of IDs for piping into xargs
gh issue list --search "label:stale" --json number --jq '.[].number' | \
  xargs -I {} gh issue close {} --comment "Closing as stale"
```

### Go Template Output

```bash
# Single-line template
gh repo view --template '{{.name}}: {{.description}}'

# Multi-line formatted template
gh pr view 123 --template 'Title: {{.title}}
Author: {{.author.login}}
State: {{.state}}
'
```

______________________________________________________________________

## Getting Help

```bash
# Command-specific help
gh <command> --help
gh pr checkout --help
gh issue create --help

# Built-in help topics
gh help formatting
gh help environment
gh help exit-codes
gh help accessibility
```

______________________________________________________________________

## External References

- Official CLI Manual: https://cli.github.com/manual/
- GitHub CLI Documentation: https://docs.github.com/en/github-cli
- REST API Reference: https://docs.github.com/en/rest
- GraphQL API Reference: https://docs.github.com/en/graphql
