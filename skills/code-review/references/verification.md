# Verification

This reference covers running commands during a review to confirm or rule out specific findings. Verification is not a general pass/fail check of the change. That is CI's job. For PRs, read CI results instead of rerunning the whole suite.

Running commands never relaxes the review-only rule. No command may change the user's working tree, and no fix is applied anywhere the user will see it.

## Tiers

| Tier | When | Allowed | Where |
| :--- | :--- | :--- | :--- |
| **1a: static analyzers, linters, and remote checks** | Always (pre-security-gate) | CI status and failed-job logs; static analyzers, linters, and formatters in check mode (`ruff`, `dart format --output=none`, `python3 -I` AST parse, `dart analyze`, `tsc --noEmit`, `eslint`), unless the PR diff modifies that tool's executable config or plugin registration (`eslint.config.*`, `.eslintrc*`, `tsconfig*.json` `plugins`, `analysis_options.yaml` `plugins`), which promotes that specific tool to Tier 1b | Local changes: in place, with the working-tree guard below. PRs: the `pr-review-*` checkout, or in place with the guard if the PR branch is already checked out locally. |
| **1b: build steps, dry-runs, and repo scripts** | After the Pre-Execution Security Gate clears the change (`SAFE_FOR_LOCAL_EXECUTION`) | Build steps (`swift build`); toolchain dry-run and task-graph queries (`make -n`, `bazel query`, `gradle tasks`, `--dry-run`); scripts located in the repository itself (such as doc validators); any Tier 1a linter or analyzer whose executable config or plugin file was modified in the PR | Same as Tier 1a, once the Pre-Execution Security Gate records `SAFE_FOR_LOCAL_EXECUTION` (or user approval). |
| **2: tests and mutations** | `--verify` (or `--max`), the change is trusted, and the Pre-Execution Security Gate cleared the change | Targeted existing tests for the changed files; scratch tests (`review_scratch_*`) that reproduce a suspected finding; reverted single-line mutation runs; dependency installs inside the throwaway checkout | The reviewer's own throwaway `pr-review-*` checkout only. Never the user's working tree. |
| **Never** | Any flags | See "Never run" below | N/A |

### Tier 1a and Tier 1b Commands

Run only tools that are already installed and whose dependencies are already present. Never install dependencies for Tier 1a or Tier 1b. If a tool can't run, skip it, say so in the report, and rely on CI.

#### Tier 1a Commands (Always Safe Pre-Gate, Unless Config Modified in PR)

| Kind | Examples |
| :--- | :--- |
| CI status (PRs) | `gh pr checks <n>`; for each failed check, `gh run view <run-id> --log-failed \| tail -n 100` |
| Dart | `dart analyze <paths>` (moves to Tier 1b if `analysis_options.yaml` `plugins` changed); `dart format --output=none --set-exit-if-changed <paths>` |
| TypeScript | `npx --no-install tsc --noEmit -p <tsconfig>` (moves to Tier 1b if `tsconfig*.json` `plugins` changed); `npx --no-install eslint <paths>` (no `--fix`; moves to Tier 1b if `eslint.config.*` or `.eslintrc*` changed) |
| Python | `ruff check <paths>` (no `--fix`); `ruff format --check <paths>`; `python3 -I -c "import ast,sys; [ast.parse(open(f).read(), f) for f in sys.argv[1:]]" <paths>` (pass `-I` so Python never imports from the checkout's working directory; never use `py_compile`, which writes `__pycache__`) |

#### Tier 1b Commands (Gated on Pre-Execution Security Review)

Because build manifests (`Package.swift`, `Makefile` `$(shell ...)`, `build.gradle`, Starlark repo rules) and scripts located inside the repository execute code from the checkout, run them only after the Pre-Execution Security Gate clears the change:

| Kind | Examples |
| :--- | :--- |
| Swift | `swift build` only if the package is already resolved and the security gate passed; otherwise skip |
| Build / task graph | `make -n`, `bazel query <expr>`, `gradle tasks`, or the build tool's `--dry-run` mode |
| Documents | The repo's doc validators (for example `python3 -I blueprints/validate_blueprints.py`); JSON Schema validation of changed examples; link checkers |

Scope each tool to the changed files or their package where the tool allows it.

### Working-Tree Guard (In-Place Runs)

Before the first in-place Tier 1a or Tier 1b command, save `git -c core.hooksPath=/dev/null -c core.fsmonitor=false status --porcelain` output. After the last one, run it again and compare. If they differ, a tool wrote to the working tree. The guard can't see writes to ignored paths (caches, `__pycache__`, build output), so prefer tools and flags that don't write at all. Report the changed paths in the review Summary. Don't revert anything yourself: the user decides what to keep.

### Never Run

With any flags, never run:

- Commands that write fixes: `--fix`, `--write`, `--update-snapshots`, `-u` for snapshot tools, formatters in write mode (`dart format` without `--output=none`, `prettier --write`, `ruff format` without `--check`).
- Code generation that writes into the user's tree.
- Publishing, deploying, pushing, or tagging.
- Full end-to-end suites, or anything that needs production credentials.
- Any command in the user's working tree that isn't Tier 1a or Tier 1b.

## Pre-Execution Security Gate

Before running any Tier 1b command (build steps, toolchain dry-runs, repo scripts, or linters whose executable config was modified in the PR) or preparing a Tier 2 `--verify` checkout, complete the Pre-Execution Security Gate:

1. **Static Execution-Risk Pre-Scan**:
   - When invoked from `pr-review`, read `security_prescan` from `pr_coords.json` and the `## Security & Execution Gate` section in `blind_description.md`.
   - When invoked standalone on a PR or branch diff, run the deterministic scanner across the full unified diff (before chunking):
     ```bash
     python3 <skill-dir>/../pr-review/scripts/review_engine.py \
       --security-scan <diff-file-or-stdin>
     ```
2. **Audit Flagged Surfaces with `view_file` (Tier 1a Only)**:
   - Open every file and line listed in `security_prescan.signals` (plus any modified build, hook, linter plugin, or test setup files) using `view_file`.
   - Check for local execution hazards:
     - **Build, hook, and plugin tampering**: malicious or unexpected scripts in `package.json` (`preinstall`, `postinstall`, `prepare`, `test`), `.npmrc`, `.yarnrc*`, `setup.py`, `pyproject.toml`, `Makefile`, `build.gradle*`, `Cargo.toml`, `build.rs`, `.cargo/config*`, `Package.swift`, `WORKSPACE*`, `MODULE.bazel`, `.bazelrc`, `analysis_options.yaml` (`plugins`), `.eslintrc*`, `eslint.config.*`, `tsconfig*.json` (`plugins`), `conftest.py`, `.gitattributes` (`filter=`, `diff=`), `.husky/*`, `.githooks/*`, `.envrc`, or `.mise.toml`.
     - **Runtime exfiltration or host mutation**: process spawning (`subprocess`, `os.system`, `child_process`, `Process.run`, `eval`, `exec`), external network connections (`curl`, `wget`, `requests`, `urllib`, `fetch`, `HttpClient`), reads of sensitive host paths (`~/.ssh`, `~/.aws`, `~/.env`, `~/.gemini`, `~/.config/gh`), writes outside `/tmp`, or base64/hex-encoded payloads in changed code or tests.
3. **Record the Gate Verdict**:
   - `SAFE_FOR_LOCAL_EXECUTION`: No execution hazards found (or all `security_prescan` signals are benign internal calls that do not execute untrusted commands, contact external hosts, or touch host files outside the checkout). Unlocks Tier 1b commands and (when trusted under `--verify`) Tier 2 checkouts and tests.
   - `REQUIRES_USER_CONFIRMATION`: The change modifies build scripts, package manifests, linter plugins, or subprocess invocations in a way that looks legitimate for the feature rather than malicious, and a Tier 1b or Tier 2 command would execute those paths. Present the exact `file:line` hunks to the user via `confirm_gate` (`genui`) or `ask_question` and run Tier 1b or Tier 2 commands only if the user approves; if declined, stay at Tier 1a.
   - `BLOCKED_UNSAFE`: The diff contains malicious, obfuscated, exfiltrating, or unsafe host-modifying code. Immediately block all Tier 1b and Tier 2 execution (even if `author_association` is `OWNER`, `MEMBER`, or `COLLABORATOR`), stay strictly at Tier 1a, note `"Local execution blocked by Pre-Execution Security Gate"` in the review Summary, and file the hazard as a `critical` Security finding at `Evidence: Read`.

## Trust Check

Tier 2 runs code from the change. It runs only when the change is trusted **and** the Pre-Execution Security Gate cleared local execution:

- **Local changes** and the user's own branches are trusted (still subject to the Pre-Execution Security Gate when reviewing imported patches or third-party branches).
- **PRs** are trusted when the author has write access to the repo. Look it up with the PR's own `owner` and `repo` written out:

  ```bash
  gh api repos/<owner>/<repo>/pulls/<n> --jq .author_association
  ```

  Do not use `gh`'s `{owner}/{repo}` placeholders here: `gh api` fills them from the repository of the current directory (or `GH_REPO`), not from the PR under review, so a session running from another checkout would query the wrong repo's PR with the same number. (`gh pr view --json` does not expose this field.) `OWNER`, `MEMBER`, or `COLLABORATOR` means trusted. `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, `FIRST_TIMER`, `NONE`, or a failed lookup means untrusted.

Untrusted PRs get Tier 1a (and Tier 1b only if `SAFE_FOR_LOCAL_EXECUTION`), never Tier 2, even with `--verify` or `--max`. No flag overrides this. The review Summary says: "`--verify` limited to Tier 1: PR author is not a repo collaborator" (or "author lookup failed").

## Throwaway Checkout

Tier 2 needs a checkout the user never works in. Build it in Step 2c **only after** both the Trust Check and the Pre-Execution Security Gate pass. Use the `pr-review-*` prefix so Step 6 cleans it up, and pass `-c core.hooksPath=/dev/null -c core.fsmonitor=false` on every `git` command so repository hooks never run:

- **Remote PR not checked out locally**: SKILL.md Step 1 already created `$pr_review_dir_a` with hooks and fsmonitor disabled.
- **Local changes, or a PR branch already checked out locally**: clone the local repo, copy the working-tree changes into `$pr_review_dir_a`, verify the diff stat matches, and commit a review snapshot so `HEAD` inside the checkout represents the exact change under review:

  ```bash
  repo_root=$(git rev-parse --show-toplevel)
  pr_review_dir_a=$(mktemp -d -t pr-review-XXXXXX)
  git -c core.hooksPath=/dev/null -c core.fsmonitor=false clone --quiet "$repo_root" "$pr_review_dir_a"
  git -C "$pr_review_dir_a" -c core.hooksPath=/dev/null -c core.fsmonitor=false checkout --quiet --detach "$(git -C "$repo_root" rev-parse HEAD)"
  # Staged and unstaged changes to tracked files.
  git -C "$repo_root" -c core.hooksPath=/dev/null -c core.fsmonitor=false diff --no-ext-diff --no-textconv --binary HEAD > "$pr_review_dir_a/.review.patch"
  if [[ -s "$pr_review_dir_a/.review.patch" ]]; then
    git -C "$pr_review_dir_a" -c core.hooksPath=/dev/null -c core.fsmonitor=false apply "$pr_review_dir_a/.review.patch"
  fi
  rm "$pr_review_dir_a/.review.patch"
  # Untracked files that aren't ignored.
  git -C "$repo_root" ls-files -z --others --exclude-standard \
    | tar -C "$repo_root" --null -T - -cf - | tar -C "$pr_review_dir_a" -xf -
  ```

  For a local PR branch with no working-tree changes, the checkout step alone is enough. Before committing the snapshot, check that `git -C "$pr_review_dir_a" -c core.hooksPath=/dev/null -c core.fsmonitor=false diff --no-ext-diff --no-textconv --stat HEAD` matches `git -C "$repo_root" -c core.hooksPath=/dev/null -c core.fsmonitor=false diff --no-ext-diff --no-textconv --stat HEAD`. Then commit the working-tree state (skipped when nothing is staged):

  ```bash
  git -C "$pr_review_dir_a" -c core.hooksPath=/dev/null -c core.fsmonitor=false add -A && (
    git -C "$pr_review_dir_a" -c core.hooksPath=/dev/null -c core.fsmonitor=false diff --cached --quiet \
    || git -C "$pr_review_dir_a" -c core.hooksPath=/dev/null -c core.fsmonitor=false -c user.name=review -c user.email=review@localhost -c commit.gpgsign=false commit --no-verify -q -m "review snapshot"
  )
  ```

  Invariant: the checkout's `HEAD` is the change under review (use `git -C "$pr_review_dir_a" rev-parse HEAD` as Head Ref in the reviewer prompt), and `git -C "$pr_review_dir_a" -c core.hooksPath=/dev/null -c core.fsmonitor=false status --porcelain` shows only edits the reviewer made.

Dependency installs for Tier 2 (`npm ci --ignore-scripts`, `dart pub get`, or a virtualenv inside the checkout) happen only inside `$pr_review_dir_a`, and only after the Pre-Execution Security Gate passes. Pass `--ignore-scripts` to `npm ci` unless the security gate specifically audited and approved the package lifecycle scripts. If an install fails (for example, offline), skip Tier 2, note it in the report, and finish the review.

### Per-Reviewer Checkouts

Each `--verify` reviewer gets its own checkout so mutation runs in one lane never corrupt files another reviewer is reading:

- **Single reviewer (`--verify`)**: uses `$pr_review_dir_a`.
- **Panel (`--panel --verify`)**: build `$pr_review_dir_a` completely in Step 2c (clone, patch, snapshot commit, and dependency install), then copy it for lane B:

  ```bash
  pr_review_dir_b=$(mktemp -d -t pr-review-XXXXXX)
  cp -a "$pr_review_dir_a/." "$pr_review_dir_b/"
  ```

  If the dependency step created a Python virtualenv with an editable install (`pip install -e` or `uv sync`), its `.pth` / editable finder still points at `$pr_review_dir_a`; rerun the editable install inside `$pr_review_dir_b` before launching the panel. Pass `$pr_review_dir_a` in lane A's prompt and `$pr_review_dir_b` in lane B's prompt.

## Mutation Testing

Mutations are allowed only inside a `pr-review-*` checkout whose Verification Mandate includes the sentence `"This checkout is reserved for you alone."` Two mutation patterns settle most test and correctness questions:

1. **Break the changed implementation**: invert a changed condition, remove a guard, or change a boundary in the production code, then run the covering test. If the test still passes, you have `Executed` proof of a coverage gap. If the test fails, the case is guarded; drop the finding.
2. **Revert the fix hunk**: revert the production fix while keeping the new regression test, then run the test. If the test passes without the fix, the regression test does not catch the bug.

Follow this five-step discipline for every mutation run:

1. Name the candidate finding and the exact test command before touching any file.
2. Apply **one** small mutation to a tracked file in your reserved checkout. Never stack multiple mutations at once.
3. Run only the targeted test file or test case, never the full suite.
4. Record the command and trimmed output at `Evidence: Executed`.
5. Immediately revert the file with `git -C "$checkout" -c core.hooksPath=/dev/null -c core.fsmonitor=false restore <path>` and confirm `git -C "$checkout" -c core.hooksPath=/dev/null -c core.fsmonitor=false status --porcelain` shows only `review_scratch_*` files before running another command or returning your report.

## Tier 2 Rules for Reviewers

The orchestrator passes these rules to the reviewer in a Verification Mandate block:

- **Security-First Ordering Rule**: Complete your read-only inspection of the diff (including Criterion 5: Security and reading the target test files you plan to run) **before** executing your first Tier 2 command. Under `--panel --verify`, where Lane A runs tests while Lane B owns Security, Lane A must still read each target test file and the production diff lines it exercises to verify they contain no external network calls, credential reads, or host-modifying operations outside `$pr_review_dir_a` before running that test. If you uncover an unflagged execution hazard in the diff or test files, abort Tier 2 execution immediately, run zero local commands, and report the hazard as a `critical` Security finding at `Evidence: Read`.
- Run commands only inside the named `pr-review-*` checkout.
- Run at most 8 tier 2 commands in total, at most 3 of them mutation runs. Prioritize falsification runs on `critical` and `high` findings first. Every run counts, including reruns of the same test, so get a scratch test right before running it. Before each command, name the finding or hypothesis it checks.
- Prefer existing tests for the changed files. To confirm a suspected bug with no covering test, write a small `review_scratch_*` test file inside the checkout.
- Revert every mutation with `git -c core.hooksPath=/dev/null -c core.fsmonitor=false restore <path>` before the next command and before reporting.
- Stop any command that runs longer than 10 minutes, and report that it timed out.
- A finding a command confirms gets `Evidence: Executed` (format below). A finding a test disproves is dropped and recorded in `## Checked and Found Clean`.
- A scratch test that reproduces a bug goes in the finding's `- **Repro test**:` field, in one fenced block. Never put it in `Suggestion`: the exporter posts a `Suggestion` as a committable GitHub suggestion anchored on the production lines, while a `Repro test` is posted as a plain code block.

## Orchestrator Clean Check

Before Step 5's citation audit, inspect every `--verify` checkout with hooks and fsmonitor disabled:

```bash
git -C "$dir" -c core.hooksPath=/dev/null -c core.fsmonitor=false status --porcelain
```

Any line other than `?? review_scratch_*` means the reviewer left a mutation or untracked file behind. Restore tracked files and remove unexpected untracked files:

```bash
git -C "$dir" -c core.hooksPath=/dev/null -c core.fsmonitor=false restore .
git -C "$dir" -c core.hooksPath=/dev/null -c core.fsmonitor=false clean -fd -e 'review_scratch_*'
```

Note the leftover edit in the review Summary, and run Step 5's citation audit across **all** findings from that reviewer (not only `critical` and `high`).

## Evidence Format

Every finding carries an `Evidence` field whose first token is its tier from [evidence.md](evidence.md#tiers). For a command run in tier 1 or tier 2:

```markdown
- **Evidence**: `Executed` — `<command>` — <trimmed output, at most 15 lines>
```

Keep only the lines that show the result (the failing assertion, the unexpected pass under a mutation, the error, or the analyzer message).
