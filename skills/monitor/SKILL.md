---
name: monitor
description: >-
  Watches one or more GitHub pull requests from the current conversation for
  review status, CI check completions, merge conflicts, review assignment, and
  stack state changes, updating a live stack_board A2UI pane from a background
  daemon and waking the main agent via agentapi to fix CI failures and merge
  conflicts on the user's own pull requests. Use when the user runs /monitor,
  asks to watch or babysit a pull request or stack, wants automatic CI or
  conflict fixes as checks finish, or asks to be notified when a pull request is
  green and ready to merge. Flags: --stack, --actionable-only, --until, --events,
  --timeout, --once, --no-a2ui, --daemon. Don't use for reviewing a pull request
  (use pr-review) or addressing comments once (use pr-feedback-handler).
---

# PR Monitor (`/monitor`)

`monitor` watches one or more GitHub pull requests across a session. Its helper script, [scripts/monitor_prs.py](scripts/monitor_prs.py), polls GitHub notifications (`If-Modified-Since`, free `304` responses every 60 seconds) alongside periodic GraphQL refreshes (every 120 seconds) and diffs each pull request against a per-conversation state file.

On every refresh, `monitor_prs.py` writes `<scratch>/monitor/board.a2ui` and appends `stack_board` updates directly to the `a2ui-pane` conversation JSONL store (`~/.gemini/jetski/plugin_data/a2ui-pane/conversations/<conversationId>.jsonl`), streaming live badge and CI ETA updates to the open pane at zero LLM tokens. When launched with `--daemon`, it runs as a persistent background process, handles flaky-check reminder logic, and wakes the conversation via `agentapi send-message` whenever actionable events occur on `mine: true` pull requests.

Read [references/action_policy.md](references/action_policy.md) before acting on any event, [references/output_schema.md](references/output_schema.md) for the JSON payload contract, CLI flags, event kinds, and exit codes, and [references/daemon_protocol.md](references/daemon_protocol.md) for the control file protocol.

## 1. Resolve Target Pull Requests

Pass the target pull requests explicitly on the `monitor_prs.py` command line. Never rely on the script to guess targets from the current git branch:

1. Include every pull request the user names in their prompt (`owner/repo#123`, a pull request URL, or `#123` with `--repo owner/repo`).
2. Add every open pull request already discussed or created in the current conversation.
3. Pass `--stack` when the user asks to watch a stack or when any target pull request is part of a branch stack.

Store all monitor files inside the conversation scratch directory:

- State snapshot: `<conversation-scratch-dir>/monitor/state.json`
- Event history log: `<conversation-scratch-dir>/monitor/events.jsonl`
- Latest output file: `<conversation-scratch-dir>/monitor/last.json`
- A2UI Express DSL file: `<conversation-scratch-dir>/monitor/board.a2ui`
- Control commands file: `<conversation-scratch-dir>/monitor/control.jsonl`
- Captured CI failure logs: `<conversation-scratch-dir>/monitor/ci_<repo>_<pr>.log`

## 2. Initial Snapshot and Live Pane Setup

Before launching the background daemon (and after pushing a fix), run a single immediate pass with `--once`:

```bash
python3 <skill-dir>/scripts/monitor_prs.py \
  <PR_1> <PR_2> \
  --state <conversation-scratch-dir>/monitor/state.json \
  --log <conversation-scratch-dir>/monitor/events.jsonl \
  --output <conversation-scratch-dir>/monitor/last.json \
  --conversation-id <current-conversation-id> \
  --once
```

1. When `a2ui-pane` is installed, `--conversation-id` creates or updates the `stack_board` surface (`surfaceId: "monitor"`) directly in the conversation JSONL store (`payload.a2ui.store_updated == true`) and deletes any stale surfaces (such as a prior `git-stack` or `progress_board` card) so `"monitor"` is the sole board in the pane. If `store_updated` is `false` and `render_a2ui` is available, read `<conversation-scratch-dir>/monitor/board.a2ui` and call `render_a2ui` once with `template: "stack_board"`, `surfaceId: "monitor"`, `mode: "pane"`, and `replace: true` (omitting `stack: true` so stale surfaces are cleared). Include `[A2UI Pane](sidecar://a2ui-pane/a2ui-pane/)` in your reply whenever the pane is created or updated.
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

## 5. Clean Exit

When monitoring is complete or all watched pull requests reached `--until` (`exit_reason == "terminal"`):

1. Stop the daemon gracefully:
   ```bash
   echo '{"stop": true}' >> <conversation-scratch-dir>/monitor/control.jsonl
   ```
2. When `convo` is on `PATH`, clear the waiting badge (`convo --resume`).
3. Summarize the final state of each watched pull request for the user and include `[A2UI Pane](sidecar://a2ui-pane/a2ui-pane/)` so they can inspect the completed board.
