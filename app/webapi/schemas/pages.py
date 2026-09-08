from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.localization.texts import get_texts


class RichTextPageResponse(BaseModel):
    """Generic representation for rich text informational pages."""

    requested_language: str = Field(
        ..., description=get_texts().t('RICH_TEXT_PAGE_REQUESTED_LANGUAGE_DESCRIPTION', 'Язык, запрошенный клиентом')
    )
    language: str = Field(
        ..., description=get_texts().t('RICH_TEXT_PAGE_LANGUAGE_DESCRIPTION', 'Фактический язык найденной записи')
    )
    is_enabled: bool | None = Field(
        default=None,
        description=get_texts().t(
            'RICH_TEXT_PAGE_IS_ENABLED_DESCRIPTION',
            'Текущий статус публикации страницы (если применимо)',
        ),
    )
    content: str = Field(
        ..., description=get_texts().t('RICH_TEXT_PAGE_CONTENT_DESCRIPTION', 'Полное содержимое страницы')
    )
    content_pages: list[str] = Field(
        default_factory=list,
        description=get_texts().t(
            'RICH_TEXT_PAGE_CONTENT_PAGES_DESCRIPTION',
            'Содержимое, разбитое на страницы фиксированной длины',
        ),
    )
    created_at: datetime | None = Field(
        default=None,
        description=get_texts().t('RICH_TEXT_PAGE_CREATED_AT_DESCRIPTION', 'Дата создания записи'),
    )
    updated_at: datetime | None = Field(
        default=None,
        description=get_texts().t('RICH_TEXT_PAGE_UPDATED_AT_DESCRIPTION', 'Дата последнего обновления записи'),
    )


class RichTextPageUpdateRequest(BaseModel):
    language: str = Field(
        default='ru',
        min_length=2,
        max_length=10,
        description=get_texts().t(
            'RICH_TEXT_PAGE_UPDATE_LANGUAGE_DESCRIPTION',
            'Язык, для которого выполняется обновление',
        ),
    )
    content: str = Field(
        ..., description=get_texts().t('RICH_TEXT_PAGE_UPDATE_CONTENT_DESCRIPTION', 'Новое содержимое страницы')
    )
    is_enabled: bool | None = Field(
        default=None,
        description=get_texts().t(
            'RICH_TEXT_PAGE_UPDATE_IS_ENABLED_DESCRIPTION',
            'Если указано — обновить статус публикации',
        ),
    )


class FaqPageResponse(BaseModel):
    id: int
    language: str
    title: str
    content: str
    content_pages: list[str] = Field(default_factory=list)
    display_order: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


class FaqPageListResponse(BaseModel):
    requested_language: str
    language: str
    is_enabled: bool
    total: int
    items: list[FaqPageResponse]


class FaqPageCreateRequest(BaseModel):
    language: str = Field(
        default='ru',
        min_length=2,
        max_length=10,
        description=get_texts().t('FAQ_PAGE_CREATE_LANGUAGE_DESCRIPTION', 'Язык создаваемой страницы'),
    )
    title: str = Field(..., min_length=1, max_length=255)
    content: str = Field(...)
    display_order: int | None = Field(
        default=None,
        ge=0,
        description=get_texts().t(
            'FAQ_PAGE_CREATE_DISPLAY_ORDER_DESCRIPTION',
            'Порядок отображения (если не указан — будет рассчитан автоматически)',
        ),
    )
    is_active: bool | None = Field(
        default=True,
        description=get_texts().t('FAQ_PAGE_CREATE_IS_ACTIVE_DESCRIPTION', 'Начальный статус активности страницы'),
    )


class FaqPageUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    content: str | None = None
    display_order: int | None = Field(default=None, ge=0)
    is_active: bool | None = None


class FaqReorderItem(BaseModel):
    id: int = Field(..., ge=1)
    display_order: int = Field(..., ge=0)


class FaqReorderRequest(BaseModel):
    language: str = Field(
        default='ru',
        min_length=2,
        max_length=10,
        description=get_texts().t('FAQ_REORDER_LANGUAGE_DESCRIPTION', 'Язык, для которого применяется сортировка'),
    )
    items: list[FaqReorderItem]


class FaqStatusResponse(BaseModel):
    requested_language: str
    language: str
    is_enabled: bool


class FaqStatusUpdateRequest(BaseModel):
    language: str = Field(
        default='ru',
        min_length=2,
        max_length=10,
    )
    is_enabled: bool


class ServiceRulesResponse(BaseModel):
    id: int
    title: str
    content: str
    language: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ServiceRulesUpdateRequest(BaseModel):
    language: str = Field(
        default='ru',
        min_length=2,
        max_length=10,
        description=get_texts().t(
            'SERVICE_RULES_UPDATE_LANGUAGE_DESCRIPTION',
            'Язык, для которого обновляются правила',
        ),
    )
    title: str | None = Field(
        default='Правила сервиса',
        min_length=1,
        max_length=255,
    )
    content: str = Field(...)


class ServiceRulesHistoryResponse(BaseModel):
    language: str
    total: int
    items: list[ServiceRulesResponse]
