---
name: pr-feedback-handler
description: Interactively handles GitHub PR review feedback by launching a review dashboard or generating a triage report before applying any workspace changes. Use when tasked with addressing PR comments.
---

# PR Feedback Handler Skill

> [!CAUTION]
> **MANDATORY DASHBOARD WORKFLOW & TOOL RESTRICTIONS**
> - You MUST NOT modify repository code files (`replace_file_content`, `write_to_file`, etc.) during the analysis phase.
> - During Phase 1, you may ONLY use read/view tools to inspect code and `write_to_file` to write your proposed fixes to `<scratch>/proposals.json`.
> - You MUST execute `launch_dashboard.py` (or generate an artifact report in headless/artifact mode) and wait for user approval before making any code modifications.

This skill guides the process of retrieving, analyzing, empirically verifying, implementing, and resolving PR review comments.

> [!IMPORTANT]
> **WRITING GUIDELINES**
> When drafting replies, explanations, or any prose, refer to the [write-prose](../write-prose/SKILL.md) skill to ensure clarity, accuracy, and tone.

> [!IMPORTANT]
> **CRITICAL RULE: USER APPROVAL REQUIRED**
> Before performing any action that modifies the remote state (making changes public), you **MUST** get explicit user approval:
> 1. **Pushing Code**: Request approval before running `git push` or making remote updates to the PR branch.
> 2. **Replying to Threads**: Present your draft reply or clarifying question to the user and obtain their approval before posting it.
> 3. **Resolving Threads**: Request approval before marking any review thread as resolved on GitHub.

---

## Workflow

Follow these steps when tasked with addressing PR review feedback:

> [!WARNING]
> **DO NOT IMPLEMENT CHANGES PREMATURELY**
> Do **NOT** modify any files in the workspace (source code, tests, etc.) during Phase 1. Only write proposed fixes and draft replies to `<scratch>/proposals.json` and launch the dashboard. Modifying files before the user approves them defeats the purpose of interactive reviews and causes git tree status issues on the dashboard.

---

### Step 0: Pre-Flight Workspace, Branch & CI Check

Before analyzing PR feedback or launching the dashboard:
1. **Verify Workspace State & Branch Sync**: Run `git status` in the target project directory. Verify active branch matches the PR HEAD branch and there are no uncommitted changes.
2. **Check for Active/Pending CI Runs**:
   - Inspect the `pendingChecks` list from `analyze_comments.py`.
   - If active checks are running, prompt the user via `ask_question` to determine whether to proceed immediately with triage or wait for CI completion.

---

### Step 1: Analyze Comments, Empirical Verification & Proposals

#### Phase 1: Analysis & Empirical Verification (Read-Only Workspace Access)

1. **Fetch and Save Comments**: Run `analyze_comments.py` with `--output` to save the full PR metadata report to `pr_comments.json` in your scratch directory. Always use `env -u GITHUB_TOKEN` to prevent environment token overrides:
   ```bash
   env -u GITHUB_TOKEN python3 ~/.gemini/config/skills/analyze-github-pr/scripts/analyze_comments.py --output <conversation-scratch-directory>/pr_comments.json --dir <path-to-target-workspace-directory>
   ```

2. **Empirical Verification Gate**:
   - **Do NOT blindly trust reviewer comments**: Automated bots and reviewers may propose changes based on incorrect assumptions, hallucinations, or obsolete code context.
   - For every comment/suggestion:
     - View the code context in the current local repository.
     - If the comment reports a bug or test failure, verify if the behavior actually reproduces.
     - Evaluate if the suggested modification could introduce regressions or break invariants.
   - **Categorize Each Feedback Item**:
     - `🔥 Urgent`: Critical bug, security issue, or broken test.
     - `👍 Solid`: Valid improvement, correct fix, or helpful refactor.
     - `🤷 Meh`: Minor stylistic nit or preference with neutral impact.
     - `👎 Disagree`: Factually incorrect, based on a hallucination, or introduces a bug.

3. **Formulate Proposed Fixes & Draft Replies**: For each unresolved thread in the report:
   - Formulate a concrete plan to address the feedback (`proposedFix`).
   - Draft a succinct, professional reply (following the `write-prose` skill) describing what was done or explaining why a suggestion was declined (`draftReply`).

4. **Write Proposals File**: Save your proposals mapping to `proposals.json` in your conversation scratch directory (`<appDataDir>/brain/<conversation-id>/scratch/proposals.json`):
   ```json
   {
     "<thread_id_1>": {
       "proposedFix": "Add null check before accessing property.",
       "draftReply": "Added null check to prevent NPE as suggested."
     },
     "<thread_id_2>": {
       "proposedFix": "Decline change; the existing loop invariant guarantees non-emptiness.",
       "draftReply": "The caller guarantees this collection is non-empty before entry, so extra guard is unnecessary."
     }
   }
   ```

#### Phase 2: Launch Dashboard & Interactive Review

5. **Launch Dashboard**: Start the standalone dashboard app as a background task, pointing it to the target workspace directory and conversation scratch directory:
   ```bash
   env -u GITHUB_TOKEN python3 ~/.gemini/config/skills/pr-feedback-handler/scripts/launch_dashboard.py --project-dir <path-to-target-workspace-directory> --data-dir <conversation-scratch-directory> --mode auto
   ```
   *Note*: In headless or remote cloud environments without browser display, you may specify `--mode artifact` to generate a markdown triage report artifact directly into `data-dir` (`pr_triage_report.md`).

6. **Wait for Completion**: Stop calling tools and go idle. The launcher will automatically merge `proposals.json` into the review UI, open the browser for the user (when local), and block until they click "Save & Apply Plan" or "Abort". Once submitted, you will receive a notification with the command's exit status.

---

### Step 2: Implement Approved Fixes & Add Regression Tests

Once the dashboard review completes:

1. **Verify Exit Status**:
   - If the task exited with status `0` (success), proceed to implement the fixes.
   - If the task exited with a non-zero status (e.g., `1` for Abort), stop and ask the user for further instructions.

2. **Read the Plan**: Read `feedback_state.json` from your conversation-specific scratch directory (`<appDataDir>/brain/<conversation-id>/scratch/feedback_state.json`).

3. **Execute Approved Fixes**: For each item in `decisions` where `approved: true` and `action: "accept"`:
   - Apply the suggestion or implement the fix in the target file.
   - If `agentInstructions` is populated, prioritize those instructions over your original `proposedFix`.
   - If `action: "decline"` or `action: "clarify"`, skip code changes for that thread.

4. **Add Regression Tests**: When fixing any reviewer-reported bug or logic flaw, add targeted unit tests to verify the fix and prevent future regressions.

5. **Verify and Commit**: Delegate local verification and committing to the `commit-changes` skill. Format, lint, run tests, and commit the changes locally.

---

### Step 3: Respond, Resolve on GitHub & Completion Menu

Once the approved code changes are verified and committed:

1. **Interactive Next Steps Menu**: Use `ask_question` to ask the user how they would like to proceed:
   - **Option 1**: "(Recommended) Push changes and update/resolve review threads on GitHub."
   - **Option 2**: "Push changes to remote only (do not resolve threads yet)."
   - **Option 3**: "Keep changes local for manual review."

2. **Submit Replies and Resolve Threads in Bulk**:
   - If approved to resolve on GitHub, run the bulk thread updater:
     ```bash
     python3 ~/.gemini/config/skills/pr-feedback-handler/scripts/update_thread.py --file <conversation-scratch-directory>/feedback_state.json
     ```
   - If any thread updates fail, review the printed failure report, make adjustments, and re-run if needed.

---

## Bundled Resources

- **`scripts/update_thread.py`**: Bulk posts replies to and resolves approved PR review threads on GitHub.
- **`scripts/launch_dashboard.py`**: Standalone review dashboard launcher supporting local web mode, remote SSH/Cloud detection, and markdown artifact export.
- **`assets/pr_feedback.html`**: Interactive dark-themed web dashboard with tabs for inline comments, top-level reviews, conversation comments, CI failures (with check annotations), and active checks.

