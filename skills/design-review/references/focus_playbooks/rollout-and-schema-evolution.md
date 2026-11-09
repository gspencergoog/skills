# Rollout and Schema Evolution

Playbook id: `rollout-and-schema-evolution`. Primary principles: `evolution-fitness`, `resilience-ops`. Lane for merges: A.

## Cues (for triage)

- Additions, removals, renames, or type changes in SQL tables, JSON/YAML artifacts, Protobuf messages, or state files.
- Staged deployments, multi-wave migrations, backfill jobs, or versioned wire contracts.
- Feature flags, configuration switches, or deprecations altering runtime behavior.

## Failure modes

1. A schema migration drops, renames, or narrows a column or JSON key in a single step instead of an expand-contract sequence, breaking callers or in-flight jobs that still read the old field name.
   Look at: migration steps, schema diffs, and rollout wave ordering in the spec or repository. Settles it: `rg` for all readers of the altered field and compare their update timing against the schema change; `Read` or `Executed`.
2. New code reads persisted records or state files written before the upgrade and crashes or silently misclassifies records because newly required fields lack a backward-compatible default or migration backfill.
   Look at: deserializers, dataclass/schema loaders, and SQL queries reading existing state files or tables. Settles it: trace loading a pre-change record missing the new field through the proposed reader; `Deduction`.
3. Older readers still active during a staged rollout or on an un-upgraded client reject payloads written by the new version due to strict unknown-field rejection or unguarded new enum values.
   Look at: consumer parsers, enum switch statements, and schema validation flags across the codebase. Settles it: inspect reader parsing rules for unknown fields or unrecognized enum variants; `Read`.
4. Rolling back the binary or reverting the commit after the new version has written data leaves persisted records in a format that the previous version cannot parse or that corrupts older state loaders.
   Look at: rollback sections, state file schemas, and destructive write paths. Settles it: trace running the previous version's reader against a record mutated by the new version; `Deduction`.
5. Backfill or migration scripts are not idempotent, so re-running them after a partial failure raises constraint errors, duplicates rows, or overwrites newer live writes.
   Look at: DDL/DML migration statements, startup upgrade hooks, and backfill loops. Settles it: trace executing the migration a second time after a crash halfway through the batch; `Deduction`.
6. A high-risk behavioral cutover lacks a runtime kill switch or feature flag, forcing a full code revert and redeploy if production latency or error rates spike.
   Look at: rollout plan, configuration parameters, and branching logic around the new code path. Settles it: check whether the old path remains selectable via flag or config until the rollout stabilizes; `Read`.

## Hypothesis templates

- "Renaming or removing `<field_or_column>` in `<schema>` without an expand-contract transition breaks `<existing_reader>` at `<path:line>`."
- "Loading a pre-upgrade `<record_or_state_file>` that lacks `<new_field>` causes `<loader>` to fail or default to `<wrong_state>`."
- "Reverting to the prior release after `<new_writer>` persists `<new_format>` poisons `<old_reader>` because `<old_parser>` rejects `<field_or_enum>`."
- "Re-running `<migration_step>` after an interrupted backfill duplicates or corrupts records in `<table_or_file>`."
- "The cutover from `<old_mechanism>` to `<new_mechanism>` provides no flag or fallback to restore the old path without reverting the deployment."
