"""
Unit of Work pattern для атомарных транзакций.

Использование:

    async with UnitOfWork() as uow:
        user = await uow.users.create(...)
        subscription = await uow.subscriptions.create(user_id=user.id, ...)
        await uow.transactions.create(...)
        await uow.commit()  # Один commit на всю операцию

    # Если что-то падает - автоматический rollback

Постепенная миграция:
- Новый код пишем с UoW
- Старый код продолжает работать через CRUD с автокоммитами
- Критичные места (платежи, подписки) переводим на UoW в первую очередь
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Self

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.database import AsyncSessionLocal

if TYPE_CHECKING:
    from app.database.repositories.base import BaseRepository


logger = logging.getLogger(__name__)


class UnitOfWork:
    """
    Unit of Work - управляет транзакцией и репозиториями.

    Гарантирует атомарность: либо все операции успешны, либо ни одна.
    """

    def __init__(self, session_factory=AsyncSessionLocal):
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self._repositories: dict[str, BaseRepository] = {}

    @property
    def session(self) -> AsyncSession:
        """Текущая сессия. Доступна только внутри контекста."""
        if self._session is None:
            raise RuntimeError('UnitOfWork not started. Use `async with UnitOfWork() as uow:`')
        return self._session

    async def __aenter__(self) -> Self:
        self._session = self._session_factory()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is not None:
            await self.rollback()
            logger.warning('UoW: rollback due to %s: %s', exc_type.__name__, exc_val)
        await self._session.close()
        self._session = None
        self._repositories.clear()

    async def commit(self) -> None:
        """Зафиксировать все изменения."""
        await self._session.commit()

    async def rollback(self) -> None:
        """Откатить все изменения."""
        await self._session.rollback()

    async def flush(self) -> None:
        """
        Отправить изменения в БД без коммита.

        Полезно когда нужно получить ID созданного объекта
        до финального коммита.
        """
        await self._session.flush()

    def _get_repository(self, repo_class: type[BaseRepository], name: str) -> BaseRepository:
        """Ленивая инициализация репозитория."""
        if name not in self._repositories:
            self._repositories[name] = repo_class(self.session)
        return self._repositories[name]

    # =========================================================================
    # Репозитории (добавляем по мере рефакторинга)
    # =========================================================================

    @property
    def users(self):
        """Репозиторий пользователей."""
        from app.database.repositories.user import UserRepository
        return self._get_repository(UserRepository, 'users')

    @property
    def subscriptions(self):
        """Репозиторий подписок."""
        from app.database.repositories.subscription import SubscriptionRepository
        return self._get_repository(SubscriptionRepository, 'subscriptions')

    @property
    def transactions(self):
        """Репозиторий транзакций (финансовых)."""
        from app.database.repositories.transaction import TransactionRepository
        return self._get_repository(TransactionRepository, 'transactions')


# =============================================================================
# Совместимость со старым кодом
# =============================================================================


@asynccontextmanager
async def atomic():
    """
    Контекстный менеджер для атомарных операций.

    Упрощённый вариант для случаев когда не нужны репозитории:

        async with atomic() as session:
            # работаем с session напрямую
            session.add(obj)
        # автоматический commit или rollback
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# =============================================================================
# Dependency для FastAPI
# =============================================================================


async def get_uow():
    """
    FastAPI dependency для инъекции UnitOfWork.

    Использование:

        @router.post('/subscriptions')
        async def create_subscription(uow: UnitOfWork = Depends(get_uow)):
            async with uow:
                ...
                await uow.commit()
    """
    return UnitOfWork()
