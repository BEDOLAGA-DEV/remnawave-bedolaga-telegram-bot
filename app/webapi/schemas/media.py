from __future__ import annotations

from pydantic import BaseModel, Field

from app.localization.texts import get_texts


class MediaUploadResponse(BaseModel):
    media_type: str = Field(
        description=get_texts().t('MEDIA_TYPE_DESCRIPTION', 'Тип загруженного файла (photo, video, document)')
    )
    file_id: str = Field(
        description=get_texts().t('MEDIA_FILE_ID_DESCRIPTION', 'Telegram file_id загруженного файла')
    )
    file_unique_id: str | None = Field(
        default=None,
        description=get_texts().t('MEDIA_FILE_UNIQUE_ID_DESCRIPTION', 'Уникальный идентификатор файла'),
    )
    media_url: str | None = Field(
        default=None,
        description=get_texts().t('MEDIA_URL_DESCRIPTION', 'Прямая ссылка на файл для предпросмотра'),
    )
