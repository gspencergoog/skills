# Reviewing Pre-Code Design Documents

Use this rubric when `prepare_design_context.py` classifies the target as `mode: "design-doc"` (design specs, RFCs, ADRs, proposals, and JSON Schema contracts). Apply the scoring anchors in [canonical_design_principles.md](canonical_design_principles.md) and the evidence tiers and severity caps in [evidence.md](../../review-core/references/evidence.md) and [evidence_and_critique.md](evidence_and_critique.md).

## Table of Contents

- [Staged Context and Pre-Checks](#staged-context-and-pre-checks)
- [Problem Isolation and Falsifiable Scope](#problem-isolation-and-falsifiable-scope)
- [Rational Decision Formula and Simpler Baseline](#rational-decision-formula-and-simpler-baseline)
- [Multi-View Coherence and Mermaid Alignment](#multi-view-coherence-and-mermaid-alignment)
- [One-Way Door Scrutiny](#one-way-door-scrutiny)
- [Goldfish Self-Containment and Downstream Impact](#goldfish-self-containment-and-downstream-impact)

## Staged Context and Pre-Checks

Start from `<conversation-scratch-dir>/design_context.json` produced by `prepare_design_context.py`:
- Inspect `door_classification.overall` (`one-way` or `two-way`), matched `signals`, `markdown.broken_links`, `markdown.mermaid_count`, and `markdown.settled_decisions`.
- Report any entry in `markdown.broken_links` as `Executed` evidence of a broken target (`high` for missing contracts or code targets, `medium` for reference links).
- For large specs (`> 500` lines) staged under `markdown.staged_dir`, read the `md_chunk_*.md` files in sequence. Query `md_sections.jsonl`, `md_tables.jsonl`, and `md_requirements.jsonl` via `staged_md_macros.sql` to inventory RFC 2119 keywords (`MUST`, `SHOULD`) and option tables.

## Problem Isolation and Falsifiable Scope

Evaluate `problem-scope` before critiquing mechanisms:
- Decouple the problem from the proposed mechanism. The problem statement must describe user pain, bottlenecks, or failure rates without assuming the solution ("p95 lookup exceeds 400 ms", not "add a daemon"). Flag solution-smuggled problem statements as `high` or `medium` under `problem-scope`.
- Require falsifiable goals. Every goal and normative claim must map to a pass/fail test or numeric threshold. Flag subjective targets ("fast", "flexible", "clean", "minimal overhead") that a test or benchmark cannot disprove.
- Check `Non-Goals` for realistic boundary guards. Flag missing non-goals only when adjacent scope creep is a concrete risk, and state that risk in `Why`.

## Rational Decision Formula and Simpler Baseline

Evaluate `tradeoffs-simplicity` against SEI ATAM tradeoff rules and the Settled-Decision boundary in [evidence_and_critique.md](evidence_and_critique.md):
- Require the rational decision formula for major choices. Each decision must state *"Priority X led to choice Y, accepting cost Z."* Flag decisions that omit downsides.
- Apply the **Simpler-Baseline Test** to the alternatives section. Check that the spec compares its design against a low-complexity baseline (doing nothing, extending a module, or using stdlib primitives) rather than strawman options.
- Respect locked `Settled Decisions` (`D1..Dn`) from the spec or `--post-design` briefing packet. Never re-litigate a settled tradeoff on stylistic preference. Challenge it only with `Read`, `Executed`, or line-cited `Deduction` proof of a `critical` or `high` feasibility, compatibility, or security blocker.

## Multi-View Coherence and Mermaid Alignment

Evaluate `ergonomics-coherence`, `module-depth`, and `boundaries-state` across three architectural views (Kruchten 4+1 and C4), recording one concrete bullet per view in `## View & Boundary Assessment`:
- In the **Module view**, check static package boundaries, narrow public interfaces, acyclic dependency direction, and information hiding.
- In the **Runtime view**, trace end-to-end execution sequences, concurrency controls, queue bounds, timeouts, and partial-failure recovery.
- In the **State and allocation view**, check persisted schemas, single-writer state ownership, cache invalidation lifecycles, and process or filesystem boundaries.
- Audit **Mermaid-to-schema and prose alignment** across every `flowchart`, `sequenceDiagram`, and `stateDiagram-v2` block. Quote both `path:line` locations under `Read` evidence when a diagram disagrees with prose or schemas on component names, arrow direction, cardinality, fields, or error transitions.

## One-Way Door Scrutiny

Calibrate scrutiny and `Door` tags (`one-way` vs. `two-way`) under `evolution-fitness` and `resilience-ops`:
- When `door_classification.overall` is `one-way` (`persisted-schema`, `wire-protocol`, `public-contract`, `trust-boundary`, `cross-repo-split`, or `large-spec`), require version identifiers, expand-contract migration steps, and mixed-version reader/writer skew rules.
- Check that the rollback plan handles data or state written by the new version before rollback so older readers are not poisoned.
- Check that irreversible schemas or external protocols sit behind a narrow adapter and pair with a runnable fitness test or schema validator.

## Goldfish Self-Containment and Downstream Impact

Check that the document can be implemented safely inside the target repository:
- Run the **Goldfish Test** (`ergonomics-coherence`): assume an engineer or coding agent has only this document and linked repository files, with zero chat history. Flag silent assumptions, unstated preconditions, undefined error states, or unresolved questions that a core path depends on.
- Run a **Downstream Impact Pass** across up to 10 existing repository files (linked code modules, callers, schemas, manifests, or sibling skills). Check that every referenced file and symbol exists on disk and that the spec lists all downstream files needing updates.
