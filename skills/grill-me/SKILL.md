---
name: grill-me
description: >-
  Runs a focused alignment session before work continues: finds unverified
  assumptions, API or schema details that need tuning, architectural forks,
  scope and edge-case triage, state-machine failure modes, and rollout ordering,
  then asks about them through genui surfaces when the a2ui-pane plugin is
  installed or the host's question tool otherwise. Use when returning to a task
  after a break, when a task feels stalled or misaligned, before a costly
  architectural pivot, before transitioning to /plan, or when the user runs
  /grill-me. Don't use to execute the plan itself (use plan), or for questions
  the codebase can answer by reading it.
---

# Grill-Me

`grill-me` runs a focused alignment session. It decides *what* to ask and *which shape* the question has; the `genui` skill (shipped by the `a2ui-pane` plugin) decides how to draw it. Read `genui` before rendering.

## 1. Identify the blind spots

Review the task state, codebase, and conversation. Look for:

- Inferred codebase facts or invariants that were never confirmed.
- An API route, schema, CLI flag surface, or payload that needs field-level tuning.
- An architectural fork with competing strategies and non-obvious blast radius.
- A batch of discovered edge cases, call sites, or findings that needs scope triage.
- A stateful workflow, retry loop, or protocol with subtle transition failures.
- Tasks or deferred decisions with dependencies that govern rollout order.

If the codebase can answer a question, read the codebase and present the answer as a pre-populated default instead of asking.

## 2. Match the shape to decision complexity

Scale the visual weight of the surface to the complexity of each question.

### Single-shape batches and specialized workflows

Use a named `genui` template when every item in the turn shares one uniform shape:

| Shape of the decision | `genui` template | Surface |
| :--- | :--- | :--- |
| Confirm 2 to 5 inferred facts by exception | `assumption_ledger` | inline |
| Batch of uniform `Yes / No / Defer` confirmations | `decision_batch` | inline (<=3 items) or pane (>=4) |
| Tune 2 to 4 identifiers, routes, or parameters with live validation | `contract_workbench` | inline |
| Compare 2 to 3 strategies with stacked consequence cards, badges, trace, and multi-select hybrid chips | `consequence_cards` | inline (1 compact fork) or pane (with trace or 3+ cards) |
| Compare 3 or more options across criteria with multi-select chips | `comparison_matrix` | pane |
| Classify 3 to 12 items into delivery buckets | `triage_matrix` | pane |
| Audit transitions and retry policies under a persistent state diagram | `state_walkthrough` | pane |
| Assign work items to rollout waves under a dependency DAG | `wave_sequencer` | pane |

### Mixed-complexity, orthogonal-axis, or improvised Express surfaces (3-Tier Rubric)

Because `ChoicePicker.options` is static in the A2UI v1.0 Basic Catalog, a `_template` list such as `decision_batch` gives every item the exact same chip labels. Templates and bespoke A2UI Express (`dsl` without `template`) are co-equal peers: when a turn needs custom domain choices per question, decomposes independent design axes, previews candidate UI layouts in `Tabs`, tunes thresholds with `Slider`, or steps through >4 deep forks with `Stepper`, author a custom A2UI Express surface. Combine these three block tiers above one shared footer:

| Complexity tier | When to use | Express block anatomy |
| :--- | :--- | :--- |
| **Tier 1: Simple Choice** (`QuickChoice`) | Scope checks, naming picks, or low-risk defaults where the tradeoff is obvious from the option labels. | `Row([Text(prompt, "body", 1), Badge(tag, tone)])`, optional 1-line `Text(hint, "caption")`, and `ChoicePicker` with 2 to 4 named chips (`(Recommended)` first, `"Defer"` last). Omit per-item text fields. |
| **Tier 2: Consequence-Aware Decision** (`ConsequenceCard`) | Choices between 2 to 5 named alternatives with downstream blast radius (API contracts, cross-SDK parity, test churn, migration cost). | Header row with semantic impact `Badge`, brief context caption, an inline bulleted `Text` block (`**Consequences of this decision:**\n- ...`), a `Collapsible("Affected files & blast radius", Text(trace, "caption"), summary)` disclosure, and named `ChoicePicker` chips (use `"multipleSelection"` when options can combine). |
| **Tier 3: Complex Architectural Fork** (`ForkCards` / `CriteriaTable` / `ParameterTuner`) | Competing strategies with multi-dimensional tradeoffs, orthogonal axes, live layout previews, or tunable numeric parameters. | Vertically stacked option `Card`s (`Column(_template($/q3/options, ...), _, "stretch")`), a dense `Table` with `kind: "badge"` cells, `Tabs` layout previews, or `Slider` + `CheckBox` controls above `"multipleSelection"` or per-axis `ChoicePicker` chips (use a 1-by-1 `Stepper` pane deck when >4 deep forks exist). |

End the worksheet with a single shared `TextField("Overall notes or overrides", $/notes)` and one primary `Button(Text("Submit Decisions"), "primary", Event("submit_alignment", ...))`. Copy-pasteable Express block recipes are in `genui`'s `references/templates.md`.

## 3. Prepare the context and defaults

1. In the chat message, state in one or two sentences what the code or plan does now and why alignment is needed, with clickable `[file](file:///...)` links.
2. Put real option names on the `ChoicePicker` chips. Never encode named alternatives inside caption text as `"Yes = Option A; No = Option B"` just to fit `decision_batch`.
3. Put consequences inside the surface. On Tier 2 and Tier 3 questions, include an inline **Consequences of this decision** bullet list and a `Collapsible` blast-radius trace of affected files.
4. Keep simple questions clutter-free: omit per-item `Or specify` and `Note` text boxes on Tier 1 questions and rely on the shared footer note field.
5. Pre-populate your recommendation in the data (`$/q1/choice = ["dual_write"]`, `holds: true`, `bucket: ["p0"]`) so the user edits exceptions in one click.

## 4. Render

- With the plugin: follow `genui`. Call `a2ui_instructions` once per session, then `render_a2ui({ conversationId, template, surfaceId, mode, dsl })` with your own conversation ID. After a pane render include `[A2UI Pane](sidecar://a2ui-pane/a2ui-pane/)` in the reply; after an inline render include the `<agent-embed>` tag. End the turn and wait.
- Without the plugin (`render_a2ui` is neither a native tool nor a lazily loaded MCP tool of the `a2ui-pane` server reachable through `call_mcp_tool`): present the same questions in one batch using your host's interactive question tool (`ask_question` in Jetski/Antigravity, `AskUserQuestion` in Claude Code) or as a numbered list in chat (ending your turn to wait for a reply). Put thought into selecting the recommended option, list it first with `(Recommended)`, add a `"Let's come back to this"` option, and put the consequence text in the chat message as a short table.

## 5. Handle the answer

Inspect the `<a2ui-action>` payload (or the interactive question tool / chat reply):

- **Settled items**: acknowledge the choices (including combined `"multipleSelection"` picks) and say how they shape the next steps; update `task.md` or `implementation_plan.md` if they exist.
- **Unchecked assumptions or follow-up questions in `notes`**: keep every settled answer, trace the flagged item in the codebase (call sites, types, failure modes), and render a follow-up scoped to the unresolved items only.
- **Deferred items (`choice: ["defer"]`, `wave: ["defer"]`), the Parking Lot**: note what depends on each, present a baseline plan with explicit `[DEFERRED: ...]` placeholders and say whether anything is blocked, then drain the lot in dependency order (foundational decisions first), using `wave_sequencer` when ordering itself is the question. When the lot is empty, present the completed plan.
