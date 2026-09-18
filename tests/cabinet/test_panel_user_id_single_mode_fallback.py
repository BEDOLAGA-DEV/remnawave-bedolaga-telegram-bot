"""Кабинет в одиночном режиме находит аккаунт панели и через подписку.

Вопрос владельца (18.09): в мультитарифе устройства показываются, а если
переключить оператора обратно на одиночный режим — по нулям. Причина: аккаунт,
созданный в мультитарифе, записан только у подписки (``subscriptions.remnawave_id``),
а одиночный режим в кабинете читал строго ``users.remnawave_id``. Бот в Telegram
уже умеет запасной путь через подписку — кабинет должен так же. В мультитарифе
запасного пути нет намеренно: там у каждой подписки свой аккаунт.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.cabinet.routes.subscription_modules.devices import _resolve_panel_user_id
from app.config import Settings


def _mode(monkeypatch, *, multi: bool) -> None:
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: multi)


def test_single_mode_falls_back_to_subscription_account(monkeypatch):
    _mode(monkeypatch, multi=False)

    assert _resolve_panel_user_id(SimpleNamespace(remnawave_id=555), SimpleNamespace(remnawave_id=None)) == 555


def test_single_mode_prefers_user_account_when_present(monkeypatch):
    _mode(monkeypatch, multi=False)

    assert _resolve_panel_user_id(SimpleNamespace(remnawave_id=555), SimpleNamespace(remnawave_id=1)) == 1


def test_multi_mode_never_falls_back_to_user_account(monkeypatch):
    _mode(monkeypatch, multi=True)

    assert _resolve_panel_user_id(SimpleNamespace(remnawave_id=None), SimpleNamespace(remnawave_id=1)) is None
