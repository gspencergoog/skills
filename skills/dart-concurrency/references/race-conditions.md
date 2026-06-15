---
title: Race Conditions & State Inconsistency
description: Diagnosis and prevention of logical race conditions in Dart's single-threaded isolate model.
---

# Race Conditions & State Inconsistency

Although Dart code runs in a single isolate (thread), logical race conditions are common when asynchronous operations interleave unpredictably.

## Types of Races

### 1. Interleaved View Updates (Stale Data)
**Scenario:** User navigates away, but a network request completes and tries to update the now-defunct or reused widget state.
**Diagnosis:** Logs show "setState() called after dispose()" or UI flickers with old data.
**Remediation:** Check `mounted` (in State) or cancellation tokens before applying side effects.

### 2. Resource Contention (The "Check-Then-Act" Bug)
**Scenario:** Two async functions check a condition (e.g., `if (!isInitialized)`) and both proceed to initialize.
**Diagnosis:** Double initialization logs, corrupted database state.
**Remediation:** Use `package:synchronized` or a `Completer`-based lock.

## Remediation Strategies

### Cancellation Tokens
Pass a token to async operations to signal they should abort.

```dart
// CancelableOperation from package:async
final operation = CancelableOperation.fromFuture(
  fetchData(),
  onCancel: () => print('Cancelled'),
);

// On dispose
operation.cancel();
```

### Async Locking (`package:synchronized`)
Serialize access to a critical section.

```dart
import 'package:synchronized/synchronized.dart';

final _lock = Lock();

Future<void> criticalSection() async {
  await _lock.synchronized(() async {
    // Only one execution enters here at a time
    if (!isInitialized) {
      await initialize();
    }
  });
}
```

**Lock Hierarchies:** If acquiring multiple locks, always acquire them in the same order (e.g., Lock A then Lock B) across the entire codebase to prevent deadlocks.

### Stream Management
Always cancel subscriptions in `dispose()`.

```dart
StreamSubscription? _subscription;

@override
void dispose() {
  _subscription?.cancel();
  super.dispose();
}
```
