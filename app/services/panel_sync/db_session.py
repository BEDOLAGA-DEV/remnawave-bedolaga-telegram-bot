"""Транзакция базы на время долгих походов в панель.

Открытая транзакция, пока сессия минутами ждёт панель (страницы по 500, паузы по
429 — десятки минут), — гарантированный обрыв соединения по простою: так падал
проход «в панель» (0715b5c7), тем же грозил импорт из панели. Поэтому перед
сетевой фазой транзакцию закрываем (пул с pre_ping выдаст живое соединение на
следующее чтение), а после сбоя откатываем, чтобы сессия осталась пригодной для
следующего шага полной синхронизации.
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


async def release_transaction(db: AsyncSession) -> None:
    """Закрыть транзакцию, которую сессия принесла от вызывающего.

    Из кабинета это чтение авторизации, из бота — middleware; свои изменения
    вызывающий тем самым тоже фиксирует, как и батчевые коммиты внутри синка.
    """
    try:
        await db.commit()
    except Exception as error:
        logger.warning('Не удалось закрыть транзакцию перед походом в панель — откат', error=str(error)[:200])
        await rollback_quietly(db)


async def rollback_quietly(db: AsyncSession) -> None:
    """Откатить сессию после сбоя; неудача самого отката — только в debug-лог."""
    try:
        await db.rollback()
    except Exception as error:
        logger.debug('Откат после сбоя не удался', error=str(error)[:200])
