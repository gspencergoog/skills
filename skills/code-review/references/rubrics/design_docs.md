# Rubric: Design Docs

Use this rubric for design docs, RFCs, architecture decision records (ADRs), proposals, and implementation plans. A design doc exists to record a decision and the trade-offs behind it, so reviewers can challenge the decision before code exists. The main question is: is the decision sound, and is it justified against real alternatives?

Read [reviewing_documents.md](../reviewing_documents.md) first for the lenses, categories, evidence rules, and severity scale.

## Problem and Goals

- **Problem first**: The doc states the problem and its context before the solution. A problem statement that already assumes the solution ("we need a cache") hides the decision. Report it as `rationale-gap`.
- **Falsifiable goals**: Each goal could be shown to be unmet. "Improve performance" isn't falsifiable. "p95 under 200 ms at 1k QPS" is. Unfalsifiable goals are `unverifiable`.
- **Non-goals**: Non-goals name things a reader might reasonably expect to be in scope. A missing non-goal is a finding only when scope creep is a real risk. Name the risk.
- **Success metrics**: The doc says how anyone will know the design worked after launch.

## Decisions and Alternatives

- **Decisive stance**: The doc makes a recommendation. A doc that lists options without choosing one isn't ready for review, unless it says it is asking for input.
- **Rationale for each key decision**: A decision with real alternatives and no stated reason is `high` `rationale-gap`.
- **Real alternatives**: The alternatives are ones a competent engineer would actually consider, including "do nothing" and a simpler design. Straw-man alternatives, rejected with one line, are `rationale-gap`.
- **Trade-offs stated**: The doc says what the chosen design gives up. A design with no stated drawbacks usually hasn't been examined.
- **Problem/solution fit**: The design solves the stated problem, all of it, and not a different problem. Check each goal against the design. A goal the design doesn't meet is `critical` if the doc claims it does.

## Assumptions and Dependencies

- Assumptions are written down, especially about load, data size, ordering, availability of other systems, and team ownership.
- Each dependency on another team, service, or library names its owner and what happens if the dependency is late, changes, or fails.
- Assumptions that contradict code, data, or other docs are `drift` or `inconsistency`. Quote both sides.

## Cross-Cutting Concerns

Each of these needs numbers or a specific plan, not a promise. Unquantified claims ("scales well", "minimal overhead") are `medium` `unverifiable`.

- **Performance and scale**: expected load, growth, latency and throughput targets, and the bottleneck.
- **Security and privacy**: trust boundaries, authentication and authorization, sensitive data handling, and abuse cases.
- **Reliability**: failure modes of each component, what users see, and how the system recovers.
- **Observability**: what is logged and measured, and how an operator detects that something is wrong.
- **Cost**: compute, storage, and people, compared with the alternatives.

## Delivery

- **Migration**: how existing data, clients, and callers move to the new design, and what runs in parallel during the move.
- **Rollout and rollback**: staged rollout, the signal that stops it, and how to roll back, including data written by the new version.
- **Compatibility**: which existing behavior changes, and who is told.

## Consistency Within the Doc

- Diagrams match the prose: the same components, names, and data flow. A mismatch is `inconsistency`.
- The same component or concept has one name throughout.
- Open questions are listed in one place. A question that the design silently depends on is a finding. Name the part of the design at risk.

## Implementation Plans

For plans that break a design into phases or tasks:

- Each goal and requirement maps to at least one task, and each task maps back to a goal (`traceability`).
- Phase order respects dependencies. A phase that uses something a later phase creates is `infeasible`.
- Each phase has a verification step that could actually fail.
- File paths and commands the plan names exist, or are marked as new.
