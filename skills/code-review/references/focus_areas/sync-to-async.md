# Sync-to-Async Conversion

Theme id: `sync-to-async`. Lane for merges: A.

## Cues (for triage)

- Function or method signatures gain `async`, `Future`, `Promise`, `Task`, or coroutine return types.
- Callbacks or synchronous returns become `await` expressions or `.then()` chains.
- Blocking file, network, or subprocess operations move onto an event loop or async executor.

## Failure modes

1. A caller invokes the converted function without `await`, silently dropping the returned `Future`, `Promise`, or `Task` before completion.
   Look at: all callers of changed signatures across the repository. Settles it: `rg` every caller and inspect how the return value is handled; `Read`.
2. Exceptions thrown inside an un-awaited future or background task escape without an attached error handler, or the unreferenced task is garbage-collected mid-flight.
   Look at: task-spawning helpers (`create_task`, `unawaited`, floating promises, `go` blocks) and whether task handles are retained. Settles it: trace error propagation and task lifetime from the spawn site; `Deduction` or `Read`.
3. Callers read shared state or return values immediately after launching the async call, assuming synchronous completion.
   Look at: statements following the call site and any state mutated inside the async body. Settles it: trace execution order across the suspension point with `path:line` citations; `Deduction`.
4. Suspending at `await` allows a second invocation to interleave and mutate the same instance or module state.
   Look at: instance fields, collections, or caches read before `await` and written after it. Settles it: trace two concurrent calls through the `await` boundary; `Deduction`.
5. Blocking I/O, `sleep`, synchronous filesystem checks (`mkdir`, `exists`), or heavy CPU work remains on the event loop thread.
   Look at: synchronous filesystem, socket, lock, or helper calls executed before or inside `to_thread` and `async` functions. Settles it: read the called API declarations on disk to confirm whether they block; `Read`.
6. Existing tests pass only because fake or mock implementations complete synchronously or fail to await the call under test.
   Look at: test doubles and test assertions for the converted functions. Settles it: drop one `await` or insert a pending delay in the fake and run the test; `Executed` under `--verify`.

## Hypothesis templates

- "Caller `<caller>` invokes `<async_fn>` without awaiting its result, so side effects or errors after the first suspension point are lost."
- "If `<async_fn>` is entered twice before its first `await` resolves, both executions interleave and corrupt `<shared_state>`."
- "Calling synchronous `<blocking_call>` inside `<async_fn>` blocks the event loop while waiting on I/O."
- "Test `<test_name>` still passes if `await` is removed from `<call_site>` because `<fake_or_mock>` resolves synchronously."
