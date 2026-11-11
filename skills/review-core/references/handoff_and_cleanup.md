# Workspace Cleanup and Interactive Hand-Off

Shared Step 6 (cleanup) and Step 7 (stop and hand off) rules for `code-review` and `design-review`.

## Step 6: Workspace Cleanup Guard

Remove every temporary directory created during the review (cloned PR checkouts, `--verify` per-reviewer checkouts, staged diff/markdown directories, or scratch staging directories), even if an error or early exit occurred:

1. If the shell `cwd` entered a temporary checkout directory, return to the original workspace directory first.
2. Verify each path is non-empty, exists, and contains the expected temporary directory prefix (`*/pr-review-*`, `*/pr-review-diffs-*`, `*/design-review-*`) before calling `rm -rf`.
3. Retain `--json` output paths requested by a caller (`pr_review_results.json`, `review_results.json`, `design_review_results.json`) and any `<conversation-artifact-dir>/` reports.
4. Keep parked `swarm` workers (`rev-*` / `des-*`) alive as `waiting` per [delegation.md](delegation.md#worker-lifecycle-reuse-vs-fresh-unanchored-context); do not `kill` them here.

## Step 7: Interactive Hand-Off and Stop

### Caller Mode

When invoked by an automated or multi-stage caller (`pr-review`, `ship-it`, or `design-review --post-design` when clean with `0` `critical`/`high` findings and `< 3` total findings/questions):

- Complete Steps 1 through 6 (including cleanup) and skip the standalone chat stop below.
- Hand control back to the caller without printing the triage question.

### Standalone Mode (and Non-Clean `--post-design`)

1. **A2UI Pane Surfaces (`genui`)**: When `render_a2ui` (`a2ui-pane`) is reachable (native or via `call_mcp_tool`), read the `genui` skill:
   - In `design-review`, render `template: "scorecard"` (`mode: "pane"`, `surfaceId: "design-scorecard"`) from `design_review_results.json`'s numeric-only `scorecard` block. In `code-review`, render `scorecard` only when the review includes a scored quality rubric across 3+ criteria.
   - When `3` or more total findings and questions exist, render `template: "triage_matrix"` (`mode: "pane"`, `stack: true` when stacking with a scorecard) with each item's file/line or target anchor in `trace`. Pre-populate recommended buckets (`Fix now` / `Follow-up` / `Discuss` in `code-review`; `Revise design` / `Accept tradeoff` / `Defer to /plan` in `design-review`).
2. **One Concise Chat Message, Then End the Turn**:
   - Never print the full review markdown in chat.
   - Link the report artifact (`review_results.md` or `design_review_results.md`, plus the `--json` file if exported) and `[A2UI Pane](sidecar://a2ui-pane/a2ui-pane/)` if a surface was rendered.
   - State the `Verdict` (`Ready` / `Minor Revisions Needed` / `Critical Blockers`), overall score (for `design-review`), finding counts by severity plus question count, any non-empty `packrat` `user_note`, and any Step 5b playbook proposals link.
   - End with one closing question asking which findings to address (or carry into `/plan`) and **stop without editing any workspace files**.

### Wake-Up and Two-Request Rules

- **Reviewer wake-up rule**: When a swarm worker or reviewer subagent wakes you, the only steps remaining are Step 5 (synthesis), Step 6 (cleanup), and Step 7 (stop and hand off). This holds even if the conversation was implementing code before the review started.
- **"Review and fix" requests**: Treat them as two requests. The review still ends at Step 7; editing files starts only in a later turn after the user confirms which findings to address.
