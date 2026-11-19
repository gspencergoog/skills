# Design Convergence & Interactive UI Guide (`design`)

This reference provides the decision-to-UI routing table, footer and multi-turn mechanics, and the Phase 2 and Phase 3 artifact skeletons for `/design`.

## Table of Contents

- [1. Decision-to-UI Routing](#1-decision-to-ui-routing)
- [2. Phase 2 Artifact Skeleton](#2-phase-2-artifact-skeleton)
- [3. Phase 3 Artifact Skeleton](#3-phase-3-artifact-skeleton)

## 1. Decision-to-UI Routing

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

### Footer Controls: Review Toggle (`runReview`) & Fast-Path Escape Hatch

On architectural and empirical sessions (skip `runReview` for creative ideation), pair a preselected review toggle with a global fast-path exit (`Accept remaining defaults and converge now`). Place that fast-path exit in the shared footer or as a standalone final item, separate from per-question options. Include the optional Round 2 rebuttal control when `--panel` personas split on a load-bearing tradeoff and Round 2 has not run yet (see [panel_divergence.md](panel_divergence.md)):

- In custom A2UI Express (`dsl` without `template`), set `$/runReview = true` and mount `review_check = CheckBox("Run independent design review after convergence (recommended)", $/runReview)` in the shared footer `Row`. Mount `fast_btn = Button(Text("Accept remaining defaults and converge now"), "default", Event("converge_defaults", {runReview: $/runReview, _oneShot: true, _group: "design_submit"}))` beside `submit_btn`, and pass `runReview: $/runReview` in both `Event(...)` payloads. On a `--panel` split, also mount `r2_btn = Button(Text("Run Round 2 rebuttals"), "default", Event("run_panel_round2", {_oneShot: true, _group: "design_submit"}))` alongside `fast_btn` and `submit_btn`. Sharing `_group: "design_submit"` disables all three on click, and `r2_btn` omits `runReview` since Round 2 returns to Phase 2 rather than converging.
- In `template: "decision_batch"`, append a preselected review item `{prompt: "Post-convergence design review", hint: "Runs a clean-room /design-review --post-design check on the converged spec.", options: [{value: "yes", label: "Yes · Clean-room review", badge: "Recommended", tone: "success"}, {value: "no", label: "No · Skip review"}], choice: ["yes"]}` and a separate final fast-path item `{prompt: "Convergence pace", options: [{value: "review_answers", label: "Use selections above", badge: "Default"}, {value: "converge_now", label: "Accept remaining defaults and converge now"}], choice: ["review_answers"]}`. On a `--panel` split, add `{value: "round2", label: "Run Round 2 rebuttals before converging"}` inside the existing `"Convergence pace"` item instead of a conflicting separate item.
- On Tier 2 or Tier 3 fallbacks (`ask_question`, `AskUserQuestion`, or numbered chat), ask `"Run an independent design review on the converged spec?"` with `"(Recommended) Yes"` first. Offer `"Accept remaining defaults and converge now"` as a separate final item or footer note, and offer `"Run Round 2 rebuttals before converging"` alongside it when the panel split. Choosing the fast-path exit applies each question's `(Recommended)` option plus all inline `[assumed: say otherwise]` defaults and converges into Phase 3.

Never declare an unmounted `CheckBox` or `Button` inside a named template (`consequence_cards`, `comparison_matrix`, `decision_batch`), which fails orphan validation.

### Multi-Turn `Settled | Open` Ledger & Backtracking

When Phase 2 spans multiple turns (such as a `Stepper` Deck or premise-shift backtrack), append a 2-line ledger in chat. This keeps progress visible without rereading the draft:

```markdown
Settled: D1 (<chosen option>), D2 (<chosen axis>) | Assumed: <N> inline [assumed: say otherwise] defaults
Open: <active fork IDs and topics> (<strikes>/2 backtrack resets used, if any)
```

If an answer or write-in invalidates an earlier premise, move the affected `Settled` entries back to `Open` and revisit only those forks. Cap premise resets at 2 strikes before rendering one direct tie-breaker surface instead of looping.

### Cross-Harness Fallback (When `a2ui-pane` Is Not Installed)

1. Use your host's structured question tool (`ask_question` or `AskUserQuestion`). Place `(Recommended)` on the first option and include `"Defer"` where appropriate.
2. If no question tool exists, present a numbered list in chat (`(Recommended)` first) and end your turn to wait for the user's reply.

______________________________________________________________________

## 2. Phase 2 Artifact Skeleton

Target `150` to `250` lines and at most `2,000` words. Keep prose crisp (`<= 20` words per sentence, zero em-dashes, one `Tradeoff Matrix`). State routine, reversible defaults inline tagged `[assumed: say otherwise]` rather than promoting them to `Open Decisions`:

```markdown
# <Topic>: Design Options

## At a Glance
- **Problem**: <1-2 measurable outcome or pain sentences grounded in code or metrics; zero solution mechanisms>
- **Hard Constraints**: <user-stated runtime, compatibility, or scope limits>
- **Recommended Direction**: <1-sentence synthesis and why it beats the simplest baseline>

## Current Baseline & Pain Points
<Short evidence summary with clickable [file](file:///...) links; pair with an inline Chart in chat when >= 4 shared-unit metrics exist. Tag routine inferred defaults inline as [assumed: say otherwise].>

## Contradiction Triage *(include only when given multiple competing docs, issues, or notes)*
- **Competing Paths**: <where input artifacts propose incompatible directions>
- **Blind Spots**: <failure modes, callers, or limits omitted across inputs>
- **Implicit Assumptions**: <unstated premises the inputs depend on>

## Constraints, Non-Goals & Anti-Goals
- <User-stated hard constraint or verified code invariant>
- <Routine default choice> `[assumed: say otherwise]`
- **Non-Goal**: <Out-of-scope work, or 1-2 sentence dismissal of an unviable strawman option>
- **Anti-Goal**: <Failure outcome that disqualifies a working design, such as silent fallbacks or manual migrations>

## Ranked Decision Criteria
1. **<Criterion 1>** > 2. **<Criterion 2>** > 3. **<Criterion 3>** *(derived before scoring; drives Tradeoff Matrix rows)*

## Candidate Approaches (or Orthogonal Design Axes)

### Simplest Baseline: <Extend Existing Module / Manual Step / Do Nothing>
<1 short paragraph stating the minimum viable baseline every heavier option must beat.>

### Option A: <Name> (Recommended)
<Core mechanism in 1 short paragraph + Mermaid flowchart or sequenceDiagram when structural flow differs.>

### Option B: <Name> *(Design-It-Twice structural contrast, e.g., deep/minimal vs. flexible/seamed)*
<Core mechanism in 1 short paragraph + diagram if structurally distinct.>

### Tradeoff Matrix
| Criterion (Ranked) | Simplest Baseline | Option A (Recommended) | Option B |
| :--- | :--- | :--- | :--- |
| 1. <Criterion 1> | ... | ... | ... |
| 2. <Criterion 2> | ... | ... | ... |
| 3. <Criterion 3> | ... | ... | ... |
| Deletion cost | ... | ... | ... |

## Panel Splits & Codebase Audit *(include only under --panel)*
Verified <X>/<Y> claims; <Z> falsified:
- `<panel-worker>` (`<Claim ID>`, `FALSIFIED`): assumed <false assumption> vs. verified `<file:line>` (<code truth>); dropped <dependent proposal point>. *(omit bullets when 0 falsified)*

| Trade-off | Competing Views | Resolution |
| :--- | :--- | :--- |
| <Disputed dimension> | `panel-<lens1>`: <position> vs. `panel-<lens2>` / `panel-red`: <counter-position> | <Feeds Open Decisions (D1) / Settled by verified code fact> |

## Open Decisions
<Brief pointer to the interactive A2UI surface rendered in this turn (or numbered fallback questions).>
```

______________________________________________________________________

## 3. Phase 3 Artifact Skeleton

When decisions arrive on an architectural or feature task, update the **existing** artifact in place as the final specification. Do not narrate earlier brainstorm turns, and omit empty optional sections instead of writing `"N/A"`:

```markdown
# <Topic>: Converged Design

## At a Glance
- **Problem**: <1-2 objective-isolated sentences>
- **Chosen Direction**: <Selected or combined hybrid architecture>
- **Status**: Converged; ready for `/plan`

## Settled Decisions
- **D1 (YYYY-MM-DD)**: Because <priority or hard constraint>, we chose <design>, accepting <specific downside>.
- **D2 (YYYY-MM-DD)**: Because <priority>, we chose <threshold or boundary>, accepting <specific downside>.

## Deferred Decisions *(omit section if none)*
- <Secondary choice intentionally left to `/plan` or the implementer, with guardrails if needed>

## Architecture & Component Design
<Unified prose and Mermaid architecture diagram(s). Tag brownfield deltas with `[NEW]`, `[MODIFIED]`, and plain existing nodes plus a 1-line legend; split Module View (packages/interfaces) from Runtime View (processes/sequences) when both are complex. Apply the Mechanism Rule (actor, inputs, exact contract/command, empty/missing/failure behavior, ranking tie-breaks). Cite primary sources for numbers or tag `[proposed]` / `[assumed: say otherwise]`.>

## Data Model & API Contracts
<Structural types, schemas, wire envelopes, or function signatures verified against `review-core/references/canonical_api_design.md`. Elide unchanged fields with `...` on modified structs, classes, schemas, or protos instead of copying full declarations.>

## Lifecycle, State & Failure Modes
<Mermaid stateDiagram-v2, sequenceDiagram, or erDiagram where applicable, plus timeout, retry, empty/missing input handling, tie-breaks, and rollback behavior.>
- *(Optional EARS invariants for stateful protocols)* `WHEN <event> WHILE <state>, <actor> SHALL <action>; IF <failure>, THEN <fallback>.`

## Rollout, Migration & Expected PR Split
1. <Wave / PR 1 scope>
2. <Wave / PR 2 scope>

## Verification & Benchmark Sketch
- <Key invariant, unit/golden test, and live smoke or benchmark check>

## Alternatives Considered
| Option | Summary | Why Not Selected | Revisit If |
| :--- | :--- | :--- | :--- |
| Simplest Baseline | <1-line summary> | <Criterion where it fell short> | <Trigger condition> |
| <Rejected Option B> | <1-line summary> | <Key tradeoff> | <Trigger condition> |

## Design Review Sign-Off *(omit section when `--post-design` was skipped)*
<Overall principle score, verdict, and 1 to 3 disproved pre-mortem or simpler-baseline hypotheses.>
```
