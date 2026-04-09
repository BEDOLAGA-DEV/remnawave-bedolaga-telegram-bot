"""Admin routes for managing news article media (images/videos)."""

import asyncio
import re

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from PIL import Image as PILImage

from app.config import settings
from app.database.models import User
from app.services.news_media_service import (
    SavedMedia,
    delete_media_file,
    detect_file_type,
    ensure_upload_dirs,
    save_image,
    save_video,
)

from ..dependencies import require_permission
from ..schemas.news_media import NewsMediaUploadResponse


logger = structlog.get_logger(__name__)

_BYTES_PER_MB = 1024 * 1024

# Only allow UUID-hex filenames with expected extensions (path traversal defense-in-depth).
# thumb_ prefix is NOT allowed — thumbnails are cleaned up automatically when the main file is deleted.
_SAFE_FILENAME_RE = re.compile(r'^[0-9a-f]{32}\.(jpg|mp4|webm)$')

router = APIRouter(prefix='/admin/news/media', tags=['Cabinet Admin News Media'])


_ALLOWED_SCHEMES = frozenset({'http', 'https'})


def _build_media_url(request: Request, relative_path: str) -> str:
    """Build a full URL for a media file, respecting reverse proxy headers."""
    proto = request.headers.get('X-Forwarded-Proto', request.url.scheme).split(',')[0].strip()
    if proto not in _ALLOWED_SCHEMES:
        proto = 'https'
    host = request.headers.get('X-Forwarded-Host', request.headers.get('Host', request.url.netloc))
    host = host.split(',')[0].strip()
    return f'{proto}://{host}/uploads/{relative_path}'


def _build_response(request: Request, saved: SavedMedia) -> NewsMediaUploadResponse:
    """Convert SavedMedia to API response with full URLs."""
    thumbnail_url = _build_media_url(request, saved.thumbnail_path) if saved.thumbnail_path else None

    return NewsMediaUploadResponse(
        url=_build_media_url(request, saved.relative_path),
        thumbnail_url=thumbnail_url,
        media_type=saved.media_type,
        filename=saved.filename,
        size_bytes=saved.size_bytes,
        width=saved.width,
        height=saved.height,
    )


@router.post('/upload', response_model=NewsMediaUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_media(
    request: Request,
    file: UploadFile = File(...),
    admin: User = Depends(require_permission('news:edit')),
) -> NewsMediaUploadResponse:
    """Upload an image or video for a news article."""
    # Read with a hard budget to prevent memory exhaustion from huge uploads.
    # Read slightly over the max allowed size so we can detect oversized files.
    absolute_max_bytes = settings.MEDIA_MAX_VIDEO_SIZE_MB * _BYTES_PER_MB + 1
    data = await file.read(absolute_max_bytes)
    await file.close()
    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Empty file',
        )

    if len(data) >= absolute_max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f'File too large. Absolute maximum: {settings.MEDIA_MAX_VIDEO_SIZE_MB} MB',
        )

    # Detect type from magic bytes
    try:
        media_type, _ext = detect_file_type(data)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail='Unsupported file type. Allowed: JPEG, PNG, WebP, MP4, WebM',
        ) from None

    # Enforce per-type size limits
    max_size_mb = settings.MEDIA_MAX_IMAGE_SIZE_MB if media_type == 'image' else settings.MEDIA_MAX_VIDEO_SIZE_MB
    if len(data) > max_size_mb * _BYTES_PER_MB:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f'File too large. Maximum size for {media_type}: {max_size_mb} MB',
        )

    upload_path = settings.get_media_upload_path()
    await asyncio.to_thread(ensure_upload_dirs, upload_path)

    try:
        if media_type == 'image':
            saved = await save_image(
                data,
                upload_path,
                max_dim=settings.MEDIA_IMAGE_MAX_DIMENSION,
                quality=settings.MEDIA_JPEG_QUALITY,
            )
        else:
            saved = await save_video(data, upload_path)
    except (ValueError, OSError, PILImage.DecompressionBombError) as exc:
        logger.warning('Failed to save uploaded media', media_type=media_type, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Failed to process uploaded file',
        ) from None

    logger.info(
        'Media uploaded',
        filename=saved.filename,
        media_type=saved.media_type,
        size_bytes=saved.size_bytes,
        admin_id=admin.id,
    )

    return _build_response(request, saved)


@router.delete('/{filename}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_media(
    filename: str,
    admin: User = Depends(require_permission('news:delete')),
) -> None:
    """Delete a previously uploaded media file."""
    if not _SAFE_FILENAME_RE.match(filename):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid filename',
        )

    upload_path = settings.get_media_upload_path()

    deleted = await asyncio.to_thread(delete_media_file, filename, upload_path)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='File not found',
        )

    logger.info('Media deleted', filename=filename, admin_id=admin.id)
