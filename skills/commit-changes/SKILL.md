---
name: commit-changes
description: >-
  Commits changes to a git repository: checks that the repository is healthy by
  running the language's formatter, linter, and tests, reviews the staged diff,
  and commits with a conventional commit message that explains why the change
  was made. Use when the user asks to commit, "finish up", "wrap this up", or
  land the current work in a git repository. Don't use for the full pre-review
  pipeline with coverage and review loops (use ship-it) or for writing a pull
  request description (use write-pr-description).
---

# Commit Changes Skill

This skill guides the process of ensuring code health and committing changes with a conventional commit message across various technology stacks.

## Workflow

Before committing, stage all intended files and make sure you understand the changes being committed.

1. Stage the intended files with `git add <files>`.
2. Run language-specific format, lint, and test checks. Read the matching reference file for detailed instructions:
   - Dart / Flutter: [references/dart.md](references/dart.md)
   - JS / TS / Angular: [references/javascript.md](references/javascript.md)
   - Python: [references/python.md](references/python.md)
   - Other languages: run the standard formatter, linter, and test suite for the language.
3. Review the staged diff:
   - Check the diff summary first with `git diff --cached --stat` to gauge scope.
   - Inspect detailed diffs in groups or per file (`git diff --cached -- <file> | cat`) for large changesets instead of reading one raw diff.
   - Check that every staged file is reviewed and that code comments explain "why" rather than "what".
4. Commit the staged changes:
   - Format the commit message according to [references/conventional_commits.md](references/conventional_commits.md).
   - Run `git commit -m "<message>"` directly without asking the user to review or approve the staged changes or commit message first.
