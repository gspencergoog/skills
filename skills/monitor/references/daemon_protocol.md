# Daemon Mode Protocol (`--daemon`)

This document defines the persistent daemon protocol used by `/monitor`.

## Overview

In daemon mode (`--daemon`), the monitor process does not terminate when actionable events occur. Instead, it:
1. Emits the event payload to `<state-dir>/last.json` and updates the A2UI board pane.
2. Posts an actionable batch message into the conversation via `agentapi send-message`.
3. Sets an internal acknowledgment gate (`AckGate`) and continues polling in the background.
4. Reads incremental instructions from `<state-dir>/control.jsonl` on each polling tick.
5. Emits a reminder if the batch remains unacknowledged beyond `--reminder` seconds.
6. Once acknowledged, unblocks the gate and sends the next pending batch.

## State Directory Files

All daemon files are located in the state directory (e.g. `<conversation-scratch-dir>/monitor/`):

| File | Writer | Purpose |
| :--- | :--- | :--- |
| `daemon.lock` | Daemon | Exclusive `fcntl.flock` lock file preventing duplicate daemons. |
| `daemon.pid` | Daemon | Contains the PID of the active daemon process. |
| `armed.json` | Daemon | Metadata declaring that the monitor is armed and expected to run. Contains `{"argv": [...], "cwd": "...", "skill": "...", "started_at": "...", "poll_s": ...}`. The PR monitor adds `"source"` (`"packrat"` or `"github"`, rewritten when it falls back) and, in `packrat` mode, `"packrat_db"`. |
| `heartbeat` | Daemon | Empty file whose `mtime` is touched every polling loop tick. |
| `control.jsonl` | Agent | Append-only instruction file consumed by `ControlReader`. |
| `last.json` | Daemon | Latest emitted payload, including `batch_id`, `notify_mode`, and `notifier`. |
| `events.jsonl` | Daemon | Continuous historical event log. |
| `board.a2ui` | Daemon | A2UI Express DSL board definition for the side pane. |
| `watchdog_nags.json` | Watchdog | Tracks hook nag history: `{"pids": {pid: count}, "recent_nags": [timestamps]}`. |

## Control Instructions (`control.jsonl`)

The agent interacts with a running daemon by appending one JSON object per line to `control.jsonl`. Each line must end with `\n`.

### 1. Acknowledging a Batch
```json
{"ack": 1}
```
Signals that the agent has received and begun handling batch `1`. Older or equal batch IDs clear the pending wait.

### 2. Suppressing Self-Pushed SHAs (PR Monitor)
```json
{"pushed_sha": {"owner/repo#123": "c0ffee1234567890"}}
```
Records a commit SHA pushed by the agent so the subsequent `head_push` event is suppressed.
In `packrat` mode the daemon also runs `packrat refresh` for that pull request, so the new head shows up without waiting for a signal.

### 3. Marking CI Reruns
```json
{"mark_rerun": ["owner/repo#123=test-check@c0ffee"]}
```
Records a flaky check rerun performed by the agent.

### 4. Stopping the Daemon
```json
{"stop": true}
```
Instructs the daemon to disarm (`armed.json` removed), emit a final payload with `exit_reason == "stopped"`, and cleanly exit code `0`.

## `packrat` Leases (PR Monitor)

When `/monitor` reads from `packrat serve` (`--source auto` with a fresh heartbeat, or `--source packrat`), it holds a lease on every watched pull request:

1. **Start:** `packrat watch add --consumer monitor --id <id> --freshness 120 --ttl 900 <PR>...`, where `<id>` is a hash of `--conversation-id` (or of the state directory). Pull requests that `packrat` hasn't refreshed in the last 15 minutes get a `packrat refresh` and up to 30 seconds to arrive; any still missing are fetched once with `gh`.
2. **Running:** `packrat watch renew` every 5 minutes. The daemon waits on `packrat changes --since <seq> --wait <debounce>` and re-reads only the changed pull requests from the views. A `{"reset": true}` answer re-reads every watched pull request.
3. **Exit:** after the daemon loop ends (`stop`, `--timeout`, terminal state, or a signal), `packrat watch remove` drops the leases. A crashed daemon's leases expire after the 900-second TTL. `--once` keeps its lease so the daemon that usually follows starts warm.
4. **Fallback:** if the heartbeat goes stale or a `packrat` command fails, the daemon switches to direct GitHub polling for the rest of the run, logs it once, and rewrites `armed.json` with `"source": "github"`.

## Exit Codes & Exit Reasons

| Code | Reason | Meaning | Action by Agent / Watchdog |
| :--- | :--- | :--- | :--- |
| `0` | `change` | (Under `--once` or non-daemon exit mode) Actionable events ready. | Remediate and relaunch. |
| `0` | `stopped` | Clean exit requested via `{"stop": true}`. `armed.json` removed. | None (monitor stopped). |
| `1` | `error` | Startup error or consecutive API errors exhausted backoff. | Inspect stderr and notify user. |
| `2` | `timeout` | `--timeout` elapsed with no events. | Ask user whether to keep watching. |
| `3` | `terminal` | All watched items reached terminal state (`--until`). | Announce completion (and run `convo --resume` if `convo` is on `PATH`). |
| `4` | `already_running` | Another process holds `daemon.lock`. | Do not start a duplicate daemon. |
| `130` / `143` | (none) | `SIGINT` / `SIGTERM` received. State and `armed.json` preserved. | Watchdog will nag to relaunch after restart. |

## Watchdog (`Stop` Hook) Interaction

The `monitor-watchdog` plugin inspects `armed.json` whenever the agent attempts to conclude a turn (`terminationReason == "NO_TOOL_CALL"`).

If `armed.json` is present but:
1. `os.kill(pid, 0)` raises `ProcessLookupError` (or `heartbeat` is older than `max(3 * poll_s, 600)` seconds), AND
2. `last.json` does not indicate `exit_reason == "terminal"` or `stopped`,

the hook blocks turn completion and injects a `decision: "continue"` directive instructing the agent to relaunch the daemon with its saved `argv`.

### Crash-Loop Protection
To prevent runaway relaunch loops:
- The watchdog allows at most **2 nags** for the same specific PID.
- The watchdog allows at most **4 total nags** across all PIDs within any rolling **15-minute window**.
- If a threshold is exceeded, the watchdog disarms the monitor (unlinking `armed.json`) and logs a suppression warning to stderr.
