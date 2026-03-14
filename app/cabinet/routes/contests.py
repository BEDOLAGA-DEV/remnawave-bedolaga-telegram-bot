"""Contests routes for cabinet - user participation in games/contests."""

import random
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.contest import (
    create_attempt,
    get_active_rounds,
    get_attempt,
    increment_winner_count,
)
from app.database.crud.subscription import get_subscription_by_user_id
from app.database.models import SubscriptionStatus, User
from app.services.contest_rotation_service import (
    GAME_ANAGRAM,
    GAME_BLITZ,
    GAME_CIPHER,
    GAME_EMOJI,
    GAME_LOCKS,
    GAME_QUEST,
    GAME_SERVER,
)

from ..dependencies import get_cabinet_db, get_current_cabinet_user


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/contests', tags=['Cabinet Contests'])


# ============ Schemas ============


class ContestInfo(BaseModel):
    """Contest/game info."""

    id: int
    slug: str
    name: str
    description: str | None = None
    prize_type: str
    prize_value: str
    is_available: bool
    already_played: bool = False


class ContestGameData(BaseModel):
    """Data for playing a contest game."""

    round_id: int
    game_type: str
    game_data: dict[str, Any]
    instructions: str


class ContestAnswerRequest(BaseModel):
    """Request to submit contest answer."""

    round_id: int
    answer: str


class ContestResult(BaseModel):
    """Result of contest attempt."""

    is_winner: bool
    message: str
    prize_type: str | None = None
    prize_value: str | None = None


# ============ Helpers ============


def _user_allowed(subscription) -> bool:
    """Check if user is allowed to participate in contests."""
    if not subscription:
        return False
    return subscription.status in {
        SubscriptionStatus.ACTIVE.value,
        SubscriptionStatus.TRIAL.value,
        SubscriptionStatus.LIMITED.value,
    }


async def _award_prize(db: AsyncSession, user_id: int, prize_type: str, prize_value: str) -> str:
    """Award prize to winner."""
    if prize_type == 'days':
        try:
            days = int(prize_value)
        except ValueError:
            return 'Error: invalid prize value'

        subscription = await get_subscription_by_user_id(db, user_id)
        if not subscription:
            return 'Error: subscription not found'

        subscription.end_date = subscription.end_date + timedelta(days=days)
        subscription.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(subscription)

        logger.info('🎁 Extended subscription for user by days (contest prize)', user_id=user_id, days=days)
        return f'Subscription extended by {days} days'

    if prize_type == 'balance':
        from app.database.crud.user import get_user_by_id

        try:
            amount = float(prize_value)
        except ValueError:
            return 'Error: invalid prize value'

        user = await get_user_by_id(db, user_id)
        if not user:
            return 'Error: user not found'

        from app.database.crud.user import lock_user_for_update

        user = await lock_user_for_update(db, user)
        user.balance_kopeks += int(round(amount * 100))
        await db.commit()
        await db.refresh(user)

        logger.info('🎁 Added to balance for user (contest prize)', amount=amount, user_id=user_id)
        return f'Balance increased by {amount}'

    logger.warning('Unknown prize type', prize_type=prize_type)
    return f"Prize type '{prize_type}' not supported"


# ============ Routes ============


class ContestsCountResponse(BaseModel):
    """Count of available contests."""

    count: int


@router.get('/count', response_model=ContestsCountResponse)
async def get_contests_count(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get count of contests available for the user."""
    subscription = await get_subscription_by_user_id(db, user.id)

    if not _user_allowed(subscription):
        return ContestsCountResponse(count=0)

    active_rounds = await get_active_rounds(db)

    # Count unique available contests (not yet played)
    count = 0
    seen_templates = set()
    for rnd in active_rounds:
        if not rnd.template or not rnd.template.is_enabled:
            continue
        tpl_slug = rnd.template.slug if rnd.template else ''
        if tpl_slug in seen_templates:
            continue
        seen_templates.add(tpl_slug)

        # Check if user already played this round
        attempt = await get_attempt(db, rnd.id, user.id)
        if not attempt:
            count += 1

    return ContestsCountResponse(count=count)


@router.get('', response_model=list[ContestInfo])
async def get_contests(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get list of available contests/games."""
    subscription = await get_subscription_by_user_id(db, user.id)

    if not _user_allowed(subscription):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Contests are only available for users with active or trial subscriptions',
        )

    active_rounds = await get_active_rounds(db)

    # Group by template to avoid duplicates
    unique_templates = {}
    for rnd in active_rounds:
        if not rnd.template or not rnd.template.is_enabled:
            continue
        tpl_slug = rnd.template.slug if rnd.template else ''
        if tpl_slug not in unique_templates:
            unique_templates[tpl_slug] = rnd

    contests = []
    for tpl_slug, rnd in unique_templates.items():
        # Check if user already played this round
        attempt = await get_attempt(db, rnd.id, user.id)

        contests.append(
            ContestInfo(
                id=rnd.id,
                slug=tpl_slug,
                name=rnd.template.name if rnd.template else tpl_slug,
                description=rnd.template.description if rnd.template else None,
                prize_type=rnd.template.prize_type if rnd.template else 'days',
                prize_value=rnd.template.prize_value if rnd.template else '1',
                is_available=True,
                already_played=attempt is not None,
            )
        )

    return contests


@router.get('/{round_id}', response_model=ContestGameData)
async def get_contest_game(
    round_id: int,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get game data for a specific contest round."""
    subscription = await get_subscription_by_user_id(db, user.id)

    if not _user_allowed(subscription):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Contests are only available for users with active or trial subscriptions',
        )

    active_rounds = await get_active_rounds(db)
    round_obj = next((r for r in active_rounds if r.id == round_id), None)

    if not round_obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Contest round not found or already finished',
        )

    if not round_obj.template or not round_obj.template.is_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='This contest is disabled',
        )

    # Check if already played
    attempt = await get_attempt(db, round_id, user.id)
    if attempt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='You have already played this round',
        )

    tpl = round_obj.template
    game_type = tpl.slug
    game_data = {}
    instructions = ''

    if game_type == GAME_QUEST:
        rows = round_obj.payload.get('rows', 3)
        cols = round_obj.payload.get('cols', 3)
        secret = random.randint(0, rows * cols - 1)
        game_data = {
            'rows': rows,
            'cols': cols,
            'secret': secret,
            'grid_size': rows * cols,
        }
        instructions = 'Select one of the nodes in the grid. Find the hidden server!'

    elif game_type == GAME_LOCKS:
        total = round_obj.payload.get('total', 20)
        secret = random.randint(0, total - 1)
        game_data = {
            'total': total,
            'secret': secret,
        }
        instructions = 'Find the unlocked button among the locks!'

    elif game_type == GAME_SERVER:
        flags = round_obj.payload.get('flags') or []
        shuffled_flags = flags.copy()
        random.shuffle(shuffled_flags)
        game_data = {
            'flags': shuffled_flags,
        }
        instructions = 'Choose a server by clicking on a flag!'

    elif game_type == GAME_CIPHER:
        question = round_obj.payload.get('question', '')
        game_data = {
            'question': question,
            'input_type': 'text',
        }
        instructions = 'Decrypt the cipher and enter the answer!'

    elif game_type == GAME_EMOJI:
        question = round_obj.payload.get('question', '🤔')
        emoji_list = question.split()
        random.shuffle(emoji_list)
        game_data = {
            'question': ' '.join(emoji_list),
            'input_type': 'text',
        }
        instructions = 'Guess the service by emojis!'

    elif game_type == GAME_ANAGRAM:
        letters = round_obj.payload.get('letters', '')
        game_data = {
            'letters': letters,
            'input_type': 'text',
        }
        instructions = 'Make a word from the given letters!'

    elif game_type == GAME_BLITZ:
        game_data = {
            'button_text': "I'm here!",
        }
        instructions = 'Click the button as fast as you can!'

    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Unknown contest type',
        )

    return ContestGameData(
        round_id=round_id,
        game_type=game_type,
        game_data=game_data,
        instructions=instructions,
    )


@router.post('/{round_id}/answer', response_model=ContestResult)
async def submit_contest_answer(
    round_id: int,
    request: ContestAnswerRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Submit answer for a contest round."""
    subscription = await get_subscription_by_user_id(db, user.id)

    if not _user_allowed(subscription):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Contests are only available for users with active or trial subscriptions',
        )

    active_rounds = await get_active_rounds(db)
    round_obj = next((r for r in active_rounds if r.id == round_id), None)

    if not round_obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Contest round not found or already finished',
        )

    # Check if already played
    attempt = await get_attempt(db, round_id, user.id)
    if attempt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='You have already played this round',
        )

    tpl = round_obj.template
    answer = request.answer
    is_winner = False

    # Determine if winner based on game type
    if tpl.slug == GAME_SERVER:
        flags = round_obj.payload.get('flags') or []
        secret_idx = round_obj.payload.get('secret_idx')
        correct_flag = flags[secret_idx] if secret_idx is not None and secret_idx < len(flags) else ''
        is_winner = answer == correct_flag

    elif tpl.slug in {GAME_QUEST, GAME_LOCKS}:
        try:
            parts = answer.split('_')
            if len(parts) >= 2:
                idx = int(parts[0])
                secret = int(parts[1])
                is_winner = idx == secret
        except (ValueError, IndexError):
            is_winner = False

    elif tpl.slug == GAME_BLITZ:
        is_winner = answer.lower() == 'blitz'

    elif tpl.slug in {GAME_CIPHER, GAME_EMOJI, GAME_ANAGRAM}:
        correct = (round_obj.payload.get('answer') or '').upper()
        is_winner = correct and answer.upper() == correct

    # Record attempt
    await create_attempt(db, round_id=round_obj.id, user_id=user.id, answer=str(answer), is_winner=is_winner)

    if is_winner:
        await increment_winner_count(db, round_obj)
        prize_text = await _award_prize(db, user.id, tpl.prize_type, tpl.prize_value)
        return ContestResult(
            is_winner=True,
            message=f'🎉 Congratulations! You won! {prize_text}',
            prize_type=tpl.prize_type,
            prize_value=tpl.prize_value,
        )
    lose_messages = {
        GAME_QUEST: ['Empty node', 'Wrong server', 'Try another'],
        GAME_LOCKS: ['Locked', 'No access', 'Try again'],
        GAME_SERVER: ['Server overloaded', 'No response', 'Try tomorrow'],
    }
    messages = lose_messages.get(tpl.slug, ['Incorrect', 'Try again next round'])
    return ContestResult(
        is_winner=False,
        message=random.choice(messages),
    )
