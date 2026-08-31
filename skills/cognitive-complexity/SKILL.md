---
name: cognitive-complexity
description: Calculate Cognitive Complexity for source code across multiple programming languages (Python, TypeScript, JavaScript, Dart, Swift, and Kotlin) according to the SonarSource standard. Use this skill whenever asked to: (1) determine, measure, or evaluate the cognitive complexity of code, files, or directories, (2) identify high-complexity functions or code hotspots for refactoring, (3) inspect complexity breakdowns with line-by-line increments, or (4) assess maintainability and understandability metrics.
---

# Cognitive Complexity Skill

This skill provides instructions and standalone CLI tools to calculate and audit the **Cognitive Complexity** of source code across **Python**, **TypeScript/JavaScript**, **Dart**, **Swift**, and **Kotlin** according to the official SonarSource standard.

---

## 1. Overview

While Cyclomatic Complexity measures the number of linearly independent paths required to test code, **Cognitive Complexity** measures how difficult the code is for a human developer to understand and maintain.

The metric evaluates code based on three core rules:
1. **Increments**: Adds +1 for control flow breaks (`if`, ternary `? :`, `switch`/`when`/`match`, `for`, `while`, `catch`, recursion, boolean sequence changes).
2. **Nesting Penalties**: Adds `+1 + nesting_level` when control flow structures are nested inside others.
3. **Idiom Shorthands**: No penalty for `else if`/`elif`, method chaining, or safe navigation (`?.`).

For a detailed theoretical explanation of the rules, see [SonarSource Rules Reference](references/sonar_rules.md) and [Language AST Mapping Reference](references/language_rules.md).

---

## 2. Thresholds & Score Interpretation

| Complexity Score | Classification | Risk Level | Recommended Action |
| :--- | :--- | :--- | :--- |
| **0 – 5** | Simple | Low | Ideal maintainability. No changes needed. |
| **6 – 10** | Moderate | Low | Normal function logic. Keep clear comments. |
| **11 – 15** | Elevated | Medium | Approaching threshold. Look for simplification opportunities. |
| **16 – 24** | High | High | Exceeds standard threshold (15). Refactor via Extract Method or Guard Clauses. |
| **25+** | Critical Hotspot | Extreme | High risk of bugs during maintenance. Immediate refactoring required. |

---

## 3. Quick Start & CLI Invocation

### 3.1. Unified Multi-Language CLI

The top-level orchestrator automatically detects the language from the file extension or directory contents:

```bash
# Analyze a single file
python3 scripts/cognitive_complexity.py path/to/file.py

# Analyze standard input
cat snippet.ts | python3 scripts/cognitive_complexity.py --lang typescript -

# Analyze a directory recursively with verbose line-by-line breakdown
python3 scripts/cognitive_complexity.py path/to/project/ --threshold 15 --verbose

# Emit machine-readable JSON for downstream parsing
python3 scripts/cognitive_complexity.py path/to/project/ -f json
```

### 3.2. Language-Specific Engines

Each language also provides a standalone CLI with an identical argument contract:

```bash
# Python Engine
python3 scripts/python/cognitive_complexity.py [OPTIONS] [PATH]

# TypeScript Engine
node scripts/typescript/dist/src/cli.js [OPTIONS] [PATH]

# Dart Engine
dart run scripts/dart/bin/cognitive_complexity.dart [OPTIONS] [PATH]

# Swift Engine
scripts/swift/.build/release/CognitiveComplexity [OPTIONS] [PATH]

# Kotlin Engine
java -jar scripts/kotlin/cognitive-complexity-kt.jar [OPTIONS] [PATH]
```

For complete argument specifications, formatting options, and exit codes, see [CLI Reference](references/cli_reference.md).

---

## 4. Agent Workflow: Analyzing & Refactoring Code

When tasked with measuring or improving code complexity:

### Step 1: Run Complexity Analysis
Run the analyzer with `-f json -v` on the target files:
```bash
python3 scripts/cognitive_complexity.py <target_file> -f json -v
```

### Step 2: Identify High-Complexity Functions
Check `functions_exceeding_threshold` in the summary or review any functions with score $> 15$.

### Step 3: Inspect the Diagnostic Audit Trail
Examine the `breakdown` array for each function. Note where high nesting penalties accumulate:
- **Nested loops / conditionals**: Functions with deep nesting (`nesting >= 2`) contribute large increments ($+3$, $+4$ per statement).
- **Long `switch` / `when` statements**: Check if polymorphism or strategy patterns can simplify branching.
- **Complex boolean sequences**: Check if intermediate boolean variables or helper functions can reduce expression complexity.

### Step 4: Apply Standard Refactoring Patterns
1. **Extract Method**: Extract nested loops or inner conditionals into private helper functions. This resets the nesting level back to 0 in the helper function.
2. **Guard Clauses / Early Returns**: Replace nested `if-else` blocks with top-level guard clauses (e.g. `if (!condition) return;`).
3. **De-nesting**: Invert conditions to return or continue early.

### Step 5: Re-verify Complexity
Re-run the analysis to verify that the cognitive complexity score has dropped below the threshold.
