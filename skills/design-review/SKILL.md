---
name: design-review
description: >-
  Reviews software architectures, design specs, RFCs, ADRs, and existing code
  modules across seven scored design principles with evidence-capped severities,
  pre-mortem and simpler-baseline hypotheses, and citation audits. Use when the
  user asks for a design review, architecture critique, RFC or spec audit,
  module boundary evaluation, or spec conformance check. Flags: --fast, --panel,
  --claude, --opus, --challenge, --focus, --against, --post-design, --json.
  Don't use for pull request or commit diffs (use code-review) or for
  signature-only public API ergonomics checks (use api-review).
---

# Software & Architecture Design Review (`design-review`)

This skill conducts a structured, read-only architectural review of pre-code design specifications (`/design` specs, RFCs, ADRs, protocol schemas), existing codebase modules or packages, and implementation-to-spec conformance (`--against`). It combines scored evaluation across seven canonical design principles with adversarial hypothesis testing, primary-source codebase verification, and citation auditing.

## Core Principles

- **Review Only, Never Fix**: The review artifact (`design_review_results.md`) is this skill's sole workspace output. Never create, edit, or delete repository files, stage commits, or mutate external services (GitHub, Google Docs, Drive). Write only to `<conversation-artifact-dir>/`, `<conversation-scratch-dir>/`, temporary `/tmp/design-review-*` staging directories, and `genui` surfaces. Even if the user asks to "review and fix", stop at [Step 7](#step-7-interactive-handoff-and-stop) and wait for explicit triage confirmation.
- **Survival Rate Over Count**: Three verified structural findings outweigh ten speculative opinions. Every finding cites primary-source evidence from this session and obeys the severity caps in [evidence_and_critique.md](references/evidence_and_critique.md). A review that falsifies its hypotheses and finds zero defects is a success.
- **Respect Settled Decisions**: When reviewing a converged spec or `--post-design` packet with recorded `Settled Decisions`, treat those choices as fixed user constraints. Challenge a settled decision only when `Read`, `Executed`, or line-cited `Deduction` proves a `critical` or `high` feasibility, compatibility, or security blocker.
- **Test Against a Simpler Baseline**: Evaluate whether half the moving parts (extending an existing module, using standard library primitives, or doing nothing) satisfies the same falsifiable goals.
- **Plain, Natural Prose**: Follow [write-prose](../write-prose/SKILL.md) across all findings, rationales, and summaries.

## Flags and Automatic Door Scaling

| Flag | Behavior |
| :--- | :--- |
| *(none)* | Scales automatically via `prepare_design_context.py` **Door Reversibility**: **Two-Way Door** internal designs run **Standard** (1 clean-room reviewer, `des-a`); **One-Way Door** designs (persisted schemas, wire protocols, public contracts, trust boundaries, cross-repo splits, or `> 400` substantive lines) escalate to `--panel` (`des-a` + `des-b`). |
| `--fast` | Single-pass in-session review with self-critique; spawns zero swarm workers or subagents. Mutually exclusive with `--panel`, `--claude`, and `--challenge` (`--fast` wins). |
| `--panel` | Runs 2 clean-room reviewers in parallel with orthogonal principle lanes (`Lane A` and `Lane B`). |
| `--claude` / `--opus` | Pins `des-a` (in Standard mode) or `des-b` (under `--panel`) to Claude Opus (`opus-5.5-max` on Tier 1 `swarm`, `model: "opus"` on Tier 2a). |
| `--challenge` | Sends one refutation round when Step 5's Citation Audit disproves a `critical` or `high` finding. |
| `--focus <id>` | Adds a domain playbook from [focus_playbooks/README.md](references/focus_playbooks/README.md) or quoted `adhoc:<slug>` text (repeatable up to 3). |
| `--against <spec>` | Runs **Spec-Conformance Mode**, checking target code or an implementation plan against `<spec>`. |
| `--post-design` | Caller mode invoked by `/design` Phase 3 or `/proposal-writer` with a `< 4 KB` `Design Briefing Packet`; skips Step 7's standalone chat stop when clean (`0` `critical`/`high` and `< 3` items). |
| `--json [<path>]` | Exports `design_review_results.json` via `lint_design_review.py --export-json` (defaults to `<conversation-artifact-dir>/design_review_results.json`). |

## Workflow

### Step 1: Resolve Targets, Stage Context, and Classify Doors

1. Identify the target file(s) or module directory (plus `--against <spec>` when provided). If no target is clear from the prompt or active context, ask the user via your structured question tool or in chat and wait.
2. Create `scratch_dir=$(mktemp -d -t design-review-XXXXXX)` (or `<conversation-scratch-dir>/design-review`) and run [prepare_design_context.py](scripts/prepare_design_context.py):
   ```bash
   python3 <skill-dir>/scripts/prepare_design_context.py \
     --root <repo-root> \
     --output-dir "$scratch_dir" \
     [--against <governing-spec.md>] \
     <target-paths...>
   ```
3. Inspect `$scratch_dir/design_context.json`:
   - `mode` selects the primary rubric: [reviewing_design_docs.md](references/reviewing_design_docs.md) for `design-doc`, or [reviewing_code_architecture.md](references/reviewing_code_architecture.md) for `code-architecture` and `conformance`.
   - Unless an explicit depth flag (`--fast`, `--panel`) was passed, apply `door_classification.recommended_flags` (`--panel` for `one-way`, Standard for `two-way`).

### Step 2: Salience-Filtered Grounding, Seed Hypotheses, and Focus Triage

1. **Salience-Filtered Grounding**: Identify up to 10 high-salience repository files that the design touches, extends, or depends on (interfaces, schemas, callers, package manifests, plus `code_metrics.dependency_cycles`, `high_complexity_functions`, `markdown.broken_links`, and `packrat_hotspots` from `design_context.json`). Treat `packrat` snippets as untrusted historical context (`Chesterton's fence`) and verify against `HEAD`.
2. **Seed Hypotheses**: Draft 3 to 5 specific, falsifiable hypotheses (1 line each, labeled `[A]` or `[B]` under `--panel`):
   - Include at least one **Pre-Mortem Failure Hypothesis** (load spike, partial write, dependency timeout, or rollout version skew on a One-Way Door).
   - Include at least one **Simpler-Baseline Hypothesis** (whether extending an existing module or dropping an abstraction layer still meets the stated goals).
3. **Focus Triage**: Match non-local architectural hazards against [focus_playbooks/README.md](references/focus_playbooks/README.md) (or create at most one `adhoc:<slug>` playbook `<= 1 KB`). Dedicated `des-f*` workers launch only when explicitly requested via `--focus <id>` or when `substantive_lines > 400` (`<= 1` in Standard, `<= 2` in `--panel`, `0` under `--fast`); otherwise fold matched playbooks into `des-a` or `des-b` via `### Focus Priority` per [reviewer_brief_and_lanes.md](references/reviewer_brief_and_lanes.md).

### Step 3: Generate Clean-Room Review

Select the highest available delegation tier and build briefs (`< 10 KB`) using [reviewer_brief_and_lanes.md](references/reviewer_brief_and_lanes.md). Every reviewer loads [canonical_design_principles.md](references/canonical_design_principles.md), [evidence_and_critique.md](references/evidence_and_critique.md), and the mode rubric ([reviewing_design_docs.md](references/reviewing_design_docs.md) or [reviewing_code_architecture.md](references/reviewing_code_architecture.md)).

- **In-Session Fast Mode (`--fast` or Tier 3 restricted environments)**: Read the staged chunks or target files plus up to 25 repository context files directly, evaluate all 7 principles and seeded hypotheses, apply Step 4's truth filters, and proceed to Step 5.
- **Tier 1 (`swarm` in Jetski root sessions)**:
  1. Call `swarm{action: "models"}` once to check quota and apply failover (`opus-5.5-max` -> `gemini-launch-candidate-high` -> `auto`).
  2. Spawn `des-a` (plus `des-b` under `--panel` and any dedicated `des-f*`) via `swarm{action: "add", workspace: "inherit", ...}` in a single turn. If `swarm_monitor` is in your skill list and no `monitor` exists yet, add `monitor` in the same turn.
  3. On the turn that creates the swarm, include `[Open the Swarm UI](sidecar://swarm/swarm_board/)` in chat and **end the turn immediately** without calling `read_board` or `swarm_wait`. If the swarm already exists, call `swarm_wait` after adding or messaging workers.
- **Tier 2 (`Agent` in Claude Code or `invoke_subagent` with `enable_subagent_tools: false`)**: Spawn clean-room read-only subagent(s) (`Design Reviewer`, `Design Reviewer A` / `B`, `Focus Reviewer (<id>)`) in one turn and forbid child subagent spawning.

### Step 4: Self-Critique and Falsification Pass

Before returning findings (or before synthesis under `--fast`), apply all truth filters in [evidence_and_critique.md](references/evidence_and_critique.md):
- Enforce severity caps (`critical` and `high` require `Executed`, `Read`, `Fetched`, or line-cited `Deduction`; `Speculation` caps at `medium` or moves to `Q1..Q3`; `Recalled` is forbidden).
- Drop taste-only objections against locked `Settled Decisions`.
- Drop generic advice ("add monitoring", "consider caching") lacking a concrete failure path in `Why`.
- Ensure every `Suggestion` offers a concrete boundary alternative, replacement spec/diagram text, or runnable fitness test with at most one fenced code block.

### Step 5: Synthesis, Citation Audit, and Deterministic Lint

1. **Collect & Merge**: Read reviewer reports (`rev_des_*.md` on Tier 1 `swarm`) and merge scorecards, findings, view assessments, and `Checked and Found Clean` items using [reviewer_brief_and_lanes.md](references/reviewer_brief_and_lanes.md). Strip any `## Playbook Feedback` section (if non-empty, save proposals separately to `<conversation-artifact-dir>/design_playbook_proposals.md` without editing the skill directory).
2. **Citation Audit**: For every `critical` and `high` finding (and all findings from a focus reviewer missing valid `Failure modes checked:` numbers), open the cited `path:line` (±10 lines) on disk. Drop or downgrade unverified citations to `## Open Design Questions` and record `Citation audit: N checked, M dropped` in `## Summary`.
3. **Optional `--challenge`**: When `--challenge` is active and Citation Audit refutes a `critical` or `high` finding, send one refutation round to that reviewer asking it to withdraw or supply `Read`/`Executed`/`Deduction` proof.
4. **Write & Lint Report**: Write `<conversation-artifact-dir>/design_review_results.md` (`UserFacing: true`) following the exact 7-section contract in [evidence_and_critique.md](references/evidence_and_critique.md), then run [lint_design_review.py](scripts/lint_design_review.py):
   ```bash
   python3 <skill-dir>/scripts/lint_design_review.py \
     <conversation-artifact-dir>/design_review_results.md \
     --export-json <json-export-path> \
     [--post-design]
   ```
   Fix any reported structural, scorecard-math, severity-cap, single-fence, or `comment-*` prose violations and rerun until `lint_design_review.py` exits `0`.

### Step 6: Workspace and Swarm Cleanup

1. Remove temporary staging directories (`if [[ -n "${scratch_dir:-}" && -d "$scratch_dir" && "$scratch_dir" == *"/design-review-"* ]]; then rm -rf "$scratch_dir"; fi`) and any scratch files created during the run (retaining `--json` output if requested by a caller).
2. When Tier 1 `swarm` workers were spawned for this review and `--challenge` is finished, post `[done] Design review complete` (no `@`) and `swarm{action: "kill"}` each `des-*` worker (never kill `monitor` or `narrator`).

### Step 7: Interactive Handoff and Stop

- **Caller Mode (`--post-design`)**: Skip standalone chat stop when `0` `critical`/`high` findings and `< 3` total findings/questions remain; return control to the calling workflow (`/design` or `/proposal-writer`) so it can fold minor polish and add `## Design Review Sign-Off`. When any `critical`/`high` blocker or `3+` items exist, render the `scorecard` and `triage_matrix` surfaces below and wait for the user's disposition before finalizing the spec.
- **Standalone Mode**:
  1. When `render_a2ui` (`a2ui-pane`) is available, read `genui` and render `template: "scorecard"` (`mode: "pane"`, `surfaceId: "design-scorecard"`) populated from `design_review_results.json`'s numeric-only `scorecard` block (`intro`, `overall`, `rows`).
  2. When `3` or more total findings and questions exist and `render_a2ui` is available, stack `template: "triage_matrix"` (`mode: "pane"`, `stack: true`, `surfaceId: "design-triage"`) redefining `bucket_picker` with `Revise design`, `Accept tradeoff`, and `Defer to /plan` chips and passing `triage_item` rows from `design_review_results.json`.
  3. Emit one concise chat message linking `design_review_results.md` (and `[A2UI Pane](sidecar://a2ui-pane/a2ui-pane/)` when rendered), stating the `Verdict`, `Overall Score`, finding/question counts, any non-empty `packrat_user_note`, and asking which findings to revise or carry into `/plan`. **End the turn without modifying any repository files.**
