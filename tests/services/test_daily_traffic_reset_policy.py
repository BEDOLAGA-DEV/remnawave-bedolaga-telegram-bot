"""Правило «обнулять ли трафик при суточном списании».

Жалоба владельца: у суточного тарифа счётчик трафика не обнулялся никогда.
Автосписание раз в 24 часа проходило, деньги списывались, а израсходованный
трафик копился через все продления — пока человек не упирался в лимит.
Причина: в суточном списании стояло жёсткое «не обнулять», хотя во всех
остальных оплатах проекта решает выключатель ``RESET_TRAFFIC_ON_PAYMENT``.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services.traffic_reset_policy import should_reset_traffic_on_daily_charge


def _tariff(mode: str | None) -> SimpleNamespace:
    return SimpleNamespace(name='суточный', traffic_reset_mode=mode)


def test_reset_when_setting_enabled(monkeypatch):
    """Выключатель включён — суточное списание обнуляет счётчик, как и любая оплата."""
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', True)
    monkeypatch.setattr(settings, 'DEFAULT_TRAFFIC_RESET_STRATEGY', 'MONTH')

    assert should_reset_traffic_on_daily_charge(_tariff('NO_RESET')) is True


def test_no_reset_when_setting_disabled(monkeypatch):
    """Выключатель выключен — поведение прежнее, счётчик не трогаем."""
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False)
    monkeypatch.setattr(settings, 'DEFAULT_TRAFFIC_RESET_STRATEGY', 'MONTH')

    assert should_reset_traffic_on_daily_charge(_tariff('NO_RESET')) is False


def test_no_reset_when_panel_already_resets_daily(monkeypatch):
    """Панель сама обнуляет раз в сутки — второй сброс дал бы две квоты за день.

    Это ровно тот обход, которым владелец закрыл баг до фикса
    (``traffic_reset_mode='DAY'`` у тарифа). Оставленный включённым вместе с
    ``RESET_TRAFFIC_ON_PAYMENT`` он должен не удваивать квоту, а уступать панели.
    """
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', True)
    monkeypatch.setattr(settings, 'DEFAULT_TRAFFIC_RESET_STRATEGY', 'MONTH')

    assert should_reset_traffic_on_daily_charge(_tariff('DAY')) is False


def test_no_reset_when_global_strategy_is_daily(monkeypatch):
    """У тарифа режим не задан — стратегия берётся из общей настройки."""
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', True)
    monkeypatch.setattr(settings, 'DEFAULT_TRAFFIC_RESET_STRATEGY', 'DAY')

    assert should_reset_traffic_on_daily_charge(_tariff(None)) is False


def test_reset_for_weekly_panel_strategy(monkeypatch):
    """Недельный сброс панели суточную квоту не покрывает — обнуляем сами."""
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', True)
    monkeypatch.setattr(settings, 'DEFAULT_TRAFFIC_RESET_STRATEGY', 'MONTH')

    assert should_reset_traffic_on_daily_charge(_tariff('WEEK')) is True


def test_missing_tariff_falls_back_to_global(monkeypatch):
    """Тариф не передан — решает общая настройка, без падения."""
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', True)
    monkeypatch.setattr(settings, 'DEFAULT_TRAFFIC_RESET_STRATEGY', 'MONTH')

    assert should_reset_traffic_on_daily_charge(None) is True


# ── lift_panel_traffic_limit: снятие лимита после оплаты суток ───────────────


class _Panel:
    def __init__(self, *, fail: bool = False) -> None:
        self.enabled: list[int] = []
        self._fail = fail

    async def enable_remnawave_user(self, panel_user_id, db=None):
        if self._fail:
            raise RuntimeError('panel down')
        self.enabled.append(panel_user_id)
        return True


@pytest.mark.asyncio
async def test_lift_uses_owner_account_in_single_tariff(monkeypatch):
    """В одиночном режиме аккаунт панели — у пользователя, не у подписки."""
    import app.services.traffic_reset_policy as policy
    from app.config import Settings

    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(policy, 'get_user_by_id', AsyncMock(return_value=SimpleNamespace(remnawave_id=9001)))
    panel = _Panel()

    await policy.lift_panel_traffic_limit(object(), SimpleNamespace(id=10, user_id=1, remnawave_id=None), service=panel)

    assert panel.enabled == [9001]


@pytest.mark.asyncio
async def test_lift_uses_subscription_account_in_multi_tariff(monkeypatch):
    """В мультитарифе у каждой подписки свой аккаунт — владельца не спрашиваем."""
    import app.services.traffic_reset_policy as policy
    from app.config import Settings

    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    owner_lookup = AsyncMock(return_value=SimpleNamespace(remnawave_id=9001))
    monkeypatch.setattr(policy, 'get_user_by_id', owner_lookup)
    panel = _Panel()

    await policy.lift_panel_traffic_limit(object(), SimpleNamespace(id=10, user_id=1, remnawave_id=42), service=panel)

    assert panel.enabled == [42]
    owner_lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_lift_survives_panel_error(monkeypatch):
    """Деньги уже взяты, подписка активна — ошибка панели не должна ронять оплату."""
    import app.services.traffic_reset_policy as policy
    from app.config import Settings

    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    panel = _Panel(fail=True)

    await policy.lift_panel_traffic_limit(object(), SimpleNamespace(id=10, user_id=1, remnawave_id=42), service=panel)

    assert panel.enabled == []


@pytest.mark.asyncio
async def test_lift_does_nothing_without_panel_account(monkeypatch):
    import app.services.traffic_reset_policy as policy
    from app.config import Settings

    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    panel = _Panel()

    await policy.lift_panel_traffic_limit(object(), SimpleNamespace(id=10, user_id=1, remnawave_id=None), service=panel)

    assert panel.enabled == []
