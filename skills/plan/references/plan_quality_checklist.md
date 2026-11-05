# Plan Quality & Independent Review Checklist

This checklist defines the quality standards for implementation plans. To prevent context contamination, run this review using an independent reviewer (`swarm` worker, subagent, or in-session review pass) with a fresh perspective.

______________________________________________________________________

## 1. Intent & Alignment (Audited Against Briefing Packet)

- [ ] The plan directly solves the user's primary engineering objective.
- [ ] All explicit user constraints and preferences (runtime limits, dependencies, conventions) are honored.
- [ ] The plan does not introduce out-of-scope work or re-introduce items designated as non-goals.
- [ ] Architectural choices agreed upon in conversation highlights or recorded under `## Decisions` are implemented rather than re-litigated.
- [ ] Specific doubts or risky areas called out in the briefing packet are analyzed.

## 2. Grounding & File Integrity

- [ ] Every file marked `[MODIFY]` or `[DELETE]` exists on disk in the target repository.
- [ ] Every file marked `[NEW]` does not already exist on disk, and its planned parent directory is valid.
- [ ] All file paths use proper GitHub markdown links with the `file://` scheme (`[basename](file:///absolute/path/to/file)`).
- [ ] The plan contains no unresolved placeholders (`TODO`, `TBD`, `implement rest here`, `...`).

## 3. Scope, Brief & Problem Framing

- [ ] The first `##` section is `## At a glance` with the required fields (`Goal`, `Shape`, `Decisions needed from you`, `Biggest risk`, `Not doing`). Lightweight plans require only `Goal`, `Decisions needed from you`, and `Not doing`, and the bullets total 180 words or fewer.
- [ ] Plans with 2 or more waves include a Mermaid wave diagram and a `### PRs` or `### Commits` table.
- [ ] Breaking changes, critical trade-offs, and architecture decisions are called out using GitHub alerts (`> [!IMPORTANT]`, `> [!WARNING]`) under `## User Review Required`.
- [ ] Open questions use `### Q<n>: <title>` headings with 2 or more option bullets (`- (Recommended) A: ... Consequence: ...`) and exactly one `(Recommended)` choice, or `## Open Questions` states `None currently blocking.`
- [ ] Answered questions are recorded under `## Decisions` in the agent's own words.

## 4. Architecture & Reusability

- [ ] The plan reuses existing utilities, models, or patterns in the codebase instead of inventing duplicate logic.
- [ ] Key function signatures, data models, or interface definitions are explicitly drafted in the plan.
- [ ] Functions and modules adhere to cognitive complexity limits (cognitive complexity $\le 15$, per the `cognitive-complexity` skill).

## 5. Phasing, Parallelism & Verifiability

- [ ] Independent work units are grouped into parallel tracks (`#### Track NA`) inside `[Parallel]` waves, separated from sequential foundation and integration waves (Sandwich Wave topology).
- [ ] Parallel tracks within the same wave modify strictly disjoint file sets (`validate_plan.py` reports zero intra-wave file collisions).
- [ ] Every parallel track specifies `- **Model tier**:` matching the complexity of the track.
- [ ] Shared chokepoint files (barrel exports such as `index.ts`, `lib/*.dart`, `__init__.py`, central registries, and `CHANGELOG.md`) are isolated in sequential foundation or integration waves.
- [ ] Multi-track plans specify the orchestration mode (`/swarm` with `1` verifier per `2-4` authors, `/run-workflow` `parallel()`, or sequential) and workspace mode (`share`, `branch`, `worktree`, or `inherit`).
- [ ] Every sequential wave and every parallel track includes an explicit, targeted verification command (`**Track NA Verification**:` or `**Wave N Verification**:`) plus a wave-barrier gate before the next wave begins.
- [ ] Each file entry describes concrete changes, methods touched, and error handling added.

## 6. Verification & Safety

- [ ] Test commands match the project's actual test runner and configuration (`pytest`, `cargo test`, `flutter test`, `npm test`).
- [ ] Specific edge cases, error conditions, and boundary values are listed for testing.
- [ ] Clear criteria and steps are provided for landing changes (`## Landing`) and rolling back if issues occur.

## 7. Prose & Communication Standards (Two-Layer Audit)

Check the brief layer and the execution layer separately:

- [ ] **Brief layer** (`At a glance`, `Commits` or `PRs` table, `User Review Required`, `Open Questions`, `Decisions`): follows strict [write-prose](../../write-prose/SKILL.md) rules. Sentences average 20 words or fewer (`validate_plan.py` enforces $\le 20$), sentence complexity stays low ($\text{PCC} \le 5$), and no em-dashes or fluff appear.
- [ ] **Execution layer** (`Context`, `Design`, waves, tracks, `Verification Plan`, `Landing`, `Rollback`): uses tables and checklists for agent consumption. Follows `write-prose` vocabulary rules (plain words, no AI-isms, zero em-dashes, "Recommended" instead of "default"), while allowing longer sentences and dense code spans.
- [ ] The chat response presents only a concise pointer to the artifact and brief highlights, leaving full details in the artifact viewer.

______________________________________________________________________

## Subagent Review Prompt Template

When delegating the review (`swarm` worker on Tier 1, or subagent on Tier 2), assemble a **Review Briefing Packet** using the template below. Keep the prompt under 10 KB. For plans or specs over 500 lines, stage chunks (`<= 35 KB`, `<= 700` lines) with `stage_markdown.py --split-chunks` and list them under Reference Artifacts.

````text
Please perform an objective quality review of the implementation plan located at:
<path/to/plan_artifact.md>

Target repository workspace:
<path/to/workspace>

Do not modify, create, or delete any file in the workspace. Do not spawn subagents or call `swarm` to add helpers.

## Review Briefing Packet

### 1. Purpose & Engineering Goal
<Concise description of the user's core request and definition of success>

### 2. User Constraints & Preferences
<Key constraints, runtime limits, dependencies permitted/forbidden, team conventions>

### 3. Conversation Highlights & Recorded Decisions
<Summary of major architectural trade-offs settled, rejected alternatives and why, and the plan's recorded ## Decisions entries>

### 4. Explicit Non-Goals
<Out-of-scope items agreed upon to prevent scope creep>

### 5. Planner's Focus Areas / Suspected Risks
<Specific questions or risky transitions the planner wants verified, e.g. parallel track isolation, backward compatibility>

### 6. Reference Artifacts
<Links to prior discussion artifacts, brainstorming docs, RFCs, or staged md_chunk_NN.md files>

---

## Instructions for Reviewer

Review the plan against <skill-dir>/references/plan_quality_checklist.md:
1. Audit Intent & Alignment: Verify the plan satisfies the briefing packet, honors constraints and non-goals, and aligns with recorded Decisions without re-litigating them.
2. Audit Grounding & File Integrity: Verify that all files marked [MODIFY] or [DELETE] exist on disk in the target workspace.
3. Audit Parallelism, Model Tiers & File Disjointness: Verify that parallel tracks within the same wave own strictly disjoint files, specify a model tier, isolate shared chokepoint files into sequential waves, and define per-track verification commands.
4. Audit Brief & Technical Feasibility: Check the two-layer prose rules, verify test commands against repo scripts, evaluate edge cases and failure modes, and verify wave/phase dependency ordering.
5. Address Focus Areas: Explicitly investigate each focus question or risk raised by the planner.

Format your review report with:
- Summary verdict (Approved / Revisions Required)
- Alignment & parallelism audit (Checks against briefing packet and intra-wave file disjointness)
- Technical findings grouped by severity:
  - Blockers (`"blocker"`: violates constraints, intra-wave file collisions, invalid file paths, broken test commands)
  - Major Risks & Gaps (`"major"`: unhandled edge cases, unisolated chokepoint files, sequencing hazards)
  - Minor Suggestions (`"minor"`: optional design or documentation polish)
- End your report with a fenced `json` findings array so `plan_surfaces.py triage` can parse it:

```json
[
  {
    "id": "F1",
    "severity": "blocker",
    "title": "Short finding title",
    "summary": "One or two sentences explaining the defect and its consequence.",
    "evidence": "Section or file path and line numbers"
  }
]
```

If there are no findings, emit `[]` inside the `json` fence.
````

### Anti-Pattern: The Barebones Prompt (Do Not Use)

Never send a prompt that only provides paths and generic bullet points:

```text
Please review the plan at <path> against the checklist.
1. Check files on disk.
2. Check edge cases.
```

A barebones prompt starves the reviewer of user intent, constraints, settled decisions, and non-goals, turning an architectural evaluation into a superficial syntax check.
