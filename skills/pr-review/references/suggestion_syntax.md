# GitHub Markdown Suggestion Syntax

GitHub pull request reviews allow reviewers to propose direct code changes using the ```` ```suggestion ```` fence syntax.

## Syntax Format

In any inline PR review comment attached to a modified diff line, enclose the suggested replacement in triple backticks with the `suggestion` language identifier:

````markdown
```suggestion
replacement line 1
replacement line 2
```
````

When GitHub renders this comment on the PR conversation or Files Changed tab, it displays an interactive diff widget with a button: **"Commit suggestion"** (or "Add suggestion to batch"). The PR author can apply the suggestion directly from the web UI with a single click.

---

## Rules for Valid Suggestions

1. **Exact Indentation**:
   The suggestion block replaces the entire line range `[line_start, line_end]` specified by the comment. Leading whitespace, tabs, and indentation in the replacement code must match the indentation context of the original lines.

2. **Full Line Replacements**:
   Suggestions replace complete lines, not inline tokens or substrings. Include all surrounding code on the affected lines.

3. **Multi-line Anchoring**:
   When proposing changes across multiple lines:
   - In GitHub's Review API, set `start_line` to the first line and `line` to the last line.
   - Both `start_line` and `line` must fall within the same diff hunk on `side: "RIGHT"`.
   - The contents of ```` ```suggestion ```` will replace the range from `start_line` through `line` inclusive.

4. **Diff Hunk Constraint**:
   GitHub strictly rejects suggestion blocks attached to lines that were not modified in the PR. Any suggestions targeting unchanged code outside diff hunks must be discussed as standard text in the top-level review body instead of as inline suggestions.
