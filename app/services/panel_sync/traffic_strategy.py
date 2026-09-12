"""Стратегия сброса трафика, с которой подписка уезжает в панель.

Это правило поля запроса, поэтому живёт в пакете синхронизации, а не в
сервисе подписок: ``payload`` собирает его в каждый запрос, суточная политика
(``traffic_reset_policy``) спрашивает, обнуляет ли панель счётчик сама.
"""

from __future__ import annotations

import structlog

from app.config import settings
from app.external.remnawave_api import TrafficLimitStrategy


logger = structlog.get_logger(__name__)

_STRATEGY_BY_MODE = {
    'NO_RESET': 'NO_RESET',
    'DAY': 'DAY',
    'WEEK': 'WEEK',
    'MONTH': 'MONTH',
    'MONTH_ROLLING': 'MONTH_ROLLING',
}


def get_traffic_reset_strategy(tariff=None) -> TrafficLimitStrategy:
    """Стратегия сброса трафика: настройка тарифа, иначе общая из конфига.

    Args:
        tariff: Объект тарифа. Если у тарифа задан ``traffic_reset_mode``,
            используется он, иначе глобальная ``DEFAULT_TRAFFIC_RESET_STRATEGY``.
    """
    if tariff is not None:
        tariff_mode = getattr(tariff, 'traffic_reset_mode', None)
        if tariff_mode is not None:
            mapped_strategy = _STRATEGY_BY_MODE.get(tariff_mode.upper(), 'NO_RESET')
            logger.info(
                '🔄 Стратегия сброса трафика из тарифа',
                value=getattr(tariff, 'name', 'N/A'),
                tariff_mode=tariff_mode,
                mapped_strategy=mapped_strategy,
            )
            return getattr(TrafficLimitStrategy, mapped_strategy)

    strategy = settings.DEFAULT_TRAFFIC_RESET_STRATEGY.upper()
    mapped_strategy = _STRATEGY_BY_MODE.get(strategy, 'NO_RESET')
    logger.info('🔄 Стратегия сброса трафика из конфига', strategy=strategy, mapped_strategy=mapped_strategy)
    return getattr(TrafficLimitStrategy, mapped_strategy)
