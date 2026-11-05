# Lightweight Plan Template

Use this template for focused, low-complexity tasks such as single-file bug fixes, minor parameter additions, or localized tweaks (no waves, at most 2 file actions).

Lightweight plans require only three fields in `## At a glance` (`Goal`, `Decisions needed from you`, and `Not doing`), and they omit the PR table and model tiers. Keep the `At a glance` bullets at 180 words or fewer. Keep the brief sections (`At a glance`, `User Review Required`, `Open Questions`, and `Decisions`) at 20 words per sentence or fewer under [write-prose](../../write-prose/SKILL.md).

______________________________________________________________________

````markdown
# [Bug Fix / Minor Feature Title]

## At a glance

- **Goal:** [One short sentence stating the bug fix or small feature.]
- **Decisions needed from you:** [None, or "1 (Q1 under Open Questions)."]
- **Not doing:** [Explicit non-goals in one short line.]

## User Review Required

> [!NOTE]
> [Any notable behavior change or edge case decision. If none, write "None."]

## Open Questions

None currently blocking.

<!-- When a decision is needed, replace "None currently blocking." with:
### Q1: [Short question title?]
- (Recommended) A: [Recommended choice.] Consequence: [Concrete effect of choosing A.]
- B: [Alternative choice.] Consequence: [Concrete effect of choosing B.]
-->

## Decisions

- **Q1 (YYYY-MM-DD):** A. [Recorded answer in the agent's own words.]

## Context & Key Files

- **Target File(s)**:
  - `path/to/file.ext`
- **Verification Command**:
  - `[e.g. pytest tests/test_file.py or npm test]`

## Proposed Changes

- [ ] [MODIFY] [file.ext](file:///absolute/path/to/file.ext)
  - [Precise description of the change, function signature, or logic adjustment]

## Verification Plan

### Automated Tests
- `[exact test command]`

### Manual Verification
- `[brief manual check steps]`
````
