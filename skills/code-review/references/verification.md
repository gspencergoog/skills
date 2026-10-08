# Verification

This reference covers running commands during a review to confirm or rule out specific findings. Verification is not a general pass/fail check of the change. That is CI's job. For PRs, read CI results instead of rerunning the whole suite.

Running commands never relaxes the review-only rule. No command may change the user's working tree, and no fix is applied anywhere the user will see it.

## Tiers

| Tier | When | Allowed | Where |
| :--- | :--- | :--- | :--- |
| **1: read-only checks** | Always | CI status and failed-job logs; analyzers and linters in check mode; schema and example validation; repo doc validators; toolchain dry-run and task-graph queries (`--dry-run`, `-n`, `bazel query`, `gradle tasks`) | Local changes: in place, with the working-tree guard below. PRs: the `pr-review-*` checkout, or in place with the guard if the PR branch is already checked out locally. |
| **2: tests and mutations** | `--verify` (or `--max`), and the change is trusted | Targeted existing tests for the changed files; scratch tests (`review_scratch_*`) that reproduce a suspected finding; reverted single-line mutation runs; dependency installs inside the throwaway checkout | The reviewer's own throwaway `pr-review-*` checkout only. Never the user's working tree. |
| **Never** | Any flags | See "Never run" below | — |

### Tier 1 Commands

Run only tools that are already installed and whose dependencies are already present. Never install dependencies for tier 1. If a tool can't run, skip it, say so in the report, and rely on CI.

| Kind | Examples |
| :--- | :--- |
| CI status (PRs) | `gh pr checks <n>`; for each failed check, `gh run view <run-id> --log-failed \| tail -n 100` |
| Dart | `dart analyze <paths>`; `dart format --output=none --set-exit-if-changed <paths>` |
| TypeScript | `npx --no-install tsc --noEmit -p <tsconfig>`; `npx --no-install eslint <paths>` (no `--fix`) |
| Python | `ruff check <paths>` (no `--fix`); `ruff format --check <paths>`; `python3 -c "import ast,sys; [ast.parse(open(f).read(), f) for f in sys.argv[1:]]" <paths>` (not `py_compile`, which writes `__pycache__`) |
| Swift | `swift build` only if the package is already resolved; otherwise skip |
| Build / task graph | `make -n`, `bazel query <expr>`, `gradle tasks`, or the build tool's `--dry-run` mode |
| Documents | The repo's doc validators (for example `python3 blueprints/validate_blueprints.py`); JSON Schema validation of changed examples; link checkers |

Scope each tool to the changed files or their package where the tool allows it.

### Working-Tree Guard (In-Place Runs)

Before the first in-place tier 1 command, save `git status --porcelain` output. After the last one, run it again and compare. If they differ, a tool wrote to the working tree. The guard can't see writes to ignored paths (caches, `__pycache__`, build output), so prefer tools and flags that don't write at all. Report the changed paths in the review Summary. Don't revert anything yourself: the user decides what to keep.

### Never Run

With any flags, never run:

- Commands that write fixes: `--fix`, `--write`, `--update-snapshots`, `-u` for snapshot tools, formatters in write mode (`dart format` without `--output=none`, `prettier --write`, `ruff format` without `--check`).
- Code generation that writes into the user's tree.
- Publishing, deploying, pushing, or tagging.
- Full end-to-end suites, or anything that needs production credentials.
- Any command in the user's working tree that isn't tier 1.

## Trust Check

Tier 2 runs code from the change. It runs only when the change is trusted:

- **Local changes** and the user's own branches are trusted.
- **PRs** are trusted when the author has write access to the repo. Look it up with:

  ```bash
  gh api repos/{owner}/{repo}/pulls/<n> --jq .author_association
  ```

  (`gh pr view --json` does not expose this field.) `OWNER`, `MEMBER`, or `COLLABORATOR` means trusted. `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, `FIRST_TIMER`, `NONE`, or a failed lookup means untrusted.

Untrusted PRs get tier 1 only, even with `--verify` or `--max`. No flag overrides this. The review Summary says: "`--verify` limited to tier 1: PR author is not a repo collaborator" (or "author lookup failed").

## Throwaway Checkout

Tier 2 needs a checkout the user never works in. Use the `pr-review-*` prefix so Step 6 cleans it up.

- **Remote PR not checked out locally**: SKILL.md Step 1 already creates `$pr_review_dir_a`.
- **Local changes, or a PR branch already checked out locally**: clone the local repo, copy the working-tree changes into `$pr_review_dir_a`, verify the diff stat matches, and commit a review snapshot so `HEAD` inside the checkout represents the exact change under review:

  ```bash
  repo_root=$(git rev-parse --show-toplevel)
  pr_review_dir_a=$(mktemp -d -t pr-review-XXXXXX)
  git clone --quiet "$repo_root" "$pr_review_dir_a"
  git -C "$pr_review_dir_a" checkout --quiet --detach "$(git -C "$repo_root" rev-parse HEAD)"
  # Staged and unstaged changes to tracked files.
  git -C "$repo_root" diff --binary HEAD > "$pr_review_dir_a/.review.patch"
  if [[ -s "$pr_review_dir_a/.review.patch" ]]; then
    git -C "$pr_review_dir_a" apply "$pr_review_dir_a/.review.patch"
  fi
  rm "$pr_review_dir_a/.review.patch"
  # Untracked files that aren't ignored.
  git -C "$repo_root" ls-files -z --others --exclude-standard \
    | tar -C "$repo_root" --null -T - -cf - | tar -C "$pr_review_dir_a" -xf -
  ```

  For a local PR branch with no working-tree changes, the checkout step alone is enough. Before committing the snapshot, check that `git -C "$pr_review_dir_a" diff --stat HEAD` matches `git -C "$repo_root" diff --stat HEAD`. Then commit the working-tree state (skipped when nothing is staged):

  ```bash
  git -C "$pr_review_dir_a" add -A && (
    git -C "$pr_review_dir_a" diff --cached --quiet \
    || git -C "$pr_review_dir_a" -c user.name=review -c user.email=review@localhost -c commit.gpgsign=false commit -q -m "review snapshot"
  )
  ```

  Invariant: the checkout's `HEAD` is the change under review (use `git -C "$pr_review_dir_a" rev-parse HEAD` as Head Ref in the reviewer prompt), and `git -C "$pr_review_dir_a" status --porcelain` shows only edits the reviewer made.

Dependency installs for tier 2 (`npm ci`, `dart pub get`, a virtualenv inside the checkout) happen only inside `$pr_review_dir_a`. If an install fails (for example, offline), skip tier 2, note it in the report, and finish the review.

### Per-Reviewer Checkouts

Each `--verify` reviewer gets its own checkout so mutation runs in one lane never corrupt files another reviewer is reading:

- **Single reviewer (`--verify`)**: uses `$pr_review_dir_a`.
- **Panel (`--panel --verify`)**: build `$pr_review_dir_a` completely (clone, patch, snapshot commit, and dependency install), then copy it for lane B:

  ```bash
  pr_review_dir_b=$(mktemp -d -t pr-review-XXXXXX)
  cp -a "$pr_review_dir_a/." "$pr_review_dir_b/"
  ```

  If the dependency step created a Python virtualenv with an editable install (`pip install -e` or `uv sync`), its `.pth` / editable finder still points at `$pr_review_dir_a`; rerun the editable install inside `$pr_review_dir_b` before launching the panel. Pass `$pr_review_dir_a` in lane A's prompt and `$pr_review_dir_b` in lane B's prompt.

## Mutation Testing

Mutations are allowed only inside a `pr-review-*` checkout whose Verification Mandate includes the sentence `"This checkout is reserved for you alone."` Two mutation patterns settle most test and correctness questions:

1. **Break the changed implementation**: invert a changed condition, remove a guard, or change a boundary in the production code, then run the covering test. If the test still passes, you have `Executed` proof of a coverage gap. If the test fails, the case is guarded—drop the finding.
2. **Revert the fix hunk**: revert the production fix while keeping the new regression test, then run the test. If the test passes without the fix, the regression test does not catch the bug.

Follow this five-step discipline for every mutation run:

1. Name the candidate finding and the exact test command before touching any file.
2. Apply **one** small mutation to a tracked file in your reserved checkout. Never stack multiple mutations at once.
3. Run only the targeted test file or test case, never the full suite.
4. Record the command and trimmed output at `Evidence: Executed`.
5. Immediately revert the file with `git -C "$checkout" restore <path>` and confirm `git -C "$checkout" status --porcelain` shows only `review_scratch_*` files before running another command or returning your report.

## Tier 2 Rules for Reviewers

The orchestrator passes these rules to the reviewer in a Verification Mandate block:

- Run commands only inside the named `pr-review-*` checkout.
- Run at most 8 tier 2 commands in total, at most 3 of them mutation runs. Prioritize falsification runs on `critical` and `high` findings first. Every run counts, including reruns of the same test, so get a scratch test right before running it. Before each command, name the finding or hypothesis it checks.
- Prefer existing tests for the changed files. To confirm a suspected bug with no covering test, write a small `review_scratch_*` test file inside the checkout.
- Revert every mutation with `git restore <path>` before the next command and before reporting.
- Stop any command that runs longer than 10 minutes, and report that it timed out.
- A finding a command confirms gets `Evidence: Executed` (format below). A finding a test disproves is dropped and recorded in `## Checked and Found Clean`.
- A scratch test that reproduces a bug goes in the finding's `Suggestion` as the proposed regression test.

## Orchestrator Clean Check

Before Step 5's citation audit, inspect every `--verify` checkout:

```bash
git -C "$dir" status --porcelain
```

Any line other than `?? review_scratch_*` means the reviewer left a mutation or untracked file behind. Restore tracked files and remove unexpected untracked files:

```bash
git -C "$dir" restore .
git -C "$dir" clean -fd -e 'review_scratch_*'
```

Note the leftover edit in the review Summary, and run Step 5's citation audit across **all** findings from that reviewer (not only `critical` and `high`).

## Evidence Format

Every finding carries an `Evidence` field whose first token is its tier from [evidence.md](evidence.md#tiers). For a command run in tier 1 or tier 2:

```markdown
- **Evidence**: `Executed` — `<command>` — <trimmed output, at most 15 lines>
```

Keep only the lines that show the result (the failing assertion, the unexpected pass under a mutation, the error, or the analyzer message).
