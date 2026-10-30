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
    "verdict": "REQUEST_CHANGES",
    "checked_clean": [
      "Memory leak in buffer allocation: Executed - valgrind clean",
      "Race condition in worker shutdown: Deduction - locked by mutex"
    ],
    "structural_assessment": {
      "implementer": "Clean interface definition",
      "tester / verifier": "Missing integration test coverage"
    }
  },
  "pr_description": {
    "markdown": "## Intent Alignment: Partial Divergence\n\n## Summary\nAdds empty-input handling to `src/parser.ts`...",
    "alignment": "PARTIAL"
  },
  "findings": [
    {
      "id": "H1",
      "severity": "high",
      "category": "correctness",
      "file_path": "src/parser.ts",
      "line_start": 120,
      "line_end": 122,
      "side": "RIGHT",
      "evidence_tier": "Read",
      "evidence_detail": "src/parser.ts:121 - return Promise without await",
      "description": "The promise from resolveDynamicValue() is never awaited.",
      "why": "Async functions report success before completion, dropping errors silently.",
      "draft_comment": "`resolveDynamicValue(fc)` returns a promise that is never awaited, so async functions report success before completion and drop errors silently.\n\n```suggestion\n    await resolveDynamicValue(fc);\n```",
      "is_diff_hunk": true,
      "original_code": "    resolveDynamicValue(fc);"
    },
    {
      "id": "Q1",
      "severity": "question",
      "file_path": "src/config.ts",
      "line_start": 45,
      "line_end": 45,
      "side": "RIGHT",
      "evidence_tier": "Speculation",
      "evidence_detail": "Unclear if default timeout applies to streaming RPCs",
      "description": "Does defaultTimeout apply to long-lived streaming calls?",
      "why": "If streaming RPCs use the 30s timeout, long transfers will terminate prematurely.",
      "to_settle": "Check grpc client timeout options in net/client.ts",
      "draft_comment": "Does defaultTimeout apply to long-lived streaming calls? If streaming RPCs use the 30s timeout, long transfers will terminate prematurely.",
      "is_diff_hunk": true
    }
  ]
}
```

| Field | Written by | Meaning |
| :--- | :--- | :--- |
| `pr_metadata` | agent, from `pr_meta.json` | Copy `review_engine.py --output-meta` output as is. The dashboard needs `owner`, `repo`, `pull_number`, `head_sha`, and `title` (plus optional `latest_head_sha` when new commits were pushed after Stage 1). Passing `--meta-file` to the launcher fills any missing fields. |
| `review_summary.overview` | agent | 1-2 paragraphs: what the PR does, the assessment, and the verdict rationale. Don't list the findings. |
| `review_summary.verdict` | agent | `COMMENT`, `REQUEST_CHANGES`, or `APPROVE`. The user can change it. |
| `review_summary.checked_clean` | agent / `code-review` | Optional array of disproved hypotheses verified clean during review. |
| `review_summary.structural_assessment` | agent / `code-review` | Optional dictionary keyed by the lowercased Structural Assessment labels that `lint_review.py --export-json` emits (`implementer`, `tester / verifier`, `consumer`, `operator / adversary`, `repo validators`, `downstream impact`, and `unchanged-file findings` when present). Any additional bold label in that section becomes a key the same way. |
| `pr_description` | `--description-file` or agent | Reviewer-only PR description and intent alignment panel shown at the top of the dashboard (`{"markdown": "...", "alignment": "MATCHES\|PARTIAL\|DIVERGES\|UNSTATED"}`). Passing `--description-file <scratch>/pr_description.md` to `launch_dashboard.py` populates this automatically. `apply_review.py` never posts this field; its text is included in a review only if the user explicitly clicks **+ Insert into Review Summary**. |
| `findings[].severity` | agent | `critical`, `high`, `medium`, `low`, or `question`. |
| `findings[].line_start`, `line_end` | agent | Lines in the new file (`side: RIGHT`) or the old file (`side: LEFT`). |
| `findings[].evidence_tier` | agent / `code-review` | `Executed`, `Read`, `Fetched`, `Deduction`, or `Speculation`. |
| `findings[].evidence_detail` | agent / `code-review` | Command output, citation location, or deductive trace. |
| `findings[].why` | agent / `code-review` | Concrete consequence if shipped as-is. |
| `findings[].suggestion` | `code-review` | Raw text of the finding's `Suggestion` block. Already folded into `draft_comment` (as a committable ```` ```suggestion ```` block unless it was a ```` ```diff ````/```` ```patch ```` fence); nothing downstream reads it on its own. |
| `findings[].repro_test` | `code-review` | (`--verify` only) Raw text of the finding's `Repro test` block. Folded into `draft_comment` under a `**Repro test**` label as a plain code block, never as a committable suggestion. |
| `findings[].to_settle` | agent / `code-review` | (Question findings only) Exact command, file, or symbol that settles the question. |
| `findings[].description` | agent | One short sentence naming the problem for the dashboard card header. |
| `findings[].draft_comment` | agent / `code-review` | The concise peer-facing comment body ($\le 80$ prose words, $\le 4$ sentences): the defect and its consequence first, followed by a suggestion block (or a natural 1–2 sentence question for `Q1…`). If `draft_comment` is empty, the launcher falls back to `description`. |
| `findings[].is_diff_hunk` | `diff_parser.py` | `true` for an inline comment. `false` means the comment goes under "Broader Context" in the summary. |
| `findings[].original_code` | `diff_parser.py` | The source lines the finding covers, shown in the dashboard. |
| `findings[].approved` | dashboard only | Don't write it. The launcher sets every finding to `false`, and the user checks the ones to post. |

`diff_parser.py` and `launch_dashboard.py` also accept and normalize these legacy/alias names: `pr_meta` for `pr_metadata`, a top-level `head_sha`, top-level `summary_comment` / `summary` / `overview` and `verdict` for `review_summary` (a dict-valued `summary` is treated as `review_summary`), a string value for `pr_description`, top-level `inline_findings` / `general_findings` / `comments` for `findings`, a top-level `questions` list (merged into `findings` and then removed; entries without a `severity` default to `"question"`), and per-finding `file` / `path` (`file_path`), `start_line` / `line` (`line_start`), `end_line` / `line` (`line_end`), `title` (`description`), and `body` / `comment_body` (`draft_comment`). Always use the canonical names in new files.

## `pr_coords.json`, `pr_stack.json`, and `pr_context.json`

`review_engine.py` splits metadata and narrative fetching across two stages:

- **Stage 1 (`--prepare-workspace --output-coords pr_coords.json --output-stack pr_stack.json`)**: Writes `owner`, `repo`, `pull_number`, `base_ref`, `head_sha` (verified against the checked-out `HEAD` commit), `pr_url`, `state`, `workspace_dir`, `stack`, `security_prescan`, and `review_round` (`1` on the first review in `<conversation-scratch-dir>`, incremented on each re-review) without `title`, `body`, `author`, `head_ref`, or comments. When an existing `pr_coords.json` for the same PR is present in the scratch directory, `--prepare-workspace` records `prev_head_sha`, writes `pr_incremental.diff` (`incremental_diff_file` in `pr_coords.json`) when `head_sha` moved, moves the prior `blind_description.md` to `prev_blind_description.md` so Stage 1 must write a fresh description, and archives `review_results.json` and `review_decisions.json` to `prev_review_results.json` and `prev_review_decisions.json`. `security_prescan` holds the static execution-risk summary over `pr.diff` (`status` as `"clean"` or `"flagged"`, `risk_count`, `flagged_files[]`, `categories` map, and `signals[]` with `category`, `file_path`, `line`, `reason`, and `snippet`; also available standalone via `review_engine.py --security-scan <diff-file>`). `stack` is `null` for an unstacked PR. For a stacked PR it holds numbers only: `number` (the stack's number), `size`, `position` (1 = bottom, closest to the trunk), `needs_rebase` for this PR, `source` (`gh stack view` or `git ancestry`), and `entries[]` with `number`, `state`, `is_draft`, `is_target`, `needs_rebase`, and (from `gh stack view`) `is_merged` and `is_queued`. `pr_stack.json` is written only for a stacked PR; it adds `trunk`, `entries_complete`, and each entry's `head_ref` and `base_ref` so Stage 2 can carry the rebase status over. It has no titles, but branch names are narrative, so leave it unread until Stage 2.
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


