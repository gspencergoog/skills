# Reviewing Documents

This reference covers document-type files in a review: normative specifications, design docs, blueprints, and reference docs. For these files the review looks at structure, correctness, completeness, consistency, verifiability, and downstream impact. Wording and grammar findings are still reported, but always at `low` severity.

Code files keep using [review_criteria.md](review_criteria.md). A mixed diff gets both: the code mandate first, then the document mandate.

## When Document Mode Applies

Run [classify_artifacts.py](../scripts/classify_artifacts.py) on the changed file list. It prints one JSON object per file with its `type`, the `rule` that matched, and the `rubric` to load.

```bash
git diff --name-only <base>...HEAD | python3 <skill-dir>/scripts/classify_artifacts.py --root <repo-root> -
```

Only prose files (`.md`, `.rst`, `.adoc`, `.txt`) and JSON Schema files can be documents. JSON Schema files are `*.schema.json` files, or `.json` files whose `"$schema"` names a `json-schema.org` meta-schema. Every other file is `code`, which keeps `spec/` test directories, `*.spec.ts`, and design-token code out of document mode. For eligible files, the first matching rule wins:

| Rank | Rule | Type | Rubric |
| :--- | :--- | :--- | :--- |
| 1 | Frontmatter `type: module`, `feature`, or `codebase`, or filename `*.blueprint.md` | `blueprint` | [blueprints.md](rubrics/blueprints.md) |
| 2 | A JSON Schema file | `normative-spec` | [normative_specs.md](rubrics/normative_specs.md) |
| 3 | At least 2 design headings (Alternatives considered, Non-goals, Decision, Drawbacks, Unresolved questions, Consequences, `Status:`), or a path under `rfcs/`, `adr/`, or `proposals/` | `design` | [design_docs.md](rubrics/design_docs.md) |
| 4 | At least 3 lines with uppercase RFC 2119 keywords (`MUST`, `SHALL`, `SHOULD`, `MAY`, and so on), or a path under `spec/`, `specs/`, or `specification/` | `normative-spec` | [normative_specs.md](rubrics/normative_specs.md) |
| 5 | `CHANGELOG*` | `reference-doc` | [reference_docs.md](rubrics/reference_docs.md) |
| 6 | Any other eligible file (`README`, `docs/`, `SKILL.md`, `AGENTS.md`, plans) | `reference-doc` | [reference_docs.md](rubrics/reference_docs.md) |

Headings and keywords inside fenced code blocks don't count. If the classifier gets a file wrong (for example, a guide that quotes many `MUST` lines), override it and say so in the reviewer prompt.

## The Four Lenses

The lenses come from perspective-based reading: reviewers who each take one stakeholder's view find more defects, with less overlap, than reviewers who work through one generic checklist. Each lens is a procedure. Build a model of the document from that point of view, and report where the model breaks.

1. **Implementer**: Could you build this from the document alone, with no access to the author (the "Goldfish test")? Would two independent implementers produce the same observable behavior?
   - List every entity, operation, state, and error the document names.
   - Flag operations with undefined inputs, outputs, or error behavior.
   - Flag choices the document leaves open without saying they are open.
2. **Tester / Verifier**: Can you write a pass/fail test for each normative statement, and does something trace to it?
   - Turn each `MUST` and `SHOULD` into a one-line test sketch.
   - Flag statements you can't test ("fast", "user-friendly", "handles errors gracefully").
   - Flag missing negative cases, and examples that don't match the schema or the prose.
   - Flag requirements that no test, task, or conformance case covers.
3. **Consumer**: Does this solve the stated problem, and is the surface coherent?
   - Walk the main use cases end to end.
   - Check problem/solution fit, naming and terminology consistency, and extension points.
   - Check that diagrams match the prose, and that goals and non-goals match what is delivered.
4. **Operator / Adversary**: How does this fail, change over time, or get abused?
   - Check failure modes and recovery.
   - Check security and privacy considerations.
   - Check versioning, forward and backward compatibility, and unknown-field handling.
   - Check migration, rollout and rollback, and observability.

Without `--panel`, one reviewer applies all four lenses in order. With `--panel`, reviewer A applies Implementer and Tester, and reviewer B applies Consumer and Operator (see SKILL.md Step 3).

## Finding Categories

Every document finding gets one `Category`:

| Category | Meaning |
| :--- | :--- |
| `ambiguity` | Two plausible readings lead to different implementations. |
| `inconsistency` | Two statements conflict, in the same document or across artifacts. |
| `incompleteness` | A plausible input, state, or error has no defined behavior. |
| `unverifiable` | No pass/fail test can check the statement. |
| `infeasible` | The requirement can't be met as written, or conflicts with a stated constraint. |
| `rationale-gap` | A key decision has real alternatives but no stated reason. |
| `compatibility` | The change breaks existing implementations, data, or clients without a version or migration story. |
| `traceability` | A requirement has no test or task, or a test or task points at nothing. |
| `drift` | The document and the code, schema, or governing spec disagree. |
| `structure` | Wrong section, duplicated normative content, broken cross-reference, or normative and informative text not labeled. |
| `wording` | Phrasing, grammar, or style that doesn't change meaning. Always `low`. |

### Wording

Keep reporting wording and grammar issues, at `low`, sorted last. When a wording problem has two readings that lead to different behavior, it is not wording: report it as `ambiguity` with a correctness severity.

## Evidence and Anchoring

- **Quote the text.** Every document finding uses the universal `Evidence` field ([evidence.md](evidence.md#tiers)) at the `Read` tier (or `Fetched` for linked web docs), quoting the text it is about with its `path:line` location (`Evidence: Read — "…" (path:line)`). An `inconsistency` finding quotes both sides with both locations. Findings without a quote are dropped in critique. This rule exists because models find ambiguity and inconsistency well but invent problems when they don't cite the source.
- **Anchor to changed lines.** Each finding's `Line` is a changed line (`+` or `-`) in the new file. You may quote unchanged text as evidence. When a change contradicts unchanged text, anchor the finding to the changed line.
- **Unchanged files go in the Structural Assessment.** A finding that lives only in an unchanged file (for example, an inbound link broken by a rename) goes in the Structural Assessment, not in the per-line comments.
- **Check before claiming a gap.** Before reporting an `incompleteness`, `traceability`, or "undefined" finding, search the rest of the document, its parent document, and its linked files. Drop the finding if the term is defined elsewhere, or if the document explicitly defers or scopes out the gap and the deferral causes no concrete risk.
- **Specific questions only.** A design doc finding may be a question if it is specific ("What happens to in-flight requests when the flag flips?"). Drop generic questions ("Have you considered performance?").
- **Name the risk.** Drop "add a section on X" unless the missing content causes a concrete risk. Name that risk in the finding's `Why` field.

## Severity

| Severity | Examples |
| :--- | :--- |
| `critical` | A normative contradiction on a core path. Two compliant implementations can't interoperate. A breaking change with no version bump or migration. A design that violates a stated goal or a security boundary. |
| `high` | Undefined behavior for plausible input. Missing error semantics. Schema and prose disagree. An example is invalid. A key decision has no rationale when real alternatives exist. A downstream artifact was not updated. |
| `medium` | Unquantified performance or scale claims. Missing non-goals or success metrics. A `MUST` vs `SHOULD` mismatch on an edge path. Duplicated normative content. A broken cross-reference. Terminology drift. |
| `low` | Wording, grammar, and style. Clarity improvements that don't change meaning. |

## Downstream Impact Pass

After the lens pass, answer one question: what else must change for this change to be complete, and did the diff change it? Check up to 10 candidate files, in this order:

1. Files the changed document links to.
2. Files that link to it. Search for its repo-relative path (for example, `rg -n "blueprints/modules/a2ui_core.blueprint.md"`), not its basename.
3. Files that mention the defined terms or section IDs the diff touched.

Candidates are files outside the diff; don't list the changed files themselves. Report each candidate in the Structural Assessment as updated in this diff, needs update, or not affected. A candidate that needs an update and is part of the change's contract (a schema, a test, a governing spec, or a code path the document describes) is also a `high` `drift` finding, anchored to the changed line that makes it stale.

## Repo Doc Validators

If the repo ships a validator for its documents (for example `validate_blueprints.py`, a link checker, or a JSON Schema example validator), the orchestrator runs it in check mode during Step 2 and passes the output to the reviewer. Treat its errors as evidence. Report the command and result in the Structural Assessment. Don't install dependencies to run a validator. If it can't run, say so.

## Output Additions

Document findings use the same fields as code findings (`File`, `Line`, `Severity`, `Evidence`, `Body`, `Why`, `Suggestion`), plus one document-only field:

- **Category**: one of the categories above (document files only).
- **Evidence**: `Read` (or `Fetched`) followed by the quoted text and its location. For `inconsistency`, both sides.
- **Suggestion** holds replacement text or a specific question instead of compilable code.

The report gains a `## Structural Assessment` section right after `## Summary`. It appears only when a document partition exists, so code-only reports don't change:

```markdown
## Structural Assessment
- **Implementer**: <one-line verdict>
- **Tester / Verifier**: <one-line verdict>
- **Consumer**: <one-line verdict>
- **Operator / Adversary**: <one-line verdict>
- **Repo validators**: <command> — <pass | N issues | none found>
- **Downstream impact**: <artifact> — <updated in this diff | needs update | not affected>
- **Unchanged-file findings**: <path:line> — <finding> (omit if none)
```

Each lens verdict names what the reviewer checked and what it found, for example "Checked each v1.0 example against `common_types.json`: all use `@path`; the builder section names a host-language member `DataBinding.@path` that can't exist (L2)". A bare "no issues" or a restatement of the change is not a verdict.

Under `--panel`, each reviewer fills in only its own lenses. Synthesis merges both into one section.
