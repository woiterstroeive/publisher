# Architecture — Publisher Backend

## Status: v1.0 — feature freeze

The project remains in feature freeze. Reliability fixes to existing modules and new
publisher implementations against the existing `Publisher` interface are in scope. New
frameworks or speculative abstractions are not.

Supported publishers: `peertube`, `internet_archive`.

## Single-production workflow

```text
main.py
  -> acquire exclusive per-production lock
  -> load_config()                         config.py
  -> load_metadata()                       metadata/loader.py
  -> validate_metadata()                   metadata/validator.py
  -> MediaItem                             models/media_item.py
  -> construct selected Publisher
  -> bind durable local file descriptor with Publisher.prepare()
  -> atomically write `publishing` with local identity
  -> Publisher.connect()
  -> Publisher.publish(media_item)         PublicationResult or classified exception
  -> Publisher.verify(result)              exact platform verification
  -> Publisher.disconnect()                best-effort; never masks primary outcome
  -> atomically write outcome to publication-result.json
  -> apply state-specific directory policy
```

Upload acceptance and publication verification are separate states. A successful
`publish()` result is not sufficient to move a production to `published/`.

## Module responsibilities

- `main.py` owns single-production orchestration, durable state writes, exit codes, and
  safe directory finalization.
- `config.py` owns application directory and logging configuration.
- `metadata/loader.py` only parses metadata input.
- `metadata/validator.py` validates input and constructs a platform-neutral `MediaItem`.
- `models/` contains shared platform-neutral contracts.
- `publishers/base.py` defines the stable `Publisher` contract and shared error classes.
- Each concrete publisher owns its credentials, API protocol, idempotency checks, remote
  verification, and platform-specific error classification.

Publishers communicate with orchestration only through shared models and shared publisher
exceptions. `MediaItem` does not contain Internet Archive or PeerTube-specific state.

## Reliability invariants

### Explicit platform choice

`--publisher` is required. The CLI never silently chooses a publishing destination.

### Validation before side effects

Metadata and all local asset paths are validated before a publisher is constructed or a
remote write is attempted. Assets must resolve inside the production folder and must be
regular files. Exactly one primary media file is required.

### Classified outcomes

The workflow distinguishes:

- verified success;
- accepted but not yet verified;
- unknown remote write outcome;
- retryable pre-write failure;
- definitive failure;
- local finalization failure.

Exit code `2` means the folder intentionally remains in workspace and can be reconciled or
retried. Exit code `1` means a definitive or local operational failure. Exit code `0` is
reserved for exact remote verification plus successful local finalization.

### Durable state

`publication-result.json` is written using a temporary file, flush, file `fsync`, and
`os.replace`. This prevents readers from seeing a partial JSON record. The implementation does
not claim POSIX power-loss durability for the directory entry because it does not fsync the
parent directory. Before remote work it records a `publishing` write-intent. Every resumable or
terminal record includes publisher, local identifier, filename, byte size, SHA-256, and
attempt number alongside available remote identity. On restart, malformed state fails closed,
missing or unknown state names fail closed, and potentially remote states may resume only when
the recorded local identity still matches.
Runtime sidecars are deliberately excluded from Git.

### No blind overwrite

Publisher implementations must inspect existing remote state before a repeated write when
the platform supports it. The Internet Archive implementation checks identifier, exact
original filename, and byte size before PUT.

The complete local run is protected by an exclusive per-production file lock, protecting its
sidecar and directory policy. Internet Archive additionally locks the validated remote
identifier in a runtime-user-local lock directory across preflight, PUT, bounded exact
verification, and disconnect cleanup. This closes the local GET-then-PUT/verification race even
when different production folders under the same OS account target the same IA identifier.
Internet Archive does not document a conditional create-only PUT, so uploaders under another
account or on another machine remain outside this guarantee.
IA PUTs therefore also request
`x-archive-keep-old-version: 1` as damage-limiting backup behavior, not as concurrency control.

Local destination folders are never overwritten. Finalization uses a directory rename and
has no copy/delete fallback, so a cross-volume setup fails explicitly rather than risking a
partially copied production.

### Exact verification

Internet Archive verification requires the remote identifier, expected original filename,
and byte size. It uses bounded polling because Archive.org exposes new uploads
asynchronously. Derived files
or unrelated files do not prove that the intended primary upload is complete.

Before connect/upload, `Publisher.prepare()` binds the platform-neutral filename, size, and
SHA-256 descriptor used by the durable sidecar. Internet Archive validates it before an
existing-item resume. For a new PUT it copies and hashes the source into an anonymous temporary
snapshot, then uploads only that verified immutable snapshot. This closes same-size mutation
windows between validation and transport.

### Safe transient behavior

Temporary pre-write failures use `PublisherTemporaryError`; remote writes with an uncertain
outcome use `PublisherOutcomeUnknownError`. The orchestration layer leaves both in workspace
and persists distinct states. A later run performs the publisher's remote preflight before
any new write.

The system has no general-purpose retry queue. The only automatic polling is the bounded,
read-only verification loop after an accepted or previously discovered IA item.

## Adding a publisher when real need arises

1. Create `publishers/<name>.py` implementing `Publisher`.
2. Add a publisher-owned configuration class with `from_env()`.
3. Translate platform errors into `PublicationResult` or shared publisher exceptions.
4. Implement platform-appropriate verification and idempotency behavior.
5. Register one factory in `_register_default_publishers()`.
6. Add regression tests before enabling it operationally.

The shared models and public `Publisher` interface should not require modification.

The external evidence and rejected alternatives behind cleanup outcome preservation and
Internet Archive concurrency handling are recorded in
[`reliability-research.md`](reliability-research.md).

## Explicitly out of scope

- Database
- Queue manager or folder watcher
- GUI, web interface, or REST API
- Scheduling
- Parallel batch execution
- General automatic re-upload retries
- FFmpeg, Whisper, thumbnail generation, or AI metadata
- Additional publishers without a demonstrated operational need
