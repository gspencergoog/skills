# Distributed State and Consistency

Playbook id: `distributed-state-and-consistency`. Primary principles: `boundaries-state`, `resilience-ops`. Lane for merges: A.

## Cues (for triage)

- Multi-step mutations across databases, local files, caches, message queues, or external RPCs.
- Background workers, daemons, polling loops, or retry handlers that mutate shared state.
- Caching layers, materialized views, or state synchronization between processes or worktrees.

## Failure modes

1. Retrying a timed-out mutation without a deterministic idempotency key duplicates records, side effects, or outbound notifications.
   Look at: retry policies, queue consumers, and RPC/CLI mutation entry points in the spec or module. Settles it: trace a timeout after the server commits step 1 and verify whether the retry deduplicates by key; `Read` or `Deduction`.
2. Dual writes to two storage systems (such as SQLite plus a JSON file, or a database plus a cache) execute without an atomic transaction, outbox log, or reconciliation pass, leaving permanent state drift when the second write fails.
   Look at: sequence diagrams and write paths touching more than one persistence store or file. Settles it: trace a process crash between write 1 and write 2 and check how readers recover consistency; `Deduction`.
3. Two modules, workers, or concurrent CLI invocations write to the same table, state file, or cache key without single-writer ownership, advisory locking, or compare-and-swap versioning.
   Look at: component ownership boundaries, file write paths, and concurrency sections across views. Settles it: identify all writers to the state store via spec tables or `rg` and check lock or version guards; `Read` or `Executed`.
4. Out-of-order, delayed, or replayed events overwrite newer state because consumers apply payloads blindly instead of checking a monotonic sequence number, watermark, or causal version.
   Look at: event handlers, webhook receivers, watch loops, and cache invalidation paths. Settles it: trace arrival of event $N-1$ after event $N$ and check whether stale data replaces newer fields; `Deduction`.
5. A multi-step workflow defines happy-path transitions in its state diagram but omits compensating rollback steps or dead-letter handling when an intermediate step fails halfway through.
   Look at: `stateDiagram-v2` transitions, error branches, and cleanup hooks. Settles it: inspect every non-terminal state in the diagram or code and verify a defined transition on error or timeout; `Read`.
6. Read-through or write-around caches lack an explicit invalidation trigger or bounded TTL, serving stale authorization, configuration, or status records after an upstream write.
   Look at: cache key definitions, TTL values, and mutation paths that bypass the cache wrapper. Settles it: trace a mutation followed immediately by a read across process boundaries; `Deduction`.

## Hypothesis templates

- "Retrying `<operation>` after a timeout duplicates `<side_effect>` because `<entry_point>` does not require or persist an idempotency key."
- "If the process crashes after writing `<store_a>` but before updating `<store_b>`, `<reader>` observes permanently inconsistent state with no reconciliation path."
- "Concurrent execution of `<writer_a>` and `<writer_b>` corrupts or overwrites `<state_resource>` because neither enforces single-writer locking or version checks."
- "Delivering `<event_or_update>` out of order causes `<consumer>` to overwrite newer state at `<target>` because payloads carry no watermark or sequence check."
- "Failure at `<intermediate_state>` leaves `<resource>` stranded because the state machine defines no compensating rollback or expiration transition."
