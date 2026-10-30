---
name: monitor
description: >-
  Watches one or more GitHub pull requests from the current conversation for
  review status, CI check completions, merge conflicts, review assignment, and
  stack state changes, updating a live stack_board A2UI pane from a background
  flash watcher subagent and waking the main agent to fix CI failures and merge
  conflicts on the user's own pull requests. Use when the user runs /monitor,
  asks to watch or babysit a pull request or stack, wants automatic CI or
  conflict fixes as checks finish, or asks to be notified when a pull request is
  green and ready to merge. Flags: --stack, --actionable-only, --until, --events,
  --timeout, --once, --no-a2ui. Don't use for reviewing a pull request (use
  pr-review) or addressing comments once (use pr-feedback-handler).
---

# PR Monitor (`/monitor`)

`monitor` watches one or more GitHub pull requests across a session. Its helper script, [scripts/monitor_prs.py](scripts/monitor_prs.py), polls GitHub notifications (`If-Modified-Since`, free `304` responses every 60 seconds) alongside periodic GraphQL refreshes (every 120 seconds) and diffs each pull request against a per-conversation state file.

On every refresh, `monitor_prs.py` writes `<scratch>/monitor/board.a2ui` and appends `stack_board` updates directly to the `a2ui-pane` conversation JSONL store (`~/.gemini/jetski/plugin_data/a2ui-pane/conversations/<conversationId>.jsonl`), streaming live badge and CI ETA updates to the open pane at zero LLM tokens. A lightweight `flash` watcher subagent runs the blocking loop with `--actionable-only`, handles one-time flaky CI reruns and failing-log capture, and wakes the main agent only when workspace remediation or terminal completion occurs.

Read [references/action_policy.md](references/action_policy.md) before acting on any event and [references/output_schema.md](references/output_schema.md) for the JSON payload contract, CLI flags, event kinds, and exit codes.

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
- Captured CI failure logs: `<conversation-scratch-dir>/monitor/ci_<repo>_<pr>.log`

## 2. Initial Snapshot and Live Pane Setup

Before launching the `flash` watcher subagent (and after the main agent pushes a fix), run a single immediate pass with `--once`:

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
3. If the initial pass already surfaced actionable events that require main-agent workspace work (`diagnose_ci`, `resolve_conflicts`, `update_branch`, `rebase_onto_base`, or `pr_feedback_handler`), handle them immediately under Step 4 before starting the watcher subagent.
4. If the initial pass returned exit code `3` (`exit_reason == "terminal"`), finish under Step 5 without starting a watcher.

## 3. Delegate the Watch Loop to a `flash` Subagent

Launch a single `self` subagent on the `flash` model (`invoke_subagent` with `TypeName: "self"`, `Model: "flash"`, `Role: "PR Monitor Watcher"`). Reuse the same subagent across the session via `send_message` rather than spawning a new subagent after each remediation round.

Give the `flash` watcher subagent a focused brief containing:

1. Explicitly forbid the subagent from calling `invoke_subagent`, `define_subagent`, or `swarm`, and forbid it from editing repository files or running `git push`.
2. Pass the exact watcher command:
   ```bash
   python3 <skill-dir>/scripts/monitor_prs.py \
     <PR_1> <PR_2> \
     --state <conversation-scratch-dir>/monitor/state.json \
     --log <conversation-scratch-dir>/monitor/events.jsonl \
     --output <conversation-scratch-dir>/monitor/last.json \
     --conversation-id <parent-conversation-id> \
     --actionable-only
   ```
   Include optional user-requested flags (`--stack`, `--until all-green`, `--until merged`, `--events <list>`, `--timeout <seconds>`).
3. When `convo` is on `PATH`, instruct the subagent to run `convo --waiting "Watching <short PR list>"` before starting `monitor_prs.py` (`WaitMsBeforeAsync: 500`, without polling in a loop), and run `convo --resume` when returning to the main agent.
4. Instruct the subagent to run pre-triage before waking the main agent:
   - When `monitor_prs.py` exits `0` and the only actionable events have `suggested_action == "rerun_ci"` on a `mine: true` pull request, inspect `gh run view <run_id> --repo <owner/repo> --log-failed | tail -n 120`. If the failure is transient or infrastructure-related, run `gh run rerun <run_id> --repo <owner/repo> --failed` and relaunch `monitor_prs.py` with `--mark-rerun "<pr>=<check>@<head_sha>"` without waking the main agent.
   - When an actionable event requires `diagnose_ci` (or a rerun check fails again), save `gh run view <run_id> --repo <owner/repo> --log-failed | tail -n 120` to `<conversation-scratch-dir>/monitor/ci_<repo>_<pr>.log` and finish the subagent turn with the failure summary and log file path.
   - For any other exit (`resolve_conflicts`, `update_branch`, `rebase_onto_base`, `pr_feedback_handler`, `celebrate`, timeout `2`, terminal `3`, or error `1`), finish the subagent turn and report `exit_code`, `summary`, and `<conversation-scratch-dir>/monitor/last.json` to the main agent.

After invoking or messaging the `flash` watcher subagent, stop calling tools and end your turn; the host wakes you automatically when the subagent replies.

## 4. Remediate in the Main Agent and Resume

When the `flash` watcher subagent wakes the main agent, read `<conversation-scratch-dir>/monitor/last.json` and any captured `<conversation-scratch-dir>/monitor/ci_<repo>_<pr>.log`:

| Exit code | `exit_reason` | Meaning | Main agent action |
| :--- | :--- | :--- | :--- |
| `0` | `change` | Actionable events ready for remediation | Follow [references/action_policy.md](references/action_policy.md) for each event on pull requests where `mine` is `true`, then resume the `flash` subagent |
| `1` | error | Startup error or 5 consecutive GitHub failures | Surface `stderr` to the user and stop |
| `2` | `timeout` | `--timeout` elapsed with no matching changes | Report the timeout to the user and ask whether to resume watching |
| `3` | `terminal` | All watched pull requests reached `--until` | Proceed to Step 5 |

After every fix that pushes a commit (`<new_head_sha>`), run a `--once` refresh with `--pushed-sha "<pr>=<new_head_sha>"` so the pane updates immediately and self-triggered `head_push` events are suppressed, then send a message to the existing `flash` watcher subagent (`send_message`) instructing it to resume `monitor_prs.py` with `--pushed-sha "<pr>=<new_head_sha>"`.

## 5. Clean Exit

When `monitor_prs.py` exits with code `3` (`exit_reason == "terminal"`):

1. When `convo` is on `PATH`, clear the waiting badge (`convo --resume`).
2. Summarize the final state of each watched pull request for the user and include `[A2UI Pane](sidecar://a2ui-pane/a2ui-pane/)` so they can inspect the completed board.
