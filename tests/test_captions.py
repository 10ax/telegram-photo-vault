from datetime import datetime

from PIL import Image

from app.services.telegram import (
    _extract_datetime_with_source_sync,
    _format_caption,
    _parse_filename_datetime,
)


def test_filename_datetime_full():
    assert _parse_filename_datetime("IMG_20240612_193000.jpg") == datetime(2024, 6, 12, 19, 30, 0)
    assert _parse_filename_datetime("2024-06-12 19.30.00.jpg") == datetime(2024, 6, 12, 19, 30, 0)


def test_filename_datetime_date_only():
    assert _parse_filename_datetime("VID-20230101-WA0001.mp4") == datetime(2023, 1, 1)


def test_filename_datetime_rejects_non_dates():
    assert _parse_filename_datetime("IMG_1234.jpg") is None
    assert _parse_filename_datetime("P1080123.jpg") is None
    # invalid month
    assert _parse_filename_datetime("20241399.jpg") is None


def test_caption_format():
    assert _format_caption(datetime(2024, 6, 1)) == "#2024 #06_2024 #2024_06_01"


def test_source_priority_exif(tmp_path):
    path = tmp_path / "IMG_20200101_000000.jpg"  # filename date present but EXIF must win
    exif = Image.Exif()
    exif[306] = "2020:05:06 07:08:09"
    Image.new("RGB", (2, 2)).save(path, format="JPEG", exif=exif)

    value, source = _extract_datetime_with_source_sync(path)
    assert (value, source) == (datetime(2020, 5, 6, 7, 8, 9), "exif")


def test_source_priority_filename(tmp_path):
    path = tmp_path / "IMG_20240612_193000.jpg"
    Image.new("RGB", (2, 2)).save(path, format="JPEG")

    value, source = _extract_datetime_with_source_sync(path)
    assert (value, source) == (datetime(2024, 6, 12, 19, 30, 0), "filename")


def test_source_priority_fallback_then_mtime(tmp_path):
    path = tmp_path / "clip.bin"
    path.write_bytes(b"x")

    fallback = datetime(2019, 2, 3, 4, 5, 6)
    assert _extract_datetime_with_source_sync(path, fallback) == (fallback, "fallback")

    value, source = _extract_datetime_with_source_sync(path)
    assert source == "mtime"
    assert value == datetime.fromtimestamp(path.stat().st_mtime)
