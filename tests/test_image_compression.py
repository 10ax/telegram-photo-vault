"""Characterisation tests for the WebP mirror copy sent to the Odroid.

This is the only lossy step in the pipeline, and it never touches the archived
original — it writes a separate file under WORKER_COMPRESSED_ROOT.
"""
import pytest
from PIL import Image, ImageOps, UnidentifiedImageError

from app.services.image import MAX_LONG_SIDE, compress_to_webp


def _write(path, size, mode="RGB", color=(200, 60, 40), **save_kwargs):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new(mode, size, color).save(path, **save_kwargs)
    return path


async def test_a_large_image_is_scaled_down_to_the_long_side_cap(tmp_path):
    source = _write(tmp_path / "big.png", (4000, 1000))
    target = tmp_path / "out" / "big.webp"

    await compress_to_webp(source, target)

    with Image.open(target) as result:
        assert result.format == "WEBP"
        assert max(result.size) == MAX_LONG_SIDE
        assert result.size == (MAX_LONG_SIDE, 480)  # aspect ratio preserved


async def test_a_portrait_image_is_capped_on_its_own_long_side(tmp_path):
    source = _write(tmp_path / "tall.png", (1000, 4000))
    target = tmp_path / "tall.webp"

    await compress_to_webp(source, target)

    with Image.open(target) as result:
        assert result.size == (480, MAX_LONG_SIDE)


async def test_a_small_image_is_never_upscaled(tmp_path):
    source = _write(tmp_path / "small.png", (320, 240))
    target = tmp_path / "small.webp"

    await compress_to_webp(source, target)

    with Image.open(target) as result:
        assert result.size == (320, 240)


async def test_an_image_exactly_at_the_cap_is_left_alone(tmp_path):
    source = _write(tmp_path / "exact.png", (MAX_LONG_SIDE, 100))
    target = tmp_path / "exact.webp"

    await compress_to_webp(source, target)

    with Image.open(target) as result:
        assert result.size == (MAX_LONG_SIDE, 100)


@pytest.mark.parametrize("mode", ["RGBA", "P"])
async def test_modes_webp_cannot_take_directly_are_converted(tmp_path, mode):
    source = _write(tmp_path / f"{mode}.png", (100, 80), mode=mode)
    target = tmp_path / f"{mode}.webp"

    await compress_to_webp(source, target)

    with Image.open(target) as result:
        assert result.format == "WEBP"
        assert result.size == (100, 80)


async def test_the_output_directory_is_created_on_demand(tmp_path):
    source = _write(tmp_path / "a.png", (10, 10))
    target = tmp_path / "deep" / "nested" / "a.webp"

    await compress_to_webp(source, target)

    assert target.is_file()


async def test_an_exif_rotation_is_baked_into_the_pixels(tmp_path):
    # Orientation 6 = rotate 90°: a 200x100 source must come out 100x200, because
    # WebP viewers should not have to re-apply the tag.
    source = tmp_path / "rotated.jpg"
    exif = Image.Exif()
    exif[274] = 6  # Orientation
    Image.new("RGB", (200, 100), (10, 90, 200)).save(source, format="JPEG", exif=exif)
    with Image.open(source) as probe:
        assert ImageOps.exif_transpose(probe).size == (100, 200)
    target = tmp_path / "rotated.webp"

    await compress_to_webp(source, target)

    with Image.open(target) as result:
        assert result.size == (100, 200)


async def test_a_file_that_is_not_an_image_raises(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("this is not a picture")

    # The worker turns this into a retry + traceback in error_log, not a crash.
    with pytest.raises(UnidentifiedImageError):
        await compress_to_webp(source, tmp_path / "notes.webp")
