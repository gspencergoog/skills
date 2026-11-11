# Reviewing Code Architecture and Spec Conformance

Use this rubric when `prepare_design_context.py` classifies the target as `mode: "code-architecture"` (existing directories, packages, or modules) or `mode: "conformance"` (`--against <governing-spec.md>`). Apply the principles in [canonical_design_principles.md](canonical_design_principles.md) and the evidence rules in [evidence.md](../../review-core/references/evidence.md) and [evidence_and_critique.md](evidence_and_critique.md).

## Table of Contents

- [Staged Code Metrics and Applicability](#staged-code-metrics-and-applicability)
- [Ousterhout Module Depth and Information Hiding](#ousterhout-module-depth-and-information-hiding)
- [Dependency DAG, Cycles, and Single-Writer State](#dependency-dag-cycles-and-single-writer-state)
- [Complexity Hotspot and Churn Correlation](#complexity-hotspot-and-churn-correlation)
- [Spec Conformance Checks](#spec-conformance-checks)
- [Runnable Fitness Tests for Architecture](#runnable-fitness-tests-for-architecture)

## Staged Code Metrics and Applicability

Start from `<conversation-scratch-dir>/design_context.json` under `code_metrics`:
- Inspect `file_count`, `dependency_cycles`, `high_complexity_functions`, `packrat_hotspots`, `tools_used`, and `skipped_probes`.
- On a pure `code-architecture` review with no governing spec, mark `problem-scope` as `N/A` in `## Principle Scorecard`. `lint_design_review.py` excludes `N/A` from `overall.max` and omits it from `scorecard.rows[]` in `--export-json`. In `conformance` mode (`--against`), score all seven principles.
- When `sem` appears in `tools_used`, run `sem` read-only queries to inspect symbol dependencies and blast radius across files. When `sem` is in `skipped_probes`, trace imports and call sites with `rg` and `fd`.

## Ousterhout Module Depth and Information Hiding

Evaluate `module-depth` by comparing each module's public entry points against the complexity they hide:
- Flag **shallow modules** whose public signatures, configuration records, or required setup sequences are nearly as complex as their internal implementation.
- Flag **pass-through functions and layers** that forward parameters unchanged across call frames without enforcing a domain invariant, policy boundary, or data transformation.
- Flag **information leakage** when two or more modules hardcode the same file format, SQL layout, wire field, or regex rule instead of sharing one contract.
- Flag **temporal decomposition** when code is split across modules solely by execution order (such as `load_state.py`, `parse_state.py`, `save_state.py`) while sharing internal representation secrets.

## Dependency DAG, Cycles, and Single-Writer State

Evaluate `boundaries-state` across module imports and mutable data stores:
- Treat any circular dependency chain in `code_metrics.dependency_cycles` as `Executed` evidence of a DAG violation (`high` or `critical` under `boundaries-state`). Check non-Python packages for import cycles or upward layer imports (domain core importing CLI, UI, or transport adapters) using `sem` or `rg`.
- Enforce the **single-writer rule** for every persisted table, state file, cache, or mutable registry. Flag designs where multiple modules mutate the same store directly without routing through one owning module.
- Check that configuration, clocks, file paths, and external clients enter through explicit parameters or constructors rather than hidden singletons, mutable globals, or scattered environment lookups.

## Complexity Hotspot and Churn Correlation

Combine `code_metrics.high_complexity_functions` (Cognitive Complexity `> 15`) with `code_metrics.packrat_hotspots`:
- High cognitive complexity is an architectural flaw when one function mixes responsibilities (parsing, domain rules, retries, formatting) or aligns with `packrat_hotspots` churn.
- Report the exact `cognitive_complexity.py` score from `design_context.json` under `Executed` evidence and propose a clean responsibility split under `module-depth` or `resilience-ops`.
- Check historical context via `packrat` (Chesterton's fence) before flagging an unusual boundary so deliberate compatibility or security guards are not misclassified as tech debt.

## Spec Conformance Checks

When `mode` is `"conformance"` (`--against <governing-spec.md>`), audit bidirectional drift between the staged specification and the code:
- Check **spec-to-code coverage** across every RFC 2119 requirement (`MUST`, `SHOULD`), state machine transition, timeout, and error contract in the spec. Flag missing invariants, unvalidated inputs, or silent fallback behaviors that violate the spec.
- Check **code-to-spec fidelity** for unapproved public entry points, extra configuration knobs, direct state mutations, or new dependencies that contradict `Settled Decisions` or `Non-Goals`.
- Anchor every conformance finding with `Read` evidence quoting both `spec_path:line` and `code_path:line`.

## Runnable Fitness Tests for Architecture

Under `evolution-fitness`, prevent boundary erosion by pairing structural findings with a runnable **architecture fitness function**:
- When a module boundary, dependency DAG, or schema contract lacks automated enforcement, provide a `unittest` check, AST import-cycle assertion, or schema test inside `- **Suggestion**:`. Use at most one fenced code block.
- Make sure the suggested fitness test is fast, deterministic, and runs on existing repository test runners without adding third-party dependencies.
