# Post-Plan UI & Fallback Guide (`post_plan_ui.md`)

This guide covers the pre-plan question rule, the `render_a2ui` calls for each post-plan surface, how to record decisions by hand, and the three-tier fallback contract.

---

## 1. Pre-Plan Question Rule: Write First, Ask Second

Before the plan artifact exists on disk, ask only questions that block scope. Examples include which repository to target or whether a major subsystem is in scope.

- Write pre-plan questions in plain language.
- Never use option letters (`Option A`), wave names (`Wave 2`), or question IDs (`Q1`) before the user has a plan to read.
- Put every design trade-off, API detail, schema choice, and rollout question into `## Open Questions` inside the plan artifact. Ask them after the plan is drafted via the post-plan worksheet.

### Bad Example (from the `/plan` transcript audit)

Before drafting a plan, the agent rendered **"Express number literals: four decisions before the plan"**. Its **"Confirm and write the plan"** button gated four internal parser questions referencing unseen phases.

### Good Example

- When scope is clear, ask nothing before drafting. Write the plan artifact with `### Q1` to `### Q4` under `## Open Questions`, present the brief, and render the post-plan worksheet.
- When scope is blocked, ask one plain question before drafting: _"Should this change target only the CLI package, or also update the web client?"_

---

## 2. Post-Plan Surfaces (`render_a2ui`)

Always pass `conversationId` from the session metadata or `$ANTIGRAVITY_CONVERSATION_ID`.

- After any `mode: "pane"` render, include `[A2UI Pane](sidecar://a2ui-pane/a2ui-pane/)` inline in your chat reply.
- After any `mode: "inline"` render, include the returned `<agent-embed ...></agent-embed>` tag in your chat reply and end your turn without calling `await_a2ui_action`.

### 2.1 Post-Plan Worksheet (`dsl` only, no `template`)

Generate the Express worksheet from the validated plan:

```bash
python3 <skill-dir>/scripts/plan_surfaces.py questions <path/to/plan.md>
```

The script emits a standalone Express surface (`surface("plan-decisions", sendDataModel=true)`):

1. The header shows `### Plan Decisions` and the caption `"Recommended answers are preselected."`.
2. Each `### Q<n>` in `## Open Questions` becomes a block with a `Q<n>` badge header and `ChoiceCards($/qN/options, $/qN/choice)`. Each option card carries `value`, `label` (`"<letter> · <text>"`), `description` (the option's consequence), and `badge: "Recommended"` with `tone: "success"` on the recommended option, followed by a quiet `{value: "defer", label: "Defer", quiet: true}` toggle. Each block preselects the recommended letter.
3. The shared footer has one notes field (`$/notes`), a review checkbox (`CheckBox("Run an independent review of this plan (recommended)", $/review)` with `$/review = true`), and one primary button sending `Event("submit_plan_decisions", {review: $/review, q1: $/q1/choice, ..., notes: $/notes})`.

Render the worksheet by passing the script's stdout as `dsl` (do **not** set `template`):

```json
{
  "conversationId": "<conversation-id>",
  "surfaceId": "plan-decisions",
  "mode": "pane",
  "dsl": "<stdout of plan_surfaces.py questions>"
}
```

Use `mode: "inline"` when the plan has zero open questions and the worksheet contains only the notes field and footer review checkbox.

If `render_a2ui` fails validation because an older `a2ui-pane` install does not recognize `ChoiceCards`, retry with `--layout list`:

```bash
python3 <skill-dir>/scripts/plan_surfaces.py questions <path/to/plan.md> --layout list
```

The `list` layout replaces `ChoiceCards` with one letter-badge row and consequence caption per option plus short `A`, `B`, ..., `Defer` chips, while keeping the same intro caption, footer review `CheckBox`, and `review: true|false` payload.

| Tier / Format                                            | How the review choice is presented                           | How to read the review answer                                                  |
| :------------------------------------------------------- | :----------------------------------------------------------- | :----------------------------------------------------------------------------- |
| Tier 1 (`plan_surfaces.py questions`, `cards` or `list`) | Footer `CheckBox` bound to `$/review` (`true` by default)    | `review: true` or `review: false` in the `submit_plan_decisions` event payload |
| Tier 2 (`--format ask-question`)                         | First question (`"Run an independent review of this plan?"`) | `"(Recommended) Yes"` or `"No"`                                                |
| Tier 3 (`--format markdown`)                             | Item 1 in the numbered Markdown list                         | User reply of `Yes` (or option 1) vs `No`                                      |

If review findings later invalidate a recorded decision or add new `### Q<n>` questions, render a second worksheet with only the new or changed `Q<n>` blocks. Omit the review checkbox on that second worksheet.

### 2.2 Review Findings Triage (`template: "triage_matrix"`)

When the review checkbox is checked (or the fallback answer is Yes) and the reviewer reports 3 or more findings, save the fenced `json` array (`[{id, severity, title, summary, evidence}]`) to `<scratch>/findings.json`. Run:

```bash
python3 <skill-dir>/scripts/plan_surfaces.py triage <scratch>/findings.json
```

The script maps severities to icons and pre-selects `Apply` for `"blocker"` and `"major"` findings and `Discuss` for `"minor"` findings. It also redefines `bucket_picker` with `Apply`, `Discuss`, and `Reject` chips. Render with:

```json
{
  "conversationId": "<conversation-id>",
  "template": "triage_matrix",
  "surfaceId": "plan-review-triage",
  "mode": "pane",
  "dsl": "<stdout of plan_surfaces.py triage>"
}
```

When the reviewer reports 1 or 2 findings, apply straightforward fixes directly and note them in chat without opening a triage surface.

### 2.3 Plan Overview (`template: "plan_overview"`)

For multi-wave plans (2 or more waves), generate the overview data lines:

```bash
python3 <path/to/plan>/scripts/plan_surfaces.py overview <path/to/plan.md> [--diagram {auto,compact,plan}] > <scratch>/overview.a2ui
```

The `--diagram auto` flag is the default. It synthesizes a compact `flowchart TD` `subgraph` diagram from parsed waves. It falls back to the plan's raw Mermaid block only when `### Wave N` headings are absent.

Use `--diagram plan` to preserve a custom architecture diagram in Mermaid verbatim. Use `--diagram compact` to force synthesis from parsed waves.

The script emits data assignments matching the `plan_overview` contract (`/title`, `/subtitle`, `/status`, `/statusTone`, `/glance[]`, `/waveDiagram`, `/prsHeading`, `/prs[]`, `/openQuestions`, `/ackLabel`). Render with:

```json
{
  "conversationId": "<conversation-id>",
  "template": "plan_overview",
  "surfaceId": "plan-overview",
  "mode": "pane",
  "dsl": "<stdout of plan_surfaces.py overview>"
}
```

### 2.4 Wave Progress (`template: "progress_board"`)

After the user clicks **Proceed**, render `progress_board` at the start of Wave 1 with one row per wave and parallel track:

```json
{
  "conversationId": "<conversation-id>",
  "template": "progress_board",
  "surfaceId": "plan-progress",
  "mode": "pane",
  "dsl": "$/title = \"### Plan execution\"\n$/subtitle = \"Wave 1 running.\"\n$/items = [\n  {label: \"**Wave 1:** Shared parser and contract\", detail: \"plan_model.py\", status: \"Running\", tone: \"active\"},\n  {label: \"**Track 2A:** Validator checks\", detail: \"validate_plan.py\", status: \"Queued\", tone: \"neutral\"},\n  {label: \"**Track 2B:** Post-plan surfaces\", detail: \"plan_surfaces.py\", status: \"Queued\", tone: \"neutral\"},\n  {label: \"**Wave 3:** Skill workflow and commit\", detail: \"SKILL.md\", status: \"Queued\", tone: \"neutral\"}\n]"
}
```

As each wave or track starts and completes, send data-only updates to the same `surfaceId` (`"plan-progress"`):

```json
{
  "conversationId": "<conversation-id>",
  "surfaceId": "plan-progress",
  "mode": "pane",
  "dsl": "$/subtitle = \"Wave 2 running (2 tracks).\"\n$/items/0/status = \"Done\"\n$/items/0/tone = \"success\"\n$/items/1/status = \"Running\"\n$/items/1/tone = \"active\"\n$/items/2/status = \"Running\"\n$/items/2/tone = \"active\""
}
```

### 2.5 Landing Gate (`template: "confirm_gate"`)

After the final wave verification passes, render an inline `confirm_gate` titled **Land this work** with a summary listing the code review, commit, and PR steps:

```json
{
  "conversationId": "<conversation-id>",
  "template": "confirm_gate",
  "surfaceId": "plan-landing-gate",
  "mode": "inline",
  "dsl": "$/title = \"### Land this work\"\n$/summary = \"All waves passed verification. Next steps:\\n1. Run `/code-review --fast` on the diff and fix blocker or major findings.\\n2. Commit the changes with `commit-changes` as listed in the plan.\\n3. Open or update the pull requests.\"\n$/confirmLabel = \"Run code review, commit, and open PRs\"\n$/confirmButtonLabel = \"Land changes\"\n$/cancelLabel = \"Stop here\""
}
```

---

## 3. Recording Decisions by Hand

There is no script that patches answers into the plan. Record user answers by editing the plan artifact yourself:

1. Write the decision in your own words: state the chosen option letter, the date (`YYYY-MM-DD`), and a short summary incorporating any caveats from `$/notes`.
2. Delete each answered `### Q<n>` block from `## Open Questions` and add a bullet under `## Decisions`.
3. If the user chose `Defer` on `Q<n>`, leave that `### Q<n>` block in `## Open Questions`.
4. Update `- **Decisions needed from you:**` in `## At a glance`. When no open questions remain, set it to `none.` and write `None currently blocking.` under `## Open Questions`.
5. Run `validate_plan.py` after saving the updated plan.

### Before Worksheet Submission

```markdown
## At a glance

- **Goal:** Make plans faster to absorb and move post-plan decisions into UI.
- **Shape:** 3 waves, 4 parallel tracks, and 4 commits.
- **Decisions needed from you:** 2 (Q1 and Q2 under Open Questions).
- **Biggest risk:** New error checks fail older plans when revalidated.
- **Not doing:** No eval harness and no sync to the public `skills` repo.

## Open Questions

### Q1: Should the new validator checks be errors or warnings?

- A: Warnings at first, errors with `--strict`. Consequence: older plans still pass without edits.
- (Recommended) B: Errors right away. Consequence: older plans fail if revalidated until updated.

### Q2: How should user answers be written back into the plan?

- A: Add an `apply-decisions` script. Consequence: fast and deterministic, but wording is mechanical.
- (Recommended) B: Record answers by hand. Consequence: the agent can reword answers and fold in notes.

## Decisions

None yet.
```

### After Worksheet Submission (`q1 = ["b"]`, `q2 = ["b"]`, `notes = "Fix old plans only if we touch them"`)

```markdown
## At a glance

- **Goal:** Make plans faster to absorb and move post-plan decisions into UI.
- **Shape:** 3 waves, 4 parallel tracks, and 4 commits.
- **Decisions needed from you:** none. Your two answers are recorded under Decisions.
- **Biggest risk:** New error checks fail older plans when revalidated. You accepted this (Q1).
- **Not doing:** No eval harness and no sync to the public `skills` repo.

## Open Questions

None currently blocking.

## Decisions

- **Q1 (2026-11-04):** B. New checks are errors right away. Older plans get updated only when they are revalidated.
- **Q2 (2026-11-04):** B. The agent records answers in the plan by hand so it can reword them and fold in notes.
```

---

## 4. Three-Tier Fallbacks

When `render_a2ui` is unavailable, fall back in the three-tier order:

1. Tier 1 (`a2ui-pane`): Render the A2UI surfaces above.
2. Tier 2 (Host question tool, such as `ask_question` or `AskUserQuestion`): Pass `--format ask-question` to `plan_surfaces.py questions` or `plan_surfaces.py triage` for a JSON question list with `(Recommended)` first.
3. Tier 3 (Chat turn boundary): Pass `--format markdown` to `plan_surfaces.py questions` or `plan_surfaces.py triage` for a numbered Markdown prompt, output it in chat, and end your turn.

Fallbacks happen at the same point in the workflow as the surface they replace. The plan artifact stays the source of truth in every tier.

| Surface             | Tier 2: host question tool                                                                                                                                                                                   | Tier 3: chat                                                                                                                      |
| :------------------ | :----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :-------------------------------------------------------------------------------------------------------------------------------- |
| Post-plan worksheet | One `ask_question` call: the review item first, then one question per `Q<n>`, with the recommended option first and labeled `(Recommended)`. Split into several calls if the tool limits the question count. | A numbered list (review item first, then each `Q<n>` with lettered options and the recommendation marked). End the turn and wait. |
| Review triage       | One multi-select question per severity group ("Apply these findings?"), with the blocker and major findings preselected.                                                                                     | A short table of findings with a proposed disposition for each. Ask for overrides and end the turn.                               |
| Plan overview       | Not needed. The At a glance section and wave diagram in the artifact already serve as the overview.                                                                                                          | Same: no extra chat output.                                                                                                       |
| Wave progress       | Not applicable.                                                                                                                                                                                              | One short status line in chat at each wave barrier, plus ticked checkboxes in the plan artifact.                                  |
| Landing gate        | One single-choice question: `(Recommended) Review, commit, and open PRs`, `Commit only`, `Stop here`.                                                                                                        | The same three options as a numbered prompt. End the turn and wait.                                                               |
