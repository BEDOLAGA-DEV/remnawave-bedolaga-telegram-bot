"""Вебхук ``user.bandwidth_usage_threshold_reached``: процент берётся из самого события.

По схеме Remnawave 3.4.3 у события ``data`` — это объект пользователя, и сработавший порог
лежит в ``data.lastTriggeredThreshold``. Полей ``thresholdPercent``/``threshold`` и
``meta.thresholdPercent`` у панели нет — обработчик, который искал только их, всегда
показывал пользователю «80%».
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.remnawave_webhook_service import RemnaWaveWebhookService


@pytest.fixture(autouse=True)
def _traffic_warning_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('app.utils.notification_prefs.is_traffic_warning_enabled', lambda user: True)


def _service() -> RemnaWaveWebhookService:
    svc = RemnaWaveWebhookService(MagicMock())
    svc._notify_user = AsyncMock()
    svc._get_traffic_keyboard = MagicMock(return_value=None)
    return svc


def _user() -> MagicMock:
    user = MagicMock()
    user.id = 1
    return user


def _sent_percent(svc: RemnaWaveWebhookService) -> str:
    return svc._notify_user.await_args.kwargs['format_kwargs']['percent']


async def test_percent_comes_from_last_triggered_threshold() -> None:
    svc = _service()

    await svc._handle_bandwidth_threshold(None, _user(), None, {'id': 42, 'lastTriggeredThreshold': 90})

    assert _sent_percent(svc) == '90'
    assert svc._notify_user.await_args.args[1] == 'WEBHOOK_SUB_BANDWIDTH_THRESHOLD'


async def test_legacy_threshold_percent_field_still_accepted() -> None:
    svc = _service()

    await svc._handle_bandwidth_threshold(None, _user(), None, {'thresholdPercent': 75})

    assert _sent_percent(svc) == '75'


async def test_zero_last_triggered_threshold_is_not_a_percent() -> None:
    """0 — «порог ещё не срабатывал»; показывать «0%» нельзя, берём запасное значение."""
    svc = _service()

    await svc._handle_bandwidth_threshold(None, _user(), None, {'lastTriggeredThreshold': 0})

    assert _sent_percent(svc) == '80'
