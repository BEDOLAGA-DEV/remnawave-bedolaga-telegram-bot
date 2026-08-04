from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.account_deletion_service import AccountDeletionCleanupStats
from app.services.monitoring_service import MonitoringService


@pytest.mark.asyncio
async def test_retry_pending_account_deletions_uses_independent_transaction() -> None:
    session = AsyncMock()
    stats = AccountDeletionCleanupStats(processed=1, completed=1)

    with (
        patch('app.services.monitoring_service.AsyncSessionLocal', return_value=session),
        patch(
            'app.services.account_deletion_service.account_deletion_service.process_pending_panel_cleanup',
            AsyncMock(return_value=stats),
        ) as process_cleanup,
    ):
        await MonitoringService(bot=None)._retry_pending_account_deletions()

    process_cleanup.assert_awaited_once_with(session, limit=10)
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()
    session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_pending_account_deletions_notifies_admin_on_failed_jobs() -> None:
    session = AsyncMock()
    bot = MagicMock()
    stats = AccountDeletionCleanupStats(processed=1, failed=1, failed_request_ids=(42,))
    notification_service = MagicMock()
    notification_service.send_admin_notification = AsyncMock(return_value=True)

    with (
        patch('app.services.monitoring_service.AsyncSessionLocal', return_value=session),
        patch(
            'app.services.account_deletion_service.account_deletion_service.process_pending_panel_cleanup',
            AsyncMock(return_value=stats),
        ),
        patch(
            'app.services.admin_notification_service.AdminNotificationService',
            return_value=notification_service,
        ) as notification_cls,
    ):
        await MonitoringService(bot=bot)._retry_pending_account_deletions()

    session.commit.assert_not_awaited()
    notification_cls.assert_called_once_with(bot)
    notification_service.send_admin_notification.assert_awaited_once()
    message = notification_service.send_admin_notification.await_args.args[0]
    assert 'Account deletion cleanup failed' in message
    assert 'manual verification' in message
    assert 'exhausted all retry attempts' not in message
    assert '42' in message
