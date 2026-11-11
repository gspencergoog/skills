# Reviewer Delegation and Roster

This reference defines the delegation tiers, reviewer roster, quota failover, and worker lifecycle shared by `code-review` (prefix `rev-`) and `design-review` (prefix `des-`).

## Delegation Tiers

Select the highest available tier from this hierarchy:

1. **Fast In-Session (`--fast`)**: Spawn zero swarm workers or subagents. Read the staged diff chunks or target files directly, evaluate all criteria/principles and focus themes (`Focus Priority`), apply the self-critique truth filters, and proceed straight to synthesis without citation auditing or challenge loops.
2. **Tier 1: Jetski Root Session (`swarm` tool available)**:
   - Call `swarm{action: "models"}` once before spawning or waking workers.
   - Spawn `<prefix>a` (plus `<prefix>b` under `--panel` and any dedicated `<prefix>f*`) in a single turn via `swarm{action: "add", workspace: "inherit", ...}`. Never add a `monitor` worker.
   - State the total reviewer count in chat, include `[Open the Swarm UI](sidecar://swarm/swarm_board/)`, and **end the turn immediately** on the turn that creates the swarm. Do not call `read_board`, `post_message`, or `swarm_wait` on that first creation turn; those tools mount on the next turn when a worker wakes `@coordinator`. (If the swarm already exists from an earlier pass, call `swarm_wait{since: <last_seq>}` after adding or messaging workers.)
3. **Tier 2a: Claude Code (`Agent` or `Task` tool available, no `swarm`)**:
   - Spawn 1 `general-purpose` subagent (2 under `--panel`, plus 1 per focus reviewer, all in one turn). Pass `model: "opus"` to the single or lane B reviewer when `--claude` or `--opus` is active.
4. **Tier 2b: Jetski Child Subagent (`invoke_subagent` available, `swarm` unavailable or non-root)**:
   - Call `invoke_subagent` with `TypeName: "self"`, `Model: "inherit"`, and `enable_subagent_tools: false`, forbidding child subagent spawning in the prompt.
5. **Tier 3: Restricted Single-Agent or Swarm Worker (no `swarm` / subagent tool, or brief forbids spawning helpers)**:
   - Run the `--fast` in-session flow (running Lane A then Lane B sequentially if `--panel` was requested). Focus themes fold in as Focus Priority.

## Reviewer Roster (`<prefix>` = `rev-` or `des-`)

| Reviewer | Lane / Assignment | Model (Tier 1 `swarm`) | Present when |
| :--- | :--- | :--- | :--- |
| `<prefix>a` | `general`, or `A` under `--panel` | `gemini-launch-candidate-high` (or `opus-5.5-max` with `--claude`/`--opus` in Standard mode) | Every review except `--fast` |
| `<prefix>b` | `B` | `gemini-launch-candidate-high`, or `opus-5.5-max` with `--claude`/`--opus` | `--panel`, `--max` |
| `<prefix>f`, `<prefix>f2`, `<prefix>f3` | `focus:<theme-id>` (one theme each) | `gemini-launch-candidate-high` | Explicit `--focus <id>` (up to 3), or strong triage match above 400 substantive lines within the depth cap |

Tier 2 role names:
- `code-review`: `Code Reviewer`, `Code Reviewer A`, `Code Reviewer B`, `Focus Reviewer (<theme-id>)`.
- `design-review`: `Design Reviewer`, `Design Reviewer A`, `Design Reviewer B`, `Focus Reviewer (<playbook-id>)`.

## Quota Check and Failover

Before spawning or waking workers, call `swarm{action: "models"}` once:

- Failover order: `opus-5.5-max` → `gemini-launch-candidate-high` → `auto` (Gemini Pro Next, a separate quota bucket).
- If the preferred model has zero remaining quota at spawn time, use the next model in the order and report the fallback in chat and in `## Summary`.
- If a worker hits `RESOURCE_EXHAUSTED` or drops out of quota between passes, switch it in place via `swarm{action: "model", name: "<name>", model: "<fallback>"}`.
- Focus reviewers always run on Gemini (`gemini-launch-candidate-high` → `auto`) to protect third-party quota.

## Worker Lifecycle: Reuse vs. Fresh Unanchored Context

- **Stay parked after the review**: Neither `code-review` nor `design-review` kills review workers in Step 6. Leaving `<prefix>*` parked (`waiting`) keeps them available for `--challenge` rounds and iterative loops (`ship-it` homeostasis) without burning roster names (`kill` burns the roster name permanently).
- **Warm reuse (continuity desired)**: For Step 5 `--challenge` rounds or `ship-it` homeostasis loops, check `swarm{action: "status"}` and wake the `waiting` worker via `post_message` + `swarm_wait{since: <last_seq>}`. Never `kill` and re-`add` the same name.
- **Unanchored re-review (fresh context required)**: For external PR re-reviews (`pr-review` Round 2+ after new author commits) or when a prior worker was killed, spawn a fresh worker with an incremented suffix (`<prefix>a-2`, `<prefix>b-2`, `<prefix>f-2`) via `swarm{action: "add"}` so the new pass evaluates the full target unanchored by Round 1's conversation history.
- **In-session re-review (`--fast` or Tier 3)**: Complete the full unanchored pass (Steps 1 through 4) before reading prior threads or incremental diffs, then merge in Step 5.
