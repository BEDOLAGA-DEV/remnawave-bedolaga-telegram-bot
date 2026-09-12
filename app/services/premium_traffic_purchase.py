"""Докупка премиум-трафика по отдельному скваду.

Отдельно от обычной докупки: у премиум-сквада своя цена, свои пакеты и свой
потолок, заданные в тарифе (``server_traffic_limits[uuid]``). Общий
``traffic_topup_packages`` тарифа сюда не применяется и наоборот.

Докупленное живёт до конца текущего периода: ``extra_bytes`` обнуляется вместе с
периодом, как и ``used_bytes``. Это осознанно — иначе купленные гигабайты
копились бы из месяца в месяц и лимит перестал бы что-либо ограничивать.

Правила цены здесь, а не в роутере: покупать премиум умеют и кабинет, и бот, а
разойтись в цене они не должны.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.premium_traffic import add_extra_bytes, get_or_create_state, get_state
from app.utils.premium_traffic import BYTES_IN_GB, PremiumSquadConfig, get_premium_squads_for_tariff


logger = structlog.get_logger(__name__)


class PremiumTopupError(Exception):
    """Докупка невозможна. ``code`` переводится вызывающим в ответ или текст."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class PremiumTopupQuote:
    """Проверенное намерение купить: сквад, объём и цена до скидок."""

    squad_uuid: str
    gb: int
    base_price_kopeks: int
    config: PremiumSquadConfig

    @property
    def bytes(self) -> int:
        return self.gb * BYTES_IN_GB


def get_premium_topup_options(subscription) -> dict[str, PremiumSquadConfig]:
    """Сквады подписки, где докупка премиум-трафика включена и есть пакеты."""
    configs = get_premium_squads_for_tariff(getattr(subscription, 'tariff', None))
    connected = set(subscription.connected_squads or [])
    return {
        uuid: config
        for uuid, config in configs.items()
        # Право на сквад — обязательное условие: платить за трафик по серверу,
        # которого нет в подписке, пользователь не должен.
        if config.topup_enabled and uuid in connected
    }


async def quote_premium_topup(
    db: AsyncSession,
    subscription,
    squad_uuid: str,
    gb: int,
) -> PremiumTopupQuote:
    """Проверить возможность покупки и посчитать цену до скидок."""
    options = get_premium_topup_options(subscription)
    config = options.get(squad_uuid)
    if config is None:
        raise PremiumTopupError('topup_unavailable', 'Докупка премиум-трафика для этого сервера недоступна')

    price = config.price_kopeks_for(gb)
    if price is None:
        raise PremiumTopupError('package_not_found', f'Пакет {gb} ГБ не настроен для этого сервера')

    if config.max_topup_gb > 0:
        state = await get_state(db, subscription.id, squad_uuid)
        already_gb = (state.extra_bytes or 0) / BYTES_IN_GB if state else 0
        if already_gb + gb > config.max_topup_gb:
            raise PremiumTopupError(
                'topup_limit_reached',
                f'Больше {config.max_topup_gb} ГБ за период докупить нельзя (уже докуплено {already_gb:.0f} ГБ)',
            )

    return PremiumTopupQuote(squad_uuid=squad_uuid, gb=gb, base_price_kopeks=price, config=config)


async def apply_premium_topup(
    db: AsyncSession,
    subscription,
    quote: PremiumTopupQuote,
    *,
    period_start_at,
) -> tuple[object, bool]:
    """Начислить купленный объём.

    Возвращает состояние и признак, что сквад был снят и теперь возвращается —
    вызывающему это нужно, чтобы отправить набор сквадов в панель и уведомить
    пользователя.

    Состояние создаётся, если воркер до подписки ещё не дошёл: покупка не должна
    ждать первого прохода.
    """
    state = await get_or_create_state(
        db,
        subscription.id,
        quote.squad_uuid,
        limit_bytes=quote.config.limit_bytes,
        period_start_at=period_start_at,
    )
    was_limited = bool(state.is_limited)
    add_extra_bytes(state, quote.bytes)
    restored = was_limited and not state.is_limited

    logger.info(
        'Докуплен премиум-трафик',
        subscription_id=subscription.id,
        squad_uuid=quote.squad_uuid,
        gb=quote.gb,
        restored=restored,
    )
    return state, restored
