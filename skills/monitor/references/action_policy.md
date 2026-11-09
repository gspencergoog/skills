# Proactive Action Policy

This document defines the division of work between the background monitor daemon and the main agent when `monitor_prs.py` emits events, how control commands are sent to the daemon, when the main agent must pause for user confirmation, and what neither agent may ever do.

## 1. Ownership and Agent Boundaries

Check `prs[pr].mine` before taking any action:

- When `mine` is `true` (the authenticated GitHub user authored the pull request), the monitor daemon collects events, sends a notification message to the conversation via `agentapi send-message`, sets an `AckGate`, and waits for an acknowledgment. The main agent handles all code edits, branch rebases, merge conflict resolutions, flaky-CI reruns, and review feedback dashboards.
- When `mine` is `false` in author mode, the pull request is watch-only. `monitor_prs.py` updates the `stack_board` pane in place, and neither agent ever pushes commits, reruns workflows, or changes branches on that pull request.
- Under `--reviews` (reviewer mode), `monitor_prs.py` tracks incoming and active reviews across the user's review queue (`mine` is `false` on every reviewed pull request). By default in `--reviews --daemon`, the daemon updates the A2UI review-queue pane (`REVIEW_BOARD_COMPONENTS`), `last.json`, and `events.jsonl` in the background without waking the main agent. When `--wake-on-review` is passed, the daemon sends an `agentapi send-message` batch notification on actionable reviewer events. Neither agent ever changes branches, pushes commits, or reruns CI jobs on reviewed pull requests.
- While `/monitor` is active, `monitor_prs.py` owns the `stack_board` surface (`surfaceId: "monitor"`) in the A2UI Pane and clears stale surfaces on each sync. Neither agent may call `render_a2ui` to open a separate `progress_board`, `stack_board`, or `git-stack` surface during CI remediation or stack rebases.

## 2. Daemon Control Commands (`control.jsonl`)

The daemon reads commands continuously from `<state-dir>/control.jsonl`. Each command is a single-line JSON object:

- `{"ack": N}`: Acknowledge batch `N`. Clears the `AckGate` and allows the daemon to send the next batch of events.
- `{"pushed_sha": {"<pr>": "<sha>"}}`: Record that the agent pushed a new commit SHA to `<pr>`. Suppresses spurious `head_push` notifications.
- `{"mark_rerun": ["<pr>=<check>@<sha>"]}`: Record that a check was rerun on `<sha>` (dictionary mapping `{"<pr>": "<check>@<sha>"}` is also accepted).
- `{"stop": true}`: Gracefully stop the monitor daemon (exits 0 with `exit_reason: "stopped"`).

The main agent can send commands with a simple shell append:
```bash
echo '{"ack": 1}' >> <state-dir>/control.jsonl
```

## 3. Routing by `suggested_action`

Each item in `events[]` includes `actionable` (`true` or `false`) and an advisory `suggested_action` field. When `--actionable-only` is passed, passive events (`actionable == false`) update `state.json`, `board.a2ui`, and the live A2UI Pane JSONL store in place without waking the conversation.

### `rerun_ci` (Flaky Check Guard in the Main Agent)

Suggested when an effective required check failed on a new commit after passing on the previous head commit, and no rerun has been recorded for `<check>@<head_sha>`:

1. Check that `detail.run_id` is present and `detail.rerun_attempted` is `false`.
2. Inspect the failed job log tail (`gh run view <run_id> --repo <owner/repo> --log-failed | tail -n 120`). If the log shows an obvious infrastructure or transient failure (runner lost, network timeout, registry 5xx, rate limit) or no code diff in the pull request could explain the failure, trigger a single rerun:
   ```bash
   gh run rerun <run_id> --repo <owner/repo> --failed
   ```
3. Record the rerun and acknowledge the batch in `control.jsonl`:
   ```bash
   echo '{"mark_rerun": ["<pr>=<check>@<head_sha>"]}' >> <state-dir>/control.jsonl
   echo '{"ack": <batch_id>}' >> <state-dir>/control.jsonl
   ```
   Never rerun the same check twice on the same commit SHA. If the log shows a deterministic test or compile failure caused by the pull request, proceed to `diagnose_ci`.

### `diagnose_ci` (Required CI Check Failure in the Main Agent)

Suggested when an effective required check fails and `rerun_ci` does not apply:

1. Capture `gh run view <run_id> --repo <owner/repo> --log-failed | tail -n 120` into `<conversation-scratch-dir>/monitor/ci_<repo>_<pr>.log` (or record `detail.url` when `run_id` is null).
2. Read the captured log file and identify the root cause in the local checkout or worktree for the pull request branch (`head_ref`).
3. Check the **Confirmation Gates** in Section 4 before editing or pushing if the fix is wide or risky.
4. Implement the fix, run the relevant linter and unit tests locally to check that the failure is resolved, commit the change on the pull request branch, and push to the remote head branch.
5. Notify the daemon of the pushed SHA and acknowledge the batch:
   ```bash
   echo '{"pushed_sha": {"<pr>": "<new_head_sha>"}}' >> <state-dir>/control.jsonl
   echo '{"ack": <batch_id>}' >> <state-dir>/control.jsonl
   ```

### `resolve_conflicts` and `update_branch` (Main Agent)

Suggested when `mergeable` becomes `CONFLICTING` or `merge_state` becomes `DIRTY` or `BEHIND`:

1. Locate the local worktree for the pull request branch (`head_ref`).
2. Follow the [merge-code](../../merge-code/SKILL.md) skill to merge or rebase against `base_ref` according to the repository's convention, resolving conflicts by reconstructing the intent of both sides and running the test suite.
3. If updating the branch requires `git push --force-with-lease` (for example after a rebase), trigger the confirmation gate in Section 4 before pushing. A clean fast-forward or merge commit that satisfies the gate rules may be pushed directly.
4. Notify the daemon and acknowledge the batch:
   ```bash
   echo '{"pushed_sha": {"<pr>": "<new_head_sha>"}}' >> <state-dir>/control.jsonl
   echo '{"ack": <batch_id>}' >> <state-dir>/control.jsonl
   ```

### `rebase_onto_base` (Stack Parent Merged in the Main Agent)

Suggested on `stack_parent` when the parent pull request in a stack merges:

1. Follow the [git-stack](../../git-stack/SKILL.md) and [merge-code](../../merge-code/SKILL.md) skills to retarget the child pull request onto the parent's base branch (`main`) and cascade-rebase the remaining stack branches.
2. Because rebasing a stack rewrites branch history and requires `--force-with-lease`, present a confirmation gate before force-pushing the rebased stack.
3. Notify the daemon of the new head SHAs and acknowledge the batch in `control.jsonl`.

### `pr_feedback_handler` (Human Review Comments or Changes Requested in the Main Agent)

Suggested on `human_threads` or when `review_decision` becomes `CHANGES_REQUESTED`:

1. Invoke the [pr-feedback-handler](../../pr-feedback-handler/SKILL.md) workflow in interactive dashboard mode so the user reviews the proposed fixes and draft replies per thread.
2. Once the user approves and `pr-feedback-handler` finishes pushing fixes, notify the daemon of the new head SHA and acknowledge the batch:
   ```bash
   echo '{"pushed_sha": {"<pr>": "<new_head_sha>"}}' >> <state-dir>/control.jsonl
   echo '{"ack": <batch_id>}' >> <state-dir>/control.jsonl
   ```

### `celebrate` (Approved and All Checks Green in the Main Agent)

Suggested when a pull request has `review_decision == "APPROVED"` and all required checks pass:

1. Inform the user that the pull request is green and ready to merge (and include `[A2UI Pane](sidecar://a2ui-pane/a2ui-pane/)` when `a2ui-pane` is installed).
2. Do not merge the pull request or enable auto-merge unless the user explicitly asks you to do so.
3. Stop the daemon if monitoring is no longer needed:
   ```bash
   echo '{"stop": true}' >> <state-dir>/control.jsonl
   ```

### `review_pr` and `<a2ui-action>` `trigger_pr_review` (Reviewer Mode)

Suggested on actionable reviewer events under `--reviews`, or triggered when the user clicks **Review** or **Re-review** on a review-queue card:

1. On a `--reviews --wake-on-review` wakeup, summarize the updated review items, link `[A2UI Pane](sidecar://a2ui-pane/a2ui-pane/)`, and acknowledge the batch in `control.jsonl`:
   ```bash
   echo '{"ack": <batch_id>}' >> <state-dir>/control.jsonl
   ```
   Do not start a review automatically on a daemon wakeup unless the user asks you to review a pull request.
2. When the user clicks **Review** or **Re-review** in the pane, the host sends `<a2ui-action>` with `name: "trigger_pr_review"` and `context: {"pr": "<display_id>", "key": "<repo>#<number>", "repo": "<repo>", "number": <number>, "url": "<url>", "headSha": "<sha>"}`:
   - If `agentapi` is on `PATH`, start an isolated conversation for the review:
     ```bash
     env -u ANTIGRAVITY_SOURCE_METADATA agentapi new-conversation --title "Review <repo>#<number>" "/pr-review <url>"
     ```
     Pass only `"/pr-review <url>"` as the prompt. Omit the pull request title and branch name so [pr-review](../../pr-review/SKILL.md) Stage 1 blind diff summary stays unanchored. Reply with `[Review <repo>#<number>](conversation://<id>)`.
   - If `agentapi` is not on `PATH`, run [pr-review](../../pr-review/SKILL.md) inline on `<url>`.
3. When the user clicks **Mute** (`name: "mute_review_pr"`) on an active review row or **Unhide** (`name: "unmute_review_pr"`) inside the bottom **Muted & Dormant** drawer, update `~/.config/monitor/review_overrides.json` and refresh `monitor.jsonl` in place by running `monitor_prs.py --reviews --mute <key> --once` (or `--unmute <key> --once`) against the same `--state` and `--conversation-id`. Muting and unhiding are local queue operations only: never remove reviewers, close pull requests, or post comments on GitHub when handling `mute_review_pr` or `unmute_review_pr`.

## 4. Confirmation Gates

Pause and ask the user for confirmation in the main agent (using `render_a2ui` with `template: "confirm_gate"`, `surfaceId: "monitor-confirm"`, and `stack: true`, or `ask_question` when `a2ui-pane` is unavailable) before pushing when any of the following conditions hold:

1. The push requires `--force` or `--force-with-lease`.
2. The proposed fix changes files outside the pull request's existing diff against `base_ref`.
3. The proposed fix touches more than 5 files.
4. The relevant test suite cannot be executed locally to check the fix before pushing.
5. Three consecutive fix attempts on the same pull request have failed to make CI green.

## 5. Actions Neither Agent May Ever Take

- Never push directly to `main`, `master`, or any shared base branch.
- Never post pull request comments, submit reviews, reply to review threads, or resolve review threads without presenting the draft text to the user and obtaining explicit approval.
- Never merge a pull request or enable `gh pr merge --auto` unless explicitly instructed by the user.
- Never change branches or rerun jobs on pull requests where `mine` is `false`.
