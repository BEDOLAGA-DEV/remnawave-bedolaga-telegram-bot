"""Массовые проходы синхронизации.

Здесь живёт только механика прохода: пачки подписок из базы, ограничение
параллельности, грейс-лиза на каждую подписку и подсчёт итогов. Что именно
отправлять в панель, решает ``push_subscription``.

Раньше эта механика была вплетена в трёхсотстрочный метод сервиса вместе со
сборкой запроса, поиском аккаунта и обработкой ошибок панели — и именно поэтому
расходилась с кнопками, которые делали то же самое по-своему.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass

import structlog
from sqlalchemy.exc import DBAPIError

from app.external.remnawave_api import RemnaWaveTransientError
from app.services.panel_sync.writer import push_subscription


logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SyncStats:
    """Итоги прохода. Ключи словаря совпадают с прежними — их читают кнопки."""

    created: int = 0
    updated: int = 0
    errors: int = 0
    skipped: int = 0

    def as_dict(self) -> dict[str, int]:
        return {'created': self.created, 'updated': self.updated, 'errors': self.errors}


async def _load_batch(db, get_subscriptions_batch, *, offset: int, limit: int):
    """Прочитать пачку и сразу закрыть транзакцию чтения.

    Дальше минуты сетевых запросов к панели (с паузами по 429 — десятки минут);
    открытая на это время транзакция — гарантированный обрыв соединения по
    простою. Обрыв при самом чтении — повтор один раз на свежем соединении.
    """
    for attempt in (1, 2):
        try:
            subscriptions = await get_subscriptions_batch(db, offset=offset, limit=limit)
            await db.commit()
            return subscriptions
        except DBAPIError as error:
            logger.warning(
                'Соединение с базой потеряно при чтении пачки подписок — повтор',
                offset=offset,
                attempt=attempt,
                error=str(error)[:200],
            )
            try:
                await db.rollback()
            except Exception as rollback_error:
                logger.debug('Откат после обрыва не удался', error=str(rollback_error)[:200])
            if attempt == 2:
                raise
    return []


async def push_all_subscriptions(
    db,
    api,
    *,
    batch_size: int = 500,
    concurrency: int = 5,
) -> SyncStats:
    """Отправить в панель все подписки бота.

    Пачками по ``batch_size`` с фиксацией после каждой: проход по большой базе
    идёт минутами, и терять уже сделанное из-за одной ошибки в конце нельзя.
    """
    # Поздние импорты: тесты подменяют эти имена в своих модулях, а раннер должен
    # видеть подмену — как это делал прежний метод сервиса.
    from app.database.crud.subscription import get_subscriptions_batch
    from app.services.grace_access_runtime import grace_sensitive_panel_update

    created = updated = errors = skipped = 0
    offset = 0
    semaphore = asyncio.Semaphore(concurrency)

    while True:
        subscriptions = await _load_batch(db, get_subscriptions_batch, offset=offset, limit=batch_size)
        if not subscriptions:
            break

        # Подписка без пользователя — осиротевшая строка: в панель её отправлять
        # не от чьего имени.
        valid = [subscription for subscription in subscriptions if subscription.user]
        if not valid:
            if len(subscriptions) < batch_size:
                break
            offset += batch_size
            continue

        async def process(subscription):
            async with semaphore, AsyncExitStack() as stack:
                lease = await stack.enter_async_context(grace_sensitive_panel_update(subscription.id))
                if not lease.allowed:
                    logger.debug(
                        'Синхронизация в панель пропущена: подписки нет или открыт грейс',
                        subscription_id=subscription.id,
                    )
                    return 'skipped'
                # Канонический PATCH собирается только из объекта, прочитанного
                # ПОСЛЕ ожидания блокировки, — иначе уедет устаревшее состояние.
                locked = lease.subscription
                # Писать связь нужно в ТУ ЖЕ сессию, которой принадлежит объект под
                # блокировкой: у лизы она своя. Иначе, во-первых, изменения уезжают
                # мимо той транзакции, которая их коммитит, а во-вторых, пять задач
                # прохода одновременно ходят в одну общую сессию — SQLAlchemy этого
                # не допускает, и однажды это упало бы на живой базе.
                locked_db = getattr(lease, 'db', None) or db
                try:
                    # Записанный id не проверяем отдельным запросом: на большой
                    # базе это удвоило бы число обращений к панели, а протухший
                    # id обнаружится по ответу на PATCH и приведёт к пересозданию.
                    # Синхронизация — не продление: устройства не сбрасываем (и не удваиваем
                    # число запросов к панели, которая и так ограничивает частоту).
                    result = await push_subscription(
                        api, locked.user, locked, db=locked_db, verify_recorded_id=False, reset_devices=False
                    )
                except RemnaWaveTransientError as error:
                    # Троттлинг/недоступность панели — warning: это не ошибка приложения,
                    # и в админ-чат такому не место (форвардер шлёт только error+).
                    logger.warning(
                        'Панель временно не приняла подписку (троттлинг или недоступность)',
                        subscription_id=subscription.id,
                        error=str(error)[:200],
                    )
                    return 'error'
                except Exception as error:
                    logger.error(
                        'Ошибка синхронизации подписки в панель',
                        subscription_id=subscription.id,
                        telegram_id=getattr(locked.user, 'telegram_id', None),
                        error=error,
                    )
                    return 'error'
                return result.action

        outcomes = await asyncio.gather(*(process(s) for s in valid), return_exceptions=True)
        for subscription, outcome in zip(valid, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                # Падение ДО тела задачи (взятие грейс-лизы, отмена) иначе
                # молча превращалось в «errors += 1» без единой строки в логе.
                logger.error(
                    'Подписку не удалось отправить в панель',
                    subscription_id=getattr(subscription, 'id', None),
                    error=outcome,
                )
                errors += 1
            elif outcome == 'created':
                created += 1
            elif outcome == 'updated':
                updated += 1
            elif outcome == 'skipped':
                skipped += 1
            else:
                errors += 1

        # Записи подписок ушли через сессии лиз (каждая коммитит сама); здесь
        # фиксировать нечего — прежний коммит на общей сессии падал по обрыву
        # соединения и засчитывал всю пачку ошибками.
        logger.info(
            '📦 Обработана партия подписок',
            offset=offset + len(subscriptions),
            created=created,
            updated=updated,
            errors=errors,
        )

        if len(subscriptions) < batch_size:
            break
        offset += batch_size

    return SyncStats(created=created, updated=updated, errors=errors, skipped=skipped)
