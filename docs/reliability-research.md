# Reliability research: cleanup outcomes and concurrent IA writes

Research date: 2026-07-24

Scope: the two critical findings from the independent review of the single-item Internet
Archive flow. This research validates narrow reliability decisions; it does not propose an
architecture redesign.

## 1. Cleanup must not replace the primary outcome

### Primary sources

- [Python `try`/`finally` language reference](https://docs.python.org/3.11/reference/compound_stmts.html#the-try-statement)
  states that a saved exception is re-raised after `finally`, but an exception raised by the
  `finally` clause becomes the active exception. Therefore an unguarded disconnect/close can
  mask an upload timeout, a definitive failure, or an unknown remote-write outcome.
- The official `internetarchive` client closes the request body directly in a `finally` block
  ([`item.py`, lines 1597–1598 at commit `84b9436`](https://github.com/jjjake/internetarchive/blob/84b943650de8d2e5ac218f0e36cd9f1a74ef641e/internetarchive/item.py#L1597-L1598)).
  That guarantees cleanup but can theoretically replace the primary exception if `close()`
  itself fails. It is not a suitable outcome-preservation pattern for this backend.
- The same client deliberately catches errors while gathering secondary diagnostics and then
  raises the original connection-reset failure
  ([`item.py`, lines 1559–1577](https://github.com/jjjake/internetarchive/blob/84b943650de8d2e5ac218f0e36cd9f1a74ef641e/internetarchive/item.py#L1559-L1577)).
  This supports treating secondary work as subordinate to the primary operation.

### Decision for Publisher

- `disconnect()` remains in a nested `try` inside the outer `finally`.
- It deliberately catches ordinary `Exception`, but not `BaseException`, because even an
  unexpected cleanup bug must not reclassify the remote upload outcome.
- The cleanup error is logged separately; the primary publish/verification exception or result
  remains authoritative.
- Regression coverage proves that a disconnect failure cannot replace
  `PublisherOutcomeUnknownError`.

## 2. Concurrent GET-then-PUT and overwrite behavior

### Internet Archive behavior

- [Official IAS3 documentation](https://archive.org/developers/ias3.html) states that PUT can
  overwrite an existing file. `x-archive-keep-old-version` defaults to `0`; setting it to `1`
  moves the old file to the item's history directory instead of deleting it.
- The official `internetarchive` client similarly describes upload as clobbering an existing
  key and defaults `keep_old_version=True`
  ([`item.py`, lines 1363–1388](https://github.com/jjjake/internetarchive/blob/84b943650de8d2e5ac218f0e36cd9f1a74ef641e/internetarchive/item.py#L1363-L1388)).
- Its S3 request builder emits `x-archive-keep-old-version: 1`
  ([`iarequest.py`, lines 260–268](https://github.com/jjjake/internetarchive/blob/84b943650de8d2e5ac218f0e36cd9f1a74ef641e/internetarchive/iarequest.py#L260-L268)).
- Neither the IAS3 documentation nor the official client documents a create-only conditional
  PUT using `If-None-Match` or an equivalent IA header.
- Standard Amazon S3 does document [`If-None-Match: *` conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html),
  returning `412 Precondition Failed` for an existing key. IAS3 differs from Amazon S3 in
  documented behavior and real-world feature support; Publisher must not assume that Amazon's
  conditional-write guarantee exists on IA without a controlled IA test.

### Comparable open-source uploaders

#### Official `internetarchive` Python client

- Can compare a local MD5 with IA file metadata before deciding whether a file is already
  uploaded
  ([`item.py`, lines 1402–1410](https://github.com/jjjake/internetarchive/blob/84b943650de8d2e5ac218f0e36cd9f1a74ef641e/internetarchive/item.py#L1402-L1410)).
- Can send `Content-MD5` and describes it as server-side verification
  ([`item.py`, lines 1384–1388](https://github.com/jjjake/internetarchive/blob/84b943650de8d2e5ac218f0e36cd9f1a74ef641e/internetarchive/item.py#L1384-L1388)).
- Automatically retries HTTP 503 by rewinding the body
  ([`item.py`, lines 1482–1501](https://github.com/jjjake/internetarchive/blob/84b943650de8d2e5ac218f0e36cd9f1a74ef641e/internetarchive/item.py#L1482-L1501)).
  Publisher intentionally does not copy that policy: after a PUT request, a transient response
  is treated as an unknown outcome and reconciled with remote state before any later write.
- True single-file resumable/multipart upload remains an open request:
  [issue #386](https://github.com/jjjake/internetarchive/issues/386). This confirms that
  retrying a single streaming PUT is not equivalent to byte-range resume.
- MD5 may be calculated twice in some client modes:
  [issue #253](https://github.com/jjjake/internetarchive/issues/253). Any checksum enhancement
  here should avoid accidental duplicate full-file reads where practical.

#### Tubeup

- Tubeup performs an `item.exists` check and skips the operation if the item already exists
  ([`TubeUp.py`, lines 140–160 at commit `544a88f`](https://github.com/bibanon/tubeup/blob/544a88fdc11ea341ee552e8423ee729b91327658/tubeup/TubeUp.py#L140-L160)).
- It delegates upload to the IA client with a very high retry count
  ([`TubeUp.py`, lines 392–399](https://github.com/bibanon/tubeup/blob/544a88fdc11ea341ee552e8423ee729b91327658/tubeup/TubeUp.py#L392-L399)).
- It does not provide an atomic server-side create, per-identifier process lock, exact
  filename/size reconciliation, or durable per-production outcome state. Its pattern is useful
  as evidence that duplicate-item checks matter, but is not strong enough for this backend's
  deterministic guarantees.

#### PyConZA Internet Archive video uploader

- The uploader uses a manually maintained `done` flag and then calls `item.upload(...)`
  ([`upload_videos.py` at commit `f58a3e8`](https://github.com/PyConZA/internet_archive_video_uploader/blob/f58a3e831c57d8e8b943220779a4f3bb3271ca9f/upload_videos.py)).
- It has no exact remote verification, lock, or crash-safe result record. It is a useful small
  comparison, but its manual bookkeeping is explicitly not adopted.

### Decision for Publisher

1. The production-path lock protects sidecar writes, local moves, and two invocations targeting
   the same folder.
2. The IA publisher also takes a runtime-user-local lock keyed by the validated remote
   identifier around the complete metadata-preflight-to-PUT section. This prevents two
   different local folders or direct local callers under the same OS account from racing to
   the same IA object.
3. A durable `publishing` write-intent is persisted before remote work so a crash is reconciled
   rather than treated as a clean retry.
4. IA PUTs include `x-archive-keep-old-version: 1`. This limits damage from an external writer;
   it is not concurrency control.
5. Transient errors after a possible PUT remain `uncertain`; no blind automatic re-upload is
   performed.

## Residual limitation and next evidence-based enhancement

A process under another OS account or on another machine that does not share the identifier
lock can still race Publisher.
No documented IAS3 conditional-create primitive was found. Operationally, only one authorized
uploader should own a given identifier namespace until IA documents or a controlled canary
proves a server-side precondition.

Remote MD5 comparison plus `Content-MD5` is the strongest next narrow enhancement supported by
the official client. It would detect same-name/same-size content conflicts and add server-side
transport validation, but it should be designed to avoid unnecessary duplicate reads of large
video files. It is recommended rather than silently added as part of the lock fix.
