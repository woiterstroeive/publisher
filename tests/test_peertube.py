from __future__ import annotations

from pathlib import Path
from typing import Any

from models.media_item import MediaFile, MediaItem
from publishers.peertube import PeerTubeConfig, PeerTubePublisher


class RejectedResponse:
    ok = False
    status_code = 400
    text = "upstream echoed access_token=secret-value-that-must-not-persist"


class RejectingSession:
    def post(self, *_: Any, **__: Any) -> RejectedResponse:
        return RejectedResponse()


def test_rejected_upload_does_not_persist_peer_response_body(tmp_path: Path) -> None:
    video = tmp_path / "video.mkv"
    video.write_bytes(b"media")
    item = MediaItem(
        identifier="video-001",
        title="Title",
        description="Description",
        creator="Creator",
        files=(MediaFile(video),),
    )
    publisher = PeerTubePublisher(
        PeerTubeConfig(
            instance_url="https://video.example",
            username="user",
            password="password",
            channel_id=1,
        )
    )
    publisher._session = RejectingSession()  # type: ignore[assignment]

    result = publisher.publish(item)

    assert result.success is False
    assert "HTTP 400" in result.message
    assert "secret-value-that-must-not-persist" not in result.message
    assert RejectedResponse.text not in result.message
