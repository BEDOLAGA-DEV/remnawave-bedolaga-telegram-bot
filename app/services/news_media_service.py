"""Media processing service for news article images and videos.

Handles file validation (magic bytes), image resizing via Pillow,
thumbnail generation, and atomic file writes with UUID filenames.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import structlog
from PIL import Image, ImageOps


logger = structlog.get_logger(__name__)

# Hard limit on decompressed image pixels to prevent decompression bombs.
# 25M pixels ≈ 5000x5000, roughly 75 MB of raw RGB data — safe for a news editor.
Image.MAX_IMAGE_PIXELS = 25_000_000

# Minimum file size to attempt magic byte detection
_MIN_MAGIC_BYTES = 12

# --- Magic byte signatures for file type detection ---

ALLOWED_IMAGE_SIGNATURES: dict[bytes, str] = {
    b'\xff\xd8\xff': '.jpg',
    b'\x89PNG': '.png',
    # WebP: starts with RIFF....WEBP (bytes 0-3 = RIFF, bytes 8-11 = WEBP)
}

ALLOWED_VIDEO_SIGNATURES: dict[bytes, str] = {
    # MP4: bytes 4-7 = 'ftyp'
    b'\x1a\x45\xdf\xa3': '.webm',
}

# Known ISO base media file format brands for video.
# Rejects HEIC/HEIF image brands (heic, heix, mif1, msf1, avif) that share the ftyp box format.
_MP4_VIDEO_BRANDS: frozenset[bytes] = frozenset(
    {
        b'isom',
        b'iso2',
        b'iso3',
        b'iso4',
        b'iso5',
        b'iso6',
        b'mp41',
        b'mp42',
        b'mp71',
        b'M4V ',
        b'M4VH',
        b'M4VP',
        b'MSNV',
        b'avc1',
        b'mmp4',
        b'dash',
        b'3gp4',
        b'3gp5',
        b'3gp6',
        b'NDAS',
        b'NDSC',
        b'NDSH',
        b'NDSS',
        b'NDSM',
        b'NDSP',
        b'qt  ',
    }
)

_IMAGES_DIR = 'images'
_VIDEOS_DIR = 'videos'
_THUMBNAILS_DIR = 'thumbnails'

_THUMBNAIL_SIZE = (400, 400)


@dataclass(frozen=True, slots=True)
class SavedMedia:
    """Result of saving a media file."""

    filename: str
    relative_path: str
    thumbnail_path: str | None
    media_type: Literal['image', 'video']
    content_type: str
    size_bytes: int
    width: int | None
    height: int | None


def ensure_upload_dirs(upload_path: Path) -> None:
    """Create images/, videos/, thumbnails/ subdirectories under upload_path."""
    for subdir in (_IMAGES_DIR, _VIDEOS_DIR, _THUMBNAILS_DIR):
        (upload_path / subdir).mkdir(parents=True, exist_ok=True)


MediaType = Literal['image', 'video']


def detect_file_type(data: bytes) -> tuple[MediaType, str]:
    """Detect media type and extension from magic bytes.

    Returns:
        Tuple of (media_type, extension), e.g. ('image', '.jpg').

    Raises:
        ValueError: If file type is not recognized.
    """
    if len(data) < _MIN_MAGIC_BYTES:
        msg = 'File too small to identify'
        raise ValueError(msg)

    # Check WebP: RIFF at offset 0, WEBP at offset 8
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image', '.webp'

    # Check standard image signatures
    for signature, ext in ALLOWED_IMAGE_SIGNATURES.items():
        if data[: len(signature)] == signature:
            return 'image', ext

    # Check MP4/MOV: bytes 4-7 must be 'ftyp', bytes 8-12 must be a known video brand.
    # Rejects HEIC/HEIF images (ftypheic, ftypmif1, etc.) which share the ftyp box format.
    if data[4:8] == b'ftyp':
        brand = data[8:12]
        if brand in _MP4_VIDEO_BRANDS:
            return 'video', '.mp4'
        logger.warning('Unknown ftyp brand rejected', brand=brand.decode('ascii', errors='replace'))

    # Check standard video signatures
    for signature, ext in ALLOWED_VIDEO_SIGNATURES.items():
        if data[: len(signature)] == signature:
            return 'video', ext

    msg = 'Unsupported file type: magic bytes do not match any allowed format'
    raise ValueError(msg)


def _process_and_save_image(
    data: bytes,
    upload_path: Path,
    max_dim: int,
    quality: int,
) -> SavedMedia:
    """Process image: validate, resize, convert to JPEG, generate thumbnail.

    This is a CPU-bound function intended to be run via asyncio.to_thread.
    """
    img = Image.open(io.BytesIO(data))
    try:
        # Double-check pixel count (defense-in-depth alongside Image.MAX_IMAGE_PIXELS)
        if img.size[0] * img.size[1] > 25_000_000:
            msg = 'Image dimensions too large'
            raise ValueError(msg)

        # Fix EXIF orientation (rotated photos from phones)
        transposed = ImageOps.exif_transpose(img)
        if transposed is not None:
            old_img = img
            img = transposed
            old_img.close()

        # Normalize to RGB for consistent JPEG output
        if img.mode != 'RGB':
            old_img = img
            img = img.convert('RGB')
            old_img.close()

        original_width, original_height = img.size

        # Resize if any dimension exceeds max_dim (preserving aspect ratio)
        if original_width > max_dim or original_height > max_dim:
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)

        width, height = img.size
        filename = f'{uuid.uuid4().hex}.jpg'
        image_dir = upload_path / _IMAGES_DIR
        target_path = image_dir / filename

        # Atomic write: save to temp file, then rename
        tmp_path = target_path.with_suffix('.tmp')
        try:
            img.save(tmp_path, format='JPEG', quality=quality, optimize=True)
            tmp_path.rename(target_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        size_bytes = target_path.stat().st_size

        # Generate thumbnail
        thumbnail_filename = f'thumb_{filename}'
        thumbnail_dir = upload_path / _THUMBNAILS_DIR
        thumbnail_target = thumbnail_dir / thumbnail_filename

        tmp_thumb = thumbnail_target.with_suffix('.tmp')
        try:
            thumb = img.copy()
            try:
                thumb.thumbnail(_THUMBNAIL_SIZE, Image.LANCZOS)
                thumb.save(tmp_thumb, format='JPEG', quality=quality, optimize=True)
                tmp_thumb.rename(thumbnail_target)
            finally:
                thumb.close()
        except Exception:
            tmp_thumb.unlink(missing_ok=True)
            # Non-fatal: log and continue without thumbnail
            logger.warning('Failed to generate thumbnail', filename=filename, exc_info=True)
            thumbnail_filename = None

        relative_path = f'{_IMAGES_DIR}/{filename}'
        thumbnail_path = f'{_THUMBNAILS_DIR}/{thumbnail_filename}' if thumbnail_filename else None

        return SavedMedia(
            filename=filename,
            relative_path=relative_path,
            thumbnail_path=thumbnail_path,
            media_type='image',
            content_type='image/jpeg',
            size_bytes=size_bytes,
            width=width,
            height=height,
        )
    finally:
        img.close()


async def save_image(
    data: bytes,
    upload_path: Path,
    max_dim: int,
    quality: int,
) -> SavedMedia:
    """Validate, resize, and save an image file. Runs PIL operations in a thread."""
    return await asyncio.to_thread(_process_and_save_image, data, upload_path, max_dim, quality)


def _save_video_sync(data: bytes, upload_path: Path) -> SavedMedia:
    """Save a video file. CPU-bound function for asyncio.to_thread."""
    media_type, ext = detect_file_type(data)
    if media_type != 'video':
        msg = 'Data does not contain a recognized video format'
        raise ValueError(msg)

    filename = f'{uuid.uuid4().hex}{ext}'
    video_dir = upload_path / _VIDEOS_DIR
    target_path = video_dir / filename

    content_type_map: dict[str, str] = {
        '.mp4': 'video/mp4',
        '.webm': 'video/webm',
    }

    # Atomic write
    tmp_path = target_path.with_suffix('.tmp')
    try:
        tmp_path.write_bytes(data)
        tmp_path.rename(target_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    size_bytes = target_path.stat().st_size

    return SavedMedia(
        filename=filename,
        relative_path=f'{_VIDEOS_DIR}/{filename}',
        thumbnail_path=None,
        media_type='video',
        content_type=content_type_map.get(ext, 'application/octet-stream'),
        size_bytes=size_bytes,
        width=None,
        height=None,
    )


async def save_video(data: bytes, upload_path: Path) -> SavedMedia:
    """Validate and save a video file. Runs I/O in a thread."""
    return await asyncio.to_thread(_save_video_sync, data, upload_path)


def delete_media_file(filename: str, upload_path: Path) -> bool:
    """Delete a media file by filename with path traversal protection.

    Searches images/, videos/, thumbnails/ directories.

    Returns:
        True if at least one file was deleted, False otherwise.
    """
    deleted = False

    for subdir in (_IMAGES_DIR, _VIDEOS_DIR, _THUMBNAILS_DIR):
        candidate = (upload_path / subdir / filename).resolve()
        base_dir = (upload_path / subdir).resolve()

        # Path traversal guard
        if not candidate.is_relative_to(base_dir):
            logger.warning(
                'Path traversal attempt blocked',
                filename=filename,
                resolved=str(candidate),
            )
            continue

        if candidate.is_file():
            candidate.unlink()
            deleted = True
            logger.info('Deleted media file', path=str(candidate))

    # Also try to delete matching thumbnail
    if not filename.startswith('thumb_'):
        thumb_name = f'thumb_{filename}'
        thumb_path = (upload_path / _THUMBNAILS_DIR / thumb_name).resolve()
        thumb_base = (upload_path / _THUMBNAILS_DIR).resolve()

        if thumb_path.is_relative_to(thumb_base) and thumb_path.is_file():
            thumb_path.unlink()
            logger.info('Deleted thumbnail', path=str(thumb_path))

    return deleted
