# GitHub CLI Actions Reference (`gh run`, `gh workflow`)

Command reference and troubleshooting guide for GitHub Actions workflows and runs in GitHub CLI (`gh run`, `gh workflow`).

______________________________________________________________________

## Commands

### 1. Workflow Management (`gh workflow`)

#### `gh workflow list`

List all workflows in the repository.

```bash
# List active workflows
gh workflow list

# Include disabled workflows in the list
gh workflow list --all
```

Flags:

- `-a, --all`: Include disabled workflows.
- `--json <fields>`: Output JSON with specified fields (`id`, `name`, `path`, `state`).
- `-q, --jq <expression>`: Filter output with jq.

#### `gh workflow view`

View workflow file details and configuration.

```bash
# View summary and recent runs for a workflow
gh workflow view build.yml

# View the raw YAML configuration file
gh workflow view build.yml --yaml

# Open workflow in the browser
gh workflow view build.yml --web
```

Flags:

- `-y, --yaml`: View workflow YAML file directly.
- `-w, --web`: Open workflow in browser.

#### `gh workflow run`

Manually trigger a workflow run via `workflow_dispatch`. The target workflow must define an `on.workflow_dispatch` trigger.

```bash
# Trigger workflow with string input parameters
gh workflow run build.yml -f environment=staging -f deploy=true

# Trigger workflow on a specific branch or tag
gh workflow run build.yml --ref feature-branch -f debug=true

# Pass complex JSON input parameters via stdin
echo '{"environment":"staging", "retries": 3}' | gh workflow run build.yml --json
```

Flags:

- `-r, --ref <string>`: Branch or tag containing the version of the workflow to run.
- `-f, --raw-field <key=value>`: Add string parameter in key=value format.
- `-F, --field <key=value>`: Add string parameter, respecting `@filename` syntax to read from files.
- `--json`: Read workflow inputs as JSON via standard input.

#### `gh workflow enable` & `gh workflow disable`

```bash
# Enable a disabled workflow
gh workflow enable build.yml

# Disable an active workflow
gh workflow disable build.yml
```

______________________________________________________________________

### 2. Workflow Run Management (`gh run`)

#### `gh run list`

List recent workflow runs with filters.

```bash
# List workflow runs for a specific workflow file
gh run list --workflow build.yml

# Filter runs by branch, status, and triggering event
gh run list --branch main --status failure --event push

# Filter by triggering user and include disabled workflows
gh run list --user octocat --all

# Output structured JSON with custom fields
gh run list --workflow test.yml --json databaseId,conclusion,status,headSha,event --jq '.[] | select(.conclusion == "failure")'
```

Flags:

- `-w, --workflow <name|file>`: Filter by workflow name or filename.
- `-b, --branch <name>`: Filter runs by branch.
- `-c, --commit <SHA>`: Filter runs by commit SHA.
- `-s, --status <string>`: Filter by status: `{queued|completed|in_progress|requested|waiting|pending|action_required|cancelled|failure|neutral|skipped|stale|startup_failure|success|timed_out}`.
- `-e, --event <name>`: Filter runs by event trigger (`push`, `pull_request`, `workflow_dispatch`, etc.).
- `-u, --user <login>`: Filter runs by triggering user.
- `-a, --all`: Include disabled workflow runs.
- `-L, --limit <int>`: Maximum number of runs to fetch (default: 20).
- `--json <fields>`: Output JSON with specified fields (`databaseId`, `name`, `headBranch`, `headSha`, `status`, `conclusion`, `url`, `startedAt`, `createdAt`).

#### `gh run view`

View status, summary, and log output for a workflow run or job.

```bash
# View summary of a specific workflow run
gh run view 12345

# View full logs for the entire run
gh run view 12345 --log

# View only the logs of failed steps (ideal for troubleshooting)
gh run view 12345 --log-failed

# View logs for a specific job ID within the run
gh run view 12345 --log --job 456789

# View specific attempt of a workflow run
gh run view 12345 --attempt 2

# Check exit status in script (exits non-zero if run failed)
gh run view 12345 --exit-status && echo "Run succeeded"

# Open run in the web browser
gh run view 12345 --web
```

Flags:

- `--log`: View full logs.
- `--log-failed`: View logs for failed steps only.
- `-j, --job <id>`: View specific job ID.
- `-a, --attempt <uint>`: View specific attempt number.
- `-v, --verbose`: Show job steps.
- `--exit-status`: Exit with non-zero exit code if run failed.
- `-w, --web`: Open in web browser.

#### `gh run watch`

Watch an active workflow run in real time until it completes.

```bash
# Watch a workflow run until completion
gh run watch 12345

# Watch run in compact mode (showing only failed / in-progress steps)
gh run watch 12345 --compact

# Watch run and fail script if run fails
gh run watch 12345 --exit-status
```

Flags:

- `--compact`: Show only relevant and failed steps.
- `--exit-status`: Exit with non-zero exit code if run fails.
- `-i, --interval <int>`: Refresh interval in seconds (default: 3).

#### `gh run rerun`

Rerun a completed workflow run or specific job.

```bash
# Rerun only failed jobs in a workflow run
gh run rerun 12345 --failed

# Rerun all jobs in the entire workflow run
gh run rerun 12345

# Rerun a specific job ID (and its dependent downstream jobs)
gh run rerun 12345 --job 456789

# Rerun with debug logging enabled
gh run rerun 12345 --failed --debug
```

Flags:

- `--failed`: Rerun only failed jobs including dependencies.
- `-j, --job <id>`: Rerun specific job ID.
- `-d, --debug`: Enable GitHub Actions step debug logging.

#### `gh run download`

Download artifacts produced by a workflow run.

```bash
# Download all artifacts from a run into the current directory
gh run download 12345

# Download artifacts into a target directory
gh run download 12345 --dir ./build-artifacts

# Download specific artifact by name
gh run download 12345 -n release-binaries

# Download artifacts matching a glob pattern
gh run download 12345 -p "test-results-*"
```

Flags:

- `-D, --dir <path>`: Directory to extract artifacts into (default: `.`).
- `-n, --name <names>`: Filter by artifact names.
- `-p, --pattern <glob>`: Filter by glob pattern.

#### `gh run cancel` & `gh run delete`

```bash
# Cancel an active workflow run
gh run cancel 12345

# Delete a completed workflow run and its logs
gh run delete 12345
```

______________________________________________________________________

## Troubleshooting & Common Pitfalls

1. **Job ID vs URL Job Number**:
   - The number in a browser URL (`.../actions/runs/<run-id>/jobs/<number>`) is often **not** the internal job `databaseId` required by `gh run rerun --job <id>` or `gh run view --job <id>`. Using the browser URL number results in `404 NOT FOUND`.
   - To find the true job `databaseId`:
     ```bash
     gh run view <run-id> --json jobs --jq '.jobs[] | {name, databaseId}'
     ```
2. **Script Exit Codes with `--exit-status`**:
   - Both `gh run view` and `gh run watch` support `--exit-status`, which causes the CLI to exit with code `1` if the workflow run concluded with failure, cancellation, or startup failure. This enables clean bash pipeline gating:
     ```bash
     gh run watch "$RUN_ID" --exit-status || { echo "CI failed!"; exit 1; }
     ```
3. **Retrieving Run ID After Triggering**:
   - `gh workflow run` triggers asynchronously and does not print the run ID by default. To capture the run ID of a triggered workflow:
     ```bash
     gh workflow run build.yml --ref main
     sleep 3
     RUN_ID=$(gh run list --workflow=build.yml --limit=1 --json databaseId --jq '.[0].databaseId')
     gh run watch "$RUN_ID"
     ```
