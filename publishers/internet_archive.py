"""
publishers/internet_archive.py

Internet Archive publisher implementation.

Responsibility:
    Implement the Publisher interface for archive.org: IAS3 (S3-like)
    authenticated upload, and translation of Internet Archive's API
    responses into PublicationResult.

All Internet Archive-specific concepts (IAS3 access/secret keys, item
metadata headers, collection/mediatype conventions, archive.org's
JSON response shapes) are confined to this file. Nothing outside
publishers/internet_archive.py may depend on them.

Internet Archive is intended as the preferred archival backend for
large historical collections, complementing PeerTube rather than
replacing it. PeerTube remains unchanged.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import requests

from config import ConfigurationError
from models.media_item import MediaItem
from models.publication_result import PublicationResult
from publishers.base import (
    Publisher,
    PublisherConnectionError,
    PublisherPublishError,
)

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECONDS = 60
_UPLOAD_TIMEOUT_SECONDS = 7200

_S3_ENDPOINT = "https://s3.us.archive.org"
_METADATA_ENDPOINT = "https://archive.org/metadata"
_DETAILS_ENDPOINT = "https://archive.org/details"


@dataclass(frozen=True, slots=True)
class InternetArchiveConfig:
    """
    Internet Archive-specific configuration, owned entirely by this
    module.

    Attributes:
        access_key: IAS3 access key (from archive.org/account/s3.php).
        secret_key: IAS3 secret key.
        collection: Target collection identifier items are added to,
            e.g. "opensource_movies". Required by archive.org to
            auto-create a new item.
        mediatype: Internet Archive mediatype for the item, e.g.
            "movies", "audio", "texts". Defaults to "movies".
    """

    access_key: str
    secret_key: str
    collection: str
    mediatype: str = "movies"

    @classmethod
    def from_env(cls) -> "InternetArchiveConfig":
        """
        Build and validate InternetArchiveConfig from the process
        environment.

        Expects IA_ACCESS_KEY, IA_SECRET_KEY, and IA_COLLECTION to be
        set (e.g. via a loaded .env file). IA_MEDIATYPE is optional.

        Returns:
            A validated InternetArchiveConfig.

        Raises:
            ConfigurationError: If a required variable is missing.
        """
        try:
            access_key = os.environ["IA_ACCESS_KEY"]
            secret_key = os.environ["IA_SECRET_KEY"]
            collection = os.environ["IA_COLLECTION"]
        except KeyError as missing:
            raise ConfigurationError(
                f"Missing required Internet Archive environment variable: {missing}"
            ) from missing

        mediatype = os.environ.get("IA_MEDIATYPE", "movies")

        return cls(
            access_key=access_key,
            secret_key=secret_key,
            collection=collection,
            mediatype=mediatype,
        )


class InternetArchivePublisher(Publisher):
    """
    Publisher implementation for archive.org.

    Authenticates using IAS3 access/secret keys, uploads the primary
    file via archive.org's S3-compatible endpoint (which also creates
    the item and attaches metadata in the same request), and reports
    outcomes as PublicationResult.
    """

    publisher_name = "internet_archive"

    def __init__(self, config: InternetArchiveConfig) -> None:
        """
        Args:
            config: Validated Internet Archive configuration.
        """
        self._config = config
        self._session: requests.Session | None = None

    def connect(self) -> None:
        """
        Prepare an authenticated session and confirm the IAS3
        credentials are accepted.

        Unlike PeerTube, Internet Archive's IAS3 API is key-based and
        does not involve an OAuth handshake or issued token — the
        access/secret key pair is sent directly on every request.
        connect() still performs one lightweight request against the
        S3 endpoint so that invalid credentials are caught here,
        rather than surfacing later as a confusing upload failure.

        Raises:
            PublisherConnectionError: If the credentials are rejected
                or the endpoint cannot be reached.
        """
        session = requests.Session()
        session.headers.update(
            {
                "Authorization": (
                    f"LOW {self._config.access_key}:{self._config.secret_key}"
                )
            }
        )

        try:
            response = session.get(
                f"{_S3_ENDPOINT}/",
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            raise PublisherConnectionError(
                f"Could not reach Internet Archive S3 endpoint: {error}"
            ) from error

        if response.status_code in (401, 403):
            raise PublisherConnectionError(
                "Internet Archive rejected the provided IAS3 access/secret key."
            )

        self._session = session
        logger.info(
            "Connected to Internet Archive as access key '%s'",
            self._config.access_key,
        )

    def publish(self, media_item: MediaItem) -> PublicationResult:
        """
        Upload a MediaItem's primary file to archive.org.

        The item is created (if it does not already exist) and the
        file is uploaded in the same request, with metadata attached
        via x-archive-meta-* headers. media_item.identifier is used
        directly as the archive.org item identifier, so it must
        already be a valid archive.org identifier (lowercase
        alphanumerics, underscores, hyphens, periods).

        Args:
            media_item: The media item to publish.

        Returns:
            A PublicationResult describing the outcome. Ordinary
            upload failures (rejected requests, archive.org-side
            errors) are reported as success=False, not raised.

        Raises:
            PublisherConnectionError: If connect() was not called
                first.
        """
        if self._session is None:
            raise PublisherConnectionError(
                "InternetArchivePublisher.publish() called before connect()."
            )

        try:
            primary_file = media_item.primary_file()
        except ValueError as error:
            return self.build_result(
                success=False,
                remote_id=None,
                url=None,
                message=str(error),
            )

        upload_url = (
            f"{_S3_ENDPOINT}/{media_item.identifier}/{primary_file.path.name}"
        )

        headers = {
            "x-amz-auto-make-bucket": "1",
            "x-archive-meta01-title": media_item.title,
            "x-archive-meta01-description": media_item.description,
            "x-archive-meta01-creator": media_item.creator,
            "x-archive-meta01-collection": self._config.collection,
            "x-archive-meta01-mediatype": self._config.mediatype,
        }
        if media_item.tags:
            headers["x-archive-meta01-subject"] = ";".join(media_item.tags)

        try:
            with primary_file.path.open("rb") as video_file:
                response = self._session.put(
                    upload_url,
                    data=video_file,
                    headers=headers,
                    timeout=_UPLOAD_TIMEOUT_SECONDS,
                )
        except OSError as error:
            return self.build_result(
                success=False,
                remote_id=None,
                url=None,
                message=f"Could not read video file '{primary_file.path}': {error}",
            )
        except requests.RequestException as error:
            raise PublisherPublishError(
                f"Internet Archive upload request failed for "
                f"'{media_item.identifier}': {error}"
            ) from error

        if not response.ok:
            return self.build_result(
                success=False,
                remote_id=None,
                url=None,
                message=(
                    f"Internet Archive rejected upload for "
                    f"'{media_item.identifier}': HTTP {response.status_code} - "
                    f"{response.text[:500]}"
                ),
            )

        public_url = f"{_DETAILS_ENDPOINT}/{media_item.identifier}"
        logger.info(
            "Published '%s' to Internet Archive as %s",
            media_item.identifier,
            media_item.identifier,
        )

        return self.build_result(
            success=True,
            remote_id=media_item.identifier,
            url=public_url,
            message="Upload accepted by Internet Archive.",
        )

    def verify(self, result: PublicationResult) -> bool:
        """
        Confirm the item is retrievable from archive.org's metadata
        API and actually contains uploaded files.

        Internet Archive processes newly uploaded items asynchronously
        (derives, indexing) after accepting the upload, so a
        successful publish() does not guarantee the item is
        immediately visible via the metadata API. A False result here
        may simply mean processing has not completed yet.

        Args:
            result: A PublicationResult previously returned by
                publish().

        Returns:
            True if the item is confirmed present with at least one
            file, False otherwise (including if remote_id is missing).
        """
        if self._session is None:
            raise PublisherConnectionError(
                "InternetArchivePublisher.verify() called before connect()."
            )

        if not result.remote_id:
            return False

        try:
            response = self._session.get(
                f"{_METADATA_ENDPOINT}/{result.remote_id}",
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            logger.warning(
                "Internet Archive verification request failed for %s: %s",
                result.remote_id,
                error,
            )
            return False

        if not response.ok:
            return False

        try:
            data = response.json()
        except ValueError:
            return False

        return bool(data.get("files"))

    def get_public_url(self, result: PublicationResult) -> str | None:
        """
        Return the public URL recorded in a PublicationResult.

        Internet Archive's item URL is deterministic from the
        identifier, so this simply returns the URL already computed
        in publish().

        Args:
            result: A PublicationResult previously returned by
                publish().

        Returns:
            The public URL, or None if not available.
        """
        return result.url

    def disconnect(self) -> None:
        """Close the underlying HTTP session, if one is open."""
        if self._session is not None:
            self._session.close()
            self._session = None
            logger.debug("Disconnected from Internet Archive.")
