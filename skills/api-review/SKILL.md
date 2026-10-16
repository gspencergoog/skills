---
name: api-review
description: >-
  Reviews code against the canonical API design principles: contract-first
  interfaces, KISS and YAGNI, intent-revealing names, command-query separation,
  strict types over strings, visible validation, and lean public entry points,
  and reports findings with concrete rewrites. Use when the user asks for an API
  review, asks whether a public interface is well designed, or wants a class,
  module, or endpoint checked against API design principles before it ships.
  Don't use for a full code review of a change (use code-review), which already
  applies these principles to the modified lines.
---

# API review skill

This skill reviews code against the canonical API Design guidelines.

## Instructions

1. **Load Guidelines**: Read the API design guidelines from [references/canonical_api_design.md](references/canonical_api_design.md) to ensure they are fully available in the context.
2. **Identify Target**: Identify the code to review.
   - If the user specified files (e.g., "review main.dart"), use those.
   - If the user has an open file in their context, assume that is the target.
   - If neither, prompt the user (using your interactive question tool if available, or by asking in chat and waiting for their reply) to specify the target files.
3. **Analyze**: For each target file, perform a deep analysis against the "Foundations of Canonical API Design Principles" (loaded in step 1), specifically looking for:
   - **Contract-First**: Is the interface clear and decoupled from implementation?
   - **KISS/YAGNI**: Are there unnecessary parameters or over-generalized features?
   - **Ergonomics**: Are names intent-revealing? Do they follow the Principle of Least Astonishment?
   - **Complexity & Simplicity**: Are public methods and entrypoints lean and free of excessive internal branching or high cognitive complexity (refer to the [cognitive-complexity](../cognitive-complexity/SKILL.md) skill)?
   - **CQS**: Are commands and queries separated?
   - **Safety**: Are types used strictly (Enums vs Strings)? Is validation visible?
   - **Explicit Configuration**: Are dependencies explicitly injected rather than implicitly resolved via global state, registries, or environment variables?
4. **Report**: Generate a structured report:
   - **Score**: Give a rough letter grade (A-F) based on alignment.
   - **Critical Issues**: Violations that _must_ be fixed (e.g., severe strictness or safety issues).
   - **Suggestions**: Ergonomic improvements (renaming, rearranging).
   - **Code Examples**: Provide `before` vs `after` code blocks for the suggested improvements.
   - Save the report as a markdown artifact in the conversation artifacts directory (e.g., `<appDataDir>/brain/<conversation-id>/api_review_results.md`).
   - When the review recommends tuning specific signatures, route paths, or parameter defaults with competing ergonomic tradeoffs and the `a2ui-pane` plugin is available, also read the `genui` skill and render an inline `contract_workbench` or `consequence_cards` surface alongside the artifact link so the user can interactively select the target signature; skip the surface when `a2ui-pane` is not installed.
