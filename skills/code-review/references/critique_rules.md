# Rules for Reviewing Reviews

This reference document provides guidelines for reviewing and filtering generated code review comments (the "review the review" step). Use these rules to ensure that only high-quality, actionable comments are included in the final output.

## Filtering Guidelines

A comment should be **dropped** if it meets any of the following conditions:

- It is not on a line that was actually changed (lines starting with `+` or `-` in the diff). Document findings may quote unchanged text as evidence, but they are still anchored to a changed line. Findings that exist only in unchanged files move to the Structural Assessment instead of being dropped.
- It is merely informational, explaining what the code does.
- It is complimentary (e.g., "Good job", "Nice fix").
- It tells the user to "check", "confirm", "verify", or "ensure" something without pointing to a specific issue.
- It is out of bounds for the line range allowed by the SCM API.

A comment should be **kept** or **modified** if:

- It identifies a real issue or bug.
- Its content can be made more concise or actionable.
- Its severity can be adjusted to better match the guidelines.

## Truth Filters

Apply these falsification rules (from [evidence.md](evidence.md)) to every candidate finding before keeping it:

- **Tier required, never `Recalled`**: Every finding must carry an `Evidence` field starting with `Executed`, `Read`, `Fetched`, `Deduction`, or `Speculation`. Drop any finding whose tier is `Recalled` or missing, unless you can verify the claim on disk or over the network first.
- **Severity obeys the evidence cap**: `critical` and `high` require `Executed`, `Read`, `Fetched`, or a `Deduction` trace where every step cites an exact `path:line`. Downgrade or convert to a Question when the evidence is `Speculation` or an incomplete trace.
- **Concrete `Why` required**: Every finding must include a `Why` field naming the concrete consequence (what breaks, corrupts, leaks, or fails). If you cannot state a concrete consequence, drop the finding or downgrade it to a Question.
- **Tree checked before claiming removal, misuse, or missing coverage**: Drop any claim that code or test coverage was deleted unless `rg` against `HEAD` confirmed it did not move. Drop any API-misuse or missing-argument claim unless you read the target signature and call sites in this session. Drop any "test pins incidental behavior" claim unless you checked the docstring or spec first.

## Questions

Keep `## Questions` (`Q1…`) tight and actionable:

- Keep a Question only when it names both a specific suspected claim and the exact check (`To settle`) that resolves it. "Verify that…" or "Make sure…" without a concrete claim and check is still dropped.
- Cap at 3 Questions per review. When candidates exceed 3, keep the ones with the highest stated consequence in `Why` and drop the rest.

## Severity Guidelines (Reminders)

Ensure severity levels are applied consistently:

- **Refactoring hardcoded strings/numbers**: Generally `low` severity.
- **Log messages or enhancements**: Generally `low` severity.
- **Comments on reference-doc prose** (READMEs, guides, and changelogs, not specs, design docs, or blueprints): Usually `medium` or `low` severity. Specs, design docs, and blueprints use the severity scale in [reviewing_documents.md](reviewing_documents.md#severity), where a contradiction can be `critical`.
- **Adding/expanding docstrings**: Usually `low` severity.
- **Suppressing warnings or TODOs**: Usually `low` severity.
- **Typos**: Usually `low` severity. In documents, typos and other wording issues are always `low`.
- **Test files**: Comments on tests are usually `low` severity unless they point to a critical gap in coverage. A gap proven by a mutation run (`Evidence: Executed`) where the test passes against broken production code may be `high`.

## Document Findings

For findings on document files, also apply these filters:

- **No evidence, no finding**: Drop a finding that doesn't quote the text it is about. Drop an `inconsistency` finding that doesn't quote both sides.
- **Check before claiming a gap**: Before reporting something as undefined, missing, or unaddressed, search the whole document, the document it is excerpted from, and the files it links to. A term defined elsewhere is not undefined. A gap the document explicitly defers or scopes out (for example, "add a category alongside the first suite that asserts it") is not a finding, unless the deferral itself causes a concrete risk. Name that risk.
- **No template-filling**: Drop "add a section on X" and other generic completeness comments unless the finding names a concrete risk the gap causes.
- **No generic questions**: Keep a question only if it is specific to this document. Drop "Have you considered performance?"
- **Wording stays low**: A `wording` finding is always `low`. If a wording issue has two readings that lead to different behavior, change its category to `ambiguity` and give it a correctness severity.
- **Unchanged files**: Move findings that exist only in unchanged files (for example, inbound links broken by a rename) to the Structural Assessment.
- **Category required**: Every document finding has exactly one category from [reviewing_documents.md](reviewing_documents.md#finding-categories).

## Panel Runs

Under `--panel`, each reviewer drops its own out-of-lane findings below `high`. Out-of-lane findings at `critical` or `high` are kept and tagged `out-of-lane` in their Body.

## Code Suggestion Quality

When reviewing code suggestions within comments, ensure:

- They are accurately anchored to the lines they intend to replace.
- They preserve the indentation and spacing of the original code.
- They are compilable or syntactically correct for the language.
- They are succinct and easy to understand.
