# Path and IO Safety

Load when changed code accepts or constructs a path, reads or writes a file or
stream, creates temporary artifacts, or passes credentials to an IO layer. Skip
this file when the change has no IO surface.

Language-agnostic by contract: no language syntax, language-specific APIs,
product paths, or product identifiers belong here. The concrete shape of a path
or stream abstraction belongs to the reviewed repository and to the applicable
language pack; derive it from the code under review, never assume it.

## 1. Untrusted paths

- Normalize and validate any path derived from external input against an
  allow-listed root before use. Reject absolute paths, traversal segments, and
  symlink escapes.
- Compose destination paths with a helper that performs traversal checks rather
  than hand-concatenating a base directory with a caller-supplied component.
- Bound accepted file sizes and types before processing.

## 2. Credential handling

- Read credentials from environment or configuration; never hard-code them in
  source or fixtures.
- Security-relevant options must reach the read/write call intact and must never
  be logged or embedded in a path or query string.

## 3. Consumer compatibility

Give the downstream consumer the access shape it accepts, and avoid an
unnecessary full in-memory copy of the data. Derive the required shape from the
consumer itself; verify it is honored on every branch, including error and
fallback paths.

## 4. Temporary-file lifecycle

- Create temporary files through a secure facility with randomized names; never
  reuse a fixed name in a shared location.
- Clean them up on every exit path, including errors and cancellation.

## 5. Failure observability

- Write failures and partial writes must be observable: propagate the error or
  log it with context. Swallowing a write failure and continuing is silent
  degradation.
- Clean up partial outputs on failure, or mark them clearly incomplete so a
  retry cannot consume them as valid data.
- Verify that closing a stream cannot be skipped by an early return or a
  cancelled task.

## 6. Anti-patterns

- Hand-built destination paths from caller-supplied components.
- Unconditional full-file buffering for every consumer.
- Fixed temporary filenames in shared locations, or temporary files never
  deleted.

## 7. Severity

- Traversal-vulnerable path composition, dropped credentials, or a consumer
  given an incompatible access shape → Critical (functional defect or security).
- An access-shape choice that wastes a full copy → Major.
- Temporary-file cleanup details → Minor.

Give a location, the observed read/write form, and the deviation from the rule
for every finding; see `reporting.md` for the evidence triple.
