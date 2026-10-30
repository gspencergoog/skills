# Resource Lifecycle

Theme id: `resource-lifecycle`. Lane for merges: A.

## Cues (for triage)

- New file descriptors, sockets, database connections, streams, subscriptions, timers, or native buffers are opened.
- Cleanup blocks (`close`, `dispose`, `cancel`, `free`, `defer`, `with`, `finally`) are added, moved, or removed.
- Ownership of a handle moves between a factory, a caller, a cache, or a struct field.

## Failure modes

1. A resource is opened before a fallible step, and an early return or exception exits without closing it.
   Look at: every path between resource acquisition and its guard (`defer`, `with`, `try`/`finally`, or `dispose`). Settles it: trace early returns and thrown exceptions between open and close; `Read` or `Deduction`.
2. Composite resources are closed or disposed in the wrong order, flushing or referencing a child handle after it closed.
   Look at: `dispose`, `close`, or destructor methods that tear down multiple fields or wrappers. Settles it: read the teardown sequence and compare it against the construction dependency order; `Read`.
3. Ownership transfers to a caller or container while the creator also closes the handle on scope exit, causing double-close or use-after-close.
   Look at: factory helpers or methods that store or return a handle opened inside a scoped guard. Settles it: trace the handle lifetime across the return boundary; `Deduction`.
4. Reassigning a field that holds an active subscription, timer, or stream overwrites the previous handle without canceling it first.
   Look at: assignments to handle fields in `init`, `update`, reconnect, or reload methods. Settles it: trace two consecutive calls to the method and check whether the first handle is closed; `Deduction`.
5. Callbacks, listeners, or background tasks continue to fire after the owning component is disposed and access closed state.
   Look at: event subscriptions, timers, or completion callbacks registered during setup. Settles it: check whether `dispose` cancels each registration and whether callbacks guard against closed state; `Read`.

## Hypothesis templates

- "If `<fallible_call>` fails at `<file:line>`, the `<resource>` opened on line `<open_line>` is leaked without being closed."
- "Calling `<reinit_method>` a second time overwrites `<handle_field>` without closing or canceling the previous instance."
- "Function `<factory_fn>` closes `<resource>` in its `finally`/`defer` block even when ownership was returned to `<caller>`."
- "After `<owner>.dispose()` runs, callback `<callback>` can still fire and access closed `<resource>`."
