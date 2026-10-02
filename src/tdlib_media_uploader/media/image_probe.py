"""Unified image media probe and Telegram Photo compatibility evaluation.

Provides dimension, aspect ratio, file size, EXIF orientation and animation
checks for Telegram Photo uploads.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

TELEGRAM_PHOTO_MAX_BYTES = 10 * 1024**2
TELEGRAM_PHOTO_TARGET_BYTES = int(9.5 * 1024**2)

TELEGRAM_PHOTO_MAX_DIMENSION_SUM = 10000
TELEGRAM_PHOTO_MAX_ASPECT_RATIO = 20.0

PHOTO_TARGET_MAX_SIDE = 2560


@dataclass(frozen=True)
class ImageMediaInfo(Mapping[str, Any]):
    """Unified metadata and transformation indicators for image files."""

    format: str
    width: int
    height: int
    file_size: int
    animated: bool
    frame_count: int
    has_alpha: bool
    needs_resize: bool
    needs_transcode: bool
    needs_compression: bool
    needs_aspect_fix: bool
    telegram_compatible: bool

    @property
    def needs_normalization(self) -> bool:
        return not self.telegram_compatible

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)

    def __iter__(self):
        return iter(
            (
                "format",
                "width",
                "height",
                "file_size",
                "animated",
                "frame_count",
                "has_alpha",
                "needs_resize",
                "needs_transcode",
                "needs_compression",
                "needs_aspect_fix",
                "telegram_compatible",
            )
        )

    def __len__(self) -> int:
        return 12


def probe_image(path: Path | str) -> ImageMediaInfo:
    """Probe image file with Pillow, respecting EXIF orientation."""
    file_path = Path(path)
    if not file_path.is_file():
        raise RuntimeError(f"图片文件不存在：{file_path}")

    stat = file_path.stat()
    file_size = stat.st_size
    if file_size <= 0:
        raise RuntimeError(f"图片文件为空：{file_path.name}")

    try:
        with Image.open(file_path) as raw_img:
            fmt = str(raw_img.format or "").upper()
            frame_count = getattr(raw_img, "n_frames", 1)
            is_animated = bool(getattr(raw_img, "is_animated", False) or frame_count > 1)
            has_alpha = (
                raw_img.mode in ("RGBA", "LA", "PA")
                or (raw_img.mode == "P" and "transparency" in raw_img.info)
            )

            # Apply EXIF transpose to obtain real post-orientation geometry
            transposed = ImageOps.exif_transpose(raw_img)
            width, height = transposed.size

            if width <= 0 or height <= 0:
                raise RuntimeError(f"图片尺寸异常：{file_path.name}")

            exif = raw_img.getexif() if hasattr(raw_img, "getexif") else None
            orientation = exif.get(0x0112) if exif else 1
            exif_rotated = orientation not in (None, 1)
    except Exception as exc:
        raise RuntimeError(f"无法读取图片：{file_path.name}\n{type(exc).__name__}: {exc}") from exc

    aspect_ratio = (
        max(width / height, height / width) if width > 0 and height > 0 else 0.0
    )
    needs_aspect_fix = aspect_ratio > TELEGRAM_PHOTO_MAX_ASPECT_RATIO
    needs_resize = (
        max(width, height) > PHOTO_TARGET_MAX_SIDE
        or (width + height) > TELEGRAM_PHOTO_MAX_DIMENSION_SUM
    )
    needs_compression = file_size > TELEGRAM_PHOTO_TARGET_BYTES

    if fmt in ("BMP", "TIFF", "WEBP"):
        needs_transcode = True
    elif fmt == "PNG":
        # PNG remains PNG if within size and geometry limits
        needs_transcode = False
    elif fmt == "JPEG":
        needs_transcode = False
    else:
        needs_transcode = True

    telegram_compatible = (
        fmt in ("JPEG", "PNG")
        and not is_animated
        and not needs_resize
        and not needs_compression
        and not needs_aspect_fix
        and not needs_transcode
        and not exif_rotated
    )

    return ImageMediaInfo(
        format=fmt,
        width=width,
        height=height,
        file_size=file_size,
        animated=is_animated,
        frame_count=frame_count,
        has_alpha=has_alpha,
        needs_resize=needs_resize,
        needs_transcode=needs_transcode,
        needs_compression=needs_compression,
        needs_aspect_fix=needs_aspect_fix,
        telegram_compatible=telegram_compatible,
    )
