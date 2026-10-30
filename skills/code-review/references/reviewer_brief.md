# Reviewer Brief

This reference holds the brief that the [`code-review`](../SKILL.md) orchestrator sends to every reviewer in Step 3: the packaging rules first, then the template. The roster, lane blocks, and focus blocks it refers to are in [lanes.md](lanes.md).

## Packaging Rules (Strict Invariants)

- **No Dummy Probes**: Never launch a swarm worker or subagent with a test or placeholder prompt (e.g. "Test if agent starts"). Always supply the full review payload directly in the initial `brief` or `Prompt`.
- **Programmatic Diff Staging & Prompt Size Guard**: Keep total briefs and subagent prompts under 10 KB so the platform never truncates them:
  - Always place all instructions (`### Target Workspace`, `### Context & Key Reference Files`, `### Tier 1 Verification Results`, `### Hypotheses`, `### Review Mandate`, `### Document Review Mandate`, `### API Review Mandate`, `### Lane Assignment`, `### Focus Priority` or `### Focus Mandate`, `### Verification Mandate`, and `### Report Format`) **before** `### Modified Code Diff`.
  - Cap `### Tier 1 Verification Results` at 1 KB inline (failing lines first; stage full logs to `$diff_dir/tier1.log`).
  - For small diffs ($\le 150$ lines and $\le 6\text{ KB}$), embed the unified diff inline at the bottom of the prompt under `### Modified Code Diff`.
  - For larger diffs (> 150 lines or > 6 KB), do **not** paste the raw diff inline. List the `diff_chunk_XX.diff` paths and covered files from `stage_diffs.py --grouped` under `### Modified Code Diff`, and instruct the reviewer to read each chunk with `view_file`.
- **Explicit Workspace & Context Paths**: Specify the root directory of the workspace (`$pr_review_dir_a`, `$pr_review_dir_b`, or worktree path). List the context files from Step 2 (imports, interfaces, tests, linked documents) so the reviewer can read them via `view_file`.
- **Absolute Reference Paths**: Give reference and rubric files as absolute paths under this skill's directory, so the reviewer can open them.
- **Mixed Diffs**: Use one prompt per reviewer. Put the code mandate first, then the Document Review Mandate. The document mandate adds to the code review and replaces none of it: code files get the same depth as in a code-only review.
- **Large Diffs**: For diffs over 500 lines or 10 files, pass the grouped `diff_chunk_XX.diff` files from `stage_diffs.py --grouped` or split across reviewer lanes ([splitting_reviews.md](splitting_reviews.md)).

## Brief Template

Pass this payload as `brief` in `swarm{action: "add"}` (or `post_message` when waking a worker) on Tier 1. On Tier 2, pass it as `Prompt` in `Agent`/`Task` or `invoke_subagent`, including each optional block only when its condition holds:

`````markdown
Please conduct a deep review of PR #<number> (<title>) following the `code-review` skill criteria. Read `<skill-dir>/references/reviewer_mandate.md` and `<skill-dir>/references/evidence.md` first and apply their evidence tiers, severity caps, and falsification pass.

Do not modify, create, or delete any file in the workspace, and do not run git or gh commands that change state. Your only write is the review report. Do not spawn subagents and never call `swarm` to add helpers (your `<swarm_worker>` helper-spawning instructions do not apply to this review). Under `--panel`, do not call `read_board` during your initial review pass.

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
<Optional, when a focus reviewer covers a theme: A focus reviewer covers <theme-id>; still report critical or high issues in it, tagged out-of-lane.>

### Review Mandate
Execute Step 3 (Generate Initial Review) and Step 4 (Critique and Refine) per `<skill-dir>/references/reviewer_mandate.md`:
1. Evaluate the modified code against correctness, concurrency/failure modes, edge cases, maintainability, and security (at most 25 file reads outside the diff and listed context; at most 3 network fetches).
2. Apply critique and truth filters: comment only on modified lines (+/-), omit compliments, provide compilable replacement snippets with matching indentation, and give every finding `Evidence` with a tier and `Why` with a concrete consequence.

### Document Review Mandate  (include only when the diff has document files)
These files are documents. Review them for structure, correctness, completeness, consistency, verifiability, and downstream impact. Report wording and grammar at `low` only.
- Files and rubrics: <path — type — absolute rubric path, one per document file>
- Read `<skill-dir>/references/reviewing_documents.md` first, then each listed rubric.
- Apply the lenses in order: Implementer, Tester / Verifier, Consumer, Operator / Adversary.
- Every document finding has a `Category`, a `Why`, and an `Evidence` field (`Read` or `Fetched`) quoting the text (both sides for an inconsistency). Findings without quoted evidence are dropped.
- Run the downstream impact pass (at most 10 files outside this diff) and fill in the `## Structural Assessment` section. Each lens verdict names what you checked; a bare "no issues" is not a verdict.

### API Review Mandate  (include only when diff modifies public APIs, signatures, endpoints, or configuration surfaces)
These code changes modify public API surfaces, signatures, exported symbols, endpoints, or configuration options.
- Read `<skill-dir>/references/reviewing_apis.md` first.
- Apply the Dual-Scope Model: inspect sibling methods and call sites for context, but report only findings anchored to modified lines (+/-).
- Evaluate contract integrity, minimal surface area (KISS/YAGNI), ergonomics (naming, CQS, least astonishment), explicit configuration, strict validation, and operational reliability.
- Do not demand refactoring untouched legacy code or changing existing conventions outside the diff.

### Lane Assignment  (include only under --panel; see [lanes.md](lanes.md#lane-specs))
<lane A or lane B block>

### Focus Priority  (include only for a general or lane reviewer when Step 2d folded a theme into this brief)
<Focus Priority block from lanes.md, filled in; or its inline `#### Ad hoc playbook` variant>

### Focus Mandate  (include only for a focus reviewer `rev-f*`; never together with Focus Priority)
<Focus Mandate block from lanes.md, filled in; or its inline `#### Ad hoc playbook` variant>

### Verification Mandate  (include only under --verify for a trusted change that passed the Step 2a Security Gate)
- Your checkout: `<pr_review_dir_a or pr_review_dir_b>`. This checkout is reserved for you alone. Run tier 2 commands only there.
- Security-first ordering: Complete your read-only inspection of the diff (including Criterion 5: Security and reading the target test files with `view_file`) before running your first tier 2 command. Under `--panel --verify`, Reviewer A (Lane A) must also inspect each target test file and the production diff lines it exercises to confirm no external network calls, credential reads, or host-modifying commands are present before executing it. If you discover an unflagged execution hazard, abort tier 2 execution immediately and report the finding using `Read` or `Deduction` evidence.
- Allowed: targeted existing tests, `review_scratch_*` repro tests, reverted single-line mutations, and dependency installs inside the checkout.
- At most 8 commands in total, at most 3 of them mutations (`critical`/`high` falsification first). Every run counts. Before each command, name the finding or hypothesis it checks. Never run anything on the "Never run" list in `<skill-dir>/references/verification.md`.
- Mutation discipline: apply one mutation at a time, run only the targeted test, record `Evidence: Executed`, then `git -c core.hooksPath=/dev/null -c core.fsmonitor=false restore <path>` so `git -c core.hooksPath=/dev/null -c core.fsmonitor=false status --porcelain` shows only `review_scratch_*` files.
- Give confirmed findings `Evidence: Executed: <command>: <trimmed output, <=15 lines>`. Drop disproved findings and record them in `## Checked and Found Clean`.

### Report Format
If running as a swarm worker (Tier 1), post your complete report on the board via `post_message` addressed strictly to `@coordinator` (never `@all` or a sibling reviewer) and call `swarm_wait` in the same turn. If running as a subagent (Tier 2), return your report via `send_message` (or subagent output). Use this exact structure:

## Summary
<1-2 paragraphs summarizing changes. End with a line of the form `Verdict: <Ready | Minor Revisions Needed | Critical Blockers>`.>

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
- **Focus**: `<theme-id>` (focus reviewers only)
- **Evidence**: `<Executed | Read | Fetched | Deduction | Speculation>` — <command and trimmed output, quoted text with path:line, or line-cited trace>
- **Body**: `<1-3 short peer sentences explaining the defect without restating the diff lines (<= 25 words/sentence, PCC <= 5)>`
- **Why**: `<1 short sentence (<= 25 words) naming the concrete consequence if shipped as-is (combined comment prose <= 80 words, <= 4 sentences)>`
- **Suggestion**: (Optional drop-in code snippet, replacement text, or specific question. One fenced block at most; the exporter posts it as a committable GitHub suggestion unless the fence is ```` ```diff ```` or ```` ```patch ````. Never put a test or example here.)
- **Repro test**: (Optional, `--verify` only. The scratch test that reproduces the defect, in one fenced block. It is posted as a plain code block, never as a committable suggestion.)

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

## Playbook Feedback  (focus reviewers only)
Failure modes checked: <numbers from the playbook>
- <at most 4 lines, or `None.`>

### Modified Code Diff
<For diffs <= 150 lines and <= 6 KB, embed inline:>
````diff
<full_unified_diff>
````
<For diffs > 150 lines or > 6 KB, list the staged chunk files from `stage_diffs.py --grouped` instead of pasting inline:>
Read every staged diff chunk file below with `view_file` before evaluating the changes (<= 700 lines and <= 35 KB per chunk):
- `/tmp/pr-review-diffs-XXXXXX/diff_chunk_01.diff` (`<lines>` lines, `<bytes>` bytes): `<covered_files>`
`````
