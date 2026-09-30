# PR Review Guidelines

These review guidelines ensure feedback is actionable, objective, and respectful of the author's effort.

## Core Review Principles

1. **Focus Exclusively on Actionable Findings**:
   - Only leave a review comment if there is a concrete bug, logic error, performance issue, security concern, test gap, or clear maintainability issue.
   - Do **NOT** leave purely complimentary comments (e.g. "Looks good!", "Nice refactor!"). Compliments in line comments create notification noise and clutter the PR review timeline.
   - Do **NOT** explain what the code does or echo back the author's logic without suggesting a change.

2. **Respect Author Agency & Style**:
   - If a code style choice is not governed by an existing linter or documented project style guide, do not enforce personal stylistic preferences.
   - Categorize comments honestly:
     - `🔥 Critical`: Security vulnerabilities, data corruption, severe memory leaks, or broken tests.
     - `⚠️ High`: Logic bugs, unexpected runtime exceptions, or missing error handling.
     - `💡 Medium`: Performance optimizations, suboptimal data structures, or missing test assertions.
     - `🌱 Low`: Minor clarity improvements, dead code removal, or documentation typos.

3. **Provide Concrete Code Suggestions**:
   - Whenever proposing a replacement for fewer than 10 lines of code, use GitHub suggestion syntax:
     ````markdown
     ```suggestion
     replacement code
     ```
     ````
   - Ensure the indentation matches the target file's indentation exactly.

4. **Verify Outside-Diff Context**:
   - Read referenced classes, interfaces, and callers to verify whether assumptions in the PR hold.
   - If an issue exists on lines outside the diff hunk, note it in the top-level review summary rather than attempting an invalid inline comment.

---

## Evaluation Categories

### 1. Correctness & Edge Cases
- Are null/undefined values handled where appropriate?
- Are off-by-one errors present in loops, slicing, or boundary checks?
- Are resources (file handles, streams, subscriptions, network sockets) properly disposed of?
- Are asynchronous operations properly awaited or handled?

### 2. Security
- Is untrusted input sanitized before database queries, shell execution, or DOM insertion?
- Are authentication or permission checks enforced at trust boundaries?
- Are sensitive secrets or keys hardcoded into source files or test fixtures?

### 3. API Soundness & Breaking Changes
- Are public signatures modified in a way that breaks existing callers?
- Are parameters, return types, or default arguments changed compatibly?
- Are newly introduced symbols well-documented?

### 4. Maintainability & Complexity
- Are functions doing too many things at once?
- Does the change introduce deep nesting or high cognitive complexity?

### 5. Testing
- Do new features or bug fixes have corresponding automated unit or integration tests?
- Do tests assert specific expected outcomes rather than merely running code without assertions?
- Are edge cases (empty lists, negative values, timeouts, network failures) covered?
