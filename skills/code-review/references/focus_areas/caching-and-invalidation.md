# Caching and Invalidation

Theme id: `caching-and-invalidation`. Lane for merges: B.

## Cues (for triage)

- New in-memory maps, LRU caches, memoization decorators, HTTP cache headers, or distributed cache reads/writes.
- Changes to cache key construction, TTL values, or eviction policies.
- Write, update, or delete paths added for data that is also read through a cache.

## Failure modes

1. A write, update, or delete path mutates the underlying data without evicting or updating the corresponding cache entry (often masked when tests remove post-mutation read assertions).
   Look at: every mutation path for the cached entity and any test hunks that drop post-write read checks. Settles it: `rg` all writers of the underlying store and check whether each invalidates the cache key; `Read`.
2. The cache key omits a parameter that affects the result (such as tenant, locale, permission scope, or query option), serving one caller's data to another.
   Look at: the cache key builder and all inputs read inside the cached computation. Settles it: compare the set of inputs used in the function body against the components in the key; `Read`.
3. An in-memory cache has no size cap or TTL, growing without bound over the process lifetime as distinct keys arrive.
   Look at: maps or sets stored on long-lived singletons, module globals, or class instances. Settles it: read the container declaration and check for eviction or bounded capacity; `Read`.
4. The cache stores mutable object references, so a caller mutating the returned value corrupts subsequent cache hits.
   Look at: callers of the cache getter and whether values are cloned or frozen on read or write. Settles it: trace what callers do with the returned reference; `Read` or `Deduction`.
5. Failed lookups or transient errors are memoized (such as caching a rejected promise or `null` on error) and poison future calls until restart.
   Look at: how the cache handles exceptions, rejected futures, or error returns from the loader. Settles it: trace the cache state after the loader throws or rejects; `Deduction`.
6. Concurrent misses for the same cold or expired key trigger duplicate expensive loads instead of coalescing or locking per key.
   Look at: the miss path when the loader is asynchronous or releases the lock during fetch. Settles it: trace two concurrent calls missing on the same key; `Deduction`.

## Hypothesis templates

- "Mutating `<entity>` via `<write_fn>` does not invalidate `<cache>`, so `<read_fn>` returns stale data."
- "Cache key in `<fn>` omits `<parameter>`, causing callers with different `<parameter>` values to share cached results."
- "When `<loader_fn>` throws or rejects, `<cache>` retains the failed entry and causes subsequent calls to fail."
- "Caller `<caller>` mutates the object returned by `<cache_getter>`, corrupting the cached value for future callers."
