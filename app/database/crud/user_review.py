import structlog
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import UserReview


logger = structlog.get_logger(__name__)


async def create_review(
    db: AsyncSession,
    user_id: int,
    rating: int,
    text: str,
    bonus_kopeks: int,
    source_chat_id: int | None = None,
    source_message_id: int | None = None,
) -> UserReview:
    review = UserReview(
        user_id=user_id,
        rating=rating,
        text=text,
        bonus_kopeks=bonus_kopeks,
        source_chat_id=source_chat_id,
        source_message_id=source_message_id,
    )
    db.add(review)
    await db.commit()
    await db.refresh(review)
    return review


async def get_review_by_user(db: AsyncSession, user_id: int) -> UserReview | None:
    result = await db.execute(
        select(UserReview).where(UserReview.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def get_pending_reviews(db: AsyncSession) -> list[UserReview]:
    result = await db.execute(
        select(UserReview)
        .where(UserReview.is_approved == False)  # noqa: E712
        .options(selectinload(UserReview.user))
        .order_by(UserReview.created_at.asc())
    )
    return list(result.scalars().all())


async def get_approved_reviews(db: AsyncSession, limit: int = 5) -> list[UserReview]:
    result = await db.execute(
        select(UserReview)
        .where(UserReview.is_approved == True)  # noqa: E712
        .options(selectinload(UserReview.user))
        .order_by(UserReview.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def approve_review(db: AsyncSession, review_id: int) -> UserReview | None:
    result = await db.execute(
        select(UserReview)
        .where(UserReview.id == review_id)
        .options(selectinload(UserReview.user))
    )
    review = result.scalar_one_or_none()
    if review is None:
        return None
    review.is_approved = True
    await db.commit()
    await db.refresh(review)
    return review


async def reject_review(db: AsyncSession, review_id: int) -> bool:
    result = await db.execute(
        select(UserReview).where(UserReview.id == review_id)
    )
    review = result.scalar_one_or_none()
    if review is None:
        return False
    await db.delete(review)
    await db.commit()
    return True


async def set_channel_message_id(db: AsyncSession, review_id: int, message_id: int) -> None:
    await db.execute(
        update(UserReview)
        .where(UserReview.id == review_id)
        .values(channel_message_id=message_id)
    )
    await db.commit()
