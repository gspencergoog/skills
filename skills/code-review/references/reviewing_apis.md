# Reviewing API Changes

This reference document guides reviewers in evaluating modified and newly introduced public APIs, signatures, and configuration surfaces during a code review. It adapts the core principles from the [Canonical API Design Principles](../../api-review/references/canonical_api_design.md) reference in the `api-review` skill to incremental changesets.

Under `--panel`, API review belongs to **Lane B** (safety and maintainability), while Lane A verifies that tests adequately cover the public contracts.

---

## The Dual-Scope Model

An effective API review requires a deliberate balance between investigation depth and reporting restraint.

### 1. Wide Investigation Scope (Context Gathering)

To evaluate an API change accurately, you cannot view diff hunks in isolation. You are expected to inspect related files in the repository:
- Read sibling methods in the target class or module to understand existing naming, parameter patterns, and return types.
- Read parent interfaces, base classes, or contracts that the modified code implements or extends.
- Search for call sites across the codebase using search tools to see how consumers invoke similar interfaces.
- Inspect error types and status handling across the module to verify consistency.

### 2. Narrow Reporting Scope (Diff Bounding)

While the investigation looks wide, the reported review comments must remain strictly bounded to the code under review:
- Every finding in `## Review Comments` must anchor directly to a line modified in the diff (lines starting with `+` or `-`).
- Never demand that the author refactor, rename, or redesign existing, untouched APIs in the codebase.
- Distinguish between a defect introduced by this change and a pre-existing flaw in the surrounding architecture. Pre-existing flaws outside the diff are not actionable feedback for the author.
- If a wider systemic flaw makes the proposed change unsafe, raise it as a Question (`### Q1. ...`) with a specific `To settle` check, or drop it if speculative.

---

## Distinguishing In-Scope from Out-of-Scope Feedback

| Scenario | Investigation Context (Wide) | Permitted Review Finding (Narrow / Apropos) | Disallowed Finding (Scope Creep) |
| :--- | :--- | :--- | :--- |
| **New method added** | Sibling methods in the class use `getUser(UserId id)`. The new method is `fetchUser(String id)`. | Flag the new method: suggest `getUser` naming and `UserId` type to align with existing domain conventions. | Demanding that the entire class or other classes rename all `fetch*` or `get*` methods across the repository. |
| **Parameter added to public function** | Callers configure the function by passing ambient global state. | Flag the parameter if it leaks internal storage models, uses an untyped string instead of an enum, or adds speculative generality (YAGNI). | Telling the author to refactor the entire package to adopt dependency injection or rewrite the whole function signature. |
| **New endpoint or RPC** | Service uses cursor pagination for collections and structured error payloads. | Flag the new endpoint if it introduces offset pagination or returns unstructured string errors. | Demanding migration of older, existing endpoints in other files that still use offset pagination. |
| **Signature changed or tightened** | Call sites across the repository invoke the modified function. | Flag call site breakages directly at the modified signature line, citing impacted call sites with concrete line numbers. | Filing findings against call site files that were not part of the pull request. |

---

## Canonical Principles to Evaluate

Evaluate added or modified public surfaces against these core principles from [canonical_api_design.md](../../api-review/references/canonical_api_design.md):

### 1. Contract-First and Information Hiding
The API must expose capabilities, not internal implementation details or persistence models.
- Do not expose database schemas, internal data representations, or helper classes through public signatures.
- Keep the boundary decoupled so that the internal implementation can change without breaking callers.
- Ensure exported types and signatures are clean and explicit.

### 2. Minimal Surface Area (KISS and YAGNI)
Expose only what is required for current concrete use cases.
- Flag speculative parameters or configuration toggles added for hypothetical future needs.
- Reject "Swiss Army knife" functions that combine multiple unrelated responsibilities.
- Keep entry points lean and free of unnecessary cognitive complexity.

### 3. Interface Ergonomics and Predictability
Interfaces should be intuitive, self-descriptive, and follow standard idioms.
- **Intent-revealing names**: Names must describe business intent rather than internal mechanics (for example, `publishArticle()` rather than `updateRowStatus()`).
- **Principle of Least Astonishment**: Behavior should match standard caller expectations without surprising side effects.
- **Command-Query Separation (CQS)**: Separate operations that retrieve data (queries) from operations that mutate state (commands). Queries should be safe and idempotent.
- **Orthogonal primitives**: Provide composable building blocks rather than rigid, monolithic operations.

### 4. Explicit Configuration
Prefer explicit parameter passing and constructor injection over implicit global configuration.
- Avoid requiring callers to set global variables, singletons, or environment flags to configure the API.
- Explicit parameters make dependencies visible, keep tests isolated, and prevent concurrency hazards.

### 5. Strict Type Safety and Input Validation
Make invalid states unrepresentable through types.
- Use enums or sealed types instead of unconstrained strings or raw magic numbers.
- Validate all inputs at the public boundary and provide clear, structured errors rather than generic failure codes or swallowed exceptions.

### 6. Operational Reliability and Evolution
Design public interfaces with production realities in mind.
- **Idempotency**: Mutating operations in distributed environments should be designed for safe retries where possible.
- **Pagination**: Large collections must use scalable cursor pagination rather than unbounded queries or offset pagination.
- **Evolution**: When modifying existing public signatures, avoid unannounced breaking changes. Verify whether deprecation warnings or migration shims are warranted.

---

## Local Idioms vs. Canonical Ideals

When reviewing an incremental addition to an established codebase, consistency with surrounding code takes precedence over abstract global purity.

- If an existing class follows a specific local convention (such as parameter ordering or error code formats), follow that convention unless the author is intentionally introducing a new architectural pattern.
- Do not demand that a new helper method use a design pattern foreign to the rest of the file simply because it is theoretically cleaner.
- Do not suggest adding complex builder patterns or factory wrappers for simple data structures unless the cognitive complexity of instantiation justifies the abstraction.

---

## Review Matrix and Self-Critique

Before including an API finding in your review report, verify these checks:

1. **Changed line anchor**: Is the finding attached to a line modified in the diff (`+` or `-`)?
2. **Actionable replacement**: Does the finding provide a concrete, drop-in replacement snippet or specific signature suggestion?
3. **Evidence tier**: Is the finding supported by `Read`, `Executed`, or line-cited `Deduction` evidence?
4. **Concrete consequence**: Does the `Why` field explain the actual impact (such as broken contracts, integration friction, or runtime error)?
5. **No legacy refactor demand**: Does the suggestion only apply to the reviewed code, without asking the author to clean up unchanged code?
