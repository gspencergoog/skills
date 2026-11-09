# Canonical Software Design Principles

This reference defines the seven orthogonal principles scored during a `/design-review` pass (`0` to `10`, or `N/A` when inapplicable). Use the exact backticked `Principle` IDs below in scorecard tables and finding metadata.

## Table of Contents

- [Scoring Scale](#scoring-scale)
- [1. Problem Isolation & Falsifiable Scope (`problem-scope`)](#1-problem-isolation-falsifiable-scope-problem-scope)
- [2. Module Depth & Information Hiding (`module-depth`)](#2-module-depth-information-hiding-module-depth)
- [3. Dependency & State Boundaries (`boundaries-state`)](#3-dependency-state-boundaries-boundaries-state)
- [4. Decisive Tradeoffs & Simpler Baseline (`tradeoffs-simplicity`)](#4-decisive-tradeoffs-simpler-baseline-tradeoffs-simplicity)
- [5. Failure Modes & Operational Resilience (`resilience-ops`)](#5-failure-modes-operational-resilience-resilience-ops)
- [6. Evolutionary Design & Reversibility (`evolution-fitness`)](#6-evolutionary-design-reversibility-evolution-fitness)
- [7. Cognitive Ergonomics & View Coherence (`ergonomics-coherence`)](#7-cognitive-ergonomics-view-coherence-ergonomics-coherence)

## Scoring Scale

| Score | Band | Meaning |
| :--- | :--- | :--- |
| `9-10` | Exemplary | Clear contracts, falsifiable invariants, explicit tradeoffs, and zero structural gaps. |
| `7-8` | Sound | Solid boundaries with only minor `low` or `medium` refinements needed. |
| `4-6` | Partial | Notable ambiguity, leaky boundaries, unquantified claims, or missing failure paths (`high` findings). |
| `0-3` | Inadequate | Missing, contradictory, or unworkable design (`critical` blockers). |
| `N/A` | Inapplicable | Target genuinely lacks this surface (for example, `problem-scope` on a pure in-code module audit with no spec). Excluded from `overall.max`. |

## 1. Problem Isolation & Falsifiable Scope (`problem-scope`)

Separate the problem from the proposed mechanism before evaluating solutions.
- State the user or system pain point without embedding the implementation choice ("p95 query latency exceeds 400 ms", not "we need a Redis cache").
- Express goals as falsifiable pass/fail conditions with numbers or concrete observable behaviors rather than subjective adjectives ("fast", "clean", "easy").
- Name at least two non-obvious `Non-Goals` when adjacent scope creep is plausible.

## 2. Module Depth & Information Hiding (`module-depth`)

Concentrate functionality behind a small, simple interface (Ousterhout and Parnas).
- Favor **deep modules** whose public entry points are much simpler than the logic they encapsulate.
- Flag **shallow modules**, **pass-through functions** that forward arguments unchanged across layers, and **overexposed configs** that force callers to understand rare internals.
- Eliminate **information leakage** where two modules hardcode the same file format, SQL layout, or wire detail, creating change amplification when that detail shifts.
- Avoid **temporal decomposition** (splitting modules solely by execution step order when both steps share the same secret knowledge).

## 3. Dependency & State Boundaries (`boundaries-state`)

Keep inter-module coupling acyclic and state ownership unambiguous.
- Module and package dependencies must form a directed acyclic graph (DAG) flowing toward stable abstractions or domain cores, with zero import cycles.
- Enforce the **single-writer rule**: every persisted table, cache, or mutable state store has one owning module accountable for mutations.
- Inject configuration and collaborators explicitly via parameters or constructors rather than hidden singletons, mutable registries, or ambient environment reads.

## 4. Decisive Tradeoffs & Simpler Baseline (`tradeoffs-simplicity`)

Justify architectural commitments against real alternatives (SEI ATAM and YAGNI).
- Every major choice states a ranked priority, the chosen design, and the accepted downside (*"Priority X led to choice Y, accepting cost Z"*).
- Compare against genuine contenders, always including a **simpler baseline** (extending an existing module, using standard library primitives, or doing nothing) rather than strawman options.
- Reject speculative generality, unused plugin hooks, and abstractions built for hypothetical future callers.

## 5. Failure Modes & Operational Resilience (`resilience-ops`)

Assume dependencies fail, inputs are malformed, and traffic spikes post-launch (DRBFM and Nygard).
- Bound every external call, queue, batch, and retry loop with explicit timeouts, size limits, idempotency keys, and backoff or circuit-breaking behavior.
- Define partial-failure and crash recovery when an operation spans multiple files, processes, or network hops.
- Specify concrete diagnostic signals (structured errors, counters, logs, or health checks) and a fast rollback path that does not poison persisted state for older readers.

## 6. Evolutionary Design & Reversibility (`evolution-fitness`)

Isolate irreversible commitments and automate architectural governance (Fowler).
- Classify **One-Way Doors** (persisted schemas, wire protocols, public CLI/SDK contracts, security boundaries) and isolate them behind narrow versioned adapters.
- Account for mixed-version skew during staged rollouts and expand-contract schema migrations.
- Pair structural invariants with runnable **architecture fitness functions** (import-cycle unit tests, schema validators, budget checks, or linter rules) so boundaries do not erode after merge.

## 7. Cognitive Ergonomics & View Coherence (`ergonomics-coherence`)

Keep every representation consistent and minimize human or agent mental load (ISO 42010 and Green & Petre).
- Maintain **cross-view coherence**: prose, Mermaid diagrams (`flowchart`, `sequenceDiagram`, `stateDiagram-v2`), schemas, and type signatures must agree on component names, cardinality, and direction of flow.
- Check developer and user surfaces for closeness of domain mapping, low viscosity (small intent changes require small edits), explicit preconditions, and `--dry-run` provisionality.
- Satisfy the **Goldfish Test**: an engineer or agent with zero access to conversation history can implement or extend the design without guessing silent assumptions.
