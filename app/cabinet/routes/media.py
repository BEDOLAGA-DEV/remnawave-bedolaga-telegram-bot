"""Media upload/download routes for cabinet tickets."""

import mimetypes

import structlog
from aiogram.types import BufferedInputFile
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot_factory import create_bot
from app.config import settings
from app.database.models import Ticket, TicketMessage, User
from app.services.support_settings_service import SupportSettingsService

from ..dependencies import get_cabinet_db, get_current_cabinet_user


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/media', tags=['Cabinet Media'])

ALLOWED_MEDIA_TYPES = {'photo', 'video', 'document'}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB


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
    """Build URL for downloading media."""
    return str(request.url_for('cabinet_download_media', file_id=file_id))


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
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> Response:
    """
    Download media file by file_id.
    Used to display images/documents in ticket messages.

    Authorization: requester must either
      - be the owner of a ticket containing a TicketMessage with this file_id, OR
      - be the author of a TicketMessage with this file_id (covers admin/moderator
        replies that staff later view), OR
      - be an admin or active moderator (full support staff access).
    Otherwise the file_id is treated as not found to prevent enumeration oracles.
    """
    is_staff = False
    if user.telegram_id is not None:
        is_staff = settings.is_admin(user.telegram_id) or SupportSettingsService.is_moderator(user.telegram_id)
    elif user.email and user.email_verified:
        is_staff = settings.is_admin(email=user.email)

    if not is_staff:
        # Verify file_id is referenced by a TicketMessage in a ticket the user owns,
        # or that the user authored. This prevents arbitrary Telegram-file enumeration.
        stmt = (
            select(TicketMessage.id)
            .join(Ticket, Ticket.id == TicketMessage.ticket_id)
            .where(
                TicketMessage.media_file_id == file_id,
                (Ticket.user_id == user.id) | (TicketMessage.user_id == user.id),
            )
            .limit(1)
        )
        result = await db.execute(stmt)
        if result.scalar_one_or_none() is None:
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

        media_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'

        return Response(
            content=content,
            media_type=media_type,
            headers={
                'Content-Disposition': f'inline; filename={filename}',
                'Cache-Control': 'public, max-age=86400',  # Cache for 24 hours
            },
        )
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
