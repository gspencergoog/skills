# Reviewing a batch of pull requests with a swarm

Use this when the user asks for reviews of several PRs at once. Each PR still
goes through the stages in `SKILL.md`. This file covers only what changes when
several reviews run side by side.

## Before starting

- Check each PR's live state with `gh pr view <N> --json state,mergedAt` and
  drop any that merged or closed. Repeat this before opening each dashboard.
- A pasted `/issues/N` URL can point at a PR. `gh pr view <N>` settles it.
- PRs from outside contributors may have workflow runs waiting in
  `action_required`. See the `gh-cli` skill's `references/ci-triage.md` for the
  approval call.
- Stage 1 clones over SSH. In an agent shell with no SSH agent, set
  `GIT_SSH_COMMAND` to name the key, or the clone fails with
  `Permission denied (publickey)`.
- Four Deep reviews in parallel take about 20 minutes and a large share of one
  model's daily quota. Size the batch to the quota that is left.

## Worker pool

Cap concurrency with a fixed pool of workers and feed them from a queue. Do not
add one worker per PR.

- Each worker runs `code-review` in its own session, with no nested subagents
  and no swarm helpers.
- Each PR gets its own directory, `<scratch>/pr-review-<N>/`, for every file
  the stages write.
- Keep Stage 1 blind: the brief names only the PR number or URL. Leave out the
  title, author, and branch.
- When a worker finishes a PR, it cleans up that PR's workspace and waits for
  the next one from the queue.
- If `code-review` does add helpers, prefix their names with the PR number
  (`rev-<N>-a`) and address their reports to the PR's worker. The default
  helper names and the default report address collide when several reviews
  share one roster.
- When quota runs low while workers are mid-turn, set a short timer next to the
  wait so a worker that parks on quota is switched to another model promptly.

## Two ways to finish

Pick one per batch, from what the user asked for.

### Dashboards one at a time (default)

Workers stop before Stage 4. Each one writes its artifacts under the
coordinator's scratch directory, runs `launch_dashboard.py --dump-json` to
check line anchors, and leaves `workspace_dir.txt` and the checkout in place.
The coordinator then takes the PRs in order: open the dashboard, wait for the
user's submission, apply the review, clean up, and move to the next PR.

### Direct submission

Only when the user asks to skip the dashboard. Each worker validates with
`launch_dashboard.py --dump-json`, writes `review_decisions.json`, and submits
with `apply_review.py`. Derive the verdict from `review_summary.verdict`:

| Findings | Verdict |
| :------- | :------ |
| None, and every prior thread is verified resolved | `APPROVE` |
| Medium, low, or question findings only | `COMMENT` |
| Any critical or high finding | `REQUEST_CHANGES` |

Read the `Verdict:` line that `apply_review.py` prints. The verdict in the
decisions file is the one posted, even when the written summary says
otherwise.

## Re-reviews

A worker that reads the prior threads first checks only those threads and
skips most of the code. For round 2 and later, or for any PR that already has
review threads:

1. Copy the previous round's `pr_coords.json`, `blind_description.md`,
   `review_results.json`, and `review_decisions.json` into the PR's directory
   without reading them, so the engine detects the round.
2. Run the full `code-review` pass on the whole PR diff before reading
   `pr_context.json`, the previous decisions, or the incremental diff.
3. Only then read the prior threads and merge them with the new findings.
