# PR Review Guidelines

These review guidelines ensure feedback is actionable, objective, and respectful of the author's effort.

## Core Review Principles

1. **Focus Exclusively on Actionable Findings**:
   - Only leave a review comment if there is a concrete bug, logic error, performance issue, security concern, test gap, or clear maintainability issue.
   - Do **NOT** leave purely complimentary comments (e.g. "Looks good!", "Nice refactor!"). Compliments in line comments create notification noise and clutter the PR review timeline.
   - Do **NOT** explain what the code does or echo back the author's logic without suggesting a change.

2. **Respect Author Agency & Style**:
   - If a code style choice is not governed by an existing linter or documented project style guide, do not enforce personal stylistic preferences.
   - Categorize comments honestly:
     - `🔥 Critical`: Security vulnerabilities, data corruption, severe memory leaks, or broken tests.
     - `⚠️ High`: Logic bugs, unexpected runtime exceptions, or missing error handling.
     - `💡 Medium`: Performance optimizations, suboptimal data structures, or missing test assertions.
     - `🌱 Low`: Minor clarity improvements, dead code removal, or documentation typos.

3. **Provide Concrete Code Suggestions**:
   - Whenever proposing a replacement for fewer than 10 lines of code, use GitHub suggestion syntax:
     ````markdown
     ```suggestion
     replacement code
     ```
     ````
   - Ensure the indentation matches the target file's indentation exactly.

4. **Verify Outside-Diff Context**:
   - Read referenced classes, interfaces, and callers to verify whether assumptions in the PR hold.
   - If an issue exists on lines outside the diff hunk, note it in the top-level review summary rather than attempting an invalid inline comment.

5. **State the Problem Before the Fix**:
   - Each comment is read on its own, next to the code. Open with what is wrong and what it causes in 1 to 3 short sentences, then give the fix or suggestion block.
   - Never restate what the diff lines already show; the author can see the diff right above the comment.
   - Keep each comment's visible prose (`Body`, `Why`, and any prose `Suggestion` or lead-in) within 120 words and 6 sentences ($\le 30$ words per sentence, $\text{PCC} \le 5$), with no em-dashes or AI filler words (see [`write-prose`](../../write-prose/references/audiences.md)). Code blocks do not count toward the word cap, and suggestion blocks stay visible outside the fold.
   - Put optional supporting reasoning in `- **Details**:` (up to 150 words, may continue on indented lines, same sentence and prose rules) for root-cause traces, affected callers, or rejected alternatives. Keep `Body` and `Why` self-contained so the comment still reads clearly without opening the fold.
   - The exporter appends a collapsed `<details>` fold at the end of `draft_comment` containing the `Evidence` detail, optional `Details` prose, the `Repro test` code block, and (for questions) `To settle`. The fold is omitted when all of those are empty.
   - Keep `description` to one short sentence naming the problem for the dashboard card header; `launch_dashboard.py` does not prepend `**{description}**` onto non-empty `draft_comment` text.

6. **Keep the Review Summary Short**:
   - Write 1-2 short paragraphs: what the PR does, the overall assessment, and the reason for the verdict.
   - Don't list the individual findings. The inline comments already carry them, and approved outside-diff findings are appended under "Broader Context" automatically.

---

## Evaluation Categories

### 1. Correctness & Edge Cases
- Are null/undefined values handled where appropriate?
- Are off-by-one errors present in loops, slicing, or boundary checks?
- Are resources (file handles, streams, subscriptions, network sockets) properly disposed of?
- Are asynchronous operations properly awaited or handled?

### 2. Security
- Is untrusted input sanitized before database queries, shell execution, or DOM insertion?
- Are authentication or permission checks enforced at trust boundaries?
- Are sensitive secrets or keys hardcoded into source files or test fixtures?

### 3. API Soundness & Breaking Changes
- Are public signatures modified in a way that breaks existing callers?
- Are parameters, return types, or default arguments changed compatibly?
- Are newly introduced symbols well-documented?

### 4. Maintainability & Complexity
- Are functions doing too many things at once?
- Does the change introduce deep nesting or high cognitive complexity?

### 5. Testing
- Do new features or bug fixes have corresponding automated unit or integration tests?
- Do tests assert specific expected outcomes rather than merely running code without assertions?
- Are edge cases (empty lists, negative values, timeouts, network failures) covered?

---

## Blind-First PR Description & Intent Reconciliation

To give the human reviewer an unbiased view of what a PR actually does before they read the author's framing, generate the PR description in two stages.

### Stage 1: Blind Diff Description (`blind_description.md`)

Derive `blind_description.md` **solely** from `pr.diff` and `pr_coords.json` (including `security_prescan`) before fetching the PR title, description, or comments. Read a file in the temporary checkout only when `security_prescan.signals` names it; the code-review workers read surrounding code later:

```markdown
## Summary
<Up to 3 sentences covering what the code changes do and why they appear to exist, based strictly on the diff.>

## Quality Assessment
<1 sentence on code structure, test coverage, complexity, and adherence to codebase patterns.>

## Security & Execution Gate
- **Gate Verdict**: `SAFE_FOR_LOCAL_EXECUTION` (static pre-scan clean)
<!-- When the verdict is not SAFE_FOR_LOCAL_EXECUTION or security_prescan.signals is non-empty, use full bullets instead:
- **Gate Verdict**: `<SAFE_FOR_LOCAL_EXECUTION | REQUIRES_USER_CONFIRMATION | BLOCKED_UNSAFE>`
- **Static Pre-Scan**: `<clean | N signal(s) across M file(s) audited>`
- **Execution Risk Notes**: <Audit of any flagged build/hook/linter configs, subprocess calls, network access, or test execution risks.>
-->

## Changes
- **`path/to/component`**: <Up to 6 bullets total, grouped by component; tests and changelog share one bullet.>

## Reviewer Focus & Risks
- <Up to 3 bullets, 1 sentence each, naming specific areas, edge cases, or side effects for close inspection.>
```

Keep each Stage 1 section within its budget (checked by `review_engine.py --check-description-budgets`):

| Section | Budget |
| :--- | :--- |
| `## Summary` | Up to 3 sentences covering what changed and why (replaces separate `Summary` and `Inferred Purpose` sections). |
| `## Quality Assessment` | 1 sentence. |
| `## Security & Execution Gate` | 1 line when the verdict is `SAFE_FOR_LOCAL_EXECUTION` and `security_prescan.signals` is empty; full bullets otherwise. |
| `## Changes` | Up to 6 top-level bullets grouped by component. Tests and changelog share one bullet. |
| `## Reviewer Focus & Risks` | Up to 3 top-level bullets, one sentence each. |

### Stage 2: Narrative Context Reconciliation (`pr_description.md`)

Stage 2 runs the unanchored `code-review` pass (Step 3.2, reading `code_review_prep.json`, `pr_coords.json`, and `blind_description.md`) **before** reading `pr_context.json`, `prev_review_results.json`, `prev_review_decisions.json`, or `pr_incremental.diff` in Step 3.3. Once Step 3.2 has produced its initial unanchored findings, compare `blind_description.md` against `pr_context.json` (the author's PR title, body, general comments, reviews, and `threads`) and write `intent_reconciliation.json`. `review_engine.py --reconcile-description` splices it into `pr_description.md` and keeps every Stage 1 section verbatim, so the diff-derived text is never rewritten or softened to match the author's framing. Never edit `blind_description.md` after `--fetch-context`: the splice compares it against the recorded `blind_sha256` and exits 1 on a mismatch.

```json
{
  "alignment": "PARTIAL",
  "callouts": [
    {"section": "Changes", "kind": "WARNING", "text": "The body says only retries changed, but the default timeout also drops from 30s to 5s."},
    {"section": "Summary", "kind": "NOTE", "text": "Author notes this unblocks issue #3810."}
  ],
  "stack_markdown": "",
  "prior_resolution_markdown": ""
}
```

1. **`alignment`** sets the `## Intent Alignment: <Status>` heading at the top of `pr_description.md`:
   - `MATCHES` gives `Matches Stated Purpose`: the code changes match the PR title and description with no unstated side effects or missing claims.
   - `PARTIAL` gives `Partial Divergence`: the code mostly implements the stated purpose but includes unmentioned behavior changes, leaves out a claimed change, or differs in scope.
   - `DIVERGES` gives `Diverges from Stated Purpose`: the code changes contradict the stated purpose or do something substantially different from what the PR description claims.
   - `UNSTATED` gives `Unstated Scope`: the PR description is empty or minimal, or the diff includes significant additional changes not described by the author.
2. **`callouts[]`** places each callout as `> [!<kind>]` directly under the named `section`, which must be an existing `##` heading in `blind_description.md` (for example `Summary`, `Quality Assessment`, `Security & Execution Gate`, `Changes`, or `Reviewer Focus & Risks`):
   - `WARNING`: **Conflict / Unstated Behavior**. The PR description or comments claim X, but the diff does Y; or a file change introduces a behavioral side effect not mentioned in the PR description.
   - `IMPORTANT`: **Unaddressed Review Feedback or Missing Claim**. The PR description claims a fix or test that is absent from the diff, or an open review thread raises a concern that remains unresolved at `HEAD`.
   - `NOTE`: **Helpful Context from PR Body or Discussion**. Background rationale, linked issue context, or design constraints that explain *why* a non-obvious approach was chosen (or note if `threads_complete` is `false` or `latest_head_sha` moved after Stage 1).
3. **`stack_markdown`** and **`prior_resolution_markdown`** are optional. The splice places them after `## Intent Alignment`, in that order.
4. **Put a `## Stack Context` table in `stack_markdown` when `pr_context.json` has a non-null `stack`**. List the members bottom-first (closest to the trunk first), mark the PR under review, and use `gh stack view`'s icons (`✓` merged, `◎` queued, `○` open, `⚠` needs rebase):

   ```markdown
   ## Stack Context

   Stack #3046 (3 PRs on `main`), reviewing 3 of 3.

   | # | PR | Title | State | Base |
   | :-- | :-- | :-- | :-- | :-- |
   | 1 | #3027 | fix(python): follow v1.0 catalog resolution | ⚠ open, needs rebase | `main` |
   | 2 | #3044 | fix(typescript): follow v1.0 catalog resolution | ○ open | #3027 |
   | 3 | **#3045 (this PR)** | fix(dart): follow v1.0 catalog resolution | ○ open | #3044 |

   > [!NOTE]
   > #3027 does not contain the tip of `main`; GitHub will rebase the stack when it merges.
   ```

   The stack shapes the review but is not itself a finding:
   - The diff is this PR's slice on top of its predecessor. Review it on its own terms; code the slice depends on but does not touch belongs to the predecessor's review.
   - When the PR body says a later PR in the stack handles something, confirm it with `gh pr diff <n>` before accepting the claim; if it is not there, use a `> [!IMPORTANT]` callout.
   - A predecessor that is a draft, closed, or `needs_rebase` is a `> [!NOTE]`. A predecessor whose state is `CLOSED` (not merged) means this PR's base will not land; say so in `## Stack Context`.
   - Do not repeat findings that were already raised and resolved on a predecessor PR.
5. **Put a `## Prior Review Resolution` section in `prior_resolution_markdown` when re-reviewing (`review_round > 1`) or when `pr_context.json.threads` is non-empty**. Perform this prior-thread audit and three-way merge in Step 3.3 *after* the Step 3.2 full-diff review (`rev-a-2` or in-session Steps 1 through 4) has already generated its unanchored findings. Audit every prior review thread in `pr_context.json.threads` and any archived finding in `prev_review_results.json` against `pr_incremental.diff` and the current checkout (`HEAD`). Start with a count line, include table rows only for items that are not `Addressed` (`Partially Addressed` or `Unaddressed`), and place `Addressed` items inside a nested `<details>` list:

   ```markdown
   ## Prior Review Resolution

   Compared Round 1 (`4bd4b791`) against Round 2 (`312a49ba`): 1 addressed, 1 partially addressed, 1 unaddressed.

   | Prior Item | Location | Status | Notes |
   | :--- | :--- | :--- | :--- |
   | M1. Race in `dispose()` | `src/client.ts:88` | Partially Addressed | Lock added, but error path skips release |
   | M2. Unused export `LegacyConfig` | `src/index.ts:14` | Unaddressed | Still exported at `HEAD` (open thread) |

   <details>
   <summary>Addressed items (1)</summary>

   - **H1. Missing null guard on `catalogId`** (`src/parser.ts:120`): Fixed in `c7b42a27` with unit test.

   </details>
   ```

   When synthesizing `review_results.md` in Step 3.3 (`code-review` Step 5), reconcile this table with the unanchored Step 3.2 full-diff review (`rev-a-2` or in-session Steps 1 through 4) using the three-way merge rules:
   - **Both Fresh Review and Prior Audit flag the same defect**: Merge into a single finding in `review_results.md` at the higher severity, keeping the fresh review's `HEAD` line anchor and `Evidence` tier. Prefix its heading with `[Unaddressed]`, `[Partially Addressed]`, or `[Unaddressed (open thread)]` and adapt `Body` and `Suggestion` to state what remains broken at `HEAD`.
   - **Only Prior Audit flags an unaddressed or partially addressed item**: Verify it at `HEAD` with `Read` evidence and carry it forward into `review_results.md` as a single finding so reviewer variance never drops an unfixed defect.
   - **Addressed items**: Record in `## Prior Review Resolution` (in `pr_description.md`) and `## Checked and Found Clean` (in `review_results.md`), never as findings.


