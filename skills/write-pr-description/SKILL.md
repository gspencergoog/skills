---
name: write-pr-description
description: Creates a description of the current branch's changes in the form of a pull request description. Use this when the user asks to write, generate, draft, or create a pull request (PR) description.
---

# Write PR Description Skill

Analyze the differences between the current branch and the main branch and construct a well-written pull request description. Write the description to your temporary scratch area as a Markdown file.

> [!IMPORTANT] **WRITING GUIDELINES** Before writing prose like PR descriptions,
> refer to the [natural-writing](../natural-writing/SKILL.md) skill to ensure
> clarity, accuracy, and a natural, non-hyperbolic tone.

> [!IMPORTANT] **GITHUB CLI AUTHENTICATION** In order to run the `gh` command
> line tool, you have to run it with `env -u GITHUB_TOKEN` to unset the dummy
> environment variable (e.g., `env -u GITHUB_TOKEN gh pr create`).

## Workflow

1. **Analyze Changes**:
   - Evaluate the differences between the current branch and the main branch using `sem diff` (via the [sem-semantic-diff](../sem-semantic-diff/SKILL.md) skill) or the git MCP `git_log_or_diff` tool.
   - If the `git diff` is empty or unclear, do not make up features. Only describe what is present in the provided code.

2. **Construct Description**:
   - Draft a well-written pull request description following the guidelines and mandatory structure below.
   - Write the description to your temporary scratch area as a Markdown file.
   - If the PR already exists, update the PR body using the GitHub CLI command:
     `env -u GITHUB_TOKEN gh pr edit --body-file <file>`

## PR Title (Squash Commit Message)

The title should follow the same rules as for the [conventional-commits](../commit-changes/references/conventional_commits.md) skill. PR descriptions are different from conventional commit descriptions, however.

## PR Description

- **Focus:** Prioritize the **"Why"** (business/logic intent) over the **"How"** (line-by-line changes).
- **Formatting:** Use Markdown backticks for variable names, file paths, and CLI commands. Use bullet points for readability.
- **Accuracy:** If the `git diff` is empty or unclear, do not make up features. Only describe what is present in the provided code.

## Mandatory Structure

1. **## Summary**: A 1-2 sentence high-level overview of the goal of this PR.
2. **## Changes**: A bulleted list of specific logic changes, refactors, or new files.
3. **## Impact & Risks**: Highlight any breaking changes, database migrations, or performance side effects.
4. **## Testing**: Provide a step-by-step list for the reviewer to verify the changes, and what testing related changes were made in the PR.

## Constraints

- Do not include conversational filler (e.g., "Here is the PR description...").
- Include references to issues that the PR addresses or resolves.
   - If the issue is completely fixed, write "Resolves <issue_number>", which will close the issue when the PR is submitted.
   - If the issue is only partially resolved, use "Addresses <issue_number>" instead.
- Don't describe the changes in hyperbolic language (e.g. "..is a fantastic improvement that the world will rejoice!") or use marketing terms. This is meant to be a factual description of the changes.
