"""Service for atomic contest attempt operations."""

from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.contest import create_attempt, get_attempt, update_attempt
from app.database.crud.subscription import extend_subscription, get_subscription_by_user_id
from app.database.crud.user import get_user_by_id
from app.database.models import ContestAttempt, ContestRound, ContestTemplate
from app.services.contests.enums import PrizeType
from app.services.contests.games import get_game_strategy


logger = structlog.get_logger(__name__)


@dataclass
class AttemptResult:
    """Result of processing a contest attempt."""

    success: bool
    is_winner: bool
    message: str
    already_played: bool = False
    round_finished: bool = False


class ContestAttemptService:
    """Service for processing contest attempts with atomic operations."""

    async def process_button_attempt(
        self,
        db: AsyncSession,
        round_obj: ContestRound,
        user_id: int,
        pick: str,
        language: str,
    ) -> AttemptResult:
        """
        Process a button-based game attempt atomically.

        Args:
            db: Database session
            round_obj: Contest round
            user_id: User ID
            pick: User's pick (button callback data)
            language: User's language

        Returns:
            AttemptResult with outcome details
        """
        tpl = round_obj.template
        if not tpl:
            return AttemptResult(
                success=False,
                is_winner=False,
                message='Конкурс не найден',
            )

        # Check if user already played
        existing_attempt = await get_attempt(db, round_obj.id, user_id)
        if existing_attempt:
            return AttemptResult(
                success=False,
                is_winner=False,
                message='У вас уже была попытка',
                already_played=True,
            )

        # Get game strategy and check answer
        strategy = get_game_strategy(tpl.slug)
        if not strategy:
            return AttemptResult(
                success=False,
                is_winner=False,
                message='Тип игры не поддерживается',
            )

        check_result = strategy.check_answer(pick, round_obj.payload or {}, language)
        is_winner = check_result.is_correct

        # Atomic winner check with row lock
        is_winner = await self._atomic_winner_check(db, round_obj.id, is_winner)

        # Create attempt record
        await create_attempt(
            db,
            round_id=round_obj.id,
            user_id=user_id,
            answer=str(pick),
            is_winner=is_winner,
        )

        logger.info(
            "Contest attempt: user , round , pick '', winner",
            user_id=user_id,
            round_obj_id=round_obj.id,
            pick=pick,
            is_winner=is_winner,
        )

        if is_winner:
            prize_msg = await self._award_prize(db, user_id, tpl, language)
            return AttemptResult(
                success=True,
                is_winner=True,
                message=f'🎉 Победа! {prize_msg}' if prize_msg else '🎉 Победа!',
            )

        return AttemptResult(
            success=True,
            is_winner=False,
            message=check_result.response_text or 'Неудача',
        )

    async def process_text_attempt(
        self,
        db: AsyncSession,
        round_obj: ContestRound,
        user_id: int,
        text_answer: str,
        language: str,
    ) -> AttemptResult:
        """
        Process a text-input game attempt atomically.

        Args:
            db: Database session
            round_obj: Contest round
            user_id: User ID
            text_answer: User's text answer
            language: User's language

        Returns:
            AttemptResult with outcome details
        """
        tpl = round_obj.template
        if not tpl:
            return AttemptResult(
                success=False,
                is_winner=False,
                message='Конкурс не найден',
            )

        # For text games, attempt should already exist (created in render phase)
        attempt = await get_attempt(db, round_obj.id, user_id)
        if not attempt:
            return AttemptResult(
                success=False,
                is_winner=False,
                message='Сначала начните игру',
            )

        # Check if already answered
        if attempt.answer is not None:
            return AttemptResult(
                success=False,
                is_winner=False,
                message='У вас уже была попытка',
                already_played=True,
            )

        # Get game strategy and check answer
        strategy = get_game_strategy(tpl.slug)
        if not strategy:
            return AttemptResult(
                success=False,
                is_winner=False,
                message='Тип игры не поддерживается',
            )

        check_result = strategy.check_answer(text_answer, round_obj.payload or {}, language)
        is_winner = check_result.is_correct

        # Atomic winner check with row lock
        is_winner = await self._atomic_winner_check(db, round_obj.id, is_winner)

        # Update attempt with answer
        await update_attempt(db, attempt, answer=text_answer.strip().upper(), is_winner=is_winner)

        logger.info(
            "Contest text attempt: user , round , answer '', winner",
            user_id=user_id,
            round_obj_id=round_obj.id,
            text_answer=text_answer,
            is_winner=is_winner,
        )

        if is_winner:
            prize_msg = await self._award_prize(db, user_id, tpl, language)
            return AttemptResult(
                success=True,
                is_winner=True,
                message=f'🎉 Победа! {prize_msg}' if prize_msg else '🎉 Победа!',
            )

        return AttemptResult(
            success=True,
            is_winner=False,
            message=check_result.response_text or 'Неверно, попробуй в следующем раунде',
        )

    async def create_pending_attempt(
        self,
        db: AsyncSession,
        round_id: int,
        user_id: int,
    ) -> ContestAttempt | None:
        """
        Create a pending attempt for text-input games.
        This blocks re-entry while user is answering.

        Args:
            db: Database session
            round_id: Round ID
            user_id: User ID

        Returns:
            Created attempt or None if already exists
        """
        existing = await get_attempt(db, round_id, user_id)
        if existing:
            return None

        return await create_attempt(
            db,
            round_id=round_id,
            user_id=user_id,
            answer=None,
            is_winner=False,
        )

    async def _atomic_winner_check(
        self,
        db: AsyncSession,
        round_id: int,
        is_winner: bool,
    ) -> bool:
        """
        Atomically check and increment winner count.
        Uses SELECT FOR UPDATE to prevent race conditions.

        Args:
            db: Database session
            round_id: Round ID
            is_winner: Whether user answered correctly

        Returns:
            True if user is a winner, False if max winners reached
        """
        if not is_winner:
            return False

        stmt = select(ContestRound).where(ContestRound.id == round_id).with_for_update()
        result = await db.execute(stmt)
        round_obj = result.scalar_one()

        if round_obj.winners_count >= round_obj.max_winners:
            return False

        round_obj.winners_count += 1
        await db.commit()
        return True

    async def _award_prize(
        self,
        db: AsyncSession,
        user_id: int,
        template: ContestTemplate,
        language: str,
    ) -> str:
        """
        Award prize to winner.

        Args:
            db: Database session
            user_id: Winner user ID
            template: Contest template with prize info
            language: User's language

        Returns:
            Prize notification message
        """
        from app.localization.texts import get_texts

        texts = get_texts(language)

        prize_type = template.prize_type or PrizeType.DAYS.value
        prize_value = template.prize_value or '1'

        if prize_type == PrizeType.DAYS.value:
            if settings.is_multi_tariff_enabled():
                from app.database.crud.subscription import get_active_subscriptions_by_user_id

                active_subs = await get_active_subscriptions_by_user_id(db, user_id)
                # Contest prize: prefer non-daily subscription with most days left
                non_daily = [s for s in active_subs if not (s.tariff and getattr(s.tariff, 'is_daily', False))]
                eligible = non_daily or active_subs
                subscription = max(eligible, key=lambda s: s.days_left) if eligible else None
            else:
                subscription = await get_subscription_by_user_id(db, user_id)
            if not subscription:
                return ''
            days = int(prize_value) if prize_value.isdigit() else 1
            await extend_subscription(db, subscription, days)
            tariff_name = getattr(subscription.tariff, 'name', None) if subscription.tariff else None
            prize_text = texts.t('CONTEST_PRIZE_GRANTED', 'Бонус {days} дней зачислен!').format(days=days)
            if tariff_name:
                prize_text += f' (подписка "{tariff_name}")'
            return prize_text

        if prize_type == PrizeType.BALANCE.value:
            user = await get_user_by_id(db, user_id)
            if not user:
                return ''
            kopeks = int(prize_value) if prize_value.isdigit() else 0
            if kopeks > 0:
                from app.database.crud.user import lock_user_for_update

                user = await lock_user_for_update(db, user)
                user.balance_kopeks += kopeks
                await db.commit()
                return texts.t('CONTEST_BALANCE_GRANTED', 'Бонус {amount} зачислен!').format(
                    amount=settings.format_price(kopeks)
                )

        elif prize_type == PrizeType.CUSTOM.value:
            return f'🎁 {prize_value}'

        return ''


# Singleton instance
contest_attempt_service = ContestAttemptService()
