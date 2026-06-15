---
name: code-documentation
description: Guide for writing effective code documentation. Use when writing new code, adding features, or improving existing documentation to ensure clarity and maintainability.
---

# Code Documentation Skill

This skill provides comprehensive guidelines for documenting code, prioritizing user-centric writing, clarity, and consistency.

## 1. General Philosophy

- **User-Centric**: Write for the person using your API. If you had to look up how to use something, document it so others don't have to.
- **Explain "Why"**: The code signature tells _what_ it does. The documentation should explain _why_ it exists and _how_ to use it effectively.
- **Be Concise**: Omit fluff. If the documentation only restates the code name, it's not helpful.
- **Consistency**: Use standard terminology and consistent formatting.
- **Public APIs**: Documentation is **mandatory** for all public APIs (classes, members, top-level functions).
- **Code Samples**: Strongly consider adding code samples to explain usage.

## 2. General Structure

Most languages follow a similar structure for documentation comments:

1.  **Summary Sentence**: The first line must be a single-sentence summary, ending with a period.
2.  **Blank Line**: Follow the summary with a blank line.
3.  **Details**: Add paragraphs, code samples, or lists as needed to explain parameters, return values, exceptions, and behavior.
4.  **Annotations**: Place doc comments **before** any metadata annotations.

## 3. Writing Guidelines

### Brevity & Style

- **Avoid Fluff**: Omit "This class...", "This method...", "Is used to...", "Note that...".
  - _Bad_: "This method is used to calculate the total."
  - _Good_: "Calculates the total."
- **Third-Person Verbs**: Start function/method docs with a third-person singular verb.
  - _Examples_: "Returns...", "Calculates...", "Updates...", "Creates...".
- **Noun Phrases**: Start variable/property docs with a noun phrase.
  - _Examples_: "The current color.", "A list of active users.".
- **Booleans**: Always start with "Whether" (or similar clear indicator).
  - _Good_: "Whether this widget is enabled."
  - _Bad_: "If this widget is enabled...", "True if...", "Flag to indicate...".
- **Avoid Jargon**: Use plain English unless the term is a widely accepted standard (e.g., "HTTP", "URL").

### Formatting

- **Sparingly**: Use Markdown features (bold, lists) sparingly.
- **No HTML**: Avoid HTML unless strictly necessary and supported by the documentation tool.
- **Parameters/Returns/Exceptions**: Use prose to describe parameters, return values, and thrown exceptions. Do not rely solely on tags like `@param` unless mandated by the language standard (e.g., Javadoc).

## 4. Implementation Comments

Implementation comments (`//`) should be accurate, relevant, provide information that isn't readily understandable from the code, and factual. If they don't meet those conditions, then they need to be removed or reworded. If they provide information not already in the documentation comments that would be useful to the developer using the API, consider moving them to documentation comments.

## 5. Review Checklist

Use this checklist to verify your documentation:

1.  [ ] **Summary**: Does every public member start with a one-sentence summary ending in a period?
2.  [ ] **Brevity**: Have you removed "This class..." or "This function..." fluff?
3.  [ ] **Completeness**: Are strict constraints (e.g., "must not be null") and exceptions documented?
4.  [ ] **Examples**: Consider adding a code sample for complex widgets or methods.

## 6. Language Specific Instructions

For detailed instructions on structure, linking, and framework-specific patterns, refer to the language guides:

- **Dart / Flutter**: [references/dart.md](references/dart.md)
- **TypeScript / JavaScript**: [references/typescript.md](references/typescript.md)
- **Python**: [references/python.md](references/python.md)
