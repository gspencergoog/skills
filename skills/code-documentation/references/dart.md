# Dart & Flutter Documentation

## Documentation Structure (`///`)

Use `///` (doc comments) for all public members. This allows tools like `dartdoc` to process them.

### Comments Format

1.  **Summary Sentence**: The first line must be a single-sentence summary, ending with a period.
2.  **Blank Line**: Follow the summary with a blank line.
3.  **Details**: Add paragraphs, code samples, or lists as needed to explain parameters, return values, exceptions, and behavior.
4.  **Annotations**: Place doc comments **before** any metadata annotations (e.g., `@override`, `@Deprecated`).

```dart
/// A button that initiates a purchase flow.
///
/// This widget handles the loading state automatically and disables
/// itself while the transaction is processing.
@Deprecated('Use PurchaseButtonV2 instead')
class PurchaseButton extends StatelessWidget { ... }
```

### Property Documentation

- **Getters override Setters**: Document the getter. The tools will combine them. Do NOT document both.
  ```dart
  /// The current optimization level (0.0 to 1.0).
  double get optimizationLevel => _level;
  set optimizationLevel(double value) { ... }
  ```

### Library Documentation

- **Library Comments**: Add a doc comment at the top of the file (before imports) for libraries (files) to provide a high-level overview.

## Writing Guidelines

### Terminology

- Use **canonical terms**:
  - Refer to "widgets", "state", "build context", "render object".
  - Avoid generic terms like "component" or "element" when you mean "Widget".

### Code References

- Use square brackets `[MyClass]`, `[variableName]`, `[methodName]` to link to in-scope identifiers.
- Use backticks `` `true` ``, `` `null` ``, `` `this` `` for keywords and literals.
- **Code Samples**: Include code blocks to demonstrate usage.
  ````dart
  /// Example:
  ///
  /// ```dart
  /// final path = FlightPath(coordinates: [a, b]);
  /// ```
  ````

## Flutter Specifics

### Widgets

- **Purpose**: Explain what the widget does and when to use it.
- **Parameters**: Document key parameters, especially if they are required or have complex constraints.
- **State**: If the widget has interesting state behavior (e.g., keeping position on scroll), mention it.

```dart
/// Displays a flight path on a map.
///
/// Use this widget within a [MapLayout]. The path is drawn using
/// the provided [coordinates].
class FlightPath extends StatelessWidget {
  /// Creates a flight path.
  ///
  /// The [coordinates] must contain at least two points.
  const FlightPath({required this.coordinates, super.key});
```

### State Management

- **Lifecycle**: If a class manages a complex lifecycle (like a controller), explain how to dispose of it.
- **Private Classes**: While public APIs are the priority, significantly complex private classes (like intricate `State` logic) _should_ be documented to aid maintainability.
