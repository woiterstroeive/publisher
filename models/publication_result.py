"""
models/publication_result.py

Defines the common result object returned by every publisher after a
publish attempt.

Responsibility:
    Hold structured data describing the outcome of a single publish
    operation, in a shape that is identical across all publishers.

This object is what decouples the rest of the backend from individual
publisher APIs. A publisher may internally deal with wildly different
response formats (PeerTube's JSON, YouTube's OAuth-flavored API,
Rumble's own conventions), but every publisher must translate its own
result into this same PublicationResult shape before returning.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class PublicationResult:
    """
    Outcome of a single publish attempt, in publisher-independent form.

    Attributes:
        success: Whether the publish attempt succeeded.
        publisher: Identifier of the publisher that produced this
            result, e.g. "peertube", "youtube". Used for logging and
            routing (e.g. which folder to move a file into), never
            for branching business logic elsewhere in the backend.
        remote_id: The publisher-issued identifier for the published
            item (e.g. a PeerTube video UUID, a YouTube video ID).
            None if publishing did not succeed far enough to obtain one.
        url: Public URL of the published item, if available. None if
            publishing failed, or if the publisher does not expose a
            public URL (e.g. a private/unlisted upload).
        message: Human-readable detail about the outcome — a success
            confirmation or an error description. Always populated,
            so failures are never silent.
        timestamp: When this result was produced.
    """

    success: bool
    publisher: str
    remote_id: str | None
    url: str | None
    message: str
    timestamp: datetime
