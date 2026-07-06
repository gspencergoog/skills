---
name: commit-changes
description: Ensure the git repository is in a healthy state (linting, testing, formatting) and prepare a descriptive git commit. Use this when the user asks to commit changes or "finish up" a task and the project is in a git repository.
---

# Commit Changes Skill

This skill guides the process of ensuring code health and crafting a conventional commit message across various technology stacks.

## Workflow

Before committing, ensure all intended files are staged and that you understand the changes being committed.

1.  **Stage Changes**: Use the standard CLI command `git add <files>`.
2.  **Verify Integrity**: Perform language-specific checks. **Read the relevant reference file for detailed instructions**:
    - **Dart / Flutter**: [references/dart.md](references/dart.md)
    - **JS / TS / Angular**: [references/javascript.md](references/javascript.md)
    - **Python**: [references/python.md](references/python.md)
    - **Other**: Run standard format/lint/test commands for the language.
3.  **Review**:
    - Check diffs (`git diff --cached | cat`).
    - Review comments for clarity and "why" vs "what".
4.  **Confirm**: Get user approval for the plan/message. Use the ask_question tool to ask the user if they approve of the plan/message.
5.  **Commit**:
    - Use the standard CLI command `git commit -m "<message>"`.
    - Message Format: Follow [references/conventional_commits.md](references/conventional_commits.md).
