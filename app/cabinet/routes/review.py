"""Cabinet routes: user-facing review submission + status.

Mirrors the Telegram bot review flow (`app/handlers/review.py`), but exposed
through the web cabinet so users can submit reviews from the SPA. Moderation
still happens in the existing admin panel — submission creates a row with
`is_approved=False`, and balance bonus (if any) is credited when the admin
approves the review.

Eligibility rules duplicated from `_check_eligibility` in handlers/review.py:
- user has no existing review (UNIQUE constraint on user_id anyway, but we
  return a friendly status code instead of letting Postgres raise);
- user has at least one active subscription;
- account age >= REVIEW_MIN_DAYS.
"""

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import get_active_subscriptions_by_user_id
from app.database.crud.system_setting import get_setting_value
from app.database.crud.user_review import (
    create_review,
    get_approved_reviews,
    get_review_by_user,
)
from app.database.models import User
from app.utils.user_utils import format_user_public_display


REVIEWS_CHANNEL_URL_KEY = 'REVIEWS_CHANNEL_URL'

from ..dependencies import get_cabinet_db, get_current_cabinet_user


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/review', tags=['Cabinet Review'])


# ============ Schemas ============


class ReviewSubmitRequest(BaseModel):
    rating: int = Field(..., ge=1, le=5)
    text: str = Field(..., min_length=10, max_length=1000)


class ReviewStatusResponse(BaseModel):
    """Current user's review status."""

    exists: bool
    is_approved: bool | None = None
    rating: int | None = None
    text: str | None = None
    bonus_kopeks: int | None = None
    created_at: datetime | None = None


class ReviewEligibilityResponse(BaseModel):
    eligible: bool
    reason: str | None = None  # 'already_left' | 'no_subscription' | 'too_new'
    wait_days: int | None = None
    min_days: int = settings.REVIEW_MIN_DAYS


class ReviewFeedItem(BaseModel):
    """Approved review for public feed."""

    rating: int
    text: str
    user_display: str | None = None
    created_at: datetime


class ReviewFeedResponse(BaseModel):
    """Approved reviews + Telegram channel link (so the SPA can offer the
    canonical 'see all in Telegram' deep link without baking the URL in)."""

    channel_url: str | None = None
    items: list[ReviewFeedItem]


# ============ Helpers ============


async def _check_eligibility(db: AsyncSession, user: User) -> ReviewEligibilityResponse:
    """Same checks as the bot side, plain enums for the SPA to localize."""
    existing = await get_review_by_user(db, user.id)
    if existing:
        return ReviewEligibilityResponse(eligible=False, reason='already_left')

    subscriptions = await get_active_subscriptions_by_user_id(db, user.id)
    if not subscriptions:
        return ReviewEligibilityResponse(eligible=False, reason='no_subscription')

    now = datetime.now(UTC)
    user_created = getattr(user, 'created_at', None)
    if user_created and user_created.tzinfo is None:
        user_created = user_created.replace(tzinfo=UTC)

    if user_created:
        member_days = (now - user_created).days
    else:
        member_days = max(
            ((now - sub.start_date).days if sub.start_date else 0)
            for sub in subscriptions
        )

    min_days = settings.REVIEW_MIN_DAYS
    if member_days < min_days:
        return ReviewEligibilityResponse(
            eligible=False,
            reason='too_new',
            wait_days=max(0, min_days - member_days),
        )

    return ReviewEligibilityResponse(eligible=True)


# ============ Routes ============


@router.get('/eligibility', response_model=ReviewEligibilityResponse)
async def get_eligibility(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ReviewEligibilityResponse:
    """Whether the user can submit a review right now and why not if they can't."""
    return await _check_eligibility(db, user)


@router.get('/me', response_model=ReviewStatusResponse)
async def get_my_review(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ReviewStatusResponse:
    """Returns the current user's review (if any) so the SPA can render
    "pending moderation" / "approved" / "submit new" states."""
    review = await get_review_by_user(db, user.id)
    if not review:
        return ReviewStatusResponse(exists=False)
    return ReviewStatusResponse(
        exists=True,
        is_approved=review.is_approved,
        rating=review.rating,
        text=review.text,
        bonus_kopeks=review.bonus_kopeks,
        created_at=review.created_at,
    )


@router.post('', response_model=ReviewStatusResponse)
async def submit_review(
    payload: ReviewSubmitRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ReviewStatusResponse:
    """Create a pending review. Bonus is paid out only on admin approval (see
    `on_approve_review` in handlers/admin/reviews.py)."""
    eligibility = await _check_eligibility(db, user)
    if not eligibility.eligible:
        # Map enum reason to a stable HTTP code for the SPA.
        # 409 = state conflict (already left), 412 = precondition (no sub / too new).
        code = (
            status.HTTP_409_CONFLICT
            if eligibility.reason == 'already_left'
            else status.HTTP_412_PRECONDITION_FAILED
        )
        raise HTTPException(
            status_code=code,
            detail={'reason': eligibility.reason, 'wait_days': eligibility.wait_days},
        )

    bonus_kopeks = settings.REVIEW_BONUS_KOPEKS  # may be 0 — admin disabled it
    review = await create_review(
        db=db,
        user_id=user.id,
        rating=payload.rating,
        text=payload.text.strip(),
        bonus_kopeks=bonus_kopeks,
        # Cabinet submissions have no telegram chat to forward from — the
        # admin channel forward path will skip these (handles None gracefully).
        source_chat_id=None,
        source_message_id=None,
    )

    logger.info(
        'Cabinet review submitted',
        user_id=user.id,
        review_id=review.id,
        rating=review.rating,
    )

    return ReviewStatusResponse(
        exists=True,
        is_approved=review.is_approved,
        rating=review.rating,
        text=review.text,
        bonus_kopeks=review.bonus_kopeks,
        created_at=review.created_at,
    )


@router.get('/feed', response_model=ReviewFeedResponse)
async def get_feed(
    limit: int = 10,
    _user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ReviewFeedResponse:
    """Last N approved reviews for the public feed widget plus the public
    Telegram channel URL (mirrors what `app/handlers/admin/reviews.py` posts
    on approve). Caller must be authenticated (cabinet-internal endpoint)."""
    limit = max(1, min(limit, 50))
    reviews = await get_approved_reviews(db, limit=limit)

    items: list[ReviewFeedItem] = []
    for r in reviews:
        # Shared helper: @username for Telegram users, first_name, anonymized
        # email for site-only users, or `Пользователь #N` as last resort.
        display = format_user_public_display(getattr(r, 'user', None))
        items.append(
            ReviewFeedItem(
                rating=r.rating,
                text=r.text,
                user_display=display,
                created_at=r.created_at,
            )
        )

    # Channel URL stored in system_settings (admin-editable). May be None if
    # the admin hasn't configured a channel yet — SPA hides the link button.
    channel_url = await get_setting_value(db, REVIEWS_CHANNEL_URL_KEY)
    if channel_url is not None:
        channel_url = channel_url.strip() or None

    return ReviewFeedResponse(channel_url=channel_url, items=items)
