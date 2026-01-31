"""
Базовый класс репозитория.

Репозитории:
- Инкапсулируют SQL логику
- НЕ делают commit (это задача UoW)
- Работают с одной сущностью (или тесно связанными)
"""
from __future__ import annotations

import logging
from typing import Any, Generic, TypeVar

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Base


logger = logging.getLogger(__name__)

ModelType = TypeVar('ModelType', bound=Base)


class BaseRepository(Generic[ModelType]):
    """
    Базовый репозиторий с общими CRUD операциями.

    Наследники переопределяют model_class и добавляют
    специфичные методы.
    """

    model_class: type[ModelType] = None  # Переопределить в наследнике

    def __init__(self, session: AsyncSession):
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session

    async def get_by_id(self, id: int) -> ModelType | None:
        """Получить по ID."""
        return await self._session.get(self.model_class, id)

    async def get_all(
        self,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> list[ModelType]:
        """Получить все с пагинацией."""
        result = await self._session.execute(
            select(self.model_class)
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def count(self) -> int:
        """Подсчитать общее количество."""
        result = await self._session.execute(
            select(func.count()).select_from(self.model_class)
        )
        return result.scalar_one()

    def add(self, entity: ModelType) -> ModelType:
        """
        Добавить сущность в сессию.

        Возвращает ту же сущность (для chaining).
        После flush() у неё будет ID.
        """
        self._session.add(entity)
        return entity

    def add_all(self, entities: list[ModelType]) -> list[ModelType]:
        """Добавить несколько сущностей."""
        self._session.add_all(entities)
        return entities

    async def delete(self, entity: ModelType) -> None:
        """Удалить сущность."""
        await self._session.delete(entity)

    async def refresh(self, entity: ModelType) -> ModelType:
        """
        Обновить сущность из БД.

        Полезно после flush() для получения сгенерированных значений.
        """
        await self._session.refresh(entity)
        return entity

    async def create(self, **kwargs: Any) -> ModelType:
        """
        Создать новую сущность.

        Не делает commit - только добавляет в сессию.
        Делает flush для получения ID.
        """
        entity = self.model_class(**kwargs)
        self.add(entity)
        await self._session.flush()
        await self._session.refresh(entity)
        return entity

    async def update(self, entity: ModelType, **kwargs: Any) -> ModelType:
        """
        Обновить сущность.

        Не делает commit - только меняет атрибуты.
        """
        for key, value in kwargs.items():
            if hasattr(entity, key):
                setattr(entity, key, value)
        await self._session.flush()
        return entity
