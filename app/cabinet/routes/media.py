"""Media upload/download routes for cabinet tickets."""

import hashlib
import hmac
import mimetypes
import re
import time

import structlog
from aiogram.types import BufferedInputFile
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from pydantic import BaseModel

from app.bot_factory import create_bot
from app.config import settings
from app.database.models import User

from ..dependencies import get_current_cabinet_user


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/media', tags=['Cabinet Media'])

ALLOWED_MEDIA_TYPES = {'photo', 'video', 'document'}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB

# Only these raster image types may be served INLINE from the cabinet origin.
# Everything else — documents, SVG, HTML, XML, anything — is forced to download
# as an opaque blob. User attachments are served from the app's own origin, so an
# HTML/SVG file rendered inline would execute JS with access to the cabinet's
# localStorage tokens (stored XSS → session/token theft).
_SAFE_INLINE_IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}

# Active/scriptable content rejected at upload time (defense in depth — the
# download endpoint already forces these to download, but refuse them at the door).
_BLOCKED_UPLOAD_EXTENSIONS = (
    '.html',
    '.htm',
    '.xhtml',
    '.shtml',
    '.mhtml',
    '.svg',
    '.xml',
    '.js',
    '.mjs',
    '.htc',
)
_BLOCKED_UPLOAD_CONTENT_TYPES = {
    'text/html',
    'application/xhtml+xml',
    'image/svg+xml',
    'application/xml',
    'text/xml',
    'application/javascript',
    'text/javascript',
}


def _sanitize_download_filename(filename: str) -> str:
    """Strip path separators, quotes and control chars so a Telegram-supplied
    filename cannot break out of the Content-Disposition header."""
    name = filename.rsplit('/', 1)[-1].rsplit('\\', 1)[-1]
    name = re.sub(r'[\r\n"\\]+', '', name)
    return name[:128] or 'file'


def _content_response_params(filename: str) -> tuple[str, dict[str, str]]:
    """Decide the safe Content-Type / disposition / hardening headers for a download.

    Renders only a strict allow-list of raster images inline; forces everything
    else to download as application/octet-stream. Adds nosniff + a locked-down CSP
    so a user file can never execute script in the cabinet origin.
    """
    guessed_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    if guessed_type in _SAFE_INLINE_IMAGE_TYPES:
        media_type = guessed_type
        disposition = 'inline'
    else:
        media_type = 'application/octet-stream'
        disposition = 'attachment'

    safe_filename = _sanitize_download_filename(filename)
    headers = {
        'Content-Disposition': f'{disposition}; filename="{safe_filename}"',
        # Private attachments must never be cached by shared proxies/CDNs.
        'Cache-Control': 'private, no-store',
        # Never let the browser MIME-sniff a blob back into HTML/JS.
        'X-Content-Type-Options': 'nosniff',
        # Belt-and-braces: even if a renderable type slipped through, sandbox it
        # (no scripts, no network) so it can't touch the app origin.
        'Content-Security-Policy': "default-src 'none'; img-src 'self' data:; sandbox",
    }
    return media_type, headers


# Telegram file_ids are opaque URL-safe base64 strings.
_FILE_ID_RE = re.compile(r'^[A-Za-z0-9_-]{16,256}$')

# Media download tokens — a leaked raw file_id must NOT be downloadable. Media
# URLs are signed with the cabinet JWT secret and expire, and are minted only
# inside authenticated, owner-scoped ticket responses. (Attachments load via
# <img src>, which can't carry the Authorization header, so a short-lived signed
# URL is the right primitive.)
_MEDIA_TOKEN_TTL_SECONDS = 24 * 60 * 60


def _media_signature(file_id: str, exp: int) -> str:
    secret = (settings.get_cabinet_jwt_secret() or '').encode()
    return hmac.new(secret, f'{file_id}.{exp}'.encode(), hashlib.sha256).hexdigest()


def make_media_token(file_id: str) -> str:
    """Signed, expiring token authorizing download of `file_id`."""
    exp = int(time.time()) + _MEDIA_TOKEN_TTL_SECONDS
    return f'{exp}.{_media_signature(file_id, exp)}'


def _verify_media_token(file_id: str, token: str) -> bool:
    exp_str, _, sig = (token or '').partition('.')
    if not sig:
        return False
    try:
        exp = int(exp_str)
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    return hmac.compare_digest(_media_signature(file_id, exp), sig)


class MediaUploadResponse(BaseModel):
    """Response after successful media upload."""

    media_type: str
    file_id: str
    file_unique_id: str | None = None
    media_url: str


def _resolve_target_chat_id() -> int:
    """Get chat ID for uploading files (notification channel or first admin)."""
    chat_id = settings.get_admin_notifications_chat_id()
    if chat_id is not None:
        return chat_id

    admin_ids = settings.get_admin_ids()
    if admin_ids:
        return admin_ids[0]

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail='No chat configured for file uploads',
    )


def _build_media_url(request: Request, file_id: str) -> str:
    """Build a signed, expiring URL for downloading media."""
    base = str(request.url_for('cabinet_download_media', file_id=file_id))
    sep = '&' if '?' in base else '?'
    return f'{base}{sep}token={make_media_token(file_id)}'


@router.post('/upload', response_model=MediaUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_media(
    request: Request,
    user: User = Depends(get_current_cabinet_user),
    file: UploadFile = File(...),
    media_type: str = Form('photo', description='File type: photo, video, or document'),
):
    """
    Upload media file for use in ticket messages.
    Returns file_id that can be used when creating ticket or adding message.
    """
    media_type_normalized = (media_type or '').strip().lower()
    if media_type_normalized not in ALLOWED_MEDIA_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Unsupported media type. Allowed: {", ".join(ALLOWED_MEDIA_TYPES)}',
        )

    # Read and validate file
    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='File is empty',
        )

    if len(file_bytes) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'File too large. Maximum size: {MAX_FILE_SIZE // 1024 // 1024}MB',
        )

    # Validate content type for photos
    if media_type_normalized == 'photo':
        allowed_image_types = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}
        if file.content_type and file.content_type not in allowed_image_types:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Invalid image type. Allowed: JPEG, PNG, GIF, WebP',
            )

    # Reject active/scriptable content for ALL upload types (defense in depth).
    # A user file is served from the cabinet origin, so HTML/SVG/JS must never be
    # accepted — even though the download endpoint also forces them to download.
    declared_type = (file.content_type or '').split(';')[0].strip().lower()
    filename_lower = (file.filename or '').lower()
    if declared_type in _BLOCKED_UPLOAD_CONTENT_TYPES or filename_lower.endswith(_BLOCKED_UPLOAD_EXTENSIONS):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='This file type is not allowed',
        )

    target_chat_id = _resolve_target_chat_id()
    upload = BufferedInputFile(file_bytes, filename=file.filename or 'upload')

    bot = create_bot()

    try:
        # Send with disable_notification to avoid pinging admins — this is just staging
        if media_type_normalized == 'photo':
            message = await bot.send_photo(
                chat_id=target_chat_id,
                photo=upload,
                disable_notification=True,
            )
            media = message.photo[-1]
        elif media_type_normalized == 'video':
            message = await bot.send_video(
                chat_id=target_chat_id,
                video=upload,
                disable_notification=True,
            )
            media = message.video
        else:
            message = await bot.send_document(
                chat_id=target_chat_id,
                document=upload,
                disable_notification=True,
            )
            media = message.document

        # Delete the staging message immediately — file_id persists after deletion
        try:
            await bot.delete_message(chat_id=target_chat_id, message_id=message.message_id)
        except Exception:
            pass  # Best-effort cleanup — file_id is already captured

        media_url = _build_media_url(request, media.file_id)

        logger.info(
            'User uploaded',
            telegram_id=user.telegram_id,
            media_type_normalized=media_type_normalized,
            file_id=media.file_id,
        )

        return MediaUploadResponse(
            media_type=media_type_normalized,
            file_id=media.file_id,
            file_unique_id=getattr(media, 'file_unique_id', None),
            media_url=media_url,
        )
    except HTTPException:
        raise
    except Exception as error:
        logger.error('Failed to upload media for user', telegram_id=user.telegram_id, error=error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to upload media',
        ) from error
    finally:
        await bot.session.close()


@router.get('/{file_id}', name='cabinet_download_media')
async def download_media(
    file_id: str,
    token: str = Query('', description='Signed access token from the ticket response'),
) -> Response:
    """
    Download media file by file_id.
    Used to display images/documents in ticket messages.
    """
    # Validate the id shape, then require a valid, unexpired signed token. The
    # token is minted only inside an authenticated, owner-scoped ticket response,
    # so a leaked raw file_id is not downloadable on its own and the URL expires.
    if not _FILE_ID_RE.match(file_id) or not _verify_media_token(file_id, token):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Media file not found',
        )

    bot = create_bot()

    try:
        file = await bot.get_file(file_id)
        if not file.file_path:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Media file not found',
            )

        buffer = await bot.download_file(file.file_path)

        if hasattr(buffer, 'seek'):
            buffer.seek(0)

        content = buffer.read() if hasattr(buffer, 'read') else bytes(buffer)
        filename = file.file_path.split('/')[-1]

        media_type, headers = _content_response_params(filename)

        return Response(content=content, media_type=media_type, headers=headers)
    except HTTPException:
        raise
    except Exception as error:
        logger.error('Failed to download media', file_id=file_id, error=error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to download media',
        ) from error
    finally:
        await bot.session.close()
