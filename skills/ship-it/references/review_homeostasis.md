# Review Homeostasis Loop Guide

Use this guide during Phase 3 of the `ship-it` workflow to execute iterative API and Code reviews using background subagents until homeostasis is reached.

## 1. Concept of Review Homeostasis

**Homeostasis** is reached when a code review pass returns **zero findings** (across `HIGH`, `MEDIUM`, and `LOW` severities), and all public APIs strictly conform to project standards.

To prevent context window bloat and maintain fresh perspectives, perform review passes using **isolated background subagents** (`invoke_subagent` tool with `research-google` or `self` role, specifying `"Workspace": "inherit"` to inspect current working tree state).

---

## 2. Execution Procedure

### Step 1: Launch API Review Subagent
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
- Run static analyzer / linter and unit tests to confirm no regressions.
- Create a git commit (e.g. `refactor(api): apply API review mitigations`).

### Step 3: Launch Code Review Subagent Loop
Invoke a subagent to execute the `code-review` skill on the working directory or branch diff.

```json
{
  "Subagents": [
    {
      "TypeName": "self",
      "Role": "Code Review Auditor",
      "Workspace": "inherit",
      "Prompt": "Perform a comprehensive code review following the code-review skill on the changes on this branch. Audit for code clarity, edge cases, error handling, performance pitfalls, and standard compliance. Return a list of findings categorized by severity (HIGH, MEDIUM, LOW) with actionable fix suggestions."
    }
  ]
}
```

### Step 4: Iterative Fix, Oscillation Check & Commit Loop
1. Parse findings from the subagent response (`HIGH`, `MEDIUM`, and `LOW`).
2. **Oscillation Detection**:
   - Maintain a list of files and line ranges modified in previous review iterations of the conversation.
   - If a finding (especially `LOW` severity) suggests modifying lines that were already edited in a previous pass, flag it as a potential **oscillation**.
   - **User Resolution**: For any detected oscillation, do not guess or silently skip. Call `ask_question` to present the conflicting recommendations to the user, explain the tradeoff, and ask how they prefer to resolve it.
3. Apply agreed-upon fixes.
4. Run static analyzer and unit test suite to verify fixes.
5. Create git commit (e.g. `fix(review): address code review feedback - pass N`).
6. If findings were addressed, launch another subagent pass to re-audit.
7. **Homeostasis Reached**: Stop looping when a review pass yields zero findings (or after max 5 iterations). **Terminate all active subagents using `manage_subagents` with Action `'kill_all'`.**

---

## 3. Stopping Criteria & Safeguards

- **Homeostasis Condition**: 0 findings reported across all severity levels (`HIGH`, `MEDIUM`, and `LOW`).
- **Subagent Cleanup**: Explicitly call `manage_subagents` (`Action: 'kill_all'`) when review loops complete to ensure no background subagent processes remain active.
- **Oscillation Intervention**: `ask_question` prompt triggered whenever a review recommendation conflicts with code modified in a previous loop pass.
- **Max Iterations Cap**: Maximum 5 review loops per phase. If issues persist after 5 loops, terminate subagents (`manage_subagents` with `'kill_all'`), pause, and present remaining items to the user via `ask_question`.
- **Git Checkpoint Policy**: Always commit changes after each loop iteration so progress is checkpointed and revertable.
