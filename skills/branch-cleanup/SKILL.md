---
name: branch-cleanup
description: Clean up local and remote tracking git branches that have already been merged to the main branch. Use this when a repository has stale local or remote branches that have been merged (either normally or via squash-and-merge) and need to be deleted safely.
---

# Git Branch Cleanup Skill

Finds branches whose work has landed and deletes them, together with their
worktrees and, on request, their remote tracking branches.

The script runs in two phases. `analyze` inspects the repository and writes a
plan saying what it believes is safe to delete and why. `--execute` reads that
plan back, checks every branch tip still matches, and only then deletes
anything. Nothing is deleted without a plan.

## Workflow

1. Analyze. This fetches every remote first, because a merge verdict measured
   against stale refs is worthless.

   ```bash
   python3 scripts/cleanup_branches.py --repo-dir /path/to/repo
   ```

   The plan is printed and written to `<repo>/.git/branch-cleanup-plan.json`
   alongside a readable `.md` version. Use `--out` to put it elsewhere.

2. Read the plan. Every branch carries a verdict and the evidence behind it.
   Check the *Needs review* section by hand.

3. Execute.

   ```bash
   python3 scripts/cleanup_branches.py --execute /path/to/plan.json
   ```

## Verdicts

Only `CONTAINED` and `SQUASHED` are deleted.

| Verdict | Meaning |
| --- | --- |
| `CONTAINED` | The branch is an ancestor of a target, or every commit has an equivalent patch there. |
| `SQUASHED` | Merging the branch into the target changes nothing, so its content already landed. |
| `DIVERGED` | Neither git nor the pull request data shows the work landing. |
| `UNKNOWN` | The evidence is inconclusive. Reported for review, never deleted. |
| `PROTECTED` | Excluded by rule. The reason is recorded in the plan. |

Absence of evidence is reported as `UNKNOWN` rather than folded into
`DIVERGED`, because the two need different responses from you.

## How merges are detected

Three git signals run against each target, in order of strength:

1. **Ancestry** — `git merge-base --is-ancestor`.
2. **Patch equivalence** — `git cherry`, which catches rebases and
   cherry-picks.
3. **Squash equivalence** — `git merge-tree --write-tree` against the target.
   When the resulting tree equals the target's own tree, the branch adds
   nothing. This is what detects a squash merge, where both the commit ids and
   the patch ids were rewritten. It needs git 2.38 or newer; on older git the
   affected branches come back `UNKNOWN`.

If git finds nothing and the GitHub CLI is available, a forge pass asks
whether the branch had a pull request. A merged pull request is checked
against its own base branch, not against the recorded head commit, because
rebases routinely break that correspondence. Commits still left over are
attributed to other merged pull requests, which is how stacked pull requests
are resolved. Pass `--no-forge` to skip this.

## What is protected

- Every integration target, matched by branch name. A target resolved as
  `upstream/v1_0` protects the local `v1_0`, so a branch can never be judged
  merged into itself.
- The default branch published by any remote, plus `main` and `master`.
- The branch checked out in the main worktree. A branch in a *linked* worktree
  is fair game: removing it with its worktree is the point of the tool.
- Branches with an open pull request.
- Anything matching `--protect`.

Two further rules apply at deletion time:

- A branch whose worktree holds uncommitted or untracked changes is withheld,
  and the plan says so. The check runs again during execution, so work
  started after the plan was written is still safe.
- Execution stops entirely if any planned branch tip has moved since the plan
  was written (exit code 3). Re-analyze to get a fresh plan.

Every deleted branch is tagged first, so a mistake is recoverable:

```bash
git branch <name> archive/<name>
```

Remote branches are never touched unless you pass `--delete-remote`. When the
repository has more than one remote you must also name it with
`--allow-remote`, so a cleanup aimed at your fork cannot delete a branch on a
shared upstream.

## Options

| Option | Effect |
| --- | --- |
| `--repo-dir <path>` | Repository to inspect. Defaults to the working directory. If the path is not itself a repository but contains exactly one, that one is used. |
| `--targets <a,b>` | Integration branches to measure against. Repeatable. Defaults to the detected default branch. |
| `--target-ref local\|remote` | Resolve targets to local branches or remote-tracking refs. Default `remote`. Among several candidates the most advanced wins, so a lagging fork does not become the target. |
| `--protect <glob>` | Never delete matching branches. Repeatable. |
| `--only <glob>` / `--exclude <glob>` | Restrict which branches are considered. Repeatable. |
| `--delete-remote` | Allow deleting remote tracking branches. |
| `--allow-remote <name>` | Permit deletions on that remote. Required when the repository has more than one. |
| `--archive-tag-prefix <p>` | Prefix for recovery tags. Default `archive/`. |
| `--no-archive` | Delete without creating recovery tags. |
| `--no-fetch` | Do not fetch; judge against local refs. |
| `--allow-stale` | Continue when fetching fails. |
| `--no-forge` | Skip the pull request pass. |
| `--out <path>` | Where to write the plan. |
| `--json` | Print the plan as JSON and write nothing. |

Exit codes: `0` success, `1` error, `2` the repository path was ambiguous,
`3` the plan no longer matches the repository.

## Limitations

- Archive tags accumulate. Remove them yourself when you no longer want them:
  `git tag -d archive/<name>`.
- Squash detection needs git 2.38 or newer.
- The forge pass is GitHub-specific and needs an authenticated `gh`.
- Detection writes unreferenced git objects, which a later `git gc` reclaims.

## Tests

```bash
python3 scripts/tests/run_coverage.py
```

This runs the suite and reports line coverage, failing below 85%. The tests
build real temporary repositories; only the GitHub CLI is stubbed.
