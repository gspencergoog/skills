---
name: dart-modern-features
description: Guidelines for using modern Dart features (v3.0 - v3.10) to reduce boilerplate, improve type safety, and increase readability. Trigger this skill when writing or refactoring Dart/Flutter code to apply modern paradigms including Records, Pattern Matching, Switch Expressions, Class Modifiers, Extension Types, Digit Separators, Wildcards, Null-Aware Elements, and Dot Shorthands.

---

# Dart Modern Features Guidelines

## 1. Features

### Records

Use records to bundle multiple objects into a single, anonymous, immutable, aggregate structure without defining a custom class. Prefer them when returning multiple values from a function or temporarily grouping related data.

**Before:** Create a dedicated class just to return multiple values, or rely on weakly-typed collections like `List<dynamic>` or `Map`.

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

**After:** Use records to bundle types on the fly with positional or named fields for clarity and type safety.

```dart
// Returns a record with named fields.
({String name, int age}) fetchUser() {
  return (name: 'Alice', age: 42);
}

void main() {
  var user = fetchUser();
  print(user.name); // Alice
  print(user.age);  // 42
}
```

### Patterns and Pattern Matching

Use patterns to destructure complex data into local variables and match against specific shapes or values. Combine them with `switch` statements, `if-case` statements, or variable declarations to unpack data directly.

**Before:** Validate and extract data using multiple manual type checks, null checks, and key lookups.

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

**After:** Use patterns to combine type-checking, structural validation, and variable assignment into a single, concise statement.

```dart
void processJson(Map<String, dynamic> json) {
  // Destructure the map directly into variables.
  if (json case {'name': String name, 'age': int age}) {
    print('$name is $age years old.');
  }
}
```

### Switch Expressions

Use switch expressions to return a value directly, eliminating the need for bulky `case` and `break` statements. Prefer them when computing and assigning a single value based on multiple conditions.

**Before:** Use switch statements that require `return` or assignment inside every single case block.

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

**After:** Use switch expressions to return evaluated values directly using the `=>` syntax.

```dart
String describeStatus(int code) {
  // Return the evaluated expression directly.
  return switch (code) {
    200 => 'Success',
    404 => 'Not Found',
    _ => 'Unknown',
  };
}
```

### Class Modifiers

Apply class modifiers (`sealed`, `final`, `base`, `interface`) to restrict how classes and mixins are used outside their defining library. Use `sealed` to define a closed family of subtypes, allowing the compiler to enforce exhaustive switch statements without a default case.

**Before:** Avoid legacy abstract classes where the compiler cannot verify exhaustiveness, forcing redundant default fallback cases.

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

**After:** Use `sealed` to guarantee to the compiler that no other subclasses can exist outside this file, enabling compile-time exhaustiveness checking.

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

Use extension types to establish a zero-cost wrapper around an existing type. Apply them when restricting operations on an existing type or adding custom behavior without the runtime overhead of allocating wrapper objects.

**Before:** Avoid allocating new wrapper objects in memory solely to add domain-specific logic or compile-time type safety to a primitive type.

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

**After:** Use extension types to compile down to the underlying type at runtime (zero overhead) while maintaining type safety and custom methods at compile time.

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

Use underscores (`_`) as digit separators in number literals to improve the visual readability of large numeric values.

**Before:** Avoid hard-to-read, long numeric literals.

```dart
const int oneMillion = 1000000;
```

**After:** Insert underscores to separate thousands or any other logical groupings; the compiler ignores them at compile time.

```dart
const int oneMillion = 1_000_000;
```

### Wildcard Variables

Use wildcards (`_`) as non-binding variables or parameters to explicitly signal that a value is intentionally ignored. Declare multiple wildcards in the same scope to prevent naming collisions and unused variable warnings.

**Before:** Avoid inventing clunky, placeholder variable names (like `ignored`, `unused`) just to bypass compiler warnings.

```dart
void handleEvent(String ignoredName, int status) {
  print('Status: $status');
}
```

**After:** Use the underscore to explicitly drop the binding. Declare multiple underscores in the same parameter list or pattern destructuring block.

```dart
// Ignore both the first and second parameters using wildcards.
void handleEvent(String _, int _, double value) {
  print('Value: $value');
}

void main() {
  // Destructure only the first element of a record, ignoring the rest.
  var (name, _, _) = ('Alice', 42, 'Developer');
  print(name); // Alice
}
```

### Null-Aware Elements

Use null-aware elements (prefixed with `?`) inside collection literals (lists, sets, or maps) to conditionally include items only if they evaluate to a non-null value. Prefer this to safely build collections without manual null-filtering boilerplate.

**Before:** Avoid using verbose collection `if` statements solely to perform simple null checks.

```dart
String? optionalName = null;

var names = [
  'Alice',
  if (optionalName != null) optionalName,
  'Charlie'
];
```

**After:** Apply the `?` prefix directly to elements within the collection literal for inline null-safety.

```dart
String? optionalName = null;

// The list will only contain 'Alice' and 'Charlie'.
var names = ['Alice', ?optionalName, 'Charlie'];
```

### Dot Shorthands

Use dot shorthands to omit the explicit type name when the compiler can confidently infer it from the surrounding context. Apply this to reduce boilerplate when working with enums, static fields, and named constructors.

**Before:** Avoid fully qualifying the enum or static class name when the target type is already obvious from the context.

```dart
enum LogLevel { info, warning, error }

LogLevel currentLevel = LogLevel.info;
```

**After:** Omit the class prefix; the compiler infers the type, saving keystrokes and reducing visual noise.

```dart
enum LogLevel { info, warning, error }

// The compiler infers LogLevel from the variable declaration context.
LogLevel currentLevel = .info;
```
