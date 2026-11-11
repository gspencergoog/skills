# Public API Surface and Contract Ergonomics

Playbook id: `api-surface`. Primary principles: `module-depth`, `ergonomics-coherence`. Lane for merges: B.

## Cues (for triage)

- Exported function, class, or method signatures, public SDK modules, and library entry points.
- Configuration records, option structs, builder types, and dependency injection boundaries.
- Public error hierarchies, status codes, pagination envelopes, and CLI or RPC request/response contracts.

## Failure modes

1. Public signatures or return types expose internal storage schemas, database rows, or representation types instead of domain capabilities (leaked internals in [canonical_api_design.md](../../../review-core/references/canonical_api_design.md)).
   Look at: exported return types, parameter types, and public struct or dataclass fields. Settles it: trace whether refactoring the backing store or parser forces callers to change; `Read`.
2. Functions or configuration records include speculative parameters, unused hooks, or multi-purpose flags with no current caller (YAGNI and KISS violations).
   Look at: optional parameters, configuration fields, and boolean mode switches on public entry points. Settles it: search all callers in `HEAD` with `rg` or `sem` to confirm whether the parameter has a real consumer; `Executed` or `Read`.
3. Boundary parameters accept raw strings, untyped maps, or magic numbers where a closed enum, literal union, or value type prevents invalid states.
   Look at: string parameters representing modes, statuses, kinds, or units in public signatures and schemas. Settles it: inspect the function body or schema validator for ad hoc string comparisons; `Read`.
4. A single operation mixes state mutation with data retrieval (command-query separation violation) or reads hidden global state, singletons, or environment variables instead of explicit parameters.
   Look at: getter or query methods that mutate state, and functions reading globals or `os.environ` directly. Settles it: trace whether calling the query twice alters state or depends on ambient process globals; `Read` or `Deduction`.
5. Collection endpoints or list functions return unbounded results or use offset pagination instead of cursor pagination, degrading at scale or skipping items under concurrent writes.
   Look at: list/search signatures, pagination parameters (`offset`, `page`, `limit`), and response envelopes. Settles it: trace behavior when rows are inserted or deleted between pages; `Read` or `Deduction`.
6. Public entry points report failures through generic booleans, raw string messages, or undocumented exceptions instead of structured, machine-actionable error types or codes.
   Look at: `raise` / `throw` sites, error return types, and RPC error schemas. Settles it: check whether a caller can distinguish retryable vs. permanent or input-validation errors programmatically; `Read`.

## Hypothesis templates

Pair every confirmed finding from these templates with a before-and-after signature block in `- **Suggestion**:`:

- "Public entry point `<symbol>` in `<path>` leaks internal type `<internal_type>` in its signature, coupling callers to the storage representation."
- "Parameter `<param>` on `<symbol>` has zero callers in `HEAD` and adds speculative configuration surface; remove it or fold it into a narrower signature."
- "Entry point `<symbol>` accepts raw `str` for `<parameter>` instead of a closed enum or literal union, allowing invalid values past the boundary."
- "Function `<symbol>` both mutates `<state>` and returns a query result (or reads hidden global `<global>`), violating command-query separation and explicit configuration."
