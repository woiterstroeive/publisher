from __future__ import annotations

from pathlib import Path

import pytest

from metadata.validator import MetadataValidationError, validate_metadata


def _valid_metadata() -> dict[str, object]:
    return {
        "identifier": "video-001",
        "title": "A valid title",
        "description": "Description",
        "creator": "Wouter Stroeve",
        "tags": ["archive", "video"],
        "files": [{"path": "video.mkv", "role": "primary"}],
    }


def _touch(base_dir: Path, name: str = "video.mkv") -> Path:
    path = base_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"media")
    return path


def test_rejects_whitespace_only_required_text(tmp_path: Path) -> None:
    _touch(tmp_path)
    raw = _valid_metadata()
    raw["identifier"] = "   "
    raw["title"] = "\t"

    with pytest.raises(MetadataValidationError) as caught:
        validate_metadata(raw, base_dir=tmp_path)

    assert "Field 'identifier' must not be blank" in caught.value.errors
    assert "Field 'title' must not be blank" in caught.value.errors


def test_rejects_blank_required_description(tmp_path: Path) -> None:
    _touch(tmp_path)
    raw = _valid_metadata()
    raw["description"] = "  \t  "

    with pytest.raises(MetadataValidationError) as caught:
        validate_metadata(raw, base_dir=tmp_path)

    assert "Field 'description' must not be blank" in caught.value.errors


def test_rejects_zero_byte_primary_file(tmp_path: Path) -> None:
    (tmp_path / "video.mkv").write_bytes(b"")

    with pytest.raises(MetadataValidationError) as caught:
        validate_metadata(_valid_metadata(), base_dir=tmp_path)

    assert any("primary file is empty" in error for error in caught.value.errors)


def test_rejects_directory_as_media_file(tmp_path: Path) -> None:
    (tmp_path / "video.mkv").mkdir()

    with pytest.raises(MetadataValidationError) as caught:
        validate_metadata(_valid_metadata(), base_dir=tmp_path)

    assert any("is not a file" in error for error in caught.value.errors)


def test_requires_exactly_one_primary_file(tmp_path: Path) -> None:
    _touch(tmp_path, "one.mkv")
    _touch(tmp_path, "two.mkv")
    raw = _valid_metadata()
    raw["files"] = [
        {"path": "one.mkv", "role": "primary"},
        {"path": "two.mkv", "role": "primary"},
    ]

    with pytest.raises(MetadataValidationError) as caught:
        validate_metadata(raw, base_dir=tmp_path)

    assert "Exactly one entry in 'files' must have role 'primary'; found 2" in caught.value.errors


def test_rejects_media_path_outside_production_directory(tmp_path: Path) -> None:
    production = tmp_path / "production"
    production.mkdir()
    outside = _touch(tmp_path, "outside.mkv")
    raw = _valid_metadata()
    raw["files"] = [{"path": str(outside), "role": "primary"}]

    with pytest.raises(MetadataValidationError) as caught:
        validate_metadata(raw, base_dir=production)

    assert any("outside production directory" in error for error in caught.value.errors)


def test_normalizes_required_text_and_tags(tmp_path: Path) -> None:
    _touch(tmp_path)
    raw = _valid_metadata()
    raw["identifier"] = "  video-001  "
    raw["title"] = "  A valid title  "
    raw["creator"] = "  Wouter Stroeve  "
    raw["tags"] = [" archive ", "", "video", "archive"]

    item = validate_metadata(raw, base_dir=tmp_path)

    assert item.identifier == "video-001"
    assert item.title == "A valid title"
    assert item.creator == "Wouter Stroeve"
    assert item.tags == ("archive", "video")
