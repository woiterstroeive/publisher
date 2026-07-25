"""
main.py

Command-line entry point for the Publisher backend.

Responsibility:
    Orchestrate the workflow described in the project's architecture:
    load configuration, load and validate a production's metadata,
    build a MediaItem, publish it via the selected publisher, verify
    the result, and move the production folder to published/ or
    failed/ accordingly.

This module is the only place in the backend allowed to know about
*multiple* publishers by name (for the --publisher selection). It
still never reaches into a publisher's internals — it only ever
talks to the Publisher interface and to PublicationResult.

Usage:
    python main.py <production_dir> --publisher peertube [--metadata-file metadata.toml]

Example:
    python main.py workspace/2026-07-21-example-episode --publisher peertube
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from filelock import FileLock, Timeout

from config import AppConfig, ConfigurationError, load_config
from metadata.loader import MetadataLoadError, load_metadata
from metadata.validator import MetadataValidationError, validate_metadata
from models.media_item import MediaItem
from models.publication_result import PublicationResult
from publishers.base import (
    LocalFileDescriptor,
    Publisher,
    PublisherError,
    PublisherOutcomeUnknownError,
    PublisherTemporaryError,
)

logger = logging.getLogger(__name__)


class ProductionMoveError(Exception):
    """Raised when a production cannot be moved to its final local state."""


#: Maps a --publisher value to the publisher's config/class factory.
#: This is the one, intentionally small, seam where a second publisher
#: will be registered later. It is a plain dict, not a plugin
#: framework, by design (see Step 6/8 discussion).
_PUBLISHER_FACTORIES: dict[str, Callable[[], Publisher]] = {}


def _register_default_publishers() -> None:
    """Populate _PUBLISHER_FACTORIES with the currently supported publishers."""
    from publishers.internet_archive import (
        InternetArchiveConfig,
        InternetArchivePublisher,
    )
    from publishers.peertube import PeerTubeConfig, PeerTubePublisher

    def _build_peertube() -> Publisher:
        return PeerTubePublisher(PeerTubeConfig.from_env())

    def _build_internet_archive() -> Publisher:
        return InternetArchivePublisher(InternetArchiveConfig.from_env())

    _PUBLISHER_FACTORIES["peertube"] = _build_peertube
    _PUBLISHER_FACTORIES["internet_archive"] = _build_internet_archive


def _configure_logging(app_config: AppConfig) -> None:
    """Configure root logging to both console and a log file."""
    log_file = app_config.logs_dir / "publisher.log"
    logging.basicConfig(
        level=getattr(logging, app_config.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            RotatingFileHandler(
                log_file,
                maxBytes=10 * 1024 * 1024,
                backupCount=10,
                encoding="utf-8",
            ),
        ],
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Publish a single media production to a publishing platform.",
    )
    parser.add_argument(
        "production_dir",
        type=Path,
        help="Path to the production folder containing the media files and metadata.",
    )
    parser.add_argument(
        "--publisher",
        choices=sorted(_PUBLISHER_FACTORIES.keys()),
        required=True,
        help="Which publisher to publish to (required).",
    )
    parser.add_argument(
        "--metadata-file",
        type=str,
        default="metadata.toml",
        help="Name of the metadata file inside production_dir (default: metadata.toml).",
    )
    return parser.parse_args(argv)


def _move_production(production_dir: Path, destination_root: Path) -> Path:
    """
    Move a production folder into a destination root (published/ or
    failed/), preserving its folder name.

    Args:
        production_dir: The production folder to move.
        destination_root: The root directory to move it into.

    Returns:
        The new path of the moved production folder.
    """
    destination = destination_root / production_dir.name
    if not production_dir.exists():
        raise ProductionMoveError(f"Production directory does not exist: {production_dir}")
    if destination.exists():
        raise ProductionMoveError(
            f"Destination already exists: {destination}. Production remains at {production_dir}."
        )
    try:
        production_dir.rename(destination)
    except OSError as error:
        raise ProductionMoveError(
            f"Could not move {production_dir} to {destination}: {error}. "
            "No copy/delete fallback was attempted."
        ) from error
    return destination


def _write_publication_record(
    production_dir: Path,
    *,
    state: str,
    result: PublicationResult | None = None,
    publisher: str | None = None,
    remote_id: str | None = None,
    message: str | None = None,
    local_primary_file: dict[str, str | int] | None = None,
    local_identifier: str | None = None,
    attempt: int | None = None,
) -> Path:
    """Atomically persist the latest operational state for one production."""
    record_path = production_dir / "publication-result.json"
    temporary_path = record_path.with_suffix(".json.tmp")
    record = {
        "state": state,
        "publisher": result.publisher if result else publisher,
        "remote_id": result.remote_id if result else remote_id,
        "url": result.url if result else None,
        "message": message if message is not None else (result.message if result else None),
        "publisher_timestamp": result.timestamp.isoformat() if result else None,
        "local_identifier": local_identifier,
        "local_primary_file": local_primary_file,
        "attempt": attempt,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    with temporary_path.open("w", encoding="utf-8", newline="\n") as file_handle:
        json.dump(record, file_handle, ensure_ascii=False, indent=2)
        file_handle.write("\n")
        file_handle.flush()
        os.fsync(file_handle.fileno())
    os.replace(temporary_path, record_path)
    return record_path


def _sha256_file(path: Path) -> str:
    """Return a stable content identity for restart-safety checks."""
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_publication_record(production_dir: Path) -> dict[str, object] | None:
    """Read an existing sidecar, failing closed on malformed state."""
    record_path = production_dir / "publication-result.json"
    if not record_path.exists():
        return None
    try:
        value = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not read durable publication state: {error}") from error
    if not isinstance(value, dict):
        raise TypeError("Durable publication state must be a JSON object.")
    return value


def _run_locked(argv: list[str]) -> int:
    """
    Execute the publishing workflow for a single production.

    Args:
        argv: Command-line arguments (excluding the program name).

    Returns:
        Process exit code: 0 on success, 1 on failure.
    """
    _register_default_publishers()
    args = _parse_args(argv)

    app_config = load_config()
    _configure_logging(app_config)

    production_dir: Path = args.production_dir.resolve()
    metadata_path = production_dir / args.metadata_file

    logger.info("Starting publish run for %s", production_dir)

    # --- Load metadata ---
    try:
        raw_metadata = load_metadata(metadata_path)
    except MetadataLoadError as error:
        logger.error("Failed to load metadata: %s", error)
        return 1

    # --- Validate metadata / build MediaItem ---
    try:
        media_item: MediaItem = validate_metadata(raw_metadata, base_dir=production_dir)
    except MetadataValidationError as error:
        logger.error("%s", error)
        return 1

    primary_file = media_item.primary_file().path
    try:
        local_descriptor = LocalFileDescriptor(
            name=primary_file.name,
            size=primary_file.stat().st_size,
            sha256=_sha256_file(primary_file),
        )
        local_primary_file = local_descriptor.to_record()
    except OSError as error:
        logger.error("Could not snapshot primary file '%s': %s", primary_file, error)
        return 1

    try:
        previous_record = _read_publication_record(production_dir)
    except (TypeError, ValueError) as error:
        logger.error("%s", error)
        return 1

    known_states = {
        "publishing",
        "accepted_unverified",
        "uncertain",
        "retryable",
        "failed",
        "verified",
        "verified_move_failed",
        "failed_move_failed",
    }
    if previous_record is not None and previous_record.get("state") not in known_states:
        logger.error(
            "Refusing to replace durable publication state with missing or unknown state: %r",
            previous_record.get("state"),
        )
        return 1

    protected_states = {
        "publishing",
        "uncertain",
        "accepted_unverified",
        "verified",
        "verified_move_failed",
    }
    if previous_record is not None and previous_record.get("state") in protected_states:
        same_identity = (
            previous_record.get("publisher") == args.publisher
            and previous_record.get("local_identifier") == media_item.identifier
            and previous_record.get("local_primary_file") == local_primary_file
        )
        if not same_identity:
            logger.error(
                "Refusing to resume '%s': publisher, identifier, or primary file changed "
                "after a potentially remote write.",
                media_item.identifier,
            )
            return 1

    previous_attempt = previous_record.get("attempt", 0) if previous_record is not None else 0
    attempt = previous_attempt + 1 if isinstance(previous_attempt, int) else 1
    record_context = {
        "local_identifier": media_item.identifier,
        "local_primary_file": local_primary_file,
        "attempt": attempt,
    }

    # --- Build the selected publisher ---
    try:
        publisher = _PUBLISHER_FACTORIES[args.publisher]()
    except ConfigurationError as error:
        logger.error("Publisher configuration error: %s", error)
        return 1

    publisher.prepare(
        media_item,
        local_descriptor,
        reconcile_only=(
            previous_record is not None and previous_record.get("state") in protected_states
        ),
    )

    try:
        _write_publication_record(
            production_dir,
            state="publishing",
            publisher=args.publisher,
            remote_id=(media_item.identifier if args.publisher == "internet_archive" else None),
            message="Remote publication attempt started; reconcile before repeating after a crash.",
            **record_context,
        )
    except OSError as error:
        logger.error("Could not persist pre-upload state: %s", error)
        return 1

    # --- Connect, publish, verify ---
    result: PublicationResult | None = None
    verified = False
    try:
        publisher.connect()
        try:
            result = publisher.publish(media_item)
            if result.success:
                verified = publisher.verify(result)
                if not verified:
                    logger.warning(
                        "Publish reported success for '%s' but verification failed.",
                        media_item.identifier,
                    )
        finally:
            try:
                publisher.disconnect()
            # Cleanup is deliberately best-effort: no ordinary disconnect bug may
            # replace a definitive or uncertain remote publication outcome.
            except Exception as disconnect_error:  # noqa: BLE001
                logger.error(
                    "Publisher disconnect failed for '%s'; preserving the primary "
                    "publish/verification outcome: %s",
                    media_item.identifier,
                    disconnect_error,
                )
    except PublisherOutcomeUnknownError as error:
        logger.error(
            "Publisher outcome is unknown for '%s': %s",
            media_item.identifier,
            error,
        )
        _write_publication_record(
            production_dir,
            **record_context,
            state="uncertain",
            publisher=args.publisher,
            remote_id=error.remote_id,
            message=str(error),
        )
        return 2
    except PublisherTemporaryError as error:
        logger.warning(
            "Temporary publisher failure for '%s': %s",
            media_item.identifier,
            error,
        )
        _write_publication_record(
            production_dir,
            **record_context,
            state="retryable",
            publisher=args.publisher,
            message=str(error),
        )
        return 2
    except PublisherError as error:
        logger.error("Publisher error while publishing '%s': %s", media_item.identifier, error)
        _write_publication_record(
            production_dir,
            **record_context,
            state="failed",
            publisher=args.publisher,
            message=str(error),
        )
        try:
            _move_production(production_dir, app_config.failed_dir)
        except ProductionMoveError as move_error:
            _write_publication_record(
                production_dir,
                **record_context,
                state="failed_move_failed",
                publisher=args.publisher,
                message=str(move_error),
            )
            logger.error("Failed production could not be moved locally: %s", move_error)
        return 1

    # --- Report outcome and move the production folder ---
    if result.success and verified:
        _write_publication_record(
            production_dir,
            state="verified",
            result=result,
            **record_context,
        )
        logger.info(
            "Published '%s' via %s -> %s",
            media_item.identifier,
            result.publisher,
            result.url or "(no public URL yet)",
        )
        try:
            _move_production(production_dir, app_config.published_dir)
        except ProductionMoveError as error:
            _write_publication_record(
                production_dir,
                **record_context,
                state="verified_move_failed",
                result=result,
                message=str(error),
            )
            logger.error("Verified upload could not be finalized locally: %s", error)
            return 1
        return 0

    if result.success:
        _write_publication_record(
            production_dir,
            **record_context,
            state="accepted_unverified",
            result=result,
        )
        logger.warning(
            "Upload for '%s' was accepted but is not yet verified; leaving it in place.",
            media_item.identifier,
        )
        return 2

    _write_publication_record(
        production_dir,
        state="failed",
        result=result,
        **record_context,
    )
    logger.error(
        "Publishing '%s' via %s failed: %s",
        media_item.identifier,
        result.publisher,
        result.message,
    )
    try:
        _move_production(production_dir, app_config.failed_dir)
    except ProductionMoveError as error:
        _write_publication_record(
            production_dir,
            **record_context,
            state="failed_move_failed",
            result=result,
            message=str(error),
        )
        logger.error("Failed production could not be moved locally: %s", error)
    return 1


def run(argv: list[str]) -> int:
    """Acquire the per-production process lock and execute one publish run."""
    _register_default_publishers()
    args = _parse_args(argv)
    production_dir = args.production_dir.resolve()
    lock_path = production_dir.parent / f".{production_dir.name}.publisher.lock"
    lock = FileLock(lock_path)

    try:
        with lock.acquire(timeout=0):
            return _run_locked(argv)
    except Timeout:
        print(
            f"Another publisher process already owns production: {production_dir}",
            file=sys.stderr,
        )
        return 2
    except OSError as error:
        print(f"Could not acquire production lock {lock_path}: {error}", file=sys.stderr)
        return 1


def main() -> None:
    """Entry point for `python main.py ...`."""
    exit_code = run(sys.argv[1:])
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
