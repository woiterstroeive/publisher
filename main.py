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
    python main.py <production_dir> [--publisher peertube] [--metadata-file metadata.toml]

Example:
    python main.py workspace/2026-07-21-example-episode --publisher peertube
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from config import AppConfig, ConfigurationError, load_config
from metadata.loader import MetadataLoadError, load_metadata
from metadata.validator import MetadataValidationError, validate_metadata
from models.media_item import MediaItem
from models.publication_result import PublicationResult
from publishers.base import Publisher, PublisherError

logger = logging.getLogger(__name__)

#: Maps a --publisher value to the publisher's config/class factory.
#: This is the one, intentionally small, seam where a second publisher
#: will be registered later. It is a plain dict, not a plugin
#: framework, by design (see Step 6/8 discussion).
_PUBLISHER_FACTORIES: dict[str, "callable[[], Publisher]"] = {}


def _register_default_publishers() -> None:
    """Populate _PUBLISHER_FACTORIES with the currently supported publishers."""
    from publishers.peertube import PeerTubeConfig, PeerTubePublisher

    def _build_peertube() -> Publisher:
        return PeerTubePublisher(PeerTubeConfig.from_env())

    _PUBLISHER_FACTORIES["peertube"] = _build_peertube


def _configure_logging(app_config: AppConfig) -> None:
    """Configure root logging to both console and a log file."""
    log_file = app_config.logs_dir / "publisher.log"
    logging.basicConfig(
        level=getattr(logging, app_config.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
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
        default="peertube",
        help="Which publisher to publish to (default: peertube).",
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
    if destination.exists():
        logger.warning(
            "Destination %s already exists; leaving %s in place instead of moving.",
            destination,
            production_dir,
        )
        return production_dir
    shutil.move(str(production_dir), str(destination))
    return destination


def run(argv: list[str]) -> int:
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

    # --- Build the selected publisher ---
    try:
        publisher = _PUBLISHER_FACTORIES[args.publisher]()
    except ConfigurationError as error:
        logger.error("Publisher configuration error: %s", error)
        return 1

    # --- Connect, publish, verify ---
    result: PublicationResult | None = None
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
            publisher.disconnect()
    except PublisherError as error:
        logger.error("Publisher error while publishing '%s': %s", media_item.identifier, error)
        _move_production(production_dir, app_config.failed_dir)
        return 1

    # --- Report outcome and move the production folder ---
    if result.success:
        logger.info(
            "Published '%s' via %s -> %s",
            media_item.identifier,
            result.publisher,
            result.url or "(no public URL yet)",
        )
        _move_production(production_dir, app_config.published_dir)
        return 0

    logger.error(
        "Publishing '%s' via %s failed: %s",
        media_item.identifier,
        result.publisher,
        result.message,
    )
    _move_production(production_dir, app_config.failed_dir)
    return 1


def main() -> None:
    """Entry point for `python main.py ...`."""
    exit_code = run(sys.argv[1:])
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
