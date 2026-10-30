# Concurrency and Locking

Theme id: `concurrency-and-locking`. Lane for merges: A.

## Cues (for triage)

- Diff introduces or changes locks, mutexes, semaphores, channels, atomics, threads, goroutines, or isolates.
- Lock acquisition scope, granularity, or ordering changes across methods or callbacks.
- Mutable fields, maps, or queues become shared across worker tasks or event handlers.

## Failure modes

1. Two code paths acquire the same pair of locks in opposite orders, creating a deadlock cycle under concurrent load.
   Look at: every function acquiring more than one lock, or calling another locked helper while holding a lock. Settles it: trace the lock acquisition graph across callers with `path:line` citations; `Deduction`.
2. An early return, panic, or exception path exits a critical section without releasing the acquired lock or permit.
   Look at: lock acquisitions not bound to `defer`, `with`, `try`/`finally`, or RAII guards. Settles it: read every exit path between acquire and release; `Read`.
3. A check-then-act sequence inspects state outside a lock (or across two separate lock acquisitions) and acts on stale assumptions.
   Look at: `if` checks on shared state followed by a release, re-acquire, or unlocked write. Settles it: trace a second worker mutating the state between the check and the action; `Deduction`.
4. Shared mutable collections or fields are read or written from concurrent tasks without synchronization or atomic operations.
   Look at: maps, slices, lists, or counters captured by closures, callbacks, or worker threads. Settles it: `rg` all reads and writes of the field and check the enclosing synchronization; `Read`.
5. An initialization or start routine can be triggered twice concurrently, spawning duplicate background workers or overwriting active handles.
   Look at: `start`, `init`, or lazy-loading methods lacking an atomic state transition or single-flight guard. Settles it: trace two concurrent calls entering the method before the state flag is set; `Deduction`.
6. Holding a lock across a blocking I/O call, channel send, or user callback stalls other waiters or re-enters the lock.
   Look at: network calls, disk writes, or external callbacks invoked inside a locked region. Settles it: read the critical section body and trace what each callee invokes; `Read` or `Deduction`.

## Hypothesis templates

- "Path `<fn_a>` acquires `<lock_1>` then `<lock_2>`, while `<fn_b>` acquires `<lock_2>` then `<lock_1>`, allowing a deadlock."
- "When `<operation>` throws or returns early at `<file:line>`, `<lock_or_semaphore>` is never released."
- "Between checking `<condition>` and mutating `<state>` in `<fn>`, another concurrent caller can modify `<state>`."
- "Field `<field>` is mutated by `<worker_a>` and read by `<worker_b>` without holding `<lock>`."
