---
name: review-core
description: >-
  Supplies the shared evidence tiers, delegation hierarchy, focus-block templates,
  synthesis rules, cleanup and hand-off rules, canonical API design principles,
  and report lint parser for review skills. Use when loaded as a library by
  code-review or design-review, not directly by users.
disable-slash-command: true
---

# Review Core

Shared library loaded by `code-review` and `design-review`. This skill has no standalone slash command and is never invoked directly by users.

## Contents

| Asset | Role |
| :--- | :--- |
| [references/evidence.md](references/evidence.md) | Evidence tiers, severity caps, hypotheses, tree checks, falsification pass, anti-patterns, and code/design finding formats |
| [references/delegation.md](references/delegation.md) | Delegation tiers, `<prefix>` roster, quota failover, warm-worker reuse, and no-monitor / stay-parked rules |
| [references/focus_blocks.md](references/focus_blocks.md) | Focus Priority and Focus Mandate templates, ad hoc themes, playbook-read check, and `## Playbook Feedback` handling |
| [references/synthesis.md](references/synthesis.md) | Report collection, citation audit, `--challenge`, multi-reviewer merge, and unified verdict vocabulary |
| [references/handoff_and_cleanup.md](references/handoff_and_cleanup.md) | Temporary directory cleanup guard, `triage_matrix` / `scorecard` hand-off, wake-up rule, and caller mode |
| [references/canonical_api_design.md](references/canonical_api_design.md) | Canonical API design principles shared by `--api` code reviews, `api-surface` design reviews, and `/design` |
| [scripts/review_lint_common.py](scripts/review_lint_common.py) | Shared report-parsing, sequence, evidence-cap, fence, verdict, and prose-lint library |
