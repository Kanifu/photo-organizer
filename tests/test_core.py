from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from organize import (
    cluster_into_events,
    normalize_input_dirs,
    parse_date_from_filename,
    scan_photos,
    validate_source_dirs,
)


def make_image(path: Path, color=(120, 40, 40)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (80, 60), color).save(path, "JPEG")
    return path


def test_parse_date_from_common_filename_patterns():
    assert parse_date_from_filename(Path("20260423_143946_family.jpg")) == datetime(2026, 4, 23, 14, 39, 46)
    assert parse_date_from_filename(Path("IMG_2026-04-23.jpg")) == datetime(2026, 4, 23)
    assert parse_date_from_filename(Path("holiday_20260423.jpg")) == datetime(2026, 4, 23)


def test_cluster_into_events_returns_empty_tuple_for_no_photos():
    events, raw_clusters = cluster_into_events([], gap_hours=4)

    assert events == {}
    assert raw_clusters == []


def test_scan_photos_collects_multiple_sources_and_skips_screenshots(tmp_path):
    source_a = tmp_path / "camera"
    source_b = tmp_path / "phone"
    make_image(source_a / "20260601_100000_a.jpg")
    make_image(source_b / "20260601_110000_b.jpg")
    make_image(source_b / "Screenshot_20260601.jpg")
    (source_b / "notes.txt").write_text("not a photo")

    photos, skipped = scan_photos([source_a, source_b], skip_screenshots=True)

    assert [photo.name for photo in photos] == [
        "20260601_100000_a.jpg",
        "20260601_110000_b.jpg",
    ]
    assert skipped == 1


def test_validate_source_dirs_rejects_output_inside_input(tmp_path):
    source = tmp_path / "photos"
    output = source / "album-output"
    source.mkdir()

    with pytest.raises(ValueError, match="outside every input folder"):
        validate_source_dirs([source], output)


def test_normalize_input_dirs_deduplicates_paths(tmp_path):
    source = tmp_path / "photos"
    source.mkdir()

    assert normalize_input_dirs([source, source]) == [source.resolve()]


def test_normalize_input_dirs_ignores_empty_values_and_requires_one_input(tmp_path):
    source = tmp_path / "photos"
    source.mkdir()

    assert normalize_input_dirs([None, "", source]) == [source.resolve()]

    with pytest.raises(ValueError, match="At least one input folder is required."):
        normalize_input_dirs([None, ""])
