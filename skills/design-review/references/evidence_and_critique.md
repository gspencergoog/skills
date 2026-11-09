# Evidence Tiers, Falsification, and Output Formats

Optimize for finding survival rate over finding count. Every architectural claim must trace to primary-source evidence gathered in the current session, carry a severity capped by its evidence tier, and survive falsification before synthesis.

## Table of Contents

- [Evidence Tiers and Severity Caps](#evidence-tiers-and-severity-caps)
- [Design-Specific Critique Rules](#design-specific-critique-rules)
- [Falsification Pass](#falsification-pass)
- [Report Markdown Contract](#report-markdown-contract)

## Evidence Tiers and Severity Caps

| Tier | Meaning in `/design-review` | Allowed Severities |
| :--- | :--- | :--- |
| `Executed` | Output from a command run this session (`sem`, `cognitive_complexity.py`, `prepare_design_context.py`, a validator, or a read-only query). | `critical`, `high`, `medium`, `low` |
| `Read` | Exact text or code opened from disk this session, quoted with `path:line`. Contradictions must quote both sides. | `critical`, `high`, `medium`, `low` |
| `Fetched` | Verbatim text retrieved from an official specification or reference URL this session (at most 3 network fetches), cited by URL. | `critical`, `high`, `medium`, `low` |
| `Deduction` | Step-by-step state trace, dependency trace, or back-of-envelope capacity arithmetic derived from exact `path:line` citations. | `critical`, `high` (with line citations), `medium`, `low` |
| `Speculation` | Unmeasured scaling hunch, incomplete call trace, or external unknown that could not be verified within read budget. | `medium`, `low`, or `Question` (`Q1..Q3`) |
| `Recalled` | Memory of how a library, protocol, or repo file works without reading it this session. | Forbidden. Verify on disk or drop. |

## Design-Specific Critique Rules

1. **The Settled-Decision Boundary**: When the target spec or `Design Briefing Packet` lists `Settled Decisions` (`D1...`), treat those choices as fixed user constraints. Never re-litigate a settled tradeoff on stylistic preference. Challenge a settled decision only when `Read`, `Executed`, or line-cited `Deduction` proves a `critical` or `high` feasibility, compatibility, or security blocker.
2. **The Simpler-Baseline Test**: Before filing an abstraction recommendation, verify it reduces net complexity. Flag proposed layers, daemons, or plugin hooks when extending an existing module or function meets the same falsifiable goal with fewer moving parts.
3. **Subjective Objection Filter**: Drop vague advice ("add monitoring", "consider caching", "may not scale") unless `Why` names a concrete failure mode, corrupted invariant, or change-amplification path.
4. **Cross-View Search Before Incompleteness**: Before claiming a state, error, or migration step is undefined, check all sections, tables, Mermaid diagrams, and linked repository files.
5. **Actionable Counter-Design**: Every finding's `Suggestion` provides a concrete boundary change, state rule, replacement spec text/diagram, or runnable fitness test sketch (using at most one fenced code block).

## Falsification Pass

Run four checks on every candidate finding before emitting it:

1. **Cheapest disproof**: Open the cited target file, caller, or schema on disk (`<= 25` extra files read budget) to check whether an existing guard or section already handles the case.
2. **Historical context (`packrat`)**: Check whether prior commits or review threads intentionally chose the current structure (Chesterton's fence) before calling an unfamiliar boundary a flaw.
3. **Door calibration**: Confirm `Door` (`one-way` vs. `two-way`) is accurate; avoid inflating reversible internal organization choices above `medium`.
4. **Prose discipline**: Keep visible finding prose (`Body` + `Why` + non-fenced `Suggestion`) within `120` words and `6` sentences (`<= 30` words per sentence, `PCC <= 5`, zero banned AI words, zero em-dashes). Move deeper traces to `- **Details**:` (`<= 150` words).

## Report Markdown Contract

`lint_design_review.py` validates these exact seven `##` sections in order:

1. `## Summary` (ending with `Verdict: <Ready for /plan | Revisions Recommended | Architectural Blockers>`)
2. `## Principle Scorecard` (pipe table with columns `| Principle | Score | Assessment |` covering all 7 `Principle` IDs, scored `0/10`..`10/10` or `N/A`)
3. `## View & Boundary Assessment` (concise bullets covering Module, Runtime, and State/Allocation coherence, One-Way Doors, and downstream files)
4. `## Findings (Ordered by Severity)` (`None.` when clean, or sequential `### C1.`, `### H1.`, `### M1.`, `### L1.` blocks)
5. `## Open Design Questions` (`None.` when none, or up to 3 `### Q1.`..`### Q3.` blocks)
6. `## Checked and Found Clean` (`None.` when none, or up to 10 `- <hypothesis>: \`<Executed | Read | Fetched | Deduction>\`: <proof>` bullets)
7. `## Recommended Next Steps`

### Finding and Question Field Layout

```markdown
### H1. <short architectural claim>
- **Target**: `path/to/file:42` (or `path/to/spec.md:88 (## Data Model)`)
- **Door**: `one-way | two-way`
- **Severity**: `critical | high | medium | low`
- **Principle**: `<one of the 7 canonical principle IDs>`
- **Focus**: `<playbook-id>` (optional; include when a focus playbook applies)
- **Evidence**: `<Executed | Read | Fetched | Deduction | Speculation>`: <command output, exact quote with path:line, or line-cited trace>
- **Body**: <1 to 3 concise sentences stating the structural flaw without restating the spec>
- **Why**: <1 concise sentence naming what breaks, drifts, or amplifies change if built as-is>
- **Suggestion**: <concrete counter-design, replacement text, or fitness test with at most one code fence>
- **Details**: <optional root-cause trace or rejected alternatives, <= 150 words>

### Q1. <high-stakes open question?>
- **Target**: `path/to/file:15`
- **Evidence**: `<Speculation | Deduction>`: <what was checked and where the trace stopped>
- **Why**: <1 concise sentence stating what breaks if the assumption fails>
- **To settle**: <the exact file, benchmark, or stakeholder check that resolves it>
```
