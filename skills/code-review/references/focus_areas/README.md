# Focus Area Catalog

A focus theme is a cross-cutting mechanism change in a diff whose failure modes are non-local: reading each hunk in isolation would not reveal them. `code-review` Step 2d (focus triage) reads this index to recognize familiar themes quickly, and a focus reviewer reads the matching playbook before the diff. See [lanes.md](../lanes.md) for how themes become focus reviewers or Focus Priority blocks.

## How Triage Uses This Catalog

- Triage starts from the diff, not from this list. It asks which single change, if subtly wrong, would be hardest to catch by reading each hunk on its own, and only then looks for a matching id.
- Prefer a catalog id over free text when one fits, so the same mechanism gets the same name across reviews and playbook proposals extend the right file.
- The catalog never limits what triage may name. A mechanism with no entry becomes an ad hoc theme (`adhoc:<slug>`) with an inline playbook of at most 1 KB, and Step 5b proposes it for the catalog.
- API contract changes are not a theme here; the API Review Mandate already covers them.

## Index

| id | Cues | Lane for merges |
| :--- | :--- | :--- |
| `sync-to-async` | Signatures gain `async`, `Future`, `Promise`, `Task`, or coroutine types; callbacks become awaits; blocking I/O moves onto an event loop | A |
| `concurrency-and-locking` | New locks, mutexes, atomics, isolates, threads, or shared mutable state; changed lock scope or order | A |
| `error-handling-and-retries` | Changed exception types or error returns at a boundary; new retry loops, backoff, or fallbacks; catch blocks added or removed | A |
| `resource-lifecycle` | New open, acquire, subscribe, or allocate calls; changed `close`, `dispose`, `finally`, `with`, or `defer` handling; ownership moves | A |
| `schema-and-format-migration` | Changed schemas, protos, serialized fields, file formats, database columns, or migration scripts | B |
| `trust-boundary-change` | Validation, authentication, or authorization moved, added, or removed; untrusted input reaching shell, path, query, or template code | B |
| `dependency-and-toolchain-change` | Version bumps in manifests or lockfiles; new dependencies; compiler, SDK, or build tool upgrades | B |
| `caching-and-invalidation` | New caches, memoization, or TTLs; changed cache keys; writes that should invalidate cached reads | B |
| `time-locale-and-encoding` | Date, time zone, duration, locale, number formatting, string length, or encoding changes | A |
| `feature-flag-and-rollout` | New or removed flags, experiments, config switches, or staged rollout gates | B |
| `deletion-and-consolidation` | Mostly deletions, moves, or merged files; removed public symbols or test files | A |

## Playbook Format

Each playbook lives at `references/focus_areas/<id>.md`, is at most 80 lines so a reviewer reads it in one call, and uses this shape:

```markdown
# <Theme title>

Theme id: `<id>`. Lane for merges: A | B.

## Cues (for triage)

<2 to 4 lines of signals in a diff that suggest this theme>

## Failure modes

1. <Defect pattern in one sentence.>
   Look at: <where to look>. Settles it: <the cheapest read or command, with its evidence tier>.
2. ...

## Hypothesis templates

- "<falsifiable claim with <placeholders> for symbols from the diff>"
```

Failure modes are numbered because a focus reviewer reports `Failure modes checked: <numbers>` in its `## Playbook Feedback`, which is how Step 5 confirms the playbook was read.

## Proposals

Step 5b writes `focus_playbook_proposals.md` in the conversation artifact directory when a focus reviewer's feedback is not `None.` or when the theme was ad hoc. Each proposal is a ready-to-paste section or a whole playbook, with the evidence that settles each new failure mode. A review never edits this directory. Applying a proposal is a separate, user-approved change committed with the `commit-changes` skill.
