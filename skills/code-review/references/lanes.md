# Reviewer Roster and Lanes

This reference defines the lane assignments, volume gates, and focus depth cap for [`code-review`](../SKILL.md). Shared delegation tiers, roster naming, quota failover, focus-block templates, and synthesis merge rules live in `review-core`:

- **Reviewer Roster & Lifecycle**: See [delegation.md](../../review-core/references/delegation.md) using prefix `rev-` (`rev-a`, `rev-b`, `rev-f`, `rev-f2`, `rev-f3`) and Tier 2 subagent roles `Code Reviewer`, `Code Reviewer A`, `Code Reviewer B`, and `Focus Reviewer (<theme-id>)`.
- **Focus Blocks & Playbook-Read Check**: See [focus_blocks.md](../../review-core/references/focus_blocks.md) with `<playbook-dir>` set to `<skill-dir>/references/focus_areas` and Step 5b proposals written to `focus_playbook_proposals.md`.
- **Multi-Reviewer Merge**: See [synthesis.md](../../review-core/references/synthesis.md#5-multi-reviewer-merge) (in `code-review`, merge partial `## Structural Assessment` sections across lanes and sort `wording` findings last within `low`).

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

## Volume Gates

`classify_artifacts.py --combined --diff-file <diff>` weights the diff with [diff_weight.py](../../context-indexer/scripts/diff_weight.py) before choosing reviewers. The weighted count only gates reviewer count. Every changed file, including lockfiles and generated code, is still staged and reviewed.

| Gate | Threshold | Above the threshold | At or below the threshold |
| :--- | :--- | :--- | :--- |
| `panelMinLines` | 225 substantive lines | Ring 3 runs `--panel` (`rev-a` + `rev-b`) | Ring 3 runs Standard with `rev-a` only, and `rev-a` gets the API Review Mandate block (`ring.api_mandate`) |
| `focusMinLines` | 400 substantive lines | Step 2d may give strong themes a dedicated `rev-f*` (`focus_dedicated_allowed`) | Every theme folds into `rev-a` (or `rev-b`) as Focus Priority |

Substantive lines:

- Count 0: lockfiles, generated or build output, snapshots and fixtures, assets and l10n files, `.rst`/`.txt`/`.adoc` and extensionless `CHANGELOG`/`LICENSE`/`NOTICE` files, pure renames, blank, comment, import, and bare delimiter lines, and whitespace-only reflows.
- Count 1.0x: production code logic lines, and every non-blank line in Markdown (`.md`, `.mdx`), since skills, references, blueprints, and docs are reviewed like code.
- Count 0.25x: logic lines in test files.

Explicit `--panel`, `--max`, and `--focus` flags bypass both gates. The thresholds come from an audit of 98 past reviews: below about 225 lines a second reviewer added no unique findings, and below 400 lines dedicated focus reviewers found nothing in 4 of 5 runs.

## Depth Cap

Step 2d ranks candidate themes by verdict strength and then by anchor count. The number that get a dedicated focus reviewer depends on depth:

| Depth | Focus reviewers |
| :--- | :--- |
| `--fast` | 0 (every theme folds in as Focus Priority) |
| Standard | 1 (0 when `focus_dedicated_allowed` is false) |
| `--panel`, `--max` | 2 (0 when `focus_dedicated_allowed` is false) |
| Explicit `--focus <theme>` flags | One per flag, up to 3, regardless of depth (except `--fast`) |

Strong themes beyond the cap fold in as Focus Priority: the first overflow theme goes to `rev-a` and the second to `rev-b` when it exists, otherwise also to `rev-a`. The Step 2d chat banner lists every theme and its disposition, so the user can ask for a late focus pass on any of them.
