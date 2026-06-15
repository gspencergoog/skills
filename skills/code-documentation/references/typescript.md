# TypeScript Documentation

## Documentation Styles

### JSDoc vs Implementation Comments

- **Documentation**: Use `/** ... */` for implementation that should be visible to users of the code (JSDoc/TSDoc).
- **Implementation**: Use `//` for implementation details that are only relevant to developers reading the source code.
- **Block Comments**: Avoid `/* ... */` for multi-line comments. Use multiple `//` lines instead.

```typescript
/**
 * Computes the weight based on three factors.
 *
 * @param itemsSent The number of items sent.
 * @param itemsReceived The number of items received.
 */
function computeWeight(itemsSent: number, itemsReceived: number): number {
  // This is an implementation comment explaining the formula.
  // It uses multiple lines of // comments.
  return itemsSent * 2 + itemsReceived;
}
```

## JSDoc/TSDoc Format

### General Form

- **Single-line**: `/** This is a short description. */`
- **Multi-line**:
  ```typescript
  /**
   * Multiple lines of JSDoc text are written here,
   * wrapped normally.
   *
   * @param arg A number to do something to.
   */
  ```

### Markdown

JSDoc is written in **Markdown**.

- Use standard Markdown for lists, code blocks, and formatting.
- Avoid plain text formatting that relies on whitespace, as tools will ignore it.

```typescript
/**
 * Computes weight based on three factors:
 *
 * - items sent
 * - items received
 * - last timestamp
 */
```

### Tags

- **@param**: Must occupy its own line. Description should follow the parameter name.
- **@return**: Must occupy its own line.
- **@deprecated**: Add for deprecated symbols.

## What to Document

### Top-Level Exports

Document all top-level exports of modules (classes, interfaces, functions, constants).

- **Exception**: Symbols exported only for tooling (e.g., Angular `@NgModule` classes) may be exempt if their purpose is obvious to tooling.

### Classes

Provide enough information/context for the reader to know **how and when** to use the class.

- Textual descriptions on the constructor may be omitted if the class documentation covers it.

### Methods and Functions

- **Purpose**: Explain what the function does.
- **Parameters/Return**: Document if not immediately obvious from types and names.
- **Redundancy**: Avoid merely restating the name (e.g., `@param name The name` is useless).

## Tooling Compatibility

- TypeScript-aware editors (VS Code) and documentation generators (TypeDoc) rely on valid JSDoc structure.
- Ensure comments are well-formed to enable strict type checking and tool support.
