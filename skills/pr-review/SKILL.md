---
name: pr-review
description: >-
  Reviews someone else's GitHub pull request end to end: writes an unbiased
  summary from the diff before reading the author's description, reconciles
  stated and actual intent, runs code-review on a throwaway checkout, stages
  every comment in a local dashboard for approval, and posts one batched review.
  Use when the user asks to review, critique, or comment on a PR by number or
  URL. Flags: --fast, --panel, --verify, --claude, --challenge, --max. Don't use
  for reviewing local or uncommitted changes (use code-review), for responding
  to feedback on the user's own PR (use pr-feedback-handler), or for summarizing
  a PR without reviewing it (use analyze-github-pr).
---

# PR Review Skill

> [!CAUTION]
> **READ-ONLY WORKSPACE, BLIND-FIRST ISOLATION, ZERO-APPROVAL COMMANDS & USER APPROVAL ENFORCEMENT**
> - Do NOT modify repository code in the primary workspace.
> - Perform all checkout and diff operations in an ephemeral temporary directory (`/tmp/pr-review-XXXXXX`).
> - **Zero-Approval Command Execution**: Do not run `mkdir -p` or `rm` in shell commands (`review_engine.py`, `diff_parser.py`, `launch_dashboard.py`, and `write_to_file` create `<conversation-scratch-dir>` and parent directories automatically), and pass `<conversation-scratch-dir>/workspace_dir.txt` directly to `--repo-dir` and `--cleanup` instead of using `"$(cat ...)"` command substitution. The one exception is `code-review`'s Step 6 cleanup loop in Step 3.3, which removes only the directories `code-review` itself created.
> - **Scratch directory**: `<conversation-scratch-dir>` is `<appDataDir>/brain/<conversation-id>/scratch/pr-review/`. Every intermediate file this skill names (`pr_coords.json`, `pr.diff`, `workspace_dir.txt`, `blind_description.md`, `review_results.json`, `review_findings.json`, and so on) lives there.
> - **Stage 1 Blindness**: Before `<conversation-scratch-dir>/blind_description.md` is written solely from the code diff, do NOT inspect the PR `title`, `body`, commit messages, branch names (`headRefName`), `comments`, `reviews`, or `threads`. Only `gh pr view --json number,url` (Step 1) and `review_engine.py --prepare-workspace` (Step 2) are permitted in Stage 1. The `stack` object in `pr_coords.json` (PR numbers, states, and rebase flags only) may be read in Stage 1; `pr_stack.json` (which carries branch names) and `gh stack view` output may not.
> - Launch the review dashboard and wait for the user to submit or abort before posting any reviews or comments to GitHub.
> - Never promise future work or unverified assumptions in review comments.

This skill automates reviewing external GitHub Pull Requests (reviewing someone else's code). It derives an unbiased summary of the code changes from the diff alone before reconciling that summary against the author's stated purpose and review comments, then opens a local web dashboard so the human reviewer can inspect, edit, and approve every comment before posting to GitHub.

---

## Flags

`pr-review` accepts the same composable analysis flags as `code-review`, passing them through to Step 3:

| Flag | Effect | Cost |
| :--- | :--- | :--- |
| *(none)* | 1 Gemini reviewer; tier 1a static/remote checks always, plus tier 1b build dry-runs and repo scripts after the Pre-Execution Security Gate, and seed hypotheses | Baseline |
| `--fast` | Single-pass in-session review without subagents; tier 1a/1b checks only | Minimal latency / no subagents |
| `--claude` (or `--opus`) | Pins the single reviewer (or reviewer B under `--panel`) to Claude Opus (`opus-5.5-max` on Tier 1 swarm, or `model: "opus"` on Tier 2a Claude Code). | Uses 3P quota |
| `--panel` | 2 reviewers in parallel with non-overlapping criteria (lanes A and B) | About 2x reviewers |
| `--verify` | Adds tier 2 (targeted tests, repro tests, and reverted mutations in throwaway checkouts) for trusted authors after the Pre-Execution Security Gate passes | Test runtime and dependency installs |
| `--max` | Same as `--claude --panel --verify` | All of the above |
| `--challenge` | Challenge protocol sending refuting evidence back to reviewers | One extra turn per disputed reviewer |

Legacy `--mode <depth>` syntax maps to these flags for backward compatibility:
- `--mode fast` or `--mode standard` maps to `--fast`.
- `--mode deep` maps to the default (Gemini reviewer).
- `--mode max` maps to `--max`.

---

## Workflow

Follow these steps in order when reviewing a pull request:

### Step 1: Ingestion & Analysis Depth Selection

1. **Identify the PR**:
   - Determine the target PR from the user prompt (URL, PR number, or head branch).
   - If no PR is specified, run `gh pr view --json number,url` (do NOT request `title`, `body`, `headRefName`, `comments`, or `reviews`) in the active workspace.
2. **Select Analysis Depth**:
   - If the user did not specify analysis flags or a legacy `--mode`, prompt the user to choose (using your interactive question tool if available, or by asking in chat and waiting for their reply):
     - **(Recommended) Deep Review**: Delegate to a Gemini reviewer lane (`gemini-launch-candidate-high` via `swarm` on Tier 1) with Tier 1 static/CI checks, seeded hypotheses from Stage 1, and evidence/citation verification.
     - **Fast Review (`--fast`)**: Single-pass in-session review without spawning swarm workers or subagents for rapid turnaround.
     - **Max Review (`--max`)**: Two-lane panel (lane A Gemini `gemini-launch-candidate-high`, lane B Claude `opus-5.5-max` / `opus`) with Tier 2 test and mutation verification in isolated checkouts.

---

### Step 2: Ephemeral Workspace & Blind Diff Description (Stage 1)

1. **Prepare Isolated Workspace, Detect Stack Membership, Extract Diff & Run Static Security Pre-Scan (No Narrative Metadata)**:
   Run `review_engine.py --prepare-workspace` (do not run `mkdir -p` beforehand; the script creates `<conversation-scratch-dir>` automatically) to resolve non-narrative git coordinates, clone the PR into `/tmp/pr-review-XXXXXX`, extract the unified diff without fetching the PR title, body, branch name, or comments, and scan the diff for local execution risks:
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
   `review_engine.py` hardens every `git` and `gh` subprocess (`core.hooksPath=/dev/null`, `core.fsmonitor=false`, `diff.external=`, `--no-ext-diff`, `--no-textconv`) so cloning and diffing an untrusted PR cannot execute repository hooks or diff drivers, and writes a deterministic `security_prescan` summary into `pr_coords.json`. Do **not** run any build step, toolchain dry-run, repo script, dependency install, or test inside `workspace_dir.txt` during Stage 1.
   The script also asks GitHub whether the PR belongs to a stack (`pullRequest.stack` via GraphQL). If it does, it runs `gh stack checkout <pr>` and `gh stack view --json` in the temporary checkout (restoring the detached `HEAD` afterwards), writes the member list with rebase status to `pr_stack.json`, and records a numbers-only summary in `pr_coords.json` under `stack` (`number`, `size`, `position` with 1 = bottom of the stack, `needs_rebase`, and per-entry `number`/`state`/`is_draft`/`needs_rebase`). Without the `gh stack` extension, rebase status comes from git ancestry in the checkout instead. `stack` is `null` for an unstacked PR, and `pr_stack.json` is not written.
2. **Map Diff Hunks**:
   Parse valid diff hunks using `diff_parser.py`:
   ```bash
   python3 scripts/diff_parser.py --diff <conversation-scratch-dir>/pr.diff
   ```
3. **Draft the Blind Diff Description & Pre-Execution Security Gate**:
   - Inspect `<conversation-scratch-dir>/pr.diff`, `pr_coords.json.security_prescan`, and surrounding code in the temporary checkout (`workspace_dir.txt`) using only read-only file reads (`view_file` / `read_file` and `rg`) or Tier 1a checks ([verification.md](../code-review/references/verification.md#tier-1-commands)).
   - Do **not** read the PR title, description, commit messages, branch names, or GitHub comments during this step. Do **not** read `pr_stack.json` or run `gh stack view` yet.
   - Audit every file and line flagged in `pr_coords.json.security_prescan.signals`, plus any modified build manifests, git/shell hooks, linter plugin configs, or test files, and determine the Pre-Execution Security Gate verdict per [verification.md](../code-review/references/verification.md#pre-execution-security-gate): `SAFE_FOR_LOCAL_EXECUTION`, `REQUIRES_USER_CONFIRMATION`, or `BLOCKED_UNSAFE`.
   - Write `<conversation-scratch-dir>/blind_description.md` following the **Blind-First PR Description Template** in [review_guidelines.md](references/review_guidelines.md) (`## Summary`, `## Inferred Purpose`, `## Quality Assessment`, `## Security & Execution Gate`, `## Changes`, and `## Reviewer Focus & Risks`).
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
   - Copy the text of `blind_description.md` verbatim into `<conversation-scratch-dir>/pr_description.md`: do not rewrite or soften the Stage 1 prose to match the author's framing. Prepend a top-level `## Intent Alignment: <Status>` section (`Matches Stated Purpose`, `Partial Divergence`, `Diverges from Stated Purpose`, or `Unstated Scope`) and weave inline GitHub callouts (`> [!WARNING]`, `> [!IMPORTANT]`, `> [!NOTE]`) directly beneath the relevant items in `## Summary`, `## Inferred Purpose`, `## Quality Assessment`, `## Security & Execution Gate`, and `## Changes`. See [review_guidelines.md](references/review_guidelines.md) for callout rules.
   - If `pr_context.json` has a non-null `stack`, add a `## Stack Context` section right after `## Intent Alignment` following the template in [review_guidelines.md](references/review_guidelines.md). You may run `gh stack view --short` in the checkout (`workspace_dir.txt`) for the same information in `gh`'s own format. Treat the stack as review context, not as a finding: a predecessor that `needs_rebase` or is still a draft is a `> [!NOTE]`, a claim in the body that a later PR in the stack covers something is checked against that PR's diff (`gh pr diff <n>`) before it is accepted, and anything the diff builds on from a predecessor is reviewed in that predecessor's PR, not here.
3. **Delegate Code Review Analysis to `code-review`**:
   Execute the [`code-review`](../code-review/SKILL.md) skill workflow against the temporary checkout directory (`<conversation-scratch-dir>/workspace_dir.txt`) on the PR commit range `origin/<base_ref>...<head_sha>` (`base_ref` and `head_sha` from `pr_coords.json`) with `--json` and the chosen flags (`--fast`, `--panel`, `--verify`, `--claude`, `--challenge`, `--max`):
   - **Preserve Blindness Invariant**: Do not pass the PR number or URL directly to `code-review`. Run `code-review` strictly on the local checkout commit range `origin/<base_ref>...<head_sha>` in the temporary checkout directory.
   - **Checkout Ownership**: The `workspace_dir.txt` checkout belongs to `pr-review`. It is not `code-review`'s `$pr_review_dir_a`, even though both live under `/tmp/pr-review-*`. `code-review` reads it in place and never removes it; Step 3.4 reads it and Step 5.2 removes it. Any `--verify` checkout that `code-review` creates in its Step 2c (`$pr_review_dir_a` for lane A, `$pr_review_dir_b` for lane B per [verification.md](../code-review/references/verification.md#per-reviewer-checkouts)) is `code-review`'s own and is removed in its Step 6.
   - **Seed Hypotheses**: Pass the risks and security signals identified in Stage 1's `blind_description.md` (`## Security & Execution Gate` and `## Reviewer Focus & Risks`) into `code-review` as seed hypotheses.
   - **Execution Modes, Pre-Execution Security Gate & Trust Gate**:
     - Pass the Stage 1 `## Security & Execution Gate` verdict (`SAFE_FOR_LOCAL_EXECUTION`, `REQUIRES_USER_CONFIRMATION`, or `BLOCKED_UNSAFE`) and `pr_coords.json.security_prescan` into `code-review` Step 2a:
       - **Tier 1a** checks (remote GitHub CI status/logs, isolated `python3 -I -c "import ast..."`, and static analyzers/linters such as `ruff`, `dart format`, `dart analyze`, `tsc --noEmit`, and `eslint` whose executable config/plugin files are untouched by the PR) are always allowed.
       - **Tier 1b** checks (`swift build`, `make -n`, `bazel query`, `gradle tasks`, repo-hosted validator scripts, and any analyzer/linter whose config or plugin file was modified in the PR) and **Tier 2** checks (throwaway `--verify` checkouts, dependency installs, tests, and mutations) require `SAFE_FOR_LOCAL_EXECUTION`.
       - If the gate verdict is `REQUIRES_USER_CONFIRMATION`, prompt the user via `confirm_gate` (`genui`) or `ask_question` before running any Tier 1b or Tier 2 command. If the user declines or the gate is `BLOCKED_UNSAFE`, restrict `code-review` to Tier 1a regardless of `author_association`.
     - Under `--fast`: `code-review` performs a single-pass in-session review on the diff and context files without spawning subagents.
     - Under default (or `--claude`, `--panel`, `--verify`, `--challenge`, `--max`): `code-review` manages subagent reviewer lanes, verification tiers, citation audits, and challenge rounds as configured. Under `--verify`, look up collaborator trust with `gh api repos/<owner>/<repo>/pulls/<pull_number> --jq .author_association`, filling in `owner`, `repo`, and `pull_number` from `pr_coords.json` (do not use `gh`'s `{owner}/{repo}` placeholders, which resolve from the current directory, not from the PR under review). See [verification.md](../code-review/references/verification.md#trust-check). State both the collaborator trust result and the Stage 1 Security Gate verdict in the `code-review` prompt: Tier 2 requires both a trusted collaborator (`OWNER`, `MEMBER`, or `COLLABORATOR`) and a cleared Security Gate (`SAFE_FOR_LOCAL_EXECUTION` or user-approved `REQUIRES_USER_CONFIRMATION`).
   - **Orchestration Continuity**: Run `code-review` Steps 1 through 6 and skip Step 7. Pass `--json <conversation-scratch-dir>/review_results.json` so Step 5's `lint_review.py --export-json` writes where Step 3.4 reads (without a path, `code-review` would write to its own `<artifact-dir>`). Run `code-review`'s Step 6 cleanup loop as written; it is the one exception to the Zero-Approval rule above, and it removes only the directories `code-review` created (`$diff_dir` and any `--verify` checkouts), never the `workspace_dir.txt` checkout. After Step 6 completes, continue to Step 3.4 in the same turn, including when a swarm worker or reviewer subagent wakes you. Do not emit Step 7's hand-off question.
   - **Suggestion Syntax**: Formulate code suggestions following [suggestion_syntax.md](references/suggestion_syntax.md) so they serialize into GitHub suggestion blocks. The exporter posts each finding's `Suggestion` as a committable ```` ```suggestion ```` block (except ```` ```diff ````/```` ```patch ```` fences) and a `Repro test` as a plain code block; `lint_review.py` rejects a `Suggestion` with more than one fenced block.
   - **JSON Output**: The `code-review` workflow validates the report AST and exports `<conversation-scratch-dir>/review_results.json` via `lint_review.py --export-json <conversation-scratch-dir>/review_results.json`.
4. **Tag Hunk Boundaries and Validate Findings**:
   - Merge `pr_meta.json` into the review metadata and parse diff hunk boundaries using `diff_parser.py`:
     ```bash
     python3 scripts/diff_parser.py \
       --diff <conversation-scratch-dir>/pr.diff \
       --findings <conversation-scratch-dir>/review_results.json \
       --repo-dir <conversation-scratch-dir>/workspace_dir.txt \
       --output <conversation-scratch-dir>/review_findings.json
     ```
   - `diff_parser.py` tags each finding with `is_diff_hunk: true` (for comments on lines modified within diff hunks) or `is_diff_hunk: false` (for outside-diff or broader context observations), while preserving enriched metadata:
     - Evidence tiers (`evidence_tier`, `evidence_detail`), consequences (`why`), and resolution criteria (`to_settle`).
     - Clarification questions (merged into `findings[]` with `severity: "question"`).
     - Hypotheses checked clean (`checked_clean[]`) and structural assessment (`structural_assessment`).
   - If manual or legacy findings are used in `<conversation-scratch-dir>/raw_findings.json`, pass `--findings <conversation-scratch-dir>/raw_findings.json` instead.

---

### Step 4: Emit Annotated Description in Chat & Launch Dashboard

1. **Emit the Annotated PR Description in Chat**:
   Output the full contents of `<conversation-scratch-dir>/pr_description.md` directly in your conversation response so the user can read the unbiased summary, intent alignment verdict, quality assessment, and inline annotations in chat before reviewing the findings. In **Pane Mode**, always include a link to open the side panel (e.g. `[Open PR Review](sidecar://review-dashboards/pr-review)`) in this chat response so the user can easily open or bring the dashboard pane into view.
2. **Open the Dashboard**:
   - **Pane Mode (Preferred when `review-dashboards` MCP tools are available)**:
     Call `open_pr_review_dashboard` (native tool or via `call_mcp_tool` on the `review-dashboards` server) with:
     - `findings_file`: `<conversation-scratch-dir>/review_findings.json`
     - `meta_file`: `<conversation-scratch-dir>/pr_meta.json`
     - `description_file`: `<conversation-scratch-dir>/pr_description.md`
     - `output_file`: `<conversation-scratch-dir>/review_decisions.json`
     If the returned `warnings` array lists missing `pr_metadata` fields, fix `pr_meta.json` or the findings file and call `open_pr_review_dashboard` again. If `hosted` is `false`, fall back to Browser Mode below. Always accompany the chat response with a clickable link to open or toggle the side panel: `[Open PR Review](sidecar://review-dashboards/pr-review)`.
   - **Browser Mode (Fallback when MCP tools are unavailable or `hosted: false`)**:
     Launch `launch_dashboard.py` with `--description-file`:
     ```bash
     python3 scripts/launch_dashboard.py \
       --findings-file <conversation-scratch-dir>/review_findings.json \
       --meta-file <conversation-scratch-dir>/pr_meta.json \
       --description-file <conversation-scratch-dir>/pr_description.md \
       --output-file <conversation-scratch-dir>/review_decisions.json
     ```
     Set `WaitMsBeforeAsync` to `1000` so the server runs in the background.
     If the output shows `Warning: pr_metadata.<field> is missing`, stop the task, fix `pr_meta.json` or the findings file, and relaunch. The dashboard disables Submit until `owner`, `repo`, `pull_number`, and `head_sha` are present.
   - The annotated PR description appears in a formatted Markdown card at the top of the dashboard for reviewer context only (`apply_review.py` never posts it; its text is included only if the user clicks **+ Insert into Review Summary**).
   - Every finding starts unchecked. The user checks the ones to post, one at a time or with **Approve all shown**.
3. **Wait for Completion**:
   Stop calling tools and go idle.
   - In **Pane Mode**, tell the user the dashboard is open in the side pane, include the clickable side panel link (`[Open PR Review](sidecar://review-dashboards/pr-review)`), and wait for a message starting with `Review dashboard submitted` or `Review dashboard aborted`.
   - In **Browser Mode**, the launcher opens the review UI in the user's browser and waits until they click **"Submit Review to GitHub"** or **"Abort Review"**.
4. **Verify Status**:
   - In **Pane Mode**, call `dashboard_status` with the `session` ID: if `status` is `"saved"`, proceed to Step 5; if `status` is `"aborted"`, clean up the temporary workspace and stop without contacting GitHub.
   - In **Browser Mode**, check the background task exit code: if `0` (saved), proceed to Step 5; if `1` (aborted by user), clean up the temporary workspace and stop without contacting GitHub; if `2` (startup or file validation error), inspect `stderr`, fix the missing or malformed file, and relaunch the dashboard.

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
   Any `--verify` checkouts and the staged diff directory that `code-review` created were already removed by its Step 6 in Step 3.3.
3. **Report to User**:
   Provide the user with a summary containing the review verdict, approved comments count, and the URL to the submitted GitHub review.

---

## Bundled Resources

- **[review_guidelines.md](references/review_guidelines.md)**: Rules for blind-first PR descriptions, intent reconciliation annotations, and actionable code review feedback.
- **[suggestion_syntax.md](references/suggestion_syntax.md)**: GitHub markdown suggestion fence syntax and line range rules.
- **[findings_schema.md](references/findings_schema.md)**: Fields of `review_findings.json`, `review_decisions.json`, `pr_coords.json`, and `pr_context.json`.
- **`scripts/diff_parser.py`**: Unified diff parser and diff hunk line validator.
- **`scripts/review_engine.py`**: Two-stage workspace preparer (`--prepare-workspace`, with hardened git config, static execution-risk pre-scanning into `pr_coords.json.security_prescan` or `--security-scan`, and GitHub stack detection via `gh stack view --json` into `--output-stack`), context fetcher (`--fetch-context` with `--require-blind-description` and `--input-stack`), and cleanup utility.
- **`scripts/launch_dashboard.py`**: Local HTTP server and `--dump-json` data normalizer for the interactive review dashboard. Normalizes findings and merges `--meta-file` and `--description-file`.
- **`assets/review_dashboard.html`**: Interactive dark-themed single-page review dashboard with a top-level Blind Diff Description & Intent Alignment card.
- **`scripts/apply_review.py`**: Submits batched review payloads to the GitHub Reviews API with hybrid comment routing.

