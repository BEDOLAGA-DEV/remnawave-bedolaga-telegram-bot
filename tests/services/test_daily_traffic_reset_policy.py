"""Правило «обнулять ли трафик при суточном списании».

Жалоба владельца: у суточного тарифа счётчик трафика не обнулялся никогда.
Автосписание раз в 24 часа проходило, деньги списывались, а израсходованный
трафик копился через все продления — пока человек не упирался в лимит.
Причина: в суточном списании стояло жёсткое «не обнулять», хотя во всех
остальных оплатах проекта решает выключатель ``RESET_TRAFFIC_ON_PAYMENT``.
"""

from types import SimpleNamespace

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
