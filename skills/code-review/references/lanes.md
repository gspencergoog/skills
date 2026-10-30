# Reviewer Roster and Lanes

This reference defines who reviews a change in the [`code-review`](../SKILL.md) skill, what each reviewer is asked to cover, and how their reports merge. `SKILL.md` Step 3 composes the roster from this file, and Step 5 merges the reports with the rules at the end.

## Reviewer Roster

| Reviewer | Lane spec | Model (Tier 1 `swarm`) | Present when |
| :--- | :--- | :--- | :--- |
| `rev-a` | `general`, or `A` under `--panel` | `gemini-launch-candidate-high` | Every review except `--fast` |
| `rev-b` | `B` | `gemini-launch-candidate-high`, or `opus-5.5-max` with `--claude` / `--opus` | `--panel`, `--max` |
| `rev-f`, `rev-f2`, `rev-f3` | `focus:<theme-id>` (one theme each) | `gemini-launch-candidate-high` | A strong Step 2d verdict or a `--focus` flag, within the [depth cap](#depth-cap) |

Naming and reuse rules:

- Reuse a `waiting` worker by waking it with `post_message` and `swarm_wait{since: <last_seq>}`. Never `kill` and `add` between passes: `kill` burns the roster name permanently.
- If a worker was killed, add its replacement with an incremented suffix (`rev-a-2`, `rev-f-2`).
- `--claude` and `--opus` pin only the single reviewer, or `rev-b` under `--panel`. Focus reviewers stay on Gemini to protect third-party quota.
- Quota fallback order: `opus-5.5-max` to `gemini-launch-candidate-high`, then `gemini-launch-candidate-high` to `auto` (Gemini Pro Next, a separate bucket). Apply it at spawn when `swarm{action: "models"}` shows zero remaining quota, or in place with `swarm{action: "model"}` when a worker fails with `RESOURCE_EXHAUSTED`.
- On Tier 2 (subagents), give each reviewer the role `Code Reviewer`, `Code Reviewer A`, `Code Reviewer B`, or `Focus Reviewer (<theme-id>)`.
- Before launching more than one reviewer, state the total in chat (for example, "Launching 3 reviewers: rev-a, rev-b, rev-f (sync-to-async)").

## Lane Specs

`--panel` means the user accepts about twice the reviewer quota for better coverage. Both panel reviewers get the full diff and the same optional blocks. Each also gets a Lane Assignment block, so their criteria do not overlap:

| Lane | Theme | Code criteria | Document lenses | Reviewer |
| :--- | :--- | :--- | :--- | :--- |
| `general` | Everything | All criteria in [review_criteria.md](review_criteria.md) | All four lenses | `rev-a` |
| `A` | Does it work, and can it be verified? | Correctness; concurrency and failure modes; edge cases; tests ([reviewing_tests.md](reviewing_tests.md)) | Implementer; Tester / Verifier | `rev-a` |
| `B` | Is it safe to ship and to maintain? | Security; maintainability and cognitive complexity; efficiency; API soundness ([reviewing_apis.md](reviewing_apis.md)) | Consumer; Operator / Adversary | `rev-b` |
| `focus:<theme-id>` | Does this one mechanism change hold up across the whole diff? | The theme's playbook failure modes only | None (code partition only) | `rev-f*` |

Lane Assignment block for reviewer A (swap in lane B's lists for reviewer B):

```markdown
You are reviewer A of two. Reviewer B covers the other criteria in parallel.
- Code criteria: Correctness; Concurrency & failure modes; Edge cases; Tests.
- Document lenses: Implementer; Tester / Verifier.
Hypotheses marked with your lane are yours. Report only findings in these areas. Exception: report a `critical` or `high` issue outside them, and tag its Body with `out-of-lane`.
In the Structural Assessment, fill in only your own lenses.
```

- For a code-only diff, omit the document lenses line. For a document-only diff, omit the code criteria line.
- For a split diff, launch one A/B pair per partition ([splitting_reviews.md](splitting_reviews.md)).
- Label each seeded hypothesis with its lane spec: `[A]` for correctness, state, edge cases, and tests; `[B]` for wiring, security, API, performance, and dependencies; `[focus:<theme-id>]` for theme hypotheses.

## Focus Blocks

Two different blocks carry a focus theme, and a brief never contains both. The **Focus Mandate** is narrow and goes only to a dedicated focus reviewer that works beside a general or panel reviewer. The **Focus Priority** is additive and goes to a general or lane reviewer when no focus reviewer covers the theme: a weak verdict, `--fast`, or a strong theme beyond the depth cap. A reviewer that is the only one reading a part of the diff is never narrowed.

Each block names exactly one playbook source. For a catalog theme it is the file path `<skill-dir>/references/focus_areas/<theme-id>.md`. For an ad hoc theme (no catalog entry) the orchestrator replaces the "Read ..." sentence with an inline `#### Ad hoc playbook` block of at most 1 KB, with cues, numbered failure modes, and 3 to 5 hypothesis templates. The reviewer never chooses between the two.

### Focus Priority Block

```markdown
### Focus Priority
Theme `<theme-id>`: <one-sentence mechanism from triage>.
Read `<skill-dir>/references/focus_areas/<theme-id>.md` before the diff. Test the theme
hypotheses under `### Hypotheses` first, tracing callers and tests across files. Then
complete the full review: every criterion in the Review Mandate applies to the whole diff,
and the theme never narrows what you report. Your read budget outside the diff and listed
context is 35 files for this review instead of 25.
```

### Focus Mandate Block

```markdown
### Focus Mandate
You are the focus reviewer for theme `<theme-id>`: <one-sentence mechanism from triage>.
Read `<skill-dir>/references/focus_areas/<theme-id>.md` before the diff. It is required
reading, not optional context.
Review the whole diff through this theme only. Report findings that belong to it and add
`- **Focus**: `<theme-id>`` to each. Report an issue outside the theme only at `critical`
or `high`, tagged `out-of-lane` in its Body. The hypotheses below are yours; add up to 5 of
your own within the theme. Spend your read budget tracing the mechanism across callers,
tests, and lifecycle boundaries, not on general style.
End your report with `## Playbook Feedback`. Its first line is
`Failure modes checked: <numbers from the playbook>`. Then at most 4 lines: failure modes
you checked that the playbook lacks, hypothesis templates that were noise, and cues that
would have flagged this theme earlier. Write `None.` when nothing applies.
```

When a focus reviewer covers a theme, add this line under `### Hypotheses` in the general reviewer's brief (or in the brief of the panel lane that would otherwise own the theme): `A focus reviewer covers <theme-id>; still report critical or high issues in it, tagged out-of-lane.`

Ad hoc theme ids use the form `adhoc:<slug>` (for example `adhoc:retry-storm-on-new-client`) in the `Focus` field and on the Summary flags line.

## Depth Cap

Step 2d ranks candidate themes by verdict strength and then by anchor count. The number that get a dedicated focus reviewer depends on depth:

| Depth | Focus reviewers |
| :--- | :--- |
| `--fast` | 0 (every theme folds in as Focus Priority) |
| Standard | 1 |
| `--panel`, `--max` | 2 |
| Explicit `--focus <theme>` flags | One per flag, up to 3, regardless of depth (except `--fast`) |

Strong themes beyond the cap fold in as Focus Priority: the first overflow theme goes to `rev-a` and the second to `rev-b` when it exists, otherwise also to `rev-a`. The Step 2d chat banner lists every theme and its disposition, so the user can ask for a late focus pass on any of them.

## Multi-Reviewer Merge

Step 5 applies these rules whenever more than one reviewer ran:

- When any two reviewers flag the same issue on the same line, keep one finding at the higher severity and add "(raised by both reviewers)" to its Body.
- Keep `out-of-lane` findings.
- Keep a finding's `- **Focus**:` field through the merge. When a general and a focus finding merge, the merged finding keeps the `Focus` field.
- Merge partial Structural Assessments into one.
- `## Checked and Found Clean` takes the union of disproved hypotheses, drops lines without a tier or disproving evidence, and caps at 10 lines. When the cap squeezes, keep theme disproofs first, since they are the most informative lines for the author.
- `## Playbook Feedback` sections are read for Step 5b and never copied into `review_results.md`.

## Playbook-Read Check

Before the citation audit, check each focus reviewer's `## Playbook Feedback`:

- The first line must be `Failure modes checked:` followed by numbers that exist in the assigned playbook (or the ad hoc block).
- A missing line, or numbers the playbook does not have, means the playbook was likely skipped. Audit every finding from that reviewer, regardless of severity, as for a reviewer that failed the clean check, and note it in the Summary (for example, "rev-f did not confirm its playbook read; all 4 findings audited").
