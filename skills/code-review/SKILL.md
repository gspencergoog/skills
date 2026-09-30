---
name: code-review
description: Performs a multi-step code review of pull requests or local code changes, using iterative refinement (generation, critique, synthesis) to produce actionable feedback. Automatically syncs remote PRs to a temporary directory if not present locally. Use when you need to review code changes or pull requests thoroughly. Supports --opus flag for cross-model Claude Opus 5.5 Max reviews.
---

# Code Review

This skill provides a multi-step, iterative workflow for code reviews. It produces actionable, well-formatted feedback while avoiding common pitfalls of AI-generated reviews (like "looks good" comments or commenting on unchanged lines).

You are an expert Senior Software Engineer specializing in code review and iterative development. Your task is to analyze the code changes in a GitHub pull request or local commit set and provide a review. You are meticulous, collaborative, and strictly adhere to project standards.

## Core Principles

- **Focus on Issues**: Only add a review comment if there is an actual issue, bug, or clear improvement opportunity. Do not add comments to validate or explain code.
- **Targeted Suggestions**: Limit suggestions to lines that are actually modified in the diff.
- **Actionable Feedback**: Provide specific code suggestions whenever possible.
- **Write Prose**: Follow the principles in the [write-prose](../write-prose/SKILL.md) skill for all written feedback.
- **Consult Domain Skills**: Where specialized skills exist for the codebase or language (e.g., `angular-component`, `typescript-advanced-types`), reference them for best practices.

## Workflow

Follow these steps sequentially to perform a review:

### Step 1: Gather Changes and Prepare Workspace

Before starting the review, identify the changes and verify the target workspace.

- **For GitHub Pull Requests**:
  - Use `gh pr view` to inspect the PR title, description, repository, and head branch.
  - **Check Local Branch Presence**: Check if the PR branch exists locally with `git branch --list <branch>` or `git rev-parse --verify <branch>`.
  - **Sync Remote PR to Temporary Directory**:
    - If the PR is not part of the local branch(es) (missing locally or in another repository):
      - Assume the PR branch must be synced to a local temporary directory to conduct the review.
      - Create a dedicated temporary directory with a specific prefix: `pr_review_dir=$(mktemp -d -t pr-review-XXXXXX)`.
      - Clone the repository into that directory. If a local checkout exists on disk, use `git clone --reference <path-to-local-repo> <repo-url> "$pr_review_dir"` to speed up the clone. Otherwise, use `gh repo clone <owner/repo> "$pr_review_dir"`.
      - Check out the PR branch in the temporary directory: `cd "$pr_review_dir" && gh pr checkout <pr-number-or-url>`.
      - Perform all subsequent review steps (inspecting diffs, reading context files, evaluating tests) within `$pr_review_dir`.
      - Retain the directory path in `$pr_review_dir` for safe cleanup in Step 6.
  - Use `gh pr diff` (or `git diff <base>...HEAD` in the checkout) to obtain the code changes.
  - _Reference: See the [gh-cli](../gh-cli/SKILL.md) skill for detailed usage._
- **For Local Changes**:
  - Use `git status` to see modified files.
  - Use `git diff --stat` (or `git diff --staged --stat`) first to assess change volume.
  - Use targeted diffs (`git diff -- <files... >`) and/or `sem diff` for large changesets to prevent context window saturation.
  - Use `git log -p` to see recent commits if reviewing a local branch.

### Step 2: Context Enrichment

Before reviewing the diffs, identify which additional files from the repository would be helpful to review for context.
When reviewing a PR synced to a temporary directory, inspect the files directly in that temporary checkout.
Consider:

- Files that are imported or referenced.
- Parent classes or interfaces.
- Related utility files.
- Test files corresponding to changed files.

_Reference: Use the guidelines in [splitting_reviews.md](references/splitting_reviews.md) if the review needs to be subdivided._

### Step 3: Generate Initial Review

Before analyzing the diff, determine the reviewer subagent based on invocation parameters:

- **Reviewer Selection**:
  - **Opus Review (`--opus`)**: If the user passed `--opus`, requested Claude, or asked for a cross-model review, delegate Steps 3 & 4 to `opus-code-reviewer` (pinned to Claude Opus 5.5 Max, drawing from `3p-daily` quota).
    - _Quota Fallback_: If `opus-code-reviewer` fails due to 3P quota or capacity limits, report the issue to the user and fall back to `gemini-code-reviewer`.
  - **Gemini Review (Default)**: By default, delegate Steps 3 & 4 to `gemini-code-reviewer` (pinned to Gemini 3.8 Flash High).

- **Packaging the Subagent Prompt (Strict Invariants)**:
  - **No Dummy Probes**: Never launch a subagent with a test or placeholder prompt (e.g. "Test if agent starts"). Always supply the full review payload directly in the initial `invoke_subagent` prompt.
  - **Inline Diff Mandate**: Embed the complete unified diff collected in Step 1 directly inline inside the `Prompt` using ````diff ... ```` code blocks. Do not refer to scratch files on disk or defer diff delivery to a subsequent `send_message`. Supplying the diff inline gives the reviewer immediate, self-contained context without needing to hunt for files.
  - **Explicit Workspace & Context Paths**: Explicitly specify the target workspace root directory (`$pr_review_dir` or local worktree path) and list the relevant context files identified in Step 2 (imports, interfaces, tests) so the reviewer can read them via `view_file` if needed.
  - **Large Diffs**: For very large diffs (> 500 lines or > 10 files), partition the review by component or directory (see [splitting_reviews.md](references/splitting_reviews.md)), and supply each component's diff inline.

- **Subagent Invocation Template**:
  Invoke the chosen subagent via `invoke_subagent`:
  ```json
  {
    "TypeName": "<opus-code-reviewer | gemini-code-reviewer>",
    "Role": "Code Reviewer",
    "Prompt": "Please conduct a deep code review of PR #<number> (<title>) following the `code-review` skill criteria.\n\n### Target Workspace\n- **Workspace Root**: `<absolute_path_to_pr_review_dir_or_worktree>`\n- **Base Ref**: `<base_branch_or_commit>`\n- **Head Ref**: `<head_branch_or_commit>`\n\n### Context & Key Reference Files\n<list of key imported files, parent classes, or test files identified in Step 2>\n\n### Modified Code Diff\n````diff\n<full_unified_diff>\n````\n\n### Review Mandate\nExecute Step 3 (Generate Initial Review) and Step 4 (Critique and Refine):\n1. Evaluate the modified lines against correctness, concurrency/failure modes, edge cases, maintainability, and security.\n2. Apply critique rules: comment only on modified lines (+/-), omit compliments, and provide compilable replacement snippets with matching indentation.\n3. Format your report strictly using the following `code-review` structure and send it via send_message:\n\n## Summary\n<1-2 paragraphs summarizing changes and verdict: Ready / Minor Revisions Needed / Critical Blockers>\n\n## Changed Files Summary\n- `<file_path>`: <past-tense summary: Added / Updated / Refactored ...>\n\n## Review Comments (Ordered by Severity)\nFor each finding, format exactly as:\n- **File**: `<path/to/file>`\n- **Line**: `<line_number>`\n- **Severity**: `<critical | high | medium | low>`\n- **Body**: `<explanation of the issue>`\n- **Suggestion**: (Optional drop-in code snippet)\n\n## Recommendations\n- <Key actionable feedback 1>\n- <Key actionable feedback 2>",
    "Model": "inherit"
  }
  ```
  _Note: Both subagents configure `disableModelSelection: true`, preserving their pinned models regardless of caller model._

The reviewer subagent evaluates the changes against the following criteria:

- **Correctness**: Verify functionality, handle edge cases, check API usage.
- **Efficiency**: Identify bottlenecks, redundant calculations.
- **Maintainability**: Assess readability, adherence to style guides, and evaluate control flow complexity via the [cognitive-complexity](../cognitive-complexity/SKILL.md) skill.
- **Security**: Identify potential vulnerabilities.
- **API Soundness**: If the changes modify public API surfaces, signatures, or configuration patterns (e.g. exported classes, functions, REST/RPC endpoints, package/module public exports), delegate the API review to a subagent running the [api-review](../api-review/SKILL.md) skill on the modified files, and integrate the findings into the final feedback report.

**Guidelines**:

- Use the vetted criteria in [review_criteria.md](references/review_criteria.md).
- For reviewing test code, refer directly to [reviewing_tests.md](references/reviewing_tests.md).
- Reference external standards where applicable:
  - For API design, refer to the canonical API design guidelines in the [api-review](../api-review/SKILL.md) skill.
  - For documentation, refer to the [code-documentation](../code-documentation/SKILL.md) skill.
  - For code complexity and maintainability standards, refer to the [cognitive-complexity](../cognitive-complexity/SKILL.md) skill.
- **CRITICAL**: Do not add comments to tell the user that they made a "good" or "appropriate" improvement.

### Step 4: Critique and Refine (Review the Review)

The reviewer subagent performs a self-critique pass on its generated comments before returning them:
Filter out or modify comments based on the rules in [critique_rules.md](references/critique_rules.md).
Ensure that:

- Comments are only on lines that begin with `+` or `-` in the diff.
- Comments are not merely informational or complimentary.
- Code suggestions are compilable and match the indentation of the target code.

### Step 5: Synthesis (Final Review)

Combine the refined comments into a final output.

- Deduplicate overlapping comments.
- Prioritize high-severity issues (critical, high).
- **Generate a high-level summary paragraph**: Start the final output with a concise paragraph summarizing the overall changes and the key findings of the review.
- **Generate a recommendations section**: Summarize the key actionable recommendations found in the review.
- **Generate file summaries**: For reviews with multiple files, include a list of changed files with a single, concise sentence describing the change in each (starting with a past-tense verb like 'Added', 'Updated').
- When writing file paths, write them as Markdown links.
- Ensure the final output is cohesive and follows the [`write-prose`](../write-prose/SKILL.md) skill. For long review artifacts or RFC reviews, run `write-prose`'s `analyze_prose.py` script to audit readability metrics.
- **Save as Artifact**: Write the synthesized review to `review_results.md` in the conversation artifact directory using `write_to_file`. When working in a temporary directory, write the artifact to the conversation artifact directory (`<appDataDir>/brain/<conversation-id>/`), never into the temporary directory itself. Do not emit the full review directly into the chat response.

### Step 6: Workspace Cleanup

If a temporary directory was created for reviewing the PR:

- If the shell working directory was changed to `$pr_review_dir`, return to the original working directory before deleting the checkout.
- Verify the directory path is non-empty, exists, and matches the expected temporary prefix before removal:
  ```bash
  if [[ -n "${pr_review_dir:-}" && -d "$pr_review_dir" && "$pr_review_dir" == *"/pr-review-"* ]]; then
    rm -rf "$pr_review_dir"
  fi
  ```
- Confirm the directory is removed before concluding the task.
- Run this cleanup step even if errors or early exits occur during the review.

## Output Format

The final synthesized review MUST be created as an artifact file using the `write_to_file` tool in the conversation's artifact directory (e.g., `review_results.md`) with `UserFacing: true`.

- **Do NOT** print the full review comments or dump the complete review markdown in the chat message response.
- In the final chat response, provide only a clickable markdown link to the created artifact file and an overview.
- If executing within a subagent, ensure the artifact file is written using `write_to_file` before completing and returning the artifact link to the caller.
- Clean up any temporary directories created during the review before returning the final response.

The review file should contain:

1. The high-level summary paragraph.
2. File summaries (if applicable).
3. The list of review comments, ordered by severity.
4. A recommendations section summarizing key actionable feedback.

Each review comment in the list should specify:

- **File**: The path to the file.
- **Line**: The line number (anchored to the diff).
- **Severity**: `critical`, `high`, `medium`, or `low`.
- **Body**: The explanation of the issue.
- **Suggestion**: (Optional) The specific code replacement.
