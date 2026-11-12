# Triage of GitHub Actions results that look wrong

Use this when CI on a pull request stalls, fails without running anything, or
shows fewer checks than expected. Rule out a platform problem before debugging
the workflow itself.

## 1. Check the platform status first

```bash
# Components that are not operational, and open incidents
curl -s https://www.githubstatus.com/api/v2/summary.json |
  jq '{status, components: [.components[] | select(.status != "operational")], incidents}'

# Update timeline of unresolved incidents
curl -s https://www.githubstatus.com/api/v2/incidents/unresolved.json | jq '.incidents[] | {name, incident_updates: [.incident_updates[] | {created_at, body}]}'
```

During an incident with Actions or the Checks API, runs whose jobs all passed
can still complete as `failure`, reruns can create attempts with no jobs, and
`gh pr checks` can show only part of the check list. Wait for the incident to
clear before changing anything.

## 2. Symptoms and remedies

| Symptom | How to confirm | Cause | Remedy |
| :------ | :------------- | :---- | :----- |
| Nothing starts for a long time | `gh api "repos/<owner>/<repo>/actions/runs?status=queued" --jq .total_count` is above zero while the same query with `status=in_progress` returns 0, in more than one repository | Runner assignment incident. A plain backlog shows jobs running and the queue draining in order. | Wait. Queued runs start without help once the incident clears. |
| Job `cancelled` with no steps, run reported as `failure` | `gh run view <run> --json jobs --jq '.jobs[] | {name, conclusion, steps: (.steps | length)}'` shows `cancelled` and 0 steps, and the job has an empty `runner_name` | The job never got a runner and GitHub cancelled it (after about 15 to 55 minutes). | `gh run rerun <run> --failed` after the incident clears. |
| Same, but GitHub status is green and the repository is a fork | The fork has other queued runs, for example from a sync of its `main` branch | Runner starvation: the fork's own runs compete for its concurrent runner slots, which are scarce for macOS. | Cancel the fork's queued runs that do not matter, then `gh run rerun <run> --failed`. Logs of these jobs are not kept. |
| `actions/checkout` step `cancelled` after a few seconds | Job log | Incident side effect on a real runner. | `gh run rerun <run> --failed`. |
| A push created no `pull_request` runs at all | `gh api "repos/<owner>/<repo>/actions/runs?head_sha=<sha>" --jq '.workflow_runs[].name'` lists only `pull_request_target` workflows, and `gh pr checks` shows a handful of checks | Either the PR has conflicts, or the push event was dropped. | Run `gh pr view <pr> --json mergeable,mergeStateStatus`. `CONFLICTING` or `DIRTY` means GitHub cannot build the merge ref, so no `pull_request` run starts until the branch merges cleanly. Otherwise push an empty commit or close and reopen the PR. |
| A rerun finishes as `failure` within seconds | `gh run list --branch <branch> --workflow <name> --json attempt,conclusion,headSha` shows a new attempt, and `gh run view <run> --attempt <n> --json jobs` shows no jobs | Zero-job rerun attempt, seen during Checks API incidents. | Push a new SHA to get fresh runs: `git commit --amend --no-edit --date=now` and `git push --force-with-lease`. |
| The PR looks green but a workflow never passed | Compare `gh pr checks <pr>` with the workflows listed by `gh run list --branch <branch>` | A failed zero-job attempt removes that workflow's checks from `gh pr checks`. | Same as the previous row. Do not treat the PR as green. |
| `gh run rerun <run> --failed` returns `HTTP 404 .../actions/workflows/<id>` | The run belongs to a workflow outside the repository, such as an organization-required scan | `gh` looks up the workflow file, which the repository does not have. | `gh api -X POST repos/<owner>/<repo>/actions/runs/<run>/rerun-failed-jobs`. |
| A CLA check fails with a commit-count limit | The check's message, and `gh pr view <pr> --json commits --jq '.commits | length'` | Some CLA bots refuse PRs above a commit limit (250 for `cla/google`). Upper PRs of a stack reach it after lower PRs are squash-merged and the base branch is merged back in. | Collapse the branch history onto the base while keeping the tree: `git commit-tree HEAD^{tree} -p <base> -m "<message>"`, point the branch at the new commit, and force-push with lease. |
| Runs sit in `action_required` | `gh run list --branch <branch> --json databaseId,conclusion` | Workflows from a first-time or outside contributor need a maintainer's approval. | `gh api -X POST repos/<owner>/<repo>/actions/runs/<run>/approve`. Runs that are not from a fork return HTTP 403; skip those. |

## 3. After the remedy

Watch the checks with one blocking command instead of polling:

```bash
gh pr checks <pr> --watch --fail-fast
```
