# Publisher Backend — v1.0

A small, modular backend for publishing one validated media production at a time.
The currently registered publishers are `peertube` and `internet_archive`.

The architecture remains intentionally simple: no database, queue, web framework, or
folder watcher. Reliability state is persisted inside each production folder.

## Setup

Create a project-local environment and install the application plus development tools:

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e '.[dev]'
cp .env.example .env
```

Fill in only the credentials for the publisher you intend to use. Never commit `.env`.

Internet Archive requires:

- `IA_ACCESS_KEY`
- `IA_SECRET_KEY`
- `IA_COLLECTION`
- optional `IA_MEDIATYPE` (defaults to `movies`)

## Production folder

Create a folder under `workspace/`, add the media files, and add `metadata.toml`.
See `docs/example-metadata.toml` for the metadata shape.

Validation requires, among other things:

- non-blank identifier, title, description, and creator;
- exactly one file with role `primary`;
- a non-empty primary file;
- every referenced asset must be a real file inside the production folder;
- no path traversal or absolute path outside the production folder.

The current Internet Archive implementation deliberately supports exactly one media file.
It refuses productions containing thumbnails, subtitles, or additional media files rather
than silently ignoring those assets.

Internet Archive identifiers must contain 5–100 lowercase letters, numbers, periods,
underscores, or hyphens, and must start with a letter or number.

## Publish one production

The publisher selection is required; there is no implicit platform default:

```bash
.venv/Scripts/python main.py workspace/my-episode --publisher internet_archive
```

or:

```bash
.venv/Scripts/python main.py workspace/my-episode --publisher peertube
```

## Durable states and exit codes

Before remote work and after every publisher outcome, the backend atomically writes
`publication-result.json` in the production folder. The record contains the state,
publisher, local and remote identifiers, URL when available, timestamps, message, attempt
number, and the exact local primary filename, byte size, and SHA-256 checksum.

| State | Meaning | Folder policy | Exit code |
|---|---|---|---:|
| `publishing` | Durable write-intent exists; a crash may have interrupted remote work | Reconcile before repeating | — (interrupted process) |
| `verified` | Remote primary file was verified | Move to `published/` | `0` |
| `accepted_unverified` | Upload accepted, exact verification not complete | Leave in workspace | `2` |
| `uncertain` | Connection failed during/after a remote write; outcome may exist | Leave in workspace | `2` |
| `retryable` | Temporary failure occurred before a remote write | Leave in workspace | `2` |
| `failed` | Definitive publisher error/result after publisher construction | Move to `failed/` | `1` |
| `verified_move_failed` | Remote verification succeeded but local move failed | Leave in place | `1` |
| `failed_move_failed` | Definitive failure occurred but local move failed | Leave in place | `1` |

Metadata load, validation, and publisher-configuration errors return `1` and leave the folder
in place so the input or environment can be fixed. Configuration failures occur before an
outcome sidecar is written.

A destination folder is never silently overwritten. Local finalization uses a same-volume
rename and does not fall back to copy/delete, avoiding partially copied productions.

An exclusive per-production file lock covers the complete single-item run. A concurrent
process using the same production path exits with code `2` before publisher construction or
network access. Internet Archive also takes a runtime-user-local lock keyed by the validated
remote identifier across preflight, PUT, bounded exact verification, and disconnect cleanup,
so different local folders cannot race to the same IA object under this operating-system
account. Locks cannot serialize an unrelated uploader under another account or on another
machine.

## Internet Archive reliability behavior

Before every PUT, the publisher performs a read-only metadata lookup:

- an exact existing original filename and size is treated as an idempotent resume;
- an exact filename whose size is not visible yet is left for verification without a new PUT;
- a known filename/size conflict is rejected and never overwritten;
- only an explicit metadata `404` permits a new PUT; malformed successful responses fail closed;
- timeout or transient HTTP responses are not treated as definitive failures;
- a broken upload connection or transient upload response becomes `uncertain`.

IA PUT requests include `x-archive-keep-old-version: 1`, matching the official IA CLI's
damage-limiting behavior. This preserves a prior remote file version if an external writer
clobbers the same key, but it is not a replacement for locking or preflight reconciliation.

Verification is distinct from upload acceptance. It polls a bounded number of times and
requires the exact remote identifier, expected original filename, and byte size. Merely
seeing any derived IA file is not sufficient.

Before connect/upload, orchestration binds the filename, byte size, and SHA-256 written to the
sidecar to the publisher. IA validates that identity on existing-item resume. For a new PUT it
copies the source into an anonymous temporary snapshot while hashing, rejects any mismatch, and
streams that immutable snapshot. A same-size source replacement therefore cannot alter bytes
after validation and before/during transport.

For `accepted_unverified`, `uncertain`, or `retryable`, inspect the sidecar and rerun the
same command later. The IA preflight check reconciles remote state before any new PUT.

## Logging

Logs are written to the console and `logs/publisher.log`. File logs rotate at 10 MiB and
retain 10 backups. Credentials are not included in successful connection messages.

## Tests and lint

```bash
env -u PYTHONPATH .venv/Scripts/python -m pytest -q
env -u PYTHONPATH .venv/Scripts/python -m ruff check .
```

`env -u PYTHONPATH` avoids unrelated global Python path injection on the current Windows
host.

See `CHANGELOG.md` for behavioral history and production status,
`docs/architecture.md` for module responsibilities and invariants,
`docs/internet-archive.md` for the implemented IAS3 protocol, and
`docs/operations.md` for state recovery and canary/batch operations.
