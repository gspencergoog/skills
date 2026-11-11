# Design Convergence & Interactive UI Guide (`design`)

This reference provides the decision-to-UI routing table and the Phase 2 and Phase 3 artifact skeletons for `/design`.

## 1. Decision-to-UI Routing (`genui` Templates + Improvised Express)

Whenever Phase 2 raises 2 or more decisions, render an interactive surface instead of trailing prose questions. Treat built-in `genui` templates and bespoke A2UI Express (`dsl` without `template`) as co-equal choices. Always call `a2ui_instructions` once per session before writing custom `dsl`.

| Decision shape | Surface choice | Mode & key mechanics |
| :--- | :--- | :--- |
| Verify 2 to 5 inferred scope or portability facts | `template: "assumption_ledger"` | `mode: "inline"`. Pre-check assumptions that hold; user unchecks exceptions. |
| Confirm 2 to 6 short decisions with per-item `options` | `template: "decision_batch"` | `mode: "inline"` (`<= 3` items) or `"pane"` (`>= 4` items). Each item carries its own `options` list rendered by `ChoiceCards`; use `ChoiceCards` in Express when named alternatives need consequences, headers, or traces. Never encode named options as `"Yes = A; No = B"`. |
| Compare 2 to 3 strategies with rich impact & blast radius | `template: "consequence_cards"` | `mode: "inline"` or `"pane"`. Cards stack vertically in a `Column`; chips use `"multipleSelection"` so hybrids (`A + B`) take two clicks. |
| Compare 3 or more options across 4 or more criteria | `template: "comparison_matrix"` | `mode: "pane"`. Badge `Table` (`width: "14ch"` on criterion column) + `"multipleSelection"` chips. |
| Independent design dimensions (Storage x Transport x Trigger) | Improvised Express: **Orthogonal Axis Mixer** | `mode: "pane"` or `"inline"`. One `ChoicePicker` per independent axis instead of bundled cross-product options. |
| Candidate UI layouts or component arrangements | Improvised Express: **Live Layout Preview** | `mode: "pane"`. Render live interactive mockups inside `Tabs` above a picker. |
| Numeric budgets, timeouts, caps, or policy flags | Improvised Express: **Threshold Tuner** | `mode: "inline"` or `"pane"`. `Slider(label, min, max, $/val, steps, weight)` (7-slot v1.0 order) + `CheckBox` + reactive `Text(formatString(...))`. |
| Mixed question tiers (scope + fork + tuner in one turn) | Improvised Express: **Multi-Tier Worksheet** | `mode: "pane"`. Combine `QuickChoice`, `ConsequenceCard`, and `Table` or `Slider` blocks above one shared notes field and submit button. |
| More than 4 deep architectural forks | Improvised Express: **1-by-1 `Stepper` Deck** | `mode: "pane"`, `replace: true`. Render `Stepper($/steps, $/currentStep, "active")` above one fork per turn, advancing `$/currentStep` as answers arrive. |

Copy-pasteable Express v1.0 snippets for all four improvised patterns live in `genui`'s `references/templates.md` under `## Improvising Custom A2UI Express Surfaces`.

### Pre-Checked Independent Design Review Option (`runReview`)

For **Architecture or feature design** and **Empirical audit and redesign** sessions (skip for **Creative, naming, or demo ideation**), include a preselected review toggle so Phase 3 automatically runs `/design-review --post-design` with a `< 4 KB` `Design Briefing Packet`:

- **Custom A2UI Express (`dsl` without `template`)**: Set `$/runReview = true`, mount `review_check = CheckBox("Run independent design review after convergence (recommended)", $/runReview)` inside the shared footer `Row([review_check, submit_btn], "spaceBetween", "center")`, and pass `runReview: $/runReview` in the `Event(...)` context. Never declare an unmounted `CheckBox` inside a named template (`consequence_cards`, `comparison_matrix`, `decision_batch`), which fails orphan validation.
- **`template: "decision_batch"`**: Append a final item `{prompt: "Post-convergence design review", hint: "Runs a clean-room /design-review --post-design check on the converged spec.", options: [{value: "yes", label: "Yes · Clean-room review", badge: "Recommended", tone: "success"}, {value: "no", label: "No · Skip review"}], choice: ["yes"]}`.
- **Tier 2 / Tier 3 Fallback (`ask_question` / `AskUserQuestion` or numbered chat)**: Include `"Run an independent design review on the converged spec?"` with `"(Recommended) Yes"` listed first.

### Cross-Harness Fallback (When `a2ui-pane` Is Not Installed)

1. Use your host's structured question tool (`ask_question` or `AskUserQuestion`). Place `(Recommended)` on the first option and include `"Defer"` where appropriate.
2. If no question tool exists, present a numbered list in chat (`(Recommended)` first) and end your turn to wait for the user's reply.

______________________________________________________________________

## 2. Phase 2 Artifact Skeleton (Diverge & Ask)

Target `150` to `250` lines and at most `2,000` words. Keep prose crisp (`<= 20` words per sentence, zero em-dashes, one comparison table):

```markdown
# <Topic>: Design Options

## At a Glance
- **Problem**: <1-2 sentences grounded in current code or metrics>
- **Hard Constraints**: <runtime, compatibility, or scope limits>
- **Recommended Direction**: <1-sentence synthesis and why>

## Current Baseline & Pain Points
<Short evidence summary with clickable [file](file:///...) links; pair with an inline Chart in chat when >=4 shared-unit metrics exist.>

## Constraints & Non-Goals
- <Constraint 1>
- **Non-Goal**: <Dismiss strawman or out-of-scope approach in 1-2 sentences rather than a full Option section.>

## Candidate Approaches (or Orthogonal Design Axes)

### Option A: <Name> (Recommended)
<Core mechanism in 1 short paragraph + Mermaid flowchart or sequenceDiagram when structural flow differs.>

### Option B: <Name>
<Core mechanism in 1 short paragraph + diagram if structurally distinct.>

### Tradeoff Matrix
| Criterion | Option A (Recommended) | Option B |
| :--- | :--- | :--- |
| Blast radius | ... | ... |
| Complexity | ... | ... |
| Operational risk | ... | ... |

## Open Decisions
<Brief pointer to the interactive A2UI surface rendered in this turn (or numbered fallback questions).>
```

______________________________________________________________________

## 3. Phase 3 Artifact Skeleton (In-Place Converged Design Spec)

When user decisions arrive on an architectural or feature task, update the **existing** artifact in place so `/plan` reads one authoritative design file:

```markdown
# <Topic>: Converged Design

## At a Glance
- **Problem**: <1-2 sentences>
- **Chosen Direction**: <Selected or combined hybrid architecture>
- **Status**: Converged; ready for `/plan`

## Settled Decisions
- **D1 (YYYY-MM-DD)**: <Chosen option or hybrid combination and rationale>
- **D2 (YYYY-MM-DD)**: <Chosen threshold, axis, or constraint>

## Architecture & Component Design
<Unified prose and Mermaid architecture diagram for the chosen design.>

## Data Model & API Contracts
<Concrete types, schemas, wire envelopes, or function signatures verified against `review-core/references/canonical_api_design.md`.>

## Lifecycle, State & Failure Modes
<Mermaid stateDiagram-v2, sequenceDiagram, or erDiagram where applicable, plus timeout, retry, compatibility, and rollback behavior.>

## Rollout, Migration & Expected PR Split
1. <Wave / PR 1 scope>
2. <Wave / PR 2 scope>

## Verification & Benchmark Sketch
- <Key invariant, unit/golden test, and live smoke or benchmark check>

## Alternatives Considered
| Option | Summary | Why Not Selected | Revisit If |
| :--- | :--- | :--- | :--- |
| <Rejected Option B> | <1-line summary> | <Key tradeoff> | <Trigger condition> |

## Design Review Sign-Off
<Included when `--post-design` ran cleanly: overall principle score, verdict, and 1 to 3 disproved pre-mortem or simpler-baseline hypotheses.>
```
