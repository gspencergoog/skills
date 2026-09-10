# Lightweight Implementation Plan Template

Use this template for focused, low-complexity tasks such as single-file bug fixes, minor parameter additions, or localized tweaks.

______________________________________________________________________

```markdown
# [Bug Fix / Minor Feature Title]

[Brief explanation of the bug or small feature and the expected fix.]

## User Review Required

> [!NOTE]
> [Any notable behavior change or edge case decision. If none, write "None."]

## Context & Key Files

- **Target File(s)**:
  - `path/to/file.ext`
- **Verification Command**:
  - `[e.g. pytest tests/test_file.py or npm test]`

## Proposed Changes

- [ ] #### [MODIFY] [path/to/file.ext](file:///absolute/path/to/file.ext)
  - [Precise description of the change, function signature, or logic adjustment]

## Verification Plan

### Automated Tests
- `[exact test command]`

### Manual Verification
- `[brief manual check steps]`
```
