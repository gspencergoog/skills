---
name: pr-review
description: "Conducts interactive, high-signal code reviews on external GitHub Pull Requests by deriving an unbiased description from the diff before reading the PR description or comments, reconciling stated vs. actual intent, launching a local review dashboard with GitHub suggestion block editing, and submitting atomic batched reviews."
---

# PR Review Skill

> [!CAUTION]
> **READ-ONLY WORKSPACE, BLIND-FIRST ISOLATION, ZERO-APPROVAL COMMANDS & USER APPROVAL ENFORCEMENT**
> - Do NOT modify repository code in the primary workspace.
> - Perform all checkout and diff operations in an ephemeral temporary directory (`/tmp/pr-review-XXXXXX`).
> - **Zero-Approval Command Execution**: Do not run `mkdir -p` or `rm` in shell commands (`review_engine.py`, `diff_parser.py`, `launch_dashboard.py`, and `write_to_file` create `<conversation-scratch-dir>` and parent directories automatically), and pass `<conversation-scratch-dir>/workspace_dir.txt` directly to `--repo-dir` and `--cleanup` instead of using `"$(cat ...)"` command substitution.
> - **Stage 1 Blindness**: Before `<conversation-scratch-dir>/blind_description.md` is written solely from the code diff, do NOT inspect the PR `title`, `body`, commit messages, branch names (`headRefName`), `comments`, `reviews`, or `threads`. Only `gh pr view --json number,url` (Step 1) and `review_engine.py --prepare-workspace` (Step 2) are permitted in Stage 1. The `stack` object in `pr_coords.json` (PR numbers, states, and rebase flags only) may be read in Stage 1; `pr_stack.json` (which carries branch names) and `gh stack view` output may not.
> - Launch the review dashboard and wait for the user to submit or abort before posting any reviews or comments to GitHub.
> - Never promise future work or unverified assumptions in review comments.

This skill automates reviewing external GitHub Pull Requests (reviewing someone else's code). It derives an unbiased summary of the code changes from the diff alone before reconciling that summary against the author's stated purpose and review comments, then opens a local web dashboard so the human reviewer can inspect, edit, and approve every comment before posting to GitHub.

---

## Workflow

Follow these steps in order when reviewing a pull request:

### Step 1: Ingestion & Analysis Depth Selection

1. **Identify the PR**:
   - Determine the target PR from the user prompt (URL, PR number, or head branch).
   - If no PR is specified, run `gh pr view --json number,url` (do NOT request `title`, `body`, `headRefName`, `comments`, or `reviews`) in the active workspace.
2. **Select Analysis Depth**:
   - If the user did not specify `--mode fast`, `--mode standard`, `--mode deep`, or `--mode max`, use `ask_question` to prompt:
     - **(Recommended) Deep Review (`--mode deep`)**: Delegate to `gemini-code-reviewer` with Tier 1 static/CI checks, seeded hypotheses, and evidence/citation verification.
     - **Standard Review (`--mode standard`)**: Direct review of the diff and surrounding context files in the checkout with a self-critique pass.
     - **Fast Review (`--mode fast`)**: Diff-only single-pass scan for quick turnaround.
     - **Max Review (`--mode max`)**: Two-lane `claude-code-reviewer` panel with Tier 2 test and mutation verification in isolated `/tmp/pr-review-*` checkouts.

---

### Step 2: Ephemeral Workspace & Blind Diff Description (Stage 1)

1. **Prepare Isolated Workspace, Detect Stack Membership & Extract Diff (No Narrative Metadata)**:
   Run `review_engine.py --prepare-workspace` (do not run `mkdir -p` beforehand; the script creates `<conversation-scratch-dir>` automatically) to resolve non-narrative git coordinates, clone the PR into `/tmp/pr-review-XXXXXX`, and extract the unified diff without fetching the PR title, body, branch name, or comments:
   ```bash
   python3 scripts/review_engine.py \
     --pr <pr-number-or-url> \
     --prepare-workspace \
     --reference <path-to-local-repo-if-available> \
     --output-coords <conversation-scratch-dir>/pr_coords.json \
     --output-diff <conversation-scratch-dir>/pr.diff \
     --output-workspace <conversation-scratch-dir>/workspace_dir.txt \
     --output-stack <conversation-scratch-dir>/pr_stack.json
   ```
   The script also asks GitHub whether the PR belongs to a stack (`pullRequest.stack` via GraphQL). If it does, it runs `gh stack checkout <pr>` and `gh stack view --json` in the temporary checkout (restoring the detached `HEAD` afterwards), writes the member list with rebase status to `pr_stack.json`, and records a numbers-only summary in `pr_coords.json` under `stack` (`number`, `size`, `position` with 1 = bottom of the stack, `needs_rebase`, and per-entry `number`/`state`/`is_draft`/`needs_rebase`). Without the `gh stack` extension, rebase status comes from git ancestry in the checkout instead. `stack` is `null` for an unstacked PR, and `pr_stack.json` is not written.
2. **Map Diff Hunks**:
   Parse valid diff hunks using `diff_parser.py`:
   ```bash
   python3 scripts/diff_parser.py --diff <conversation-scratch-dir>/pr.diff
   ```
3. **Draft the Blind Diff Description**:
   - Inspect `<conversation-scratch-dir>/pr.diff` and surrounding code in the temporary checkout (`workspace_dir.txt`).
   - Do **not** read the PR title, description, commit messages, branch names, or GitHub comments during this step. Do **not** read `pr_stack.json` or run `gh stack view` yet.
   - Write `<conversation-scratch-dir>/blind_description.md` following the **Blind-First PR Description Template** in [review_guidelines.md](references/review_guidelines.md) (`## Summary`, `## Inferred Purpose`, `## Quality Assessment`, `## Changes`, and `## Reviewer Focus & Risks`).
   - If `pr_coords.json` has a non-null `stack`, the diff is only this PR's slice (`base_ref` is the predecessor branch). Review the slice on its own terms: do not report code that the diff builds on but does not touch as missing, and add a bullet under `## Reviewer Focus & Risks` for anything that only makes sense once the rest of the stack is known.

---

### Step 3: PR Context Reconciliation & Code Review Analysis (Stage 2)

1. **Fetch PR Narrative Context**:
   Once `<conversation-scratch-dir>/blind_description.md` is written and non-empty, run `review_engine.py --fetch-context`. The script verifies that `blind_description.md` exists before querying GitHub for the PR title, body, reviews, and review threads:
   ```bash
   python3 scripts/review_engine.py \
     --fetch-context \
     --input-coords <conversation-scratch-dir>/pr_coords.json \
     --input-stack <conversation-scratch-dir>/pr_stack.json \
     --require-blind-description <conversation-scratch-dir>/blind_description.md \
     --output-meta <conversation-scratch-dir>/pr_meta.json \
     --output-context <conversation-scratch-dir>/pr_context.json
   ```
   `--input-stack` is ignored when the file does not exist. For a stacked PR, `pr_context.json` gains a `stack` object (members bottom-first with `number`, `title`, `state`, `is_draft`, `head_ref`, `base_ref`, `needs_rebase`, and `is_target`) and `pr_meta.json` gains `stack: {number, size, position}`.
2. **Reconcile Stated Intent vs. Actual Diff**:
   - Compare `blind_description.md` against the author's `title`, `body`, `comments`, `reviews`, and `threads` in `pr_context.json` (if `threads_complete` is `false`, or if `pr_meta.json` contains `latest_head_sha` differing from `head_sha`, note this in a `> [!NOTE]` callout).
   - Copy the text of `blind_description.md` verbatim into `<conversation-scratch-dir>/pr_description.md` — do **not** rewrite or soften the Stage 1 prose to match the author's framing. Prepend a top-level `## Intent Alignment: <Status>` section (`Matches Stated Purpose`, `Partial Divergence`, `Diverges from Stated Purpose`, or `Unstated Scope`) and weave inline GitHub callouts (`> [!WARNING]`, `> [!IMPORTANT]`, `> [!NOTE]`) directly beneath the relevant items in `## Summary`, `## Inferred Purpose`, `## Quality Assessment`, and `## Changes`. See [review_guidelines.md](references/review_guidelines.md) for callout rules.
   - If `pr_context.json` has a non-null `stack`, add a `## Stack Context` section right after `## Intent Alignment` following the template in [review_guidelines.md](references/review_guidelines.md). You may run `gh stack view --short` in the checkout (`workspace_dir.txt`) for the same information in `gh`'s own format. Treat the stack as review context, not as a finding: a predecessor that `needs_rebase` or is still a draft is a `> [!NOTE]`, a claim in the body that a later PR in the stack covers something is checked against that PR's diff (`gh pr diff <n>`) before it is accepted, and anything the diff builds on from a predecessor is reviewed in that predecessor's PR, not here.
3. **Analyze Code & Draft Concrete Suggestions**:
   Execute the analysis corresponding to the selected depth mode:
   - **`--mode fast` (Fast Review)**: Scan `<conversation-scratch-dir>/pr.diff` directly in a single pass without reading external context files or spawning subagents. Flag clear defects on modified lines.
   - **`--mode standard` (Standard Review)**: Inspect `<conversation-scratch-dir>/pr.diff` and surrounding context files in the temporary checkout (`workspace_dir.txt`) following [review_guidelines.md](references/review_guidelines.md), then perform a self-critique pass to drop speculative or unverified claims.
   - **`--mode deep` (Deep Review, default)**: Run Tier 1 read-only checks (inspect `gh pr checks` status and run configured fast static analyzers on modified files) and delegate the review to `gemini-code-reviewer` following the [`code-review`](../code-review/SKILL.md) skill criteria and [evidence.md](../code-review/references/evidence.md) (seeded hypotheses from Stage 1's `## Reviewer Focus & Risks`, evidence tiers, and `Why` consequences). Audit `critical` and `high` citations in the checkout before converting confirmed findings into `raw_findings.json`.
   - **`--mode max` (Max Review)**: Check the collaborator trust gate in [`code-review/references/verification.md`](../code-review/references/verification.md) (`authorAssociation` in `OWNER`, `MEMBER`, `COLLABORATOR`). If trusted, run `review_engine.py --pr <pr> --prepare-workspace --reference <conversation-scratch-dir>/workspace_dir.txt --output-workspace <conversation-scratch-dir>/workspace_dir_b.txt` to create a second `/tmp/pr-review-*` checkout for Lane B (and clean it up in Step 5.2 with `--cleanup <conversation-scratch-dir>/workspace_dir_b.txt`) so both lanes can run Tier 2 test and mutation verification; if untrusted, restrict both lanes to Tier 1 read-only verification. Invoke two `claude-code-reviewer` subagents in parallel (`Code Reviewer A`: correctness, concurrency, edge cases, security; `Code Reviewer B`: maintainability, test quality, API soundness, efficiency), audit `critical`/`high` citations, deduplicate findings, and convert confirmed findings into `raw_findings.json`.
   - In all modes, formulate GitHub suggestion blocks following [suggestion_syntax.md](references/suggestion_syntax.md). Each `draft_comment` states the problem and its effect first, then the fix. Keep the top-level review `overview` to 1–2 paragraphs without listing individual findings.
4. **Structure & Tag Findings**:
   - Write candidate findings to `<conversation-scratch-dir>/raw_findings.json` using the canonical shape in [findings_schema.md](references/findings_schema.md). Copy `pr_meta.json` into `pr_metadata`, and do not set `approved`:
     ```json
     {
       "pr_metadata": { "...": "copied from pr_meta.json" },
       "review_summary": {
         "overview": "1-2 paragraphs summarizing the review assessment and verdict rationale.",
         "verdict": "REQUEST_CHANGES"
       },
       "findings": [
         {
           "id": "f1",
           "severity": "high",
           "category": "correctness",
           "file_path": "src/parser.ts",
           "line_start": 120,
           "line_end": 122,
           "side": "RIGHT",
           "description": "One-sentence summary of the problem.",
           "draft_comment": "Problem and effect first, followed by fix or ```suggestion fence."
         }
       ]
     }
     ```
   - Run `diff_parser.py` to validate diff hunks and tag findings (`is_diff_hunk: true` for lines inside diff hunks; `is_diff_hunk: false` for lines outside diff hunks). Pass `<conversation-scratch-dir>/workspace_dir.txt` directly to `--repo-dir` (do not use `"$(cat ...)"`):
     ```bash
     python3 scripts/diff_parser.py \
       --diff <conversation-scratch-dir>/pr.diff \
       --findings <conversation-scratch-dir>/raw_findings.json \
       --repo-dir <conversation-scratch-dir>/workspace_dir.txt \
       --output <conversation-scratch-dir>/review_findings.json
     ```

---

### Step 4: Emit Annotated Description in Chat & Launch Dashboard

1. **Emit the Annotated PR Description in Chat**:
   Output the full contents of `<conversation-scratch-dir>/pr_description.md` directly in your conversation response so the user can read the unbiased summary, intent alignment verdict, quality assessment, and inline annotations in chat before switching to the browser.
2. **Start Dashboard Server as a Background Task**:
   Launch `launch_dashboard.py` with `--description-file`:
   ```bash
   python3 scripts/launch_dashboard.py \
     --findings-file <conversation-scratch-dir>/review_findings.json \
     --meta-file <conversation-scratch-dir>/pr_meta.json \
     --description-file <conversation-scratch-dir>/pr_description.md \
     --output-file <conversation-scratch-dir>/review_decisions.json
   ```
   Set `WaitMsBeforeAsync` to `1000` so the server runs in the background.
   - If the output shows `Warning: pr_metadata.<field> is missing`, stop the task, fix `pr_meta.json` or the findings file, and relaunch. The dashboard disables Submit until `owner`, `repo`, `pull_number`, and `head_sha` are present.
   - The annotated PR description appears in a formatted Markdown card at the top of the dashboard for reviewer context only (`apply_review.py` never posts it; its text is included only if the user clicks **+ Insert into Review Summary**).
   - Every finding starts unchecked. The user checks the ones to post, one at a time or with **Approve all shown**.
3. **Wait for Completion**:
   Stop calling tools and go idle. The launcher opens the review UI in the user's browser and waits until they click **"Submit Review to GitHub"** or **"Abort Review"**.
4. **Verify Exit Status**:
   - If the task exited with status `0` (saved), proceed to Step 5.
   - If the task exited with status `1` (aborted by user), clean up the temporary workspace and stop without contacting GitHub.
   - If the task exited with status `2` (startup or file validation error), inspect `stderr`, fix the missing or malformed file, and relaunch the dashboard.

---

### Step 5: Batch Review Submission & Workspace Cleanup

1. **Submit Batched Review**:
   Run `apply_review.py` to submit all approved decisions atomically via the GitHub API:
   ```bash
   python3 scripts/apply_review.py \
     --decisions-file <conversation-scratch-dir>/review_decisions.json
   ```
   - Inline comments (`is_diff_hunk: true`) are posted directly on their respective lines.
   - Broader context comments (`is_diff_hunk: false`) are appended under `### Broader Context & Non-Diff Observations` in the top-level review body.
   - `apply_review.py` automatically appends `<sub><!-- agent-generated --><kbd>🤖 Agent-generated</kbd></sub>` to each inline comment and non-empty review summary body; do not manually add this badge to `draft_comment` or `overview`.
2. **Clean Up Workspace**:
   Remove the temporary checkout directory by passing `<conversation-scratch-dir>/workspace_dir.txt` directly (do not use `"$(cat ...)"` or `rm -rf`):
   ```bash
   python3 scripts/review_engine.py --cleanup <conversation-scratch-dir>/workspace_dir.txt
   ```
3. **Report to User**:
   Provide the user with a summary containing the review verdict, approved comments count, and the URL to the submitted GitHub review.

---

## Bundled Resources

- **[review_guidelines.md](references/review_guidelines.md)**: Rules for blind-first PR descriptions, intent reconciliation annotations, and actionable code review feedback.
- **[suggestion_syntax.md](references/suggestion_syntax.md)**: GitHub markdown suggestion fence syntax and line range rules.
- **[findings_schema.md](references/findings_schema.md)**: Fields of `review_findings.json`, `review_decisions.json`, `pr_coords.json`, and `pr_context.json`.
- **`scripts/diff_parser.py`**: Unified diff parser and diff hunk line validator.
- **`scripts/review_engine.py`**: Two-stage workspace preparer (`--prepare-workspace`, with GitHub stack detection via `gh stack view --json` into `--output-stack`), context fetcher (`--fetch-context` with `--require-blind-description` and `--input-stack`), and cleanup utility.
- **`scripts/launch_dashboard.py`**: Local HTTP server for the interactive review dashboard. Normalizes findings and merges `--meta-file` and `--description-file`.
- **`assets/review_dashboard.html`**: Interactive dark-themed single-page review dashboard with a top-level Blind Diff Description & Intent Alignment card.
- **`scripts/apply_review.py`**: Submits batched review payloads to the GitHub Reviews API with hybrid comment routing.

