---
name: dart-concurrency
description: A specialized agentic workflow for proactively preventing, diagnosing, and remediating asynchronous anomalies in Dart and Flutter applications.
---

## Description

A specialized agentic workflow for proactively preventing, diagnosing, and remediating asynchronous anomalies in Dart and Flutter applications. This skill leverages the Model Context Protocol (MCP) to interact with the Dart VM, the Flutter Engine, and the Dart Tooling Daemon (DTD) to resolve event loop starvation, race conditions, deadlocks, and test suite failures.

## MCP Toolchain Dependencies

This skill requires access to the `dart` and `dart_analysis_server` MCP toolsets:

- **mcp_dart_analyze_files**: For static analysis and AST parsing.
- **mcp_dart_run_tests**: To reproduce hangs and capture timeout logs (`--log-file`).
- **mcp_dart_connect_dart_tooling_daemon**: To bridge into the runtime environment.
- **mcp_dart_get_runtime_errors**: To fetch current runtime errors and stack traces.
- **mcp_dart_get_widget_tree**: To detect infinite rendering loops.
- **mcp_dart_hot_reload**: To inject fixes without losing state.
- **mcp_dart_pub_dev_search**: To find pub packages (e.g., `synchronized`).

## Operational Workflows

### Phase 1: Proactive Static Analysis

Before debugging runtime issues, enforce structural correctness.

- **Scan**: Use `mcp_dart_analyze_files` to check `analysis_options.yaml`.
- **Enforce**: Ensure `unawaited_futures`, `discarded_futures`, and `use_build_context_synchronously` are enabled.
- **Remediate**: If violations are found, apply the strategies defined in [references/static-analysis.md](references/static-analysis.md).

### Phase 2: Test Suite Reproduction

If a defect is reported via CI/CD or local testing:

- **Execute**: Run `mcp_dart_run_tests` with the `--log-file` argument.
- **Parse**: Scan logs for `pumpAndSettle` timed out or `FakeAsync` discrepancies.
- **Resolve**: Apply heuristics from [references/test-suite-anomalies.md](references/test-suite-anomalies.md).

### Phase 3: Runtime Introspection (The "Hang" Protocol)

If the application is unresponsive but compiles:

- **Connect**: Invoke `mcp_dart_connect_dart_tooling_daemon`.
- **Check Errors**: Use `mcp_dart_get_runtime_errors` to identify unhandled exceptions.
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
