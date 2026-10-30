# Feature Flag and Rollout

Theme id: `feature-flag-and-rollout`. Lane for merges: B.

## Cues (for triage)

- New feature flags, experiment gates, environment switches, or runtime config toggles.
- Flag cleanup diffs that remove a flag and collapse conditional branches.
- Dual-write, shadow-read, or percentage-rollout routing logic.

## Failure modes

1. Tests exercise only the flag-enabled (or only the flag-disabled) branch, leaving the other path broken in production.
   Look at: test files touching the gated component and how the flag value is configured in fixtures. Settles it: `rg` the flag name across `test/` and check whether both states are asserted; `Read`.
2. When the flag service is unreachable or the key is missing from config, the default value enables an unready feature or breaks startup.
   Look at: the fallback value in the flag evaluation call and its behavior on missing config. Settles it: read the flag declaration and trace evaluation with an empty config map; `Read` or `Deduction`.
3. Reading a runtime-updatable flag twice within one request or lifecycle runs half the operation on the old path and half on the new path.
   Look at: multiple reads of the same runtime-updatable flag across a single request, transaction, or UI render cycle. Settles it: trace what happens if the flag flips between the first and second read; `Deduction`.
4. Turning the flag off after activation (kill switch) fails when the new path wrote state or records that the old path rejects.
   Look at: data written when the flag is on and how the flag-off branch reads that same state. Settles it: trace the flag-off read path against state produced while the flag was on; `Deduction`.
5. Removing a retired flag deletes the wrong branch, leaves dead config or helper code behind, or invert-flips a negated condition.
   Look at: boolean negations (`!flag`, `unless`) around the removed check and remaining references to the flag. Settles it: compare the kept branch against the intended permanent state and `rg` the flag name; `Read`.
6. During a gradual rollout, mixed instances with different flag states exchange messages, cache entries, or URLs that the other state rejects.
   Look at: shared caches, queues, RPC payloads, or session tokens touched by both flag branches. Settles it: trace an instance with the flag off reading a payload produced with the flag on; `Deduction`.

## Hypothesis templates

- "No test in `<test_suite>` exercises `<fn>` with `<flag_name>` set to `<untested_state>`."
- "If `<flag_name>` flips mid-request between `<call_1>` and `<call_2>`, `<fn>` mixes the old and new paths."
- "Disabling `<flag_name>` after it has run leaves `<persisted_state>` in a format the legacy branch in `<fn>` rejects."
- "Removing negated check `!<flag_name>` at `<file:line>` kept the disabled branch instead of the enabled branch."
