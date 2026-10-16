---
name: dart-concurrency
description: >-
  Prevents, diagnoses, and fixes asynchronous problems in Dart and Flutter: UI
  freezes and jank, event loop starvation, race conditions, shared-state
  corruption, isolate deadlocks, and test timeouts, using the Dart MCP analysis,
  test, and runtime tools. Use when the UI hangs or stutters, when a test times
  out around Futures, Streams, FakeAsync, or pumpAndSettle, or when the analyzer
  reports unawaited_futures, discarded_futures, or
  use_build_context_synchronously. Don't use for moving CPU-bound work such as
  JSON parsing into an isolate in an otherwise healthy app (use
  flutter-concurrency).
---

# Dart Concurrency Anomalies Workflow

## MCP Toolchain Dependencies

This skill requires access to the `dart` and `dart_analysis_server` MCP toolsets:

- **analyze_files**: For static analysis and AST parsing.
- **run_tests**: To reproduce hangs and capture timeout logs (`--log-file`).
- **dtd**: To bridge into the runtime environment (Dart Tooling Daemon).
- **get_runtime_errors**: To fetch current runtime errors and stack traces.
- **widget_inspector**: To detect infinite rendering loops.
- **hot_reload**: To inject fixes without losing state.
- **pub_dev_search**: To find pub packages (e.g., `synchronized`).

## Operational Workflows

### Phase 1: Proactive Static Analysis

Before debugging runtime issues, enforce structural correctness.

- **Scan**: Use `analyze_files` to check `analysis_options.yaml`.
- **Enforce**: Ensure `unawaited_futures`, `discarded_futures`, and `use_build_context_synchronously` are enabled.
- **Remediate**: If violations are found, apply the strategies defined in [references/static-analysis.md](references/static-analysis.md).

### Phase 2: Test Suite Reproduction

If a defect is reported via CI/CD or local testing:

- **Execute**: Run `run_tests` with the `--log-file` argument.
- **Parse**: Scan logs for `pumpAndSettle` timed out or `FakeAsync` discrepancies.
- **Resolve**: Apply heuristics from [references/test-suite-anomalies.md](references/test-suite-anomalies.md).

### Phase 3: Runtime Introspection (The "Hang" Protocol)

If the application is unresponsive but compiles:

- **Connect**: Invoke `dtd`.
- **Check Errors**: Use `get_runtime_errors` to identify unhandled exceptions.
- **Profile**: Use VM Service lookups to differentiate between Event Loop Starvation and Deadlocks.
  - If CPU is high and stack traces show recursion: See [references/event-loops.md](references/event-loops.md).
  - If CPU is low and execution halts: See [references/race-conditions.md](references/race-conditions.md) or [references/isolate-management.md](references/isolate-management.md).

## Decision Trees

### Is the UI Frozen?

- **Yes** -> Check Event Loop Starvation (High CPU) or Deadlock (Low CPU).
- **No (stale data)** -> Check Race Conditions.

### Is it a Test Timeout?

- `pumpAndSettle` -> Infinite Animation.
- Silent Hang -> `FakeAsync` vs. Real I/O conflict.
