# Complex & Migration Plan Template

Use this template for architectural refactors, cross-package migrations, multi-service systems, or breaking API changes that span multiple waves and parallel execution tracks.

## Two-Layer Prose Rule

Every plan has two prose layers checked by `validate_plan.py`:

- **Brief layer** (`## At a glance`, the `### PRs` or `### Commits` table, `## User Review Required`, `## Open Questions`, and `## Decisions`): written for the human reader under strict [write-prose](../../write-prose/SKILL.md) rules. The five `At a glance` bullets must total 180 words or fewer. Sentences across the brief sections must average 20 words or fewer.
- **Execution layer** (`## Context`, `## Design`, `## Migration & Execution Phases`, `## Verification Plan`, `## Landing`, and `## Rollback & Disaster Recovery Plan`): written for agents using tables and checklists. Follow `write-prose` vocabulary rules (plain words, no AI-isms, no em-dashes, and "Recommended" instead of "default"), while allowing longer sentences and dense code spans.

See [post_plan_ui.md](post_plan_ui.md) for rendering the post-plan worksheet, overview pane, progress board, and landing gate.

______________________________________________________________________

````markdown
# [Migration / Architecture Redesign Title]

## At a glance

- **Goal:** [One short sentence stating the target state and outcome.]
- **Shape:** [3 waves, 3 parallel tracks, and 4 PRs across 2 repositories.]
- **Decisions needed from you:** [1 (Q1 under Open Questions), or "none."]
- **Biggest risk:** [One short sentence naming the main migration risk and its mitigation.]
- **Not doing:** [Out-of-scope packages, legacy versions, or deferred follow-ups in one line.]

```mermaid
flowchart TD
    subgraph W1["Wave 1: Foundation & Shims [Sequential]"]
        S1["Shared contracts & compatibility adapters"]
    end
    subgraph W2["Wave 2: Subsystem Migration [Parallel]"]
        T2A["Track 2A: Package A Migration"]
        T2B["Track 2B: Package B Migration"]
        V2["Verifier Worker (1 per 2-4 Authors)"]
        T2A -.-> V2
        T2B -.-> V2
    end
    subgraph W3["Wave 3: Cutover & Cleanup [Sequential]"]
        S3["Barrel exports, legacy removal, & CHANGELOG"]
    end
    W1 --> W2 --> W3
```

### PRs

| # | Repo | PR / Commit | Companion PR | Wave |
| ---: | :--- | :--- | :--- | :--- |
| 1 | [repo-a] | `feat(contracts): add v2 adapter and shared types` | None | 1 |
| 2 | [repo-a] | `feat(pkg-a): migrate package A to v2 contract` | `[repo-b]#142` | 2 (2A) |
| 3 | [repo-a] | `feat(pkg-b): migrate package B to v2 contract` | None | 2 (2B) |
| 4 | [repo-a] | `refactor(exports): cut over public API and remove v1 shim` | `[repo-b]#143` | 3 |

## User Review Required

> [!WARNING]
> [State any breaking changes, protocol migrations, schema updates, or deprecations that require sign-off. Keep sentences under 20 words on average.]

## Open Questions

### Q1: [Short architecture or migration question?]
- (Recommended) A: [Recommended migration path.] Consequence: [Concrete effect of choosing A.]
- B: [Alternative migration path.] Consequence: [Concrete effect of choosing B.]

<!-- When no open questions remain, replace the Q<n> blocks above with:
None currently blocking.
-->

## Decisions

- **Q0 (YYYY-MM-DD):** A. [Recorded answer in the agent's own words.]

## Context

- **Repository Structure**: [Monorepo / Multi-package / Service layout]
- **Language / Runtime Versions**: [List active runtimes across packages]
- **Affected Packages / Modules**:
  - `package-a`: [Role and scope of changes]
  - `package-b`: [Role and scope of changes]
- **Compatibility Boundaries**: [APIs, network protocols, serialized data, or wire formats that must maintain backward compatibility]
- **Explicit Non-Goals**: [Out-of-scope packages, legacy versions, or deferred follow-ups]

## Design

[Describe the target architecture, modular boundaries, and wave-by-wave migration progression.]

### Orchestration & Parallel Verification Matrix

| Wave / Track | Execution Mode | Orchestrator / Role | Model Tier | Workspace Isolation | Owned Files / Scope | Verification Gate |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Wave 1** | Sequential | Coordinator / Single Agent | Claude Fable 5.1 High | Primary workspace or base worktree | Shared interfaces & shims | `[Wave 1 typecheck + compatibility suite]` |
| **Track 2A** | Parallel (Wave 2) | `/swarm` author (`pkg-a`) | Gemini Pro Next (`auto`) | `workspace: "share"` | `packages/pkg_a/**` | `[Track 2A unit tests + linter]` |
| **Track 2B** | Parallel (Wave 2) | `/swarm` author (`pkg-b`) | Gemini Pro Next (`auto`) | `workspace: "share"` | `packages/pkg_b/**` | `[Track 2B unit tests + linter]` |
| **Wave 2 Verifier** | Parallel Critic | `/swarm` `verifier` (`1` per `2-4` authors) | Claude Fable 5.1 High | `workspace: "inherit"` (read-only) | Audits Track 2A & 2B diffs and file bounds | `[Wave 2 cross-package integration gate]` |
| **Wave 3** | Sequential | Coordinator / Single Agent | Claude Fable 5.1 High | Primary workspace or merged branch | Barrel exports, `CHANGELOG.md`, legacy removal | `[Full repo regression + code-review]` |

### Interface & Schema Specifications

```[language]
// New interfaces, protocol buffers, types, or schemas
```

## Migration & Execution Phases

### Wave 1: Compatibility Shims & Shared Contracts [Sequential]

- **Model tier**: [e.g. Claude Fable 5.1 High]

Introduce non-breaking abstractions, backward-compatibility shims, and shared interfaces so Wave 2 tracks can run in parallel without editing shared files.

- [ ] [NEW] [shim.ext](file:///absolute/path/to/shim.ext)
  - Introduce adapter layer and shared interface definitions
- [ ] [MODIFY] [existing_contract.ext](file:///absolute/path/to/existing_contract.ext)
  - Route base contract through adapter

**Wave 1 Verification**: `[Commands to run compatibility and regression suites]`

______________________________________________________________________

### Wave 2: Core Subsystem Migration [Parallel]

Implement the new subsystems and migrate independent packages or modules concurrently across strictly disjoint file sets.

#### Track 2A: [Package A / Subsystem A]

- **Depends on**: Wave 1
- **Model tier**: [e.g. Gemini Pro Next (`auto`)]
- **Owns**: `path/to/subsystem_a.ext`, `tests/test_subsystem_a.ext`
- **Must Not Touch**: `path/to/subsystem_b.ext`, `path/to/exports.ext`, `CHANGELOG.md`

- [ ] [NEW] [subsystem_a.ext](file:///absolute/path/to/subsystem_a.ext)
  - Implement Subsystem A logic against Wave 1 contract
- [ ] [MODIFY] [test_subsystem_a.ext](file:///absolute/path/to/tests/test_subsystem_a.ext)
  - Add unit and regression tests for Subsystem A

**Track 2A Verification**: `[Scoped test & static analysis command for Track 2A]`

#### Track 2B: [Package B / Subsystem B]

- **Depends on**: Wave 1
- **Model tier**: [e.g. Gemini Pro Next (`auto`)]
- **Owns**: `path/to/subsystem_b.ext`, `tests/test_subsystem_b.ext`
- **Must Not Touch**: `path/to/subsystem_a.ext`, `path/to/exports.ext`, `CHANGELOG.md`

- [ ] [MODIFY] [subsystem_b.ext](file:///absolute/path/to/subsystem_b.ext)
  - Switch Package B consumers to Wave 1 contract
- [ ] [MODIFY] [test_subsystem_b.ext](file:///absolute/path/to/tests/test_subsystem_b.ext)
  - Update Package B test suite

**Track 2B Verification**: `[Scoped test & static analysis command for Track 2B]`

**Wave 2 Verifier & Barrier Gate**:
- Peer `verifier` confirms zero out-of-scope file edits, runs Track 2A and Track 2B test suites, and checks cognitive complexity ($\le 15$).
- Wave barrier command: `[Cross-package integration test command before Wave 3 begins]`

______________________________________________________________________

### Wave 3: Cutover, Chokepoint Wiring & Cleanup [Sequential]

- **Model tier**: [e.g. Claude Fable 5.1 High]

Wire public barrel exports, remove legacy shims, and update documentation and changelogs once all Wave 2 tracks have merged.

- [ ] [DELETE] [deprecated_file.ext](file:///absolute/path/to/deprecated_file.ext)
  - Remove legacy implementation
- [ ] [MODIFY] [exports.ext](file:///absolute/path/to/exports.ext)
  - Export finalized public API surface

**Wave 3 Verification**: `[Commands to verify zero dangling references and full suite pass]`

______________________________________________________________________

## Verification Plan

### Automated Regression & Integration Tests

- Package A tests: `[test command]`
- Package B tests: `[test command]`
- Cross-package end-to-end suite: `[e2e command]`
- Static analysis & lint: `[lint command]`

### Edge Cases & Failure Scenarios

1. [Failure scenario 1, e.g. network partition during transition]
2. [Failure scenario 2, e.g. mixed version data serialization]
3. [Failure scenario 3, e.g. concurrent schema migrations]

### Performance & Complexity Audit

- Benchmark command: `[benchmark command if applicable]`
- Complexity check: Verify that new functions comply with cognitive complexity limits ($\le 15$).

## Landing

1. Run `/code-review --fast` across each package diff and fix blocker or major findings.
2. Commit changes with `commit-changes` following the `### PRs` table order.
3. Open stacked or companion pull requests with `write-pr-description` or `ship-it`, linking each companion PR in the description.

## Rollback & Disaster Recovery Plan

1. **Rollback Trigger**: [Specific error rate, latency spike, or test failure that requires aborting the rollout]
2. **Rollback Steps**: [Exact steps and commands to revert to the previous stable state without data loss]
````
