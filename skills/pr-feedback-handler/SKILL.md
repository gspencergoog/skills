---
name: pr-feedback-handler
description: Handles retrieving, analyzing, and addressing code review feedback on a GitHub Pull Request. Use this skill when the user requests addressing or fixing a PR.
---

# PR Feedback Handler Skill

This skill guides the process of retrieving, analyzing, implementing, and resolving PR review comments using an interactive browser-based dashboard.

> [!IMPORTANT]
> **WRITING GUIDELINES**
> When drafting replies, explanations, or any prose, refer to the [natural-writing](../natural-writing/SKILL.md) skill to ensure clarity, accuracy, and tone.

> [!IMPORTANT]
> **CRITICAL RULE: USER APPROVAL REQUIRED**
> Before performing any action that modifies the remote state (making changes public), you **MUST** get explicit user approval. This applies at any step in the process:
>
> 1. **Pushing Code**: Request approval before running `git push` or making remote updates to the PR branch.
> 2. **Replying to Threads**: Present your draft reply or clarifying question to the user and obtain their approval before posting it.
> 3. **Resolving Threads**: Request approval before marking any review thread as resolved on GitHub.

---

## Workflow

Follow these steps when tasked with addressing PR review feedback:

### Step 1: Analyze Comments and Launch Dashboard

To allow the user to interactively review proposed fixes, draft replies, and provide feedback, run the interactive dashboard.

1. **Fetch Comments**: Employ the [analyze-github-pr](../analyze-github-pr/SKILL.md) skill to fetch the unresolved PR comments and CI/CD check failures in JSON format:
   `env -u GITHUB_TOKEN python3 ~/.gemini/config/skills/analyze-github-pr/scripts/analyze_comments.py --json --dir <path-to-target-workspace-directory>`

2. **Propose Fixes and Draft Replies**: For each thread in the output:
   - Inspect the target file (at the specified line if given, or the overall file if it is a file-level comment).
   - Formulate a concrete plan to address the feedback. Add this plan as a `proposedFix` field (string) to the thread object in the JSON. Provide enough detail to understand the solution details (e.g., specific files/functions to modify, logic changes, and APIs to use) while keeping it concise, focused, and actionable.
   - Draft a succinct, professional reply describing what was done to fix the issue (following the `natural-writing` skill). Add this as a `draftReply` field (string) to the thread object. Avoid generic replies like "Fixed." unless it is a trivial change.

3. **Write Data File**: Save the enriched JSON report to a file named `pr_comments.json` in your conversation-specific scratch directory (`<appDataDir>/brain/<conversation-id>/scratch/pr_comments.json`).

4. **Launch Dashboard**: Start the standalone dashboard app as a background task, pointing it to the target workspace directory and the conversation's scratch directory:
   `python3 ~/.gemini/config/skills/pr-feedback-handler/scripts/launch_dashboard.py --project-dir <path-to-target-workspace-directory> --data-dir <conversation-scratch-directory>`
   Set a reasonable `WaitMsBeforeAsync` (e.g., `1000`) so the command runs in the background.

5. **Wait for Completion**: Stop calling tools and go idle. The launcher will automatically open the browser for the user and block until they either click "Save & Apply Plan" or "Abort". Once they do, the background task will complete, and you will receive a notification with the command's exit status.

---

### Step 2: Implement Approved Fixes

Once the background task completes, check the result:

1. **Verify Exit Status**:
   - If the task exited with status `0` (success), proceed to implement the fixes.
   - If the task exited with a non-zero status (e.g., `1` for Abort), stop and ask the user for further instructions.

2. **Read the Plan**: Read `feedback_state.json` from your conversation-specific scratch directory (`<appDataDir>/brain/<conversation-id>/scratch/feedback_state.json`).

3. **Execute Approved Fixes**: For each item in `decisions` where `approved: true` and `action: "accept"`:
   - Apply the suggestion or implement the fix in the target file (at the specified line if given, or in the file overall).
   - If `agentInstructions` is populated, prioritize those instructions over your original `proposedFix` when implementing.
   - If `action: "decline"` or `action: "clarify"`, skip code changes for that thread.

4. **Verify and Commit**: Delegate local verification and committing to the `commit-changes` skill. Refer to its instructions to format, test, and commit the changes locally.

5. **Request Approval to Push**: Show the commit details to the user and obtain explicit approval before pushing the changes to the remote branch.

---

### Step 3: Respond and Resolve on GitHub

Once the approved code changes are successfully pushed:

1. **Submit Reply Comments**: For each approved item in `decisions` where a reply comment is specified:
   - Call `scripts/reply_thread.py` with the thread ID and the draft reply body.
   - Ensure the reply content matches the user-approved text from `feedback_state.json`.
2. **Resolve Review Threads**: For each approved item where `resolve: true`:
   - Call `scripts/resolve_thread.py` with the thread ID to mark the thread resolved on GitHub.

---

## Bundled Resources

- **`scripts/reply_thread.py`**: Sends replies to a specific PR review thread ID.
- **`scripts/resolve_thread.py`**: Resolves a specific PR review thread ID.
- **`scripts/launch_dashboard.py`**: Standalone dashboard launcher that starts a local server and opens the browser, blocking until saved or aborted.
- **`assets/pr_feedback.html`**: HTML dashboard template used by the launcher.
