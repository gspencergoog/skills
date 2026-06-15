---
title: Diagnosing Event Loop Starvation
description: Protocols for diagnosing and remediating Dart Event Loop starvation, including stack introspection and microtask analysis.
---

# Diagnosing Event Loop Starvation

Dart employs a dual-queue system: the **Event Queue** (I/O, UI, Timers) and the **Microtask Queue** (internal async operations). The Microtask Queue is strictly prioritized; the Event Loop will *never* process drawing events or user input as long as the Microtask Queue is not empty.

## Symptoms

- App UI freezes completely ("jank").
- Input events are ignored.
- CPU usage remains high during the freeze.
- `pumpAndSettle` tests fail with timeouts.

## Diagnostic Protocol

1. **Introspect Stack:** Use the VM Service `getStack` RPC via the DTD connection.
2. **Analyze Frames:** Look for the top-most execution frame.
   - **Signature:** Presence of internal SDK functions like `_microtaskLoop` or `_runPendingImmediateCallback`.
   - **Signature:** A recursive function appearing repeatedly in the stack snapshots without yielding.

## Remediation Strategy

The agent must break the synchronous or microtask lock to allow the Event Loop to breathe.

### Code Injection

Inject a zero-duration delay, which effectively places the continuation at the back of the Event Queue, yielding control to the engine.

```dart
// Agent injects this inside the detected loop
await Future.delayed(Duration.zero);
```

### 2. Watchdog Strategy (Optional)

For complex or intermittent stalls, inject a monitoring mechanism (e.g., a separate Isolate that pings the main Isolate). If the main Isolate fails to respond within a threshold (e.g., 100ms), the watchdog can log the stack trace or throw an error to identify the blocking synchronous code.

### Microtasks vs Events

If a task is spawning infinite microtasks (`scheduleMicrotask` or unawaited Futures completing synchronously), convert it to use `Timer.run` or `Future.delayed(Duration.zero)` to shift to the Event Queue.