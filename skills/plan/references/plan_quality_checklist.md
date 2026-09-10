# Plan Quality & Independent Review Checklist

This checklist defines the quality standards for implementation plans. To prevent context contamination, run this review using an independent subagent (such as `research` or `self`) with a fresh conversation context.

______________________________________________________________________

## 1. Intent & Alignment (Audited against Briefing Packet)

- [ ] **Fulfills User Intent**: The plan directly solves the user's primary engineering objective.
- [ ] **Respects Constraints**: All explicit user constraints and preferences (runtime limits, dependencies, conventions) are honored.
- [ ] **Honors Non-Goals**: The plan does not introduce out-of-scope work or re-introduce items designated as non-goals.
- [ ] **Respects Settled Decisions**: Architectural choices agreed upon in conversation highlights are implemented rather than re-litigated.
- [ ] **Addresses Planner Focus Areas**: Specific doubts or risky areas called out in the briefing packet are thoroughly analyzed.

## 2. Grounding & File Integrity

- [ ] **Disk Verification**: Every file marked `[MODIFY]` or `[DELETE]` exists on disk in the target repository.
- [ ] **No Path Collisions**: Every file marked `[NEW]` does not already exist on disk, and its planned parent directory is valid.
- [ ] **Exact Clickable Links**: All file paths use proper GitHub markdown links with the `file://` scheme (e.g. `[basename](file:///absolute/path/to/file)`).
- [ ] **No Vague Placeholders**: The plan contains no unresolved placeholders (`TODO`, `TBD`, `implement rest here`, `...`).

## 3. Scope & Problem Framing

- [ ] **Explicit Problem Statement**: The plan clearly states the user's requirement and engineering objective.
- [ ] **Explicit Non-Goals**: Scope boundaries are defined by listing non-goals to prevent scope creep.
- [ ] **User Review Highlighted**: Breaking changes, critical trade-offs, and architecture decisions are called out using GitHub alerts (`> [!IMPORTANT]`, `> [!WARNING]`).

## 4. Architecture & Reusability

- [ ] **Reuse Existing Abstractions**: The plan identifies existing utilities, models, or patterns in the codebase to reuse instead of inventing duplicate logic.
- [ ] **Concrete Signatures**: Key function signatures, data models, or interface definitions are explicitly drafted in the plan.
- [ ] **Diagram Clarity**: For multi-component or asynchronous workflows, a clear Mermaid diagram illustrates the data flow.
- [ ] **Complexity Limits**: Functions and modules adhere to cognitive complexity limits (guideline: cognitive complexity $\\le 15$, per `cognitive-complexity` skill).

## 5. Phasing & Verifiability

- [ ] **Atomic, Sequential Phases**: Work is broken into logical phases with clear dependency ordering (e.g. models/types first, then core logic, then integration/CLI).
- [ ] **Per-Phase Checkpoints**: Each phase includes a specific verification command or criterion before proceeding to the next phase.
- [ ] **Actionable Changes**: Each file entry describes the concrete changes, methods touched, and error handling added.

## 6. Verification & Safety

- [ ] **Real Test Commands**: Test commands match the project's actual test runner and configuration (e.g. `pytest`, `cargo test`, `flutter test`, `npm test`).
- [ ] **Edge Cases Covered**: Specific edge cases, error conditions, and boundary values are listed for testing.
- [ ] **Rollback Strategy**: Clear criteria and steps for rolling back changes if issues occur.

## 7. Prose & Communication Standards

- [ ] **Plain Language**: Follows the `write-prose` skill: plain English, active voice, short sentences, and no promotional fluff or jargon.
- [ ] **No Redundant Re-Summaries**: The chat response presents only a concise pointer to the artifact, leaving the full details in the artifact viewer.

______________________________________________________________________

## Subagent Review Prompt Template

When delegating the review to an independent subagent, assemble a **Review Briefing Packet** and use the following prompt pattern:

```text
Please perform an objective quality review of the implementation plan located at:
<path/to/plan_artifact.md>

Target repository workspace:
<path/to/workspace>

## Review Briefing Packet

### 1. Purpose & Engineering Goal
<Concise description of the user's core request and definition of success>

### 2. User Constraints & Preferences
<Key constraints, runtime limits, dependencies permitted/forbidden, team conventions>

### 3. Relevant Conversation Highlights & Prior Decisions
<Summary of major architectural trade-offs settled, rejected alternatives and why, user guidance>

### 4. Explicit Non-Goals
<Out-of-scope items agreed upon to prevent scope creep>

### 5. Planner's Focus Areas / Suspected Risks
<Specific questions or risky transitions the planner wants verified, e.g. concurrency, backward compatibility>

### 6. Reference Artifacts
<Links to prior discussion artifacts, brainstorming docs, RFCs, or issues, e.g. [brainstorm.md](file:///...)>

---

## Instructions for Reviewer

Review the plan against references/plan_quality_checklist.md:
1. Audit Intent & Alignment: Verify the plan satisfies the briefing packet, honors constraints and non-goals, and aligns with settled decisions without re-litigating them.
2. Audit Grounding & File Integrity: Verify that all files marked [MODIFY] or [DELETE] exist on disk in the target workspace.
3. Audit Technical Feasibility: Check test commands against repo scripts, evaluate edge cases and failure modes, and verify phase ordering.
4. Address Focus Areas: Explicitly investigate each focus question or risk raised by the planner.

Format your review report with:
- Summary verdict (Approved / Revisions Required)
- Alignment audit (Checks against briefing packet)
- Technical findings grouped by severity:
  - Blockers (violates constraints, invalid file paths, broken test commands)
  - Risks & Gaps (unhandled edge cases, sequencing hazards)
  - Suggestions (optional design or documentation polish)
```

### ❌ Anti-Pattern: The Barebones Prompt (Do Not Use)

Never send a prompt that only provides paths and generic bullet points:

```text
Please review the plan at <path> against the checklist.
1. Check files on disk.
2. Check edge cases.
```

A barebones prompt starves the reviewer of user intent, constraints, settled decisions, and non-goals, turning what should be a rigorous architectural evaluation into an uninformative syntax check.
