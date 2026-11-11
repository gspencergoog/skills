# Focus Priority, Focus Mandate, and Playbook Feedback

Two blocks carry a focus theme or architectural playbook (`<playbook-dir>` is `<skill-dir>/references/focus_areas` in `code-review` or `<skill-dir>/references/focus_playbooks` in `design-review`). A reviewer brief never contains both blocks.

- **Focus Priority** (additive): goes to a general or lane reviewer when no dedicated focus reviewer covers the theme (`--fast`, `substantive_lines <= 400`, weak triage verdict, or a strong theme beyond the depth cap). A reviewer that is the only one reading a part of the target is never narrowed.
- **Focus Mandate** (narrow): goes only to a dedicated `<prefix>f*` worker that works beside a general or panel reviewer.

Each block names exactly one playbook source. For a catalog theme, point to `<playbook-dir>/<theme-id>.md`. For an ad hoc theme (no catalog entry, id `adhoc:<slug>`), replace the "Read ..." sentence with an inline `#### Ad hoc playbook` block of at most 1 KB containing cues, numbered failure modes (`Look at:` and `Settles it:`), and 3 to 5 hypothesis templates (2 to 5 in `design-review`).

## Focus Priority Block

```markdown
### Focus Priority
Theme `<theme-id>`: <one-sentence mechanism from triage>.
Read `<playbook-dir>/<theme-id>.md` before inspecting the target. Test the theme
hypotheses under `### Hypotheses` first, tracing callers, schemas, and tests across files.
Then complete the full review: every criterion or principle in the Review Mandate applies
to the whole target, and the theme never narrows what you report. Your read budget outside
the staged target and listed context is 35 files for this review instead of 25.
```

## Focus Mandate Block

```markdown
### Focus Mandate
You are the focus reviewer for theme `<theme-id>`: <one-sentence mechanism from triage>.
Read `<playbook-dir>/<theme-id>.md` before inspecting the target. It is required reading,
not optional context.
Review the whole target through this theme only. Report findings that belong to it and add
`- **Focus**: `<theme-id>`` to each. Report an issue outside the theme only at `critical`
or `high`, tagged `out-of-lane` in its Body. The hypotheses below are yours; add up to 5 of
your own within the theme. Spend your read budget tracing the mechanism across callers,
tests, state transitions, and lifecycle boundaries, not on general style.
End your report with `## Playbook Feedback`. Its first line is
`Failure modes checked: <numbers from the playbook>`. Then at most 4 lines: failure modes
you checked that the playbook lacks, hypothesis templates that were noise, and cues that
would have flagged this theme earlier. Write `None.` when nothing applies.
```

When a dedicated focus reviewer covers `<theme-id>`, add this line under `### Hypotheses` in the general reviewer's brief (or the panel lane that would otherwise own the theme):

```text
A focus reviewer covers <theme-id>; still report critical or high issues in it, tagged out-of-lane.
```

## Playbook-Read Check

Before running the Step 5 citation audit on a focus reviewer's report, inspect its `## Playbook Feedback`:

- The first line must be `Failure modes checked:` followed by numbers that exist in the assigned playbook (or the inline ad hoc block).
- A missing line, or numbers the playbook does not have, means the playbook was likely skipped. Audit every finding from that reviewer regardless of severity (just as for a reviewer that failed a clean check), and note it in `## Summary` (for example, `rev-f did not confirm its playbook read; all 4 findings audited`).

## Playbook Feedback and Step 5b Proposals

- Never copy `## Playbook Feedback` into the final `review_results.md` or `design_review_results.md` report (both linters fail with `playbook-feedback-leaked`).
- In Step 5b, whenever any focus reviewer's `## Playbook Feedback` is not `None.`, or a theme was `adhoc:<slug>`, write `<conversation-artifact-dir>/<prefix>_playbook_proposals.md` (`focus_playbook_proposals.md` for `code-review`, `design_playbook_proposals.md` for `design-review`).
- Give a ready-to-paste section (or whole playbook in the catalog format): the new failure mode, its hypothesis template, and the check that settles it. Never edit the skill's own directory during a review; applying proposals is a separate, user-approved step.
