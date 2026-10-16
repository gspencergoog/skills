---
name: analyze-github-pr
description: >-
  Fetches a GitHub pull request's description, reviews, review threads,
  comments, and CI diagnostics into a JSON file and summarizes what the PR says
  about itself. Use when the user wants to understand, summarize, or extract
  information from a PR and its discussion, or when another task needs the PR's
  context before acting on it. Don't use for reviewing or commenting on the PR
  (use pr-review) or for addressing feedback on the user's own PR (use
  pr-feedback-handler).
---

# Analyze GitHub PR Skill

This skill fetches and analyzes the description and review comments of a GitHub Pull Request. It is designed to capture high-quality information from the PR.

> [!IMPORTANT]
> **WRITING GUIDELINES**
> When drafting any PR-related analysis, documentation, or summaries, refer to the [write-prose](../write-prose/SKILL.md) skill to ensure clarity, accuracy, and tone.

## Workflow

When investigating a PR or needing information from a PR and its comments:

1. **Fetch and Save PR Data**: Run the analyzer script with `env -u GITHUB_TOKEN` (to bypass dummy token injection) to fetch the PR description, reviews, comments, and CI diagnostics into your conversation scratch directory:

   ```bash
   env -u GITHUB_TOKEN python3 scripts/analyze_comments.py --output <appDataDir>/brain/<conversation-id>/scratch/pr_comments.json --dir <path-to-target-workspace-directory>
   ```

   *Note*: To target a specific PR number or GitHub PR URL explicitly, pass `--pr <number|url>`:

   ```bash
   env -u GITHUB_TOKEN python3 scripts/analyze_comments.py --pr <pr-number-or-url> --output <appDataDir>/brain/<conversation-id>/scratch/pr_comments.json --dir <path-to-target-workspace-directory>
   ```

2. **Interpret the Results**:

   - The generated `pr_comments.json` contains:
     - `prDescription`: The PR description text.
     - `threads`: Outstanding inline review threads and suggestions.
     - `reviews`: Top-level submitted reviews (summary comments, approval/rejection state).
     - `comments`: General discussion comments on the PR timeline.
     - `syncStatus`: Diagnostics on local branch vs remote PR HEAD (`in_sync`, `behind_remote`, `ahead_of_remote`, `diverged`, `branch_mismatch`).
     - `checks`: Failed CI status checks with job-level logs and line-level check annotations.
     - `pendingChecks`: Active, queued, or in-progress CI status checks.

## Bundled Resources

- **`scripts/analyze_comments.py`**: Queries GitHub GraphQL API for PR description, comments (inline and timeline), and submitted reviews; inspects failed check logs and annotations; evaluates local git sync state; and prints or outputs a structured JSON report.
