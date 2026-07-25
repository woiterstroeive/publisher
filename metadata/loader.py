"""
metadata/loader.py

Loads raw media metadata from disk into plain Python data structures.

Responsibility:
    Read a metadata file (e.g. metadata.toml) belonging to a media
    production and parse it into a plain dict.

This module does NOT validate metadata content and does NOT construct
MediaItem instances. Validation is the responsibility of
metadata/validator.py; constructing MediaItem from validated data is
the responsibility of whatever orchestrates the workflow (main.py).
Keeping these concerns separate means a future change to the file
format only touches this module.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class MetadataLoadError(Exception):
    """Raised when a metadata file cannot be found, read, or parsed."""


def load_metadata(metadata_path: Path) -> dict[str, Any]:
    """
    Load and parse a metadata file into a plain dict.

    Args:
        metadata_path: Path to the metadata file (expected to be TOML).

    Returns:
        The parsed metadata as a dict. Keys and structure are not
        validated here — see metadata/validator.py.

    Raises:
        MetadataLoadError: If the file does not exist, cannot be read,
            or cannot be parsed as TOML.
    """
    if not metadata_path.exists():
        raise MetadataLoadError(f"Metadata file not found: {metadata_path}")

    if not metadata_path.is_file():
        raise MetadataLoadError(f"Metadata path is not a file: {metadata_path}")

    try:
        raw_bytes = metadata_path.read_bytes()
    except OSError as error:
        raise MetadataLoadError(f"Could not read metadata file: {metadata_path}") from error

    try:
        parsed = tomllib.loads(raw_bytes.decode("utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise MetadataLoadError(
            f"Could not parse metadata file as TOML: {metadata_path}"
        ) from error
    except UnicodeDecodeError as error:
        raise MetadataLoadError(f"Metadata file is not valid UTF-8: {metadata_path}") from error

    logger.debug("Loaded metadata from %s", metadata_path)
    return parsed
