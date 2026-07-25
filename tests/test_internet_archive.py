from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import requests
from filelock import FileLock, Timeout

import publishers.internet_archive as ia_module
from config import ConfigurationError
from models.media_item import MediaFile, MediaItem, Subtitle
from models.publication_result import PublicationResult
from publishers.base import LocalFileDescriptor, PublisherConnectionError
from publishers.internet_archive import InternetArchiveConfig, InternetArchivePublisher

_publishers_to_disconnect: list[InternetArchivePublisher] = []


@pytest.fixture(autouse=True)
def disconnect_publishers_after_each_test() -> Any:
    """Keep test publishers from leaking lifecycle locks into later tests."""
    yield
    try:
        for publisher in reversed(_publishers_to_disconnect):
            publisher.disconnect()
    finally:
        _publishers_to_disconnect.clear()


def test_config_rejects_whitespace_only_required_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IA_ACCESS_KEY", "   ")
    monkeypatch.setenv("IA_SECRET_KEY", "secret")
    monkeypatch.setenv("IA_COLLECTION", "collection")

    with pytest.raises(ConfigurationError, match="IA_ACCESS_KEY"):
        InternetArchiveConfig.from_env()


def test_identifier_lock_blocks_second_local_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_directory = tmp_path / "locks"
    monkeypatch.setattr(ia_module, "_IDENTIFIER_LOCK_DIRECTORY", lock_directory)
    session = FakeSession()
    publisher = _publisher(session)
    item = _media_item(tmp_path)
    lock_path = lock_directory / "video-001.lock"
    lock_path.parent.mkdir(parents=True)

    with (
        FileLock(lock_path),
        pytest.raises(ia_module.PublisherTemporaryError, match="already publishing"),
    ):
        publisher.publish(item)

    assert session.get_calls == []
    assert session.put_calls == []


def test_identifier_lock_is_held_through_verification_until_disconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_directory = tmp_path / "locks"
    monkeypatch.setattr(ia_module, "_IDENTIFIER_LOCK_DIRECTORY", lock_directory)
    session = FakeSession()
    session.get_responses = [
        FakeResponse(status_code=404),
        FakeResponse(
            json_data={
                "metadata": {"identifier": "video-001"},
                "files": [{"name": "video.mkv", "size": "5", "source": "original"}],
            }
        ),
    ]
    publisher = _publisher(session)
    result = publisher.publish(_media_item(tmp_path))
    lock_path = lock_directory / "video-001.lock"

    with pytest.raises(Timeout):
        FileLock(lock_path).acquire(timeout=0)

    assert publisher.verify(result) is True
    with pytest.raises(Timeout):
        FileLock(lock_path).acquire(timeout=0)

    publisher.disconnect()
    with FileLock(lock_path):
        pass


def test_disconnect_releases_identifier_lock_when_session_close_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_directory = tmp_path / "locks"
    monkeypatch.setattr(ia_module, "_IDENTIFIER_LOCK_DIRECTORY", lock_directory)
    session = FakeSession()
    session.get_responses = [FakeResponse(status_code=404)]
    publisher = _publisher(session)
    publisher.publish(_media_item(tmp_path))
    session.close_error = RuntimeError("close failed")

    with pytest.raises(RuntimeError, match="close failed"):
        publisher.disconnect()

    with FileLock(lock_directory / "video-001.lock"):
        pass


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        json_data: dict[str, Any] | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    def json(self) -> dict[str, Any]:
        return self._json_data


class FakeSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.get_responses: list[FakeResponse | Exception] = []
        self.put_response: FakeResponse | Exception = FakeResponse()
        self.get_calls: list[str] = []
        self.put_calls: list[str] = []
        self.put_kwargs: list[dict[str, Any]] = []
        self.close_error: Exception | None = None

    def get(self, url: str, **_: Any) -> FakeResponse:
        self.get_calls.append(url)
        response = self.get_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def put(self, url: str, **kwargs: Any) -> FakeResponse:
        self.put_calls.append(url)
        self.put_kwargs.append(kwargs)
        if isinstance(self.put_response, Exception):
            raise self.put_response
        return self.put_response

    def close(self) -> None:
        if self.close_error is not None:
            raise self.close_error


def _publisher(session: FakeSession) -> InternetArchivePublisher:
    publisher = InternetArchivePublisher(
        InternetArchiveConfig(
            access_key="access",
            secret_key="secret",
            collection="opensource_movies",
        )
    )
    publisher._session = session  # type: ignore[assignment]
    _publishers_to_disconnect.append(publisher)
    return publisher


def _media_item(tmp_path: Path, *, identifier: str = "video-001") -> MediaItem:
    video = tmp_path / "video.mkv"
    video.write_bytes(b"media")
    return MediaItem(
        identifier=identifier,
        title="Title",
        description="Description",
        creator="Creator",
        files=(MediaFile(video),),
    )


def _result(identifier: str = "video-001") -> PublicationResult:
    return PublicationResult(
        success=True,
        publisher="internet_archive",
        remote_id=identifier,
        url=f"https://archive.org/details/{identifier}",
        message="accepted",
        timestamp=datetime.now(UTC),
    )


def test_rejects_invalid_archive_identifier_before_network_write(tmp_path: Path) -> None:
    session = FakeSession()
    publisher = _publisher(session)

    result = publisher.publish(_media_item(tmp_path, identifier="Invalid identifier!"))

    assert result.success is False
    assert "Invalid Internet Archive identifier" in result.message
    assert session.put_calls == []


def test_publish_before_connect_does_not_retain_identifier_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_directory = tmp_path / "locks"
    monkeypatch.setattr(ia_module, "_IDENTIFIER_LOCK_DIRECTORY", lock_directory)
    publisher = InternetArchivePublisher(
        InternetArchiveConfig("access", "secret", "opensource_movies")
    )

    with pytest.raises(PublisherConnectionError, match="before connect"):
        publisher.publish(_media_item(tmp_path))

    with FileLock(lock_directory / "video-001.lock"):
        pass


def test_reconcile_only_metadata_404_never_starts_put(tmp_path: Path) -> None:
    session = FakeSession()
    session.get_responses = [FakeResponse(status_code=404)]
    publisher = _publisher(session)
    item = _media_item(tmp_path)
    descriptor = LocalFileDescriptor(
        name="video.mkv",
        size=5,
        sha256="721c9525ade2ea8903d343ef25cf68b9bf4ab0aad56bb7b01fbe48d09bc7fcf4",
    )
    publisher.prepare(item, descriptor, reconcile_only=True)

    result = publisher.publish(item)

    assert result.success is True
    assert result.remote_id == "video-001"
    assert session.put_calls == []


def test_connect_rejects_any_non_success_response(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    session.get_responses = [FakeResponse(status_code=400, text="bad request")]
    monkeypatch.setattr(ia_module.requests, "Session", lambda: session)
    publisher = InternetArchivePublisher(
        InternetArchiveConfig("access", "secret", "opensource_movies")
    )

    with pytest.raises(PublisherConnectionError):
        publisher.connect()


def test_connect_classifies_transient_http_failure_as_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    session.get_responses = [FakeResponse(status_code=503, text="try later")]
    monkeypatch.setattr(ia_module.requests, "Session", lambda: session)
    publisher = InternetArchivePublisher(
        InternetArchiveConfig(
            access_key="access", secret_key="secret", collection="opensource_movies"
        )
    )

    with pytest.raises(Exception) as caught:
        publisher.connect()

    assert type(caught.value).__name__ == "PublisherTemporaryError"


def test_connect_close_failure_does_not_mask_primary_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    session = FakeSession()
    session.get_responses = [FakeResponse(status_code=503)]
    session.close_error = RuntimeError("close failed")
    monkeypatch.setattr(ia_module.requests, "Session", lambda: session)
    publisher = InternetArchivePublisher(
        InternetArchiveConfig("access", "secret", "opensource_movies")
    )

    with pytest.raises(ia_module.PublisherTemporaryError, match="HTTP 503"):
        publisher.connect()

    assert "close failed" in caplog.text


def test_preflight_timeout_is_retryable_and_does_not_upload(tmp_path: Path) -> None:
    session = FakeSession()
    session.get_responses = [requests.Timeout("preflight timed out")]
    publisher = _publisher(session)

    with pytest.raises(Exception) as caught:
        publisher.publish(_media_item(tmp_path))

    assert type(caught.value).__name__ == "PublisherTemporaryError"
    assert session.put_calls == []


def test_transient_upload_response_has_unknown_remote_outcome(tmp_path: Path) -> None:
    session = FakeSession()
    session.get_responses = [FakeResponse(status_code=404)]
    session.put_response = FakeResponse(status_code=503, text="try later")
    publisher = _publisher(session)

    with pytest.raises(Exception) as caught:
        publisher.publish(_media_item(tmp_path))

    assert type(caught.value).__name__ == "PublisherOutcomeUnknownError"
    assert caught.value.remote_id == "video-001"


def test_reuses_existing_matching_archive_file_without_upload(tmp_path: Path) -> None:
    session = FakeSession()
    session.get_responses = [
        FakeResponse(
            json_data={
                "metadata": {"identifier": "video-001"},
                "files": [{"name": "video.mkv", "size": "5", "source": "original"}],
            }
        )
    ]
    publisher = _publisher(session)

    result = publisher.publish(_media_item(tmp_path))

    assert result.success is True
    assert result.remote_id == "video-001"
    assert "already present" in result.message.lower()
    assert session.put_calls == []


def test_existing_item_resume_refuses_same_size_local_change(tmp_path: Path) -> None:
    item = _media_item(tmp_path)

    class MutatingSession(FakeSession):
        def get(self, url: str, **kwargs: Any) -> FakeResponse:
            response = super().get(url, **kwargs)
            item.primary_file().path.write_bytes(b"MEDIA")
            return response

    session = MutatingSession()
    session.get_responses = [
        FakeResponse(
            json_data={
                "metadata": {"identifier": "video-001"},
                "files": [{"name": "video.mkv", "size": "5", "source": "original"}],
            }
        )
    ]
    publisher = _publisher(session)
    publisher.prepare(
        item,
        LocalFileDescriptor(
            name="video.mkv",
            size=5,
            sha256="721c9525ade2ea8903d343ef25cf68b9bf4ab0aad56bb7b01fbe48d09bc7fcf4",
        ),
    )

    result = publisher.publish(item)

    assert result.success is False
    assert "changed after durable snapshot" in result.message
    assert session.put_calls == []


def test_existing_file_without_remote_size_is_resumed_without_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = FakeSession()
    session.get_responses = [
        FakeResponse(
            json_data={
                "metadata": {"identifier": "video-001"},
                "files": [{"name": "video.mkv", "source": "original"}],
            }
        ),
        FakeResponse(
            json_data={
                "metadata": {"identifier": "video-001"},
                "files": [{"name": "video.mkv", "size": "5", "source": "original"}],
            }
        ),
    ]
    publisher = _publisher(session)
    monkeypatch.setattr(ia_module, "_VERIFY_DELAY_SECONDS", 0)

    result = publisher.publish(_media_item(tmp_path))

    assert result.success is True
    assert session.put_calls == []
    assert publisher.verify(result) is True


def test_refuses_to_overwrite_conflicting_existing_archive_item(tmp_path: Path) -> None:
    session = FakeSession()
    session.get_responses = [
        FakeResponse(
            json_data={
                "metadata": {"identifier": "video-001"},
                "files": [{"name": "video.mkv", "size": "999", "source": "original"}],
            }
        )
    ]
    publisher = _publisher(session)

    result = publisher.publish(_media_item(tmp_path))

    assert result.success is False
    assert "refusing to overwrite" in result.message.lower()
    assert session.put_calls == []


def test_preflight_refuses_metadata_for_different_identifier(tmp_path: Path) -> None:
    session = FakeSession()
    session.get_responses = [
        FakeResponse(
            json_data={
                "metadata": {"identifier": "different-item"},
                "files": [],
            }
        )
    ]
    publisher = _publisher(session)

    with pytest.raises(ia_module.PublisherPublishError, match="unexpected identifier"):
        publisher.publish(_media_item(tmp_path))

    assert session.put_calls == []


def test_preflight_refuses_success_response_without_identifier(tmp_path: Path) -> None:
    session = FakeSession()
    session.get_responses = [FakeResponse(json_data={})]
    publisher = _publisher(session)

    with pytest.raises(ia_module.PublisherPublishError, match="missing identifier"):
        publisher.publish(_media_item(tmp_path))

    assert session.put_calls == []


def test_reports_unknown_outcome_when_upload_connection_breaks(tmp_path: Path) -> None:
    session = FakeSession()
    session.get_responses = [FakeResponse(status_code=404)]
    session.put_response = requests.Timeout("connection lost after send")
    publisher = _publisher(session)

    with pytest.raises(Exception) as caught:
        publisher.publish(_media_item(tmp_path))

    assert type(caught.value).__name__ == "PublisherOutcomeUnknownError"
    assert caught.value.remote_id == "video-001"
    assert "must be reconciled" in str(caught.value)


def test_verify_retries_until_exact_original_file_is_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    session.get_responses = [
        FakeResponse(
            json_data={
                "metadata": {"identifier": "video-001"},
                "files": [{"name": "metadata.xml", "size": "5", "source": "original"}],
            }
        ),
        FakeResponse(
            json_data={
                "metadata": {"identifier": "video-001"},
                "files": [{"name": "video.mkv", "size": "5", "source": "original"}],
            }
        ),
    ]
    publisher = _publisher(session)
    publisher._expected_files["video-001"] = ("video.mkv", 5)
    monkeypatch.setattr(ia_module, "_VERIFY_ATTEMPTS", 2, raising=False)
    monkeypatch.setattr(ia_module, "_VERIFY_DELAY_SECONDS", 0, raising=False)

    assert publisher.verify(_result()) is True
    assert len(session.get_calls) == 2


def test_verify_rejects_metadata_for_different_identifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = FakeSession()
    session.get_responses = [
        FakeResponse(
            json_data={
                "metadata": {"identifier": "different-item"},
                "files": [{"name": "video.mkv", "size": "5", "source": "original"}],
            }
        )
    ]
    publisher = _publisher(session)
    publisher._expected_files["video-001"] = ("video.mkv", 5)
    monkeypatch.setattr(ia_module, "_VERIFY_ATTEMPTS", 1)

    assert publisher.verify(_result()) is False


def test_refuses_file_changed_between_preflight_and_put(tmp_path: Path) -> None:
    item = _media_item(tmp_path)

    class MutatingSession(FakeSession):
        def get(self, url: str, **kwargs: Any) -> FakeResponse:
            response = super().get(url, **kwargs)
            item.primary_file().path.write_bytes(b"changed-size")
            return response

    session = MutatingSession()
    session.get_responses = [FakeResponse(status_code=404)]
    publisher = _publisher(session)

    result = publisher.publish(item)

    assert result.success is False
    assert "changed after preflight" in result.message
    assert session.put_calls == []


def test_refuses_same_size_file_changed_after_durable_snapshot(tmp_path: Path) -> None:
    item = _media_item(tmp_path)

    class MutatingSession(FakeSession):
        def get(self, url: str, **kwargs: Any) -> FakeResponse:
            response = super().get(url, **kwargs)
            item.primary_file().path.write_bytes(b"MEDIA")
            return response

    session = MutatingSession()
    session.get_responses = [FakeResponse(status_code=404)]
    publisher = _publisher(session)
    publisher.prepare(
        item,
        LocalFileDescriptor(
            name="video.mkv",
            size=5,
            sha256="721c9525ade2ea8903d343ef25cf68b9bf4ab0aad56bb7b01fbe48d09bc7fcf4",
        ),
    )

    result = publisher.publish(item)

    assert result.success is False
    assert "changed after durable snapshot" in result.message
    assert session.put_calls == []


def test_upload_stream_uses_immutable_verified_snapshot(tmp_path: Path) -> None:
    item = _media_item(tmp_path)

    class PutMutatingSession(FakeSession):
        streamed_bytes = b""

        def put(self, url: str, **kwargs: Any) -> FakeResponse:
            item.primary_file().path.write_bytes(b"MEDIA")
            self.streamed_bytes = kwargs["data"].read()
            return super().put(url, **kwargs)

    session = PutMutatingSession()
    session.get_responses = [FakeResponse(status_code=404)]
    publisher = _publisher(session)
    publisher.prepare(
        item,
        LocalFileDescriptor(
            name="video.mkv",
            size=5,
            sha256="721c9525ade2ea8903d343ef25cf68b9bf4ab0aad56bb7b01fbe48d09bc7fcf4",
        ),
    )

    result = publisher.publish(item)

    assert result.success is True
    assert session.streamed_bytes == b"media"


def test_upload_url_encodes_primary_filename_as_one_path_segment(tmp_path: Path) -> None:
    media_path = tmp_path / "clip #1%.mkv"
    media_path.write_bytes(b"media")
    item = MediaItem(
        identifier="video-001",
        title="Title",
        description="Description",
        creator="Creator",
        files=[MediaFile(path=media_path, role="primary")],
    )
    session = FakeSession()
    session.get_responses = [FakeResponse(status_code=404)]
    publisher = _publisher(session)

    result = publisher.publish(item)

    assert result.success is True
    assert session.put_calls == ["https://s3.us.archive.org/video-001/clip%20%231%25.mkv"]


def test_uri_encodes_unicode_metadata_headers(tmp_path: Path) -> None:
    session = FakeSession()
    session.get_responses = [FakeResponse(status_code=404)]
    publisher = _publisher(session)
    item = _media_item(tmp_path)
    item = MediaItem(
        identifier=item.identifier,
        title="日本語 title",
        description=item.description,
        creator=item.creator,
        files=item.files,
    )

    result = publisher.publish(item)

    headers = session.put_kwargs[0]["headers"]
    assert result.success is True
    assert headers["x-archive-meta01-title"] == ("uri(%E6%97%A5%E6%9C%AC%E8%AA%9E%20title)")
    assert headers["x-archive-keep-old-version"] == "1"


def test_rejects_assets_that_would_otherwise_be_silently_ignored(tmp_path: Path) -> None:
    session = FakeSession()
    publisher = _publisher(session)
    item = _media_item(tmp_path)
    subtitle_path = tmp_path / "captions.srt"
    subtitle_path.write_text("captions", encoding="utf-8")
    item = MediaItem(
        identifier=item.identifier,
        title=item.title,
        description=item.description,
        creator=item.creator,
        files=item.files,
        subtitles=(Subtitle(subtitle_path, "en"),),
    )

    result = publisher.publish(item)

    assert result.success is False
    assert "does not yet support subtitles or thumbnails" in result.message
    assert session.put_calls == []
