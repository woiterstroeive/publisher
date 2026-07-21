"""
metadata/validator.py

Validates raw metadata dicts and converts them into MediaItem
instances.

Responsibility:
    Enforce the business rules that define a valid media production
    (required fields, correct types, sane defaults) and construct a
    MediaItem from data that satisfies them.

This module does NOT read files from disk (see metadata/loader.py)
and does NOT define MediaItem's shape (see models/media_item.py). It
sits between the two: raw dict in, validated MediaItem out.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

from models.media_item import MediaFile, MediaItem, Subtitle

logger = logging.getLogger(__name__)

REQUIRED_FIELDS: tuple[str, ...] = (
    "identifier",
    "title",
    "description",
    "creator",
    "files",
)


class MetadataValidationError(Exception):
    """
    Raised when metadata fails validation.

    Collects all validation problems found, rather than only the
    first one, so a person fixing a metadata file can address every
    issue in a single pass.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        message = "Metadata validation failed:\n" + "\n".join(
            f"  - {error}" for error in errors
        )
        super().__init__(message)


def validate_metadata(
    raw_metadata: dict[str, Any],
    *,
    base_dir: Path,
) -> MediaItem:
    """
    Validate a raw metadata dict and construct a MediaItem from it.

    Args:
        raw_metadata: Parsed metadata, as produced by
            metadata.loader.load_metadata().
        base_dir: Directory that relative file paths in the metadata
            (files, thumbnail, subtitles) are resolved against —
            typically the production's own folder.

    Returns:
        A validated MediaItem instance.

    Raises:
        MetadataValidationError: If one or more validation rules are
            violated. All violations are reported together.
    """
    errors: list[str] = []

    _check_required_fields(raw_metadata, errors)

    identifier = _validate_non_empty_string(raw_metadata, "identifier", errors)
    title = _validate_non_empty_string(raw_metadata, "title", errors)
    description = _validate_string(raw_metadata, "description", errors)
    creator = _validate_non_empty_string(raw_metadata, "creator", errors)
    tags = _validate_string_list(raw_metadata, "tags", errors, required=False)
    files = _validate_files(raw_metadata, base_dir, errors)
    thumbnail = _validate_optional_path(raw_metadata, "thumbnail", base_dir, errors)
    subtitles = _validate_subtitles(raw_metadata, base_dir, errors)
    publication_date = _validate_optional_datetime(
        raw_metadata, "publication_date", errors
    )

    if errors:
        raise MetadataValidationError(errors)

    return MediaItem(
        identifier=identifier,
        title=title,
        description=description,
        creator=creator,
        files=tuple(files),
        tags=tuple(tags),
        thumbnail=thumbnail,
        subtitles=tuple(subtitles),
        publication_date=publication_date,
    )


def _check_required_fields(raw_metadata: dict[str, Any], errors: list[str]) -> None:
    for field_name in REQUIRED_FIELDS:
        if field_name not in raw_metadata:
            errors.append(f"Missing required field: '{field_name}'")


def _validate_string(
    raw_metadata: dict[str, Any], key: str, errors: list[str]
) -> str:
    value = raw_metadata.get(key, "")
    if not isinstance(value, str):
        errors.append(f"Field '{key}' must be a string, got {type(value).__name__}")
        return ""
    return value


def _validate_non_empty_string(
    raw_metadata: dict[str, Any], key: str, errors: list[str]
) -> str:
    value = _validate_string(raw_metadata, key, errors)
    if value == "" and key in raw_metadata:
        errors.append(f"Field '{key}' must not be empty")
    return value


def _validate_string_list(
    raw_metadata: dict[str, Any],
    key: str,
    errors: list[str],
    *,
    required: bool,
) -> list[str]:
    if key not in raw_metadata:
        if required:
            errors.append(f"Missing required field: '{key}'")
        return []

    value = raw_metadata[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        errors.append(f"Field '{key}' must be a list of strings")
        return []
    return value


def _resolve_path(raw_path: str, base_dir: Path) -> Path:
    path = Path(raw_path)
    return path if path.is_absolute() else base_dir / path


def _validate_files(
    raw_metadata: dict[str, Any], base_dir: Path, errors: list[str]
) -> list[MediaFile]:
    raw_files = raw_metadata.get("files")

    if raw_files is None:
        return []

    if not isinstance(raw_files, list) or len(raw_files) == 0:
        errors.append("Field 'files' must be a non-empty list of file entries")
        return []

    media_files: list[MediaFile] = []
    has_primary = False

    for index, entry in enumerate(raw_files):
        if not isinstance(entry, dict) or "path" not in entry:
            errors.append(
                f"files[{index}] must be a table with at least a 'path' key"
            )
            continue

        raw_path = entry["path"]
        if not isinstance(raw_path, str) or not raw_path:
            errors.append(f"files[{index}].path must be a non-empty string")
            continue

        role = entry.get("role", "primary")
        if not isinstance(role, str) or not role:
            errors.append(f"files[{index}].role must be a non-empty string")
            continue

        resolved_path = _resolve_path(raw_path, base_dir)
        if not resolved_path.exists():
            errors.append(f"files[{index}].path does not exist: {resolved_path}")
            continue

        if role == "primary":
            has_primary = True

        media_files.append(MediaFile(path=resolved_path, role=role))

    if media_files and not has_primary:
        errors.append("At least one entry in 'files' must have role 'primary'")

    return media_files


def _validate_subtitles(
    raw_metadata: dict[str, Any], base_dir: Path, errors: list[str]
) -> list[Subtitle]:
    raw_subtitles = raw_metadata.get("subtitles")

    if raw_subtitles is None:
        return []

    if not isinstance(raw_subtitles, list):
        errors.append("Field 'subtitles' must be a list of subtitle entries")
        return []

    subtitles: list[Subtitle] = []

    for index, entry in enumerate(raw_subtitles):
        if not isinstance(entry, dict) or "path" not in entry or "language" not in entry:
            errors.append(
                f"subtitles[{index}] must be a table with 'path' and 'language' keys"
            )
            continue

        raw_path = entry["path"]
        language = entry["language"]

        if not isinstance(raw_path, str) or not raw_path:
            errors.append(f"subtitles[{index}].path must be a non-empty string")
            continue

        if not isinstance(language, str) or not language:
            errors.append(f"subtitles[{index}].language must be a non-empty string")
            continue

        resolved_path = _resolve_path(raw_path, base_dir)
        if not resolved_path.exists():
            errors.append(f"subtitles[{index}].path does not exist: {resolved_path}")
            continue

        subtitles.append(Subtitle(path=resolved_path, language=language))

    return subtitles


def _validate_optional_path(
    raw_metadata: dict[str, Any],
    key: str,
    base_dir: Path,
    errors: list[str],
) -> Path | None:
    if key not in raw_metadata:
        return None

    value = raw_metadata[key]
    if not isinstance(value, str) or not value:
        errors.append(f"Field '{key}' must be a non-empty string if provided")
        return None

    resolved_path = _resolve_path(value, base_dir)
    if not resolved_path.exists():
        errors.append(f"Field '{key}' points to a file that does not exist: {resolved_path}")
        return None

    return resolved_path


def _validate_optional_datetime(
    raw_metadata: dict[str, Any], key: str, errors: list[str]
) -> datetime | None:
    if key not in raw_metadata:
        return None

    value = raw_metadata[key]

    if isinstance(value, datetime):
        return value

    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)

    errors.append(
        f"Field '{key}' must be a TOML date or datetime value, "
        f"got {type(value).__name__}"
    )
    return None
