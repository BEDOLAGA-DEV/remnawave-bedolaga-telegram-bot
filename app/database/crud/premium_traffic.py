"""CRUD состояний премиум-лимита по сквадам.

Одна строка на пару подписка + премиум-сквад. Пишут сюда конкурентно воркер и
покупка доп. трафика, поэтому получение состояния сделано идемпотентным: при
гонке на вставке выигравшая строка перечитывается, а не порождает дубль.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import and_, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import SubscriptionPremiumTraffic


async def get_state(db: AsyncSession, subscription_id: int, squad_uuid: str) -> SubscriptionPremiumTraffic | None:
    result = await db.execute(
        select(SubscriptionPremiumTraffic).where(
            and_(
                SubscriptionPremiumTraffic.subscription_id == subscription_id,
                SubscriptionPremiumTraffic.squad_uuid == squad_uuid,
            )
        )
    )
    return result.scalar_one_or_none()


async def get_states_for_subscription(db: AsyncSession, subscription_id: int) -> list[SubscriptionPremiumTraffic]:
    result = await db.execute(
        select(SubscriptionPremiumTraffic).where(SubscriptionPremiumTraffic.subscription_id == subscription_id)
    )
    return list(result.scalars().all())


async def get_states_for_squad(db: AsyncSession, squad_uuid: str) -> list[SubscriptionPremiumTraffic]:
    """Все состояния по скваду — воркер обходит их пачкой, одним запросом к панели."""
    result = await db.execute(
        select(SubscriptionPremiumTraffic).where(SubscriptionPremiumTraffic.squad_uuid == squad_uuid)
    )
    return list(result.scalars().all())


async def get_limited_squad_uuids(db: AsyncSession, subscription_id: int) -> set[str]:
    """Сквады, снятые из-за исчерпания лимита.

    Отдельный узкий запрос, потому что зовётся из горячего пути синхронизации
    сквадов с панелью: там нужны только UUID, тянуть состояния целиком незачем.
    """
    result = await db.execute(
        select(SubscriptionPremiumTraffic.squad_uuid).where(
            and_(
                SubscriptionPremiumTraffic.subscription_id == subscription_id,
                SubscriptionPremiumTraffic.is_limited.is_(True),
            )
        )
    )
    return set(result.scalars().all())


async def get_or_create_state(
    db: AsyncSession,
    subscription_id: int,
    squad_uuid: str,
    *,
    limit_bytes: int,
    period_start_at: datetime,
    panel_reset_ack_at: datetime | None = None,
) -> SubscriptionPremiumTraffic:
    """Вернуть состояние, создав его при первой встрече.

    Идемпотентно: воркер и покупка трафика могут дойти сюда одновременно, и
    уникальный ключ (subscription_id, squad_uuid) отобьёт вторую вставку. В
    этом случае перечитываем строку победителя вместо того, чтобы падать.
    """
    existing = await get_state(db, subscription_id, squad_uuid)
    if existing is not None:
        return existing

    state = SubscriptionPremiumTraffic(
        subscription_id=subscription_id,
        squad_uuid=squad_uuid,
        limit_bytes=limit_bytes,
        period_start_at=period_start_at,
        panel_reset_ack_at=panel_reset_ack_at,
    )
    try:
        # Savepoint, а не общий rollback: вызывающий (воркер, покупка трафика)
        # ведёт свою транзакцию, и откатывать её целиком из-за проигранной гонки
        # на вставке нельзя.
        async with db.begin_nested():
            db.add(state)
            await db.flush()
    except IntegrityError:
        conflicting = await get_state(db, subscription_id, squad_uuid)
        if conflicting is None:
            # Нарушен не наш уникальный ключ — прятать такое нельзя.
            raise
        return conflicting
    return state


def start_new_period(
    state: SubscriptionPremiumTraffic,
    *,
    period_start_at: datetime,
    limit_bytes: int,
    panel_reset_ack_at: datetime | None = None,
) -> SubscriptionPremiumTraffic:
    """Начать новый период: обнулить расход, докупку и уведомления.

    Снятый сквад при этом возвращается — `is_limited` сбрасывается. Лимит берём
    заново из тарифа: за прошедший период его могли поменять.
    """
    state.period_start_at = period_start_at
    state.limit_bytes = limit_bytes
    state.extra_bytes = 0
    state.used_bytes = 0
    # Поправку на первые сутки снимем заново: период новый.
    state.baseline_bytes = None
    state.notified_80 = False
    state.is_limited = False
    if panel_reset_ack_at is not None:
        state.panel_reset_ack_at = panel_reset_ack_at
    return state


def record_usage(
    state: SubscriptionPremiumTraffic,
    used_bytes: int,
    *,
    checked_at: datetime | None = None,
) -> SubscriptionPremiumTraffic:
    """Записать замер расхода.

    Расход не убывает внутри периода: панель отдаёт накопленное за диапазон, и
    просадка означала бы сбой выборки, а не возврат трафика. Берём максимум,
    чтобы такой сбой не вернул пользователю доступ к исчерпанному скваду.
    """
    state.used_bytes = max(state.used_bytes or 0, used_bytes, 0)
    state.last_checked_at = checked_at or datetime.now(UTC)
    return state


def add_extra_bytes(state: SubscriptionPremiumTraffic, extra_bytes: int) -> SubscriptionPremiumTraffic:
    """Начислить докупленный трафик и вернуть сквад, если он был снят."""
    if extra_bytes <= 0:
        return state
    state.extra_bytes = (state.extra_bytes or 0) + extra_bytes
    if not state.is_exhausted:
        state.is_limited = False
        # Порог 80 % считается от нового лимита — предупредить нужно заново.
        state.notified_80 = False
    return state


async def delete_states_for_subscription(db: AsyncSession, subscription_id: int) -> int:
    """Убрать все состояния подписки — например, при переходе на тариф без премиума."""
    result = await db.execute(
        delete(SubscriptionPremiumTraffic).where(SubscriptionPremiumTraffic.subscription_id == subscription_id)
    )
    return result.rowcount or 0


async def delete_states_for_squads(db: AsyncSession, subscription_id: int, squad_uuids: set[str]) -> int:
    """Убрать состояния конкретных сквадов — при смене тарифа их набор меняется."""
    if not squad_uuids:
        return 0
    result = await db.execute(
        delete(SubscriptionPremiumTraffic).where(
            and_(
                SubscriptionPremiumTraffic.subscription_id == subscription_id,
                SubscriptionPremiumTraffic.squad_uuid.in_(squad_uuids),
            )
        )
    )
    return result.rowcount or 0
