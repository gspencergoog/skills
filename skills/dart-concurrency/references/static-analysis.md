---
title: Static Analysis & AST Remediation Protocols
description: Protocols for enforcing concurrency best practices using Dart static analysis and AST remediation.
---

# Static Analysis & AST Remediation Protocols

The first line of defense against concurrency anomalies is the Abstract Syntax Tree (AST). The agent must enforce the following rules and apply the associated remediation strategies if they are discovered using the `mcp_dart_analyze_files` tool.

## 1. `unawaited_futures` / `discarded_futures`

- **Severity:** Critical
- **Description:** A `Future` is invoked without `await`, causing it to execute detached from the current error-handling context. Exceptions in these futures will be swallowed silently. `discarded_futures` is stricter and catches cases where `Future`s are passed as `void`.

**Remediation Strategy:**

- **Scenario A (Dependency):** If subsequent code relies on the result, inject the `await` keyword.
- **Scenario B (Fire-and-Forget):** If the operation is intentionally independent, wrap the call in `unawaited(...)` from `dart:async`.

```dart
// Before
logAnalytics(); // Linter warning

// After
unawaited(logAnalytics()); // Explicitly marked
```

## 2. `use_build_context_synchronously`

- **Severity:** High (Crash Risk)
- **Description:** Attempting to use a Flutter `BuildContext` after an asynchronous gap (an `await`). If the widget was unmounted during the wait, this throws an exception.

**Remediation Strategy:**

- Inject a mounted check immediately after the `await` and before the context usage.

```dart
await database.save();
if (!context.mounted) return; // Agent must inject this check
Navigator.pop(context);
```

## 3. `avoid_redundant_async`

- **Severity:** Performance
- **Description:** Functions marked `async` that contain no `await` keywords. This forces unnecessary microtask scheduling.

**Remediation Strategy:**

- Strip the `async` keyword.
- Change return type from `Future<T>` to `T` (unless implementing an interface requiring Future).

## 4. `prefer_async_await`

- **Severity:** Maintainability/Debuggability
- **Description:** Legacy usage of `.then()` and `.catchError()` callback chains (Callback Hell).

**Remediation Strategy:**

- Refactor callback chains into linear `try/catch` blocks using `await`. This is essential for readable stack traces during debugging.