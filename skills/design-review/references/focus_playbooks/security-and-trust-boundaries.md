# Security and Trust Boundaries

Playbook id: `security-and-trust-boundaries`. Primary principles: `boundaries-state`, `resilience-ops`. Lane for merges: A.

## Cues (for triage)

- Parsing or executing untrusted inputs (repository files, Markdown links, CLI args, webhooks, URLs, or LLM/subagent outputs).
- Subprocess execution, file-system writes, runtime-constructed SQL/DuckDB queries, network fetches, or IPC/RPC calls across privilege boundaries.
- Credential storage, token propagation, PII handling, or temporary scratch/artifact file creation.

## Failure modes

1. Untrusted strings from files, user inputs, or model outputs flow into `subprocess` with `shell=True`, interpolated SQL strings, or template evaluators without strict parameterization or argument-list isolation.
   Look at: `subprocess.run`, `os.system`, DuckDB/SQLite query formatting, and command builders in specs or scripts. Settles it: trace taint from external text to execution sink and confirm `shell=False` argv lists or parameterized queries; `Read` or `Deduction`.
2. A privileged tool, sidecar, or script operates as a confused deputy by resolving user-supplied or doc-supplied relative paths, `file://` URIs, or symlinks without canonical path containment, allowing reads or writes outside the intended workspace or scratch directory.
   Look at: `Path.resolve()`, symlink handling, archive extraction, and `--output-dir` / `--root` path validation. Settles it: trace what happens when an input path contains `../` or a symlink pointing outside `--root`; `Deduction`.
3. Authentication context, user identity, or read-only permission mode is enforced at the outer entry point but dropped when delegating to a background daemon, subagent, or internal helper script.
   Look at: cross-process calls, subagent tool allowlists, and delegation boundaries in sequence diagrams. Settles it: trace whether the callee independently enforces the read-only or principal restriction; `Read`.
4. Ingestion or staging of untrusted inputs (large repositories, deeply nested Markdown, recursive links, or unindexed graphs) runs without timeouts, file-count caps, or byte limits, enabling denial of service or context-window exhaustion.
   Look at: directory walkers, subprocess calls, network fetch counts, and recursion bounds. Settles it: check every external call or loop for explicit timeout, max-depth, and max-bytes constants; `Read`.
5. Error logs, crash dumps, JSON exports, or world-readable scratch files include raw API tokens, environment secrets, or sensitive personal data.
   Look at: exception formatters, debug logging, temp file locations, and artifact outputs. Settles it: trace which environment variables, headers, or payloads can appear in persisted outputs or stdout/stderr; `Read`.
6. Permission or validation checks fail open inside `try/except` blocks or fallback branches when a helper script, linter, or policy lookup returns an error or empty output.
   Look at: error-handling branches around verification gates, linters, and access checks. Settles it: trace the caller's decision when the validator exits non-zero or raises an exception; `Deduction`.

## Hypothesis templates

- "Untrusted string `<input_source>` reaches `<execution_or_query_sink>` at `<path:line>` without argv separation or SQL parameterization."
- "Path resolver `<function>` accepts `<input_path>` without verifying `.resolve().is_relative_to(<allowed_root>)`, allowing traversal outside the workspace."
- "Delegating `<task>` from `<outer_caller>` to `<subagent_or_script>` drops the read-only restriction on `<resource>`."
- "Processing `<unbounded_input>` in `<component>` has no timeout or size ceiling, risking memory or turn exhaustion on large repositories."
- "When validation step `<check>` fails or throws at `<path:line>`, the fallback branch treats the check as passing instead of blocking."
