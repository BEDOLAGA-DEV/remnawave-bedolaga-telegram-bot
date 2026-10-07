"""MonitoringService._monitoring_cycle must not keep its own transaction open while
the channel subscription check runs. The check calls getChatMember for every
subscription in its own sessions and can take minutes; an outer transaction left
idle that long is terminated by PostgreSQL (idle_in_transaction_session_timeout),
and every later step of the cycle then fails with "connection is closed".
"""

from __future__ import annotations

from typing import Self
from unittest.mock import AsyncMock, MagicMock

from app.config import settings
from app.services import monitoring_service as monitoring_module
from app.services.monitoring_service import MonitoringService


class _Session:
    """Tracks an open transaction the way AsyncSession autobegin does."""

    def __init__(self) -> None:
        self.in_transaction = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def execute(self, *_args, **_kwargs):
        self.in_transaction = True
        return MagicMock()

    def add(self, _obj) -> None:
        self.in_transaction = True

    async def commit(self) -> None:
        self.in_transaction = False

    async def rollback(self) -> None:
        self.in_transaction = False


_STEPS = (
    '_process_autopayments',
    '_reconcile_platega_subscriptions',
    '_reconcile_lava_subscriptions',
    '_reconcile_cashera_subscriptions',
    '_check_expired_subscriptions',
    '_check_expiring_subscriptions',
    '_check_trial_expiring_soon',
    '_check_expired_subscription_followups',
    '_check_traffic_warnings',
    '_check_low_balance_alerts',
    '_retry_stuck_guest_purchases',
    '_cleanup_expired_refresh_tokens',
    '_cleanup_button_click_logs',
    '_cleanup_inactive_users',
    '_sync_with_remnawave',
)


async def test_channel_check_runs_without_open_outer_transaction(monkeypatch):
    session = _Session()
    monkeypatch.setattr(monitoring_module, 'AsyncSessionLocal', lambda: session)
    monkeypatch.setattr(settings, 'ENABLE_AUTOPAY', False)

    async def _reads(db, *_args, **_kwargs):
        await db.execute('SELECT 1')
        return 0

    monkeypatch.setattr(monitoring_module, 'deactivate_expired_offers', _reads)
    monkeypatch.setattr(monitoring_module, 'cleanup_expired_promo_offer_discounts', _reads)
    monkeypatch.setattr(monitoring_module.promo_offer_service, 'cleanup_expired_test_access', _reads)

    svc = MonitoringService(bot=MagicMock())
    monkeypatch.setattr(svc, '_cleanup_notification_cache', AsyncMock())
    monkeypatch.setattr(svc, '_log_monitoring_event', AsyncMock())
    for name in _STEPS:
        monkeypatch.setattr(svc, name, _reads)

    seen: list[bool] = []

    async def _channel_check(db):
        seen.append(db.in_transaction)

    monkeypatch.setattr(svc, '_check_trial_channel_subscriptions', _channel_check)

    await svc._monitoring_cycle()

    assert seen == [False]
