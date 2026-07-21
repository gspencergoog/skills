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

1. **Fetch and Analyze PR Data**: Run the analyzer script with `env -u GITHUB_TOKEN` (to bypass dummy token injection) to fetch the PR description and unresolved comments in JSON format:
   ```bash
   env -u GITHUB_TOKEN python3 ~/.gemini/config/skills/analyze-github-pr/scripts/analyze_comments.py --json --dir <path-to-target-workspace-directory>
   ```

2. **Save to Scratch**: Save the resulting JSON output to a file named `pr_comments.json` in your conversation-specific scratch directory (`<appDataDir>/brain/<conversation-id>/scratch/pr_comments.json`).

3. **Interpret the Results**:
   - The JSON contains `prDescription` (the PR description) and `threads` (the unresolved comments).
   - Use `prDescription` to understand the PR context and implementation details.
   - Use `threads` to list outstanding review comments and suggestions.

## Bundled Resources

- **`scripts/analyze_comments.py`**: Queries GitHub GraphQL API for the PR description and comments (both line-level and file-level), checks them against the local codebase, and prints or outputs a structured JSON report.
