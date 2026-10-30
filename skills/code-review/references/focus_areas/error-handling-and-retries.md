# Error Handling and Retries

Theme id: `error-handling-and-retries`. Lane for merges: A.

## Cues (for triage)

- Exception types, error codes, or result wrappers change at a module, RPC, or CLI boundary.
- New retry loops, backoff policies, circuit breakers, or fallback branches appear.
- `catch`, `except`, or error-return branches are added, broadened, or removed.

## Failure modes

1. A broad catch or fallback branch swallows unexpected exceptions, turning programming bugs or fatal errors into silent successes.
   Look at: `catch (Exception)`, `except Exception`, ignored `err` returns, or default fallback values. Settles it: read the catch block and trace what callers observe when a non-transient error occurs; `Read`.
2. Changing the thrown exception type or returned error code breaks callers that branch on the previous type or status.
   Look at: error types raised in the diff and all upstream `catch` or error-matching clauses. Settles it: `rg` callers for the old and new error types and inspect their handlers; `Read`.
3. The retry loop re-executes a non-idempotent operation after a timeout or partial failure, duplicating writes or charges.
   Look at: the retried callable and any state changes or external requests it performs before failing. Settles it: trace what happens when the first attempt succeeds remotely but fails while reading the response; `Deduction`.
4. Retries run immediately without jitter, or non-positive retry bounds (`max_attempts <= 0`) skip the loop and fail on post-loop guards.
   Look at: sleep/delay calculations, retry bounds validation, and post-loop `assert` or `raise` statements. Settles it: read the retry configuration and trace `max_attempts <= 0` and worst-case attempt rate; `Read` or `Deduction`.
5. A multi-step operation fails midway and leaves in-memory state, files, or database rows half-applied without rollback.
   Look at: sequences of mutations that precede a fallible call before the function returns. Settles it: trace the state of each modified object when step N throws; `Deduction`.
6. Error wrapping drops the original cause or stack trace, or omits context needed to diagnose the failure.
   Look at: `throw new`, `raise ... from None`, or `fmt.Errorf` calls without `%w`. Settles it: read the error construction and check whether callers inspect `cause` or `errors.Is`; `Read`.

## Hypothesis templates

- "Caller `<caller>` catches `<old_error>` specifically, so throwing `<new_error>` from `<fn>` bypasses its recovery path."
- "Retrying `<operation>` after a transport timeout can execute `<non_idempotent_step>` twice."
- "When `<step_2>` fails in `<fn>`, the mutation applied to `<target>` in `<step_1>` is not rolled back."
- "The catch block in `<fn>` swallows `<error_type>` and returns `<default_value>`, hiding permanent failures from callers."
