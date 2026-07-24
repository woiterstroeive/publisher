"""
models/media_item.py

Defines the core internal data model representing a single media
production to be published.

Responsibility:
    Hold structured data describing a media item, and nothing else.

MediaItem is intentionally a pure data object. It contains no upload
logic, no publisher-specific fields, and no I/O. Publisher modules
are responsible for translating a MediaItem into whatever request
shape their target platform requires; MediaItem must never be aware
that publishers exist.

MediaItem contains only fields that are broadly applicable across
publishers. Publisher-specific data must NOT be added here — it
belongs inside the owning publisher module, which is responsible for
deriving whatever it needs (directly from MediaItem's fields, or from
its own separate inputs) at publish time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class MediaFile:
    """
    A single physical file associated with a MediaItem.

    Kept as its own type (rather than a bare Path) because a media
    item's "files" are not all interchangeable — a publisher needs to
    know whether a given file is the primary video, an alternate
    rendition, etc. Additional roles can be added over time without
    changing MediaItem itself.

    Attributes:
        path: Filesystem location of the file.
        role: Purpose of this file, e.g. "primary", "proxy", "poster".
    """

    path: Path
    role: str = "primary"


@dataclass(frozen=True, slots=True)
class Subtitle:
    """
    A single subtitle/caption track associated with a MediaItem.

    Attributes:
        path: Filesystem location of the subtitle file.
        language: BCP-47 / ISO language code, e.g. "en", "nl", "de".
    """

    path: Path
    language: str


@dataclass(frozen=True, slots=True)
class MediaItem:
    """
    Platform-independent representation of a single media production
    ready to be published.

    This object is pure data: it describes *what* is being published,
    never *how* or *where*. It must remain fully independent of any
    publisher's API, authentication, or transport concerns.

    Attributes:
        identifier: Stable, unique identifier for this media item
            (e.g. a slug or UUID), independent of any publisher-issued
            ID. Used for internal tracking, logging, and idempotency.
        title: Human-readable title of the production.
        description: Full description / show notes text.
        creator: Name of the creator or production credit.
        files: One or more MediaFile entries (e.g. the primary video
            file, and optionally alternate renditions).
        tags: Free-form tags/keywords for the item.
        thumbnail: Optional path to a thumbnail/cover image.
        subtitles: Subtitle/caption tracks associated with the item.
        publication_date: Intended or actual publication date/time.
            Optional, since not all publishers require scheduling.
    """

    identifier: str
    title: str
    description: str
    creator: str
    files: tuple[MediaFile, ...]
    tags: tuple[str, ...] = field(default_factory=tuple)
    thumbnail: Path | None = None
    subtitles: tuple[Subtitle, ...] = field(default_factory=tuple)
    publication_date: datetime | None = None

    def primary_file(self) -> MediaFile:
        """
        Return the MediaFile marked as the primary asset.

        Returns:
            The MediaFile with role "primary".

        Raises:
            ValueError: If no file with role "primary" exists.
        """
        for media_file in self.files:
            if media_file.role == "primary":
                return media_file
        raise ValueError(
            f"MediaItem '{self.identifier}' has no file with role 'primary'."
        )
