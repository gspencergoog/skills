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
   - Each comment is read on its own, next to the code. Open with what is wrong and what it causes in 1–3 short sentences, then give the fix or suggestion block.
   - Never restate what the diff lines already show; the author can see the diff right above the comment.
   - Keep each comment's prose within 80 words and 4 sentences ($\le 30$ words per sentence, $\text{PCC} \le 5$), with no em-dashes or AI filler words (see [`write-prose`](../../write-prose/references/audiences.md)).
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
<1-2 sentences describing what the code changes actually do.>

## Inferred Purpose
<Why this change appears to exist based strictly on the code and test modifications.>

## Quality Assessment
<High-level assessment of code structure, test coverage, complexity, and adherence to codebase patterns.>

## Security & Execution Gate
- **Gate Verdict**: `<SAFE_FOR_LOCAL_EXECUTION | REQUIRES_USER_CONFIRMATION | BLOCKED_UNSAFE>`
- **Static Pre-Scan**: `<clean | N signal(s) across M file(s) audited>`
- **Execution Risk Notes**: <Audit of any flagged build/hook/linter configs, subprocess calls, network access, or test execution risks.>

## Changes
- **`path/to/file.ext`**: <Concrete description of the behavioral or structural change.>

## Reviewer Focus & Risks
- <Specific areas, edge cases, or side effects that warrant close human inspection.>
```

### Stage 2: Narrative Context Reconciliation (`pr_description.md`)

After running `review_engine.py --fetch-context`, compare `blind_description.md` against `pr_context.json` (the author's PR title, body, general comments, reviews, and `threads`) and write `intent_reconciliation.json`. `review_engine.py --reconcile-description` splices it into `pr_description.md` and keeps every Stage 1 section verbatim, so the diff-derived text is never rewritten or softened to match the author's framing. Never edit `blind_description.md` after `--fetch-context`: the splice compares it against the recorded `blind_sha256` and exits 1 on a mismatch.

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
2. **`callouts[]`** places each callout as `> [!<kind>]` directly under the named `section`, which must be an existing `##` heading in `blind_description.md` (for example `Summary`, `Inferred Purpose`, `Quality Assessment`, `Security & Execution Gate`, or `Changes`):
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
5. **Put a `## Prior Review Resolution` table in `prior_resolution_markdown` when re-reviewing (`review_round > 1`) or when `pr_context.json.threads` is non-empty**. Audit every prior review thread in `pr_context.json.threads` and any archived finding in `prev_review_results.json` against `pr_incremental.diff` and the current checkout (`HEAD`):

   ```markdown
   ## Prior Review Resolution

   Compared Round 1 (`4bd4b791`) against Round 2 (`312a49ba`).

   | Prior Item | Location | Status | Notes |
   | :--- | :--- | :--- | :--- |
   | H1. Missing null guard on `catalogId` | `src/parser.ts:120` | Addressed | Fixed in `c7b42a27` with unit test |
   | M1. Race in `dispose()` | `src/client.ts:88` | Partially Addressed | Lock added, but error path skips release |
   | M2. Unused export ` LegacyConfig` | `src/index.ts:14` | Unaddressed | Still exported at `HEAD` (open thread) |
   ```

   When synthesizing `review_results.md` in Step 3.3, reconcile this table with the fresh full-diff review (`rev-a-2`) using the three-way merge rules:
   - **Both Fresh Review and Prior Audit flag the same defect**: Merge into a single finding in `review_results.md` at the higher severity, keeping the fresh review's `HEAD` line anchor and `Evidence` tier. Prefix its heading with `[Unaddressed]`, `[Partially Addressed]`, or `[Unaddressed (open thread)]` and adapt `Body` and `Suggestion` to state what remains broken at `HEAD`.
   - **Only Prior Audit flags an unaddressed or partially addressed item**: Verify it at `HEAD` with `Read` evidence and carry it forward into `review_results.md` as a single finding so reviewer variance never drops an unfixed defect.
   - **Addressed items**: Record in `## Prior Review Resolution` (in `pr_description.md`) and `## Checked and Found Clean` (in `review_results.md`), never as findings.


