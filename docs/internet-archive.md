# Internet Archive publishing protocol

This document describes the implemented single-file IAS3 behavior. The evidence behind the
design is recorded in [`reliability-research.md`](reliability-research.md).

## Configuration

Required environment values:

- `IA_ACCESS_KEY`
- `IA_SECRET_KEY`
- `IA_COLLECTION`

Optional:

- `IA_MEDIATYPE`, default `movies`

Values are trimmed. Missing, blank, or whitespace-only required values fail before remote work.
Secrets are used only in the authenticated session and must never enter logs or durable state.

## Supported production shape

Internet Archive currently accepts exactly one declared media file with role `primary`.
Thumbnails, subtitles, and additional files are rejected before PUT rather than silently
ignored. The identifier must match `[a-z0-9][a-z0-9._-]{4,99}`.

## Local byte identity

Before remote work, orchestration records a platform-neutral descriptor containing:

- basename;
- byte size;
- SHA-256.

That descriptor is written to the durable sidecar and bound to the publisher through
`Publisher.prepare()`. Existing-item reconciliation re-hashes the local file. New uploads copy
and hash the source into an anonymous temporary file, compare the snapshot to the prepared
descriptor, rewind it, and stream the snapshot. Source mutation therefore cannot change the
bytes once the checked upload snapshot exists.

The temporary snapshot requires free local temporary-disk capacity approximately equal to the
primary file size. Snapshot creation failure happens before PUT and is definitive for that
attempt; no remote write should be inferred.

## Local concurrency

The validated IA identifier selects a runtime-user-local `FileLock`. It is acquired before
metadata preflight and released by `disconnect()` after bounded verification. A contending local
writer fails as retryable before GET/PUT. This is not a distributed lock and cannot coordinate
another OS account or machine.

## Connect

`connect()` creates an authenticated `requests.Session` and performs a lightweight IAS3 request.

- `401`/`403`: definitive credential rejection;
- `408`, `429`, and all `5xx`: temporary pre-write failure;
- other unsuccessful responses: definitive connection failure;
- request exception: temporary pre-write failure.

A failed connection closes its temporary session. Cleanup errors are logged with traceback but
never replace the primary connection classification.

## Metadata preflight

Before every new PUT, Publisher GETs `https://archive.org/metadata/<identifier>`.

- Only explicit HTTP `404` permits creation by PUT.
- Temporary status or request failure is retryable and starts no PUT.
- Other unsuccessful status is definitive.
- Invalid JSON, a missing metadata identifier, or a different identifier fails closed.
- Exact original filename plus equal known size is an idempotent existing-item result.
- Exact original filename with size not visible yet proceeds to bounded verification without
  another PUT.
- Existing identifier with conflicting original filename/size is rejected; no overwrite is
  attempted.

The current exact remote reconciliation uses identifier, filename, source=`original`, and size.
Remote checksum comparison is a recommended later enhancement; it is not currently claimed.

## PUT

The filename is percent-encoded as one path segment. Metadata values use IA's `uri(...)`
percent-encoding convention when needed for Unicode or whitespace.

PUT includes `x-archive-keep-old-version: 1`. This preserves prior content in IA history if an
external writer still overwrites the key; it is damage limitation, not concurrency control.

Classification:

- request exception after PUT begins: `PublisherOutcomeUnknownError`;
- HTTP `408`, `429`, or `5xx` after PUT: unknown outcome;
- other unsuccessful response: definitive rejected result;
- successful response: accepted, not yet verified.

No post-PUT unknown outcome is automatically re-uploaded.

## Exact verification

Verification performs bounded Metadata API polling. Success requires all of:

- metadata identifier equals the expected remote identifier;
- exact expected original filename;
- exact expected byte size;
- `source` equals `original`.

Derived files or a generic successful response do not prove publication. If the bounded window
expires, the state becomes `accepted_unverified` and remains in workspace for later read-only
reconciliation.

## Cleanup and outcome precedence

`disconnect()` closes the session and releases the identifier lock. Orchestration treats it as
best-effort cleanup: an ordinary disconnect exception is logged separately and may not replace
a verified result, definitive publisher failure, or unknown remote-write outcome. Process-control
exceptions outside ordinary `Exception` are not swallowed.

## Documented IA limitation

IAS3 documents overwrite-capable PUT but no verified create-only conditional operation equivalent
to Amazon S3 `If-None-Match: *`. Therefore this implementation makes no distributed atomicity
claim. Operate a single authoritative uploader per identifier namespace until IA documents or a
controlled non-destructive test proves stronger server behavior.
