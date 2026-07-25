# Changelog

This file records behavioral and operational decisions that should remain understandable after
the implementation details and Git history are no longer fresh.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The package
version is currently `1.0.0`, but the reliability revision below remains **unreleased** until the
canary and staged Internet Archive rollout have succeeded.

## [Unreleased]

### Added

- Atomic `publication-result.json` sidecars with attempt count, timestamps, publisher, local and
  remote identifiers, outcome state, and exact primary filename/size/SHA-256 identity.
- Explicit outcome states for `publishing`, `verified`, `accepted_unverified`, `retryable`,
  `uncertain`, `failed`, `verified_move_failed`, and `failed_move_failed`.
- Per-production process locking and runtime-user-local Internet Archive identifier locking.
- Bounded exact Internet Archive verification of identifier, original filename, and byte size.
- Immutable temporary upload snapshot validated against the durable local SHA-256 descriptor.
- Reliability research, Internet Archive protocol, and operator recovery documentation.
- Regression coverage for concurrency, cleanup precedence, unknown outcomes, state recovery,
  source mutation, malformed state, exact verification, and response-body redaction.

### Fixed

- Disconnect/cleanup exceptions no longer replace the real upload or verification outcome.
- Different local production folders under the same OS account can no longer concurrently write
  the same Internet Archive identifier through this application.
- The Internet Archive identifier lock now remains held through preflight, PUT, verification,
  and disconnect rather than ending immediately after PUT.
- Calling Internet Archive `publish()` before `connect()` no longer acquires and retains the
  identifier lock.
- Protected resume states use read-only reconciliation; a metadata `404` cannot authorize a new
  PUT after a potentially accepted or previously verified write.
- PeerTube rejection messages no longer persist arbitrary upstream response bodies; only safe
  context and HTTP status are retained.
- Changed local bytes, including same-size replacements, are rejected when they no longer match
  the durable descriptor.
- Unsupported Internet Archive assets are rejected instead of silently ignored.
- Local finalization never overwrites an existing destination and never falls back to
  copy/delete across volumes.

### Changed

- Publisher selection is explicit and required through `--publisher`.
- Internet Archive preflight permits a new PUT only after an explicit metadata `404`; malformed
  or inconsistent successful metadata fails closed.
- A transient error after a possible PUT is classified as an unknown remote outcome and is never
  blindly retried.
- Internet Archive PUTs include `x-archive-keep-old-version: 1` as damage limitation. This does
  not provide distributed locking.
- File logging is bounded through rotation.
- Project scope is frozen to the existing publishers. No additional publisher integrations are
  planned during the Internet Archive production rollout.

### Known issues

- Malformed-but-successful Internet Archive metadata shapes still need controlled classification
  instead of allowing incidental `AttributeError`/`TypeError` failures.
- Exceptions from `Publisher.prepare()` still need a controlled pre-write outcome.
- A verification exception after an accepted write must remain accepted/unverified rather than
  being classified as safely retryable.
- Identifier-lock release error handling needs one final robustness pass.
- Remote checksum comparison is not yet implemented; exact reconciliation currently uses
  identifier, original filename, and byte size.
- The identifier lock coordinates only cooperating processes under the same OS account. It does
  not serialize another machine, account, website upload, or unrelated client.
- No normalized Excel/CSV/JSONL source manifest for the planned 30- or 300-item batches is present
  in the repository yet.

### Production status

- Local reliability gates previously reached 50 passing tests with Ruff, compileall, formatting
  scope, and `git diff --check` green. The complete gate must be rerun after the latest Critical
  fixes.
- Latest Critical fixes have targeted green tests but still require the full suite and a fresh
  independent final review.
- New controlled Internet Archive uploads: **0/30**.
- Larger rollout: **0/300**.
- Canary: not started.
- Planned rollout after the remaining high-risk review fixes: **1 → 5 → 20 → 30**, then evaluate
  production evidence before building or scaling the batch uploader and metadata generator.

## [1.0.0] - Initial baseline

### Added

- Modular `Publisher` interface and shared `MediaItem` / `PublicationResult` models.
- Single-production metadata loading, validation, publishing, verification, and folder movement.
- Initial PeerTube and Internet Archive implementations.

### Production status

- Historical baseline only. It predates the reliability revision documented under Unreleased and
  should not be treated as the approved production rollout version.
