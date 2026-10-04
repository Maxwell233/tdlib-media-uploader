"""Unified image preparation and normalization pipeline for Telegram Photo.

Implements EXIF transpose, alpha compositing, extreme aspect ratio padding,
proportional downscaling, and bounded quality search to produce Telegram Photo
compatible images saved strictly to the cache directory without modifying source files.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import math
import os
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from ..core.source_snapshot import capture_snapshot, validate_snapshots
from ..config.paths import IMAGE_COMPRESSION_CACHE_DIR
from ..core.filesystem import stable_path
from .image_probe import (
    PHOTO_TARGET_MAX_SIDE,
    TELEGRAM_PHOTO_MAX_ASPECT_RATIO,
    TELEGRAM_PHOTO_MAX_DIMENSION_SUM,
    TELEGRAM_PHOTO_TARGET_BYTES,
    ImageMediaInfo,
    probe_image,
)


@dataclass(frozen=True)
class PreparedImage:
    """Result of image preparation containing source, upload path and geometry."""

    source_path: Path
    upload_path: Path
    source_info: ImageMediaInfo
    output_format: str
    output_width: int
    output_height: int
    output_size: int
    transformed: bool


def parse_hex_color(color_str: str) -> tuple[int, int, int]:
    """Parse hex color string like #FFFFFF or #FFF into an RGB tuple."""
    raw = color_str.strip().lstrip("#")
    if len(raw) == 6:
        try:
            return (
                int(raw[0:2], 16),
                int(raw[2:4], 16),
                int(raw[4:6], 16),
            )
        except ValueError:
            pass
    elif len(raw) == 3:
        try:
            return (
                int(raw[0] * 2, 16),
                int(raw[1] * 2, 16),
                int(raw[2] * 2, 16),
            )
        except ValueError:
            pass
    return (255, 255, 255)


def get_image_cache_key(
    source_sig: str,
    policy_version: str = "image-v2",
    target_format: str = "jpeg",
    max_side: int = PHOTO_TARGET_MAX_SIDE,
    target_bytes: int = TELEGRAM_PHOTO_TARGET_BYTES,
    aspect_policy: str = "pad",
    background: str = "#FFFFFF",
) -> str:
    """Build a deterministic cache key digest incorporating all transform parameters."""
    normalized_bg = background.strip().upper() if background else "#FFFFFF"
    key_text = (
        f"{source_sig}|{policy_version}|{target_format.lower()}|"
        f"max_side={max_side}|target={target_bytes}|"
        f"aspect={aspect_policy}|background={normalized_bg}"
    )
    return hashlib.sha256(key_text.encode("utf-8")).hexdigest()


def encode_jpeg_under_limit(
    img: Image.Image,
    target_bytes: int = TELEGRAM_PHOTO_TARGET_BYTES,
    cancel_event=None,
) -> tuple[bytes, int, int]:
    """Encode image to JPEG within target_bytes using bounded binary quality search."""
    def _is_cancelled():
        return cancel_event is not None and cancel_event.is_set()

    if _is_cancelled():
        raise TimeoutError("图片编码处理已取消")

    # Step 1: Try high quality (~92)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92, optimize=True)
    data = buf.getvalue()
    if len(data) <= target_bytes:
        return data, img.width, img.height

    if _is_cancelled():
        raise TimeoutError("图片编码处理已取消")

    # Step 2: Binary search on quality between [30, 85]
    low, high = 30, 85
    best_data = None
    for _ in range(5):
        if _is_cancelled():
            raise TimeoutError("图片编码处理已取消")
        mid = (low + high) // 2
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=mid, optimize=True)
        data = buf.getvalue()
        if len(data) <= target_bytes:
            best_data = data
            low = mid + 1
        else:
            high = mid - 1

    if best_data is not None:
        return best_data, img.width, img.height

    # Step 3: If still over target, proportionally downscale resolution and search quality
    current_img = img
    for scale in (0.8, 0.6, 0.4, 0.25):
        if _is_cancelled():
            raise TimeoutError("图片编码处理已取消")
        new_w = max(1, int(round(current_img.width * scale)))
        new_h = max(1, int(round(current_img.height * scale)))
        resized = current_img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        for q in (80, 60, 40, 20):
            if _is_cancelled():
                raise TimeoutError("图片编码处理已取消")
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=q, optimize=True)
            data = buf.getvalue()
            if len(data) <= target_bytes:
                return data, resized.width, resized.height

    raise RuntimeError(
        f"无法将图片规范化至目标大小（{target_bytes} 字节）以内。"
    )


def prepare_image_for_telegram(
    path: Path | str,
    cancel_event=None,
    *,
    source_info: ImageMediaInfo | None = None,
    max_side: int = PHOTO_TARGET_MAX_SIDE,
    target_bytes: int = TELEGRAM_PHOTO_TARGET_BYTES,
    aspect_policy: str = "pad",
    background: str = "#FFFFFF",
    extreme_aspect_policy: str | None = None,
    transparency_background: str | None = None,
) -> PreparedImage:
    """Prepare an image for Telegram Photo upload.

    Returns the original file unchanged when already compliant.
    When transformation is required, produces a cached normalized JPEG.
    Never modifies source files.
    """
    if cancel_event is not None and cancel_event.is_set():
        raise TimeoutError("图片准备已取消")

    if extreme_aspect_policy is not None:
        aspect_policy = extreme_aspect_policy
    if transparency_background is not None:
        background = transparency_background
    source_path = Path(path).resolve()
    source_snapshot = capture_snapshot(source_path)
    if source_info is None:
        source_info = probe_image(source_path)
    if source_info.source_snapshot is not None:
        validate_snapshots(((source_path, *source_info.source_snapshot),))
    validate_snapshots((source_snapshot,))

    if cancel_event is not None and cancel_event.is_set():
        raise TimeoutError("图片准备已取消")

    if source_info.animated:
        raise RuntimeError("检测到动画图片，不会自动转换为静态 Photo。")

    # If completely compatible with Telegram Photo standards, use source directly
    if source_info.telegram_compatible:
        return PreparedImage(
            source_path=source_path,
            upload_path=source_path,
            source_info=source_info,
            output_format=source_info.format,
            output_width=source_info.width,
            output_height=source_info.height,
            output_size=source_info.file_size,
            transformed=False,
        )

    # Check extreme aspect ratio skip policy
    ratio = (
        max(source_info.width / source_info.height, source_info.height / source_info.width)
        if source_info.width > 0 and source_info.height > 0
        else 1.0
    )
    if ratio > TELEGRAM_PHOTO_MAX_ASPECT_RATIO and aspect_policy == "skip":
        raise RuntimeError(
            f"图片长宽比（{ratio:.2f}）超过限制 {TELEGRAM_PHOTO_MAX_ASPECT_RATIO}，且配置为跳过：{source_path.name}"
        )

    # Check cache
    source_sig = f"{stable_path(source_path)}|{source_snapshot[1]}|{source_snapshot[2]}"
    cache_key = get_image_cache_key(
        source_sig=source_sig,
        policy_version="image-v2",
        target_format="jpeg",
        max_side=max_side,
        target_bytes=target_bytes,
        aspect_policy=aspect_policy,
        background=background,
    )
    cache_dir = IMAGE_COMPRESSION_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{cache_key}.jpg"

    if cache_path.is_file():
        cache_stat = cache_path.stat()
        if 0 < cache_stat.st_size <= target_bytes:
            try:
                with Image.open(cache_path) as cached_img:
                    cached_img.load()
                    w, h = cached_img.size
                    if (
                        cached_img.format != "JPEG"
                        or min(w, h) <= 0
                        or max(w, h) > max_side
                        or w + h > TELEGRAM_PHOTO_MAX_DIMENSION_SUM
                        or max(w / h, h / w) > TELEGRAM_PHOTO_MAX_ASPECT_RATIO
                    ):
                        raise ValueError("缓存图片不符合当前尺寸或格式限制")
                if cancel_event is not None and cancel_event.is_set():
                    raise TimeoutError("图片准备已取消")
                return PreparedImage(
                    source_path=source_path,
                    upload_path=cache_path,
                    source_info=source_info,
                    output_format="JPEG",
                    output_width=w,
                    output_height=h,
                    output_size=cache_stat.st_size,
                    transformed=True,
                )
            except TimeoutError:
                raise
            except Exception:
                cache_path.unlink(missing_ok=True)

    # Transform image
    if cancel_event is not None and cancel_event.is_set():
        raise TimeoutError("图片准备已取消")

    with Image.open(source_path) as raw_img:
        # Step 1: EXIF transpose (Orientation)
        img = ImageOps.exif_transpose(raw_img)

        # Step 2: Handle alpha compositing
        has_alpha = (
            img.mode in ("RGBA", "LA", "PA")
            or (img.mode == "P" and "transparency" in img.info)
        )
        bg_rgb = parse_hex_color(background)
        if has_alpha:
            rgba = img.convert("RGBA")
            bg_canvas = Image.new("RGBA", rgba.size, (*bg_rgb, 255))
            img = Image.alpha_composite(bg_canvas, rgba).convert("RGB")
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # Step 3: Geometry normalization
        w, h = img.size
        curr_ratio = max(w / h, h / w) if w > 0 and h > 0 else 1.0

        if curr_ratio > TELEGRAM_PHOTO_MAX_ASPECT_RATIO:
            # First scale proportionally so max side <= max_side
            if max(w, h) > max_side:
                scale = max_side / max(w, h)
                w = max(1, int(round(w * scale)))
                h = max(1, int(round(h * scale)))
                img = img.resize((w, h), Image.Resampling.LANCZOS)

            # Pad shorter side to bring aspect ratio under 20.0
            if w > h:
                target_h = int(math.ceil(w / TELEGRAM_PHOTO_MAX_ASPECT_RATIO))
                target_w = w
            else:
                target_w = int(math.ceil(h / TELEGRAM_PHOTO_MAX_ASPECT_RATIO))
                target_h = h

            canvas = Image.new("RGB", (target_w, target_h), bg_rgb)
            offset_x = (target_w - w) // 2
            offset_y = (target_h - h) // 2
            canvas.paste(img, (offset_x, offset_y))
            img = canvas
        else:
            # Standard resize if max side > max_side or dimension sum > 10000
            if max(w, h) > max_side:
                scale = max_side / max(w, h)
                w = max(1, int(round(w * scale)))
                h = max(1, int(round(h * scale)))
                img = img.resize((w, h), Image.Resampling.LANCZOS)

            if (w + h) > TELEGRAM_PHOTO_MAX_DIMENSION_SUM:
                scale = TELEGRAM_PHOTO_MAX_DIMENSION_SUM / (w + h)
                w = max(1, int(round(w * scale)))
                h = max(1, int(round(h * scale)))
                img = img.resize((w, h), Image.Resampling.LANCZOS)

        # Step 4: Encode to JPEG under target size
        encoded_bytes, out_w, out_h = encode_jpeg_under_limit(
            img, target_bytes=target_bytes, cancel_event=cancel_event
        )
        if len(encoded_bytes) > target_bytes:
            raise RuntimeError(
                f"规范化后图片大小（{len(encoded_bytes)} 字节）仍超过目标上限（{target_bytes} 字节）"
            )

    validate_snapshots((source_snapshot,))

    # Step 5: Atomically write to cache
    if cancel_event is not None and cancel_event.is_set():
        raise TimeoutError("图片准备已取消")
    temp_path = cache_path.with_name(f"{cache_path.name}.tmp.{os.getpid()}")
    try:
        temp_path.write_bytes(encoded_bytes)
        temp_path.replace(cache_path)
    finally:
        temp_path.unlink(missing_ok=True)

    return PreparedImage(
        source_path=source_path,
        upload_path=cache_path,
        source_info=source_info,
        output_format="JPEG",
        output_width=out_w,
        output_height=out_h,
        output_size=len(encoded_bytes),
        transformed=True,
    )
