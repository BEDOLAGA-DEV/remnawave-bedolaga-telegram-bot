"""Admin reviews routes for cabinet."""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import Any

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.bot_factory import create_bot
from app.config import settings
from app.database.crud.user_review import (
    approve_review as crud_approve_review,
    reject_review as crud_reject_review,
    set_channel_message_id,
)
from app.database.models import User, UserReview

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/reviews', tags=['Admin Reviews'])


# ============== Schemas ==============


class ReviewUserInfo(BaseModel):
    id: int
    telegram_id: int | None = None
    email: str | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    created_at: datetime | None = None


class ReviewResponse(BaseModel):
    id: int
    user_id: int
    rating: int
    text: str
    bonus_kopeks: int
    is_approved: bool
    channel_message_id: int | None = None
    created_at: datetime | None = None
    user: ReviewUserInfo | None = None
    channel_preview: str | None = None


class ReviewListResponse(BaseModel):
    items: list[ReviewResponse]
    total: int
    pending_count: int
    approved_count: int
    limit: int
    offset: int


class ReviewActionResponse(BaseModel):
    success: bool
    message: str
    review: ReviewResponse | None = None
    channel_posted: bool = False
    channel_error: str | None = None


# ============== Helpers ==============


def _serialize_user(user: User | None) -> ReviewUserInfo | None:
    if not user:
        return None
    return ReviewUserInfo(
        id=user.id,
        telegram_id=user.telegram_id,
        email=user.email,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        full_name=getattr(user, 'full_name', None),
        created_at=user.created_at,
    )


def _build_channel_preview(review: UserReview) -> str:
    """Build a plain-text preview of how the review will look in the channel."""
    stars = '\u2b50' * review.rating
    username = review.user.username if review.user and review.user.username else None
    user_display = f'@{username}' if username else (
        (review.user.first_name if review.user else None) or 'Пользователь'
    )

    days = 0
    if review.user and review.user.created_at:
        days = (datetime.now(UTC) - review.user.created_at).days

    return (
        f'{stars} ({review.rating}/5)\n\n'
        f'"{review.text}"\n\n'
        f'— {user_display}, пользователь {days} дней'
    )


def _format_review_for_channel(review: UserReview) -> str:
    """Format a review for posting to the public channel (HTML escaped)."""
    stars = '\u2b50' * review.rating
    username = review.user.username if review.user and review.user.username else None
    user_display = f'@{username}' if username else (
        (review.user.first_name if review.user else None) or 'Пользователь'
    )

    days = 0
    if review.user and review.user.created_at:
        days = (datetime.now(UTC) - review.user.created_at).days

    escaped_text = html.escape(review.text)

    return (
        f'{stars} ({review.rating}/5)\n'
        f'\n'
        f'"{escaped_text}"\n'
        f'\n'
        f'— {user_display}, пользователь {days} дней'
    )


def _serialize_review(review: UserReview, include_preview: bool = False) -> ReviewResponse:
    return ReviewResponse(
        id=review.id,
        user_id=review.user_id,
        rating=review.rating,
        text=review.text,
        bonus_kopeks=review.bonus_kopeks or 0,
        is_approved=bool(review.is_approved),
        channel_message_id=review.channel_message_id,
        created_at=review.created_at,
        user=_serialize_user(getattr(review, 'user', None)),
        channel_preview=_build_channel_preview(review) if include_preview else None,
    )


async def _get_review_with_user(db: AsyncSession, review_id: int) -> UserReview | None:
    result = await db.execute(
        select(UserReview)
        .where(UserReview.id == review_id)
        .options(selectinload(UserReview.user))
    )
    return result.scalar_one_or_none()


async def _get_counts(db: AsyncSession) -> tuple[int, int]:
    """Get (pending_count, approved_count)."""
    pending = await db.execute(
        select(func.count(UserReview.id)).where(UserReview.is_approved.is_(False))
    )
    approved = await db.execute(
        select(func.count(UserReview.id)).where(UserReview.is_approved.is_(True))
    )
    return int(pending.scalar() or 0), int(approved.scalar() or 0)


# ============== Endpoints ==============


@router.get('', response_model=ReviewListResponse)
async def list_reviews(
    admin: User = Depends(require_permission('reviews:read')),
    db: AsyncSession = Depends(get_cabinet_db),
    review_status: str = Query('pending', alias='status', pattern='^(pending|approved|all)$'),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> ReviewListResponse:
    """Get list of reviews filtered by status."""
    query = (
        select(UserReview)
        .options(selectinload(UserReview.user))
        .order_by(UserReview.created_at.desc())
    )
    count_query = select(func.count(UserReview.id))

    if review_status == 'pending':
        query = query.where(UserReview.is_approved.is_(False))
        count_query = count_query.where(UserReview.is_approved.is_(False))
    elif review_status == 'approved':
        query = query.where(UserReview.is_approved.is_(True))
        count_query = count_query.where(UserReview.is_approved.is_(True))

    query = query.limit(limit).offset(offset)

    result = await db.execute(query)
    reviews = list(result.scalars().all())

    total_result = await db.execute(count_query)
    total = int(total_result.scalar() or 0)

    pending_count, approved_count = await _get_counts(db)

    return ReviewListResponse(
        items=[_serialize_review(r) for r in reviews],
        total=total,
        pending_count=pending_count,
        approved_count=approved_count,
        limit=limit,
        offset=offset,
    )


@router.get('/{review_id}', response_model=ReviewResponse)
async def get_review(
    review_id: int,
    admin: User = Depends(require_permission('reviews:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ReviewResponse:
    """Get a single review detail with channel preview."""
    review = await _get_review_with_user(db, review_id)
    if not review:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Review not found')
    return _serialize_review(review, include_preview=True)


@router.post('/{review_id}/approve', response_model=ReviewActionResponse)
async def approve_review_endpoint(
    review_id: int,
    admin: User = Depends(require_permission('reviews:approve')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ReviewActionResponse:
    """Approve a review and post it to the public channel."""
    existing = await _get_review_with_user(db, review_id)
    if not existing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Review not found')

    review = await crud_approve_review(db, review_id)
    if not review:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Review not found')

    channel_posted = False
    channel_error: str | None = None

    channel_id = settings.REVIEW_CHANNEL_ID
    if channel_id:
        bot = create_bot()
        try:
            channel_text = _format_review_for_channel(review)
            sent_msg = await bot.send_message(
                chat_id=channel_id,
                text=channel_text,
                parse_mode='HTML',
            )
            await set_channel_message_id(db, review.id, sent_msg.message_id)
            channel_posted = True
            logger.info(
                'Отзыв опубликован в канале из админ-кабинета',
                review_id=review.id,
                channel_message_id=sent_msg.message_id,
                admin_id=admin.id,
            )
        except Exception as exc:
            channel_error = str(exc)
            logger.error(
                'Не удалось опубликовать отзыв в канале',
                error=exc,
                review_id=review.id,
                admin_id=admin.id,
            )
        finally:
            try:
                await bot.session.close()
            except Exception:
                pass
    else:
        channel_error = 'REVIEW_CHANNEL_ID is not configured'

    refreshed = await _get_review_with_user(db, review_id)
    return ReviewActionResponse(
        success=True,
        message='Review approved',
        review=_serialize_review(refreshed, include_preview=True) if refreshed else None,
        channel_posted=channel_posted,
        channel_error=channel_error,
    )


@router.post('/{review_id}/reject', response_model=ReviewActionResponse)
async def reject_review_endpoint(
    review_id: int,
    admin: User = Depends(require_permission('reviews:reject')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ReviewActionResponse:
    """Reject and delete a review. Removes channel message if previously posted."""
    review = await _get_review_with_user(db, review_id)
    if not review:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Review not found')

    channel_message_id = review.channel_message_id
    channel_error: str | None = None

    if channel_message_id and settings.REVIEW_CHANNEL_ID:
        bot = create_bot()
        try:
            await bot.delete_message(
                chat_id=settings.REVIEW_CHANNEL_ID,
                message_id=channel_message_id,
            )
        except TelegramBadRequest as exc:
            channel_error = str(exc)
            logger.warning(
                'Не удалось удалить сообщение отзыва из канала',
                review_id=review_id,
                error=str(exc),
            )
        except Exception as exc:
            channel_error = str(exc)
            logger.error(
                'Ошибка при удалении сообщения отзыва из канала',
                review_id=review_id,
                error=str(exc),
            )
        finally:
            try:
                await bot.session.close()
            except Exception:
                pass

    deleted = await crud_reject_review(db, review_id)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Review not found')

    logger.info(
        'Отзыв отклонён из админ-кабинета',
        review_id=review_id,
        admin_id=admin.id,
    )

    return ReviewActionResponse(
        success=True,
        message='Review rejected and deleted',
        review=None,
        channel_posted=False,
        channel_error=channel_error,
    )
