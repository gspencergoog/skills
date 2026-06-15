---
name: api-review
description: Reviews the specified code against the canonical API Design guidelines. Use this skill when the user asks for an API review or to check code against API design principles.
---

# API Review Skill

This skill reviews code against the canonical API Design guidelines.

## Instructions

1. **Load Guidelines**: Read the API design guidelines from [references/canonical_api_design.md](references/canonical_api_design.md) to ensure they are fully available in the context.
2. **identify Target**: Identify the code to review.
   - If the user specified files (e.g., "review main.dart"), use those.
   - If the user has an open file in their context, assume that is the target.
   - If neither, ask the user to specify the target files.
3. **Analyze**: For each target file, perform a deep analysis against the "Foundations of Canonical API Design Principles" (loaded in step 1), specifically looking for:
   - **Contract-First**: Is the interface clear and decoupled from implementation?
   - **KISS/YAGNI**: Are there unnecessary parameters or over-generalized features?
   - **Ergonomics**: Are names intent-revealing? Do they follow the Principle of Least Astonishment?
   - **CQS**: Are commands and queries separated?
   - **Safety**: Are types used strictly (Enums vs Strings)? Is validation visible?
4. **Report**: Generate a structured report:
   - **Score**: Give a rough letter grade (A-F) based on alignment.
   - **Critical Issues**: Violations that _must_ be fixed (e.g., severe strictness or safety issues).
   - **Suggestions**: Ergonomic improvements (renaming, rearranging).
   - **Code Examples**: Provide `before` vs `after` code blocks for the suggested improvements.
   - Write the report to a markdown file (e.g., `api_review_results.md`) for review.
