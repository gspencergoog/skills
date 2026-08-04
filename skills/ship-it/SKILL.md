---
name: ship-it
description: Codifies the end-to-end code preparation, quality audit, and PR shipping workflow. Use when preparing code to ship out for review, including: (1) PR segmentation & stacking analysis, (2) Unit test creation, test quality auditing, and 90%+ coverage enforcement, (3) Iterative subagent API and code review homeostasis loops, (4) Code documentation and README synchronization, and (5) Post-PR draft creation, CI tracking, and feedback handling.
---

# Ship-It: End-to-End Code Preparation & PR Shipping Workflow

This skill guides agents through a rigorous, multi-step checklist before code is shipped out for peer review. It ensures high quality, high test coverage, clean public APIs, well-structured PR segmentation, and post-PR creation draft tracking.

---

## Procedural Workflow Summary

Execute these 5 phases sequentially:

1. **Phase 1: PR Segmentation & Stacking Analysis** (Pre-flight git status check, diff scope analysis, and stacked worktree proposals)
2. **Phase 2: Per-Branch Static Analysis & Test Audit** (Linter/analyzer gate, 90%+ coverage enforcement, and test quality audit)
3. **Phase 3: Per-Branch Review Homeostasis Loop** (Iterative subagent API & code reviews until clean)
4. **Phase 4: Per-Branch Documentation Sync & PR Description** (Update code documentation, READMEs, and generate PR description)
5. **Phase 5: Post-PR Creation Draft Tracking & Feedback Handling** (Prompt user to submit Draft PR, schedule CI/comment checks, handle feedback, and mark ready for review)

---

## Phase 1: PR Segmentation & Stacking Analysis

1. **Pre-flight Working Tree Check**:
   - Run `git status --porcelain` to inspect untracked files and uncommitted working tree changes.
   - **If uncommitted or untracked changes are found**: Explicitly prompt the user via `ask_question` asking what to do with them (e.g. stage and commit them into the current branch, stash them, or include them in the diff scope calculation).
2. **Evaluate Diff Scope**:
   - Inspect git branch diff against base branch (e.g. `git diff main...HEAD`).
   - Evaluate if the change should remain a single PR or be split into stacked PRs:
     - Total diff > 500 lines of code (excluding lockfiles/generated files)?
     - Contains distinct logical layers (e.g. refactoring vs. new feature vs. API extension)?
     - Touches unrelated modules requiring different domain reviewers?
3. **If segmentation is needed**:
   - Refer to [pr_segmentation.md](references/pr_segmentation.md) for criteria and stacking plan rules.
   - Generate a **PR Stacking Plan Proposal** artifact (`pr_stacking_plan.md`) outlining stacked branches, dedicated `git worktree` locations, and commands.
   - Present the proposed stacking plan to the user using the `grill-me` skill for alignment before executing worktree creation.
4. **If single PR is sufficient**: Proceed directly to Phase 2 on the current branch.

---

## Phase 2: Per-Branch Static Analysis & Test Audit

Execute on each branch/worktree in the stack:

1. **Project & Language Auto-Detection**:
   - Identify project type via manifest files (`pubspec.yaml` -> Dart/Flutter, `pyproject.toml`/`setup.py` -> Python, `package.json` -> Node/TS, `Cargo.toml` -> Rust, `go.mod` -> Go).
2. **Pre-Test Linter & Analyzer Gate**:
   - Run static analyzer / linter before executing tests to catch syntax or type errors early:
     - For Dart/Flutter: Use Dart MCP `analyze_files` tool.
     - For Python: Run `ruff check` / `flake8` / `mypy`.
     - For TypeScript/Node: Run `tsc --noEmit` / `npm run lint`.
   - Fix all analyzer errors before proceeding to test execution.
3. **Run Test Suite with Coverage**:
   - For Python: `pytest --cov`
   - For Dart/Flutter: Dart MCP `run_tests` or `flutter test --coverage`
   - For TypeScript/Node: `jest --coverage` / `npm test`
4. **Enforce 90%+ Coverage Target**:
   - Add missing unit tests for uncovered lines or branch conditions until all modified/new files achieve **>= 90% line and branch coverage**.
5. **Audit Test Quality**:
   - Refer to [test_quality_audit.md](references/test_quality_audit.md) for quality dimensions (hermaticity, false positives, assertion precision, timing hygiene).
   - Ensure every bug fix introduced during the conversation has a dedicated, failing-before-fix regression test.

---

## Phase 3: Per-Branch Review Homeostasis Loop

Execute iterative review passes on the current branch/worktree:

1. **API Review Pass**:
   - Launch an isolated subagent (`Workspace: "inherit"`) to perform a review following the `api-review` skill on modified public interfaces.
   - Implement recommended mitigations for findings across all severity levels (`HIGH`, `MEDIUM`, and objective `LOW`).
   - Commit clean state: `git commit -m "refactor(api): apply API review mitigations"`.
2. **Code Review Iterative Loop**:
   - Refer to [review_homeostasis.md](references/review_homeostasis.md) for subagent prompt templates, oscillation handling rules, and homeostasis criteria.
   - Launch a subagent (`Workspace: "inherit"`) following the `code-review` skill on the working directory diff.
   - Parse findings (`HIGH`, `MEDIUM`, and `LOW`).
   - Apply fixes, run unit tests, and commit changes (`git commit -m "fix(review): address review comments"`).
   - **Oscillation Detection**: If a finding touches lines previously modified in a prior review iteration, use `ask_question` to ask the user how to resolve the conflicting recommendations.
   - **Homeostasis Reached**: Stop looping when a review pass yields zero findings across all severity levels (or after max 5 iterations). **Terminate all active subagents using `manage_subagents` with Action `'kill_all'`** before proceeding to Phase 4 (Documentation Sync).

---

## Phase 4: Per-Branch Documentation Sync & PR Description

1. **Code-Level Documentation**:
   - Audit all new or modified public classes, functions, and parameters.
   - Write clear, concise inline documentation (docstrings / dartdocs / JSDoc / type hints) following the `code-documentation` skill guidelines.
2. **Project & README Documentation**:
   - Check if CLI flags, configuration schemas, installation procedures, or public API usage changed.
   - Update `README.md`, proposal documents, or package guides to reflect updated code behavior.
3. **Final Verification**:
   - Run the full test suite one final time to verify all tests pass.
   - Commit documentation updates (`git commit -m "docs: sync inline documentation and README"`).
4. **Summary & PR Description**:
   - Output a concise summary to the user outlining coverage stats, review loop iterations completed, and stacking structure.
   - Automatically run the `write-pr-description` skill to generate the PR title and description artifact for submission.

---

## Phase 5: Post-PR Creation Draft Tracking & Feedback Handling

Refer to [post_pr_creation.md](references/post_pr_creation.md) for detailed execution steps:

1. **Prompt User for Draft Submission Approval**: Present the generated PR description and prompt the user via `ask_question` for explicit permission to create the Draft PR on GitHub.
2. **Submit Draft PR**: Upon approval, push branch and run `gh pr create --draft` (following `gh-cli` skill).
3. **5-Minute CI & Feedback Schedule**: Set a 5-minute schedule timer (`schedule` tool) to monitor CI status (`gh pr checks`) and review comments (`analyze-github-pr` script).
4. **Feedback Resolution Selection**: If CI fails or review comments exist, prompt the user via `ask_question`:
   - **Interactive Dashboard**: Delegate to `pr-feedback-handler` skill (`launch_dashboard.py`).
   - **Direct Fix & Respond**: Fix findings directly, verify with unit tests, commit, push, and submit thread replies via `pr-feedback-handler` scripts (`update_thread.py`).
5. **Mark Ready for Review**: Once CI passes and feedback is clean, prompt the user via `ask_question` and run `gh pr ready` to publish out of draft state.
