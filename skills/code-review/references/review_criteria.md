# Review Criteria

This reference document outlines the criteria to prioritize when performing a code review, as well as guidelines for severity and constraints to ensure high-quality feedback.

Sections 0 to 6 apply to code files. Document files (specs, design docs, blueprints, and reference docs, as classified in SKILL.md Step 1b) use [reviewing_documents.md](reviewing_documents.md) and its rubrics instead. Agent instruction files (`SKILL.md`, `AGENTS.md`, and rule files) get both: these criteria for their control flow and commands, and their rubric.

Under `--panel`, each criterion belongs to one lane: **A** (does it work, and can it be verified?) or **B** (is it safe to ship and to maintain?). The lane is shown in each heading. The roster, lane blocks, and focus lanes are defined in [lanes.md](lanes.md).

## Prioritized Criteria

### 0. System Fit and Necessity (lane B)

Before the hunk-by-hunk review, check whether each new wrapper, flag, pair of booleans that always change together, or effect that copies derivable state duplicates or bypasses a mechanism the repository already has. The procedure, including the exemption for staged code in stacked changes, is Step 0 in [reviewer_mandate.md](reviewer_mandate.md#step-0-system-fit--necessity).

### 1. Testing (lane A)

New or modified code should be tested with tests that cover the modified code. Fixes for bugs should include regression tests if possible. When the diff changes test files, read the [Reviewing Tests reference](./reviewing_tests.md) and review the tests before the code.

When `--verify` is on and the change is trusted, back a test or correctness claim with an `Executed` run where you can, including the two mutation patterns (break the changed implementation and run its test; revert the fix hunk and run the new regression test) described in [verification.md](verification.md#mutation-testing).

### 2. Correctness, Concurrency, and Edge Cases (lane A)

Verify code functionality, handle edge cases, and ensure alignment between function descriptions and implementations.

- **Logic errors**: Check for flawed logic or incorrect algorithms.
- **Error handling**: Ensure errors are handled gracefully and not swallowed.
- **Race conditions**: Look for potential concurrency issues, deadlocks, and missing resource cleanup.
- **Edge cases**: Empty collections, zero and maximum boundaries, duplicates, Unicode, and unexpected input types.
- **Data validation**: Verify that inputs are validated correctly.
- **API usage**: Ensure APIs are used correctly and efficiently.

### 3. Efficiency (lane B)

Identify performance bottlenecks and optimize for efficiency.

- Avoid unnecessary loops, iterations, or calculations.
- Watch for memory leaks or inefficient data structures.
- Avoid excessive logging in performance-critical paths.
- An unmeasured performance claim is `Speculation`: cap it at `medium`, or file a Question asking the author for the measurement.

### 4. Maintainability (lane B)

Assess code readability, modularity, and adherence to language idioms.

- **Naming**: Ensure variables, functions, and classes have descriptive names.
- **Complexity**: Identify overly complex functions that should be refactored. Use the [cognitive-complexity](../../cognitive-complexity/SKILL.md) skill to inspect functions exceeding the threshold score of 15 and suggest structural refactorings (such as Guard Clauses, Early Returns, or Extract Method).
- **Code duplication**: Look for opportunities to reuse code.
- **Style**: Adhere to specified style guides. Violations should be noted.
- **Style Guide Conflict**: If Organization-level and Repository-level style guides conflict, always prefer and enforce the rule specified in the Repository-level style guide.

### 5. Security (lane B)

Identify potential vulnerabilities.

- Insecure storage of sensitive data.
- Injection attacks (SQL, command, etc.).
- Insufficient access controls or validation.

### 6. API Soundness (lane B)

Only when the diff changes public APIs, exported signatures, or configuration surfaces, read [reviewing_apis.md](./reviewing_apis.md) and evaluate the changes against it and the canonical principles in [canonical_api_design.md](../../review-core/references/canonical_api_design.md):

- **Contract Integrity**: Ensure interfaces expose capabilities rather than internal implementation details or storage models.
- **Minimal Surface Area (KISS/YAGNI)**: Avoid speculative parameters or over-generalized helper abstractions.
- **Ergonomics & Predictability**: Use intent-revealing names, adhere to the Principle of Least Astonishment, and enforce Command-Query Separation (CQS).
- **Explicit Configuration**: Ensure dependencies are explicitly parameter-injected. Reject stateful globals, singletons, registries, or environment variables for configuring packages.
- **Strict Typing & Validation**: Use strong domain types or enums instead of raw strings, and validate inputs at boundaries.
- **Operational Reliability**: Design mutating operations with safe retries (idempotency) and use cursor-based pagination for collections.
- **Diff-Anchored Scope**: Every reported API finding must anchor to a line modified in the diff (+/-). Do not demand refactoring untouched legacy code or changing existing conventions outside the diff.

## Severity Levels

Use these severity levels to categorize your findings. Severity follows the consequence in `Why` and is capped by the evidence tier in [evidence.md](../../review-core/references/evidence.md#severity-caps). Document files use the more specific scale in [reviewing_documents.md](reviewing_documents.md#severity).

- **critical**: Must be addressed immediately. Could lead to serious consequences for correctness, security, or performance. Requires `Executed`, `Read`, `Fetched`, or a line-cited `Deduction`.
- **high**: Should be addressed soon. Likely to cause problems in the future. Requires `Executed`, `Read`, `Fetched`, or a line-cited `Deduction`.
- **medium**: Should be considered for future improvement. Not critical or urgent. Highest severity allowed for `Speculation`.
- **low**: Minor or stylistic issues. Can be addressed at the author's discretion.

## Critical Constraints

- **Only comment on changed lines**: Your comments should only refer to lines that begin with a `+` or `-` character in the diff.
- **Every finding has `Evidence` and `Why`**: State the evidence tier (`Executed`, `Read`, `Fetched`, `Deduction`, or `Speculation`) with its proof, and state the concrete consequence in `Why`.
- **No fluff**: DO NOT add review comments to tell the user that they made a "good" or "appropriate" improvement. Only comment when there is an improvement opportunity.
- **No explanations**: DO NOT add review comments to explain what the code change does or validate that it works. The author knows what they wrote.
- **Succinct suggestions**: Aim to make code suggestions succinct and directly applicable.
- **Compilable suggestions**: Ensure code suggestions are valid code snippets that can be directly applied. For document files, suggestions are drop-in replacement text or a specific question.
