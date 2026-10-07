# Review Criteria

This reference document outlines the criteria to prioritize when performing a code review, as well as guidelines for severity and constraints to ensure high-quality feedback.

Sections 1–6 apply to code files. Document files (specs, design docs, blueprints, and reference docs, as classified in SKILL.md Step 1b) use [reviewing_documents.md](reviewing_documents.md) and its rubrics instead.

Under `--panel`, each criterion belongs to one lane: **A** (does it work, and can it be verified?) or **B** (is it safe to ship and to maintain?). The lane is shown in each heading.

## Prioritized Criteria

### 1. Testing (lane A)

New or modified code should be tested with tests that cover the modified code. Fixes for bugs should include regression tests if possible. Review the tests before reviewing the code using the [Reviewing Tests reference](./reviewing_tests.md).

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

If changes alter public APIs or configuration surfaces:

- **Contract Integrity**: Ensure interfaces are clear, decoupled from implementation details, and statically typed.
- **Explicit Configuration**: Prevent stateful globals, singletons, registries, or environment variables for configuring packages. Ensure dependencies are explicitly parameter-injected.
- **KISS/YAGNI**: Avoid over-generalizing or building speculative future-proof features.

## Severity Levels

Use these severity levels to categorize your findings. Severity follows the consequence in `Why` and is capped by the evidence tier in [evidence.md](evidence.md#severity-caps). Document files use the more specific scale in [reviewing_documents.md](reviewing_documents.md#severity).

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
