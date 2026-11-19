# Panel Divergence Protocol (`--panel`)

This reference defines the opt-in multi-perspective divergence workflow for `/design --panel`. It runs an unanchored panel of independent read-only workers, checks cited code claims before synthesis, and surfaces unresolved splits in Phase 2's interactive UI instead of smoothing them into fake consensus.

## Table of Contents

- [1. Opt-In Gate and Roster](#1-opt-in-gate-and-roster)
- [2. Unanchored Brief and Round Formats](#2-unanchored-brief-and-round-formats)
- [3. Cross-Harness Engine Routing](#3-cross-harness-engine-routing)
- [4. Synthesizer Handoff, Follow-Ups, and Teardown](#4-synthesizer-handoff-follow-ups-and-teardown)

## 1. Opt-In Gate and Roster

`--panel` is strictly opt-in because it spawns up to 4 parallel read-only workers:

- Passing `/design --panel` explicitly authorizes launching the 4-worker panel immediately after Phase 1 recon.
- When the user did not pass `--panel` on an open-ended problem, ask for explicit approval before spawning more than two parallel workers.
- Prefix all roster names with `panel-` so they never collide with `/design-review`'s `des-*` workers or other session agents:
  - Two opposing domain personas (`panel-<lens1>`, `panel-<lens2>`, such as `panel-pragmatist` and `panel-architect`, or `panel-latency` and `panel-security`) whose priorities pull in different directions.
  - One red-teamer (`panel-red`): runs a pre-mortem on the problem space in Round 1, and attacks premises shared by every proposal if Round 2 runs.
  - One fact-checker (`panel-facts`): checks every cited file, line, and symbol claim against the live repository with read-only tools. Skip `panel-facts` only for pure conceptual ideation with zero code or document citations, and state that skip explicitly.
- On a second panel in the same conversation, append a non-numeric `-r2` suffix to worker names (`panel-red-r2`, `panel-facts-r2`) and scratch files (`panel_brief_r2.md`) because killed swarm names cannot be reused and numeric suffixes address groups.

Default cadence is **1 round plus fact-check**. Offer Round 2 rebuttals and shared-premise red-teaming only when personas split on a load-bearing tradeoff.

## 2. Unanchored Brief and Round Formats

Workers start fresh and cannot see conversation history. Write `<conversation-scratch-dir>/panel_brief.md` using Phase 1's grounded facts while omitting candidate solutions A/B/C so personas explore independently without anchoring on the coordinator's hunch:

```markdown
# Design Panel Brief: <Topic>

## Background & Verified Baseline
<Objective-isolated problem statement, user-stated hard constraints, Anti-Goals, and verified repo file paths from Phase 1. Do not propose candidate architectures here.>

## Open Trade-Offs to Explore
<2 to 4 open design questions, constraints, or competing priorities.>

## Execution Rules
- Work strictly read-only: never edit files, create commits, write implementation plans, or load `/design` recursively.
- Domain personas compare 2 to 3 distinct options from their assigned lens (including the simplest honest baseline and YAGNI cuts) before recommending one.
- `panel-red` maps failure modes, broken invariants, and runtime traps in the problem space rather than proposing a full architecture.
- Cap Round 1 at 450 words and include a `Claims to Verify` table for every code, config, or doc assertion.
```

### Round 1 Output Format (`<= 450` Words)

Every debater (`panel-<lens1>`, `panel-<lens2>`, `panel-red`) returns five short blocks:

1. One-line position from its assigned lens.
2. Confidence (`Low`, `Medium`, or `High`) and explicit assumptions.
3. A `Claims to Verify` table with columns `Claim ID | Mechanism or Invariant | Exact file:line or Symbol | How to Falsify`.
4. Options evaluated and recommended mechanism (or pre-mortem failure map for `panel-red`), plus an optional signature sketch (`<= 15` lines).
5. Top risks and mitigations.

### Fact-Checker Audit Format (`panel-facts`)

`panel-facts` checks every Claim Tuple from Round 1 against the live workspace using read-only tools and emits one table quoting the checked lines:

| Worker | Claim ID | Verdict (`VERIFIED` / `FALSIFIED` / `UNVERIFIABLE`) | `file:line` Checked and Quoted Evidence | Impact on Proposal |
| :--- | :--- | :--- | :--- | :--- |
| `panel-pragmatist` | `C1` | `FALSIFIED` | `path/to/file.py:14` (`MAX_RETRIES = 2`, not `5`) | Invalidates retry budget estimate |

### Round 2 Output Format (`<= 250` Words, On-Demand)

When the user triggers Round 2 after a panel split, wake only the clashing domain personas plus `panel-red`. Each reads all Round 1 proposals plus the fact-checker audit (treated as ground truth) and replies with:

1. Targeted critiques of opposing proposals grounded in verified facts (for `panel-red`, attack the premises shared by every Round 1 proposal).
2. Peer corrections accepted or rejected, stating which invariant governs each choice.
3. Position shift (`None`, or what changed and which verified point caused the shift).

## 3. Cross-Harness Engine Routing

Borrow only the 3-tier delegation hierarchy and the swarm creation-turn rule from [delegation.md](../../review-core/references/delegation.md). Override `delegation.md` in two ways: keep this protocol model-ID-free (do not hardcode stale model IDs), and tear down `panel-*` workers at Phase 3 instead of leaving them parked indefinitely.

Select the highest available engine and stay on that engine for the full panel:

### Tier 1: Host With `swarm` Tool (Preferred)

1. Call `swarm{action: "models"}` once before spawning. Spread the 4 read-only workers across top-tier models with available quota (falling back to `auto`), honoring the host's model rules and never selecting lite or coordinator tiers.
2. Set or keep a descriptive swarm title (`swarm{action: "title", title: "Design Panel: <topic>"}`).
3. Spawn `panel-<lens1>`, `panel-<lens2>`, and `panel-red` in one turn via `swarm{action: "add", workspace: "inherit", ...}`:
   - Tell each debater to read `<conversation-scratch-dir>/panel_brief.md`, post its Round 1 output ending with `@panel-facts` (and no `@coordinator` mention, so staggered completions do not wake the coordinator early), then park via `swarm_wait` with `since` set to its own post sequence number.
   - If `panel-facts` is skipped (pure conceptual ideation), have debaters end with `@coordinator` instead.
4. Spawn `panel-facts` last via `swarm{action: "add", workspace: "inherit", ...}` with an explicit staggered-completion barrier:
   - On startup, `panel-facts` reads `<conversation-scratch-dir>/panel_brief.md` and calls `swarm_wait` with `since` set to its assignment sequence number.
   - On each wakeup, `panel-facts` calls `read_board{since: <last_seq>, limit: 100}` and checks `swarm{action: "status"}` in separate steps (never batching `read_board` and `swarm_wait` together). It checks newly posted claims and advances `<last_seq>`.
   - If fewer than all active Round 1 debaters (`k = 3`, or an updated count posted by `@coordinator` after stall recovery) have posted since the brief ended, `panel-facts` parks again with `swarm_wait{since: <last_seq>}`.
   - Once all `k` Round 1 debaters have posted, `panel-facts` posts one consolidated Fact-Checker Audit Table ending with `@coordinator` and parks.
5. Schedule a 180-second backup timer (`schedule{DurationSeconds: 180, TimerCondition: "any", Prompt: "Check design panel status for stalled Round 1 workers"}`).
6. Preserve the creation-turn rule from [delegation.md](../../review-core/references/delegation.md): on the turn that creates the swarm, emit the Launch Card below with `[Open the Swarm UI](sidecar://swarm/swarm_board/)` and end your turn without calling `read_board`, `post_message`, or `swarm_wait`. If a swarm already exists, emit the Launch Card or call `swarm_wait{since: <brief_end_seq>}`.
7. On timer or `@coordinator` wakeup, call `swarm{action: "status"}` alone first:
   - If a debater is `waiting` without posting or silent past 10 minutes, kill it (`swarm{action: "kill"}`) and spawn one replacement with a `-retry` suffix (or drop it and post the reduced `k` count to `@panel-facts`).
   - If a worker hits quota exhaustion while `waiting`, switch its model in place with `swarm{action: "model"}` to another top-tier bucket (`auto` as fallback).
   - Once `panel-facts` posts, read all Round 1 proposals and the audit in one `read_board{since: <brief_end_seq>, limit: 100}` call.

### Tier 2: Hosts Without `swarm` (Parallel Subagents)

1. Spawn `panel-<lens1>`, `panel-<lens2>`, and `panel-red` concurrently in one turn (read-only, forbidding child subagent spawning) with `<conversation-scratch-dir>/panel_brief.md`. Emit the Launch Card and await replies.
2. Write all Round 1 replies verbatim into `<conversation-scratch-dir>/panel_round1.md` under `## <persona>` headings. Never paraphrase replies, because paraphrasing drops exact `file:line` citations.
3. Spawn `panel-facts` to check `panel_brief.md` and `panel_round1.md` and return the Fact-Checker Audit Table. Save the audit to `<conversation-scratch-dir>/panel_audit.md`.
4. If the user requests Round 2, resume only the clashing domain personas and `panel-red` pointing to `panel_round1.md` and `panel_audit.md`. Replace a stalled or quota-failed subagent at most once with a `-retry` suffix.

### Tier 3: Single-Agent Fallback

When neither `swarm` nor parallel subagents are available (or you are running inside a child worker), state that parallel workers are unavailable and run a sequential multi-lens pass (2 opposing lenses, pre-mortem red-team, and direct codebase check) clearly labeled as a single-agent fallback.

### Launch Card Format

Emit this 4-line card in chat immediately after launching Round 1:

```markdown
- Question: <topic>
- Panel: panel-<lens1> (<lens>), panel-<lens2> (<lens>), panel-red (pre-mortem), panel-facts (verifier)
- Engine: /swarm (<board link>) | Subagents | Single-agent fallback
- Mode: Round 1 + fact-check (Round 2 offered if panel splits)
```

## 4. Synthesizer Handoff, Follow-Ups, and Teardown

1. Treat the fact-checker audit as ground truth. Drop any proposal point that rests on a `FALSIFIED` claim and record the correction in Phase 2's `## Panel Splits & Codebase Audit` section.
2. Act as a synthesizer rather than an arbiter. Keep genuine persona disagreements visible in the Phase 2 artifact (`| Trade-off | Competing Views | Resolution |`) and map unresolved splits directly into the Phase 2 interactive A2UI surface (see [design_convergence_and_ui.md](design_convergence_and_ui.md)).
3. When personas split and Round 2 has not run yet, include the optional `"Run Round 2 rebuttals"` action in the Phase 2 surface footer so the user can either decide immediately or request one rebuttal and shared-premise red-team pass.
4. While Phase 2 is open, keep panel workers parked (`waiting`) so Round 2 or targeted follow-up questions can route directly to the matching persona without losing investigation context.
5. Once Phase 3 converges (and before launching `/design-review --post-design` so `des-*` workers start cleanly), cancel any active backup timer, tear down all `panel-*` workers (`swarm{action: "kill"}` or `manage_subagents`), and delete all temporary `<conversation-scratch-dir>/panel_*.md` files.
