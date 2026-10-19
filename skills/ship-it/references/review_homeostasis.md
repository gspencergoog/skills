# Review Homeostasis Loop Guide

Use this guide during Phase 3 of the `ship-it` workflow to execute iterative API and Code reviews until homeostasis is reached.

## 1. Concept of Review Homeostasis

**Homeostasis** is reached when a review pass returns **zero `critical`, `high`, `medium`, and `low` findings** (`Questions` excluded) and all public APIs conform to project standards. Answer each Question once (in the commit message or PR description) and carry it no further.

To prevent context window bloat and maintain independent perspectives:
- Run the API Review Pass using an isolated background subagent (`invoke_subagent` with `TypeName: "self"`, `"Workspace": "inherit"`).
- Run the Code Review Loop directly from the root session using the `code-review` skill. Only the root session can start a `native_swarm`; it spawns `@rev-a` (`gemini-launch-candidate-high`) on pass 1 and wakes it on later passes. When `swarm` is unavailable, `code-review` falls back to `invoke_subagent` (`TypeName: "self"`, `"Workspace": "inherit"`).

______________________________________________________________________

## 2. Execution Procedure

### Step 1: Start API Review Subagent

Invoke a subagent to execute the `api-review` skill on the target files or modified git diff.

```json
{
  "Subagents": [
    {
      "TypeName": "self",
      "Role": "API Review Auditor",
      "Workspace": "inherit",
      "Prompt": "Perform an API review following the api-review skill on the recent changes in the repository. Inspect all public signatures, types, naming conventions, and parameter designs against API best practices. Return a structured list of findings categorized by severity (HIGH, MEDIUM, LOW) with concrete remediation recommendations."
    }
  ]
}
```

### Step 2: Implement API Mitigations

- Review findings returned by the API Review subagent across `HIGH`, `MEDIUM`, and `LOW` severities.
- Apply code changes to fix identified issues.
- Run static analyzer / linter and unit tests to check for regressions.
- Create a git commit (e.g. `refactor(api): apply API review mitigations`).

### Step 3: Run Code Review Loop (`swarm` Primary, Subagent Fallback)

Execute the `code-review` skill on the working directory or branch diff (Steps 1 through 6, skipping Step 7):

- **Tier 1 (Jetski root session with `swarm`)**:
  - Pass 1 spawns `@rev-a` (`model: "gemini-launch-candidate-high"`) with `swarm{action: "add"}` and ends the turn. When `@rev-a` posts its report and calls `swarm_wait`, read the report via `read_board` and record the latest `seq` cursor.
  - Passes 2 through 5 wake the `waiting` `@rev-a` worker via `post_message` (with the new `base_sha`/`head_sha` diff and `Ruled out last pass:` lines) and call `swarm_wait{since: <last_seq>}`.
- **Tier 2b Fallback (when `swarm` is unavailable)**:
  Invoke a `self` subagent:

```json
{
  "Subagents": [
    {
      "TypeName": "self",
      "Role": "Code Review Auditor",
      "Workspace": "inherit",
      "Prompt": "Perform a code review following the code-review skill (reading references/reviewer_mandate.md and references/evidence.md) on the changes on this branch. Do not spawn subagents. Return the structured review report via send_message."
    }
  ]
}
```

### Step 4: Iterative Fix, Oscillation Check & Commit Loop

1. Parse `critical`, `high`, `medium`, and `low` findings from the report. `Questions` do not count toward homeostasis; answer each Question once in the commit or PR description.
2. **Oscillation Detection**:
   - Maintain a list of files and line ranges modified in previous review iterations of the conversation.
   - Flag a potential **oscillation** whenever a finding (especially `LOW` severity) asks to edit lines already modified in a prior pass.
   - **User Resolution**: For any detected oscillation, do not guess or silently skip. Present the conflicting options via `consequence_cards` in `genui` or your question tool, listing your recommended option first with `(Recommended)`.
3. Apply agreed-upon fixes.
4. Run static analyzer and unit test suite to check fixes.
5. Create git commit (e.g. `fix(review): address code review feedback - pass N`).
6. If findings were addressed, run the next review pass (waking `@rev-a` via `post_message` + `swarm_wait{since: <last_seq>}` on Tier 1, or starting a subagent pass on Tier 2). Pass the previous `Checked and Found Clean` lines as `Ruled out last pass:` hypotheses so the reviewer re-tests them only when the new diff touches those lines.
7. **Homeostasis Reached**: Stop looping when a review pass yields zero `critical`, `high`, `medium`, and `low` findings (`Questions` excluded, or after max 5 iterations). **Kill any active swarm workers (`swarm{action: "kill", name: "rev-a"}` / `"rev-b"`) and subagents (`manage_subagents` with Action `'kill_all'`).**

______________________________________________________________________

## 3. Stopping Criteria & Safeguards

- **Homeostasis Condition**: 0 `critical`, `high`, `medium`, and `low` findings reported (`Questions` excluded).
- **Worker & Subagent Cleanup**: Explicitly call `swarm{action: "kill"}` on active swarm reviewers and `manage_subagents` (`Action: 'kill_all'`) when review loops complete so no background workers or subagents remain active.
- **Oscillation Intervention**: `consequence_cards` surface (via `genui`) or interactive question / chat prompt triggered whenever a review recommendation conflicts with code modified in a previous loop pass.
- **Max Iterations Cap**: Maximum 5 review loops per phase. If issues persist after 5 loops, kill swarm workers and subagents, then present remaining items via `triage_matrix` in `genui` or your question tool.
- **Git Checkpoint Policy**: Always commit changes after each loop iteration so progress is checkpointed and revertable.
