# Findings and Decisions Schema

`launch_dashboard.py` reads `review_findings.json` and writes `review_decisions.json`. `apply_review.py` reads `review_decisions.json`. This file defines both.

## `review_findings.json`

Write `raw_findings.json` in this shape. `diff_parser.py --findings` keeps the shape and adds `is_diff_hunk` and `original_code` to each finding.

```json
{
  "pr_metadata": {
    "owner": "octocat",
    "repo": "hello-world",
    "pull_number": 42,
    "title": "fix: handle empty input",
    "author": "octocat",
    "base_ref": "main",
    "head_ref": "fix/empty-input",
    "head_sha": "010d93ecc0c8ac1fc6618cdcbb18b5d55b16655c",
    "pr_url": "https://github.com/octocat/hello-world/pull/42",
    "state": "OPEN"
  },
  "review_summary": {
    "overview": "Adds empty-input handling to the parser. One async call is not awaited, so errors are lost.",
    "verdict": "REQUEST_CHANGES"
  },
  "pr_description": {
    "markdown": "## Intent Alignment: Partial Divergence\n\n## Summary\nAdds empty-input handling to `src/parser.ts`...",
    "alignment": "PARTIAL"
  },
  "findings": [
    {
      "id": "f1",
      "severity": "high",
      "category": "correctness",
      "file_path": "src/parser.ts",
      "line_start": 120,
      "line_end": 122,
      "side": "RIGHT",
      "description": "The promise from resolveDynamicValue() is never awaited.",
      "draft_comment": "`resolveDynamicValue(fc)` returns a promise that is never awaited, so async functions report success before they finish.\n\n```suggestion\n    await resolveDynamicValue(fc);\n```",
      "is_diff_hunk": true,
      "original_code": "    resolveDynamicValue(fc);"
    }
  ]
}
```

| Field | Written by | Meaning |
| :--- | :--- | :--- |
| `pr_metadata` | agent, from `pr_meta.json` | Copy `review_engine.py --output-meta` output as is. The dashboard needs `owner`, `repo`, `pull_number`, `head_sha`, and `title` (plus optional `latest_head_sha` when new commits were pushed after Stage 1). Passing `--meta-file` to the launcher fills any missing fields. |
| `review_summary.overview` | agent | 1-2 paragraphs: what the PR does, the assessment, and the verdict rationale. Don't list the findings. |
| `review_summary.verdict` | agent | `COMMENT`, `REQUEST_CHANGES`, or `APPROVE`. The user can change it. |
| `pr_description` | `--description-file` or agent | Reviewer-only PR description and intent alignment panel shown at the top of the dashboard (`{"markdown": "...", "alignment": "MATCHES\|PARTIAL\|DIVERGES\|UNSTATED"}`). Passing `--description-file <scratch>/pr_description.md` to `launch_dashboard.py` populates this automatically. `apply_review.py` never posts this field; its text is included in a review only if the user explicitly clicks **+ Insert into Review Summary**. |
| `findings[].severity` | agent | `critical`, `high`, `medium`, or `low`. |
| `findings[].line_start`, `line_end` | agent | Lines in the new file (`side: RIGHT`) or the old file (`side: LEFT`). |
| `findings[].description` | agent | One sentence naming the problem. |
| `findings[].draft_comment` | agent | The full comment body: the problem and its effect first, then the fix or a suggestion block. If it doesn't contain `description`, the launcher prefixes `**{description}**`. |
| `findings[].is_diff_hunk` | `diff_parser.py` | `true` for an inline comment. `false` means the comment goes under "Broader Context" in the summary. |
| `findings[].original_code` | `diff_parser.py` | The source lines the finding covers, shown in the dashboard. |
| `findings[].approved` | dashboard only | Don't write it. The launcher sets every finding to `false`, and the user checks the ones to post. |

`diff_parser.py` and `launch_dashboard.py` also accept and normalize these legacy/alias names: `pr_meta` for `pr_metadata`, a top-level `head_sha`, top-level `summary_comment` / `summary` / `overview` and `verdict` for `review_summary`, a string value for `pr_description`, top-level `inline_findings` / `general_findings` / `comments` for `findings`, and per-finding `file` / `path` (`file_path`), `start_line` / `line` (`line_start`), `end_line` / `line` (`line_end`), `title` (`description`), and `body` / `comment_body` (`draft_comment`). Always use the canonical names in new files.

## `pr_coords.json`, `pr_stack.json`, and `pr_context.json`

`review_engine.py` splits metadata and narrative fetching across two stages:

- **Stage 1 (`--prepare-workspace --output-coords pr_coords.json --output-stack pr_stack.json`)**: Writes `owner`, `repo`, `pull_number`, `base_ref`, `head_sha` (verified against the checked-out `HEAD` commit), `pr_url`, `state`, `workspace_dir`, and `stack` without `title`, `body`, `author`, `head_ref`, or comments. `stack` is `null` for an unstacked PR. For a stacked PR it holds numbers only: `number` (the stack's number), `size`, `position` (1 = bottom, closest to the trunk), `needs_rebase` for this PR, `source` (`gh stack view` or `git ancestry`), and `entries[]` with `number`, `state`, `is_draft`, `is_target`, `needs_rebase`, and (from `gh stack view`) `is_merged` and `is_queued`. `pr_stack.json` is written only for a stacked PR; it adds `trunk`, `entries_complete`, and each entry's `head_ref` and `base_ref` so Stage 2 can carry the rebase status over. It has no titles, but branch names are narrative, so leave it unread until Stage 2.
- **Stage 2 (`--fetch-context --input-coords pr_coords.json --input-stack pr_stack.json --require-blind-description blind_description.md --output-meta pr_meta.json --output-context pr_context.json`)**: Writes full `pr_metadata` to `pr_meta.json` (pinning `head_sha` to Stage 1's commit and setting `latest_head_sha` if the PR moved, plus `stack: {number, size, position}` for a stacked PR) and narrative context (`title`, `body`, `author`, `comments`, `reviews`, `threads`, `threads_complete`, and `stack`) to `pr_context.json`. `pr_context.json`'s `stack` is fetched fresh (so member `title`, `state`, `url`, `head_ref`, and `base_ref` are current) with `needs_rebase`, `is_merged`, and `is_queued` copied from `pr_stack.json` by PR number.

## `review_decisions.json`

The dashboard writes this when the user clicks **Submit Review to GitHub**:

```json
{
  "pull_number": 42,
  "owner": "octocat",
  "repo": "hello-world",
  "head_sha": "010d93ecc0c8ac1fc6618cdcbb18b5d55b16655c",
  "verdict": "REQUEST_CHANGES",
  "summary_comment": "…",
  "findings": [{ "…": "same fields as above, with the user's approved and draft_comment values" }]
}
```

`apply_review.py` posts only findings with `"approved": true`, and never includes `pr_description`. A finding without an `approved` field is not posted. Each posted comment and non-empty summary body has `<sub><!-- agent-generated --><kbd>🤖 Agent-generated</kbd></sub>` appended automatically.


