# Schema and Format Migration

Theme id: `schema-and-format-migration`. Lane for merges: B.

## Cues (for triage)

- Changes to database migrations, SQL schemas, Protobuf definitions, JSON/YAML schemas, or binary wire formats.
- Serialized struct fields are added, removed, renamed, retyped, or renumbered.
- Reader, writer, or parser code changes alongside stored or transmitted data definitions.

## Failure modes

1. New code reads existing persisted records or in-flight messages that lack a newly added field and fails or uses a wrong default.
   Look at: deserializers, ORM models, and proto/JSON readers handling records written before the change. Settles it: trace deserialization of an old payload missing the new field; `Deduction` or `Executed` under `--verify`.
2. An older binary or service still running during rollout receives data written in the new format and rejects or corrupts it.
   Look at: strict unknown-field rejection, required-field removals, or new enum values sent to existing consumers. Settles it: read consumer parsing configs and schema options across the repo; `Read`.
3. Renaming a serialized field or reusing a numeric tag silently drops persisted data or misinterprets wire bytes (especially when tests replace writer-to-reader roundtrips with writer-only checks).
   Look at: JSON keys, proto tag numbers, database column renames, `reserved` declarations, and modified roundtrip tests. Settles it: compare old and new field names, wire tags, and reader keys side by side; `Read`.
4. A migration script or backfill is not idempotent and fails or duplicates data if re-run after a partial failure.
   Look at: `CREATE`, `ALTER`, `INSERT`, or backfill statements and their conflict or existence guards. Settles it: trace running the migration twice against a partially migrated state; `Deduction`.
5. A destructive schema change (`DROP COLUMN`, type narrowing, `NOT NULL`) lands together with the code change before readers stop querying the old shape.
   Look at: migration ordering relative to application queries that still reference the column or constraint. Settles it: `rg` the codebase for all references to the altered column or field; `Read`.

## Hypothesis templates

- "Deserializing a pre-migration `<record_type>` without field `<new_field>` causes `<reader>` to fail or default to `<wrong_value>`."
- "Renaming serialized field `<old_name>` to `<new_name>` without an alias drops existing values stored in `<storage>`."
- "Running migration `<migration_id>` a second time after a failure at `<step>` raises an error or duplicates rows."
- "Old readers still deployed during rollout reject `<new_enum_or_field>` produced by `<writer>`."
