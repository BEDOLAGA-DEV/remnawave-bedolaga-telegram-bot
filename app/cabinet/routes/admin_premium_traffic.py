"""Админские операции с премиум-трафиком подписки.

Просмотр остатка, ручное начисление и сброс периода.

**Про выбор при сбросе.** Общий и премиум-трафик считаются из разных источников:
общий — счётчик пользователя в панели, премиум — история bandwidth-stats за
период. Поэтому сброс общего трафика (``reset-traffic`` в панели) премиум не
обнуляет, и наоборот. Раз операции независимы, админ выбирает область явно:

* ``regular`` — как раньше, дёргаем панель. Значение ``lastTrafficResetAt``
  записывается в ``panel_reset_ack_at``, чтобы воркер не принял этот сброс за
  досрочный и не сдвинул премиум-период следом;
* ``premium`` — панель не трогаем, начинаем премиум-период заново;
* ``both`` — и то, и другое.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.premium_traffic import (
    add_extra_bytes,
    get_or_create_state,
    get_states_for_subscription,
    start_new_period,
)
from app.database.crud.server_squad import get_squad_display_names
from app.database.crud.subscription import get_subscription_by_id
from app.database.models import User
from app.services.remnawave_service import RemnaWaveService
from app.utils.premium_traffic import BYTES_IN_GB, get_premium_squads_for_tariff

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/premium-traffic', tags=['Cabinet Admin Premium Traffic'])


class PremiumTrafficStateResponse(BaseModel):
    """Состояние премиум-лимита по одному скваду."""

    squad_uuid: str
    name: str | None = None
    limit_gb: float
    extra_gb: float
    used_gb: float
    remaining_gb: float
    is_limited: bool
    period_start_at: datetime | None = None
    last_checked_at: datetime | None = None
    # Воркер ещё не создавал состояние — показываем настройки тарифа как есть.
    has_state: bool = True


class PremiumTrafficResetRequest(BaseModel):
    """Что именно сбросить."""

    scope: Literal['premium', 'regular', 'both'] = 'premium'
    # Только для premium/both: если не задан, период начинается заново у всех
    # премиум-сквадов подписки.
    squad_uuid: str | None = Field(None, max_length=64)


class PremiumTrafficGrantRequest(BaseModel):
    """Ручное начисление премиум-гигабайтов."""

    squad_uuid: str = Field(..., min_length=1, max_length=64)
    gb: int = Field(..., ge=1, le=100_000)


async def _load_subscription(db: AsyncSession, subscription_id: int):
    subscription = await get_subscription_by_id(db, subscription_id)
    if subscription is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Subscription not found')
    return subscription


@router.get('/{subscription_id}', response_model=list[PremiumTrafficStateResponse])
async def get_premium_traffic_states(
    subscription_id: int,
    admin: User = Depends(require_permission('traffic:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Остаток по премиум-сквадам подписки."""
    subscription = await _load_subscription(db, subscription_id)
    configs = get_premium_squads_for_tariff(getattr(subscription, 'tariff', None))
    if not configs:
        return []

    states = {state.squad_uuid: state for state in await get_states_for_subscription(db, subscription_id)}
    squad_names = await get_squad_display_names(db, list(configs))

    result: list[PremiumTrafficStateResponse] = []
    for squad_uuid, config in configs.items():
        state = states.get(squad_uuid)
        limit_bytes = state.limit_bytes if state else config.limit_bytes
        extra_bytes = (state.extra_bytes or 0) if state else 0
        used_bytes = (state.used_bytes or 0) if state else 0
        result.append(
            PremiumTrafficStateResponse(
                squad_uuid=squad_uuid,
                name=config.name or squad_names.get(squad_uuid),
                limit_gb=round((limit_bytes or 0) / BYTES_IN_GB, 2),
                extra_gb=round(extra_bytes / BYTES_IN_GB, 2),
                used_gb=round(used_bytes / BYTES_IN_GB, 2),
                remaining_gb=round(max(0, (limit_bytes or 0) + extra_bytes - used_bytes) / BYTES_IN_GB, 2),
                is_limited=bool(state.is_limited) if state else False,
                period_start_at=state.period_start_at if state else None,
                last_checked_at=state.last_checked_at if state else None,
                has_state=state is not None,
            )
        )
    return result


@router.post('/{subscription_id}/reset')
async def reset_premium_traffic(
    subscription_id: int,
    request: PremiumTrafficResetRequest,
    admin: User = Depends(require_permission('traffic:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Сбросить трафик подписки: премиум, обычный или оба."""
    subscription = await _load_subscription(db, subscription_id)
    now = datetime.now(UTC)
    result: dict[str, object] = {'scope': request.scope, 'regular_reset': False, 'premium_squads': []}

    if request.scope in ('regular', 'both'):
        result['regular_reset'] = await _reset_regular(db, subscription, request.scope, now)

    if request.scope in ('premium', 'both'):
        result['premium_squads'] = await _reset_premium(db, subscription, request.squad_uuid, now)

    await db.commit()
    logger.info(
        'Админ сбросил трафик подписки',
        admin_id=admin.id,
        subscription_id=subscription_id,
        scope=request.scope,
        premium_squads=result['premium_squads'],
    )
    return result


async def _reset_regular(db: AsyncSession, subscription, scope: str, now: datetime) -> bool:
    """Сбросить общий трафик в панели."""
    service = RemnaWaveService()
    if not service.is_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={'code': 'panel_unavailable', 'message': 'Панель Remnawave не настроена'},
        )

    panel_user_id = getattr(subscription, 'remnawave_id', None) or (
        subscription.user.remnawave_id if subscription.user else None
    )
    if not panel_user_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={'code': 'no_panel_user', 'message': 'У подписки нет пользователя в панели'},
        )

    async with service.get_api_client() as api:
        await api.reset_user_traffic(panel_user_id)
        panel_user = await api.get_user_by_id(panel_user_id)

    if scope == 'both':
        # Премиум и так сбрасывается ниже — отмечать нечего.
        return True

    # Отмечаем сброс как учтённый, иначе воркер примет его за досрочный сброс
    # панели и обнулит премиум-период следом — ровно то, чего админ не просил.
    ack = getattr(panel_user, 'last_traffic_reset_at', None) or now
    for state in await get_states_for_subscription(db, subscription.id):
        state.panel_reset_ack_at = ack
    return True


async def _reset_premium(db: AsyncSession, subscription, squad_uuid: str | None, now: datetime) -> list[str]:
    """Начать премиум-период заново, вернув снятые сквады."""
    configs = get_premium_squads_for_tariff(getattr(subscription, 'tariff', None))
    if not configs:
        return []

    targets = [squad_uuid] if squad_uuid else list(configs)
    reset: list[str] = []
    for uuid in targets:
        config = configs.get(uuid)
        if config is None:
            continue
        state = await get_or_create_state(
            db,
            subscription.id,
            uuid,
            limit_bytes=config.limit_bytes,
            period_start_at=now,
        )
        start_new_period(state, period_start_at=now, limit_bytes=config.limit_bytes)
        reset.append(uuid)
    return reset


@router.post('/{subscription_id}/grant')
async def grant_premium_traffic(
    subscription_id: int,
    request: PremiumTrafficGrantRequest,
    admin: User = Depends(require_permission('traffic:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Начислить премиум-гигабайты вручную, без оплаты.

    Потолок ``max_topup_gb`` здесь не действует: он ограничивает покупку
    пользователем, а решение админа — последняя инстанция.
    """
    subscription = await _load_subscription(db, subscription_id)
    configs = get_premium_squads_for_tariff(getattr(subscription, 'tariff', None))
    config = configs.get(request.squad_uuid)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={'code': 'not_a_premium_squad', 'message': 'В тарифе нет премиум-лимита для этого сервера'},
        )

    state = await get_or_create_state(
        db,
        subscription.id,
        request.squad_uuid,
        limit_bytes=config.limit_bytes,
        period_start_at=datetime.now(UTC),
    )
    was_limited = bool(state.is_limited)
    add_extra_bytes(state, request.gb * BYTES_IN_GB)
    await db.commit()

    logger.info(
        'Админ начислил премиум-трафик',
        admin_id=admin.id,
        subscription_id=subscription_id,
        squad_uuid=request.squad_uuid,
        gb=request.gb,
    )
    return {
        'success': True,
        'squad_uuid': request.squad_uuid,
        'gb': request.gb,
        'extra_gb': round((state.extra_bytes or 0) / BYTES_IN_GB, 2),
        # Сквад вернёт ближайший проход воркера — отдельный PATCH панели здесь
        # не делаем, чтобы админский запрос не зависел от её доступности.
        'squad_restored': was_limited and not state.is_limited,
    }
