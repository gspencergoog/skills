# Trust Boundary Change

Theme id: `trust-boundary-change`. Lane for merges: B.

## Cues (for triage)

- Input validation, sanitization, authentication, or authorization checks are added, moved, weakened, or removed.
- New endpoints, RPC handlers, CLI flags, webhook receivers, or file importers accept external data.
- Untrusted strings flow into shell commands, file paths, SQL queries, URLs, or templates.

## Failure modes

1. Moving validation or authorization to a caller or middleware leaves other entry points calling the inner function without the check.
   Look at: every caller of the function from which the check was removed or weakened. Settles it: `rg` all call sites of the inner function and trace whether each path enforces the check; `Read`.
2. Authorization verifies that the caller is authenticated (or holds a role) but not that the caller owns the target resource ID.
   Look at: handlers taking a user-supplied resource ID, path parameter, or tenant key. Settles it: trace how the resource ID is scoped to the authenticated principal in the query or lookup; `Read`.
3. Untrusted input reaches a command runner, path join, query builder, or template renderer without parameterization or canonical path containment.
   Look at: string interpolation or concatenation feeding `exec`, `open`, `Path.join`, SQL, HTML, or redirect targets. Settles it: trace the taint path from external input to the sink with `path:line` citations; `Deduction`.
4. Error responses, debug logs, or telemetry include raw credentials, tokens, session cookies, or private keys on failure.
   Look at: log statements and error formatters touching request headers, config structs, or auth payloads. Settles it: read the logged or returned fields and trace what values they hold; `Read`.
5. A fallback or default branch fails open when an auth service, signature check, or policy lookup errors or returns empty.
   Look at: `catch` blocks, default switch arms, and nil/empty checks around permission evaluators. Settles it: trace the return value when the check throws or receives missing config; `Deduction`.

## Hypothesis templates

- "Removing `<check>` from `<inner_fn>` allows caller `<unauthenticated_caller>` to invoke it without authorization."
- "User-controlled `<param>` reaches `<sink>` at `<file:line>` without escaping or path containment."
- "Handler `<handler>` checks authentication but does not verify that `<resource_id>` belongs to the requesting principal."
- "When `<auth_or_policy_call>` fails at `<file:line>`, the fallback branch grants access instead of denying it."
