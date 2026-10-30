# Deletion and Consolidation

Theme id: `deletion-and-consolidation`. Lane for merges: A.

## Cues (for triage)

- Diff mostly deletes files, functions, classes, exports, or configuration keys.
- Two or more implementations, modules, or CLI commands are merged into a single shared path.
- Test files or test cases are deleted alongside refactored or consolidated code.

## Failure modes

1. A deleted function, type, file, or export is still referenced by code, build rules, scripts, or string-based lookups (reflection, registries, config keys) in `HEAD`.
   Look at: all deleted symbol names, file paths, and string-based registrations across the repository. Settles it: `rg` every deleted symbol and path across `HEAD`; `Read`.
2. Deleted test cases covered edge cases or regression scenarios that have no equivalent coverage in the remaining test suite.
   Look at: each deleted test assertion and the surviving tests for the consolidated module. Settles it: `rg` the surviving test suite for the specific input or condition tested in the deleted test; `Read`.
3. Consolidating two implementations into one silently drops a branch, option, error code, or caller-specific behavior from one of the originals.
   Look at: the diff between each deleted implementation and the unified replacement, plus all redirected call sites. Settles it: compare inputs, branches, and return values of the deleted function against the replacement; `Read` or `Deduction`.
4. Configuration files, dependency manifests, router tables, or documentation still reference the removed command, flag, or module path.
   Look at: READMEs, CI workflows, config templates, exports in `index`/`mod` files, and package entry points. Settles it: `rg` the removed identifier across docs, configs, and manifests in `HEAD`; `Read`.
5. Moving or renaming a public symbol changes its import path or visibility without updating re-exports or downstream consumers in the workspace.
   Look at: barrel files (`__init__.py`, `index.ts`, public library headers) and workspace callers. Settles it: `rg` the old import path across the repository; `Read`.

## Hypothesis templates

- "Deleted symbol `<symbol>` is still referenced at `<file:line>` in `HEAD`."
- "Deleted test `<deleted_test>` covered `<scenario>`, which is not tested anywhere else in `HEAD`."
- "Replacing `<old_fn>` with consolidated `<new_fn>` drops handling for `<branch_or_option>` expected by `<caller>`."
- "Config or documentation file `<doc_or_config>` still references removed `<flag_or_path>`."
