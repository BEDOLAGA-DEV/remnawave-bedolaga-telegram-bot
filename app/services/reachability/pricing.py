"""Деньги интеграции: кредиты API = копейки, оценка VLESS, потолок цены задачи."""

from __future__ import annotations


DEFAULT_VLESS_LEG_KOPEKS = 110  # наблюдалось 103 на gold (−7 %); без скидки ≈110


def format_rubles(kopeks: int) -> str:
    return f'{kopeks // 100},{kopeks % 100:02d} ₽'


class CostLimitExceeded(Exception):
    def __init__(self, cost_kopeks: int, limit_kopeks: int) -> None:
        self.cost_kopeks = cost_kopeks
        self.limit_kopeks = limit_kopeks
        super().__init__(f'Цена задачи {format_rubles(cost_kopeks)} выше потолка {format_rubles(limit_kopeks)}')


def credits_to_kopeks(credits: int | None) -> int | None:
    return None if credits is None else int(credits)


def estimate_vless_kopeks(n_servers: int, n_units: int, leg_kopeks: int | None) -> int:
    return max(0, n_servers) * max(0, n_units) * (leg_kopeks or DEFAULT_VLESS_LEG_KOPEKS)


def enforce_cost_limit(cost_kopeks: int | None, limit_kopeks: int) -> None:
    if limit_kopeks > 0 and cost_kopeks is not None and cost_kopeks > limit_kopeks:
        raise CostLimitExceeded(cost_kopeks, limit_kopeks)
