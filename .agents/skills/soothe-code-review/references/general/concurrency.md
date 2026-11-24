# Concurrency Safety

Load when a change touches shared state, threads, processes, asynchronous work,
or a runtime whose behavior depends on the calling thread. State only
language-neutral principles; concrete synchronization primitives, library
names, and runtime-specific patterns belong in `../languages/{lang}/`.

## 1. Shared state and races

- Identify every mutable value reachable from more than one execution context.
  Reads and writes need one consistent synchronization policy; a value guarded
  on one path but not another is still a race.
- Watch for compound operations (check-then-act, increment, lazy initialization)
  that appear atomic but are not.
- Prefer immutability, confinement to one execution context, or message passing
  over shared mutable state.
- Caches and memoization require an explicit invalidation and expiry policy;
  an unbounded or never-invalidated cache is a correctness and resource risk.

## 2. Locking

- A mutual-exclusion primitive guarantees exclusion, **not** thread identity.
  Do not use one as a substitute for pinning work to a specific thread.
- Check lock granularity: a lock held across blocking I/O or a remote call
  serializes unrelated work; a lock too narrow fails to protect the invariant.
- Check lock ordering across the call graph. Nested acquisition without a
  single global order, and callbacks invoked while a lock is held, are deadlock
  risks.
- Every acquisition must have a release on all paths, including errors,
  cancellation, and early returns.
- Timeouts and cancellation must not leave a lock held or waiters stranded.

## 3. Execution-context affinity

Some state is valid only in the execution context that created it. Verify that
anything created in one thread, process, or worker is consumed in a compatible
context, or that the state is re-established before use. When a change adds such
state, confirm every consumer honors that requirement.

## 4. Pool and task lifetime

- Thread and process pools must be shut down explicitly, waiting for in-flight
  tasks before the owning object is discarded.
- Tasks submitted but never awaited lose their results and their exceptions.
  Ensure every submitted task has an owner that observes its outcome.
- Bound the number of in-flight tasks and the size of any work queue.
- Cancellation should stop new work, not just discard the handle of work
  already running.

## 5. Failure propagation

- Exceptions raised in a worker must reach the caller or a monitored error
  path. A swallowed worker failure is the concurrency form of silent
  degradation.
- Verify that partial results from one failed worker cannot be combined into a
  seemingly successful aggregate.

## Severity guidance

- Data race, wrong results from thread-affinity violation, lost exceptions, or
  a deadlock reachable in normal operation → Critical.
- Unbounded queues, missing pool shutdown, coarse locking on a hot path →
  Major.
- Redundant synchronization, opportunities for confinement → Minor or
  Learning.
