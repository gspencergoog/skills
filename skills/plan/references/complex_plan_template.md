# Complex / Migration Implementation Plan Template

Use this template for major architectural refactors, cross-package migrations, multi-service systems, or breaking API changes.

______________________________________________________________________

````markdown
# [Migration / Architecture Redesign Title]

[High-level overview of the redesign, motivation, target state, and expected benefits.]

## User Review Required

> [!WARNING]
> [Critical breaking changes, protocol migrations, schema updates, or deprecations that require sign-off.]

## Open Questions

> [!IMPORTANT]
> [List any open architecture questions or decisions. Recommend `/grill-me` if consensus is not reached.]

## Existing System Context & Constraints

- **Repository Structure**: [Monorepo / Multi-package / Service layout]
- **Language / Runtime Versions**: [List all active runtimes across packages]
- **Affected Packages / Modules**:
  - `package-a`: [Role and scope of changes]
  - `package-b`: [Role and scope of changes]
- **Compatibility Boundaries**: [APIs, network protocols, serialized data, wire formats that must maintain backward compatibility]

## Target Architecture

[Describe the target architecture, modular boundaries, and migration progression.]

```mermaid
flowchart TD
    subgraph Current State
        A[Old Service] --> B[(Shared Storage)]
    end
    subgraph Transition Phase
        A --> C[Compatibility Shim]
        C --> D[New Service]
        D --> B
    end
    subgraph Final Target
        D --> B
    end
````

### Interface & Schema Specifications

```[language]
// New interfaces, protocol buffers, types, or schemas
```

## Migration & Execution Phases

### Phase 1: Dual-Write / Compatibility Shims

[Introduce non-breaking abstractions, backward-compatibility shims, and shared interfaces without breaking existing callers.]

- [ ] #### [NEW] \[path/to/shim.ext\](file:///absolute/path/to/shim.ext)
  - Introduce adapter layer
- [ ] #### [MODIFY] \[path/to/existing_caller.ext\](file:///absolute/path/to/existing_caller.ext)
  - Route through adapter

**Phase 1 Verification**: [Commands to run compatibility and regression suites]

______________________________________________________________________

### Phase 2: Core Migration

[Implement the new subsystems, migrate core logic, and switch default routing.]

- [ ] #### [NEW] \[path/to/new_subsystem.ext\](file:///absolute/path/to/new_subsystem.ext)
  - Implement new subsystem logic
- [ ] #### [MODIFY] \[path/to/consumer.ext\](file:///absolute/path/to/consumer.ext)
  - Switch consumers to new subsystem

**Phase 2 Verification**: [Commands to test new subsystem and integration suites]

______________________________________________________________________

### Phase 3: Cleanup & Deprecation

[Remove legacy code, drop deprecated shims, update documentation.]

- [ ] #### [DELETE] \[path/to/deprecated_file.ext\](file:///absolute/path/to/deprecated_file.ext)
  - Remove deprecated legacy adapter
- [ ] #### [MODIFY] \[path/to/exports.ext\](file:///absolute/path/to/exports.ext)
  - Clean up public API surface

**Phase 3 Verification**: [Commands to verify no dangling references remain]

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
- Complexity check: Verify that new functions comply with cognitive complexity limits ($\\le 15$).

## Rollback & Disaster Recovery Plan

1. **Rollback Trigger**: [Specific error rate, latency spike, or test failure that mandates aborting the rollout]
2. **Rollback Steps**: [Exact steps/commands to revert back to the previous stable state without data loss]

```

```
