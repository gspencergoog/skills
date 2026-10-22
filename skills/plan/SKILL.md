---
name: plan
description: >-
  Writes a phased implementation plan with parallel execution waves for a
  software task as a reviewable artifact with a Proceed button, while keeping
  the target codebase read-only: scope and non-goals, size-routed discovery
  (duckdb, context-indexer, rg, and sem-semantic-info), concrete file-level
  changes, and per-track verification commands, checked by a plan linter and an
  optional independent review. Use when the user asks to plan, "create a plan",
  "make an implementation plan", or runs /plan, when a brainstorm has settled on
  a direction, when a task spans several files or a migration, or when the user
  wants steps laid out before code is written. Don't use for exploring options
  before a direction is chosen, or for stakeholder-facing RFCs and design
  documents (use proposal-writer).
---

# Implementation Plan

Use this skill to produce actionable, wave-structured implementation plans for software tasks while keeping the target codebase strictly read-only.

## Core Rules

1. Guard the target repository as strictly read-only during planning. Never edit, add, or delete codebase files or run mutating commands (`git checkout`, `git stash`, `sed -i`, `rm`, `npm install`). The only permitted file creation is the plan artifact itself.
2. Save the plan as a markdown artifact in `<appDataDir>/brain/<conversation-id>/implementation_plan.md` (or `<name>_plan.md`) using `write_file` (`write_to_file`).
3. Include `ArtifactMetadata` when creating or updating the plan artifact:
   - Set `RequestFeedback: true` to display the interactive **"Proceed"** button in the UI.
   - Set `UserFacing: true` to open the plan in the artifacts panel for inline comments.
   - Set `Summary: "<multi-line summary>"` to describe the plan's scope and waves.
4. Do not copy or re-summarize the full plan in chat. Output a concise message linking to the artifact, highlight key open questions or decisions, and stop to await user approval.

______________________________________________________________________

## Procedural Workflow

### 1. Clarify & Define Scope

Establish clear boundaries before inspecting implementation details:

- Define the engineering objective and the problem being solved.
- Identify constraints such as language version, backward compatibility, and performance targets.
- List explicit non-goals to prevent scope creep.
- When requirements, assumptions, or wave dependencies are unclear, recommend `/grill-me` or render a `decision_batch`, `assumption_ledger`, or `wave_sequencer` surface via the `genui` skill. Fall back to your host's structured question tool or a numbered chat prompt when `a2ui-pane` is not installed.

### 2. Size-Routed Read-Only Codebase Discovery

Ground every step of the plan in real repository facts while keeping token usage low. Consult [codebase_discovery.md](references/codebase_discovery.md) for copy-paste SQL and CLI recipes.

> [!IMPORTANT]
> **Slice Before Reading (`>= 350` lines)**: Never call `read_file` (`view_file`) on a whole file of 350 or more lines. Index its section spans (`duckdb` or `stage_markdown.py` for Markdown) or symbol lines (`rg -n` or `sem` for code) first, then read only `StartLine`..`EndLine`.

1. Query prior session context and past `*plan*.md` artifacts first with [duckdb](../duckdb/SKILL.md) (`duckdb -batch -dark-mode` over `conversation_summaries.db` and `read_md_sections('brain/*/*plan*.md')`).
2. Route file and specification discovery by artifact format and size:
   - Point lookups and source files `< 350` lines: `rg -n` and `fd` ([unix-cli-best-practices](../unix-cli-best-practices/SKILL.md)), then targeted `read_file` line slices.
   - Markdown specs or audits `>= 350` lines or `10+` files: `duckdb` with `markdown_macros.sql` (`read_md_sections`, `read_md_code_blocks`).
   - Complex specs `> 500` lines, pipe tables, frontmatter, or RFC 2119 rules: `stage_markdown.py` and `staged_md_macros.sql` ([context-indexer](../context-indexer/SKILL.md)).
   - Raw branch or PR diffs `<= 500` lines: `duckdb` with `diff_macros.sql` (`read_diff_lines`, `read_diff_hunks`).
   - Branch or PR diffs `> 500` lines (AST churn and signature drift): `stage_diffs.py` and `staged_diff_macros.sql` ([context-indexer](../context-indexer/SKILL.md)).
   - Worktree symbol graphs and blast radius: `sem impact <file>` ([sem-semantic-info](../sem-semantic-info/SKILL.md)) and language MCP tools (`find_definition`, `analyze_files`).
3. Inspect package manifests (`package.json`, `pubspec.yaml`, `pyproject.toml`, `Cargo.toml`, `go.mod`) to find exact build commands, test runners, and linters.
4. Identify shared chokepoint files such as barrel exports (`index.ts`, `lib/*.dart`, `__init__.py`), central registries, and `CHANGELOG.md`. Isolate those chokepoints into sequential waves.

### 3. Select Plan Depth

Choose the reference template that matches the scope of work:

- [lightweight_plan_template.md](references/lightweight_plan_template.md) for small bug fixes, single-file edits, or isolated minor features (1 to 2 files, sequential).
- [standard_plan_template.md](references/standard_plan_template.md) for multi-file features, refactors, or new components, including parallel tracks when 2 or more tracks operate on disjoint files.
- [complex_plan_template.md](references/complex_plan_template.md) for cross-package architectural shifts, breaking API updates, or multi-wave migrations with parallel execution and verification lanes.

### 4. Draft the Implementation Plan Artifact

Draft the plan according to the chosen template and write it using `write_file` (`write_to_file`):

- Organize multi-component work using the **Sandwich Wave Topology**:
  - `### Wave 1: Foundation & Shared Contracts [Sequential]` for shared types, schemas, interfaces, and test harnesses.
  - `### Wave 2: Core Implementation [Parallel]` divided into `#### Track 2A: ...` and `#### Track 2B: ...` where each track edits a strictly disjoint set of files.
  - `### Wave 3: Integration, Exports & Cleanup [Sequential]` for barrel exports, central registries, CLI wiring, and end-to-end verification.
- Isolate shared chokepoint files into sequential foundation or integration waves so parallel tracks never touch the same file path.
- Specify an **Execution Strategy & Parallelization** table for multi-track plans, selecting the right orchestrator:
  - `/swarm` (`native_swarm`) when 2 or more tracks edit disjoint files across 3 or more files, pairing `1` verifier per `2-4` authors (`workspace: "share"` or `"branch"`).
  - `/run-workflow` (`parallel()` / `pipeline()`) when tracks follow a uniform per-shard recipe across many files or modules.
  - Single-agent sequential execution when the plan touches 1 to 2 files or has a strictly linear dependency chain.
- Format every planned file entry as a task list item without heading hashes (never write `- [ ] #### ...`):
  - `- [ ] [NEW] [basename](file:///absolute/path/to/file)`
  - `- [ ] [MODIFY] [basename](file:///absolute/path/to/file)`
  - `- [ ] [DELETE] [basename](file:///absolute/path/to/file)`
- Describe concrete function signatures, methods, error handling, and parameters rather than placeholders.
- Include a Mermaid architecture or wave-DAG diagram following [mermaid-diagrams](../mermaid-diagrams/SKILL.md) for multi-component or multi-wave workflows.
- Enforce function maintainability budgets ($\le 15$ per [cognitive-complexity](../cognitive-complexity/SKILL.md)) and clear prose ([write-prose](../write-prose/SKILL.md)).
- End every sequential wave and parallel track with a runnable test command (`**Track 2A Verification**:` or `**Wave 1 Verification**:`) and a wave-barrier gate.

### 5. Automated Plan Validation

Run [validate_plan.py](scripts/validate_plan.py) to check structure, code fence balance, file path integrity, and parallel wave disjointness:

```bash
python3 <skill-dir>/scripts/validate_plan.py <path/to/plan.md> --workspace <path/to/workspace>
```

The validator checks:

- All required structural sections are present and code fences are balanced.
- Task list items do not contain inline heading hashes (`- [ ] #### ...`).
- Every file marked `[MODIFY]` or `[DELETE]` exists on disk in the target workspace, and `[NEW]` files do not collide with existing files.
- Parallel tracks within the same `[Parallel]` wave edit strictly disjoint file sets (emitting `ERROR` on any intra-wave file collision).
- Every parallel track (`#### Track NA`) includes its own verification command (`WARNING` by default, `ERROR` with `--strict`).
- No unresolved placeholders (`TODO`, `TBD`, `implement rest here`) remain.

Resolve all reported validation errors before proceeding.

### 6. Independent Review Decision & Execution (Anti-Contamination)

To prevent confirmation bias from the drafting steps, offer an independent quality review of the plan before final presentation.

1. Prompt the user via an interactive UI tool before launching the review. Call your host's question tool (`ask_question` or `AskUserQuestion`) or render an inline `confirm_gate` surface via the `genui` skill (`render_a2ui` with `mode: "inline"`). Do not ask this question as plain chat text unless neither tool is available:
   - `(Recommended) Yes, run an independent review`
   - `No, skip independent review and proceed directly to presentation`
   - If the user skips the review, jump to **Step 7**.
2. Assemble the **Review Briefing Packet** using the template in [plan_quality_checklist.md](references/plan_quality_checklist.md), covering the goal, constraints, settled decisions, non-goals, focus areas, and paths. Keep the prompt under 10 KB. For plans or specs over 500 lines, stage chunks (`<= 35 KB`, `<= 700` lines) with `stage_markdown.py --split-chunks` and pass the chunk paths.
3. Delegate the review using the highest available tier:
   - **Tier 1 (Jetski Root Session with `swarm`)**: Spawn a read-only `plan-reviewer` worker via `swarm(action="add", name="plan-reviewer", workspace="inherit", brief=...)` and forbid helper workers. Wait for its board report, then stop it with `swarm(action="kill", name="plan-reviewer")`.
   - **Tier 2 (Subagent Tool Available)**: Spawn one read-only subagent (`Agent` or `invoke_subagent` with `research-google` or `self`, setting `enable_subagent_tools: false` and forbidding child subagents).
   - **Tier 3 (Already Inside a Subagent or Swarm Worker)**: Perform the checklist audit directly in-session without spawning further agents.
4. Audit the plan against the workspace files and [plan_quality_checklist.md](references/plan_quality_checklist.md), checking intent alignment, disk grounding, intra-wave file disjointness, chokepoint isolation, and per-track verification commands.
5. Resolve any blockers or gaps in the plan artifact and re-run `validate_plan.py` before presenting the plan to the user.

### 7. Present Plan & Await Approval

Once validated and reviewed:

1. Output a concise response in chat with a markdown link to the generated plan artifact.
2. Highlight critical decisions, breaking changes, or open questions that need the user's attention.
3. **STOP and do not start executing changes.** Wait for the user to review, submit inline comments, or click the **"Proceed"** button in the UI.

______________________________________________________________________

## Relationship to Other Skills

- Use [just-brainstorm](../just-brainstorm/SKILL.md) for early exploration of candidate architectures and tradeoffs before selecting a direction for `plan`.
- Use [proposal-writer](../proposal-writer/SKILL.md) for stakeholder-facing RFCs and architecture documents ("why" and "what"), whereas `plan` produces the execution blueprint ("how").
- Use [grill-me](../grill-me/SKILL.md) and the `genui` skill when requirements are underspecified or wave ordering needs interactive alignment (`wave_sequencer`, `decision_batch`, `assumption_ledger`).
- Use [duckdb](../duckdb/SKILL.md), [context-indexer](../context-indexer/SKILL.md), and [sem-semantic-info](../sem-semantic-info/SKILL.md) during Step 2 discovery to index specs, diffs, and symbol blast radius with minimal token overhead.
- Use [api-review](../api-review/SKILL.md) and [cognitive-complexity](../cognitive-complexity/SKILL.md) to check API signatures and function complexity budgets ($\le 15$) in the design.
- Use `swarm`, `run-workflow`, [ship-it](../ship-it/SKILL.md), and [commit-changes](../commit-changes/SKILL.md) to execute, test, and land the approved waves after the user clicks **"Proceed"**.
