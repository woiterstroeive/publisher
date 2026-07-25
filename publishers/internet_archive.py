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

import hashlib
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import requests
from filelock import FileLock, Timeout

from config import ConfigurationError
from models.media_item import MediaItem
from models.publication_result import PublicationResult
from publishers.base import (
    LocalFileDescriptor,
    Publisher,
    PublisherConnectionError,
    PublisherOutcomeUnknownError,
    PublisherPublishError,
    PublisherTemporaryError,
)

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECONDS = 60
_UPLOAD_TIMEOUT_SECONDS = 7200
_VERIFY_ATTEMPTS = 7
_VERIFY_DELAY_SECONDS = 10
_TRANSIENT_HTTP_STATUSES = frozenset({408, 429})
_IDENTIFIER_LOCK_DIRECTORY = Path(tempfile.gettempdir()) / "publisher-backend-ia-locks"


def _is_transient_http_status(status_code: int) -> bool:
    return status_code in _TRANSIENT_HTTP_STATUSES or 500 <= status_code <= 599


_S3_ENDPOINT = "https://s3.us.archive.org"
_METADATA_ENDPOINT = "https://archive.org/metadata"
_DETAILS_ENDPOINT = "https://archive.org/details"
_IDENTIFIER_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{4,99}")


def _encode_metadata_header(value: str) -> str:
    """Encode IA metadata using the official client's URI header convention."""
    if not value.isascii() or any(character.isspace() for character in value):
        return f"uri({quote(value)})"
    return value


def _close_failed_connection(session: requests.Session) -> None:
    """Release a failed connection without replacing its primary error."""
    try:
        session.close()
    except Exception:
        logger.exception(
            "Internet Archive session cleanup failed while preserving connect failure."
        )


def _file_matches_descriptor(path: Path, descriptor: LocalFileDescriptor) -> bool:
    """Check the current file bytes against the descriptor bound before remote work."""
    try:
        with path.open("rb") as file_handle:
            digest = hashlib.sha256()
            for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
                digest.update(chunk)
            return (
                path.name == descriptor.name
                and os.fstat(file_handle.fileno()).st_size == descriptor.size
                and digest.hexdigest() == descriptor.sha256
            )
    except OSError:
        return False


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
    def from_env(cls) -> InternetArchiveConfig:
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

        values = {
            "IA_ACCESS_KEY": access_key.strip(),
            "IA_SECRET_KEY": secret_key.strip(),
            "IA_COLLECTION": collection.strip(),
        }
        blank_names = [name for name, value in values.items() if not value]
        if blank_names:
            raise ConfigurationError(
                "Internet Archive environment variables must not be blank: "
                + ", ".join(blank_names)
            )

        mediatype = os.environ.get("IA_MEDIATYPE", "movies").strip()
        if not mediatype:
            raise ConfigurationError("IA_MEDIATYPE must not be blank.")

        return cls(
            access_key=values["IA_ACCESS_KEY"],
            secret_key=values["IA_SECRET_KEY"],
            collection=values["IA_COLLECTION"],
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
        self._identifier_lock: FileLock | None = None
        self._expected_files: dict[str, tuple[str, int]] = {}
        self._prepared_files: dict[str, LocalFileDescriptor] = {}
        self._reconcile_only_identifiers: set[str] = set()

    def prepare(
        self,
        media_item: MediaItem,
        descriptor: LocalFileDescriptor,
        *,
        reconcile_only: bool = False,
    ) -> None:
        """Bind the durable primary-file identity used by the orchestrator."""
        self._prepared_files[media_item.identifier] = descriptor
        if reconcile_only:
            self._reconcile_only_identifiers.add(media_item.identifier)
        else:
            self._reconcile_only_identifiers.discard(media_item.identifier)

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
            {"Authorization": (f"LOW {self._config.access_key}:{self._config.secret_key}")}
        )

        try:
            response = session.get(
                f"{_S3_ENDPOINT}/",
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            _close_failed_connection(session)
            raise PublisherTemporaryError(
                f"Could not reach Internet Archive S3 endpoint: {error}"
            ) from error

        if response.status_code in (401, 403):
            _close_failed_connection(session)
            raise PublisherConnectionError(
                "Internet Archive rejected the provided IAS3 access/secret key."
            )

        if _is_transient_http_status(response.status_code):
            _close_failed_connection(session)
            raise PublisherTemporaryError(
                "Internet Archive connection check is temporarily unavailable "
                f"(HTTP {response.status_code})."
            )

        if not response.ok:
            _close_failed_connection(session)
            raise PublisherConnectionError(
                f"Internet Archive S3 connection check failed with HTTP {response.status_code}."
            )

        self._session = session
        logger.info("Connected to Internet Archive.")

    def publish(self, media_item: MediaItem) -> PublicationResult:
        """Serialize local writers for one deterministic IA identifier."""
        if self._session is None:
            raise PublisherConnectionError(
                "InternetArchivePublisher.publish() called before connect()."
            )

        if _IDENTIFIER_PATTERN.fullmatch(media_item.identifier) is None:
            return self._publish_locked(media_item)

        try:
            _IDENTIFIER_LOCK_DIRECTORY.mkdir(parents=True, exist_ok=True)
            lock = FileLock(_IDENTIFIER_LOCK_DIRECTORY / f"{media_item.identifier}.lock")
            lock.acquire(timeout=0)
        except Timeout as error:
            raise PublisherTemporaryError(
                "Another local process is already publishing Internet Archive "
                f"identifier '{media_item.identifier}'."
            ) from error
        except OSError as error:
            raise PublisherTemporaryError(
                "Could not acquire the local Internet Archive identifier lock for "
                f"'{media_item.identifier}': {error}"
            ) from error

        self._identifier_lock = lock
        return self._publish_locked(media_item)

    def _publish_locked(self, media_item: MediaItem) -> PublicationResult:
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

        if _IDENTIFIER_PATTERN.fullmatch(media_item.identifier) is None:
            return self.build_result(
                success=False,
                remote_id=None,
                url=None,
                message=(
                    "Invalid Internet Archive identifier. Use 5-100 lowercase "
                    "letters, numbers, periods, underscores, or hyphens, and "
                    "start with a letter or number."
                ),
            )

        if len(media_item.files) != 1 or media_item.thumbnail or media_item.subtitles:
            return self.build_result(
                success=False,
                remote_id=None,
                url=None,
                message=(
                    "Internet Archive publishing does not yet support subtitles or "
                    "thumbnails, and requires exactly one media file. Refusing to "
                    "silently ignore production assets."
                ),
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

        file_name = primary_file.path.name
        prepared_file = self._prepared_files.get(media_item.identifier)
        if prepared_file is None:
            with primary_file.path.open("rb") as file_handle:
                digest = hashlib.sha256()
                for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                prepared_file = LocalFileDescriptor(
                    name=file_name,
                    size=os.fstat(file_handle.fileno()).st_size,
                    sha256=digest.hexdigest(),
                )
            self._prepared_files[media_item.identifier] = prepared_file

        file_size = primary_file.path.stat().st_size
        if prepared_file.name != file_name or prepared_file.size != file_size:
            return self.build_result(
                success=False,
                remote_id=media_item.identifier,
                url=None,
                message="Primary file changed after durable snapshot; refusing to upload.",
            )
        self._expected_files[media_item.identifier] = (file_name, file_size)

        try:
            metadata_response = self._session.get(
                f"{_METADATA_ENDPOINT}/{media_item.identifier}",
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            raise PublisherTemporaryError(
                "Could not check Internet Archive for an existing item "
                f"'{media_item.identifier}': {error}"
            ) from error

        if metadata_response.status_code != 404 and not metadata_response.ok:
            if _is_transient_http_status(metadata_response.status_code):
                raise PublisherTemporaryError(
                    "Internet Archive existing-item check is temporarily unavailable for "
                    f"'{media_item.identifier}' (HTTP {metadata_response.status_code})."
                )
            raise PublisherPublishError(
                "Internet Archive existing-item check failed for "
                f"'{media_item.identifier}': HTTP {metadata_response.status_code}"
            )

        if metadata_response.ok:
            try:
                remote_data = metadata_response.json()
            except ValueError as error:
                raise PublisherPublishError(
                    "Internet Archive returned invalid metadata while checking "
                    f"'{media_item.identifier}'."
                ) from error

            remote_identifier = remote_data.get("metadata", {}).get("identifier")
            if remote_identifier is None:
                raise PublisherPublishError(
                    "Internet Archive returned successful metadata with a missing identifier "
                    f"while checking '{media_item.identifier}'."
                )
            if remote_identifier != media_item.identifier:
                raise PublisherPublishError(
                    "Internet Archive returned metadata for unexpected identifier "
                    f"'{remote_identifier}' while checking '{media_item.identifier}'."
                )

            if not _file_matches_descriptor(primary_file.path, prepared_file):
                return self.build_result(
                    success=False,
                    remote_id=media_item.identifier,
                    url=None,
                    message="Primary file changed after durable snapshot; refusing to resume.",
                )

            if remote_identifier == media_item.identifier:
                remote_file = next(
                    (
                        entry
                        for entry in remote_data.get("files", [])
                        if entry.get("name") == file_name and entry.get("source") == "original"
                    ),
                    None,
                )
                if remote_file is not None:
                    remote_size = remote_file.get("size")
                    if remote_size in (None, ""):
                        return self.build_result(
                            success=True,
                            remote_id=media_item.identifier,
                            url=f"{_DETAILS_ENDPOINT}/{media_item.identifier}",
                            message=(
                                "Matching original filename is already present on Internet "
                                "Archive and is awaiting exact size verification."
                            ),
                        )
                    if str(remote_size) == str(file_size):
                        public_url = f"{_DETAILS_ENDPOINT}/{media_item.identifier}"
                        return self.build_result(
                            success=True,
                            remote_id=media_item.identifier,
                            url=public_url,
                            message="Matching file already present on Internet Archive.",
                        )
                return self.build_result(
                    success=False,
                    remote_id=media_item.identifier,
                    url=f"{_DETAILS_ENDPOINT}/{media_item.identifier}",
                    message=(
                        "Internet Archive identifier already exists but does not "
                        "contain a matching original filename and size; refusing "
                        "to overwrite it."
                    ),
                )

        if media_item.identifier in self._reconcile_only_identifiers:
            return self.build_result(
                success=True,
                remote_id=media_item.identifier,
                url=f"{_DETAILS_ENDPOINT}/{media_item.identifier}",
                message=(
                    "Read-only reconciliation found no visible Internet Archive metadata; "
                    "refusing to start a new upload."
                ),
            )

        encoded_file_name = quote(primary_file.path.name, safe="")
        upload_url = f"{_S3_ENDPOINT}/{media_item.identifier}/{encoded_file_name}"

        headers = {
            "x-amz-auto-make-bucket": "1",
            "x-archive-keep-old-version": "1",
            "x-archive-meta01-title": _encode_metadata_header(media_item.title),
            "x-archive-meta01-description": _encode_metadata_header(media_item.description),
            "x-archive-meta01-creator": _encode_metadata_header(media_item.creator),
            "x-archive-meta01-collection": _encode_metadata_header(self._config.collection),
            "x-archive-meta01-mediatype": _encode_metadata_header(self._config.mediatype),
        }
        if media_item.tags:
            headers["x-archive-meta01-subject"] = _encode_metadata_header(";".join(media_item.tags))

        try:
            with (
                primary_file.path.open("rb") as source_file,
                tempfile.TemporaryFile(mode="w+b") as video_file,
            ):
                digest = hashlib.sha256()
                for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
                    digest.update(chunk)
                    video_file.write(chunk)
                descriptor_size = video_file.tell()
                descriptor_checksum = digest.hexdigest()
                if (
                    descriptor_size != prepared_file.size
                    or descriptor_checksum != prepared_file.sha256
                ):
                    return self.build_result(
                        success=False,
                        remote_id=media_item.identifier,
                        url=None,
                        message=(
                            "Primary file changed after durable snapshot; changed after "
                            "preflight; refusing to upload "
                            f"{primary_file.path.name!r}."
                        ),
                    )
                video_file.seek(0)
                response = self._session.put(
                    upload_url,
                    data=video_file,
                    headers=headers,
                    timeout=_UPLOAD_TIMEOUT_SECONDS,
                )
        except requests.RequestException as error:
            raise PublisherOutcomeUnknownError(
                "Internet Archive upload connection failed for "
                f"'{media_item.identifier}'. The remote outcome is unknown and "
                "must be reconciled before retrying.",
                remote_id=media_item.identifier,
            ) from error
        except OSError as error:
            return self.build_result(
                success=False,
                remote_id=None,
                url=None,
                message=f"Could not read video file '{primary_file.path}': {error}",
            )

        if _is_transient_http_status(response.status_code):
            raise PublisherOutcomeUnknownError(
                "Internet Archive returned a temporary upload response for "
                f"'{media_item.identifier}' (HTTP {response.status_code}). The remote "
                "outcome is unknown and must be reconciled before retrying.",
                remote_id=media_item.identifier,
            )

        if not response.ok:
            return self.build_result(
                success=False,
                remote_id=None,
                url=None,
                message=(
                    f"Internet Archive rejected upload for "
                    f"'{media_item.identifier}': HTTP {response.status_code}"
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
            True only if the exact remote identifier and expected
            original filename and byte size are present; False otherwise.
        """
        if self._session is None:
            raise PublisherConnectionError(
                "InternetArchivePublisher.verify() called before connect()."
            )

        if not result.remote_id:
            return False

        expected_file = self._expected_files.get(result.remote_id)
        if expected_file is None:
            logger.warning(
                "Cannot verify Internet Archive item %s without expected filename and size.",
                result.remote_id,
            )
            return False

        expected_name, expected_size = expected_file
        for attempt in range(1, _VERIFY_ATTEMPTS + 1):
            try:
                response = self._session.get(
                    f"{_METADATA_ENDPOINT}/{result.remote_id}",
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as error:
                logger.warning(
                    "Internet Archive verification attempt %d/%d failed for %s: %s",
                    attempt,
                    _VERIFY_ATTEMPTS,
                    result.remote_id,
                    error,
                )
            else:
                if response.ok:
                    try:
                        data = response.json()
                    except ValueError:
                        data = {}

                    metadata_matches = (
                        data.get("metadata", {}).get("identifier") == result.remote_id
                    )
                    exact_file_present = metadata_matches and any(
                        entry.get("name") == expected_name
                        and str(entry.get("size")) == str(expected_size)
                        and entry.get("source") == "original"
                        for entry in data.get("files", [])
                    )
                    if exact_file_present:
                        return True

            if attempt < _VERIFY_ATTEMPTS:
                time.sleep(_VERIFY_DELAY_SECONDS)

        return False

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
        """Close the HTTP session and release the identifier lifecycle lock."""
        try:
            if self._session is not None:
                self._session.close()
                logger.debug("Disconnected from Internet Archive.")
        finally:
            self._session = None
            if self._identifier_lock is not None:
                self._identifier_lock.release()
                self._identifier_lock = None
