# Operations guide

This guide covers one production at a time. Do not start batch execution until the
single-item completion gate is green and one explicitly approved canary has been reconciled.

## Preflight

1. Work from the project root with the project `.venv`.
2. Confirm `.env` exists locally but is ignored by Git. Never print or copy its values into
   logs, sidecars, commands, or issue reports.
3. Put one production directory under `workspace/` with `metadata.toml` and exactly one
   non-empty primary media file for Internet Archive.
4. Run the local gates:

   ```bash
   env -u PYTHONPATH .venv/Scripts/python -m pytest -q
   env -u PYTHONPATH .venv/Scripts/python -m ruff check .
   env -u PYTHONPATH .venv/Scripts/python -m ruff format --check \
     main.py metadata/validator.py publishers/base.py \
     publishers/internet_archive.py tests
   env -u PYTHONPATH .venv/Scripts/python -m compileall -q \
     main.py metadata publishers tests
   git diff --check
   ```

5. Choose the publisher explicitly:

   ```bash
   env -u PYTHONPATH .venv/Scripts/python main.py \
     workspace/<production> --publisher internet_archive
   ```

## Exit codes

- `0`: exact remote verification succeeded and the production moved to `published/`.
- `1`: definitive input, configuration, publisher, durable-state, or local finalization
  failure. Read the log and sidecar before changing anything.
- `2`: intentionally non-final. The item is retryable, uncertain, accepted but not yet
  verified, or another process owns the production.

The process exit code never replaces `publication-result.json` as the durable item record.

## State recovery

### `publishing`

The process may have stopped after durable write-intent and before a terminal state was
recorded. Do not remove the sidecar. Verify that publisher, local identifier, filename, size,
and SHA-256 still match, then rerun the same command. IA preflight must reconcile remote state
before any new PUT.

### `uncertain`

A PUT may have reached Internet Archive, but the response was lost or temporary. Never perform
a blind retry. Leave the production in `workspace/` and rerun unchanged so the read-only
metadata preflight can reconcile it.

### `accepted_unverified`

IA accepted the upload or a matching remote filename was visible, but exact identifier/name/size
verification did not complete within the bounded polling window. Wait for IA ingestion, then
rerun the unchanged production. Do not move it manually to `published/`.

### `retryable`

The failure occurred before a remote write was attempted. Correct temporary connectivity or
service availability and rerun. If the local identity has changed intentionally, treat it as a
new reviewed production rather than editing potentially remote states in place.

### `verified_move_failed`

The remote object is verified, but local same-volume rename failed. Do not upload again. Resolve
the destination conflict, permissions, or volume layout, then perform a reviewed local
finalization. Never overwrite an existing destination.

### `failed` / `failed_move_failed`

The publisher produced a definitive failure. Inspect the concrete message. A move failure means
the source remains in place; it does not turn the remote outcome into success.

### Malformed, missing-state, or unknown-state sidecar

The workflow fails closed and refuses to replace it. Preserve the file as evidence. Repair only
after manual review; do not guess a state or delete the record to force a retry.

## Locks

- The production-path lock protects one local production folder, its sidecar, and final move.
- The IA identifier lock protects cooperating processes under the same OS account, even when
  two local folders target the same remote identifier.
- The IA lock remains held through preflight, PUT, exact verification, and disconnect.
- Lockfiles may remain on disk after exit; ownership is determined by the OS lock, not file
  existence alone.
- Another OS account, host, web uploader, or unrelated tool is outside the identifier lock.
  Operate one authoritative uploader for the assigned identifier namespace.

## Canary gate

A real canary requires explicit approval. Before it:

- all local tests and checks are green;
- the final diff has passed independent review;
- a unique identifier is selected (never reuse `test_video_001`);
- the test file is small and non-sensitive;
- the destination collection and public visibility are confirmed;
- no other uploader can target the same identifier.

After the PUT, verify read-only through IA metadata:

- metadata identifier equals the requested identifier;
- the exact original filename exists;
- byte size matches;
- no unsupported declared asset was omitted;
- `publication-result.json` and final folder location match the observed outcome.

## Batch operating policy

After a successful canary, start sequentially. Preflight the entire selected batch before the
first upload, freeze normalized metadata into a manifest, reject duplicate identifiers, and run
the existing single-item lifecycle for every row. Start with a small subset, then a batch of 20,
then complete 30. Review outcomes before increasing toward 300. Do not add parallel uploads
until correctness and IA behavior have been measured.
