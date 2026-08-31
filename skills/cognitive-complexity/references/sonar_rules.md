# SonarSource Cognitive Complexity Reference

This document describes the Cognitive Complexity metric formulated by G. Ann Campbell at SonarSource ("Cognitive Complexity: A new way of measuring understandability").

## 1. Overview

While Cyclomatic Complexity measures the number of linearly independent execution paths required to achieve full test coverage, **Cognitive Complexity** measures how difficult the control flow is for a human reader to understand.

Cognitive Complexity is governed by three fundamental principles:
1. Ignore structures that allow multiple statements to be readably shorthanded into one.
2. Increment (add +1) for each break in the linear flow of the code.
3. Increment (add +1 + nesting) when control flow structures are nested.

---

## 2. Basic Increments (B1)

An increment (+1) is added for:

### B1.1. Control Flow Structures
- `if`, ternary conditional operator `? :`
- `switch` statements, `match` statements, `when` expressions
- `for` loops (including `for-in`, `for-of`, and list/collection comprehensions)
- `while` loops and `do-while` / `repeat-while` loops
- `catch` / `except` blocks in `try` statements
- `goto`, labeled `break`, labeled `continue` (jumps to non-immediate labels)

### B1.2. Hybrid / Non-Nesting Increments (+1)
- `else`, `else if`, `elif`: Adds +1 to complexity, but does **not** increase the nesting level for subsequent statements.
- **Recursion**: Direct recursion (function calls itself) or mutual recursion adds +1.

### B1.3. Sequences of Binary Logical Operators
- A continuous sequence of identical binary boolean operators (`a && b && c`) adds **+1** total for the entire sequence.
- When the boolean operator changes (e.g., `a && b || c`), each change adds an additional **+1**.
- Example:
  - `a && b && c && d` -> +1
  - `a && b || c && d` -> +3 (initial `&&` sequence +1, switch to `||` +1, switch to `&&` +1)
  - `(a && b) || (c && d)` -> +3

---

## 3. Nesting Penalties (B2)

When a control flow structure is nested within another control flow structure or closure/lambda, its cost is calculated as:
$$\text{Cost} = 1 + \text{current\_nesting\_level}$$

### Nesting Level Rules:
- The base nesting level at the top level of a function or method is `0`.
- The nesting level increases by `+1` inside the body of:
  - `if` and ternary `? :` branches
  - `switch`, `match`, and `when` blocks
  - `for`, `while`, and `do-while` loops
  - `catch` / `except` blocks
  - Nested function definitions, closures, lambdas, and anonymous functions
- The nesting level does **not** increase inside:
  - `else`, `else if`, `elif` (remains at parent's nesting level)
  - `try` blocks (only `catch` increments nesting and complexity)
  - `finally` blocks
  - `defer` blocks

---

## 4. Idiom Shorthands (B3)

No complexity penalty is assessed for:
- Safe navigation / optional chaining (`?.`), unless paired with a fallback branching operator.
- Method chaining / fluent API calls that do not introduce branches.
- Declarative signatures, type annotations, and annotations/decorators.

---

## 5. Complexity Threshold Guidelines

| Score Range | Complexity Rating | Risk Assessment | Recommended Action |
| :--- | :--- | :--- | :--- |
| **0 - 5** | Simple / Clean | Minimal risk | No action needed. Ideal maintainability. |
| **6 - 10** | Moderate | Low risk | Normal function logic. Keep clear comments. |
| **11 - 15** | Elevated | Moderate risk | Approaching threshold. Look for simplification opportunities. |
| **16 - 24** | High | High risk | Exceeds standard threshold. Refactor via Extract Method or Guard Clauses. |
| **25+** | Critical Hotspot | Extreme risk | Must be refactored. Prone to bugs and maintenance errors. |
