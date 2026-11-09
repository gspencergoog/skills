# Architectural Focus Playbook Catalog

A focus playbook targets an architectural surface whose failure modes span multiple components, state transitions, or rollout phases. Evaluating a single section or file in isolation often misses these defects. During focus triage (or when `--focus <area>` is passed), `/design-review` selects matching catalog IDs from this index so the assigned reviewer loads the domain failure modes and hypothesis templates before inspecting the target. Score findings against the principles in [canonical_design_principles.md](../canonical_design_principles.md) and apply the evidence caps in [evidence_and_critique.md](../evidence_and_critique.md).

## How Triage Uses This Catalog

- Triage starts from the design target or codebase module, not from this table. Identify the highest-risk cross-cutting mechanism first, then match it to a catalog ID.
- Prefer a catalog ID over free text when one fits so findings carry consistent `- **Focus**: <id>` metadata across reviews.
- When a target introduces a domain mechanism not covered below, assign an ad hoc theme (`adhoc:<slug>`) and include an inline playbook block of at most `1 KB` (3 to 4 numbered failure modes with `Look at:` and `Settles it:` instructions plus 2 hypothesis templates) in the Reviewer Brief.
- Public signature ergonomics remain covered by the core principles and API checks; use a focus playbook when a mechanism needs domain-specific pre-mortem probes.

## Index

| id | Cues | Lane |
| :--- | :--- | :--- |
| [distributed-state-and-consistency](distributed-state-and-consistency.md) | Multi-step state mutations, caches, queues, background workers, retries, dual writes, or concurrent writers | A |
| [rollout-and-schema-evolution](rollout-and-schema-evolution.md) | Database migrations, JSON/Proto/SQLite schema changes, wire format updates, feature flags, or staged rollouts | A |
| [module-and-plugin-boundaries](module-and-plugin-boundaries.md) | Package splits, extension hooks, plugin registries, adapter layers, or cross-module dependency restructures | B |
| [dx-cli-and-ui-ergonomics](dx-cli-and-ui-ergonomics.md) | New CLI subcommands or flags, config schemas, interactive UI surfaces, error messages, or developer workflows | B |
| [agent-skill-and-prompt-design](agent-skill-and-prompt-design.md) | Agent skills (`SKILL.md`), subagent briefs, MCP tool contracts, prompt workflows, or context-staging scripts | B |
| [security-and-trust-boundaries](security-and-trust-boundaries.md) | Untrusted input parsing, subprocess/RPC execution, auth propagation, secret handling, or resource quotas | A |

## Playbook Contract

Every playbook in `references/focus_playbooks/<id>.md` stays at or below `75` lines so a reviewer reads it in a single call, and follows this layout:

```markdown
# <Playbook Title>

Playbook id: `<id>`. Primary principles: `<principle-id>`, `<principle-id>`. Lane for merges: A | B.

## Cues (for triage)

<2 to 4 bullets naming spec or codebase signals that trigger this playbook>

## Failure modes

1. <Defect pattern in one plain sentence without a bold inline header.>
   Look at: <sections, diagrams, or code paths to inspect>. Settles it: <cheapest verification step and its evidence tier>.
2. ...

## Hypothesis templates

- "<falsifiable architectural claim with <placeholders> for components, schemas, or files>"
```

## Verification Rule (`Failure modes checked`)

Failure modes are numbered so every reviewer assigned a focus playbook records `Failure modes checked: <numbers>` (for example, `Failure modes checked: 1, 2, 4, 5`) in its raw review output or `## Checked and Found Clean` bullets. During synthesis, the orchestrator verifies that each assigned playbook's relevant failure modes were explicitly checked against the target spec or codebase before finalizing the report in `<conversation-artifact-dir>/design_review_results.md`.
