---
name: analyze-github-pr
description: Fetches and produces a comprehensive analysis of a GitHub Pull Request, including its description and review comments. Use this skill when investigating a PR or when information is needed from a PR and its comments.
---

# Analyze GitHub PR Skill

This skill fetches and analyzes the description and review comments of a GitHub Pull Request. It is designed to capture high-quality information from the PR.

> [!IMPORTANT]
> **WRITING GUIDELINES**
> When drafting any PR-related analysis, documentation, or summaries, refer to the [write-prose](../write-prose/SKILL.md) skill to ensure clarity, accuracy, and tone.

## Workflow

When investigating a PR or needing information from a PR and its comments:

1. **Fetch and Save PR Data**: Run the analyzer script with `env -u GITHUB_TOKEN` (to bypass dummy token injection) to fetch the PR description and comments directly into your conversation scratch directory:
   ```bash
   env -u GITHUB_TOKEN python3 ~/.gemini/config/skills/analyze-github-pr/scripts/analyze_comments.py --output <appDataDir>/brain/<conversation-id>/scratch/pr_comments.json --dir <path-to-target-workspace-directory>
   ```

2. **Interpret the Results**:
   - The generated `pr_comments.json` contains `prDescription` (the PR description), `threads` (the unresolved comments), `headRefName`, `headRefOid`, and `checks`.
   - Use `prDescription` to understand the PR context and implementation details.
   - Use `threads` to list outstanding review comments and suggestions.

## Bundled Resources

- **`scripts/analyze_comments.py`**: Queries GitHub GraphQL API for the PR description and comments (both line-level and file-level), checks them against the local codebase, and prints or outputs a structured JSON report.
