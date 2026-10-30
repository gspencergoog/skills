# Proactive Action Policy

This document defines the division of work between the `flash` watcher subagent and the main agent when `monitor_prs.py` emits events, when the main agent must pause for user confirmation, and what neither agent may ever do.

## 1. Ownership and Agent Boundaries

Check `prs[pr].mine` before taking any action:

- When `mine` is `true` (the authenticated GitHub user authored the pull request), the `flash` watcher subagent handles mechanical pre-triage (`rerun_ci`, log capture, and `convo` badges), and the main agent handles all code edits, branch rebases, merge conflict resolutions, and review feedback dashboards.
- When `mine` is `false`, the pull request is watch-only. `monitor_prs.py` updates the `stack_board` pane in place, and neither agent ever pushes commits, reruns workflows, or modifies branches on that pull request.
- While `/monitor` is active, `monitor_prs.py` owns the `stack_board` surface (`surfaceId: "monitor"`) in the A2UI Pane and clears stale surfaces on each sync. Neither agent may call `render_a2ui` to open a separate `progress_board`, `stack_board`, or `git-stack` surface during CI remediation or stack rebases.

## 2. Routing by `suggested_action`

Each item in `events[]` includes `actionable` (`true` or `false`) and an advisory `suggested_action` field. When `--actionable-only` is passed, passive events (`actionable == false`) update `state.json`, `board.a2ui`, and the live A2UI Pane JSONL store in place without exiting `monitor_prs.py`.

### `rerun_ci` (Flaky Check Guard in the `flash` Subagent)

Suggested when an effective required check failed on a new commit after passing on the previous head commit, and no rerun has been recorded for `<check>@<head_sha>`:

1. Verify `detail.run_id` is present and `detail.rerun_attempted` is `false`.
2. Inspect the failed job log tail (`gh run view <run_id> --repo <owner/repo> --log-failed | tail -n 120`). If the log shows an obvious infrastructure or transient failure (runner lost, network timeout, registry 5xx, rate limit) or no code diff in the pull request could explain the failure, trigger a single rerun inside the `flash` subagent:
   ```bash
   gh run rerun <run_id> --repo <owner/repo> --failed
   ```
3. Relaunch `monitor_prs.py` inside the `flash` subagent with `--mark-rerun "<pr>=<check>@<head_sha>"` without waking the main agent. Never rerun the same check twice on the same commit SHA. If the log shows a deterministic test or compile failure caused by the pull request, save the log tail to `<conversation-scratch-dir>/monitor/ci_<repo>_<pr>.log` and wake the main agent under `diagnose_ci`.

### `diagnose_ci` (Required CI Check Failure in the Main Agent)

Suggested when an effective required check fails and `rerun_ci` does not apply:

1. In the `flash` subagent, capture `gh run view <run_id> --repo <owner/repo> --log-failed | tail -n 120` into `<conversation-scratch-dir>/monitor/ci_<repo>_<pr>.log` (or record `detail.url` when `run_id` is null) and wake the main agent.
2. In the main agent, read the captured log file and identify the root cause in the local checkout or worktree for the pull request branch (`head_ref`).
3. Check the **Confirmation Gates** in Section 3 before editing or pushing if the fix is wide or risky.
4. Implement the fix, run the relevant linter and unit tests locally to verify the failure is resolved, commit the change on the pull request branch, and push to the remote head branch.
5. Run `monitor_prs.py --once --pushed-sha "<pr>=<new_head_sha>"` and resume the `flash` watcher subagent via `send_message`.

### `resolve_conflicts` and `update_branch` (Main Agent)

Suggested when `mergeable` becomes `CONFLICTING` or `merge_state` becomes `DIRTY` or `BEHIND`:

1. Wake the main agent and locate the local worktree for the pull request branch (`head_ref`).
2. Follow the [merge-code](../../merge-code/SKILL.md) skill to merge or rebase against `base_ref` according to the repository's convention, resolving conflicts by reconstructing the intent of both sides and running the test suite.
3. If updating the branch requires `git push --force-with-lease` (for example after a rebase), trigger the confirmation gate in Section 3 before pushing. A clean fast-forward or merge commit that satisfies the gate rules may be pushed directly.
4. Run `monitor_prs.py --once --pushed-sha "<pr>=<new_head_sha>"` and resume the `flash` watcher subagent via `send_message`.

### `rebase_onto_base` (Stack Parent Merged in the Main Agent)

Suggested on `stack_parent` when the parent pull request in a stack merges:

1. Wake the main agent and follow the [git-stack](../../git-stack/SKILL.md) and [merge-code](../../merge-code/SKILL.md) skills to retarget the child pull request onto the parent's base branch (`main`) and cascade-rebase the remaining stack branches.
2. Because rebasing a stack rewrites branch history and requires `--force-with-lease`, present a confirmation gate before force-pushing the rebased stack.

### `pr_feedback_handler` (Human Review Comments or Changes Requested in the Main Agent)

Suggested on `human_threads` or when `review_decision` becomes `CHANGES_REQUESTED`:

1. Wake the main agent and keep the `flash` watcher subagent paused while working through feedback so intermediate local commits do not trigger spurious wakes.
2. Invoke the [pr-feedback-handler](../../pr-feedback-handler/SKILL.md) workflow in interactive dashboard mode so the user reviews the proposed fixes and draft replies per thread.
3. Once the user approves and `pr-feedback-handler` finishes pushing fixes, run `monitor_prs.py --once --pushed-sha "<pr>=<new_head_sha>"` to refresh the board and resume the `flash` watcher subagent via `send_message`.

### `celebrate` (Approved and All Checks Green in the Main Agent)

Suggested when a pull request has `review_decision == "APPROVED"` and all required checks pass:

1. Wake the main agent to let the user know the pull request is green and ready to merge, and include `[A2UI Pane](sidecar://a2ui-pane/a2ui-pane/)`.
2. Do not merge the pull request or enable auto-merge unless the user explicitly asks you to do so.

## 3. Confirmation Gates

Pause and ask the user for confirmation in the main agent (using `render_a2ui` with `template: "confirm_gate"`, `surfaceId: "monitor-confirm"`, and `stack: true`, or `ask_question` when `a2ui-pane` is unavailable) before pushing when any of the following conditions hold:

1. The push requires `--force` or `--force-with-lease`.
2. The proposed fix modifies files outside the pull request's existing diff against `base_ref`.
3. The proposed fix touches more than 5 files.
4. The relevant test suite cannot be executed locally to verify the fix before pushing.
5. Three consecutive fix attempts on the same pull request have failed to make CI green.

## 4. Actions Neither Agent May Ever Take

- Never push directly to `main`, `master`, or any shared base branch.
- Never post pull request comments, submit reviews, reply to review threads, or resolve review threads without presenting the draft text to the user and obtaining explicit approval.
- Never merge a pull request or enable `gh pr merge --auto` unless explicitly instructed by the user.
- Never modify branches or rerun jobs on pull requests where `mine` is `false`.
