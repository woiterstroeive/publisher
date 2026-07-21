# Publisher Backend — v1.0

Modular publishing backend for distributing media productions to
publishing platforms. v1.0 supports PeerTube. See `docs/architecture.md`
for the full architecture and the v1.0 feature-freeze policy.

## Setup

```
pip install -e .
cp .env.example .env
# fill in .env with your PeerTube instance URL, username, password, channel ID
```

## Publishing a production

1. Create a folder under `workspace/`, e.g. `workspace/my-episode/`.
2. Add your media file(s) inside it (e.g. `workspace/my-episode/video/master.mp4`).
3. Add a `metadata.toml` file inside the same folder. See
   `docs/example-metadata.toml` for the expected shape.
4. Run:

```
python main.py workspace/my-episode --publisher peertube
```

On success, the folder is moved to `published/`. On a publish or
connection failure, it is moved to `failed/`. On a metadata error, the
folder is left in `workspace/` so you can fix it and rerun — check the
console output (and `logs/publisher.log`) for exactly which fields
need attention.

## Status

v1.0 — feature freeze. No new architecture or publishers will be added
until real-world usage shows a concrete need. See `docs/architecture.md`.
