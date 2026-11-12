# packrat reader contract

This is the stable interface that other tools (`/monitor`, the `inflight`
sidecar, the monitor watchdog) use to read live GitHub data from `packrat`.
Readers depend only on what this page describes. Tables, columns, and modules
that it doesn't mention can change without notice.

- Readers query the SQLite **views** listed below, never the tables behind them.
- Readers never write to `repos.sqlite`. They lease items and request refreshes
  through the `packrat` CLI (`packrat watch`, `packrat refresh`, `packrat
  changes`), which the service owns.

## Database location and read-only access

The database is `~/.local/share/packrat/repos.sqlite` unless `packrat --db`
points elsewhere. It runs in WAL mode.

Open it read-only:

- Python: `sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)`
- Node: `new DatabaseSync(dbPath, { readOnly: true })` from `node:sqlite`. The
  `readOnly` option needs Node 22.11.0 or later. `node:sqlite` still prints an
  `ExperimentalWarning` on Node 22.

WAL caveats for read-only opens:

- A `mode=ro` connection still needs the `-wal` and `-shm` files next to the
  database. If they don't exist and can't be created (for example a read-only
  data directory), the open fails with `SQLITE_CANTOPEN` ("unable to open
  database file"). Treat that, a missing file, or any `sqlite3.Error` while
  opening as "packrat unavailable" and fall back to your own GitHub source.
- Readers see a consistent snapshot per statement. Run related queries inside
  one read transaction (`BEGIN` ... `COMMIT`) when they must agree with each
  other.
- `packrat` recreates the views only when it is upgraded (a schema migration
  or a changed `views.sql`), never on an ordinary open. A long-lived reader
  that holds prepared statements may see `SQLITE_SCHEMA` once after an
  upgrade; re-prepare and retry.
- Every `packrat` CLI command exits `3` when SQLite reports the database busy
  or locked. Retry after a short pause; nothing was changed.

## Untrusted text

`title`, `body`, comment text, review text, and notification reasons in the
views are copied verbatim from GitHub and are untrusted user input. A reader
that passes any of them to a language model must wrap each value in
`<untrusted_repo_content source="<item_key>">...</untrusted_repo_content>`
(escaping any closing tag inside the value), as `packrat search` and
`packrat sql` do, and must treat the contents as data, never as instructions.

## Item keys

Every item is identified by `owner/name#number`, for example
`octo-org/widgets#123`, with the repository's canonical GitHub casing. `packrat`
matches repository names case-insensitively when you pass keys to the CLI and
returns the canonical form.

Keys for renamed or redirected repositories are rewritten to the canonical
repository name once `packrat` refreshes the item from GitHub. Readers that
pass an old repository name to `packrat watch` or `packrat refresh` should take
the canonical `item_key` from `v_pr_basic`, `v_pr_live`, or `packrat changes`.

## Repository modes

`v_pr_basic.repo_mode` and `v_pr_live.repo_mode` are one of:

| Mode | Meaning |
| :--- | :--- |
| `mirror` | Registered with `packrat repo add`. Full PR, issue, and discussion history is backfilled and kept current, so branch-name queries (stack discovery) see every PR. |
| `items` | Created automatically by the first lease, notification, or viewer search for a repository nobody registered. Only watched, notified, or viewer items are stored. Don't infer that a PR doesn't exist because it's missing. |

## Views

All timestamps are ISO-8601 UTC text (`2026-11-03T22:41:00Z`). Boolean columns
are integers (`0`, `1`) and may be `NULL` when unknown.

### `v_pr_basic`

One row per stored pull request in any mode, for history, `referencedOnly`
lookups, and stack discovery.

| Column | Type | Notes |
| :--- | :--- | :--- |
| `item_key` | text | `owner/name#number` |
| `repo_full_name` | text | `owner/name` |
| `repo_mode` | text | `mirror` or `items` |
| `number` | integer | |
| `title` | text | |
| `state` | text | `OPEN`, `CLOSED`, `MERGED` |
| `is_draft` | integer | |
| `url` | text | |
| `author` | text | login |
| `created_at`, `updated_at`, `merged_at`, `closed_at` | text | |
| `base_ref` | text | base branch name (always in the target repo) |
| `head_ref` | text | head branch name |
| `head_sha` | text | head commit |
| `head_repo_owner` | text | owner of the head repository; `NULL` until refetched |
| `is_cross_repository` | integer | `1` when the head branch lives in a fork; `NULL` until refetched |
| `body` | text | raw markdown description; `''` when unknown |

Stack discovery: a PR's parent is an open PR in the same repository whose
`head_ref` equals its `base_ref` and whose `is_cross_repository = 0`. Treat
`NULL` as same-repository, which matches `gh pr list --head`.

### `v_issue_basic`

One row per stored issue in any mode (viewer `issues` set members, notified
issues, and mirrored history).

| Column | Type | Notes |
| :--- | :--- | :--- |
| `item_key`, `repo_full_name`, `repo_mode`, `number` | | as in `v_pr_basic` |
| `title` | text | |
| `state` | text | `OPEN`, `CLOSED` |
| `url` | text | |
| `author` | text | login |
| `labels_json` | text | JSON array of label names |
| `assignees_json` | text | JSON array of assignee logins |
| `created_at`, `updated_at`, `closed_at` | text | |
| `body` | text | raw markdown description; `''` when unknown |

### `v_item_comments`

The 10 newest conversation (timeline) comments per PR and issue. Review-thread
comments are in `v_pr_threads` instead.

| Column | Type | Notes |
| :--- | :--- | :--- |
| `item_key` | text | |
| `kind` | text | `pr` or `issue` |
| `comment_id` | text | GraphQL node id |
| `author` | text | login |
| `author_type` | text | `User` or `Bot`; `NULL` for older rows stored only by the mirror |
| `created_at` | text | |

Live refreshes (leased PRs, viewer issues) store the latest 10 comments with
`author_type`. A new comment adds a change-feed row of kind `comments`.

### `v_pr_live`

One row per PR that the service has refreshed live (leased, viewer, or
notified PRs). PRs that were never refreshed live don't appear here, even if
they are in `v_pr_basic`.

| Column | Type | Notes |
| :--- | :--- | :--- |
| `item_key`, `repo_full_name`, `repo_mode`, `number` | | as in `v_pr_basic` |
| `node_id` | text | GraphQL node id |
| `title`, `url`, `state`, `is_draft`, `author` | | |
| `author_type` | text | `User` or `Bot` |
| `base_ref`, `head_ref`, `head_sha` | text | |
| `head_committed_date` | text | commit date of `head_sha` |
| `head_repo_owner`, `is_cross_repository` | | |
| `mergeable` | text | `MERGEABLE`, `CONFLICTING`, `UNKNOWN` |
| `merge_state_status` | text | GraphQL `mergeStateStatus` (`CLEAN`, `BLOCKED`, `BEHIND`, `DIRTY`, `UNSTABLE`, `HAS_HOOKS`, `UNKNOWN`, `DRAFT`) |
| `review_decision` | text | `APPROVED`, `CHANGES_REQUESTED`, `REVIEW_REQUIRED`, or `NULL` |
| `auto_merge_enabled_at` | text | `NULL` when auto-merge is off |
| `created_at`, `updated_at`, `merged_at`, `closed_at` | text | |
| `live_fetched_at` | text | when the service last refreshed this PR; use it for freshness |
| `rollup_state` | text | `statusCheckRollup.state` for `head_sha` (`SUCCESS`, `FAILURE`, `PENDING`, `ERROR`, `EXPECTED`); `NULL` when unknown for the current head |
| `rollup_total` | integer | number of contexts in the rollup |
| `rollup_fetched_at` | text | |

### `v_pr_checks`

Check runs and status contexts on each PR's current head commit only. After a
force-push the old head's rows disappear from this view.

| Column | Type | Notes |
| :--- | :--- | :--- |
| `item_key` | text | |
| `head_oid` | text | equals `v_pr_live.head_sha` |
| `run_key` | text | row identity within the head: `CheckRun:<databaseId>` for check runs, `StatusContext:<context>` for statuses. Several rows can share a `name` (re-runs, or one job name in two workflows); judge each row, so a failing duplicate is never hidden |
| `kind` | text | `CheckRun` or `StatusContext` |
| `name` | text | check name or status context |
| `status` | text | `QUEUED`, `IN_PROGRESS`, `COMPLETED`, ... (status contexts: `NULL`) |
| `conclusion` | text | `SUCCESS`, `FAILURE`, `NEUTRAL`, ... (status contexts: their state) |
| `details_url` | text | |
| `started_at`, `completed_at` | text | |
| `is_required` | integer | branch protection requires it; `NULL` when unknown |
| `workflow_run_id` | integer | GitHub Actions run id when known |

### `v_pr_review_requests`

Pending review requests: `item_key`, `reviewer` (login or `org/team-slug`),
`reviewer_kind` (`User`, `Team`, `Bot`, `Mannequin`), `requested_at` (latest
`ReviewRequestedEvent` timestamp for that reviewer, or `NULL` when unknown).

### `v_pr_latest_reviews`

GraphQL `latestOpinionatedReviews`: the latest approving or change-requesting
review per author. Columns: `item_key`, `author`, `author_type` (`User` or
`Bot`), `state`, `submitted_at`.

### `v_pr_latest_author_reviews`

The latest non-pending review per author on a pull request, including `COMMENTED`
reviews and the reviewed commit OID (`latest_reviews`, schema v8). Columns:
`item_key`, `repo_full_name`, `pr_number`, `author`, `author_type` (`User` or
`Bot`), `state` (`APPROVED`, `CHANGES_REQUESTED`, `COMMENTED`, `DISMISSED`),
`submitted_at`, `commit_oid`.

### `v_pr_threads`

One row per review thread: `item_key`, `thread_id`, `is_resolved`,
`is_outdated`, `last_author`, `last_author_type`, `last_at` (the thread's last
comment), and `participants_json`. `participants_json` holds a JSON array of
deduplicated comment logins from the opening comment and the latest 10 comments
(`'[]'` before refresh).

### `v_viewer_items`

The authenticated user's current sets: `set_name`, `item_key`,
`first_seen_at`, `last_seen_at`. Only current members appear.

| Set | Members |
| :--- | :--- |
| `authored` | PRs the user wrote, open or recently merged/closed, updated in the last 7 days |
| `review_requested` | Open PRs that request the user's review, any age |
| `reviewed` | Open PRs reviewed by the user (excluding self-authored PRs), any age |
| `issues` | Issues that involve the user, open or closed, updated in the last 7 days |

Only open PRs in these sets get the service's own lease
(`consumer='packrat-viewer'`). Read their current state from `v_pr_live`.

### `v_notifications`

GitHub notification threads: `thread_id`, `reason`, `subject_type`
(`PullRequest`, `Issue`, ...), `item_key` (`NULL` for subjects that aren't
issues or PRs), `updated_at`, `unread`, `seen_at` (when the service last saw
the thread).

### `v_service_health`

Always exactly one row.

| Column | Type | Notes |
| :--- | :--- | :--- |
| `pid` | integer | `NULL` if `packrat serve` never ran |
| `started_at`, `beat_at` | text | |
| `version` | text | service version |
| `beat_age_s` | integer | seconds since the last heartbeat; `NULL` if never |
| `graphql_remaining`, `graphql_reset_at` | integer | latest GraphQL budget seen in a response (`reset_at` is epoch seconds) |
| `core_remaining`, `core_reset_at` | integer | latest REST budget |
| `active_leases` | integer | unexpired leases across all consumers |
| `viewer_login` | text | the authenticated GitHub user; `NULL` until the first viewer refresh |
| `last_ok_at` | text | when the last tick finished without errors; `NULL` if none has |
| `consecutive_failed_ticks` | integer | ticks that ended with an error since the last clean one (`0` when healthy) |

**Stale or degraded service:** treat the service as down when the row's
`beat_age_s` is `NULL` or greater than `180`, or when
`consecutive_failed_ticks` is `3` or more, and fall back to your own GitHub
source. The `version` column names the running code; `packrat status` warns
when it differs from the installed checkout, and `packrat auto-sync on`
restarts the service onto the current code.

`packrat import` copies repositories at about `37.5 s/GiB` inside
per-repository `BEGIN IMMEDIATE` transactions. Importing a single repository
above roughly `4.8 GiB` holds the WAL write lock past `180s`, and
`packrat import` warns on `stderr` beforehand. During that copy,
`packrat serve` yields its ticks without incrementing
`consecutive_failed_ticks`, while `beat_age_s` may exceed `180`. Readers fall
back to GitHub until `COMMIT` releases the lock and the service resumes.

### `v_watches`

Every lease: `consumer`, `consumer_id`, `item_key`, `freshness_s`,
`lease_expires_at`, and `active` (`1` while `lease_expires_at` is in the
future). Use it to confirm your own leases; the service drops expired rows.

### `v_changes`

The change feed as a table: `seq`, `item_key`, `kind`, `fields` (JSON array
text), `at`. `packrat changes` (below) is the supported way to follow it; the
view exists for ad-hoc inspection and DuckDB joins.

### `v_api_calls`

The GitHub API call log: `at`, `worker`, `resource` (`graphql` or `core`),
`cost`, `remaining`, `reset_at`, `status`. Rows older than 7 days are pruned.

## Leases

A reader leases the items it shows with
`packrat watch add --consumer NAME --id ID [--freshness SECONDS] [--ttl SECONDS] ITEM...`
(defaults: freshness 120 s, TTL 900 s). Both values must be at least 1;
anything else exits `2`.

- `consumer` names the tool (`monitor`, `inflight`); `id` names the instance
  (for example a conversation hash), so two conversations don't remove each
  other's leases.
- `freshness` is the oldest data you accept. The service refreshes leased PRs
  when a signal arrives (notification, events, check-run ETag change) and,
  without any signal, at least every `max(freshness, 600)` seconds.
- Renew before the TTL lapses with `packrat watch renew` (every 5 minutes is
  enough for the default TTL). Expired leases are dropped automatically, so a
  reader that dies leaves nothing behind for longer than its TTL.
- Leasing a PR in a repository nobody registered first checks that the
  repository exists on GitHub. A `404` exits `2` and registers nothing; when
  GitHub can't be reached, the lease is created anyway with a warning on
  stderr. The repository is registered in `items` mode.
- Your own leases are visible in `v_watches` (`consumer`, `consumer_id`,
  `item_key`, `freshness_s`, `lease_expires_at`, `active`).
- `packrat refresh ITEM...` asks for an immediate refresh, for example after a
  push.

## Change feed

`packrat changes --since SEQ [--wait SECONDS] [--limit N] --json` returns
`{"seq": <last>, "changes": [{"seq", "item_key", "kind", "fields", "at"}, ...]}`,
where `<last>` is the `seq` of the last row returned. `--since` must be at
least 0 and `--limit` (default 500) at least 1; anything else exits `2`.

To start following the feed:

1. Run `packrat changes --since 9007199254740991 --json`. A `--since` above
   every assigned `seq` always returns `{"reset": true, "seq": <max>}`.
2. Take a full snapshot from the views.
3. Continue with `--since <max>`. Rows written while you took the snapshot
   come back in that call, so nothing is lost, though you may see a change the
   snapshot already showed.

Don't start with `--since 0`. It replays up to a week of old rows, and once
any row has been pruned it returns `reset` instead.

- `seq` is monotonic and never reused. Store the returned `seq` and pass it as
  the next `--since`. When no row arrives before `--wait` expires, the command
  returns `{"seq": <your since>, "changes": []}`.
- A row is written only when stored data actually changed, so an idle refresh
  adds nothing.
- Rows are kept for 7 days. When `--since` is older than the oldest kept row,
  or larger than any `seq` ever assigned, the command returns
  `{"reset": true, "seq": <max>}` instead. Take a full snapshot from the views,
  then continue from that `seq`.
- Filter rows to the item keys you lease; the feed covers every item.

`kind` says what changed, and `fields` lists the names below:

| `kind` | `fields` |
| :--- | :--- |
| `pr` | the changed `v_pr_live` column names (including `rollup_state` and `rollup_total`), plus `body` when the description changed |
| `checks` | `["check_runs"]` |
| `review_requests` | `["review_requests"]` |
| `reviews` | `["latest_opinionated_reviews"]` or `["latest_reviews"]` |
| `threads` | `["thread_activity"]` |
| `comments` | `["comments"]` |
| `notification` | `["reason"]` |
| `viewer` | `[<set_name>]`: the item entered or left that `v_viewer_items` set |

On `promoted` or `replaced` repositories, `packrat import` queues
`refresh_requests` for leased items and viewer-set PRs. For each matching pull
request, it also appends a `pr` row with `fields` set to
`["pr", "checks", "reviews", "threads"]`. Readers such as `/monitor` and
`inflight` then reload the imported state right away.

Timeline events (review requests, draft changes, force pushes, merges, and
similar) are stored in `timeline_events` but don't write change-feed rows yet,
so don't look for a `timeline` kind.

## Freshness expectations

| Data | Typical age |
| :--- | :--- |
| Leased PR fields, reviews, threads | under `freshness` after a signal; at most `max(freshness, 600)` s without one |
| Leased PR checks | under `freshness` (check-run and status ETags are polled per lease) |
| Notifications | 60 s, or GitHub's `X-Poll-Interval` |
| Viewer sets | 15 min, sooner after a notification |
| Mirror repositories (`v_pr_basic`) | about 2 min after an `/events` change |

When the GitHub budget runs low, the service defers mirror and backfill work
below 20% of the limit, and viewer searches below 5%. Leased (hot) refreshes
are never deferred by the budget; they stop only when GitHub answers with a
rate-limit error.
