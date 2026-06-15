---
name: dart-modern-features
description: Guidelines for preferentially using modern Dart features (v3.0 - v3.10) such as Records, Pattern Matching, Switch Expressions, Extension Types, Class Modifiers, Wildcards, Null-Aware Elements, and Dot Shorthands.
---

## Description

This skill defines the modern Dart features introduced between 2023 and 2025 (Dart 3.0 through Dart 3.10). When writing or updating Dart code, prefer using these advanced and efficient language features where appropriate to reduce boilerplate, improve type safety, and increase predictability.

## Features

### Records

Records are anonymous, immutable, aggregate structures that bundle multiple objects into a single object without defining a custom class. Use them when you need to return multiple values from a function or temporarily group related data.

**Before:** You had to create a dedicated class just to return multiple values, or rely on weakly-typed collections like `List<dynamic>` or `Map`.

```dart
class UserResult {
  final String name;
  final int age;
  UserResult(this.name, this.age);
}

UserResult fetchUser() {
  return UserResult('Alice', 42);
}

void main() {
  var user = fetchUser();
  print(user.name); // Alice
}
```

**After:** Records allow you to bundle types seamlessly on the fly.

```dart
// Returns a record containing a String and an int.
(String, int) fetchUser() {
  return ('Alice', 42);
}

void main() {
  var user = fetchUser();
  print(user.$1); // Alice
}
```

### Patterns and Pattern Matching

Patterns allow you to destructure complex data into local variables and match against specific shapes or values. Use them in conjunction with `switch` statements, `if-case` statements, or variable declarations to unpack data directly.

**Before:** Validating and extracting data required multiple manual type checks, null checks, and key lookups.

```dart
void processJson(Map<String, dynamic> json) {
  if (json.containsKey('name') && json['name'] is String &&
      json.containsKey('age') && json['age'] is int) {
    String name = json['name'];
    int age = json['age'];
    print('$name is $age years old.');
  }
}
```

**After:** Patterns combine type-checking, structural validation, and variable assignment into a single, concise statement.

```dart
void processJson(Map<String, dynamic> json) {
  // Destructures the map directly into variables.
  if (json case {'name': String name, 'age': int age}) {
    print('$name is $age years old.');
  }
}
```

### Switch Expressions

Switch expressions return a value directly, eliminating the need for bulky `case` and `break` statements. Use them when you need to compute and assign a single value based on multiple conditions.

**Before:** Switch statements were statements (not expressions), requiring `return` or assignment inside every single case block.

```dart
String describeStatus(int code) {
  switch (code) {
    case 200:
      return 'Success';
    case 404:
      return 'Not Found';
    default:
      return 'Unknown';
  }
}
```

**After:** Switch expressions return a value directly using the `=>` syntax.

```dart
String describeStatus(int code) {
  // Returns the evaluated expression directly.
  return switch (code) {
    200 => 'Success',
    404 => 'Not Found',
    _ => 'Unknown',
  };
}
```

### Class Modifiers

Modifiers (`sealed`, `final`, `base`, `interface`) restrict how classes and mixins can be used outside their defining library. Use `sealed` to define a closed family of subtypes, which allows the compiler to enforce exhaustive switch statements without a default case.

**Before:** The compiler couldn't know if you had covered every possible subclass in a `switch` statement, forcing you to write unnecessary `default` cases.

```dart
abstract class Result {}

class Success extends Result {}
class Failure extends Result {}

// Compiler doesn't know this is exhaustive.
String handle(Result r) {
  if (r is Success) return 'OK';
  if (r is Failure) return 'Error';
  return 'Unknown'; // Required fallback
}
```

**After:** `sealed` guarantees to the compiler that no other subclasses can exist outside this file, enabling exhaustive checking.

```dart
// Subclasses must be defined in the same file.
sealed class Result {}

class Success extends Result {}
class Failure extends Result {}

// The compiler knows no other cases exist; no default needed!
String handle(Result r) => switch(r) {
  Success() => 'OK',
  Failure() => 'Error',
};
```

### Extension Types

Extension types provide a zero-cost wrapper around an existing type. Use them when you want to restrict operations on an existing type or add custom behavior without the runtime overhead of allocating a new object wrapper.

**Before:** You had to allocate a completely new wrapper object in memory just to add domain-specific logic or type safety to a primitive type.

```dart
class Id {
  final int value;
  Id(this.value);
  bool get isValid => value > 0;
}

void main() {
  var id = Id(42); // Allocates a new Id object
  print(id.isValid);
}
```

**After:** Extension types compile down to the underlying type at runtime (zero overhead) but maintain type safety and custom methods at compile time.

```dart
// Wraps an int with zero runtime overhead.
extension type Id(int value) {
  bool get isValid => value > 0;
}

void main() {
  var id = Id(42); // At runtime, this is just an int
  print(id.isValid);
}
```

### Digit Separators

You can now use underscores (`_`) in number literals. Use this strictly to improve the visual readability of large numeric values in your source code.

**Before:** Long numbers were difficult to read at a glance.

```dart
const int oneMillion = 1000000;
```

**After:** Underscores separate thousands (or any other grouping), and the compiler ignores them.

```dart
const int oneMillion = 1_000_000;
```

### Wildcard Variables

Wildcards (`_`) act as non-binding variables or parameters. Use them to explicitly signal that a parameter is intentionally unused. You can declare multiple wildcards in the same scope without triggering naming collisions.

**Before:** You had to invent clunky, distinct variable names to avoid "unused variable" or "name collision" warnings.

```dart
void handleEvent(String ignoredName, int status) {
  print('Status: $status');
}
```

**After:** The underscore explicitly drops the binding, and you can reuse it repeatedly in the same signature.

```dart
// The underscore explicitly ignores the first argument.
void handleEvent(String _, int status) {
  print('Status: $status');
}
```

### Null-Aware Elements

Null-aware elements conditionally include items in a collection (list, set, or map) only if they evaluate to a non-null value. Use the `?` prefix inside collection literals to safely build collections without manually filtering out nulls.

**Before:** You had to use the collection `if` statement to check for nulls manually.

```dart
String? optionalName = null;

var names = [
  'Alice',
  if (optionalName != null) optionalName,
  'Charlie'
];
```

**After:** The `?` prefix does the null check inline.

```dart
String? optionalName = null;

// The list will only contain 'Alice' and 'Charlie'.
var names = ['Alice', ?optionalName, 'Charlie'];
```

### Dot Shorthands

Dot shorthands omit the explicit type name when the compiler can confidently infer it from the surrounding context. Use this to reduce boilerplate when working with enums, static fields, and named constructors.

**Before:** You had to fully qualify the enum or static class name, even when the type was obvious.

```dart
enum LogLevel { info, warning, error }

LogLevel currentLevel = LogLevel.info;
```

**After:** The compiler infers the type, saving keystrokes and reducing visual noise.

```dart
enum LogLevel { info, warning, error }

// The compiler infers LogLevel from the variable declaration context.
LogLevel currentLevel = .info;
```
