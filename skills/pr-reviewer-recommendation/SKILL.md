---
name: pr-reviewer-recommendation
description: >-
  Recommends primary and secondary reviewers for a GitHub pull request, branch,
  or commit range by combining historical PR reviews and inline review threads
  from packrat with base-branch git commits and diff-hunk blame line ownership.
  Falls back cleanly to git-only analysis when packrat lacks the repository. Use
  when asked who should review a change, to find or suggest reviewers for a PR
  or branch, or to identify the owners of the files a change touches.
---

# PR Reviewer Recommendation Skill

This skill ranks code review candidates for a GitHub pull request, branch, or commit range. It combines formal pull request reviews and inline review threads from [packrat](../packrat/SKILL.md) with base-branch commits and diff-hunk `git blame` ownership.

Primary reviewers have direct commits, blamed lines, or past reviews on the modified files. Secondary reviewers have review or commit history in the immediate parent directories.

> [!IMPORTANT]
> Present reviewer recommendations in a clear, factual, and concise tone that follows [write-prose](../write-prose/SKILL.md).

## Workflow

Follow these five steps to recommend and assign reviewers.

### 1. Run the Recommender Script

Run [scripts/recommend_reviewers.py](scripts/recommend_reviewers.py) against the target git checkout with `--dir <repo-dir>`. Pick the invocation that matches your target change:

```bash
# Analyze a specific GitHub pull request
python3 <skill-dir>/scripts/recommend_reviewers.py --dir <repo-dir> --pr 42 --repo octo-org/widgets

# Compare a local or remote branch against the default branch
python3 <skill-dir>/scripts/recommend_reviewers.py --dir <repo-dir> --branch feat/button-focus

# Compare an explicit commit or tag range
python3 <skill-dir>/scripts/recommend_reviewers.py --dir <repo-dir> --compare main...feat/button-focus

# Analyze the active branch in the target checkout
python3 <skill-dir>/scripts/recommend_reviewers.py --dir <repo-dir>
```

| Flag | Purpose |
| :--- | :--- |
| `--dir <path>` | Path to the target local git checkout (defaults to `.`). |
| `--pr <number>` | Pull request number to inspect via `gh` or offline `packrat` metadata. |
| `--branch <name>` | Branch name to compare against the upstream or origin default branch. |
| `--compare <range>` | Explicit git range such as `main...feat/button-focus` or `v1.2.0..HEAD`. |
| `--repo <owner/repo>` | Canonical GitHub repository slug such as `octo-org/widgets`. |
| `--exclude-author <val>` | Repeatable or comma-separated logins, emails, or full names to exclude from self-review. |
| `--packrat-db <path>` | Custom path to `repos.sqlite` (defaults to `~/.local/share/packrat/repos.sqlite`). |
| `--json` | Emit structured JSON output instead of the Markdown report. |

### 2. Review Packrat Coverage and Fallback Notes

The script opens `~/.local/share/packrat/repos.sqlite` automatically in read-only mode. It reconciles GitHub `@login` handles with git commit emails and names so each engineer counts once.

When `packrat` lacks the database or repository, the script runs git-only scoring and prints a `--repo`-scoped sync note:

```bash
packrat repo add octo-org/widgets --path /work/widgets && packrat sync --repo octo-org/widgets
```

When pull request history backfill is still incomplete in `packrat`, the report includes a warning with `packrat sync --repo octo-org/widgets`. Surface either note briefly so the user knows whether historical review signals contributed to the ranking.

### 3. Gather Optional PR Context

Use [analyze-github-pr](../analyze-github-pr/SKILL.md) when you need issue context or discussion details from the pull request itself. Use [sem-semantic-info](../sem-semantic-info/SKILL.md) when you want function-level or class-level impact across callers.

### 4. Present the Recommendation

Summarize Primary and Secondary candidates using a Markdown table with reviewer handles, scores, review counts, thread counts, commits, blame lines, and matched files. Unlinked git authors appear as `Name (git:email@widgets.example)` in the table.

Include the suggested `gh pr edit` command with `-R <owner/repo>` for GitHub-linked reviewers:

```bash
gh pr edit 42 --add-reviewer reviewer-pro,alice-dev,dir-reviewer -R octo-org/widgets
```

### 5. Confirm Before Assigning Reviewers

When analyzing a specific `--pr`, ask the user for confirmation before assigning reviewers on GitHub. Prompt via your interactive confirmation or question tool, or ask in chat and wait for their reply, with these options:

1. `"(Recommended) Yes, assign the recommended reviewers to the PR"`
2. `"No, I will assign them manually"`

Run `gh pr edit <pr-number> --add-reviewer <reviewers> -R <repo>` only after the user approves the first option.
