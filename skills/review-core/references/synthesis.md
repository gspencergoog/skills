# Synthesis, Citation Audit, Challenge, and Verdicts

Step 5 turns raw reviewer reports into a single lint-clean report artifact.

## 1. Collect Reports (Tier 1 `swarm`)

When a worker wakes `@coordinator`, call `read_board` (passing `since: <last_seq>` on subsequent wakes), read the report file at the path named in the post (`rev_<name>.md` or `rev_des_<name>.md`, or the post body when no path was written), and advance your `last_seq` cursor.

## 2. Clean Check and Playbook-Read Check

- Under `--verify` (in `code-review`), check `git -C "$dir" -c core.hooksPath=/dev/null -c core.fsmonitor=false status --porcelain` on every per-reviewer checkout before reading citations, restore any tracked edits outside `review_scratch_*`, and flag any dirty reviewer for a full-severity audit.
- For every dedicated focus reviewer (`rev-f*` / `des-f*`), run the [playbook-read check](focus_blocks.md#playbook-read-check) on `## Playbook Feedback` before stripping that section.

## 3. Citation Audit

For every `critical` and `high` finding (and **all** findings from any reviewer that failed the clean check or the playbook-read check):

1. Open the cited `File:Line` or `Target` (±10 lines) on disk in the target workspace/checkout with `read_file`.
2. If a quoted line, symbol, signature, or diagram claim does not match the file on disk, drop the finding or downgrade it to a Question (`Q1…`), and escalate the rest of that reviewer's findings to a full audit regardless of severity.
3. Record `Citation audit: N checked, M dropped (…)` in `## Summary` (plus any working-tree guard note if applicable).

## 4. Reviewer Challenge (`--challenge` only)

When `--challenge` is active and Citation Audit (or a direct read check) refutes a `critical` or `high` finding:

1. Challenge that reviewer, naming the finding ID and quoting the refuting evidence. Ask it to `"withdraw, or reply with evidence at a tier that supports the severity"`.
2. Delivery channel: `post_message` + `swarm_wait{since: <last_seq>}` on Tier 1, `resume: "<agentId>"` on Tier 2a, or `send_message` on Tier 2b.
3. Allow **at most one round trip** per disputed reviewer. A reply that pivots to a different argument without supplying `Executed`, `Read`, `Fetched`, or line-cited `Deduction` evidence for the original claim counts as withdrawn.
4. Record `N disputed (M withdrawn, K upheld)` in `## Summary`.

## 5. Multi-Reviewer Merge

When more than one reviewer ran:

- **Deduplicate overlapping findings**: When two reviewers flag the same issue on the same line/target, keep one finding at the higher surviving severity and append `(raised by both reviewers)` to its `Body`.
- **Keep `out-of-lane` findings** (at `critical` or `high`).
- **Keep `- **Focus**:` fields**: When a general/lane finding and a focus finding merge, the merged finding keeps the `Focus` field.
- **Merge partial assessments**: Combine lane `## Structural Assessment` or `## View & Boundary Assessment` bullets (plus `## Principle Scorecard` rows in `design-review`, taking the lower score backed by a surviving verified finding and merging rationales).
- **Union `## Checked and Found Clean`**: Deduplicate disproved hypotheses, drop any line lacking an allowed tier (`Executed`, `Read`, `Fetched`, `Deduction`) or concrete disproof, and cap at **10 lines**, keeping theme disproofs first.
- **Strip `## Playbook Feedback`**: Read it for Step 5b proposals, and never copy it into the final report.

## 6. Numbering, Questions, and Unified Verdict

- Sort findings by severity and number sequentially within each class: `### C1.`, `### H1.`, `### M1.`, `### L1.`. (In `code-review`, `wording` findings sort last within `low`.)
- Keep at most **3** Questions (`### Q1.` through `### Q3.`), choosing the ones with the highest stated stakes in `Why`. Questions never block the verdict.
- End `## Summary` with the unified verdict line:

  `Verdict: <Ready | Minor Revisions Needed | Critical Blockers>`

  - `Ready`: zero `critical` or `high` findings.
  - `Minor Revisions Needed`: `medium`/`low` refinements (or `high` items that are bounded revisions rather than fundamental blockers).
  - `Critical Blockers`: one or more `critical` blockers (or unresolved `high` blockers that prevent shipping/planning).
