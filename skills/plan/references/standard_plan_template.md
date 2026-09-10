# Standard Implementation Plan Template

Use this template for standard multi-file features, bug fixes, or refactors.

______________________________________________________________________

````markdown
# [Feature / Task Title]

[Provide a concise description of the task, background context, and the engineering objective.]

## User Review Required

> [!IMPORTANT]
> [Highlight any critical architectural decisions, potential breaking changes, or trade-offs that need user attention.]

## Open Questions

> [!NOTE]
> [List any open questions, ambiguous edge cases, or dependencies requiring clarification. If none, state "None currently blocking."]

## Codebase Context & Discovered Patterns

- **Language / Runtime**: [e.g. Python 3.11, Dart 3.7 / Flutter, TypeScript 5.4]
- **Build / Test Tooling**: [e.g. `pytest`, `flutter test`, `npm test`, `cargo test`]
- **Lint / Static Analysis**: [e.g. `ruff`, `dart analyze`, `eslint`, `mypy`]
- **Existing Abstractions to Reuse**:
  - `[Symbol / Module 1]`: [Description of how this existing code satisfies part of the requirement]
  - `[Symbol / Module 2]`: [Description of existing utility/helper to follow or reuse]
- **Key Constraints**: [e.g. backward compatibility, memory limits, isolation rules]

## Architecture & Technical Design

[Explain the proposed approach, data model changes, component interactions, and interface definitions.]

```mermaid
flowchart TD
    [Component A] --> [Component B]
    [Component B] --> [Component C]
````

### Key Interfaces & Signatures

```[language]
// Outline key interfaces, type definitions, or method signatures
```

## Phased Implementation Plan

### Phase 1: [Foundation / Types / Models]

Description of the initial setup, shared data structures, or interface definitions.

- [ ] #### [NEW] \[path/to/new_file.ext\](file:///absolute/path/to/new_file.ext)
  - Define `TypeName` struct/class with fields `...`
  - Implement validation logic for inputs
- [ ] #### [MODIFY] \[path/to/existing_file.ext\](file:///absolute/path/to/existing_file.ext)
  - Add export for new types
  - Update imports

**Phase 1 Verification**: \[Command or check to verify Phase 1, e.g. `npm run typecheck` or `pytest tests/unit/test_types.py`\]

______________________________________________________________________

### Phase 2: [Core Logic / Implementation]

Description of the primary operational changes.

- [ ] #### [MODIFY] \[path/to/service.ext\](file:///absolute/path/to/service.ext)
  - Implement `process_request()` using `[reused_helper]`
  - Add error handling for edge cases
- [ ] #### [NEW] \[path/to/handler.ext\](file:///absolute/path/to/handler.ext)
  - Implement request dispatching

**Phase 2 Verification**: \[Command to verify Phase 2, e.g. `cargo test --lib core_logic`\]

______________________________________________________________________

### Phase 3: [Integration & UI / CLI Surface]

Description of wiring into entry points, CLI commands, or external callers.

- [ ] #### [MODIFY] \[path/to/main.ext\](file:///absolute/path/to/main.ext)
  - Register new handler in dispatcher
  - Wire command-line arguments

**Phase 3 Verification**: \[Command to run integration test suite, e.g. `pytest tests/integration/`\]

______________________________________________________________________

## Verification Plan

### Automated Tests

- Run test suite: `[exact test command, e.g. pytest -v tests/test_feature.py]`
- Run linter / typechecker: `[exact command, e.g. ruff check .]`
- Run full regression suite: `[exact command, e.g. pytest]`

### Edge Cases to Validate

1. [Edge case 1, e.g. empty input / null values / timeout handling]
2. [Edge case 2, e.g. concurrent access / duplicate IDs]
3. [Edge case 3, e.g. error propagation when downstream service is unavailable]

### Manual Verification

1. [Step 1: Run application command with sample arguments]
2. [Step 2: Inspect output or verify expected state change]

## Rollback & Risk Mitigation

- **Breaking Changes**: [None / Details of breaking API changes and migration shim]
- **Rollback Strategy**: [How to cleanly revert if issues arise in production or review]

```

```
