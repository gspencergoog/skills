# Rubric: Normative Specifications

Use this rubric for protocol specs, API specs, file-format specs, and JSON Schema files. A normative spec is a contract between independent implementers. The main question is: can two teams implement it separately and still interoperate?

Read [reviewing_documents.md](../reviewing_documents.md) first for the lenses, categories, evidence rules, and severity scale.

## Statement-Level Checks

Check each changed normative statement against the characteristics in ISO/IEC/IEEE 29148 (requirements engineering):

| Characteristic | Question | Category if it fails |
| :--- | :--- | :--- |
| Necessary | Would removing it lose a capability or constraint someone relies on? | `structure` |
| Appropriate | Is it at the right level (behavior, not one implementation's internals)? | `structure` |
| Unambiguous | Does it have exactly one reasonable reading? | `ambiguity` |
| Complete | Does it say what happens for every input it covers, including errors? | `incompleteness` |
| Singular | Does it state one requirement, not several joined with "and"? | `structure` |
| Feasible | Can it be built within the stated constraints? | `infeasible` |
| Verifiable | Can a test decide pass or fail? | `unverifiable` |
| Correct | Does it match the governing source (upstream spec, schema, reference implementation)? | `drift` |
| Conforming | Does it follow the document's own conventions (keywords, IDs, templates)? | `structure` |

## Set-Level Checks

Check the spec as a whole, focusing on the parts the diff touches:

- **Complete**: every message or input, in every state, has defined behavior. That includes malformed input, unknown fields, out-of-order messages, and duplicates.
- **Consistent**: no two statements conflict. Check the changed text against unchanged sections, the schema, and the examples.
- **Feasible as a set**: the requirements can all hold at once (for example, a latency bound and a retry policy that together can't be met).
- **Comprehensible**: terms are defined before use and used with one meaning. A glossary term used with a second meaning is `inconsistency`.
- **Able to be validated**: a conformance suite could be built from it. Invariants are stated explicitly, not only implied by examples.

## RFC 2119 and RFC 8174 Discipline

- Only the uppercase keywords (`MUST`, `MUST NOT`, `SHALL`, `SHOULD`, `SHOULD NOT`, `MAY`, `REQUIRED`, `RECOMMENDED`, `OPTIONAL`) are normative. Lowercase "must" is ordinary English.
- Flag lowercase "must" or "should" in a sentence that is clearly meant to be normative. That is `ambiguity`, because implementers can't tell whether it binds them.
- Flag the same behavior stated as `MUST` in one place and `SHOULD` in another. That is `inconsistency`, `high` on a core path and `medium` on an edge path.
- Every `SHOULD` needs a reason an implementer might not follow it, or it should be a `MUST`. Every `MAY` needs defined behavior for peers that don't do it.
- Normative text and informative text (examples, notes, rationale) are labeled or clearly separated. An example that contradicts normative text is `high` `inconsistency`, and the normative text wins unless the diff says otherwise.

## Schema, Prose, and Examples

- The schema, the prose, and every example agree on field names, types, required vs optional, defaults, enum values, and constraints.
- Each example validates against the schema. If the repo has a schema validator, its output is evidence.
- A field added to the schema is described in the prose, and a field described in the prose exists in the schema.
- Changing a field from optional to required, removing an enum value, narrowing a type, or renaming a field is a breaking change. See the next section.

## Versioning, Extension, and Compatibility

- A breaking change has a version bump, a migration note, or a compatibility statement. A breaking change with none of these is `critical` `compatibility`.
- Unknown-field handling is defined (ignore, preserve, or reject) and stays the same across versions.
- Extension points say who may extend them, how conflicts are avoided (namespacing, prefixes, registries), and what a peer that doesn't recognize an extension does.
- Deprecations name what replaces the deprecated item and when it will be removed.

## Security and Privacy Considerations

Following RFC 3552, a protocol spec has a security considerations section, and changes that affect trust boundaries update it. Check:

- Which inputs come from untrusted parties, and what validation the spec requires for them.
- Injection, resource exhaustion (unbounded sizes, recursion depth, counts), replay, and confused-deputy risks.
- What data is sensitive, and where the spec allows it to be logged, cached, or forwarded.

## Traceability

- Requirements have stable IDs or anchors if the project uses them. Renaming or renumbering them breaks inbound references (`structure`, and a Structural Assessment entry for each broken reference).
- Each changed normative statement is covered by a conformance test or test case, or the report notes the gap (`traceability`).
