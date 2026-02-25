from __future__ import annotations

import mimetypes
from typing import Any

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BufferedInputFile
from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    Security,
    UploadFile,
    status,
)

from app.config import settings
from app.localization.texts import get_texts

from ..dependencies import require_api_token
from ..schemas.media import MediaUploadResponse


router = APIRouter()
logger = structlog.get_logger(__name__)

ALLOWED_MEDIA_TYPES = {'photo', 'video', 'document'}


def _resolve_target_chat_id() -> int:
    """Выбирает чат для загрузки файлов (канал уведомлений или первый админ)."""

    chat_id = settings.get_admin_notifications_chat_id()
    if chat_id is not None:
        return chat_id

    admin_ids = settings.get_admin_ids()
    if admin_ids:
        return admin_ids[0]

    texts = get_texts('ru')
    raise HTTPException(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        texts.t(
            'WEBAPI_MEDIA_UPLOAD_CHAT_NOT_CONFIGURED',
            'Не настроен чат для загрузки файлов (ADMIN_NOTIFICATIONS_CHAT_ID или ADMIN_IDS)',
        ),
    )


def _build_media_url(request: Request, file_id: str) -> str:
    return str(request.url_for('download_media', file_id=file_id))


@router.post('/upload', response_model=MediaUploadResponse, tags=['media'], status_code=status.HTTP_201_CREATED)
async def upload_media(
    request: Request,
    _: Any = Security(require_api_token),
    file: UploadFile = File(...),
    media_type: str = Form('document', description='Тип файла: photo, video или document'),
    caption: str | None = Form(None, description='Необязательная подпись к файлу'),
) -> MediaUploadResponse:
    texts = get_texts('ru')
    media_type_normalized = (media_type or '').strip().lower()
    if media_type_normalized not in ALLOWED_MEDIA_TYPES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t('WEBAPI_MEDIA_UNSUPPORTED_MEDIA_TYPE', 'Unsupported media type'),
        )

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            texts.t('WEBAPI_MEDIA_FILE_IS_EMPTY', 'File is empty'),
        )

    target_chat_id = _resolve_target_chat_id()
    upload = BufferedInputFile(file_bytes, filename=file.filename or 'upload')

    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    try:
        if media_type_normalized == 'photo':
            message = await bot.send_photo(
                chat_id=target_chat_id,
                photo=upload,
                caption=caption,
            )
            media = message.photo[-1]
        elif media_type_normalized == 'video':
            message = await bot.send_video(
                chat_id=target_chat_id,
                video=upload,
                caption=caption,
            )
            media = message.video
        else:
            message = await bot.send_document(
                chat_id=target_chat_id,
                document=upload,
                caption=caption,
            )
            media = message.document

        media_url = _build_media_url(request, media.file_id)
        return MediaUploadResponse(
            media_type=media_type_normalized,
            file_id=media.file_id,
            file_unique_id=getattr(media, 'file_unique_id', None),
            media_url=media_url,
        )
    except HTTPException:
        raise
    except Exception as error:
        logger.error('Failed to upload media', error=error)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            texts.t('WEBAPI_MEDIA_FAILED_TO_UPLOAD', 'Failed to upload media'),
        ) from error
    finally:
        await bot.session.close()


@router.get('/media/{file_id}', name='download_media', tags=['media'])
async def download_media(
    file_id: str,
    _: Any = Security(require_api_token),
) -> Response:
    texts = get_texts('ru')
    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    try:
        file = await bot.get_file(file_id)
        if not file.file_path:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                texts.t('WEBAPI_MEDIA_FILE_NOT_FOUND', 'Media file not found'),
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
            },
        )
    except HTTPException:
        raise
    except Exception as error:  # pragma: no cover - неожиданные ошибки загрузки файла
        logger.error('Failed to download media', file_id=file_id, error=error)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            texts.t('WEBAPI_MEDIA_FAILED_TO_DOWNLOAD', 'Failed to download media'),
        ) from error
    finally:
        await bot.session.close()
