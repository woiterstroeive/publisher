from __future__ import annotations

import json
import logging.handlers
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from filelock import FileLock

import main as main_module
from config import AppConfig
from models.media_item import MediaItem
from models.publication_result import PublicationResult
from publishers.base import (
    LocalFileDescriptor,
    Publisher,
    PublisherOutcomeUnknownError,
    PublisherTemporaryError,
)


class FakePublisher(Publisher):
    publisher_name = "internet_archive"

    def __init__(
        self,
        *,
        result: PublicationResult,
        verified: bool,
        publish_error: Exception | None = None,
        disconnect_error: Exception | None = None,
    ) -> None:
        self.result = result
        self.verified = verified
        self.publish_error = publish_error
        self.disconnect_error = disconnect_error
        self.disconnected = False
        self.prepared_descriptor: LocalFileDescriptor | None = None
        self.reconcile_only = False

    def prepare(
        self,
        media_item: MediaItem,
        descriptor: LocalFileDescriptor,
        *,
        reconcile_only: bool = False,
    ) -> None:
        self.prepared_descriptor = descriptor
        self.reconcile_only = reconcile_only

    def connect(self) -> None:
        return None

    def publish(self, media_item: MediaItem) -> PublicationResult:
        if self.publish_error is not None:
            raise self.publish_error
        return self.result

    def verify(self, result: PublicationResult) -> bool:
        return self.verified

    def get_public_url(self, result: PublicationResult) -> str | None:
        return result.url

    def disconnect(self) -> None:
        self.disconnected = True
        if self.disconnect_error is not None:
            raise self.disconnect_error


def _success_result() -> PublicationResult:
    return PublicationResult(
        success=True,
        publisher="internet_archive",
        remote_id="video-001",
        url="https://archive.org/details/video-001",
        message="Upload accepted.",
        timestamp=datetime.now(UTC),
    )


def _production(tmp_path: Path) -> Path:
    production = tmp_path / "workspace" / "video-001"
    production.mkdir(parents=True)
    (production / "video.mkv").write_bytes(b"media")
    (production / "metadata.toml").write_text(
        """identifier = "video-001"
title = "Title"
description = "Description"
creator = "Creator"
[[files]]
path = "video.mkv"
role = "primary"
""",
        encoding="utf-8",
    )
    return production


def _configure_run(
    monkeypatch,
    tmp_path: Path,
    publisher_factory: Callable[[], Publisher],
) -> AppConfig:
    config = AppConfig(
        base_dir=tmp_path,
        workspace_dir=tmp_path / "workspace",
        published_dir=tmp_path / "published",
        failed_dir=tmp_path / "failed",
        logs_dir=tmp_path / "logs",
        log_level="INFO",
    )
    for directory in (config.published_dir, config.failed_dir, config.logs_dir):
        directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_module, "_register_default_publishers", lambda: None)
    monkeypatch.setattr(
        main_module,
        "_PUBLISHER_FACTORIES",
        {"internet_archive": publisher_factory},
    )
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(main_module, "_configure_logging", lambda _: None)
    return config


def test_publisher_argument_is_required(monkeypatch) -> None:
    monkeypatch.setattr(
        main_module,
        "_PUBLISHER_FACTORIES",
        {"internet_archive": lambda: None},
    )

    with pytest.raises(SystemExit) as caught:
        main_module._parse_args(["production"])

    assert caught.value.code == 2


def test_concurrent_run_fails_closed_before_publishing(tmp_path: Path, monkeypatch) -> None:
    production = _production(tmp_path)
    publisher = FakePublisher(result=_success_result(), verified=True)
    config = _configure_run(monkeypatch, tmp_path, lambda: publisher)
    lock_path = production.parent / f".{production.name}.publisher.lock"

    with FileLock(lock_path):
        exit_code = main_module.run([str(production), "--publisher", "internet_archive"])

    assert exit_code == 2
    assert production.exists()
    assert not (config.published_dir / production.name).exists()
    assert publisher.disconnected is False


def test_logging_uses_bounded_rotation(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}
    config = AppConfig(
        base_dir=tmp_path,
        workspace_dir=tmp_path / "workspace",
        published_dir=tmp_path / "published",
        failed_dir=tmp_path / "failed",
        logs_dir=tmp_path,
        log_level="INFO",
    )
    monkeypatch.setattr(
        main_module.logging,
        "basicConfig",
        lambda **kwargs: captured.update(kwargs),
    )

    main_module._configure_logging(config)

    rotating = [
        handler
        for handler in captured["handlers"]
        if isinstance(handler, logging.handlers.RotatingFileHandler)
    ]
    assert len(rotating) == 1
    assert rotating[0].maxBytes == 10 * 1024 * 1024
    assert rotating[0].backupCount == 10


def test_verified_upload_reports_move_conflict_as_failure(tmp_path: Path, monkeypatch) -> None:
    production = _production(tmp_path)
    publisher = FakePublisher(result=_success_result(), verified=True)
    config = _configure_run(monkeypatch, tmp_path, lambda: publisher)
    destination = config.published_dir / production.name
    destination.mkdir()
    sentinel = destination / "existing.txt"
    sentinel.write_text("do not overwrite", encoding="utf-8")

    exit_code = main_module.run([str(production), "--publisher", "internet_archive"])

    record = json.loads((production / "publication-result.json").read_text("utf-8"))
    assert exit_code == 1
    assert production.exists()
    assert sentinel.read_text("utf-8") == "do not overwrite"
    assert record["state"] == "verified_move_failed"
    assert "Destination already exists" in record["message"]


def test_move_os_error_keeps_source_and_becomes_explicit_error(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "workspace" / "video-001"
    destination_root = tmp_path / "published"
    source.mkdir(parents=True)
    destination_root.mkdir()

    def fail_rename(self: Path, target: Path) -> Path:
        raise OSError("cross-device move refused")

    monkeypatch.setattr(Path, "rename", fail_rename)

    with pytest.raises(main_module.ProductionMoveError) as caught:
        main_module._move_production(source, destination_root)

    assert source.exists()
    assert "cross-device move refused" in str(caught.value)


def test_unverified_upload_stays_in_workspace_with_durable_state(
    tmp_path: Path, monkeypatch
) -> None:
    production = _production(tmp_path)
    publisher = FakePublisher(result=_success_result(), verified=False)
    config = _configure_run(monkeypatch, tmp_path, lambda: publisher)

    exit_code = main_module.run([str(production), "--publisher", "internet_archive"])

    record = json.loads((production / "publication-result.json").read_text("utf-8"))
    assert exit_code == 2
    assert production.exists()
    assert not (config.published_dir / production.name).exists()
    assert record["state"] == "accepted_unverified"
    assert record["remote_id"] == "video-001"
    assert record["local_primary_file"]["name"] == "video.mkv"
    assert record["local_primary_file"]["size"] == 5
    assert record["local_primary_file"]["sha256"] == (
        "721c9525ade2ea8903d343ef25cf68b9bf4ab0aad56bb7b01fbe48d09bc7fcf4"
    )
    assert publisher.prepared_descriptor is not None
    assert publisher.prepared_descriptor.to_record() == record["local_primary_file"]
    assert publisher.disconnected is True


def test_unknown_remote_outcome_stays_in_workspace(tmp_path: Path, monkeypatch) -> None:
    production = _production(tmp_path)
    publisher = FakePublisher(
        result=_success_result(),
        verified=False,
        publish_error=PublisherOutcomeUnknownError(
            "Upload result must be reconciled.", remote_id="video-001"
        ),
    )
    config = _configure_run(monkeypatch, tmp_path, lambda: publisher)

    exit_code = main_module.run([str(production), "--publisher", "internet_archive"])

    record = json.loads((production / "publication-result.json").read_text("utf-8"))
    assert exit_code == 2
    assert production.exists()
    assert not (config.failed_dir / production.name).exists()
    assert record["state"] == "uncertain"
    assert record["publisher"] == "internet_archive"
    assert record["remote_id"] == "video-001"
    assert publisher.disconnected is True


def test_disconnect_failure_does_not_mask_unknown_upload_outcome(
    tmp_path: Path, monkeypatch
) -> None:
    production = _production(tmp_path)
    publisher = FakePublisher(
        result=_success_result(),
        verified=False,
        publish_error=PublisherOutcomeUnknownError(
            "Upload outcome is unknown.", remote_id="video-001"
        ),
        disconnect_error=PublisherTemporaryError("Session cleanup failed."),
    )
    _configure_run(monkeypatch, tmp_path, lambda: publisher)

    exit_code = main_module.run([str(production), "--publisher", "internet_archive"])

    record = json.loads((production / "publication-result.json").read_text("utf-8"))
    assert exit_code == 2
    assert record["state"] == "uncertain"
    assert record["remote_id"] == "video-001"


def test_temporary_failure_stays_retryable_in_workspace(tmp_path: Path, monkeypatch) -> None:
    production = _production(tmp_path)
    publisher = FakePublisher(
        result=_success_result(),
        verified=False,
        publish_error=PublisherTemporaryError("Metadata endpoint timed out."),
    )
    config = _configure_run(monkeypatch, tmp_path, lambda: publisher)

    exit_code = main_module.run([str(production), "--publisher", "internet_archive"])

    record = json.loads((production / "publication-result.json").read_text("utf-8"))
    assert exit_code == 2
    assert production.exists()
    assert not (config.failed_dir / production.name).exists()
    assert record["state"] == "retryable"
    assert record["publisher"] == "internet_archive"
    assert publisher.disconnected is True


def test_uncertain_resume_refuses_changed_local_identity(tmp_path: Path, monkeypatch) -> None:
    production = _production(tmp_path)
    (production / "publication-result.json").write_text(
        json.dumps(
            {
                "state": "uncertain",
                "publisher": "internet_archive",
                "local_identifier": "different-id",
                "local_primary_file": {
                    "name": "video.mkv",
                    "size": 5,
                    "sha256": "not-the-current-hash",
                },
                "attempt": 1,
            }
        ),
        encoding="utf-8",
    )
    publisher = FakePublisher(result=_success_result(), verified=True)
    config = _configure_run(monkeypatch, tmp_path, lambda: publisher)

    exit_code = main_module.run([str(production), "--publisher", "internet_archive"])

    record = json.loads((production / "publication-result.json").read_text("utf-8"))
    assert exit_code == 1
    assert production.exists()
    assert not (config.published_dir / production.name).exists()
    assert record["state"] == "uncertain"
    assert publisher.disconnected is False


def test_uncertain_same_identity_resume_is_reconciliation_only(tmp_path: Path, monkeypatch) -> None:
    production = _production(tmp_path)
    (production / "publication-result.json").write_text(
        json.dumps(
            {
                "state": "uncertain",
                "publisher": "internet_archive",
                "local_identifier": "video-001",
                "local_primary_file": {
                    "name": "video.mkv",
                    "size": 5,
                    "sha256": ("721c9525ade2ea8903d343ef25cf68b9bf4ab0aad56bb7b01fbe48d09bc7fcf4"),
                },
                "attempt": 1,
            }
        ),
        encoding="utf-8",
    )
    publisher = FakePublisher(result=_success_result(), verified=False)
    _configure_run(monkeypatch, tmp_path, lambda: publisher)

    exit_code = main_module.run([str(production), "--publisher", "internet_archive"])

    assert exit_code == 2
    assert publisher.reconcile_only is True


@pytest.mark.parametrize("record", [{}, {"state": "mystery"}])
def test_unknown_durable_state_fails_closed(
    record: dict[str, object], tmp_path: Path, monkeypatch
) -> None:
    production = _production(tmp_path)
    record_path = production / "publication-result.json"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    publisher = FakePublisher(result=_success_result(), verified=True)
    config = _configure_run(monkeypatch, tmp_path, lambda: publisher)

    exit_code = main_module.run([str(production), "--publisher", "internet_archive"])

    assert exit_code == 1
    assert production.exists()
    assert not (config.published_dir / production.name).exists()
    assert json.loads(record_path.read_text("utf-8")) == record
    assert publisher.disconnected is False


@pytest.mark.parametrize("state", ["verified", "verified_move_failed"])
def test_confirmed_remote_state_refuses_changed_local_identity(
    state: str, tmp_path: Path, monkeypatch
) -> None:
    production = _production(tmp_path)
    (production / "publication-result.json").write_text(
        json.dumps(
            {
                "state": state,
                "publisher": "internet_archive",
                "local_identifier": "different-id",
                "local_primary_file": {
                    "name": "video.mkv",
                    "size": 5,
                    "sha256": "not-the-current-hash",
                },
                "attempt": 1,
            }
        ),
        encoding="utf-8",
    )
    publisher = FakePublisher(result=_success_result(), verified=True)
    config = _configure_run(monkeypatch, tmp_path, lambda: publisher)

    exit_code = main_module.run([str(production), "--publisher", "internet_archive"])

    record = json.loads((production / "publication-result.json").read_text("utf-8"))
    assert exit_code == 1
    assert production.exists()
    assert not (config.published_dir / production.name).exists()
    assert record["state"] == state
    assert publisher.disconnected is False


def test_verified_upload_moves_to_published_with_record(tmp_path: Path, monkeypatch) -> None:
    production = _production(tmp_path)
    publisher = FakePublisher(result=_success_result(), verified=True)
    config = _configure_run(monkeypatch, tmp_path, lambda: publisher)

    exit_code = main_module.run([str(production), "--publisher", "internet_archive"])

    destination = config.published_dir / production.name
    record = json.loads((destination / "publication-result.json").read_text("utf-8"))
    assert exit_code == 0
    assert not production.exists()
    assert record["state"] == "verified"
    assert record["url"] == "https://archive.org/details/video-001"


def test_failed_result_moves_to_failed_with_record(tmp_path: Path, monkeypatch) -> None:
    production = _production(tmp_path)
    failed_result = PublicationResult(
        success=False,
        publisher="internet_archive",
        remote_id=None,
        url=None,
        message="HTTP 403 collection rejected",
        timestamp=datetime.now(UTC),
    )
    publisher = FakePublisher(result=failed_result, verified=False)
    config = _configure_run(monkeypatch, tmp_path, lambda: publisher)

    exit_code = main_module.run([str(production), "--publisher", "internet_archive"])

    destination = config.failed_dir / production.name
    record = json.loads((destination / "publication-result.json").read_text("utf-8"))
    assert exit_code == 1
    assert record["state"] == "failed"
    assert record["message"] == "HTTP 403 collection rejected"


def test_failed_destination_conflict_is_recorded_without_overwrite(
    tmp_path: Path, monkeypatch
) -> None:
    production = _production(tmp_path)
    failed_result = PublicationResult(
        success=False,
        publisher="internet_archive",
        remote_id=None,
        url=None,
        message="Rejected",
        timestamp=datetime.now(UTC),
    )
    publisher = FakePublisher(result=failed_result, verified=False)
    config = _configure_run(monkeypatch, tmp_path, lambda: publisher)
    destination = config.failed_dir / production.name
    destination.mkdir()
    sentinel = destination / "existing.txt"
    sentinel.write_text("keep", encoding="utf-8")

    exit_code = main_module.run([str(production), "--publisher", "internet_archive"])

    record = json.loads((production / "publication-result.json").read_text("utf-8"))
    assert exit_code == 1
    assert production.exists()
    assert sentinel.read_text("utf-8") == "keep"
    assert record["state"] == "failed_move_failed"
    assert "Destination already exists" in record["message"]
