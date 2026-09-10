---
name: plan
description: >-
  Creates a structured, actionable implementation plan for software engineering
  tasks while maintaining a strictly read-only guarantee on the target codebase.
  Use this skill whenever: (1) the user asks to "plan", "create a plan", "make
  an implementation plan", or run "/plan", (2) transitioning from brainstorming
  (such as from the just-brainstorm skill) to an engineering execution plan,
  (3) a task requires multi-file changes, refactoring, or architectural migration,
  or (4) the user requests an actionable breakdown of implementation phases,
  concrete file changes, and verification steps before writing code.
---

# Implementation Plan

Use this skill to produce actionable, phased implementation plans for software tasks while keeping the target codebase strictly read-only.

## Core Rules

1. **Strict Read-Only Guarantee**: Under no circumstances should you modify, add, or delete files in the target codebase during planning. Do not run commands that alter repository state (e.g. `git checkout`, `git stash`, `sed -i`, `rm`, `npm install`). The only permitted file creation is the plan artifact itself.
2. **Mandatory Artifact Generation**: Save the plan as a markdown artifact in the conversation artifact directory (`<appDataDir>/brain/<conversation-id>/implementation_plan.md` or a descriptive `<name>_plan.md`) using `write_to_file`.
3. **Triggering the "Proceed" Button**: When calling `write_to_file` to create the plan artifact, you must provide `ArtifactMetadata` with:
   - `RequestFeedback: true` — This specific property triggers the interactive **"Proceed"** button in the Antigravity UI.
   - `UserFacing: true` — Displays the plan in the artifacts panel with text selection for inline comments.
   - `Summary: "<multi-line summary>"` — Outlines the plan's scope and objectives.
4. **No Full Chat Re-Summaries**: The Antigravity UI renders the full artifact for the user. Do not copy or re-summarize the full plan in your chat response. Provide only a concise message pointing to the artifact, highlight key open questions or critical decisions, and stop to await user approval.

______________________________________________________________________

## Procedural Workflow

### 1. Clarify & Define Scope

Before diving into technical details, establish clear boundaries:

- Define the engineering objective and the specific problem being solved.
- Identify constraints (language version, backward compatibility, performance targets).
- List explicit **non-goals** to prevent scope creep.
- If requirements are ambiguous or key decisions are unmade, recommend that the user runs the `/grill-me` slash command or ask targeted clarifying questions.

### 2. Read-Only Codebase Discovery

Ground every step of the plan in real repository facts. Consult [codebase_discovery.md](references/codebase_discovery.md) for ecosystem-specific patterns.

1. **Detect Tooling & Invariants**: Inspect package manifests (`package.json`, `pubspec.yaml`, `pyproject.toml`, `Cargo.toml`, `go.mod`) to find exact build commands, test runners, and linters.
2. **Locate Existing Abstractions**: Search for existing utilities, base classes, or helpers with `grep_search` or `find_by_name`. Reuse existing code rather than planning duplicate utilities.
3. **Measure Blast Radius**: Search for references to symbols being modified or deleted using `rg` or the [sem-semantic-info](../sem-semantic-info/SKILL.md) skill (`sem impact`) to ensure all downstream call sites are planned for updates.
4. **Identify Test Coverage**: Locate existing test suites that cover the affected modules to include in regression verification.

### 3. Select Plan Depth

Choose the appropriate reference template based on the scope of work:

- **Lightweight Plan**: Use [lightweight_plan_template.md](references/lightweight_plan_template.md) for small bug fixes, single-file edits, or isolated minor features.
- **Standard Plan**: Use [standard_plan_template.md](references/standard_plan_template.md) for multi-file features, typical refactors, or new components.
- **Complex / Migration Plan**: Use [complex_plan_template.md](references/complex_plan_template.md) for cross-package architectural shifts, breaking API updates, or multi-phase data migrations.

### 4. Draft the Implementation Plan Artifact

Draft the plan according to the chosen template and write it using `write_to_file`. Ensure the plan includes:

- **Component Grouping**: Group changes logically by component, service, or layer (dependencies first).
- **Explicit Action Demarcations**: For every planned file change, specify:
  - `#### [NEW] [basename](file:///absolute/path/to/file)`
  - `#### [MODIFY] [basename](file:///absolute/path/to/file)`
  - `#### [DELETE] [basename](file:///absolute/path/to/file)`
- **Concrete Signatures & Logic**: Describe the specific functions, methods, error handling, and parameters being touched, rather than vague placeholders.
- **Architecture & Diagrams**: For non-trivial workflows or component interactions, include a Mermaid diagram following the [mermaid-diagrams](../mermaid-diagrams/SKILL.md) skill.
- **Cognitive Complexity Budget**: Reference the [cognitive-complexity](../cognitive-complexity/SKILL.md) skill to ensure new or refactored functions remain maintainable (standard threshold $\\le 15$).
- **Writing Style**: Follow the [write-prose](../write-prose/SKILL.md) skill: active voice, plain language, concise sentences, and no marketing adjectives or puffery.
- **Phased Verification Checkpoints**: Each phase must end with an explicit test or build command to verify that step independently before moving to the next.

### 5. Automated Plan Validation

Run the plan validation script to verify structure, code fence balance, and file path integrity against the workspace:

```bash
python3 /Users/gspencer/code/cheats/agents/skills/plan/scripts/validate_plan.py <path/to/plan.md> --workspace <path/to/workspace>
```

The script verifies:

- All required structural sections are present.
- Every file marked `[MODIFY]` or `[DELETE]` exists on disk in the target workspace.
- No files marked `[NEW]` collide with existing files on disk.
- No unresolved placeholders (`TODO`, `TBD`, `implement rest here`) remain.

Resolve any reported validation errors before proceeding.

### 6. Subagent Review Decision & Execution (Anti-Contamination)

To prevent confirmation bias and context contamination from the generation steps, you can delegate an objective review of the draft plan to an independent subagent (e.g. `research` or `self`).

1. **Ask User for Review Confirmation**: Always prompt the user using the `ask_question` tool before launching the subagent review:
   - Ask whether to run an in-depth independent subagent review or proceed directly to presenting the plan.
   - Provide options formatted as:
     - `(Recommended) Yes, run an independent subagent review`
     - `No, skip subagent review and proceed directly to presentation`
   - If the user selects to skip, proceed directly to **Step 7**.
2. **Assemble the Review Briefing Packet**: If the user approves running the subagent review, assemble a structured briefing packet. Never send a barebones prompt containing only paths—the reviewer must have enough context to judge intent, constraints, settled decisions, and non-goals:
   - **Purpose & Core Goal**: The primary user request and definition of success.
   - **User Constraints & Preferences**: Specific boundaries (e.g., zero new dependencies, backward compatibility, runtime targets).
   - **Conversation Highlights & Prior Decisions**: Summary of settled design trade-offs and rejected alternatives to prevent re-litigating decisions.
   - **Explicit Non-Goals**: Out-of-scope boundaries to prevent the reviewer from flagging missing features that were intentionally excluded.
   - **Planner's Focus Areas / Doubts**: Specific questions or transitions where the planner wants targeted verification (e.g. concurrency, backward compatibility).
   - **Reference Artifacts**: Links to prior discussion artifacts (e.g. brainstorming docs, RFCs, issues).
   - **Plan Path & Workspace Root**: Paths to the draft plan artifact and target repository.
3. **Invoke Subagent**: Format the prompt using the template in [plan_quality_checklist.md](references/plan_quality_checklist.md).
4. **Subagent Audit**: The subagent inspects the plan against the workspace files and the quality rubric, reporting on intent alignment, disk grounding, technical feasibility, and planner focus areas.
5. **Incorporate Findings**: Resolve any blockers or gaps identified by the subagent in the plan artifact before presenting it to the user.

### 7. Present Plan & Await Approval

Once validated and reviewed:

1. Output a concise response in chat with a markdown link to the generated artifact.
2. Highlight critical decisions, breaking changes, or open questions that need the user's attention.
3. **STOP and do not start executing changes.** Wait for the user to review, submit inline comments, or click the **"Proceed"** button in the UI.

______________________________________________________________________

## Relationship to Other Cheats Skills

- **`just-brainstorm`** ([just-brainstorm](../just-brainstorm/SKILL.md)): Used for early-stage exploration of multiple architectural directions and tradeoffs. Once a direction is selected, it hands off to `plan`.
- **`proposal-writer`** ([proposal-writer](../proposal-writer/SKILL.md)): Used for high-level RFCs, business cases, and architecture documents intended for human stakeholders ("why" and "what"), whereas `plan` produces the engineering execution blueprint ("how").
- **`grill-me`** ([grill-me](../grill-me/SKILL.md)): Use when requirements are underspecified or architectural options have trade-offs that need user input.
- **`api-review`** ([api-review](../api-review/SKILL.md)): Use to review any new or modified API signatures specified in the plan.
- **`cognitive-complexity`** ([cognitive-complexity](../cognitive-complexity/SKILL.md)): Use to evaluate maintainability and enforce function complexity budgets in the design.
- **`ship-it`** ([ship-it](../ship-it/SKILL.md)) & **`commit-changes`** ([commit-changes](../commit-changes/SKILL.md)): Downstream execution skills used after implementation is approved and completed.
