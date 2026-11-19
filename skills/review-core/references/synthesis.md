# Synthesis, Citation Audit, Challenge, and Verdicts

Step 5 turns raw reviewer reports into a single lint-clean report artifact.

## 1. Collect Reports (Tier 1 `swarm`)

When a worker wakes `@coordinator`, call `read_board` (passing `since: <last_seq>` on subsequent wakes), read the report file at the path named in the post (`rev_<name>.md` or `rev_des_<name>.md`, or the post body when no path was written), and advance your `last_seq` cursor.

## 2. Clean Check and Playbook-Read Check

- Under `--verify` (in `code-review`), check `git -C "$dir" -c core.hooksPath=/dev/null -c core.fsmonitor=false status --porcelain` on every per-reviewer checkout before reading citations, restore any tracked edits outside `review_scratch_*`, and flag any dirty reviewer for a full-severity audit.
- For every dedicated focus reviewer (`rev-f*` / `des-f*`), run the [playbook-read check](focus_blocks.md#playbook-read-check) on `## Playbook Feedback` before stripping that section.

## 3. Citation Audit

In `code-review`, first run the grounding check on each reviewer report (`--worker` keeps a focus reviewer's `## Playbook Feedback` legal):

```bash
python3 <skill-dir>/scripts/lint_review.py <rev_name.md> --worker --workspace <checkout> --diff-dir "$diff_dir"
```

It checks every finding and question at every severity: the `File` exists, the `Line` touches a changed line or a deletion seam, and each quote in `Evidence: Read` appears within 15 lines of its citation. When the finding still holds after you read the cited file, apply the printed `Fix:` hint (for example, move `Line` to the nearest changed line). Otherwise treat the violation like a failed manual check in step 2 below.

Then, for every `critical` and `high` finding (and **all** findings from any reviewer that failed the clean check, the playbook-read check, or the grounding check):

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

- **Pre-cluster** (in `code-review`): run `python3 <skill-dir>/scripts/lint_review.py --cluster <rev_a.md> <rev_b.md> [...]`. It prints JSON groups of findings and questions that sit in the same file within 15 lines of each other and share wording. Read each group with two or more members and merge it only when the findings describe the same defect. Findings in different files never share a group.
- **Deduplicate overlapping findings**: When two reviewers flag the same issue on the same line/target in the same file, keep one finding at the higher surviving severity and append `(raised by both reviewers)` to its `Body`.
- **Never deduplicate across files**: Do not drop a finding because a finding or a prior review thread on another file has similar wording. Each file keeps its own finding unless the recurring-pattern rule below bundles it.
- **Resolve remove-versus-keep conflicts**: When one reviewer recommends removing code (with `Read` evidence of zero callers or of an upstream invariant that makes it redundant) and another asks to test or refactor the same lines, removal wins when the code is provably dead or duplicate: drop the test or refactor finding. When removal is a judgment call, merge both into one conditional finding, for example "Drop X because ...; if you keep it for Y, add test Z."
- **Keep `out-of-lane` findings** (at `critical` or `high`).
- **Keep `- **Focus**:` fields**: When a general/lane finding and a focus finding merge, the merged finding keeps the `Focus` field.
- **Merge partial assessments**: Combine lane `## Structural Assessment` or `## View & Boundary Assessment` bullets (plus `## Principle Scorecard` rows in `design-review`, taking the lower score backed by a surviving verified finding and merging rationales).
- **Union `## Checked and Found Clean`**: Deduplicate disproved hypotheses, drop any line lacking an allowed tier (`Executed`, `Read`, `Fetched`, `Deduction`) or a [disproof witness](evidence.md#disproof-witness), and cap at **10 lines**, keeping theme disproofs first.
- **Strip `## Playbook Feedback`**: Read it for Step 5b proposals, and never copy it into the final report.

## 6. Numbering, Questions, and Unified Verdict

- Bundle recurring patterns first, with one reviewer or many: when 3 or more surviving findings report the same defect pattern, keep one primary finding at the highest surviving severity, anchored to its strongest site, and list the other sites in its `- **Details**:` field as `Other occurrences: path:line, path:line`.
- Sort findings by severity and number sequentially within each class: `### C1.`, `### H1.`, `### M1.`, `### L1.`. (In `code-review`, `wording` findings sort last within `low`.)
- Keep at most **3** Questions (`### Q1.` through `### Q3.`), choosing the ones with the highest stated stakes in `Why`. Questions never block the verdict.
- End `## Summary` with the unified verdict line:

  `Verdict: <Ready | Minor Revisions Needed | Critical Blockers>`

  - `Ready`: zero `critical` or `high` findings.
  - `Minor Revisions Needed`: `medium`/`low` refinements (or `high` items that are bounded revisions rather than fundamental blockers).
  - `Critical Blockers`: one or more `critical` blockers (or unresolved `high` blockers that prevent shipping/planning).
