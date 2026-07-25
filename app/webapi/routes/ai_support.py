from __future__ import annotations

import json
from typing import Any

import structlog
from fastapi import APIRouter, File, HTTPException, Query, Security, UploadFile, status
from pydantic import BaseModel

from ai_support_bot.app.db import crud
from ai_support_bot.app.db.database import AsyncSessionLocal
from ai_support_bot.app.services import settings_store
from ai_support_bot.app.services.openai_client import OpenAIError
from ai_support_bot.app.services.rag_service import rag_service

from ..dependencies import require_api_token

router = APIRouter()
logger = structlog.get_logger(__name__)


class SettingsUpdateRequest(BaseModel):
    settings: dict[str, str]


class KnowledgeSourceResponse(BaseModel):
    id: int
    filename: str
    title: str | None
    is_active: bool
    chunk_count: int
    message_count: int
    created_at: str


class KnowledgeSummaryResponse(BaseModel):
    sources: list[dict[str, Any]]
    chunk_total: int
    message_total: int


class MessageItemResponse(BaseModel):
    id: int
    conversation_id: int
    telegram_id: int
    role: str
    content: str
    has_media: bool
    media_type: str | None
    model: str | None
    tokens_prompt: int | None
    tokens_completion: int | None
    used_context: list[Any] | None
    created_at: str


class HistoryResponse(BaseModel):
    messages: list[MessageItemResponse]
    page: int
    per_page: int
    total: int
    has_next: bool


@router.get('/settings')
async def get_ai_support_settings(
    _: Any = Security(require_api_token),
) -> dict[str, str]:
    """Получить текущие настройки ИИ-бота поддержки."""
    await settings_store.load()
    return settings_store.all_settings()


@router.post('/settings')
async def update_ai_support_settings(
    payload: SettingsUpdateRequest,
    _: Any = Security(require_api_token),
) -> dict[str, Any]:
    """Обновить настройки ИИ-бота поддержки."""
    for key, val in payload.settings.items():
        await settings_store.set_value(key, str(val).strip())
    return {'status': 'ok', 'settings': settings_store.all_settings()}


@router.post('/settings/reset')
async def reset_ai_support_settings(
    _: Any = Security(require_api_token),
) -> dict[str, Any]:
    """Сбросить настройки ИИ-бота поддержки к значениям по умолчанию."""
    for key in list(settings_store._cache.keys()):
        await settings_store.set_value(key, '')
    await settings_store.load()
    return {'status': 'ok', 'settings': settings_store.all_settings()}


@router.get('/knowledge', response_model=KnowledgeSummaryResponse)
async def get_knowledge_summary(
    _: Any = Security(require_api_token),
) -> KnowledgeSummaryResponse:
    """Получить информацию о базе знаний RAG и списке источников."""
    async with AsyncSessionLocal() as db:
        sources_list = await crud.list_sources(db)
        chunk_total = await crud.count_chunks(db)
        message_total = await crud.count_messages(db)

    serialized_sources = [
        {
            'id': s.id,
            'filename': s.filename,
            'title': s.title,
            'is_active': s.is_active,
            'chunk_count': s.chunk_count,
            'message_count': s.message_count,
            'created_at': s.created_at.isoformat() if s.created_at else '',
        }
        for s in sources_list
    ]

    return KnowledgeSummaryResponse(
        sources=serialized_sources,
        chunk_total=chunk_total,
        message_total=message_total,
    )


@router.post('/knowledge/upload')
async def upload_knowledge_file(
    file: UploadFile = File(...),
    _: Any = Security(require_api_token),
) -> dict[str, Any]:
    """Загрузить JSON файл в базу знаний ИИ-бота."""
    raw_bytes = await file.read()
    try:
        parsed = json.loads(raw_bytes.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Некорректный JSON файл') from err

    try:
        async with AsyncSessionLocal() as db:
            result = await rag_service.ingest_file(db, file.filename or 'upload.json', raw_bytes, parsed)
    except OpenAIError as error:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f'Ошибка OpenAI: {error}') from error
    except ValueError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
    except Exception as error:
        logger.error('Knowledge upload failed', error=str(error))
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, 'Не удалось обработать файл') from error

    return result


@router.post('/knowledge/{source_id}/toggle')
async def toggle_knowledge_source(
    source_id: int,
    _: Any = Security(require_api_token),
) -> dict[str, Any]:
    """Переключить статус активности источника базы знаний."""
    async with AsyncSessionLocal() as db:
        source = await crud.get_source(db, source_id)
        if not source:
            raise HTTPException(status.HTTP_404_NOT_FOUND, 'Источник знаний не найден')
        new_active = not source.is_active
        await crud.set_source_active(db, source_id, new_active)

    return {'status': 'ok', 'source_id': source_id, 'is_active': new_active}


@router.delete('/knowledge/{source_id}')
async def delete_knowledge_source(
    source_id: int,
    _: Any = Security(require_api_token),
) -> dict[str, Any]:
    """Удалить источник из базы знаний."""
    async with AsyncSessionLocal() as db:
        success = await crud.delete_source(db, source_id)
        if not success:
            raise HTTPException(status.HTTP_404_NOT_FOUND, 'Источник знаний не найден')

    return {'status': 'ok', 'deleted_source_id': source_id}


class ConversationSummaryItem(BaseModel):
    id: int
    telegram_id: int
    escalated: bool
    created_at: str
    updated_at: str
    message_count: int
    last_message: str
    last_message_role: str
    last_message_at: str


class ConversationsResponse(BaseModel):
    conversations: list[ConversationSummaryItem]
    page: int
    per_page: int
    total: int
    has_next: bool


@router.get('/conversations', response_model=ConversationsResponse)
async def get_ai_conversations(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    _: Any = Security(require_api_token),
) -> ConversationsResponse:
    """Получить пагинированный список диалогов (чатов) пользователей."""
    offset = (page - 1) * per_page
    async with AsyncSessionLocal() as db:
        total = await crud.count_conversations(db)
        convs = await crud.list_conversations_summary(db, limit=per_page, offset=offset)

    items = [ConversationSummaryItem(**c) for c in convs]
    return ConversationsResponse(
        conversations=items,
        page=page,
        per_page=per_page,
        total=total,
        has_next=page * per_page < total,
    )


@router.get('/history', response_model=HistoryResponse)
async def get_ai_history(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    telegram_id: int | None = Query(None),
    _: Any = Security(require_api_token),
) -> HistoryResponse:
    """Получить пагинированную историю сообщений ИИ-бота (опционально по telegram_id)."""
    offset = (page - 1) * per_page
    async with AsyncSessionLocal() as db:
        total = await crud.count_messages(db, telegram_id=telegram_id)
        messages = await crud.list_recent_messages(db, limit=per_page, offset=offset, telegram_id=telegram_id)

    serialized_messages = [
        MessageItemResponse(
            id=msg.id,
            conversation_id=msg.conversation_id,
            telegram_id=msg.telegram_id,
            role=msg.role,
            content=msg.content,
            has_media=msg.has_media,
            media_type=msg.media_type,
            model=msg.model,
            tokens_prompt=msg.tokens_prompt,
            tokens_completion=msg.tokens_completion,
            used_context=msg.used_context,
            created_at=msg.created_at.isoformat() if msg.created_at else '',
        )
        for msg in messages
    ]

    return HistoryResponse(
        messages=serialized_messages,
        page=page,
        per_page=per_page,
        total=total,
        has_next=page * per_page < total,
    )
