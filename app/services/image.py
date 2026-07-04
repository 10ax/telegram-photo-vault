from __future__ import annotations

import asyncio
from pathlib import Path

from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

register_heif_opener()

MAX_LONG_SIDE = 1920
WEBP_QUALITY = 80


def _compress_to_webp_sync(input_path: str | Path, output_path: str | Path) -> None:
    source = Path(input_path)
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(source) as image:
        normalized = ImageOps.exif_transpose(image)
        width, height = normalized.size
        long_side = max(width, height)

        if long_side > MAX_LONG_SIDE:
            scale = MAX_LONG_SIDE / long_side
            new_size = (int(width * scale), int(height * scale))
            normalized = normalized.resize(new_size, Image.Resampling.LANCZOS)

        if normalized.mode in ("RGBA", "P"):
            normalized = normalized.convert("RGB")

        normalized.save(target, format="WEBP", quality=WEBP_QUALITY)


async def compress_to_webp(input_path: str | Path, output_path: str | Path) -> None:
    await asyncio.to_thread(_compress_to_webp_sync, input_path, output_path)
