"""
publishers/base.py

Defines the abstract interface every publisher must implement.

Responsibility:
    Establish the common contract (prepare, connect, publish, verify,
    get_public_url, disconnect) that all publisher modules satisfy,
    so the rest of the backend never needs to know which specific
    platform it is talking to.

This module must remain free of any platform-specific logic. It knows
nothing about PeerTube, YouTube, or any other platform — only about
the shape every publisher must have.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self

from models.media_item import MediaItem
from models.publication_result import PublicationResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LocalFileDescriptor:
    """Platform-neutral identity of the primary bytes selected for publication."""

    name: str
    size: int
    sha256: str

    def to_record(self) -> dict[str, str | int]:
        """Return the JSON-compatible durable representation."""
        return {"name": self.name, "size": self.size, "sha256": self.sha256}


class PublisherError(Exception):
    """Base exception for errors raised by publisher implementations."""


class PublisherConnectionError(PublisherError):
    """Raised when a publisher cannot establish or authenticate a connection."""


class PublisherPublishError(PublisherError):
    """Raised when a publisher fails to publish a MediaItem."""


class PublisherTemporaryError(PublisherError):
    """Raised for a safe-to-retry failure before a remote write is attempted."""


class PublisherOutcomeUnknownError(PublisherPublishError):
    """Raised when a remote write may have succeeded but was not confirmed."""

    def __init__(self, message: str, *, remote_id: str) -> None:
        super().__init__(message)
        self.remote_id = remote_id


class Publisher(ABC):
    """
    Abstract base class for all publishing platform integrations.

    Every concrete publisher (PeerTube, YouTube, Rumble, ...) must
    inherit from this class and implement its abstract methods. The
    rest of the backend interacts exclusively through this interface
    and through PublicationResult — never through a specific
    publisher's own types or API responses.

    Concrete publishers are expected to:
        1. Define the `publisher_name` class attribute.
        2. Implement connect(), publish(), verify(), get_public_url(),
           and disconnect(); optionally override prepare() when local
           byte identity must be bound before remote work.
        3. Translate their own platform's API and errors into
           PublicationResult / PublisherError, so nothing
           platform-specific leaks past this boundary.

    Typical usage:

        publisher = PeerTubePublisher(config)
        publisher.connect()
        try:
            result = publisher.publish(media_item)
        finally:
            publisher.disconnect()

    Publisher also supports use as a context manager, which handles
    connect()/disconnect() automatically:

        with PeerTubePublisher(config) as publisher:
            result = publisher.publish(media_item)
    """

    #: Short, stable, lowercase identifier for this publisher, e.g.
    #: "peertube". Used to populate PublicationResult.publisher and
    #: for logging. Must be defined by every concrete subclass.
    publisher_name: str

    def prepare(
        self,
        media_item: MediaItem,
        descriptor: LocalFileDescriptor,
        *,
        reconcile_only: bool = False,
    ) -> None:
        """Bind durable local context before remote lifecycle work begins."""
        return

    @abstractmethod
    def connect(self) -> None:
        """
        Establish and/or authenticate a connection to the platform.

        Must be called before publish() or verify(). Implementations
        should perform whatever handshake/authentication the platform
        requires (e.g. logging in, obtaining an access token).

        Raises:
            PublisherConnectionError: If a connection cannot be
                established or authentication fails.
        """
        raise NotImplementedError

    @abstractmethod
    def publish(self, media_item: MediaItem) -> PublicationResult:
        """
        Publish a MediaItem to the platform.

        Implementations must translate the MediaItem's fields into
        whatever request shape their platform's API requires, and
        must translate the outcome — success or failure — into a
        PublicationResult. This method must not raise for ordinary
        publish failures (e.g. a rejected upload); those are reported
        via PublicationResult.success = False. It may still raise
        PublisherError subclasses for exceptional conditions (e.g.
        the connection was lost mid-upload).

        Args:
            media_item: The platform-independent media item to
                publish.

        Returns:
            A PublicationResult describing the outcome.
        """
        raise NotImplementedError

    @abstractmethod
    def verify(self, result: PublicationResult) -> bool:
        """
        Verify that a previously published item is actually live on
        the platform.

        This is a distinct step from publish() succeeding, since some
        platforms report success on upload but process the item
        asynchronously (e.g. transcoding) before it is genuinely
        available.

        Args:
            result: The PublicationResult returned by a prior
                publish() call.

        Returns:
            True if the item is confirmed present/available on the
            platform, False otherwise.
        """
        raise NotImplementedError

    @abstractmethod
    def get_public_url(self, result: PublicationResult) -> str | None:
        """
        Return the public URL for a previously published item, if any.

        Args:
            result: The PublicationResult returned by a prior
                publish() call.

        Returns:
            The public URL, or None if the platform does not expose
            one (e.g. a private/unlisted upload) or the item is not
            yet available.
        """
        raise NotImplementedError

    @abstractmethod
    def disconnect(self) -> None:
        """
        Release any resources associated with the connection.

        Implementations should ensure this is safe to call even if
        connect() was never called or already failed.
        """
        raise NotImplementedError

    def build_result(
        self,
        *,
        success: bool,
        remote_id: str | None,
        url: str | None,
        message: str,
    ) -> PublicationResult:
        """
        Construct a PublicationResult for this publisher.

        Concrete publishers should use this helper (rather than
        constructing PublicationResult directly) so that
        `publisher_name` and `timestamp` are always populated
        consistently, in exactly one place.

        Args:
            success: Whether the publish attempt succeeded.
            remote_id: Publisher-issued identifier, if available.
            url: Public URL, if available.
            message: Human-readable outcome description.

        Returns:
            A populated PublicationResult.
        """
        return PublicationResult(
            success=success,
            publisher=self.publisher_name,
            remote_id=remote_id,
            url=url,
            message=message,
            timestamp=datetime.now(UTC),
        )

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.disconnect()
