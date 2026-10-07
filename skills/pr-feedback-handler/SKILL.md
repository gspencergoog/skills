---
name: pr-feedback-handler
description: Interactively handles GitHub PR review feedback by launching a review dashboard or generating a triage report before applying any workspace changes. Use when tasked with addressing PR comments.
---

# PR Feedback Handler Skill

> [!CAUTION]
> **MANDATORY REVIEW WORKFLOW & TOOL RESTRICTIONS**
>
> - You MUST NOT modify repository code files (`replace_file_content`, `write_to_file`, etc.) during the analysis phase.
> - During Phase 1, you may ONLY use read/view tools to inspect code and `scaffold_proposals.py` / `write_to_file` to write your proposed fixes to `<scratch>/proposals.json`.
> - In interactive mode (default), you MUST execute `launch_dashboard.py` (or generate an artifact report in artifact mode) and wait for user approval before making any code modifications.
> - In auto-approve mode (`--auto-approve` / `--auto`), you execute `launch_dashboard.py --auto-approve` to synthesize approved decisions directly, and the user's explicit invocation serves as pre-approval to implement, commit, push, and resolve review threads.

This skill guides the process of retrieving, analyzing, empirically verifying, implementing, and resolving PR review comments.

> [!IMPORTANT]
> **WRITING GUIDELINES**
> When drafting replies, explanations, or any prose, refer to the [write-prose](../write-prose/SKILL.md) skill to ensure clarity, accuracy, and tone.

> [!IMPORTANT]
> **USER APPROVAL & AUTO-APPROVE MODE**
>
> 1. **Interactive Mode (Default)**: Before modifying remote state (pushing code, replying to threads, or resolving threads), you **MUST** obtain explicit user approval. Present your draft replies and request confirmation before running `git push` or `update_thread.py`.
> 2. **Auto-Approve Mode (`--auto-approve` / `--auto`)**: When the user explicitly invokes `/pr-feedback-handler --auto-approve` (or `--auto`), the interactive dashboard is bypassed, proposals are automatically synthesized as approved decisions, fixes are committed, and the branch and resolved threads are pushed directly to GitHub.
> 3. **No Promising Future Work**: Never promise future work (e.g. follow-up PRs, future issues, later refactors) in review replies posted as the user. Reply strictly about what was actually implemented in the active PR.

______________________________________________________________________

## Workflow

Follow these steps when tasked with addressing PR review feedback:

> [!WARNING]
> **DO NOT IMPLEMENT CHANGES PREMATURELY**
> Do **NOT** modify any files in the workspace (source code, tests, etc.) during Phase 1. Only write proposed fixes and draft replies to `<scratch>/proposals.json` and launch the dashboard. Modifying files before the user approves them defeats the purpose of interactive reviews and causes git tree status issues on the dashboard.

______________________________________________________________________

### Step 0: Pre-Flight Workspace, Branch & CI Check

Before analyzing PR feedback or launching the dashboard:

1. **Verify Workspace State & Branch Sync**: Run `git status` in the target project directory. Verify active branch matches the PR HEAD branch and there are no uncommitted changes.
2. **Check for Active/Pending CI Runs**:
   - Inspect the `pendingChecks` list from `analyze_comments.py`.
   - If active checks are running, prompt the user via `ask_question` to determine whether to proceed immediately with triage or wait for CI completion.

______________________________________________________________________

### Step 1: Analyze Comments, Empirical Verification & Proposals

#### Phase 1: Analysis & Empirical Verification (Read-Only Workspace Access)

1. **Fetch and Save Comments**: Run `analyze_comments.py` with `--output` to save the full PR metadata report to `pr_comments.json` in your scratch directory. Always use `env -u GITHUB_TOKEN` to prevent environment token overrides:

   ```bash
   env -u GITHUB_TOKEN python3 ../analyze-github-pr/scripts/analyze_comments.py --output <conversation-scratch-directory>/pr_comments.json --dir <path-to-target-workspace-directory>
   ```

   *Note*: When working in a fork or cloned checkout where git remotes may default to the fork, pass `--pr <pr-number-or-url>` explicitly to query the upstream PR:

   ```bash
   env -u GITHUB_TOKEN python3 ../analyze-github-pr/scripts/analyze_comments.py --pr <pr-number-or-url> --output <conversation-scratch-directory>/pr_comments.json --dir <path-to-target-workspace-directory>
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

3. **Scaffold Proposals Template**: Run `scaffold_proposals.py` to generate a pre-populated `proposals.json` template with all unresolved thread IDs (GraphQL `PRRT_...` node IDs) and review snippets:

   ```bash
   python3 scripts/scaffold_proposals.py --data-dir <conversation-scratch-directory>
   ```

   The script prints a compact summary table of unresolved threads to `stdout` and writes `<conversation-scratch-directory>/proposals.json`.

4. **Populate Proposals File**: Edit `<conversation-scratch-directory>/proposals.json` to formulate a concrete technical fix (`proposedFix`) and draft reply (`draftReply`), adjusting `action` (`accept`, `decline`, `clarify`) or `assessment` (`urgent`, `solid`, `meh`, `disagree`) as appropriate:

   ```json
   {
     "PRRT_kwDOP2Xf8s6oM9J4": {
       "path": "docs/public/concepts/glossary.md",
       "line": 161,
       "author": "reviewer",
       "summary": "Clarify function call context",
       "proposedFix": "Clarify each function call context to specify who initiates the call and where execution takes place.",
       "draftReply": "Clarified each function call context as requested.",
       "action": "accept",
       "assessment": "solid"
     }
   }
   ```

#### Phase 2: Launch Dashboard & Interactive Review (or Auto-Approve)

5. **Launch Dashboard or Auto-Approve**:

   - **Interactive Web Mode (Default)**: Start the standalone dashboard app as a background task, pointing it to the target workspace directory and conversation scratch directory:

     ```bash
     env -u GITHUB_TOKEN python3 scripts/launch_dashboard.py \
       --project-dir <path-to-target-workspace-directory> \
       --data-dir <conversation-scratch-directory> \
       --proposals-file <conversation-scratch-directory>/proposals.json \
       --mode auto
     ```

     The server writes its bound URL to `<conversation-scratch-directory>/dashboard_url.txt` and outputs `DASHBOARD_URL=http://localhost:<port>/`. View `dashboard_url.txt` to find the exact URL to present to the user.

   - **Auto-Approve Mode (Headless)**: When invoked with `--auto-approve` (e.g. `/pr-feedback-handler --auto-approve`), bypass the web server and browser completely:

     ```bash
     env -u GITHUB_TOKEN python3 scripts/launch_dashboard.py \
       --project-dir <path-to-target-workspace-directory> \
       --data-dir <conversation-scratch-directory> \
       --proposals-file <conversation-scratch-directory>/proposals.json \
       --auto-approve
     ```

     This automatically synthesizes approved decisions from `proposals.json`, writes `<conversation-scratch-directory>/feedback_state.json`, and exits immediately with status `0`.

   - *Artifact Mode*: In headless or remote cloud environments without browser display, you may specify `--mode artifact` to generate a markdown triage report artifact directly into `data-dir` (`pr_triage_report.md`).

6. **Wait for Completion (Interactive Mode Only)**: In interactive mode, stop calling tools and go idle. The launcher will automatically merge `proposals.json` into the review UI, open the browser for the user (when local), and block until they click "Save & Apply Plan" or "Abort". Once submitted, you will receive a notification with the command's exit status. In auto-approve mode, this step is skipped.

______________________________________________________________________

### Step 2: Implement Approved Fixes & Add Regression Tests

Once the dashboard review completes (or auto-approved):

1. **Verify Exit Status**:

   - If the task exited with status `0` (success), proceed to implement the fixes.
   - If the task exited with a non-zero status (e.g., `1` for Abort), stop and ask the user for further instructions.

2. **Read the Plan**: Read `feedback_state.json` from your conversation-specific scratch directory (`<appDataDir>/brain/<conversation-id>/scratch/feedback_state.json`).

3. **Execute Approved Fixes**: For each item in `decisions` where `approved: true` and `action: "accept"`:

   - Apply the suggestion or implement the fix in the target file.
   - If `agentInstructions` is populated, prioritize those instructions over your original `proposedFix`.
   - When refactoring code in response to readability, maintainability, or "simplify this" feedback, run the [cognitive-complexity](../cognitive-complexity/SKILL.md) skill before and after edits to confirm cognitive complexity was reduced.
   - If `action: "decline"` or `action: "clarify"`, skip code changes for that thread.

4. **Add Regression Tests**: When fixing any reviewer-reported bug or logic flaw, add targeted unit tests to verify the fix and prevent future regressions.

5. **Verify and Commit**: Delegate local verification and committing to the `commit-changes` skill. Format, lint, run tests, and commit the changes locally.

______________________________________________________________________

### Step 3: Respond, Resolve on GitHub & Completion

Once the approved code changes are verified and committed:

- **When `--auto-approve` was requested**:
  The user's explicit `--auto-approve` command serves as pre-authorization. Skip the interactive menu and execute the remote updates directly:
  1. Push the local branch to GitHub:
     ```bash
     git push origin <branch>
     ```
  2. Post replies and resolve review threads in bulk:
     ```bash
     python3 scripts/update_thread.py --file <conversation-scratch-directory>/feedback_state.json
     ```
  3. Report a clear completion summary to the user with the commit hash, pushed branch, and links to the resolved threads.

- **In Interactive Mode (Default)**:
  1. **Interactive Next Steps Menu**: Use `ask_question` to ask the user how they would like to proceed:
     - **Option 1**: "(Recommended) Push changes and update/resolve review threads on GitHub."
     - **Option 2**: "Push changes to remote only (do not resolve threads yet)."
     - **Option 3**: "Keep changes local for manual review."

  2. **Submit Replies and Resolve Threads in Bulk**:
     - If approved to resolve on GitHub, run the bulk thread updater:
       ```bash
       python3 scripts/update_thread.py --file <conversation-scratch-directory>/feedback_state.json
       ```
     - `update_thread.py` automatically appends `<sub><!-- agent-generated --><kbd>🤖 Agent-generated</kbd></sub>` to each posted reply; do not manually add this badge to `draftReply`.
     - If any thread updates fail, review the printed failure report, make adjustments, and re-run if needed.

______________________________________________________________________

## Bundled Resources

- **`scripts/scaffold_proposals.py`**: Generates pre-populated `proposals.json` templates from `pr_comments.json` with terminal summary output.
- **`scripts/update_thread.py`**: Bulk posts replies to and resolves approved PR review threads on GitHub.
- **`scripts/launch_dashboard.py`**: Standalone review dashboard launcher supporting local web mode, remote SSH/Cloud detection, URL file emission, and markdown artifact export.
- **`assets/pr_feedback.html`**: Interactive dark-themed web dashboard with tabs for inline comments, top-level reviews, conversation comments, CI failures (with check annotations), and active checks.
