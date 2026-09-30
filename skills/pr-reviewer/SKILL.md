---
name: pr-reviewer
description: "Conducts interactive, high-signal code reviews on external GitHub Pull Requests by inspecting diffs in an isolated temporary clone, launching a local review dashboard with GitHub suggestion block editing, and submitting atomic batched reviews with hybrid out-of-diff routing."
---

# PR Reviewer Skill

> [!CAUTION]
> **READ-ONLY WORKSPACE & USER APPROVAL ENFORCEMENT**
> - You MUST NOT modify repository code in the primary workspace.
> - When reviewing external pull requests, perform all checkout and diff operations in an ephemeral temporary directory (`/tmp/pr-review-XXXXXX`).
> - You MUST launch the review dashboard and wait for the user to submit or abort before any reviews or comments are posted to GitHub.
> - Never promise future work or unverified assumptions in review comments.

This skill automates the workflow for reviewing external GitHub Pull Requests (reviewing someone else's code), giving the human reviewer complete interactive control via a local web dashboard before posting feedback to GitHub.

---

## Workflow

Follow these steps sequentially when tasked with reviewing a pull request:

### Step 1: Ingestion & Analysis Depth Selection

1. **Identify the PR**:
   - Determine target PR from user prompt (URL, PR number, or head branch).
   - If no PR is specified, attempt `gh pr view --json number,url` in the active workspace.
2. **Select Analysis Depth**:
   - If the user did not specify `--mode deep` or `--mode fast`, use `ask_question` to prompt:
     - **Deep Review (Recommended)**: Multi-perspective analysis covering correctness, security, API soundness, maintainability, and tests with a self-critique pass.
     - **Fast Review**: Rapid single-pass scan directly on the diff for quick turnaround.

---

### Step 2: Ephemeral Workspace Setup & Diff Hunk Mapping

1. **Resolve PR Metadata**:
   Run `review_engine.py` to fetch PR title, author, base/head branches, and head commit SHA:
   ```bash
   python3 scripts/review_engine.py --pr <pr-number-or-url> --output-meta <conversation-scratch-dir>/pr_meta.json
   ```
2. **Isolate Review Workspace**:
   - If the target PR belongs to a different repository or branch, create a dedicated temporary directory (`/tmp/pr-review-XXXXXX`).
   - If a local clone of the repository exists on disk, pass `--reference <path-to-local-repo>` to accelerate cloning:
     ```bash
     git clone --reference <path-to-local-repo> https://github.com/<owner>/<repo>.git "$pr_review_dir"
     cd "$pr_review_dir" && gh pr checkout <pr-number>
     ```
3. **Map Diff Hunks**:
   - Extract the unified diff: `git diff -U0 origin/<base_ref>...HEAD > <conversation-scratch-dir>/pr.diff`.
   - Parse valid diff hunks using `diff_parser.py`:
     ```bash
     python3 scripts/diff_parser.py --diff <conversation-scratch-dir>/pr.diff
     ```

---

### Step 3: Review Analysis & Hunk Tagging

1. **Analyze Changes against Review Guidelines**:
   - Review code modifications following [review_guidelines.md](references/review_guidelines.md).
   - Inspect related context files in the checkout (imported files, interface definitions, test files).
   - Verify that all comments are actionable and free of "looks good" filler.
2. **Draft Concrete Code Suggestions**:
   - For proposed line replacements, formulate GitHub suggestion blocks following [suggestion_syntax.md](references/suggestion_syntax.md).
3. **Structure & Tag Findings**:
   - Write candidate findings to `<conversation-scratch-dir>/raw_findings.json`.
   - Run `diff_parser.py` to validate diff hunks and tag findings:
     ```bash
     python3 scripts/diff_parser.py \
       --diff <conversation-scratch-dir>/pr.diff \
       --findings <conversation-scratch-dir>/raw_findings.json \
       --repo-dir "$pr_review_dir" \
       --output <conversation-scratch-dir>/review_findings.json
     ```
   - Any comments on lines inside diff hunks are marked `is_diff_hunk: true`.
   - Any comments on unchanged lines outside diff hunks are marked `is_diff_hunk: false`.

---

### Step 4: Launch Interactive Review Dashboard

1. **Start Dashboard Server as a Background Task**:
   Launch `launch_dashboard.py` pointing to the conversation scratch directory:
   ```bash
   python3 scripts/launch_dashboard.py \
     --findings-file <conversation-scratch-dir>/review_findings.json \
     --output-file <conversation-scratch-dir>/review_decisions.json
   ```
   Set `WaitMsBeforeAsync` to `1000` so the server runs in the background.
2. **Wait for Completion**:
   Stop calling tools and go idle. The launcher will open the review UI in the user's browser and block until they click **"Submit Review to GitHub"** or **"Abort Review"**.
3. **Verify Exit Status**:
   - If the task exited with status `0` (saved), proceed to Step 5.
   - If the task exited with status `1` (aborted), clean up the temporary workspace and stop without contacting GitHub.

---

### Step 5: Batch Review Submission & Workspace Cleanup

1. **Submit Batched Review**:
   Run `apply_review.py` to submit all approved decisions atomically via GitHub API:
   ```bash
   python3 scripts/apply_review.py \
     --decisions-file <conversation-scratch-dir>/review_decisions.json
   ```
   - Inline comments (`is_diff_hunk: true`) are posted directly on their respective lines.
   - Broader context comments (`is_diff_hunk: false`) are appended under `### Broader Context & Non-Diff Observations` in the top-level review body.
2. **Clean Up Workspace**:
   Safely remove the temporary checkout directory:
   ```bash
   python3 scripts/review_engine.py --cleanup "$pr_review_dir"
   ```
3. **Report to User**:
   Provide the user with a summary card containing the review verdict, approved comments count, and the URL to the submitted GitHub review.

---

## Bundled Resources

- **[review_guidelines.md](references/review_guidelines.md)**: Rules for actionable, non-defensive code review feedback.
- **[suggestion_syntax.md](references/suggestion_syntax.md)**: GitHub markdown suggestion fence syntax and line range rules.
- **`scripts/diff_parser.py`**: Unified diff parser and diff hunk line validator.
- **`scripts/review_engine.py`**: PR metadata resolver, ephemeral workspace manager, and diff extractor.
- **`scripts/launch_dashboard.py`**: Local HTTP server for the interactive review dashboard.
- **`assets/review_dashboard.html`**: Interactive dark-themed single-page review dashboard.
- **`scripts/apply_review.py`**: Submits batched review payloads to GitHub Reviews API with hybrid comment routing.
