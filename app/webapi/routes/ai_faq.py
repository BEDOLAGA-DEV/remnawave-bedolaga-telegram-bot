"""
Web API routes for AI FAQ management.

Tag: ai-faq
Prefix: /ai-faq
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Security, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.database.models_ai_ticket import AIFaqArticle, AIFaqMedia
from ..dependencies import get_db_session, require_api_token
from ..schemas.ai_faq import (
    AIFaqArticleCreateRequest,
    AIFaqArticleResponse,
    AIFaqArticleUpdateRequest,
    AIFaqMediaCreateRequest,
    AIFaqMediaResponse,
)


router = APIRouter()


# ─────────────── Хелпер сериализации ───────────────

def _serialize_media(m: AIFaqMedia) -> AIFaqMediaResponse:
    return AIFaqMediaResponse(
        id=m.id,
        article_id=m.article_id,
        media_type=m.media_type,
        file_id=m.file_id,
        caption=m.caption,
        tag=m.tag,
        created_at=m.created_at,
    )


def _serialize_article(a: AIFaqArticle) -> AIFaqArticleResponse:
    return AIFaqArticleResponse(
        id=a.id,
        title=a.title,
        content=a.content,
        keywords=a.keywords,
        is_active=a.is_active,
        media=[_serialize_media(m) for m in (a.media or [])],
        created_at=a.created_at,
        updated_at=a.updated_at,
    )


# ─────────────── CRUD FAQ-статей ───────────────

@router.get('', response_model=list[AIFaqArticleResponse])
async def list_faq_articles(
    _: Any = Security(require_api_token),
    active_only: bool = Query(default=False, description='Fetch only active articles'),
    db: AsyncSession = Depends(get_db_session),
) -> list[AIFaqArticleResponse]:
    """Получить все FAQ-статьи."""
    stmt = select(AIFaqArticle).options(joinedload(AIFaqArticle.media))
    if active_only:
        stmt = stmt.where(AIFaqArticle.is_active == True)  # noqa: E712
    stmt = stmt.order_by(AIFaqArticle.id.desc())
    
    result = await db.execute(stmt)
    articles = result.unique().scalars().all()
    
    return [_serialize_article(a) for a in articles]


@router.get('/{article_id}', response_model=AIFaqArticleResponse)
async def get_faq_article(
    article_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> AIFaqArticleResponse:
    """Получить FAQ-статью по ID."""
    stmt = select(AIFaqArticle).options(joinedload(AIFaqArticle.media)).where(AIFaqArticle.id == article_id)
    result = await db.execute(stmt)
    article = result.unique().scalars().first()
    
    if not article:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'FAQ article not found')
        
    return _serialize_article(article)


@router.post('', response_model=AIFaqArticleResponse, status_code=status.HTTP_201_CREATED)
async def create_faq_article(
    payload: AIFaqArticleCreateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> AIFaqArticleResponse:
    """Создать новую FAQ-статью."""
    article = AIFaqArticle(
        title=payload.title,
        content=payload.content,
        keywords=payload.keywords,
        is_active=payload.is_active,
    )
    db.add(article)
    await db.commit()
    await db.refresh(article)
    
    return _serialize_article(article)


@router.put('/{article_id}', response_model=AIFaqArticleResponse)
async def update_faq_article(
    article_id: int,
    payload: AIFaqArticleUpdateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> AIFaqArticleResponse:
    """Обновить FAQ-статью."""
    stmt = select(AIFaqArticle).options(joinedload(AIFaqArticle.media)).where(AIFaqArticle.id == article_id)
    result = await db.execute(stmt)
    article = result.unique().scalars().first()
    
    if not article:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'FAQ article not found')
        
    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(article, key, value)
        
    await db.commit()
    await db.refresh(article)
    
    return _serialize_article(article)


@router.delete('/{article_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_faq_article(
    article_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    """Удалить FAQ-статью (каскадно удалит медиа)."""
    stmt = select(AIFaqArticle).where(AIFaqArticle.id == article_id)
    result = await db.execute(stmt)
    article = result.scalars().first()
    
    if not article:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'FAQ article not found')
        
    await db.delete(article)
    await db.commit()


# ─────────────── CRUD медиа-вложений FAQ ───────────────

@router.get('/{article_id}/media', response_model=list[AIFaqMediaResponse])
async def list_article_media(
    article_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> list[AIFaqMediaResponse]:
    """Получить список медиа-вложений статьи."""
    # Проверяем, что статья существует
    art_stmt = select(AIFaqArticle.id).where(AIFaqArticle.id == article_id)
    if not (await db.execute(art_stmt)).scalar():
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'FAQ article not found')

    stmt = select(AIFaqMedia).where(AIFaqMedia.article_id == article_id).order_by(AIFaqMedia.id)
    result = await db.execute(stmt)
    return [_serialize_media(m) for m in result.scalars().all()]


@router.post('/{article_id}/media', response_model=AIFaqMediaResponse, status_code=status.HTTP_201_CREATED)
async def add_article_media(
    article_id: int,
    payload: AIFaqMediaCreateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> AIFaqMediaResponse:
    """Добавить медиа-вложение к FAQ-статье. file_id получается через POST /upload."""
    # Проверяем статью
    art_stmt = select(AIFaqArticle.id).where(AIFaqArticle.id == article_id)
    if not (await db.execute(art_stmt)).scalar():
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'FAQ article not found')

    # Проверяем уникальность тега
    tag_stmt = select(AIFaqMedia.id).where(AIFaqMedia.tag == payload.tag)
    if (await db.execute(tag_stmt)).scalar():
        raise HTTPException(status.HTTP_409_CONFLICT, f'Тег "{payload.tag}" уже используется')

    media = AIFaqMedia(
        article_id=article_id,
        media_type=payload.media_type,
        file_id=payload.file_id,
        caption=payload.caption,
        tag=payload.tag,
    )
    db.add(media)
    await db.commit()
    await db.refresh(media)

    return _serialize_media(media)


@router.delete('/{article_id}/media/{media_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_article_media(
    article_id: int,
    media_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    """Удалить медиа-вложение из FAQ-статьи."""
    stmt = select(AIFaqMedia).where(AIFaqMedia.id == media_id, AIFaqMedia.article_id == article_id)
    result = await db.execute(stmt)
    media = result.scalars().first()

    if not media:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Media not found')

    await db.delete(media)
    await db.commit()
