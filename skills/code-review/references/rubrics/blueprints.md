# Rubric: Blueprints

Use this rubric for blueprints: normative implementation specs that describe a module, feature, or codebase in enough detail for a person or a coding agent to implement it, often in several languages. A blueprint sits between a protocol spec and the code. The main question is: would independent implementations built from this blueprint behave the same way, and does the blueprint agree with the spec above it and the code below it?

Read [reviewing_documents.md](../reviewing_documents.md) first for the lenses, categories, evidence rules, and severity scale. Blueprints are normative, so also apply the statement-level and RFC 2119 checks in [normative_specs.md](normative_specs.md).

## Goldfish Test for Agent Implementers

Assume the implementer is a coding agent with no memory of past discussions, no access to the author, and only this blueprint plus the files it links to.

- Every interface has a signature: names, parameter and return types, and error types.
- Every operation states its preconditions, postconditions, and what happens when a precondition fails.
- State machines list every state and transition, including the error and terminal states.
- Defaults, limits, and ordering rules are stated, not implied by one implementation.
- Anything left to the implementer is marked as implementation-defined, with the range of allowed behavior.

A gap that would let two agents build different observable behavior is `incompleteness` or `ambiguity`, `high` on a core path.

## Language Neutrality

- Behavior is described in terms every target language can express. A blueprint that relies on one language's idioms (exceptions vs result types, nullable vs optional, async models, integer overflow behavior) without saying how other languages map it is `ambiguity`.
- Names that must match across languages (wire names, event names, error codes) are separated from names each language may adapt to its own conventions.
- Numeric types, string encoding, and collection ordering are specified where they affect observable output.

## Upstream Spec Is the Authority

- The governing spec (protocol spec, JSON Schema) is upstream. The blueprint must not contradict it or quietly extend it.
- A blueprint rule that is stricter or looser than the spec is `drift`, unless the blueprint says why and marks it as a deliberate profile.
- When the diff changes a blueprint because the spec changed, check that every affected section changed, not only the one the author noticed. Use the downstream impact pass.

## Stable Structure

- Section IDs and headings are stable because code comments, tests, and other blueprints cite them. Renaming or renumbering a section breaks those references. Report it as `structure`, and list each broken inbound reference in the Structural Assessment.
- Normative content lives in one blueprint. The same rule restated in two blueprints drifts over time. Duplicated normative content is `medium` `structure`. Suggest a link instead.
- Frontmatter (name, type, description) matches the file's role and the repo's conventions.

## Traceability and Drift

- Each feature the blueprint claims has at least one testable statement, and ideally a conformance or unit test that cites it (`traceability`).
- The blueprint agrees with the reference implementation. When they differ, report `drift` and say which side looks wrong and why. Don't assume the code is right.
- Code, tests, or skills that cite the blueprint by path or section are candidates for the downstream impact pass.

## Repo Tooling

If the repo ships a blueprint validator, its output is evidence. Run it in check mode (for example, `python3 blueprints/validate_blueprints.py`). Report the result in the Structural Assessment.
