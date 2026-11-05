# `monitor_prs.py` Output Schema and Protocol

## 1. Command Line Interface

```text
monitor_prs.py [PR ...] --state FILE [options]
```

| Flag | Default | Description |
| :--- | :--- | :--- |
| `PR` | required (except `--once` with existing state) | Pull request targets: `owner/repo#N`, `https://github.com/owner/repo/pull/N`, or `N` with `--repo` |
| `--repo OWNER/REPO` | none | Default repository for bare pull request numbers |
| `--stack` | `false` | Expand each target pull request to include its parent chain and direct child pull requests (up to 20 total) |
| `--state FILE` | required | Path to the JSON state snapshot file (created automatically on first run) |
| `--events LIST` | all kinds | Comma-separated subset of `EVENT_KINDS` that should wake the caller |
| `--actionable-only` | `false` | Update `state.json`, `board.a2ui`, and the A2UI Pane board file in place on passive events (a dismissed board is not recreated); exit `0` only on actionable events |
| `--until MODE` | `closed` | Terminal condition for exit code `3`: `merged`, `closed`, or `all-green` |
| `--timeout SECONDS` | none | Exit with code `2` when no matching event occurs within this duration |
| `--debounce SECONDS` | `20` | Fixed window after the first matching wake event to collect follow-on transitions (`0` disables) |
| `--poll SECONDS` | `120` | Targeted GraphQL refresh interval in seconds. Refetches only flagged pull requests (notifications, `PENDING` CI, `UNKNOWN` mergeability, pending debounce, `control.jsonl` `pushed_sha`) and makes no call when none are flagged. With `--no-notifications`, every step is a full refresh |
| `--sweep SECONDS` | `600` | Full GraphQL refresh interval for every watched pull request |
| `--notify-poll SECONDS` | `60` | Conditional `GET /notifications` poll interval in seconds |
| `--once` | `false` | Fetch once, diff against `--state`, update board, emit JSON, save state, and exit immediately |
| `--no-a2ui` | `false` | Disable writing `board.a2ui` and the A2UI Pane board file |
| `--a2ui-file FILE` | `<state-dir>/board.a2ui` | Path to write the `stack_board` A2UI Express DSL file |
| `--conversation-id ID` | none | Target Jetski conversation ID for direct A2UI Pane store updates (`conversations/<ID>/monitor.jsonl`) |
| `--a2ui-store-dir DIR` | First existing of `$A2UI_PANE_DATA`, `$PLUGIN_DATA`, or `$ANTIGRAVITY_APP_DATA_DIR/plugin_data/a2ui-pane` (falling back to the host default plugin data dir) | Override the `a2ui-pane` plugin data directory |
| `--log FILE` | none | Append each emitted JSON payload as one line to this JSONL file |
| `--output FILE` | none | Write the emitted JSON payload to this file in addition to `stdout` |
| `--pushed-sha PR=SHA` | repeatable | Record a commit SHA pushed by the agent so `head_push` is suppressed for that SHA |
| `--mark-rerun PR=CHECK@SHA` | repeatable | Record a one-time flaky check rerun so a second failure routes to `diagnose_ci` |
| `--no-notifications` | `false` | Skip `GET /notifications` and use GraphQL polling only (every `--poll` step is a full refresh) |
| `--daemon` | `false` | Run continuously as a background daemon, notifying the conversation via `agentapi` and waiting for acks in `control.jsonl` |
| `--control FILE` | `<state-dir>/control.jsonl` | Path to the control commands file for daemon mode |
| `--reminder SECONDS` | `1800` | Re-notify conversation if an active batch remains unacknowledged (`0` disables) |
| `--notify-scope SCOPE` | `mine` | Which pull request events trigger conversation notifications: `mine` (viewer PRs + terminal) or `all` |
| `--source MODE` | `auto` | Data source: `packrat` (read a running `packrat serve`), `github` (poll GitHub directly), or `auto` (`packrat` when its heartbeat is fresh, else `github`) |
| `--packrat-db FILE` | `~/.local/share/packrat/repos.sqlite` | `packrat` database to read in `packrat` mode |

Direct GitHub polling (`--source github` or fallback) uses `gh` with a standard `repo`-scoped token; no extra `notifications` scope is needed.

## 2. Exit Codes and Precedence

When multiple conditions hold on the same tick, precedence is **terminal (`3`) > change (`0`) > timeout (`2`)**. A timeout that expires inside an active debounce window exits `0` with the accumulated events rather than `2`.

| Exit code | `exit_reason` | JSON on `stdout` | Meaning |
| :--- | :--- | :--- | :--- |
| `0` | `change` (or `snapshot` with `--once`, or `stopped` with `--daemon`) | Yes | Matching wake events were collected, `--once` completed, or daemon received `{"stop": true}` |
| `1` | none | No (diagnostics on `stderr`) | Invalid arguments, corrupt state file, startup error, or 5 consecutive GitHub failures |
| `2` | `timeout` | Yes (`events: []`) | `--timeout` elapsed with no matching wake events |
| `3` | `terminal` | Yes | Every watched pull request satisfied `--until` (`merged`, `closed`, or `all-green`) |
| `4` | `already_running` | Yes (when `--output` or `--daemon`) | Another daemon process holds the liveness lock for this state directory |
| `130` | interrupted | No (state saved) | Interrupted by `SIGINT` |
| `143` | interrupted | No (state saved) | Terminated by `SIGTERM` |

## 3. Output JSON Payload

To keep agent context usage minimal, `a2ui` in `stdout` and `--output` carries only the file path (`dsl_file`, which assigns `$/title`, `$/subtitle`, `$/status`, `$/statusTone`, `$/statusPulse`, `$/steps`, `$/items`, and `$/ackLabel` for the `stack_board` template) and the result of the direct `a2ui-pane` store update (`store_updated` and `store_status`), rather than embedding the full A2UI Express DSL string in `stdout`.

`store_status` is one of `written` (the board file `conversations/<conversationId>/monitor.jsonl` was replaced), `unchanged` (same components and `items`, so nothing was written), `dismissed` (the board file is missing and this tick may not recreate it: no actionable event, no `--once`, and not the process's first sync), or `unavailable` (no store directory, invalid ids, lock timeout, or a legacy flat store still waiting for migration). `store_updated` is `true` only for `written`. Fall back to `render_a2ui` only on `unavailable`.

Example:

```json
{
  "schema": 1,
  "exit_reason": "change",
  "exit_code": 0,
  "observed_at": "2026-10-27T21:15:00Z",
  "summary": "widget#3339: CI failed (test-web); widget#3339: changes requested by bob",
  "prs": {
    "acme/widget#3339": {
      "key": "acme/widget#3339",
      "number": 3339,
      "repo": "acme/widget",
      "title": "feat: add decision planner format",
      "url": "https://github.com/acme/widget/pull/3339",
      "author": "alice",
      "mine": true,
      "state": "OPEN",
      "draft": false,
      "head_sha": "aaa111122223333444455556666777788889999",
      "head_ref": "feat/decision-planner",
      "base_ref": "main",
      "last_committer": "alice",
      "rollup_state": "FAILURE",
      "mergeable": "MERGEABLE",
      "merge_state": "UNSTABLE",
      "review_decision": "CHANGES_REQUESTED",
      "requested_reviewers": [],
      "assignees": ["alice"],
      "approvals": ["carol"],
      "changes_requested": ["bob"],
      "checks": [
        {
          "name": "test-web",
          "status": "COMPLETED",
          "conclusion": "FAILURE",
          "required": true,
          "url": "https://github.com/acme/widget/actions/runs/500/job/601",
          "run_id": 500,
          "started_at": "2026-10-27T21:00:10Z",
          "completed_at": "2026-10-27T21:07:02Z"
        }
      ],
      "threads": [
        {
          "id": "PRRT_human_2",
          "resolved": false,
          "last_author": "bob",
          "last_author_is_bot": false,
          "last_comment_at": "2026-10-27T21:07:30Z"
        }
      ],
      "auto_merge": false,
      "stack_parent": null,
      "stack_parent_state": null,
      "truncated": [],
      "updated_at": "2026-10-27T21:08:00Z",
      "eta_s": null
    }
  },
  "events": [
    {
      "pr": "acme/widget#3339",
      "kind": "ci_check",
      "from": "IN_PROGRESS",
      "to": "FAILURE",
      "actionable": true,
      "suggested_action": "diagnose_ci",
      "summary": "CI failed (test-web)",
      "detail": {
        "check": "test-web",
        "required": true,
        "run_id": 500,
        "url": "https://github.com/acme/widget/actions/runs/500/job/601",
        "rerun_attempted": false
      }
    }
  ],
  "eta": {
    "acme/widget#3339": null
  },
  "source": "github",
  "a2ui": {
    "template": "stack_board",
    "surfaceId": "monitor",
    "dsl_file": "/path/to/scratch/monitor/board.a2ui",
    "store_updated": true,
    "store_status": "written"
  }
}
```

`source` is `packrat` while the data comes from `packrat serve`, and `github` in direct mode or after a fallback (see `--source`).

## 4. Event Kinds (`EVENT_KINDS`)

| Kind | Trigger condition | `actionable` | `suggested_action` |
| :--- | :--- | :--- | :--- |
| `review_decision` | `reviewDecision` changes to a non-null value | `true` on `CHANGES_REQUESTED` or `APPROVED` | `pr_feedback_handler` (`CHANGES_REQUESTED`), `celebrate` (`APPROVED` + CI green), else `null` |
| `human_threads` | Unresolved non-viewer review thread (human or inline review bot such as `gemini-code-assist`) is new or receives a newer comment | `true` | `pr_feedback_handler` |
| `review_requests` | `requested_reviewers` or `assignees` list changes | `false` | `null` |
| `ci_check` | Non-cancelled check reaches `COMPLETED` (failure, or recovery from failure on the same head), or check summary reaches `SUCCESS` (`to: "ALL_GREEN"`) | `true` when any check fails (`FAILURE`, `ERROR`, `TIMED_OUT`, `ACTION_REQUIRED`, `STARTUP_FAILURE`), or when `ALL_GREEN` fires on an `APPROVED` PR | `rerun_ci`, `diagnose_ci`, `celebrate` (`ALL_GREEN` + `APPROVED`), or `null` |
| `conflicts` | `mergeable` becomes `CONFLICTING` | `true` | `resolve_conflicts` |
| `merge_state` | `mergeStateStatus` changes (ignoring transitions to `UNKNOWN`, and from `UNKNOWN` unless `DIRTY`, `CONFLICTING`, or `BEHIND`) | `true` on `BEHIND` or `DIRTY` | `update_branch` (`BEHIND`), `resolve_conflicts` (`DIRTY`), else `null` |
| `head_push` | `head_sha` changes and matches neither `--pushed-sha` nor `last_committer == viewer` | `false` | `null` |
| `base_change` | `base_ref` changes | `false` | `null` |
| `draft` | `draft` boolean toggles | `false` | `null` |
| `terminal` | `state` changes (`OPEN` to `MERGED` or `CLOSED`) | `true` on `MERGED` or `CLOSED` | `celebrate` (`MERGED`), else `null` |
| `stack_parent` | Watched parent PR's `state` becomes `MERGED` or `CLOSED` | `true` on `MERGED` | `rebase_onto_base` (`MERGED`), else `null` |
| `approval_dismissed` | Previous approver disappears from `approvals` without entering `changes_requested` when `head_sha` changes | `false` | `null` |
| `auto_merge` | `auto_merge` boolean toggles | `false` | `null` |

### Monitored Checks and Conclusions

GitHub repositories frequently run test and lint workflows (`python-ci`, `python-performance`, changelog or PR health checks) that are not listed in branch-protection rulesets (`isRequired: false`), yet their failure sets `statusCheckRollup.state` to `FAILURE`. `PrSnapshot.effective_required_checks()` therefore includes all checks on the pull request so `required_summary()`, `ALL_GREEN`, `_ci_badge()`, and `_detail_bullets()` always agree with GitHub's rollup state. Cancelled check runs (`conclusion: "CANCELLED"`) are treated as neutral rather than failed so superseded runs do not emit false failure alerts. External status checks that do not run on GitHub Actions set `run_id` to `null`.

## 5. State File Layout (`--state`)

The state file is written atomically (`tmp` + `os.replace`) and holds:

- `schema`: integer schema version (`1`).
- `snapshots`: map of `owner/repo#N` to the last normalized `PrSnapshot`.
- `notifications_last_modified`: `Last-Modified` cursor string for conditional `GET /notifications` requests.
- `check_durations`: map of `owner/repo:check_name` to the last 5 completed run durations in seconds, used to compute median `eta_s`.
- `pushed_shas`: map of `owner/repo#N` to the last commit SHA pushed by the agent.
- `reruns`: map of `owner/repo#N` to `["<check>@<head_sha>", ...]` tokens already rerun.
- `reported_threads`: map of `owner/repo#N` to `{thread_id: last_comment_at}` watermarks recorded after each successful `emit`. Entries for merged or closed pull requests are pruned automatically.
