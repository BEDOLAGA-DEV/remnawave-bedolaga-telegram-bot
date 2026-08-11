"""SUBSCRIPTION_EXPIRING_PAID must format templates that use {days} (#2737).

Custom/older locale files (e.g. fa.json) use a {days} placeholder while the code
historically passed only days_text= to .format(). The KeyError('days') was
swallowed by the broad except in _send_subscription_expiring_notification, so the
user silently got no notification. The format call now passes both days and
days_text.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.config import settings
from app.services.monitoring_service import MonitoringService


class _LegacyTexts:
    """Texts stub emulating a custom locale with the legacy {days} placeholder."""

    def t(self, key: str, default: str | None = None) -> str:
        if key == 'SUBSCRIPTION_EXPIRING_PAID':
            return 'Подписка{tariff_label} истекает через {days} дн. ({days_text}) — {end_date}\n{autopay_status}\n{action_text}'
        return default if default is not None else key


def _user() -> SimpleNamespace:
    return SimpleNamespace(telegram_id=12345, language='fa', balance_kopeks=0)


def _subscription() -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        autopay_enabled=False,
        end_date=datetime.now(UTC) + timedelta(days=3),
        tariff=None,
    )


async def test_expiring_paid_supports_days_placeholder(monkeypatch):
    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', False)
    monkeypatch.setattr(settings, 'ENABLE_AUTOPAY', False)
    # Метод патчится на классе: pydantic не даёт setattr для методов на инстансе.
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr('app.services.monitoring_service.get_texts', lambda _lang: _LegacyTexts())

    bot = MagicMock()
    bot.send_message = AsyncMock()

    svc = MonitoringService(bot=bot)
    result = await svc._send_subscription_expiring_notification(_user(), _subscription(), days=3)

    # Без days= в .format() KeyError гасился общим except и метод возвращал False.
    assert result is True
    bot.send_message.assert_awaited_once()
    sent_text = bot.send_message.await_args.kwargs['text']
    assert 'через 3 дн.' in sent_text
