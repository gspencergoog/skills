# Reviewer Roster, Lanes, and Clean-Room Brief Templates

This reference defines the reviewer roster, the Lane A and Lane B split, the `< 4 KB` `--post-design` packet, the `< 10 KB` brief template, and the merge rules. See [canonical_design_principles.md](canonical_design_principles.md), [evidence_and_critique.md](evidence_and_critique.md), and [focus_playbooks/README.md](focus_playbooks/README.md).

## Table of Contents

- [Reviewer Roster and Quota Failover](#reviewer-roster-and-quota-failover)
- [Orthogonal Panel Lanes](#orthogonal-panel-lanes)
- [Focus Priority and Focus Mandate Blocks](#focus-priority-and-focus-mandate-blocks)
- [Post-Design Briefing Packet Contract](#post-design-briefing-packet-contract)
- [Template for Clean-Room Reviewer Briefs](#template-for-clean-room-reviewer-briefs)
- [Multi-Reviewer Scorecard and Finding Merge Rules](#multi-reviewer-scorecard-and-finding-merge-rules)

## Reviewer Roster and Quota Failover

| Reviewer | Assignment | Default Model (Tier 1 `swarm`) | Active When |
| :--- | :--- | :--- | :--- |
| `des-a` | `general` (Standard) or `Lane A` (`--panel`) | `gemini-launch-candidate-high` (or `opus-5.5-max` with `--claude`/`--opus` in Standard mode) | Every review except `--fast` |
| `des-b` | `Lane B` (`--panel`) | `gemini-launch-candidate-high` (or `opus-5.5-max` with `--claude`/`--opus`) | One-Way Doors or explicit `--panel` |
| `des-f`, `des-f2`, `des-f3` | `focus:<playbook-id>` | `gemini-launch-candidate-high` | Explicit `--focus <id>` (up to 3), or strong triage match when `substantive_lines > 400` (up to 1 in Standard, 2 in `--panel`) |

Roster and quota rules:
- Check `swarm{action: "models"}` before spawning. Fail over in order: `opus-5.5-max` to `gemini-launch-candidate-high`, then to `auto`. Switch a waiting worker in place with `swarm{action: "model"}` on `RESOURCE_EXHAUSTED`.
- Reuse a `waiting` worker via `post_message` and `swarm_wait{since: <last_seq>}` during `--challenge` rounds. Never `kill` and re-add the same name; use `des-a-2` or `des-b-2` when a fresh unanchored context is required.
- On Tier 2 (subagents), assign roles `Design Reviewer`, `Design Reviewer A`, `Design Reviewer B`, or `Focus Reviewer (<playbook-id>)`.

## Orthogonal Panel Lanes

Under `--panel`, `des-a` and `des-b` inspect the full target in parallel through non-overlapping principle partitions:

| Lane | Reviewer | Owned Principles | Scope and Rubrics |
| :--- | :--- | :--- | :--- |
| `Lane A` | `des-a` | `problem-scope`, `boundaries-state`, `resilience-ops` | Problem/mechanism separation, falsifiable goals, dependency DAGs and cycles, single-writer state ownership, runtime concurrency, timeouts, partial-failure recovery, rollout skew, and security boundaries ([reviewing_design_docs.md](reviewing_design_docs.md), [reviewing_code_architecture.md](reviewing_code_architecture.md)). |
| `Lane B` | `des-b` | `module-depth`, `tradeoffs-simplicity`, `evolution-fitness`, `ergonomics-coherence` | Ousterhout module depth, pass-through and leakage checks, rational decision tradeoffs, Simpler-Baseline comparison, One-Way Door adapters, fitness functions, Mermaid/view coherence, Goldfish test, and API soundness ([canonical_api_design.md](../../api-review/references/canonical_api_design.md)). |

Each lane reviewer scores its owned principles (or all seven when evidenced) and reports findings in its lane. Exception: report a `critical` or `high` defect outside the lane and tag its `Body` with `out-of-lane`.

## Focus Priority and Focus Mandate Blocks

Select playbooks from [focus_playbooks/README.md](focus_playbooks/README.md) (or inline a `<= 1 KB` `#### Ad hoc playbook` for `adhoc:<slug>`). Never include both blocks in the same brief:
- Use `### Focus Priority` when folding a playbook into `des-a` or `des-b` (`--fast`, `substantive_lines <= 400`, or overflow beyond the focus cap). The reviewer reads `<skill-dir>/references/focus_playbooks/<id>.md` first, tests its hypotheses, and completes the full review with an extra file read budget of `35` instead of `25`.
- Use `### Focus Mandate` only for a dedicated `des-f*` worker. The reviewer reads `<skill-dir>/references/focus_playbooks/<id>.md`, audits the target strictly through those failure modes, adds `- **Focus**: \`<id>\`` to each finding, and appends `## Playbook Feedback` (`Failure modes checked: <numbers>`).

## Post-Design Briefing Packet Contract

When `/design` Phase 3 or `/proposal-writer` invokes `/design-review --post-design`, embed this `< 4 KB` packet so reviewers respect fixed decisions without reading prior chat history:

```markdown
## Design Briefing Packet
- **Spec Path**: `<conversation-artifact-dir>/<topic>_design.md`
- **Target Workspace**: `<repo-root>`
- **Core Problem & Goal**: <1 to 2 sentences>
- **Hard Constraints**: <runtime, portability, or budget limits>
- **Explicit Non-Goals**: <items deliberately excluded>
- **Locked Settled Decisions** (do not re-litigate on preference; challenge only with `Read`, `Executed`, or line-cited `Deduction` proof of a `critical` or `high` feasibility, compatibility, or security blocker):
  - D1: <recorded decision>
```

## Template for Clean-Room Reviewer Briefs

Keep the total brief `< 10 KB` and place every instruction block before `### Target Paths & Staged Chunks`:

```markdown
Conduct a read-only architectural review of `<target>` for `/design-review`. Read `<skill-dir>/references/canonical_design_principles.md`, `<skill-dir>/references/evidence_and_critique.md`, and `<mode-rubric-path>` first.
Do not modify, create, or delete workspace files, run state-changing git commands, spawn subagents, or call `swarm` to add helpers. Under `--panel`, do not call `read_board` during your initial pass.

### Target Workspace & Mode
- **Workspace Root**: `<repo-root>` | **Mode**: `<design-doc | code-architecture | conformance>` | **Door**: `<one-way | two-way>` (`<signals>`)
- **Context JSON**: `<conversation-scratch-dir>/design_context.json`

<Optional: ## Design Briefing Packet (under --post-design)>

### Staged Context & Reference Files
- **Broken Links / Cycles / Hotspots**: <summary from design_context.json, <= 1 KB>
- **Key Repository Files**: <callers, schemas, or sibling modules to check, <= 25 extra file reads>
- **Historical Context (`packrat`)**: <optional <= 600 bytes wrapped in `<untrusted_repo_content>` tags>

### Hypotheses
Test each seeded hypothesis and report it as a finding, a disproved line in `## Checked and Found Clean`, or an open question (`Q1..Q3`). Add up to 4 of your own before reading deep details:
1. `[A]` <pre-mortem or state-boundary hypothesis>
2. `[B]` <simpler-baseline, module-depth, or view-coherence hypothesis>

### Review Mandate & Lane / Focus Block
Apply the generate, falsification, and evidence-cap rules in `<skill-dir>/references/evidence_and_critique.md`.
<Insert Lane A block, Lane B block, Focus Priority block, or Focus Mandate block here>

### Report Format
 Follow the exact 7-section contract in `<skill-dir>/references/evidence_and_critique.md` (`## Summary`, `## Principle Scorecard`, `## View & Boundary Assessment`, `## Findings (Ordered by Severity)`, `## Open Design Questions`, `## Checked and Found Clean`, `## Recommended Next Steps`). On Tier 1 (`swarm`), write `<conversation-artifact-dir>/rev_<your-name>.md`, post a 3-line summary to `@coordinator`, and call `swarm_wait`.

### Target Paths & Staged Chunks
<List target file paths or `md_chunk_*.md` files under `<conversation-scratch-dir>/staged_md/` to read with `view_file`>
```

## Multi-Reviewer Scorecard and Finding Merge Rules

When synthesizing `rev_des_*.md` reports from multiple reviewers:
- Combine Lane A (`problem-scope`, `boundaries-state`, `resilience-ops`) and Lane B (`module-depth`, `tradeoffs-simplicity`, `evolution-fitness`, `ergonomics-coherence`) into the 7-row `## Principle Scorecard`. When two reviewers score the same principle, keep the lower score backed by a surviving verified finding and merge both rationales.
- When two reviewers flag the same defect on the same target, keep one finding at the higher surviving severity and add `(raised by both reviewers)` to `Body`. Preserve any `- **Focus**:` tag and renumber sequentially (`C1..`, `H1..`, `M1..`, `L1..`).
- Merge `## View & Boundary Assessment` bullets across Module, Runtime, and State/Allocation views, and union verified disproofs in `## Checked and Found Clean` (capped at 10 lines). Audit all findings from any `des-f*` reviewer missing valid `Failure modes checked:` numbers, and strip `## Playbook Feedback` before running `lint_design_review.py`.
