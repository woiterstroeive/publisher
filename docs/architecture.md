# Architecture — Publisher Backend

## Status: v1.0 — Feature Freeze

As of v1.0, this project is in feature freeze. No new architecture or
abstractions are to be added. Only bug fixes on the existing modules
are in scope. The system is to be used in practice for publishing
archives. New functionality is only added once real-world usage shows
it is actually needed — not speculatively.

## Workflow

```
main.py
  -> load_config()                      (config.py)
  -> load_metadata()                    (metadata/loader.py)
  -> validate_metadata()                (metadata/validator.py)
  -> MediaItem                          (models/media_item.py)
  -> Publisher.connect()                (publishers/base.py + concrete publisher)
  -> Publisher.publish(media_item)      -> PublicationResult
  -> Publisher.verify(result)
  -> Publisher.disconnect()
  -> move production folder to published/ or failed/
```

## Core principles

- The backend core (`main.py`, `config.py`, `metadata/`, `models/`)
  never imports or references a specific publisher by name, except
  for the single small `_PUBLISHER_FACTORIES` lookup in `main.py`
  used for `--publisher` selection.
- Every publisher implements `publishers.base.Publisher` and returns
  `models.publication_result.PublicationResult` — never a raw URL,
  boolean, or publisher-specific object.
- Publisher-specific configuration (credentials, IDs, platform
  options) is owned entirely by that publisher's own module
  (e.g. `PeerTubeConfig` in `publishers/peertube.py`), not by
  `config.py`.
- `MediaItem` contains only fields that are broadly applicable across
  publishers. Publisher-specific data lives in the publisher module,
  never as a generic/catch-all field on the shared models.

## Adding a new publisher (once real need arises)

1. Create `publishers/<name>.py` implementing `Publisher`.
2. Add a `<Name>Config.from_env()` classmethod owning that
   publisher's own settings.
3. Register a factory function in `main.py`'s
   `_register_default_publishers()`.

No other file should require changes.

## Explicitly out of scope for v1.0 (do not add without real need)

- Database
- Queue manager / folder watcher
- GUI / web interface / REST API
- Scheduling
- Retry system
- FFmpeg / Whisper / thumbnail generation / AI metadata
- Additional publishers (Internet Archive, YouTube, Rumble, etc.)
