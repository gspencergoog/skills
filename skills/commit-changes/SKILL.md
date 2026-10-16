---
name: commit-changes
description: >-
  Prepares a git commit: checks that the repository is healthy by running the
  language's formatter, linter, and tests, reviews the staged diff, and writes a
  conventional commit message that explains why the change was made. Use when
  the user asks to commit, "finish up", "wrap this up", or land the current work
  in a git repository. Don't use for the full pre-review pipeline with coverage
  and review loops (use ship-it) or for writing a pull request description (use
  write-pr-description).
---

# Commit Changes Skill

This skill guides the process of ensuring code health and crafting a conventional commit message across various technology stacks.

## Workflow

Before committing, ensure all intended files are staged and that you understand the changes being committed.

1. **Stage Changes**: Use the standard CLI command `git add <files>`.
2. **Verify Integrity**: Perform language-specific checks. **Read the relevant reference file for detailed instructions**:
   - **Dart / Flutter**: [references/dart.md](references/dart.md)
   - **JS / TS / Angular**: [references/javascript.md](references/javascript.md)
   - **Python**: [references/python.md](references/python.md)
   - **Other**: Run standard format/lint/test commands for the language.
3. **Review**:
   - Check diff summary first using `git diff --cached --stat` to gauge scope.
   - Inspect detailed diffs in groups or per-file (`git diff --cached -- <file> | cat`) for large changesets rather than pulling full raw diffs all at once, but do look at all the diffs.
   - Review comments for clarity and "why" vs "what".
4. **Confirm**: Get user approval for the staged changes and commit message before running `git commit`:
   - When the `a2ui-pane` plugin is available (`render_a2ui` is reachable as a native or lazy MCP tool), read the `genui` skill and render an inline `confirm_gate` surface with the staged diff summary and proposed commit message.
   - When `a2ui-pane` is not installed, prompt the user via your host's interactive question tool (or present the commit message in chat and wait for their approval before continuing).
5. **Commit**:
   - Use the standard CLI command `git commit -m "<message>"`.
   - Message Format: Follow [references/conventional_commits.md](references/conventional_commits.md).
