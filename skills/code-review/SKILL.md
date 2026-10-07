---
name: code-review
description: Performs a multi-step review of pull requests, local changes, or whole local files, using iterative refinement (generation, critique, synthesis) to produce actionable feedback. Every finding carries an evidence tier and a consequence; severity is capped by evidence. Reviews code, and reviews documentation, normative specifications, design docs, RFCs, ADRs, and blueprints for structure, correctness, completeness, consistency, and downstream impact rather than wording. Automatically syncs remote PRs to a temporary directory if not present locally. Use when you need to review code changes, pull requests, specs, or design docs thoroughly. Supports --opus (Claude Opus reviewer), --panel (two reviewers with split criteria), --verify (run tests and mutation checks in a throwaway checkout), --max (all three), and --challenge (send refuting evidence back to the reviewer and require a retraction).
---

# Code Review

This skill provides a multi-step, iterative workflow for reviewing code and technical documents. It produces actionable, well-formatted feedback while avoiding common pitfalls of AI-generated reviews (like "looks good" comments or commenting on unchanged lines).

You are an expert Senior Software Engineer specializing in code review and in reviewing technical specifications and designs. Your task is to analyze the changes in a GitHub pull request, a local commit set, or a set of local files, and provide a review. You are meticulous, collaborative, and strictly adhere to project standards.

## Core Principles

- **Review Only, Never Fix**: The review artifact is this skill's only deliverable. Never edit, create, or delete workspace files, even when a fix looks trivial. Never run `git add`, `commit`, `push`, `stash`, `checkout`, or `reset` in the user's workspace. Never post to the PR (`gh pr review`, `gh pr comment`, resolving threads). You may read files, run the commands allowed in [verification.md](references/verification.md), write to the conversation artifact directory, and use the temporary `pr-review-*` checkout. This holds even when the conversation was implementing code just before the review, and even when the request says "review and fix": the review ends at [Step 7](#step-7-stop-and-hand-off), and fixing starts only after the user names the findings to fix.
- **Survival Rate Over Count**: Three findings that all hold up beat nine where one does. Every finding names its evidence tier and its concrete consequence ([evidence.md](references/evidence.md)), and severity is capped by the evidence supplied. A review that tested its hypotheses and found zero defects is a success; never manufacture objections to avoid an empty list.
- **Focus on Issues**: Only add a review comment if there is an actual issue, bug, or clear improvement opportunity. Do not add comments to validate or explain code.
- **Targeted Suggestions**: Anchor findings to lines that are actually modified in the diff.
- **Actionable Feedback**: Provide specific code suggestions, or for documents, replacement text or a specific question.
- **Structure Over Wording in Documents**: For specs, designs, and blueprints, look first for contradictions, undefined behavior, unverifiable requirements, missing rationale, compatibility breaks, and stale downstream artifacts. Wording findings are reported at `low` only.
- **Write Prose**: Follow the principles in the [write-prose](../write-prose/SKILL.md) skill for all written feedback.
- **Consult Domain Skills**: Where specialized skills exist for the codebase or language (e.g., `angular-component`, `typescript-advanced-types`), reference them for best practices.

## Flags

| Flag | Effect | Cost |
| :--- | :--- | :--- |
| *(none)* | 1 Gemini reviewer; tier 1 verification (read-only checks) | Baseline |
| `--opus` | The single reviewer is Opus. Under `--panel`, reviewer B is Opus. | Uses 3P quota |
| `--panel` | 2 reviewers in parallel with non-overlapping criteria (lanes A and B), for code and documents | About 2× subagents |
| `--verify` | Adds tier 2 (targeted tests, repro tests, and reverted mutations in a per-reviewer throwaway checkout) for trusted changes | Test runtime and dependency installs |
| `--max` | Same as `--opus --panel --verify` | All of the above |
| `--challenge` | After Step 5's citation audit, send refuting evidence back to the reviewer and require a withdrawal or new evidence; one round | One extra subagent turn per disputed reviewer |

Expand `--max` first, before anything else runs. `--max` does not imply `--challenge`. Flags combine, and repeating one is harmless, so `--max --opus` equals `--max`. Record the flags in effect after expansion. The report Summary lists them, along with any trust limit.

## Workflow

Follow these steps sequentially to perform a review:

### Step 1: Gather Changes and Prepare Workspace

Before starting the review, identify the changes, pin commit SHAs, and prepare the target workspace.

- **For GitHub Pull Requests**:
  - Use `gh pr view` to inspect the PR title, description, repository, and head branch.
  - **Trust Check** (only needed with `--verify`): run `gh api repos/{owner}/{repo}/pulls/<n> --jq .author_association`. `OWNER`, `MEMBER`, or `COLLABORATOR` means trusted. Any other value, or a failed lookup, means untrusted: tier 2 is off, and no flag overrides that. See [verification.md](references/verification.md#trust-check).
  - **Check Local Branch Presence**: Check if the PR branch exists locally with `git branch --list <branch>` or `git rev-parse --verify <branch>`.
  - **Sync Remote PR to Temporary Directory**:
    - If the PR is not part of the local branch(es) (missing locally or in another repository):
      - Assume the PR branch must be synced to a local temporary directory to conduct the review.
      - Create a dedicated temporary directory with a specific prefix: `pr_review_dir_a=$(mktemp -d -t pr-review-XXXXXX)`.
      - Clone the repository into that directory. If a local checkout exists on disk, use `git clone --reference <path-to-local-repo> <repo-url> "$pr_review_dir_a"` to speed up the clone. Otherwise, use `gh repo clone <owner/repo> "$pr_review_dir_a"`.
      - Check out the PR branch in the temporary directory: `cd "$pr_review_dir_a" && gh pr checkout <pr-number-or-url>`.
      - Perform all subsequent review steps (inspecting diffs, reading context files, evaluating tests) within `$pr_review_dir_a`.
      - Retain the directory path in `$pr_review_dir_a` for safe cleanup in Step 6.
  - Use `gh pr diff` (or `git diff <base>...HEAD` in the checkout) to obtain the changes, and `gh pr diff --name-only` for the file list.
  - _Reference: See the [gh-cli](../gh-cli/SKILL.md) skill for detailed usage._
- **For Local Changes**:
  - Use `git status` to see modified files.
  - Use `git diff --stat` (or `git diff --staged --stat`) first to assess change volume.
  - Get the file list with `git diff --name-only` (unstaged), `git diff --staged --name-only` (staged), or `git diff --name-only <base>...HEAD` (a branch). Add untracked files from `git ls-files --others --exclude-standard` if they are part of the change.
  - Use targeted diffs (`git diff -- <files...>`) and/or `sem diff` for large changesets to prevent context window saturation.
  - Use `git log -p` to see recent commits if reviewing a local branch.
- **For Whole Local Files** (for example, "review this spec"): produce an all-`+` diff with real line numbers using `git diff --no-index /dev/null <file>`. It exits with status 1 when it prints a diff, which is expected. The rest of the workflow treats this like any other diff.
- **Throwaway Checkouts for `--verify`**: For trusted local changes, or a trusted PR whose branch is already checked out locally, create `$pr_review_dir_a` with the working-tree changes applied and commit a review snapshot, following [verification.md](references/verification.md#throwaway-checkout). Under `--panel --verify`, after building `$pr_review_dir_a` (including any dependency install), create `$pr_review_dir_b` with `cp -a "$pr_review_dir_a/." "$pr_review_dir_b/"` per [verification.md](references/verification.md#per-reviewer-checkouts) so each lane gets its own isolated checkout.
- **Pin Base and Head SHAs**: Record `base_sha=$(git merge-base <base> HEAD)` (or `git rev-parse HEAD` for uncommitted working-tree changes against `HEAD`). Set `head_sha` to `git rev-parse HEAD` in the source repo, except for a `--verify` checkout of local working-tree changes, where `head_sha` is `git -C "$pr_review_dir_a" rev-parse HEAD` (the review snapshot commit) and the prompt notes that the top commit is the review snapshot, so `git diff $base_sha $head_sha` inside the checkout covers the change under review.
- **Stage Diff Into Size-Bounded Chunks**: For any diff over 150 lines or 6 KB, create a temporary diff directory (`diff_dir=$(mktemp -d -t pr-review-diffs-XXXXXX)`) and pipe the unified diff into `split_diff.py --grouped`:
  ```bash
  <diff command> | python3 <skill-dir>/scripts/split_diff.py --grouped --output-dir "$diff_dir"
  ```
  This packs per-file diffs (splitting oversized files along hunk boundaries) into `diff_chunk_01.diff`, `diff_chunk_02.diff`, … where each chunk is $\le 35\text{ KB}$ and $\le 700$ lines (fitting in a single `view_file` call) and writes `manifest.json`. Retain `$diff_dir` for cleanup in Step 6.

### Step 1b: Classify Artifacts

Pipe the changed file list into the classifier:

```bash
<file list> | python3 <skill-dir>/scripts/classify_artifacts.py --root <repo-root> -
```

It prints each file's `type` (`code`, `normative-spec`, `design`, `blueprint`, or `reference-doc`), the `rule` that matched, and the `rubric` to use. Group the files into a code partition and a document partition. If a classification is clearly wrong, override it and note why. If there are no document files, skip every document-specific instruction below: code-only reviews work as before. The rules are described in [reviewing_documents.md](references/reviewing_documents.md#when-document-mode-applies).

### Step 2: Context Enrichment, Tier 1 Verification, and Seed Hypotheses

Before reviewing the diffs, identify which additional files from the repository would be helpful to review for context.
When reviewing a PR synced to a temporary directory, inspect the files directly in that temporary checkout.
Consider:

- Files that are imported or referenced.
- Parent classes or interfaces.
- Related utility files.
- Test files corresponding to changed files.
- **For document files**: the files each changed document links to, the files that link to it (search for its repo-relative path), the governing spec or schema, and the code or tests the document describes.

Then run **tier 1 verification** yourself, so the reviewer can spend its effort on the review. Tier 1 is read-only and always on. It covers CI status and failed-job logs for PRs, analyzers and linters in check mode on the changed files, toolchain dry-run or task-graph queries, and repo doc validators for document files. Use only tools that are already installed. For in-place runs, compare `git status --porcelain` before and after. Collect the output for the reviewer prompt (cap inline output at 1 KB, failing lines first; if longer, write the full output to `$diff_dir/tier1.log`, creating `$diff_dir` with `mktemp -d -t pr-review-diffs-XXXXXX` if needed, and cite the path). Commands and rules are in [verification.md](references/verification.md#tier-1-commands).

Next, write **a few Seed Hypotheses** from the diff stat, the context files, and the tier 1 output—specific, falsifiable claims (one line each) per [evidence.md](references/evidence.md#hypotheses). Under `--panel`, label each hypothesis with its lane (`[A]` for correctness, state, edge cases, and tests; `[B]` for wiring, security, API, performance, and dependencies). Under `--verify`, mark which hypotheses a mutation run can settle. For deletion-heavy diffs, include hypotheses checking whether deleted symbols or deleted test coverage still exist in `HEAD` ([splitting_reviews.md](references/splitting_reviews.md#4-deletion-heavy-diffs)).

- **Ruled out by the caller**: When the invoking prompt supplies hypotheses already disproved in a prior pass (for example, the `ship-it` review loop passing the previous pass's `## Checked and Found Clean` lines), include them under the seeds as `Ruled out last pass: …`. Instruct the reviewer to re-test a ruled-out line only if the current diff touches the lines it cites.

_Reference: Use the guidelines in [splitting_reviews.md](references/splitting_reviews.md) if the review needs to be subdivided._

### Step 3: Generate Initial Review

Before analyzing the diff, determine the reviewer subagents based on the flags:

- **Reviewer Selection**:
  - **Gemini Review (Default)**: By default, delegate Steps 3 & 4 to one `gemini-code-reviewer` (pinned to Gemini 3.8 Flash High).
  - **Opus Review (`--opus`)**: If the user passed `--opus`, requested Claude, or asked for a cross-model review, delegate Steps 3 & 4 to `opus-code-reviewer` (pinned to Claude Opus 5.5 Max, drawing from `3p-daily` quota).
    - _Quota Fallback_: If `opus-code-reviewer` fails due to 3P quota or capacity limits, report the issue to the user and fall back to `gemini-code-reviewer`.
  - **Panel Review (`--panel`)**: Launch two reviewers in one `invoke_subagent` call, one per lane. See [Panel Review](#panel-review---panel) below.

- **Packaging the Subagent Prompt (Strict Invariants)**:
  - **No Dummy Probes**: Never launch a subagent with a test or placeholder prompt (e.g. "Test if agent starts"). Always supply the full review payload directly in the initial `invoke_subagent` prompt.
  - **Programmatic Diff Staging & Prompt Size Guard**: Keep total `invoke_subagent` prompts under 10 KB so the platform never truncates them:
    - Always place all instructions (`### Target Workspace`, `### Context & Key Reference Files`, `### Tier 1 Verification Results`, `### Hypotheses`, `### Review Mandate`, `### Document Review Mandate`, `### Lane Assignment`, `### Verification Mandate`, and `### Report Format`) **before** `### Modified Code Diff`.
    - Cap `### Tier 1 Verification Results` at 1 KB inline (failing lines first; stage full logs to `$diff_dir/tier1.log`).
    - For small diffs ($\le 150$ lines and $\le 6\text{ KB}$), embed the unified diff inline at the bottom of the prompt under `### Modified Code Diff`.
    - For larger diffs (> 150 lines or > 6 KB), do **not** paste the raw diff inline into `invoke_subagent`. Instead, list the generated `diff_chunk_XX.diff` absolute paths and covered files from `split_diff.py --grouped` under `### Modified Code Diff` and instruct the reviewer subagent to read every listed `.diff` chunk file with `view_file` before evaluating the changes.
  - **Explicit Workspace & Context Paths**: Explicitly specify the target workspace root directory (`$pr_review_dir_a`, `$pr_review_dir_b`, or local worktree path) and list the relevant context files identified in Step 2 (imports, interfaces, tests, linked documents) so the reviewer can read them via `view_file` if needed.
  - **Absolute Reference Paths**: Give reference and rubric files as absolute paths under this skill's directory, so the reviewer can open them.
  - **Mixed Diffs**: Use one prompt per reviewer. Put the code mandate first, then the Document Review Mandate. The document mandate adds to the code review and replaces none of it: code files get the same depth as in a code-only review.
  - **Large Diffs**: For very large diffs (> 500 lines or > 10 files), either pass the grouped `diff_chunk_XX.diff` files from `split_diff.py --grouped` or partition the review across multiple subagents (see [splitting_reviews.md](references/splitting_reviews.md)).

- **Subagent Invocation Template**:
  Invoke the chosen subagent via `invoke_subagent` with `TypeName` set to the reviewer (`gemini-code-reviewer` or `opus-code-reviewer`), `Role` set to `Code Reviewer` (or `Code Reviewer A` / `Code Reviewer B` under `--panel`), `Model` set to `inherit`, and this `Prompt`. Include each optional block only when its condition holds:

  `````markdown
  Please conduct a deep review of PR #<number> (<title>) following the `code-review` skill criteria. Read `<skill-dir>/references/evidence.md` first and apply its evidence tiers, severity caps, and falsification pass.

  Do not modify, create, or delete any file in the workspace, and do not run git or gh commands that change state. Your only write is the review report. Do not spawn subagents.

  ### Target Workspace
  - **Workspace Root**: `<absolute_path_to_pr_review_dir_a_or_b_or_worktree>`
  - **Base Ref**: `<base_sha>`
  - **Head Ref**: `<head_sha>` (note if top commit is the review snapshot)

  ### Context & Key Reference Files
  <list of key imported files, parent classes, test files, and linked documents identified in Step 2>

  ### Tier 1 Verification Results
  <up to 1 KB of CI/analyzer/validator output (failing lines first), or path to $diff_dir/tier1.log, or "None run: <reason>">

  ### Hypotheses
  Try to prove each of these. Report every one as confirmed (a finding), disproved (Checked and Found Clean), or unresolved (a Question). Add up to 5 of your own before reading the diff in detail.
  1. <seeded hypothesis 1>
  2. <seeded hypothesis 2>
  3. <seeded hypothesis 3>
  4. ...
  <Optional: Ruled out last pass: ... (re-test only if this diff touches the cited lines)>

  ### Review Mandate
  Execute Step 3 (Generate Initial Review) and Step 4 (Critique and Refine):
  1. Evaluate the modified code against correctness, concurrency/failure modes, edge cases, maintainability, and security (at most 25 file reads outside the diff and listed context; at most 3 network fetches).
  2. Apply critique and truth filters: comment only on modified lines (+/-), omit compliments, provide compilable replacement snippets with matching indentation, and give every finding `Evidence` with a tier and `Why` with a concrete consequence.

  ### Document Review Mandate  (include only when the diff has document files)
  These files are documents. Review them for structure, correctness, completeness, consistency, verifiability, and downstream impact. Report wording and grammar at `low` only.
  - Files and rubrics: <path — type — absolute rubric path, one per document file>
  - Read `<skill-dir>/references/reviewing_documents.md` first, then each listed rubric.
  - Apply the lenses in order: Implementer, Tester / Verifier, Consumer, Operator / Adversary.
  - Every document finding has a `Category`, a `Why`, and an `Evidence` field (`Read` or `Fetched`) quoting the text (both sides for an inconsistency). Findings without quoted evidence are dropped.
  - Run the downstream impact pass (at most 10 files outside this diff) and fill in the `## Structural Assessment` section. Each lens verdict names what you checked; a bare "no issues" is not a verdict.

  ### Lane Assignment  (include only under --panel; see "Panel Review")
  <lane A or lane B block>

  ### Verification Mandate  (include only under --verify for a trusted change)
  - Your checkout: `<pr_review_dir_a or pr_review_dir_b>`. This checkout is reserved for you alone. Run tier 2 commands only there.
  - Allowed: targeted existing tests, `review_scratch_*` repro tests, reverted single-line mutations, and dependency installs inside the checkout.
  - At most 8 commands in total, at most 3 of them mutations (`critical`/`high` falsification first). Every run counts. Before each command, name the finding or hypothesis it checks. Never run anything on the "Never run" list in `<skill-dir>/references/verification.md`.
  - Mutation discipline: apply one mutation at a time, run only the targeted test, record `Evidence: Executed`, then `git restore <path>` so `git status --porcelain` shows only `review_scratch_*` files.
  - Give confirmed findings `Evidence: Executed — <command> — <trimmed output, <=15 lines>`. Drop disproved findings and record them in `## Checked and Found Clean`.

  ### Report Format
  Send your report via send_message using this structure:

  ## Summary
  <1-2 paragraphs summarizing changes and verdict: Ready / Minor Revisions Needed / Critical Blockers>

  ## Structural Assessment  (only when there are document files)
  - **Implementer**: <one-line verdict>
  - **Tester / Verifier**: <one-line verdict>
  - **Consumer**: <one-line verdict>
  - **Operator / Adversary**: <one-line verdict>
  - **Repo validators**: <command> — <result>
  - **Downstream impact**: <artifact> — <updated in this diff | needs update | not affected>

  ## Changed Files Summary
  - `<file_path>`: <past-tense summary: Added / Updated / Refactored ...>

  ## Review Comments (Ordered by Severity)
  Put every finding under a `### <ID>. <short claim>` heading (`C1…`, `H1…`, `M1…`, `L1…`):
  ### H1. <short claim>
  - **File**: `<path/to/file>`
  - **Line**: `<line number in the new file>`
  - **Severity**: `<critical | high | medium | low>`
  - **Category**: `<document category>` (document files only)
  - **Evidence**: `<Executed | Read | Fetched | Deduction | Speculation>` — <command and trimmed output, quoted text with path:line, or line-cited trace>
  - **Body**: `<explanation of the defect>`
  - **Why**: `<concrete consequence if shipped as-is>`
  - **Suggestion**: (Optional drop-in code snippet, replacement text, or specific question)

  ## Questions
  At most 3 (`Q1…`); omit if none:
  ### Q1. <claim phrased as a question>
  - **File**: `<path/to/file>`
  - **Line**: `<line number in the new file>`
  - **Evidence**: `<Speculation | Deduction>` — <what was checked>
  - **Why**: `<If true, consequence and severity>`
  - **To settle**: `<exact file, symbol, or command>`

  ## Checked and Found Clean
  At most 10 lines; one per disproved hypothesis:
  - <hypothesis>: `<Executed | Read | Fetched | Deduction>` — <disproving evidence>

  ## Recommendations
  - <Key actionable feedback 1>
  - <Key actionable feedback 2>

  ### Modified Code Diff
  <For diffs <= 150 lines and <= 6 KB, embed inline:>
  ````diff
  <full_unified_diff>
  ````
  <For diffs > 150 lines or > 6 KB, list the staged chunk files from `split_diff.py --grouped` instead of pasting inline:>
  Read every staged diff chunk file below with `view_file` before evaluating the changes (<= 700 lines and <= 35 KB per chunk):
  - `/tmp/pr-review-diffs-XXXXXX/diff_chunk_01.diff` (`<lines>` lines, `<bytes>` bytes): `<covered_files>`
  `````

  _Note: Both subagents configure `disableModelSelection: true`, preserving their pinned models regardless of caller model._

The reviewer subagent evaluates code changes against the following criteria:

- **Correctness**: Verify functionality, handle edge cases, check API usage.
- **Efficiency**: Identify bottlenecks, redundant calculations.
- **Maintainability**: Assess readability, adherence to style guides, and evaluate control flow complexity via the [cognitive-complexity](../cognitive-complexity/SKILL.md) skill.
- **Security**: Identify potential vulnerabilities.
- **API Soundness**: If code files modify public API surfaces, signatures, or configuration patterns (e.g. exported classes, functions, REST/RPC endpoints, package/module public exports), also launch a subagent running the [api-review](../api-review/SKILL.md) skill on those files, in the same `invoke_subagent` call as the reviewers, and merge its findings in Step 5. Document files don't trigger this. Schema compatibility is covered by the spec rubric.

Document files are evaluated with the four lenses and the rubrics in [reviewing_documents.md](references/reviewing_documents.md).

**Guidelines**:

- Use the vetted criteria in [review_criteria.md](references/review_criteria.md) and the evidence tiers and falsification rules in [evidence.md](references/evidence.md).
- For reviewing test code, refer directly to [reviewing_tests.md](references/reviewing_tests.md).
- Reference external standards where applicable:
  - For API design, refer to the canonical API design guidelines in the [api-review](../api-review/SKILL.md) skill.
  - For documentation, refer to the [code-documentation](../code-documentation/SKILL.md) skill.
  - For code complexity and maintainability standards, refer to the [cognitive-complexity](../cognitive-complexity/SKILL.md) skill.
- **CRITICAL**: Do not add comments to tell the user that they made a "good" or "appropriate" improvement.

#### Panel Review (`--panel`)

`--panel` means the user accepts about twice the subagent quota for better coverage. Launch two reviewers in one `invoke_subagent` call. Both get the full diff and the same optional blocks. Each also gets a Lane Assignment block, so their criteria don't overlap:

| Lane | Theme | Code criteria | Document lenses | Agent |
| :--- | :--- | :--- | :--- | :--- |
| **A** | Does it work, and can it be verified? | Correctness; concurrency and failure modes; edge cases; tests ([reviewing_tests.md](references/reviewing_tests.md)) | Implementer; Tester / Verifier | `gemini-code-reviewer` |
| **B** | Is it safe to ship and to maintain? | Security; maintainability and cognitive complexity; efficiency; API soundness | Consumer; Operator / Adversary | `gemini-code-reviewer`, or `opus-code-reviewer` with `--opus` (same quota fallback as above) |

Lane Assignment block for reviewer A (swap in lane B's lists for reviewer B):

```markdown
You are reviewer A of two. Reviewer B covers the other criteria in parallel.
- Code criteria: Correctness; Concurrency & failure modes; Edge cases; Tests.
- Document lenses: Implementer; Tester / Verifier.
Hypotheses marked with your lane are yours. Report only findings in these areas. Exception: report a `critical` or `high` issue outside them, and tag its Body with `out-of-lane`.
In the Structural Assessment, fill in only your own lenses.
```

- For a code-only diff, omit the document lenses line. For a document-only diff, omit the code criteria line.
- For a split diff, launch one A/B pair per partition. Before launching, state the total subagent count in chat (for example, "Launching 6 reviewers: 3 partitions × 2 lanes").

### Step 4: Critique and Refine (Review the Review)

The reviewer subagent performs a self-critique pass on its generated comments before returning them:
Filter out or modify comments based on the rules in [critique_rules.md](references/critique_rules.md), including its Truth Filters, Questions, Document Findings, and Panel Runs sections.
Ensure that:

- Comments are anchored to lines that begin with `+` or `-` in the diff. Document findings may quote unchanged text as evidence.
- Comments are not merely informational or complimentary.
- Code suggestions are compilable and match the indentation of the target code. Document suggestions are drop-in replacement text or a specific question.
- Every finding carries an `Evidence` tier (`Executed`, `Read`, `Fetched`, `Deduction`, or `Speculation`, never `Recalled`) that satisfies the severity cap in [evidence.md](references/evidence.md#severity-caps).
- Every finding carries a concrete `Why` stating what breaks, corrupts, leaks, or fails.
- Claims of deleted code, missing coverage, API misuse, or incidental test assertions were checked against `HEAD` and the contract before reporting.

### Step 5: Synthesis (Final Review)

Audit and combine the refined comments into the final output:

- **Clean check (`--verify`)**: Before reading citations, run `git -C "$dir" status --porcelain` on `$pr_review_dir_a` (and `$pr_review_dir_b` under `--panel --verify`) per [verification.md](references/verification.md#orchestrator-clean-check). If anything other than `?? review_scratch_*` appears, restore tracked files (`git -C "$dir" restore .`), remove unexpected untracked files (`git -C "$dir" clean -fd -e 'review_scratch_*'`), note the leftover edit in the Summary, and audit all findings from that reviewer below.
- **Citation audit**: For every `critical` and `high` finding (and all findings from a reviewer that failed the clean check), open the cited `path:line` (±10 lines) in the review workspace with `view_file`. If a quoted line, symbol, or signature does not match the file on disk, drop the finding or downgrade it to a Question, and audit the rest of that reviewer's findings regardless of severity. Record `"Citation audit: N checked, M dropped (…)"` in the Summary. If the working-tree guard caught an in-place tier 1 change, note that in the Summary as well.
- **Reviewer challenge (`--challenge` only)**: When `--challenge` is active and the citation audit or a quick disproof check refutes a `critical` or `high` finding, send one `send_message` to that reviewer naming the finding ID, quoting the refuting file lines or command output, and asking it to `"withdraw, or reply with evidence at a tier that supports the severity"`. Allow at most one round trip per disputed reviewer. A reply that pivots to a different argument without supplying `Executed`, `Read`, `Fetched`, or line-cited `Deduction` evidence for the original claim counts as withdrawn. Note `"N disputed (M withdrawn, K upheld)"` in the Summary.
- Deduplicate overlapping comments.
- **Panel merge**: When both lanes flag the same issue on the same line, keep one finding at the higher severity and add "(raised by both reviewers)" to its Body. Keep `out-of-lane` findings. Merge the two partial Structural Assessments into one.
- Prioritize high-severity issues (critical, high).
- **Number findings and Questions**: Put every finding under a `### <ID>. <short claim>` heading: `C1`, `C2`… for critical, `H1`… for high, `M1`… for medium, `L1`… for low, so the user can say "fix H2 and M1" later. Wording findings sort last within `low`. Number Questions in `## Questions` as `### Q1. <question>` through `Q3` (keep at most 3, choosing the ones with the highest stated stakes in `Why`).
- **Checked and Found Clean merge**: Take the union of disproved hypotheses from the reviewer(s), deduplicate, drop any line that lacks a tier or disproving evidence, and cap at 10 lines.
- **Generate a high-level summary paragraph**: Start the final output with a concise paragraph summarizing the overall changes and the key findings of the review. Include the citation audit tally (and `--challenge` tally when used), and end with the flags in effect (for example, "Flags: `--opus --panel --verify`") and any trust limit (for example, "`--verify` limited to tier 1: PR author is not a repo collaborator").
- **Structural Assessment**: When the diff has document files, put the `## Structural Assessment` section right after the Summary.
- **Generate file summaries**: For reviews with multiple files, include `## Changed Files Summary` with a single, concise sentence describing the change in each (starting with a past-tense verb like 'Added', 'Updated').
- **Generate a recommendations section**: Summarize the key actionable recommendations in `## Recommendations` after `## Checked and Found Clean`.
- When writing file paths, write them as Markdown links.
- Ensure the final output is cohesive and follows the [`write-prose`](../write-prose/SKILL.md) skill. For long review artifacts or RFC reviews, run `write-prose`'s `analyze_prose.py` script to audit readability metrics.
- **Save and Lint Artifact**: Write the synthesized review to `review_results.md` in the conversation artifact directory (`<appDataDir>/brain/<conversation-id>/`) using `write_to_file` (never into the temporary checkout). Then run:
  ```bash
  python3 <skill-dir>/scripts/lint_review.py <artifact-dir>/review_results.md
  ```
  If `lint_review.py` exits non-zero, fix the reported formatting or severity-cap violations in `review_results.md` and rerun until it exits `0` before proceeding to Step 6. Do not emit the full review directly into the chat response.

### Step 6: Workspace Cleanup

If any temporary directory was created for reviewing the PR, for `--verify` checkouts, or for staged diff chunks:

- If the shell working directory was changed to `$pr_review_dir_a` or `$pr_review_dir_b`, return to the original working directory before deleting the checkouts.
- Verify each directory path is non-empty, exists, and matches the expected temporary prefix before removal:
  ```bash
  for dir in "${pr_review_dir_a:-}" "${pr_review_dir_b:-}"; do
    if [[ -n "$dir" && -d "$dir" && "$dir" == *"/pr-review-"* ]]; then
      rm -rf "$dir"
    fi
  done
  if [[ -n "${diff_dir:-}" && -d "$diff_dir" && "$diff_dir" == *"/pr-review-diffs-"* ]]; then
    rm -rf "$diff_dir"
  fi
  ```
- Confirm the temporary directories are removed before concluding the task.
- Run this cleanup step even if errors or early exits occur during the review.

### Step 7: Stop and Hand Off

Send one short chat message, then **end the turn**. The message contains:

- A link to the review artifact.
- The verdict (Ready / Minor Revisions Needed / Critical Blockers).
- Finding counts by severity, plus question count (for example, "1 critical, 3 high, 2 medium, 4 low, 2 questions"). Questions never block the verdict.
- One closing line: "Which findings should I address? (for example, `fix H1–H3` or `fix all high`)".

That closing line is a question for the user. It is never a reason to start fixing. Do not edit files, stage changes, or start a fix plan in the same turn.

- **Subagent wake-up rule**: When a reviewer subagent's completion message resumes you, the only steps left are Step 5 (synthesis), Step 6 (cleanup), and Step 7 (stop). This holds even if the conversation was implementing code before the review started.
- **"Review and fix" requests**: Treat them as two requests. The review still ends here. Fixing starts in a later turn, after the user confirms which findings to address.
- **Callers**: A calling workflow whose own instructions say to act on findings (for example, the `ship-it` review loop) may do so after this skill returns. The review step itself never fixes anything.

## Output Format

The final synthesized review MUST be created as an artifact file using the `write_to_file` tool in the conversation's artifact directory (e.g., `review_results.md`) with `UserFacing: true`.

- **Do NOT** print the full review comments or dump the complete review markdown in the chat message response.
- In the final chat response, provide only the Step 7 hand-off message: a clickable markdown link to the artifact, the verdict, the finding and question counts, and the closing question.
- End the turn after the hand-off message. Do not continue to fix anything in the same turn.
- If executing within a subagent, ensure the artifact file is written using `write_to_file` before completing and returning the artifact link to the caller.
- Clean up any temporary directories created during the review before returning the final response.

The review file sections appear in this order:

1. `## Summary`: high-level summary paragraph, citation audit tally, and the flags in effect.
2. `## Structural Assessment` (only when the diff has document files).
3. `## Changed Files Summary` (if applicable).
4. `## Review Comments (Ordered by Severity)`: findings under `### <ID>. <short claim>` headings (`C1`, `H1`, `M1`, `L1`).
5. `## Questions`: up to 3 unresolved questions under `### Q1. <claim as a question>` headings (omit section or write `None.` if none).
6. `## Checked and Found Clean`: up to 10 disproved hypotheses, each with its evidence tier and disproof.
7. `## Recommendations`: key actionable feedback.

Each finding in `## Review Comments` specifies:

- **File**: The path to the file.
- **Line**: The line number in the new file (anchored to a changed line).
- **Severity**: `critical`, `high`, `medium`, or `low`.
- **Category**: (Document files only) One of the categories in [reviewing_documents.md](references/reviewing_documents.md#finding-categories).
- **Evidence**: The tier (`Executed`, `Read`, `Fetched`, `Deduction`, or `Speculation`) followed by `—` and the command output, quoted text with `path:line`, or line-cited trace.
- **Body**: The explanation of the defect.
- **Why**: The concrete consequence if shipped as-is.
- **Suggestion**: (Optional) The specific code replacement, replacement text, or question.

Each entry in `## Questions` specifies `File`, `Line`, `Evidence`, `Why`, and `To settle` (the exact file, symbol, or command that answers it).
