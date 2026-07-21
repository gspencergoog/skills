# Test Quality & Coverage Audit Guide

Use this guide during Phase 2 of the `pre-review-prep` workflow to reach 90%+ code coverage and audit test quality.

---

## 1. Project Auto-Detection & Pre-Test Linter Gate

1. **Auto-Detect Project Type & Test Runner**:
   - `pubspec.yaml` -> Dart/Flutter (Use Dart MCP `analyze_files` & `run_tests` / `flutter test --coverage`)
   - `pyproject.toml` / `setup.py` / `requirements.txt` -> Python (`pytest --cov=<package> --cov-report=term-missing`)
   - `package.json` -> Node/TypeScript (`npm run lint` & `jest --coverage` / `npm test`)
   - `Cargo.toml` -> Rust (`cargo clippy` & `cargo test`)
   - `go.mod` -> Go (`golangci-lint` & `go test -cover`)

2. **Pre-Test Linter & Analyzer Gate**:
   - Run the project's static analyzer/linter before executing unit tests.
   - Fix all compilation, typing, and static analysis errors to ensure test compilation succeeds cleanly.

---

## 2. Coverage Enforcement (90%+ Target)

1. **Run Test Suite with Coverage**:
   - Execute the auto-detected test runner with coverage flags enabled.
2. **Identify Uncovered Code**:
   - Inspect missing lines reported in the coverage report.
   - Focus on error handling branches, edge-case conditions, and newly added public methods.
3. **Add Targeted Unit Tests**:
   - Write explicit unit test cases for uncovered paths.
   - Re-run coverage until all modified or added modules reach **>= 90% line and branch coverage**.

---

## 3. Test Quality & Reliability Audit

High coverage numbers alone do not guarantee good tests. Audit all new and existing tests against the following quality dimensions:

### A. Hermeticity & Test Independence (Zero State Leakage)
- **Hermetic Isolation**: Unit tests must never make real network requests or depend on external service availability. All I/O must be mocked or injected.
- **State Cleanup**: Shared singletons, static caches, mock call histories, and global state **must be reset in `setUp`/`tearDown`** so tests pass regardless of execution order or parallelism.

### B. False Positives & Pass-When-Should-Fail Detection
- **Swallowed Exceptions**: Detect tests catching all exceptions (`try { ... } catch (e) {}`) without re-throwing or asserting.
- **Missing Assertions**: Audit for tests that run execution paths without checking outputs or state changes.
- **Unawaited Async Ops**: Check for unawaited Promises/Futures that allow tests to pass before assertions execute.
- **Regression Guarantee**: Confirm that every bug fixed during the conversation has a dedicated regression test that fails if the fix is reverted.

### C. Assertion Precision & Specificity
- **Avoid Vague Assertions**: Ban loose checks like `assert result is not None` or `assertTrue(len(x) > 0)`. Assert exact values and data structures.
- **Exception Attributes**: When testing error paths, assert both the exact exception type and key message contents.
- **Floating-Point Delta**: Enforce `closeTo()` / `assertAlmostEqual()` for floating-point calculations to avoid non-deterministic precision failures.

### D. Test Structure & Readability
- **Arrange-Act-Assert (AAA)**: Enforce visual separation between setup, execution, and assertion phases.
- **Descriptive Naming**: Follow scenario-driven naming patterns: `test_<function>_<scenario>_<expected_outcome>()`.
- **Focused Scope**: Ensure each test function validates a single logical requirement rather than combining multiple unrelated features into a monolithic test.

### E. Flakiness & Timing Hygiene
- **No Hardcoded `sleep()` Calls**: Ban arbitrary delay calls (`time.sleep()`, `Future.delayed()`). Use fake clocks (`FakeAsync`), reactive completion signals, or explicit polling utilities.
- **Resource Lifecycle Management**: Ensure open streams, controllers, timers, subprocesses, and temporary files are explicitly closed/deleted in `tearDown`.

### F. Mandatory Boundary & Edge Case Matrix
Verify tests exist for standard edge-case inputs:
- Empty inputs (`""`, `[]`, `{}`)
- Null / None / undefined values
- Boundary numbers (`0`, `-1`, `MAX_INT`)
- Invalid format / malformed syntax
- Unicode and special character handling

---

## 4. Test Audit Checklist

- [ ] Static analyzer / linter passes cleanly without type or compilation errors.
- [ ] All modified/new source files achieve >= 90% line and branch coverage.
- [ ] No real network or unmocked disk I/O in unit tests.
- [ ] Shared global/static state and mocks are reset in `tearDown`.
- [ ] No unhandled unawaited asynchronous operations.
- [ ] Exception tests assert both exception type and message content.
- [ ] No hardcoded `sleep()` / `delay()` calls present.
- [ ] Boundary conditions (empty, null, 0, invalid) are explicitly tested.
- [ ] Every bug fix has a dedicated regression test.
