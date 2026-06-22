# Result and Command Patterns for Flutter

This reference guide provides the standard implementations of the `Result` and `Command` patterns, which are core utilities for error handling and asynchronous operations in the recommended Flutter architecture.

## Table of Contents
- [Result Pattern](#result-pattern)
  - [Implementation](#result-implementation)
  - [Usage Guidelines](#result-usage-guidelines)
- [Command Pattern](#command-pattern)
  - [Implementation](#command-implementation)
  - [Usage Guidelines](#command-usage-guidelines)

---

## Result Pattern

The `Result` pattern represents the outcome of an operation that can either succeed (`Ok`) or fail (`Error`). This enforces explicit error handling at compile-time and avoids throwing exceptions across architectural boundaries.

### Result Implementation

```dart
/// Represents the outcome of an operation.
sealed class Result<T> {
  const Result();

  /// Creates a successful result containing [value].
  factory Result.ok(T value) = Ok<T>;

  /// Creates a failed result containing [error].
  factory Result.error(Exception error) = Error<T>;
}

/// A successful result containing a value of type [T].
class Ok<T> extends Result<T> {
  const Ok(this.value);

  final T value;

  @override
  String toString() => 'Result.ok($value)';
}

/// A failed result containing an [error].
class Error<T> extends Result<T> {
  const Error(this.error);

  final Exception error;

  @override
  String toString() => 'Result.error($error)';
}
```

### Result Usage Guidelines

* **Return from Repositories:** All repository methods that perform asynchronous work or can fail must return a `Future<Result<T>>` instead of throwing exceptions or returning nullable types.
* **Handle in ViewModels:** ViewModels must inspect the `Result` using pattern matching or type checks (`is Ok` / `is Error`) to update the UI state accordingly.

---

## Command Pattern

The `Command` pattern encapsulates an action (usually asynchronous) triggered by the UI. It manages the execution state (e.g., whether it is currently running) and automatically notifies listeners, allowing the UI to reactively disable buttons or show loading indicators.

### Command Implementation

```dart
import 'package:flutter/foundation.dart';

/// Base class for all commands.
abstract class Command<T> extends ChangeNotifier {
  bool _running = false;

  /// Whether the command is currently executing.
  bool get running => _running;

  /// Whether the command has completed execution.
  bool _completed = false;
  bool get completed => _completed;

  /// The error, if the command failed.
  Exception? _error;
  Exception? get error => _error;

  /// Whether the command failed.
  bool get hasError => _error != null;

  @protected
  void startExecution() {
    _running = true;
    _error = null;
    _completed = false;
    notifyListeners();
  }

  @protected
  void endExecution({Exception? error}) {
    _running = false;
    _error = error;
    _completed = true;
    notifyListeners();
  }
}

/// A command that takes no parameters and returns a [Result<T>].
class Command0<T> extends Command<T> {
  Command0(this._action);

  final Future<Result<T>> Function() _action;

  /// Executes the action.
  Future<Result<T>> execute() async {
    if (running) {
      return Result.error(StateError('Command is already running.'));
    }

    startExecution();
    try {
      final result = await _action();
      if (result is Error<T>) {
        endExecution(error: result.error);
      } else {
        endExecution();
      }
      return result;
    } on Exception catch (e) {
      final result = Result.error(e);
      endExecution(error: e);
      return result;
    }
  }
}

/// A command that takes one parameter of type [P] and returns a [Result<T>].
class Command1<T, P> extends Command<T> {
  Command1(this._action);

  final Future<Result<T>> Function(P) _action;

  /// Executes the action with [parameter].
  Future<Result<T>> execute(P parameter) async {
    if (running) {
      return Result.error(StateError('Command is already running.'));
    }

    startExecution();
    try {
      final result = await _action(parameter);
      if (result is Error<T>) {
        endExecution(error: result.error);
      } else {
        endExecution();
      }
      return result;
    } on Exception catch (e) {
      final result = Result.error(e);
      endExecution(error: e);
      return result;
    }
  }
}
```

### Command Usage Guidelines

* **Declare in ViewModels:** Expose commands as `late final Command0` or `late final Command1` properties in the ViewModel.
* **Bind to UI:** In the View, bind button press callbacks directly to the command's `execute` method, and use `command.running` to conditionally disable the button or show a progress indicator.
