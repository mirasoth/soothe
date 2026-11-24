# General Scan Checklist

Load in full during `full` review and walk every item. This file states
language-neutral **principles**. Concrete syntax, APIs, and tool names belong in
`../languages/{lang}/review-checklist.md`, never here. Style, naming, and
formatting conventions are owned by language packs and are not duplicated in
this file.

## 1. Security `[security]`

- **Injection into queries**: use parameterized queries; never concatenate
  untrusted values into a query string.
- **Command construction**: never build a shell command from external input.
  Pass an argument list to the process API; when a shell is unavoidable,
  quote and validate every interpolated value.
- **Path traversal**: normalize and validate untrusted paths against an
  allow-listed root before use; allow-list accepted file types and bound file
  sizes.
- **Server-side request forgery**: validate URLs, resolve the host, and reject
  internal, loopback, link-local, and metadata addresses before fetching.
  Treat redirects as new URLs requiring the same validation.
- **Unsafe deserialization**: never deserialize untrusted data with a format
  capable of constructing arbitrary objects; use a safe, explicit schema.
- **Secrets**: never hard-code credentials, tokens, or account passwords in
  source or fixtures; read them from environment or configuration. Never log
  sensitive values.
- **Structured documents with external entities**: disable external entity
  resolution, and bound document size and nesting depth.
- **External files**: create temporary files through a secure temporary-file
  facility with randomized names; never reuse a fixed name in a shared
  location.

## 2. Potential bugs `[bug]`

- Null/empty/absent dereferences and empty-input paths.
- Deferred work that reads state which has changed before it runs.
- Shared mutable state initialized once and silently reused across calls.
- Overly broad exception handling that discards failures.
- Boundaries: zero, negative, oversized, empty collections, off-by-one, and
  first/last element cases.
- Asynchronous work: unawaited operations, exceptions not propagated, locks
  not released on all paths, cancellation not honored.
- State: shared mutable state, cache invalidation, staleness, and expiry.
- **Silent degradation**: catching a failure without logging it, re-raising it,
  or establishing a fallback. The failure is swallowed, execution continues on
  invalid state, and the real defect surfaces later in a much harder-to-diagnose
  form. Treat as at least Major.

## 3. Performance `[performance]`

- Repeated string concatenation in loops; repeated computation or I/O that
  could be hoisted or cached.
- Unnecessary copies of large objects and allocation in hot paths.
- N+1 access patterns and blocking I/O on a hot path.
- Missing pre-allocation and unbounded growth in collections or caches.
- Linear membership checks where a hashed structure is appropriate.
- **Nested concurrency**: independently configured concurrency levels compound,
  so verify the total resource demand rather than raising each one in isolation.

## 4. Resource management `[resources]`

- File handles, connections, locks, thread/process pools, and subscriptions
  must be released on every path, including error and cancellation paths.
- Pool shutdown must wait for in-flight work; a pool dropped without shutdown
  leaks tasks or silently loses results.
- Large in-memory buffers and caches must have a bounded lifetime and an
  explicit release.
- Temporary artifacts must be cleaned up by a construct that runs on every
  exit path.

## 5. Testing `[testing]`

- Cover happy path, boundary conditions, and error/failure paths.
- Tests must be readable and deterministic: no flakiness, no reliance on wall
  clock timing or execution order, no dependence on external services.
- Isolate external dependencies with fakes or mocks; parameterize to remove
  duplication without hiding cases.
- Assert against values derived from the implementation under test, not
  hard-coded magic constants that drift.
- When a change alters behavior, verify a test would fail without the change.

## 6. Conciseness `[conciseness]`

Change only **quantifiable logical duplication**: near-identical functions that
collapse into one parameterized implementation, identical trailing assignments
across many branches, or two branches differing only in one value that can be
extracted into a variable.

Do **not** simplify apparent duplication that carries distinct semantics,
encodes a public contract, or exists for type checking. Quantify the
duplication first; when in doubt, leave the code alone.

After any such simplification, keep the external signature, parameters,
user-visible messages, and boundary semantics unchanged, and confirm with the
repository's tests that there is no regression.
