# Plan Quality & Independent Review Checklist

This checklist defines the quality standards for implementation plans. To prevent context contamination, run this review using an independent subagent (such as `research` or `self`) with a fresh conversation context.

______________________________________________________________________

## 1. Grounding & File Integrity

- [ ] **Disk Verification**: Every file marked `[MODIFY]` or `[DELETE]` exists on disk in the target repository.
- [ ] **No Path Collisions**: Every file marked `[NEW]` does not already exist on disk, and its planned parent directory is valid.
- [ ] **Exact Clickable Links**: All file paths use proper GitHub markdown links with the `file://` scheme (e.g. `[basename](file:///absolute/path/to/file)`).
- [ ] **No Vague Placeholders**: The plan contains no unresolved placeholders (`TODO`, `TBD`, `implement rest here`, `...`).

## 2. Scope & Problem Framing

- [ ] **Explicit Problem Statement**: The plan clearly states the user's requirement and engineering objective.
- [ ] **Explicit Non-Goals**: Scope boundaries are defined by listing non-goals to prevent scope creep.
- [ ] **User Review Highlighted**: Breaking changes, critical trade-offs, and architecture decisions are called out using GitHub alerts (`> [!IMPORTANT]`, `> [!WARNING]`).

## 3. Architecture & Reusability

- [ ] **Reuse Existing Abstractions**: The plan identifies existing utilities, models, or patterns in the codebase to reuse instead of inventing duplicate logic.
- [ ] **Concrete Signatures**: Key function signatures, data models, or interface definitions are explicitly drafted in the plan.
- [ ] **Diagram Clarity**: For multi-component or asynchronous workflows, a clear Mermaid diagram illustrates the data flow.
- [ ] **Complexity Limits**: Functions and modules adhere to cognitive complexity limits (guideline: cognitive complexity $\\le 15$, per `cognitive-complexity` skill).

## 4. Phasing & Verifiability

- [ ] **Atomic, Sequential Phases**: Work is broken into logical phases with clear dependency ordering (e.g. models/types first, then core logic, then integration/CLI).
- [ ] **Per-Phase Checkpoints**: Each phase includes a specific verification command or criterion before proceeding to the next phase.
- [ ] **Actionable Changes**: Each file entry describes the concrete changes, methods touched, and error handling added.

## 5. Verification & Safety

- [ ] **Real Test Commands**: Test commands match the project's actual test runner and configuration (e.g. `pytest`, `cargo test`, `flutter test`, `npm test`).
- [ ] **Edge Cases Covered**: Specific edge cases, error conditions, and boundary values are listed for testing.
- [ ] **Rollback Strategy**: Clear criteria and steps for rolling back changes if issues occur.

## 6. Prose & Communication Standards

- [ ] **Plain Language**: Follows the `write-prose` skill: plain English, active voice, short sentences, and no promotional fluff or jargon.
- [ ] **No Redundant Re-Summaries**: The chat response presents only a concise pointer to the artifact, leaving the full details in the artifact viewer.

______________________________________________________________________

## Subagent Review Prompt Template

When delegating the review to an independent subagent, use the following prompt pattern:

```text
Please perform an objective quality review of the implementation plan located at:
<path/to/plan_artifact.md>

Target repository workspace:
<path/to/workspace>

Review the plan against references/plan_quality_checklist.md:
1. Verify that all files marked [MODIFY] or [DELETE] exist on disk.
2. Check for missing edge cases, untested failure modes, or ungrounded assumptions.
3. Verify that the test commands and linter configurations match the actual workspace.
4. Assess whether the implementation steps are atomic and logically sequenced.

Report any findings, defects, or ambiguities found. If the plan meets all standards, state that the plan is ready for user review.
```
