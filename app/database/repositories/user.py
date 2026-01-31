"""
Репозиторий пользователей.

Не делает commit - это ответственность UnitOfWork.
"""
from __future__ import annotations

import logging
import secrets
import string
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.database.models import (
    Subscription,
    User,
    UserPromoGroup,
    UserStatus,
)
from app.database.repositories.base import BaseRepository
from app.utils.validators import sanitize_telegram_name


logger = logging.getLogger(__name__)


def _generate_referral_code() -> str:
    """Генерация уникального реферального кода."""
    alphabet = string.ascii_letters + string.digits
    code_suffix = ''.join(secrets.choice(alphabet) for _ in range(8))
    return f'ref{code_suffix}'


class UserRepository(BaseRepository[User]):
    """Репозиторий для работы с пользователями."""

    model_class = User

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        """Получить пользователя по Telegram ID."""
        result = await self._session.execute(
            select(User)
            .options(
                selectinload(User.subscription),
                selectinload(User.user_promo_groups).selectinload(UserPromoGroup.promo_group),
                selectinload(User.referrer),
                selectinload(User.promo_group),
            )
            .where(User.telegram_id == telegram_id)
        )
        return result.scalar_one_or_none()

    async def get_by_username(self, username: str) -> User | None:
        """Получить пользователя по username."""
        result = await self._session.execute(
            select(User)
            .options(selectinload(User.subscription))
            .where(User.username == username)
        )
        return result.scalar_one_or_none()

    async def get_by_referral_code(self, referral_code: str) -> User | None:
        """Получить пользователя по реферальному коду."""
        result = await self._session.execute(
            select(User).where(User.referral_code == referral_code)
        )
        return result.scalar_one_or_none()

    async def create_user(
        self,
        telegram_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        language_code: str | None = None,
        referrer_id: int | None = None,
        promo_group_id: int | None = None,
    ) -> User:
        """
        Создать нового пользователя.

        Не делает commit - только добавляет в сессию.
        """
        # Санитизация имён
        safe_username = sanitize_telegram_name(username) if username else None
        safe_first_name = sanitize_telegram_name(first_name) if first_name else None
        safe_last_name = sanitize_telegram_name(last_name) if last_name else None

        user = User(
            telegram_id=telegram_id,
            username=safe_username,
            first_name=safe_first_name,
            last_name=safe_last_name,
            language_code=language_code,
            referrer_id=referrer_id,
            promo_group_id=promo_group_id,
            referral_code=_generate_referral_code(),
            status=UserStatus.ACTIVE.value,
            balance_kopeks=0,
        )

        self.add(user)
        await self._session.flush()
        await self._session.refresh(user)

        logger.info(
            'Создан пользователь %s (telegram_id=%s)',
            user.id,
            telegram_id,
        )

        return user

    async def update_balance(self, user: User, delta_kopeks: int) -> User:
        """
        Изменить баланс пользователя.

        delta_kopeks: положительное для пополнения, отрицательное для списания.
        """
        user.balance_kopeks = (user.balance_kopeks or 0) + delta_kopeks
        user.updated_at = datetime.utcnow()
        await self._session.flush()
        return user

    async def add_balance(self, user: User, amount_kopeks: int) -> User:
        """Пополнить баланс."""
        return await self.update_balance(user, amount_kopeks)

    async def subtract_balance(self, user: User, amount_kopeks: int) -> User:
        """
        Списать с баланса.

        Raises:
            ValueError: Если недостаточно средств.
        """
        if (user.balance_kopeks or 0) < amount_kopeks:
            raise ValueError(
                f'Недостаточно средств: {user.balance_kopeks} < {amount_kopeks}'
            )
        return await self.update_balance(user, -amount_kopeks)

    async def set_status(self, user: User, status: UserStatus) -> User:
        """Установить статус пользователя."""
        user.status = status.value
        user.updated_at = datetime.utcnow()
        await self._session.flush()
        return user

    async def count_active(self) -> int:
        """Подсчитать активных пользователей."""
        result = await self._session.execute(
            select(func.count())
            .select_from(User)
            .where(User.status == UserStatus.ACTIVE.value)
        )
        return result.scalar_one()

    async def get_referrals(self, user_id: int, limit: int = 100) -> list[User]:
        """Получить рефералов пользователя."""
        result = await self._session.execute(
            select(User)
            .options(selectinload(User.subscription))
            .where(User.referrer_id == user_id)
            .order_by(User.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())
