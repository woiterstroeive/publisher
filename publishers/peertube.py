"""
publishers/peertube.py

PeerTube publisher implementation.

Responsibility:
    Implement the Publisher interface for PeerTube instances: OAuth2
    authentication, video upload, and translation of PeerTube's API
    responses into PublicationResult.

All PeerTube-specific concepts (OAuth client credentials, channel IDs,
privacy levels, PeerTube's JSON response shapes) are confined to this
file. Nothing outside publishers/peertube.py may depend on them.
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
_UPLOAD_TIMEOUT_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class PeerTubeConfig:
    """
    PeerTube-specific configuration, owned entirely by this module.

    Attributes:
        instance_url: Base URL of the PeerTube instance, e.g.
            "https://videos.example.org" (no trailing slash).
        username: Account username used to authenticate.
        password: Account password used to authenticate.
        channel_id: Numeric ID of the channel to publish videos to.
        privacy: PeerTube privacy level. 1=Public, 2=Unlisted,
            3=Private, 4=Internal. Defaults to Public.
    """

    instance_url: str
    username: str
    password: str
    channel_id: int
    privacy: int = 1

    @classmethod
    def from_env(cls) -> PeerTubeConfig:
        """
        Build and validate PeerTubeConfig from the process environment.

        Expects PEERTUBE_INSTANCE_URL, PEERTUBE_USERNAME,
        PEERTUBE_PASSWORD, and PEERTUBE_CHANNEL_ID to be set (e.g. via
        a loaded .env file). PEERTUBE_PRIVACY is optional.

        Returns:
            A validated PeerTubeConfig.

        Raises:
            ConfigurationError: If a required variable is missing or
                a numeric variable cannot be parsed.
        """
        try:
            instance_url = os.environ["PEERTUBE_INSTANCE_URL"].rstrip("/")
            username = os.environ["PEERTUBE_USERNAME"]
            password = os.environ["PEERTUBE_PASSWORD"]
            channel_id_raw = os.environ["PEERTUBE_CHANNEL_ID"]
        except KeyError as missing:
            raise ConfigurationError(
                f"Missing required PeerTube environment variable: {missing}"
            ) from missing

        try:
            channel_id = int(channel_id_raw)
        except ValueError as error:
            raise ConfigurationError(
                f"PEERTUBE_CHANNEL_ID must be an integer, got: {channel_id_raw!r}"
            ) from error

        privacy_raw = os.environ.get("PEERTUBE_PRIVACY", "1")
        try:
            privacy = int(privacy_raw)
        except ValueError as error:
            raise ConfigurationError(
                f"PEERTUBE_PRIVACY must be an integer, got: {privacy_raw!r}"
            ) from error

        return cls(
            instance_url=instance_url,
            username=username,
            password=password,
            channel_id=channel_id,
            privacy=privacy,
        )


class PeerTubePublisher(Publisher):
    """
    Publisher implementation for PeerTube instances.

    Authenticates using PeerTube's OAuth2 password grant, uploads
    videos via the instance's REST API, and reports outcomes as
    PublicationResult.
    """

    publisher_name = "peertube"

    def __init__(self, config: PeerTubeConfig) -> None:
        """
        Args:
            config: Validated PeerTube configuration.
        """
        self._config = config
        self._session: requests.Session | None = None
        self._access_token: str | None = None

    def connect(self) -> None:
        """
        Authenticate with the PeerTube instance via OAuth2 password
        grant.

        Raises:
            PublisherConnectionError: If the OAuth client credentials
                cannot be retrieved, or authentication fails.
        """
        session = requests.Session()

        try:
            client_response = session.get(
                f"{self._config.instance_url}/api/v1/oauth-clients/local",
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
            client_response.raise_for_status()
            client_credentials = client_response.json()
            client_id = client_credentials["client_id"]
            client_secret = client_credentials["client_secret"]
        except (requests.RequestException, KeyError, ValueError) as error:
            raise PublisherConnectionError(
                f"Could not retrieve PeerTube OAuth client credentials "
                f"from {self._config.instance_url}: {error}"
            ) from error

        try:
            token_response = session.post(
                f"{self._config.instance_url}/api/v1/users/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "password",
                    "username": self._config.username,
                    "password": self._config.password,
                    "response_type": "code",
                },
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
            token_response.raise_for_status()
            token_data = token_response.json()
            access_token = token_data["access_token"]
        except (requests.RequestException, KeyError, ValueError) as error:
            raise PublisherConnectionError(
                f"PeerTube authentication failed for user '{self._config.username}': {error}"
            ) from error

        session.headers.update({"Authorization": f"Bearer {access_token}"})
        self._session = session
        self._access_token = access_token
        logger.info(
            "Connected to PeerTube instance %s as '%s'",
            self._config.instance_url,
            self._config.username,
        )

    def publish(self, media_item: MediaItem) -> PublicationResult:
        """
        Upload a MediaItem's primary file to PeerTube.

        Args:
            media_item: The media item to publish.

        Returns:
            A PublicationResult describing the outcome. Ordinary
            upload failures (rejected requests, PeerTube-side errors)
            are reported as success=False, not raised.

        Raises:
            PublisherConnectionError: If connect() was not called
                first.
        """
        if self._session is None:
            raise PublisherConnectionError("PeerTubePublisher.publish() called before connect().")

        try:
            primary_file = media_item.primary_file()
        except ValueError as error:
            return self.build_result(
                success=False,
                remote_id=None,
                url=None,
                message=str(error),
            )

        form_data: dict[str, object] = {
            "channelId": str(self._config.channel_id),
            "name": media_item.title,
            "description": media_item.description,
            "privacy": str(self._config.privacy),
        }
        if media_item.tags:
            form_data["tags[]"] = list(media_item.tags)

        try:
            with primary_file.path.open("rb") as video_file:
                files = {"videofile": (primary_file.path.name, video_file)}
                response = self._session.post(
                    f"{self._config.instance_url}/api/v1/videos/upload",
                    data=form_data,
                    files=files,
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
                f"PeerTube upload request failed for '{media_item.identifier}': {error}"
            ) from error

        if not response.ok:
            return self.build_result(
                success=False,
                remote_id=None,
                url=None,
                message=(
                    f"PeerTube rejected upload for '{media_item.identifier}': "
                    f"HTTP {response.status_code}"
                ),
            )

        try:
            response_data = response.json()
            video_uuid = response_data["video"]["uuid"]
        except (ValueError, KeyError) as error:
            return self.build_result(
                success=False,
                remote_id=None,
                url=None,
                message=(f"PeerTube upload succeeded but response was unexpected: {error}"),
            )

        public_url = f"{self._config.instance_url}/w/{video_uuid}"
        logger.info("Published '%s' to PeerTube as %s", media_item.identifier, video_uuid)

        return self.build_result(
            success=True,
            remote_id=video_uuid,
            url=public_url,
            message="Upload accepted by PeerTube.",
        )

    def verify(self, result: PublicationResult) -> bool:
        """
        Confirm the video is retrievable from the PeerTube instance.

        Args:
            result: A PublicationResult previously returned by
                publish().

        Returns:
            True if the video can be retrieved via the API, False
            otherwise (including if remote_id is missing, e.g.
            because the original publish() failed).
        """
        if self._session is None:
            raise PublisherConnectionError("PeerTubePublisher.verify() called before connect().")

        if not result.remote_id:
            return False

        try:
            response = self._session.get(
                f"{self._config.instance_url}/api/v1/videos/{result.remote_id}",
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            logger.warning(
                "PeerTube verification request failed for %s: %s",
                result.remote_id,
                error,
            )
            return False

        return response.ok

    def get_public_url(self, result: PublicationResult) -> str | None:
        """
        Return the public URL recorded in a PublicationResult.

        PeerTube's URL is deterministic from the video UUID, so this
        simply returns the URL already computed in publish(). Kept as
        its own method (rather than telling callers to just read
        result.url) to satisfy the Publisher interface and to leave
        room for publishers where the URL must be looked up
        separately.

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
            self._access_token = None
            logger.debug("Disconnected from PeerTube instance %s", self._config.instance_url)
