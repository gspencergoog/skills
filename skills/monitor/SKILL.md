---
name: monitor
description: >-
  Watches GitHub pull requests or the user's review queue from the current
  conversation for review status, CI check completions, merge conflicts, review
  assignment, and stack state changes, updating a live stack_board A2UI pane
  from a background daemon and waking the main agent via agentapi to fix CI
  failures and merge conflicts on the user's pull requests. Use when the user
  runs /monitor, asks to watch or babysit a pull request, stack, or review
  queue, wants automatic CI or conflict fixes, or asks to be notified when a
  pull request is ready to merge. Flags: --reviews, --wake-on-review, --stack,
  --actionable-only, --until, --events, --timeout, --once, --no-a2ui, --daemon,
  --source. Don't use for reviewing a pull request (use pr-review) or addressing
  comments once (use pr-feedback-handler).
---

# PR Monitor (`/monitor`)

`monitor` watches one or more GitHub pull requests across a session. Its helper script, [scripts/monitor_prs.py](scripts/monitor_prs.py), polls GitHub notifications (`If-Modified-Since`, free `304` responses every 60 seconds) and diffs each pull request against a per-conversation state file. GraphQL refetches are targeted: every `--poll` interval (default 120 seconds) it refetches only flagged pull requests (named in a notification, CI still `PENDING`, mergeability `UNKNOWN`, events in the pending debounce batch, or a `pushed_sha` in `control.jsonl`), and makes no GraphQL call when none are flagged.

A full refresh of every watched pull request runs every `--sweep` interval (default 600 seconds). With `--no-notifications`, every `--poll` step is a full refresh. Each GraphQL call logs its point cost to `stderr` (`monitor: graphql cost=N remaining=R ...`).

### Data source: `packrat` or direct GitHub

`--source auto` (the default) reads pull request data from a running `packrat serve` when its heartbeat is fresh (`v_service_health.beat_age_s <= 180`), and polls GitHub directly otherwise. `packrat` is optional: without it, `/monitor` behaves as described above. In `packrat` mode the daemon:

- leases the watched pull requests with `packrat watch add --consumer monitor` and renews the lease every 5 minutes,
- waits on `packrat changes --wait` (timeout equal to `--debounce`) instead of polling notifications and GraphQL, and re-reads only the changed pull requests from `packrat`'s read-only views,
- turns a `control.jsonl` `pushed_sha` into `packrat refresh`,
- expands `--stack` from `packrat`'s mirror for repositories registered with `packrat repo add`, and uses the `gh pr list` walk for other repositories,
- falls back to direct GitHub polling for the rest of the run if the service goes stale, logging it once.

`--source packrat` exits with an error when the service isn't running; `--source github` never reads `packrat`. The payload's `source` field and the board subtitle (`via packrat`, or `via GitHub (packrat unavailable)` after a fallback) show which source is in use. `packrat` stores neither assignees nor the head commit's committer, so those keep their last known values in `packrat` mode. Remediation still calls `gh` directly in every mode: `gh run rerun`, `gh run view --log-failed`, pushes, and review replies.

On every refresh, `monitor_prs.py` writes `<conversation-scratch-dir>/monitor/board.a2ui` and replaces the `stack_board` surface directly in the `a2ui-pane` store, streaming live badge and CI ETA updates to the open pane at zero LLM tokens. It replaces one file per conversation, `<a2ui-pane-plugin-data>/conversations/<conversationId>/monitor.jsonl`, which always holds a single `createSurface` line with the full board. It never appends. When launched with `--daemon`, it runs as a persistent background process, handles flaky-check reminder logic, and wakes the conversation via `agentapi send-message` whenever actionable events occur on `mine: true` pull requests.

Read [references/action_policy.md](references/action_policy.md) before acting on any event, [references/output_schema.md](references/output_schema.md) for the JSON payload contract, CLI flags, event kinds, and exit codes, and [references/daemon_protocol.md](references/daemon_protocol.md) for the control file protocol.

## 1. Resolve Target Pull Requests or Review Queue

Choose between author mode (explicit pull request targets) and reviewer mode (`--reviews`):

- **Author / explicit targets mode (default)**: Pass the target pull requests explicitly on the `monitor_prs.py` command line. Never rely on the script to guess targets from the current git branch:
  1. Include every pull request the user names in their prompt (`owner/repo#123`, a pull request URL, or `#123` with `--repo owner/repo`).
  2. Add every open pull request already discussed or created in the current conversation.
  3. Pass `--stack` when the user asks to watch a stack or when any target pull request is part of a branch stack.
- **Review queue mode (`--reviews`)**: Pass `--reviews` (with no positional pull request arguments) when the user asks to watch their incoming review queue (`/monitor --reviews`).
  1. Omit `--repo` to watch open review requests and active reviews across all repositories, or pass `--repo owner/repo` to scope the queue to a single repository.
  2. Under `--reviews`, `--until` defaults to `none` so an empty or all-closed queue keeps watching for new review requests.
  3. In `--reviews --daemon`, `monitor_prs.py` updates the A2UI review-queue pane, `last.json`, and `events.jsonl` in the background at zero token cost and suppresses `agentapi send-message` wakeups by default. Pass `--wake-on-review` only when the user explicitly asks to be notified in chat on new or updated review requests.

Store all monitor files inside the conversation scratch directory:

- State snapshot: `<conversation-scratch-dir>/monitor/state.json`
- Event history log: `<conversation-scratch-dir>/monitor/events.jsonl`
- Latest output file: `<conversation-scratch-dir>/monitor/last.json`
- A2UI Express DSL file: `<conversation-scratch-dir>/monitor/board.a2ui`
- Control commands file: `<conversation-scratch-dir>/monitor/control.jsonl`
- Captured CI failure logs: `<conversation-scratch-dir>/monitor/ci_<repo>_<pr>.log`

## 2. Initial Snapshot and Live Pane Setup

Before starting the background daemon (and after pushing a fix), run a single pass with `--once`. Replace `<PR_1> <PR_2>` with `--reviews` for the review queue:

```bash
python3 <skill-dir>/scripts/monitor_prs.py \
  <PR_1> <PR_2> \
  --state <conversation-scratch-dir>/monitor/state.json \
  --log <conversation-scratch-dir>/monitor/events.jsonl \
  --output <conversation-scratch-dir>/monitor/last.json \
  --conversation-id <current-conversation-id> \
  --once
```

1. When `a2ui-pane` is installed, `--conversation-id` creates or replaces the `stack_board` surface (`surfaceId: "monitor"`) directly in the store. When it creates the board, it also deletes any other surfaces (such as a prior `git-stack` or `progress_board` card) so `"monitor"` is the sole board in the pane. `payload.a2ui.store_status` reports the result:
   - `written`: the board file was replaced (`store_updated` is `true`).
   - `unchanged`: the board already shows the same pull requests. Only the status timestamp differs, so nothing was written.
   - `dismissed`: the user closed the board (or pressed **Acknowledge**, which removes it), and this tick had no actionable event. The board stays gone until an actionable event, a `--once` pass, or the first sync of a newly started process. Do not re-render it.
   - `unavailable`: there is no store, the lock timed out, or a legacy flat store has not been migrated yet.

   Only when `store_status` is `unavailable` and `render_a2ui` is available, read `<conversation-scratch-dir>/monitor/board.a2ui` and call `render_a2ui` once with `template: "stack_board"`, `surfaceId: "monitor"`, `mode: "pane"`, and `replace: true` (omitting `stack: true` so stale surfaces are cleared). Include `[A2UI Pane](sidecar://a2ui-pane/a2ui-pane/)` in your reply whenever the pane is created or updated.
2. While `/monitor` is active, never call `render_a2ui` to create a separate `progress_board`, `stack_board`, or `git-stack` surface during remediation or stack rebases. `monitor_prs.py` owns the pane and updates `surfaceId: "monitor"` directly on each `--once` pass and polling tick.
3. If the initial pass already surfaced actionable events that require workspace work (`diagnose_ci`, `resolve_conflicts`, `update_branch`, `rebase_onto_base`, or `pr_feedback_handler`), handle them immediately under Step 4 before starting the background daemon.
4. If the initial pass returned exit code `3` (`exit_reason == "terminal"`), finish under Step 5 without starting a daemon.

## 3. Run the Persistent Background Daemon

Start `monitor_prs.py` in the background with `--daemon` using `run_command` (`WaitMsBeforeAsync: 500, IsDaemon: true`):

```bash
python3 <skill-dir>/scripts/monitor_prs.py \
  <PR_1> <PR_2> \
  --state <conversation-scratch-dir>/monitor/state.json \
  --log <conversation-scratch-dir>/monitor/events.jsonl \
  --output <conversation-scratch-dir>/monitor/last.json \
  --conversation-id <current-conversation-id> \
  --daemon
```

1. If `convo` is on `PATH`, update conversation status: `convo --waiting "Watching <short PR list>"`.
2. The daemon acquires `<scratch>/monitor/daemon.lock`, writes `<scratch>/monitor/armed.json`, and begins background polling.
3. If the daemon is already running (exit code 4), another process owns the lock; do not spawn a duplicate.
4. End your turn. The daemon will wake the conversation via `agentapi send-message` whenever actionable events are detected on pull requests you own.

## 4. Remediate in the Main Agent and Acknowledge

When the daemon wakes the conversation, read `<conversation-scratch-dir>/monitor/last.json`:

1. Follow [references/action_policy.md](references/action_policy.md) for each event on pull requests where `mine` is `true`.
2. When remediating:
   - For `rerun_ci`: run `gh run rerun <run_id> --failed` and send `{"mark_rerun": ["<pr>=<check>@<head_sha>"]}` and `{"ack": <batch_id>}` to `control.jsonl`.
   - For code fixes: commit and push the fix, then send `{"pushed_sha": {"<pr>": "<new_head_sha>"}}` and `{"ack": <batch_id>}` to `control.jsonl`.
   - For review feedback or conflict resolution: execute the workflow, push, and send `{"pushed_sha": ...}` and `{"ack": <batch_id>}` to `control.jsonl`.
3. Commands can be sent with a standard shell append:
   ```bash
   echo '{"ack": 1}' >> <conversation-scratch-dir>/monitor/control.jsonl
   ```
4. The daemon automatically consumes the commands from `control.jsonl`, updates its internal state, and continues monitoring.
5. When the user clicks **Review** or **Re-review** on a `--reviews` card, the pane sends `<a2ui-action>` with `name: "trigger_pr_review"` and `context: {"pr": "<display_id>", "key": "<repo>#<number>", "repo": "<repo>", "number": <number>, "url": "<url>", "headSha": "<sha>"}`:
   - When `agentapi` is on `PATH`, start an isolated review conversation so the `/monitor` session stays lightweight:
     ```bash
     env -u ANTIGRAVITY_SOURCE_METADATA agentapi new-conversation --title "Review <repo>#<number>" "/pr-review <url>"
     ```
     Pass only `"/pr-review <url>"` as the prompt. Omit the pull request title and branch name so [pr-review](../pr-review/SKILL.md) Stage 1 blind diff summary stays unanchored. Reply in chat with `[Review <repo>#<number>](conversation://<id>)`.
   - When `agentapi` is not on `PATH`, run [pr-review](../pr-review/SKILL.md) inline on `<url>`.

## 5. Clean Exit

When monitoring is complete or all watched pull requests reached `--until` (`exit_reason == "terminal"`):

1. Stop the daemon gracefully:
   ```bash
   echo '{"stop": true}' >> <conversation-scratch-dir>/monitor/control.jsonl
   ```
2. When `convo` is on `PATH`, clear the waiting badge (`convo --resume`).
3. Summarize the final state of each watched pull request for the user (and include `[A2UI Pane](sidecar://a2ui-pane/a2ui-pane/)` when `a2ui-pane` is installed so they can inspect the completed board).
