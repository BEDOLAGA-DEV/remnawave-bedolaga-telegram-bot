"""Продление возвращает подписке то, что в ней затёр оверлей грейса.

v4.10–4.11 (жалобы сторонних установок 2026-09-15): мониторинг принимал ACTIVE
оверлея грейса за продление в панели и переносил его в подписку — сквад грейса,
лимит «расход + квота», дату конца грейса; воркер закрывал грейс «человек
продлил». Настоящая оплата потом продлевала подписку с этими значениями: пути
продления передают только дни, и сквад грейса оставался навсегда (жалоба №2), а
новый срок считался от конца грейса.

Решение владельца — такие аккаунты лечит продление. Здесь это продление и
делает: перед расчётом нового срока берёт из истории грейс-сессий то, что было
до грейса (``plan_grace_echo_repair``), и возвращает только те поля, где сейчас
стоит значение оверлея. Зовётся из общего продления (``extend_subscription``) и из
``reconcile_tariff_traffic_limit`` — входа для продлений мимо CRUD.
"""

from __future__ import annotations

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ServerSquad, Subscription, Tariff
from app.services.grace_access_codec import list_sessions_for_subscription
from app.services.grace_access_service import GraceEchoRepair, plan_grace_echo_repair


logger = structlog.get_logger(__name__)

_GIB = 1024**3


async def _sellable_squads(db: AsyncSession) -> frozenset[str]:
    """Сквады, на которых подписка может стоять честно: их продают или дают триалу.

    Синхронизация с панелью заносит в список серверов бота все сквады панели, новые —
    скрытыми, поэтому «есть в списке» ничего не значит: считаются доступные к
    покупке и пробные серверы (NULL в «доступен» — как доступен) и сквады любого
    тарифа, включая выключенные: на них продлеваются старые подписки.
    """
    servers = await db.execute(
        select(ServerSquad.squad_uuid).where(
            or_(ServerSquad.is_available.is_not(False), ServerSquad.is_trial_eligible.is_(True))
        )
    )
    squads = {uuid for uuid in servers.scalars() if uuid}
    tariffs = await db.execute(select(Tariff.allowed_squads))
    for allowed in tariffs.scalars():
        squads.update(str(uuid) for uuid in (allowed or ()) if uuid)
    return frozenset(squads)


async def _plan(db: AsyncSession, subscription: Subscription) -> GraceEchoRepair | None:
    sessions = await list_sessions_for_subscription(db, subscription.id)
    if not sessions:
        return None
    squads = tuple(subscription.connected_squads or ())
    # Справочник продаж нужен только подписке, чьи серверы — ровно сквады какого-то оверлея.
    looks_like_overlay = bool(squads) and any(
        frozenset(session.overlay.squad_uuids) == frozenset(squads) for session in sessions
    )
    return plan_grace_echo_repair(
        squad_uuids=squads,
        traffic_limit_gb=max(0, int(subscription.traffic_limit_gb or 0)),
        end_at=subscription.end_date,
        sessions=sessions,
        sellable_squads=await _sellable_squads(db) if looks_like_overlay else frozenset(),
    )


async def terms_without_grace_echo(db: AsyncSession, subscription: Subscription) -> tuple[list[str], int | None]:
    """Сквады и лимит подписки, какими они были до оверлея грейса. Только чтение.

    Цена продления в классике считается по серверам и трафику подписки ДО
    продления — по скваду грейса она выходила без своих серверов (стенд,
    2026-09-15: «Сервер не найден в БД» на скваде грейса). Продление само вернёт
    эти поля (``undo_grace_overlay_echo``); цена должна считаться по ним же.
    """
    squads = list(subscription.connected_squads or [])
    limit = subscription.traffic_limit_gb
    repair = await _plan(db, subscription)
    if repair is None:
        return squads, limit
    if repair.traffic_limit_bytes is not None:
        limit = repair.traffic_limit_bytes // _GIB
    return list(repair.squad_uuids), limit


async def undo_grace_overlay_echo(db: AsyncSession, subscription: Subscription) -> set[str]:
    """Вернуть поля, в которых осел оверлей грейса. Возвращает имена изменённых полей."""
    repair = await _plan(db, subscription)
    if repair is None:
        return set()

    before = {
        'connected_squads': list(subscription.connected_squads or []),
        'traffic_limit_gb': subscription.traffic_limit_gb,
        'end_date': subscription.end_date,
    }
    changed: set[str] = set()
    if set(repair.squad_uuids) != set(subscription.connected_squads or []):
        subscription.connected_squads = list(repair.squad_uuids)
        changed.add('connected_squads')
    if repair.traffic_limit_bytes is not None:
        limit_gb = repair.traffic_limit_bytes // _GIB
        if subscription.traffic_limit_gb != limit_gb:
            subscription.traffic_limit_gb = limit_gb
            changed.add('traffic_limit_gb')
    if repair.end_at is not None and subscription.end_date != repair.end_at:
        subscription.end_date = repair.end_at
        changed.add('end_date')
    if changed:
        logger.warning(
            'В подписке стоял оверлей грейса — продление вернуло прежние значения',
            subscription_id=subscription.id,
            fields=sorted(changed),
            before={key: str(value) for key, value in before.items() if key in changed},
        )
    return changed
