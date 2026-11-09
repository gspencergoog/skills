# Module and Plugin Boundaries

Playbook id: `module-and-plugin-boundaries`. Primary principles: `module-depth`, `boundaries-state`, `evolution-fitness`. Lane for merges: B.

## Cues (for triage)

- New packages, modules, services, or directory splits, or refactoring of existing module boundaries.
- Extension points, plugin hooks, callback interfaces, or adapter layers.
- Shared registries, dependency injection wiring, or cross-layer data structures.

## Failure modes

1. Splitting a subsystem introduces direct or transitive import cycles between packages or modules, preventing independent testing and blurring dependency direction.
   Look at: component diagrams, import statements, and `sem` dependency graph outputs in `design_context.json`. Settles it: trace the module dependency DAG via `Executed` (`sem` / `rg` imports) or `Read` on the component diagram to verify zero cycles.
2. A new wrapper module, facade, or controller is a shallow pass-through that duplicates its callee's signature without hiding complexity or enforcing invariants.
   Look at: public function signatures, parameter forwarding chains, and class interfaces. Settles it: compare the proposed interface against its underlying implementation complexity (Ousterhout depth test); `Read`.
3. Two modules share unencapsulated knowledge of the same file layout, SQL table structure, regex, or wire detail (information leakage), so a single format change requires coordinated edits across both modules.
   Look at: duplicated constants, file parsers, query builders, or schema assumptions across separate files. Settles it: check whether format knowledge is isolated inside one owning module in the spec and repo; `Read`.
4. Modules are split by execution order (such as `read_step`, `transform_step`, `write_step`) rather than by the secret each module hides (temporal decomposition), scattering format invariants across all steps.
   Look at: pipeline stage boundaries and data structures passed between sequential modules. Settles it: trace how many modules must change when one record field or invariant changes; `Deduction`.
5. Extension hooks or plugin registries rely on import-time side effects or process-global mutable state instead of explicit registration or constructor injection, causing test pollution and load-order bugs.
   Look at: global dictionaries, singleton initializers, and plugin discovery mechanisms. Settles it: trace running two tests with different plugin sets in the same process; `Read` or `Deduction`.
6. Architectural boundary rules (such as "core never imports UI" or "skills never cross-link private helpers") rely on reviewer memory instead of an automated architecture fitness test in CI.
   Look at: test plan, verification commands, and linter rules in the design or package. Settles it: check whether a runnable unit test or script enforces the boundary constraint; `Read`.

## Hypothesis templates

- "Module `<module_a>` depends on `<module_b>` while `<module_b>` imports `<module_a>` (directly or via `<module_c>`), creating a dependency cycle."
- "Proposed `<wrapper_or_layer>` forwards parameters directly to `<callee>` without hiding policy or state, adding a shallow pass-through boundary."
- "Both `<module_a>` and `<module_b>` encode the internal structure of `<format_or_table>`, leaking implementation details across the boundary."
- "Plugin registry `<registry>` mutates process-global state at import time, causing load-order sensitivity and cross-test leakage."
- "The dependency rule forbidding `<package_a>` from importing `<package_b>` has no automated fitness test to prevent regression after merge."
