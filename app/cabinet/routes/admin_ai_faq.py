"""Cabinet API routes for AI FAQ management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.database.models import User
from app.database.models_ai_ticket import AIFaqArticle, AIFaqMedia
from ..dependencies import get_cabinet_db, require_permission
from ..schemas.ai_faq import (
    CabinetAIFaqArticleCreateRequest,
    CabinetAIFaqArticleResponse,
    CabinetAIFaqArticleUpdateRequest,
    CabinetAIFaqMediaCreateRequest,
    CabinetAIFaqMediaResponse,
)

router = APIRouter(prefix='/admin/ai-faq', tags=['Cabinet Admin AI FAQ'])


# ─────────────── Helpers ───────────────

def _serialize_media(m: AIFaqMedia) -> CabinetAIFaqMediaResponse:
    return CabinetAIFaqMediaResponse(
        id=m.id,
        article_id=m.article_id,
        media_type=m.media_type,
        file_id=m.file_id,
        caption=m.caption,
        tag=m.tag,
        created_at=m.created_at,
    )


def _serialize_article(a: AIFaqArticle) -> CabinetAIFaqArticleResponse:
    return CabinetAIFaqArticleResponse(
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

@router.get('', response_model=list[CabinetAIFaqArticleResponse])
async def list_faq_articles(
    active_only: bool = Query(default=False, description='Fetch only active articles'),
    admin: User = Depends(require_permission('ai_faq:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> list[CabinetAIFaqArticleResponse]:
    """Получить все FAQ-статьи."""
    stmt = select(AIFaqArticle).options(joinedload(AIFaqArticle.media))
    if active_only:
        stmt = stmt.where(AIFaqArticle.is_active == True)  # noqa: E712
    stmt = stmt.order_by(AIFaqArticle.id.desc())
    
    result = await db.execute(stmt)
    articles = result.unique().scalars().all()
    
    return [_serialize_article(a) for a in articles]


@router.get('/{article_id}', response_model=CabinetAIFaqArticleResponse)
async def get_faq_article(
    article_id: int,
    admin: User = Depends(require_permission('ai_faq:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> CabinetAIFaqArticleResponse:
    """Получить FAQ-статью по ID."""
    stmt = select(AIFaqArticle).options(joinedload(AIFaqArticle.media)).where(AIFaqArticle.id == article_id)
    result = await db.execute(stmt)
    article = result.unique().scalars().first()
    
    if not article:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'FAQ article not found')
        
    return _serialize_article(article)


@router.post('', response_model=CabinetAIFaqArticleResponse, status_code=status.HTTP_201_CREATED)
async def create_faq_article(
    payload: CabinetAIFaqArticleCreateRequest,
    admin: User = Depends(require_permission('ai_faq:create')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> CabinetAIFaqArticleResponse:
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


@router.put('/{article_id}', response_model=CabinetAIFaqArticleResponse)
async def update_faq_article(
    article_id: int,
    payload: CabinetAIFaqArticleUpdateRequest,
    admin: User = Depends(require_permission('ai_faq:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> CabinetAIFaqArticleResponse:
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
    admin: User = Depends(require_permission('ai_faq:delete')),
    db: AsyncSession = Depends(get_cabinet_db),
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

@router.get('/{article_id}/media', response_model=list[CabinetAIFaqMediaResponse])
async def list_article_media(
    article_id: int,
    admin: User = Depends(require_permission('ai_faq:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> list[CabinetAIFaqMediaResponse]:
    """Получить список медиа-вложений статьи."""
    art_stmt = select(AIFaqArticle.id).where(AIFaqArticle.id == article_id)
    if not (await db.execute(art_stmt)).scalar():
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'FAQ article not found')

    stmt = select(AIFaqMedia).where(AIFaqMedia.article_id == article_id).order_by(AIFaqMedia.id)
    result = await db.execute(stmt)
    return [_serialize_media(m) for m in result.scalars().all()]


@router.post('/{article_id}/media', response_model=CabinetAIFaqMediaResponse, status_code=status.HTTP_201_CREATED)
async def add_article_media(
    article_id: int,
    payload: CabinetAIFaqMediaCreateRequest,
    admin: User = Depends(require_permission('ai_faq:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> CabinetAIFaqMediaResponse:
    """Добавить медиа-вложение к FAQ-статье."""
    art_stmt = select(AIFaqArticle.id).where(AIFaqArticle.id == article_id)
    if not (await db.execute(art_stmt)).scalar():
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'FAQ article not found')

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
    admin: User = Depends(require_permission('ai_faq:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> None:
    """Удалить медиа-вложение из FAQ-статьи."""
    stmt = select(AIFaqMedia).where(AIFaqMedia.id == media_id, AIFaqMedia.article_id == article_id)
    result = await db.execute(stmt)
    media = result.scalars().first()

    if not media:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Media not found')

    await db.delete(media)
    await db.commit()
