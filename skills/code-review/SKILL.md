---
name: code-review
description: >-
  Reviews code and technical documents (pull requests, local changes, whole
  files, specs, design docs, RFCs, ADRs, blueprints) in generate, critique, and
  synthesize passes. Every finding carries an evidence tier and a concrete
  consequence, and severity is capped by the evidence. Syncs remote PRs into a
  temporary checkout. Use when the user asks for a review, a critique, a second
  opinion on a diff, or a check of a spec or design before implementation.
  Flags: --fast, --json, --claude, --opus, --panel, --verify, --max,
  --challenge, --focus, --no-focus. Don't use for posting the review to GitHub
  (use pr-review), for acting on review feedback (use pr-feedback-handler), or
  for API-design-only checks (use api-review).
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
| *(none)* | Dynamic default determined by Ring classification (Ring 0: `--fast`; Ring 1-2: Standard; Ring 3: `--panel`; Ring 4: `--max`). Explicit depth flags skip Ring evaluation. | Scaled to blast radius |
| `--fast` | Single-pass in-session review without subagents; tier 1a/1b checks only | Minimal latency / no subagents |
| `--json [<path>]` | Emits `review_results.json` via `lint_review.py --export-json`, to `<path>` when given, otherwise to `<artifact-dir>/review_results.json` | Same as underlying mode |
| `--claude` (or `--opus`) | Pins the single reviewer (or reviewer B under `--panel`) to Claude Opus (`opus-5.5-max` on Tier 1 swarm, or `model: "opus"` on Tier 2a Claude Code) | Uses 3P quota |
| `--panel` | 2 reviewers in parallel with non-overlapping criteria (lanes A and B), for code and documents | About 2× reviewers |
| `--verify` | Adds tier 2 (targeted tests, repro tests, and reverted mutations in a per-reviewer throwaway checkout) for trusted changes that pass the Pre-Execution Security Gate | Test runtime and dependency installs |
| `--max` | Same as `--claude --panel --verify` | All of the above |
| `--challenge` | After Step 5's citation audit, send refuting evidence back to the reviewer and require a withdrawal or new evidence; one round | One extra turn per disputed reviewer |
| `--focus` | Launch a focus reviewer on the top Step 2d theme even when its verdict is weak | +1 reviewer |
| `--focus <theme>` | Pin a theme: a catalog id from [focus_areas/README.md](references/focus_areas/README.md) or quoted free text. Repeatable, up to 3 focus reviewers | +1 reviewer per theme |
| `--no-focus` | Skip Step 2d; no focus reviewer and no theme hypotheses | None |

`--fast` is mutually exclusive with `--claude`, `--panel`, `--verify`, and `--challenge`. If specified together, `--fast` takes precedence and the other flags are dropped (running strictly in-session without subagents). Under `--fast`, `--focus` themes fold into the in-session pass as Focus Priority. Expand `--max` first, before anything else runs. `--max` implies neither `--challenge` nor `--focus`. Flags combine, and repeating one is harmless, so `--max --claude` (or `--max --opus`) equals `--max`. Record the flags in effect after expansion. The report Summary lists them, along with any trust limit.

### Automatic Review Scaling via Reuse Rings

When no depth flag (`--fast`, `--panel`, `--verify`, `--claude`, `--max`) is specified, `code-review` evaluates the changed files against the five generalized blast-radius rings to select the scaled default review depth:

- **Ring 0 (Private / Local)**: Personal dotfiles, ad-hoc scratch scripts, or local configs. Scales to `--fast` (single-pass in-session review, no subagents).
- **Ring 1 (Internal Tooling & Repo Plumbing)**: CI workflows, linter configs, build scripts, development hooks, repo scripts, or test suites. Scales to **Standard** baseline review (1 Gemini reviewer, Tier 1a/1b checks).
- **Ring 2 (Application / Service Logic)**: Internal application and service logic, endpoints, UI components, backend business logic, database queries. Scales to **Standard** baseline review (1 Gemini reviewer, Tier 1a/1b checks).
- **Ring 3 (Shared Libraries & Public Contracts)**: Exported package surfaces, public APIs, schema or protocol contracts (`.proto`, `*.schema.json`), and package manifests (`package.json`, `pubspec.yaml`, `Cargo.toml`). Scales to `--panel` (2 parallel reviewers with non-overlapping criteria).
- **Ring 4 (Foundational Runtimes & Core Engines)**: Engine internals, runtimes, virtual machines, compilers, memory allocators, or low-level systems code. Scales to `--max` (`--claude --panel --verify`, two-lane panel with deep verification in throwaway checkouts).

When an explicit depth flag is provided by the user or caller (e.g. `--max`, `--fast`, `--panel`), ring evaluation is skipped and that depth is used directly.

Rings set depth (how many general reviewers, and whether `--verify` runs). Step 2d sets breadth (whether focus reviewers join them). The two are independent: a Ring 2 change can carry a strong theme, and a Ring 4 change may carry none.

## Workflow

Follow these steps sequentially to perform a review:

### Step 1: Gather Changes and Prepare Workspace

Before starting the review, identify the changes, pin commit SHAs, and prepare the target workspace. All `git` and `gh` clone, checkout, and diff commands in Step 1 must run with git hooks, fsmonitor, and external diff or textconv drivers disabled so syncing an untrusted branch is strictly inert. Do not run any build step, dry-run, repo script, dependency install, or test during Step 1.

- **For GitHub Pull Requests**:
  - Use `gh pr view` to inspect the PR title, description, repository, and head branch.
  - **Trust Check** (only needed with `--verify`): run `gh api repos/<owner>/<repo>/pulls/<n> --jq .author_association` with the PR's own `owner` and `repo` written out (do not use `gh`'s `{owner}/{repo}` placeholders, which resolve from the current directory). `OWNER`, `MEMBER`, or `COLLABORATOR` means trusted. Any other value, or a failed lookup, means untrusted: tier 2 is off, and no flag overrides that. See [verification.md](references/verification.md#trust-check).
  - **Check Local Branch Presence**: Check if the PR branch exists locally with `git branch --list <branch>` or `git rev-parse --verify <branch>`.
  - **Sync Remote PR to Temporary Directory**:
    - If the PR is not part of the local branch(es) (missing locally or in another repository):
      - Assume the PR branch must be synced to a local temporary directory to conduct the review.
      - Create a dedicated temporary directory with a specific prefix: `pr_review_dir_a=$(mktemp -d -t pr-review-XXXXXX)`.
      - Clone the repository into that directory with hooks and fsmonitor disabled. If a local checkout exists on disk, use `git -c core.hooksPath=/dev/null -c core.fsmonitor=false clone --reference <path-to-local-repo> <repo-url> "$pr_review_dir_a"` to speed up the clone. Otherwise, use `gh repo clone <owner/repo> "$pr_review_dir_a" -- -c core.hooksPath=/dev/null -c core.fsmonitor=false`.
      - Check out the PR branch in the temporary directory with hooks and fsmonitor disabled: `cd "$pr_review_dir_a" && GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/dev/null GIT_CONFIG_KEY_1=core.fsmonitor GIT_CONFIG_VALUE_1=false gh pr checkout <pr-number-or-url>`.
      - Perform all subsequent review steps (inspecting diffs, reading context files, evaluating tests) within `$pr_review_dir_a`.
      - Retain the directory path in `$pr_review_dir_a` for safe cleanup in Step 6.
  - Use `gh pr diff` (or `git -c core.hooksPath=/dev/null -c core.fsmonitor=false diff --no-ext-diff --no-textconv <base>...HEAD` in the checkout) to obtain the changes, and `gh pr diff --name-only` for the file list.
  - _Reference: See the [gh-cli](../gh-cli/SKILL.md) skill for detailed usage._
- **For Local Changes**:
  - Use `git -c core.hooksPath=/dev/null -c core.fsmonitor=false status` to see modified files.
  - Use `git -c core.hooksPath=/dev/null -c core.fsmonitor=false diff --no-ext-diff --no-textconv --stat` (or `--staged --stat`) first to assess change volume.
  - Get the file list with `git diff --no-ext-diff --no-textconv --name-only` (unstaged), `git diff --no-ext-diff --no-textconv --staged --name-only` (staged), or `git diff --no-ext-diff --no-textconv --name-only <base>...HEAD` (a branch). Add untracked files from `git ls-files --others --exclude-standard` if they are part of the change.
  - Use targeted diffs (`git diff --no-ext-diff --no-textconv -- <files...>`) and/or `sem diff` for large changesets to prevent context window saturation.
  - Use `git log --no-ext-diff --no-textconv -p` to see recent commits if reviewing a local branch.
- **For Whole Local Files** (for example, "review this spec"): produce an all-`+` diff with real line numbers using `git diff --no-index --no-ext-diff --no-textconv /dev/null <file>`. It exits with status 1 when it prints a diff, which is expected. The rest of the workflow treats this like any other diff.
- **Defer `--verify` Throwaway Checkouts to Step 2c**: Do not create `--verify` throwaway checkouts or run dependency installs in Step 1. Those happen in Step 2c only after the Step 2a Pre-Execution Security Gate clears the diff.
- **Pin Base and Head SHAs**: Record `base_sha=$(git merge-base <base> HEAD)` (or `git rev-parse HEAD` for uncommitted working-tree changes against `HEAD`). Set `head_sha` to `git rev-parse HEAD` in the source repo (updated in Step 2c to `git -C "$pr_review_dir_a" rev-parse HEAD` when a `--verify` snapshot commit is created for local working-tree changes).
- **Stage Diff Into Size-Bounded Chunks**: For any diff over 150 lines or 6 KB, create a temporary diff directory (`diff_dir=$(mktemp -d -t pr-review-diffs-XXXXXX)`) and pipe the unified diff into `stage_diffs.py --grouped`:
  ```bash
  <diff command> | python3 <skill-dir>/../context-indexer/scripts/stage_diffs.py \
    --grouped --repo-dir <workspace-dir> --base-ref "$base_sha" --output-dir "$diff_dir"
  ```
  This packs per-file diffs (splitting oversized files along hunk boundaries) into `diff_chunk_01.diff`, `diff_chunk_02.diff`, … where each chunk is $\le 35\text{ KB}$ and $\le 700$ lines (fitting in a single `view_file` call). It also writes `manifest.json`, `diff_lines.jsonl`, `diff_hunks.jsonl`, and `diff_entities.jsonl`. Retain `$diff_dir` for cleanup in Step 6.

### Step 1b: Classify Artifacts & Evaluate Reuse Ring

1. **Evaluate Reuse Ring (When No Depth Flag Is Specified)**:
   - If an explicit depth flag (`--fast`, `--panel`, `--verify`, `--claude`, `--max`) was specified by the user or calling skill, skip ring evaluation and proceed with the specified depth.
   - If no depth flag was specified, evaluate the file list with `classify_artifacts.py --ring`:
     ```bash
     <file list> | python3 <skill-dir>/scripts/classify_artifacts.py --root <repo-root> --ring -
     ```
     This returns the highest matched ring (Ring 0 to 4), sets the active flags accordingly (e.g. `--fast` on Ring 0, `--panel` on Ring 3, `--max` on Ring 4, or baseline 1-reviewer Standard on Ring 1 and 2), and generates a ring summary banner.
   - Record the ring banner in the review summary:
     `🛡️ Ring Classification: Ring <N> (<Label>) -> Review Level: <Level>` (or `🛡️ Review Level: <Level> (<Flag> specified, ring evaluation skipped)`).

2. **Classify Artifact Types**:
   Pipe the changed file list into the classifier:
   ```bash
   <file list> | python3 <skill-dir>/scripts/classify_artifacts.py --root <repo-root> -
   ```
   It prints each file's `type` (`code`, `normative-spec`, `design`, `blueprint`, or `reference-doc`), the `rule` that matched, and the `rubric` to use. Group the files into a code partition and a document partition. If a classification is clearly wrong, override it and note why. If there are no document files, skip every document-specific instruction below: code-only reviews work as before. The rules are described in [reviewing_documents.md](references/reviewing_documents.md#when-document-mode-applies).

### Step 2: Pre-Execution Security Gate, Context Enrichment, Tier 1 Verification, and Seed Hypotheses

#### Step 2a: Pre-Execution Security Gate

Before running any build step, toolchain dry-run, or repo-hosted validator script (Tier 1b), or building `--verify` throwaway checkouts and installing dependencies (Tier 2), evaluate the diff for local execution hazards per [verification.md](references/verification.md#pre-execution-security-gate):

1. **Obtain Static Execution-Risk Pre-Scan**: If invoked by `pr-review` with `pr_coords.json` already generated, read `pr_coords.json.security_prescan` (and the `## Security & Execution Gate` section in `pr_description.md`). Otherwise, run the static scanner on the unified diff:
   ```bash
   python3 <skill-dir>/../pr-review/scripts/review_engine.py \
     --security-scan <diff-file> \
     --output-security-scan "$diff_dir/security_prescan.json"
   ```
2. **Audit Flagged Surfaces (Read-Only)**: Using only static file reads (`view_file` / `read_file` and `rg`) and Tier 1a checks, inspect every file and line in `security_prescan.signals`, along with any modified build manifests, git/shell hooks, linter plugin registrations, or test files.
3. **Record the Gate Verdict**:
   - `SAFE_FOR_LOCAL_EXECUTION`: No execution hazards exist, or all flagged signals are verified benign in context. Tier 1b commands and (when `--verify` is active for a trusted collaborator) Tier 2 checkouts, dependency installs, and tests are unlocked.
   - `REQUIRES_USER_CONFIRMATION`: The diff modifies build scripts, package lifecycle hooks, linter plugins, or subprocess invocations that appear benign for the PR's stated purpose but alter code that local verification would execute. Pause and ask the user via `confirm_gate` (`genui`) or `ask_question` before running any Tier 1b or Tier 2 command. If declined, restrict verification to Tier 1a.
   - `BLOCKED_UNSAFE`: The diff introduces command injection, credential or `.env` access, external network exfiltration, obfuscated execution payloads, or rogue hooks/scripts that would run during build or test. Skip all Tier 1b and Tier 2 execution regardless of `author_association`, restrict verification to Tier 1a, and seed a `critical` security hypothesis for Step 3.

#### Step 2b: Context Enrichment and Tier 1a / Tier 1b Verification

Identify which additional files from the repository would be helpful to review for context. When reviewing a PR synced to a temporary directory, inspect the files directly in that temporary checkout. Consider:

- Files that are imported or referenced.
- Parent classes or interfaces.
- Related utility files.
- Test files corresponding to changed files.
- **For public API changes**: sibling methods in modified classes, parent interfaces or base classes, error types, and existing call sites across the repository to understand expected conventions.
- **For document files**: the files each changed document links to, the files that link to it (search for its repo-relative path), the governing spec or schema, and the code or tests the document describes.

Then run **Tier 1 verification** yourself, so the reviewer can spend its effort on the review:

- **Tier 1a (Static Analyzers, Linters & Remote Checks, Always On)**: Run CI status and failed-job queries for PRs (`gh pr checks`, `gh run view --log-failed`), isolated syntax parsing (`python3 -I -c "import ast..."`), and static analyzers or linters in check mode (`ruff check`, `ruff format --check`, `dart format --output=none`, `dart analyze`, `npx --no-install tsc --noEmit`, `npx --no-install eslint`). If the PR diff modifies a linter or analyzer's own executable config or plugin registration (`eslint.config.*`, `.eslintrc*`, `tsconfig*.json` `plugins`, `analysis_options.yaml` `plugins`), that specific tool promotes to Tier 1b and runs only after `SAFE_FOR_LOCAL_EXECUTION`.
- **Tier 1b (Build Steps, Toolchain Dry-Runs & Repo Scripts, Post-Security-Gate Only)**: Only when Step 2a recorded `SAFE_FOR_LOCAL_EXECUTION` (or user-approved `REQUIRES_USER_CONFIRMATION`), run toolchain dry-run or task-graph queries (`swift build`, `make -n`, `bazel query`, `gradle tasks`), repo-hosted document validator scripts (`python3 -I <script>`), and any config-modified Tier 1a analyzer or linter.

Use only tools that are already installed. For in-place runs, compare `git -c core.hooksPath=/dev/null -c core.fsmonitor=false status --porcelain` before and after. Collect the output for the reviewer prompt (cap inline output at 1 KB, failing lines first; if longer, write the full output to `$diff_dir/tier1.log`, creating `$diff_dir` with `mktemp -d -t pr-review-diffs-XXXXXX` if needed, and cite the path). Commands and rules are in [verification.md](references/verification.md#tier-1a-and-tier-1b-commands).

#### Step 2c: Throwaway Checkouts for `--verify` and Seed Hypotheses

- **Throwaway Checkouts for `--verify`**: Only when `--verify` is active, the author trust check passed (`OWNER`, `MEMBER`, or `COLLABORATOR`), and Step 2a cleared local execution (`SAFE_FOR_LOCAL_EXECUTION` or user-approved `REQUIRES_USER_CONFIRMATION`), create `$pr_review_dir_a` with the working-tree changes applied and commit a review snapshot following [verification.md](references/verification.md#throwaway-checkout). For local working-tree changes, update `head_sha` to `git -C "$pr_review_dir_a" rev-parse HEAD` (the review snapshot commit) and note in the prompt that the top commit is the review snapshot so `git diff $base_sha $head_sha` inside the checkout covers the change under review. Under `--panel --verify`, after building `$pr_review_dir_a` (including any dependency install, passing `--ignore-scripts` on `npm ci` when lifecycle scripts are not required), create `$pr_review_dir_b` with `cp -a "$pr_review_dir_a/." "$pr_review_dir_b/"` per [verification.md](references/verification.md#per-reviewer-checkouts) so each lane gets its own isolated checkout. Give each focus reviewer its own copy the same way (`$pr_review_dir_f`, `$pr_review_dir_f2`, `$pr_review_dir_f3`).
- **Seed Hypotheses**: Write **a few Seed Hypotheses** from the diff stat, the context files, the Step 2a security audit, and the Tier 1 output: specific, falsifiable claims (one line each) per [evidence.md](references/evidence.md#hypotheses). On diffs over 500 lines where `duckdb` is installed, query `$diff_dir` using `duckdb -batch -dark-mode -box -init` with [../context-indexer/scripts/staged_diff_macros.sql](../context-indexer/scripts/staged_diff_macros.sql) (`diff_untested_functions`, `diff_signature_drift`, and `diff_entity_churn`; see [splitting_reviews.md](references/splitting_reviews.md#tooling-support)) to seed hypotheses around high-churn functions, changed signatures, and untested changes. Under `--panel`, label each hypothesis with its lane spec per [lanes.md](references/lanes.md#lane-specs). Under `--verify`, mark which hypotheses a mutation run can settle. Deletion-heavy diffs are the `deletion-and-consolidation` focus theme (Step 2d).
- **Ruled out by the caller (`ship-it` homeostasis only)**: When an automated local fix-and-recheck loop (`ship-it`) supplies hypotheses disproved in the immediately preceding pass, include them under the seeds as `Ruled out last pass: …` and instruct the reviewer to re-test a ruled-out line only if the current diff touches the lines it cites. Never include `Ruled out last pass` or seed "verify prior findings" hypotheses on external PR re-reviews (`pr-review` Round 2+) or user-requested re-reviews: those passes must evaluate the full current diff unanchored.

#### Step 2d: Focus Triage

Skip this step under `--no-focus`, for a document-only diff, or when the caller pinned themes with `--focus <theme>` (go straight to the depth cap below). Otherwise answer one question for the code partition: *which single change in this diff, if subtly wrong, would be hardest to catch by reading each hunk on its own?* Start from the diff, then read the index in [focus_areas/README.md](references/focus_areas/README.md) and prefer a catalog id when one fits. A mechanism with no catalog entry is an ad hoc theme (`adhoc:<slug>`) with an inline playbook of at most 1 KB (cues, numbered failure modes, 3 to 5 hypothesis templates).

Evidence: the diff stat, the context files, the Tier 1 output, caller-supplied candidates (such as `pr-review`'s `## Reviewer Focus & Risks` bullets), the first chunk of each staged file group, and on any staged diff `diff_signature_drift` and `diff_entity_churn` from `staged_diff_macros.sql`.

Name at most three candidate themes, each with:

- a one-sentence mechanism ("`fetchProfile` and its 4 callers moved from blocking I/O to `Future`s");
- at least two `path:line` anchors in different functions or files;
- 3 to 5 hypotheses from the playbook templates, labeled `[focus:<theme-id>]`;
- a verdict. **Strong** means the diff introduces or alters the mechanism and the anchors span files or callers. **Weak** means it only touches code that uses the mechanism, or the change stays inside one function.

Rank by verdict, then anchor count, and apply the [depth cap](references/lanes.md#depth-cap): strong themes within the cap (or forced by `--focus`) get a focus reviewer; weak and overflow themes fold into a general or lane reviewer as Focus Priority. Post one banner line per theme before launch, for example `Focus: sync-to-async (strong, rev-f): 3 signatures gained async; anchors a.dart:41, b.dart:88`.

_Reference: Use the guidelines in [splitting_reviews.md](references/splitting_reviews.md) if the review needs to be subdivided._

### Step 3: Generate Initial Review

Before analyzing the diff, select the highest available reviewer delegation tier from the hierarchy below. Every tier reads and follows [reviewer_mandate.md](references/reviewer_mandate.md).

- **Reviewer Delegation Hierarchy**:
  - **Fast In-Session Review (`--fast`)**: If the user passed `--fast`, do not spawn swarm workers or subagents. Conduct the review directly in the active session:
    1. Read [reviewer_mandate.md](references/reviewer_mandate.md), the unified diff (or diff chunks if large), and surrounding context files.
    2. Run Tier 1a (and Tier 1b if Step 2a cleared local execution) read-only checks if not already run in Step 2b.
    3. Evaluate changes against the review criteria (Correctness, Concurrency, Edge Cases, Maintainability, Security, API Soundness, or Document lenses). If Step 2d named themes, read their playbooks and test the theme hypotheses first, as a Focus Priority: the full review still follows.
    4. Draft review findings using the `### <ID>. <short claim>` format with `Evidence`, `Why`, and `Suggestion` fields.
    5. Apply Step 4's self-critique truth filters ([critique_rules.md](references/critique_rules.md)) to the drafted findings, then proceed to Step 5 (Synthesis) without citation auditing or challenge loops.
  - **Tier 1: Jetski Root Session (`swarm` tool available)**:
    - **Default (Gemini)**: Spawn or wake worker `rev-a` pinned to `model: "gemini-launch-candidate-high"`, plus one `rev-f*` per focus reviewer on the same model ([lanes.md](references/lanes.md#reviewer-roster)).
    - **Claude (`--claude` / `--opus`)**: Pin the single worker `rev-a` (or lane B worker `rev-b` under `--panel`) to `model: "opus-5.5-max"` (drawing from `3p-daily` quota).
    - **Proactive Quota Check & Fallback**: Before spawning or waking workers, call `swarm{action: "models"}` once. If `opus-5.5-max` has zero remaining quota, use `gemini-launch-candidate-high` instead. If `gemini-launch-candidate-high` has zero remaining quota, use `auto` (Gemini Pro Next, a separate bucket) for every Gemini reviewer. Report any fallback in chat and in the Summary. If a worker fails with `RESOURCE_EXHAUSTED` or exhausts quota between passes, switch it in place via `swarm{action: "model", name: "<name>", model: "<fallback>"}` (Opus to `gemini-launch-candidate-high`, then to `auto`).
    - **Warm Worker Reuse vs. Unanchored Re-Reviews**: For Step 5 `--challenge` rounds or `ship-it` homeostasis loops where continuity with the prior pass is desired, check `swarm{action: "status"}` and wake a `waiting` worker via `post_message` + `swarm_wait{since: <last_seq>}` (never `kill` + `add` the same name, because `kill` burns the roster name). For an external PR re-review (`pr-review` Round 2+) or when a prior worker was killed, spawn a fresh worker with an incremented suffix (`rev-a-2`, `rev-b-2`, `rev-f-2`) via `swarm{action: "add"}` so the new pass reviews the full diff without Round 1 anchoring.
    - **First Swarm Creation**: Call `swarm{action: "add", name: "rev-a", model: "<model>", brief: "<prompt>"}`, plus one more `add` call in the same turn for `rev-b` under `--panel` and for each `rev-f*`. Emit `[Open the Swarm UI](sidecar://swarm/swarm_board/)` in chat and **end the turn immediately**. Do not call `read_board`, `post_message`, or `swarm_wait` on that turn; those tools mount on the next turn when the worker wakes `@coordinator`.
  - **Tier 2a: Claude Code (`Agent` or `Task` tool available, no `swarm`)**:
    - Spawn 1 `general-purpose` subagent (2 under `--panel`, plus 1 per focus reviewer, all in one turn), passing `model: "opus"` to the single or lane B reviewer when `--claude` or `--opus` is active.
  - **Tier 2b: Jetski Child Subagent (`invoke_subagent` available, `swarm` unavailable or non-root)**:
    - In a child subagent where `swarm` is unavailable or non-root, call `invoke_subagent` with `TypeName: "self"`, `Role: "Code Reviewer"` (or `"Code Reviewer A"` / `"Code Reviewer B"` / `"Focus Reviewer (<theme-id>)"`), and `Model: "inherit"`.
  - **Tier 3: Single-Agent CLI (no `swarm` and no subagent tool)**:
    - Follow the `--fast` in-session review flow above, reading [reviewer_mandate.md](references/reviewer_mandate.md) (and running Lane A then Lane B sequentially if `--panel` was requested). Focus themes fold in as Focus Priority.

- **Reviewer Brief**: Assemble every reviewer's `brief` (Tier 1) or `Prompt` (Tier 2) from [reviewer_brief.md](references/reviewer_brief.md). It holds the packaging invariants (no dummy probes, 10 KB cap, every instruction block before `### Modified Code Diff`, inline diff only up to 150 lines and 6 KB, staged chunk paths otherwise, absolute reference paths) and the template. Include each optional block (`### Document Review Mandate`, `### API Review Mandate`, `### Lane Assignment`, `### Focus Priority`, `### Focus Mandate`, `### Verification Mandate`) only when its condition holds.

Reviewers evaluate code against the criteria in [review_criteria.md](references/review_criteria.md) (correctness, efficiency, maintainability with the [cognitive-complexity](../cognitive-complexity/SKILL.md) skill, security, and API soundness per [reviewing_apis.md](references/reviewing_apis.md) and [canonical_api_design.md](../api-review/references/canonical_api_design.md), anchored to modified lines), test code with [reviewing_tests.md](references/reviewing_tests.md), and document files with the four lenses and rubrics in [reviewing_documents.md](references/reviewing_documents.md). Evidence tiers and falsification rules are in [evidence.md](references/evidence.md); documentation standards are in the [code-documentation](../code-documentation/SKILL.md) skill. **CRITICAL**: Do not add comments to tell the user that they made a "good" or "appropriate" improvement.

#### Roster and Lanes

Compose the roster per [lanes.md](references/lanes.md): `rev-a` always (except `--fast`), `rev-b` under `--panel`, and one `rev-f*` per focus theme that Step 2d assigned a dedicated reviewer. Launch every reviewer in one turn (two or more `swarm{action: "add"}` calls on Tier 1, or parallel subagents on Tier 2), and state the total count in chat first. Panel lanes A and B get a Lane Assignment block; focus reviewers get a Focus Mandate block; a reviewer that received a folded-in theme gets a Focus Priority block. For a split diff, launch one A/B pair per partition ([splitting_reviews.md](references/splitting_reviews.md)).

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

- **Collect Reviewer Reports (Tier 1 `swarm`)**: When a worker wakes `@coordinator`, call `read_board` (with `since: <last_seq>` on later passes) to read the report(s) and record the latest `seq` cursor.
- **Clean check (`--verify`)**: Before reading citations, run `git -C "$dir" -c core.hooksPath=/dev/null -c core.fsmonitor=false status --porcelain` on `$pr_review_dir_a` (and `$pr_review_dir_b` under `--panel --verify`) per [verification.md](references/verification.md#orchestrator-clean-check). If anything other than `?? review_scratch_*` appears, restore tracked files (`git -C "$dir" -c core.hooksPath=/dev/null -c core.fsmonitor=false restore .`), remove unexpected untracked files (`git -C "$dir" -c core.hooksPath=/dev/null -c core.fsmonitor=false clean -fd -e 'review_scratch_*'`), note the leftover edit in the Summary, and audit all findings from that reviewer below.
- **Citation audit**: For every `critical` and `high` finding (and all findings from a reviewer that failed the clean check), open the cited `path:line` (±10 lines) in the review workspace with `view_file`. If a quoted line, symbol, or signature does not match the file on disk, drop the finding or downgrade it to a Question, and audit the rest of that reviewer's findings regardless of severity. Record `"Citation audit: N checked, M dropped (…)"` in the Summary. If the working-tree guard caught an in-place tier 1 change, note that in the Summary as well.
- **Reviewer challenge (`--challenge` only)**: When `--challenge` is active and a check refutes a `critical` or `high` finding, challenge that reviewer naming the finding ID and quoting the refuting evidence. Ask it to `"withdraw, or reply with evidence at a tier that supports the severity"`. Use `post_message` + `swarm_wait{since: <last_seq>}` on Tier 1, `resume: "<agentId>"` on Tier 2a, or `send_message` on Tier 2b. Allow at most one round trip per disputed reviewer. A reply that pivots to a different argument without supplying `Executed`, `Read`, `Fetched`, or line-cited `Deduction` evidence for the original claim counts as withdrawn. Note `"N disputed (M withdrawn, K upheld)"` in the Summary.
- Deduplicate overlapping comments.
- **Multi-reviewer merge**: When more than one reviewer ran, merge per [lanes.md](references/lanes.md#multi-reviewer-merge): one finding per issue and line at the higher severity with "(raised by both reviewers)", keep `out-of-lane` findings and `Focus` fields, merge partial Structural Assessments, and never copy `## Playbook Feedback` into the report. Run the [playbook-read check](references/lanes.md#playbook-read-check) on every focus reviewer before its citation audit.
- Prioritize high-severity issues (critical, high).
- **Number findings and Questions**: Put every finding under a `### <ID>. <short claim>` heading: `C1`, `C2`… for critical, `H1`… for high, `M1`… for medium, `L1`… for low, so the user can say "fix H2 and M1" later. Wording findings sort last within `low`. Number Questions in `## Questions` as `### Q1. <question>` through `Q3` (keep at most 3, choosing the ones with the highest stated stakes in `Why`).
- **Checked and Found Clean merge**: Take the union of disproved hypotheses from the reviewer(s), deduplicate, drop any line that lacks a tier or disproving evidence, and cap at 10 lines, keeping theme disproofs first.
- **Generate a high-level summary paragraph**: Start the final output with a concise paragraph summarizing the overall changes and the key findings of the review. Include the citation audit tally (and `--challenge` tally when used), the flags in effect (for example, "Flags: `--claude --panel --verify`"; write each focus theme as its own backticked token, `` `--focus <theme-id>` ``, adding ` (auto)` inside the backticks when Step 2d chose it), and any trust or security gate limit (for example, "`--verify` limited to tier 1a/1b: PR author is not a repo collaborator" or "`--verify` and tier 1b skipped: blocked by Pre-Execution Security Gate"). End the Summary with a line of the form `Verdict: <Ready | Minor Revisions Needed | Critical Blockers>`; `lint_review.py --export-json` reads this label to set the exported verdict (falling back to finding severities when it is absent) and strips internal orchestration sentences (`🛡️ Ring Classification` / `🛡️ Review Level`, `Pre-Execution Security Gate`, `Citation audit`, `Flags`, working-tree clean check notes, and `Verdict`) as well as local `file://` links from the exported `review_summary.overview`.
- **Structural Assessment**: When the diff has document files, put the `## Structural Assessment` section right after the Summary.
- **Generate file summaries**: For reviews with multiple files, include `## Changed Files Summary` with a single, concise sentence describing the change in each (starting with a past-tense verb like 'Added', 'Updated').
- **Generate a recommendations section**: Summarize the key actionable recommendations in `## Recommendations` after `## Checked and Found Clean`.
- When writing file paths, write them as Markdown links.
- Ensure the final output is cohesive and follows the [`write-prose`](../write-prose/SKILL.md) skill. `lint_review.py` automatically runs `analyze_prose.py` on every finding and question comment.
- **Save and Lint Artifact**: Write the synthesized review to `review_results.md` in the conversation artifact directory (`<appDataDir>/brain/<conversation-id>/`) using `write_to_file` (never into the temporary checkout). Then run:
  ```bash
  python3 <skill-dir>/scripts/lint_review.py <artifact-dir>/review_results.md
  ```
  If `--json` is specified, export `review_results.json` during the lint step. The export path is the one given with `--json <path>` when the caller named one (`pr-review` passes `<conversation-scratch-dir>/review_results.json`), otherwise `<artifact-dir>/review_results.json`:
  ```bash
  python3 <skill-dir>/scripts/lint_review.py <artifact-dir>/review_results.md --export-json <export-path>
  ```
  If `lint_review.py` exits non-zero, fix the reported formatting, severity-cap, or `comment-*` prose violations (`comment-prose-length`, `comment-sentence-length`, `comment-sentence-complexity`, `comment-ai-ism`) in `review_results.md` and rerun until it exits `0` before proceeding to Step 6. A `suggestion-single-fence` violation means a finding's `Suggestion` holds more than one fenced block ("replace X with Y"): keep only the drop-in replacement, and move a regression test to `- **Repro test**:`. A `playbook-feedback-leaked` violation means a focus reviewer's `## Playbook Feedback` section was copied into the report: remove it. Do not emit the full review directly into the chat response.

### Step 5b: Playbook Learning

When a focus reviewer's `## Playbook Feedback` is not `None.`, or a theme was ad hoc, write `focus_playbook_proposals.md` in the conversation artifact directory. For each proposal, give a ready-to-paste section (or a whole playbook in the [catalog format](references/focus_areas/README.md#playbook-format)): the new failure mode, its hypothesis template, and the evidence that settles it. Never write into this skill's directory during a review; applying a proposal is a separate, user-approved change. Callers write the file but do not act on it.

### Step 6: Workspace Cleanup

If any temporary directory was created for reviewing the PR, for `--verify` checkouts, or for staged diff chunks:

- If the shell working directory was changed to a `$pr_review_dir_*` checkout, return to the original working directory before deleting the checkouts.
- Verify each directory path is non-empty, exists, and matches the expected temporary prefix before removal:
  ```bash
  for dir in "${pr_review_dir_a:-}" "${pr_review_dir_b:-}" "${pr_review_dir_f:-}" "${pr_review_dir_f2:-}" "${pr_review_dir_f3:-}"; do
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

When running as a top-level review (not invoked by `pr-review` or `ship-it`) with 3 or more total findings and questions, and the `a2ui-pane` plugin is available (`render_a2ui` is reachable as a native or lazy MCP tool), read the `genui` skill and render a `triage_matrix` surface in the pane (`mode: "pane"`, `surfaceId: "review-triage"`). Pre-populate recommended buckets (`Fix now` for `critical` and `high`, `Follow-up` for `medium` and `low`, `Discuss` for `Questions`) with each finding's file/line anchor in `trace`. When `a2ui-pane` is not installed or fewer than 3 items exist, skip the pane render. When the review includes a scored quality rubric summary across 3 or more evaluated criteria, read `genui` and render a `scorecard` surface in the pane (using `stack: true` if stacking with findings) displaying each rubric row's score, progress bar, and rationale.

Send one short chat message, then **end the turn**. The message contains:

- A link to the review artifact (`review_results.md`, and `review_results.json` if `--json` was passed), plus `[A2UI Pane](sidecar://a2ui-pane/a2ui-pane/)` if you rendered a `triage_matrix` or `scorecard` surface.
- The verdict (Ready / Minor Revisions Needed / Critical Blockers).
- Finding counts by severity, plus question count (for example, "1 critical, 3 high, 2 medium, 4 low, 2 questions"). Questions never block the verdict.
- When Step 5b wrote proposals: a link to `focus_playbook_proposals.md` and "N playbook proposals. Say `apply the playbook proposals` to add them to the catalog."
- When no focus reviewer ran but the Summary names a cross-cutting mechanism change (or Step 2d folded a strong theme in): "No focus reviewer covered `<theme-id>`. Say `focus on <theme-id>` to run one."
- One closing line: "Which findings should I address? (for example, `fix H1 to H3`, `fix all high`, or submit your selections in the A2UI Pane)".

That closing line is a question for the user. It is never a reason to start fixing. Do not edit files, stage changes, or start a fix plan in the same turn.

- **Reviewer wake-up rule**: When a swarm worker or reviewer subagent wakes you, the only steps left are Step 5 (synthesis), Step 6 (cleanup), and Step 7 (stop). This holds even if the conversation was implementing code before the review started.
- **"Review and fix" requests**: Treat them as two requests. The review still ends here. Fixing starts in a later turn, after the user confirms which findings to address.
- **Callers**: Calling workflows that automate reviews or act on findings (for example, `pr-review` or the `ship-it` review loop) run Steps 1 through 6 and skip Step 7. They resume their own flow after Step 6 completes (cleanup included), do not emit the hand-off question, and may then act on the findings per their own instructions. The review step itself never fixes anything.

## Output Format

The final synthesized review MUST be created as an artifact file using the `write_to_file` tool in the conversation's artifact directory (e.g., `review_results.md`) with `UserFacing: true`.

- **Do NOT** print the full review comments or dump the complete review markdown in the chat message response.
- In the final chat response, provide only the Step 7 hand-off message: a clickable markdown link to the artifact, the verdict, the finding and question counts, and the closing question.
- End the turn after the hand-off message. Do not continue to fix anything in the same turn.
- If executing within a subagent, ensure the artifact file is written using `write_to_file` before completing and returning the artifact link to the caller.
- Clean up any temporary directories created during the review before returning the final response.

The review file sections appear in this order:

1. `## Summary`: high-level summary paragraph, citation audit tally, the flags in effect, and a closing `Verdict: <Ready | Minor Revisions Needed | Critical Blockers>` line.
2. `## Structural Assessment` (only when the diff has document files).
3. `## Changed Files Summary` (if applicable).
4. `## Review Comments (Ordered by Severity)`: findings under `### <ID>. <short claim>` headings (`C1`, `H1`, `M1`, `L1`).
5. `## Questions`: up to 3 unresolved questions under `### Q1. <claim as a question>` headings (omit section or write `None.` if none).
6. `## Checked and Found Clean`: up to 10 disproved hypotheses, each with its evidence tier and disproof.
7. `## Recommendations`: key actionable feedback.

Field-by-field formats for findings (`File`, `Line`, `Severity`, `Category`, `Focus`, `Evidence`, `Body`, `Why`, `Suggestion`, `Repro test`) and Questions (`File`, `Line`, `Evidence`, `Why`, `To settle`) are defined in [evidence.md](references/evidence.md#formats) and the Output Contract in [reviewer_mandate.md](references/reviewer_mandate.md#output-contract). A focus reviewer's `## Playbook Feedback` never appears in the final file.
