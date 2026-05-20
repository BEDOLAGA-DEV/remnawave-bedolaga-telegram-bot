from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.database.models import AccountDeletionRequest, AccountDeletionRequestStatus, SubscriptionStatus, UserStatus
from app.services.account_deletion_service import AccountDeletionService


def _make_user() -> SimpleNamespace:
    return SimpleNamespace(
        id=123,
        telegram_id=555,
        username='user',
        first_name='First',
        last_name='Last',
        email='user@example.com',
        email_verified=True,
        email_verified_at=datetime(2025, 1, 1, tzinfo=UTC),
        email_verification_source='cabinet',
        email_verification_token='verify-token',
        email_verification_expires=datetime(2025, 1, 2, tzinfo=UTC),
        email_change_new='new@example.com',
        email_change_code='123456',
        email_change_expires=datetime(2025, 1, 2, tzinfo=UTC),
        password_hash='hash',
        password_reset_token='reset-token',
        password_reset_expires=datetime(2025, 1, 2, tzinfo=UTC),
        pending_campaign_slug='campaign',
        referral_code='ref',
        referred_by_id=10,
        remnawave_uuid='user-panel-uuid',
        trojan_password='trojan',
        vless_uuid='vless',
        ss_password='ss',
        google_id='google',
        yandex_id='yandex',
        discord_id='discord',
        vk_id=42,
        apple_id='apple',
        status=UserStatus.ACTIVE.value,
        updated_at=datetime(2025, 1, 1, tzinfo=UTC),
    )


def _make_subscription() -> SimpleNamespace:
    return SimpleNamespace(
        id=987,
        status=SubscriptionStatus.ACTIVE.value,
        autopay_enabled=True,
        end_date=datetime.now(UTC) + timedelta(days=30),
        updated_at=None,
        remnawave_uuid='subscription-panel-uuid',
        connected_squads=[],
    )


class _ScalarResult:
    def __init__(self, items: list):
        self._items = items

    def all(self) -> list:
        return self._items


class _ExecuteResult:
    def __init__(self, items: list):
        self._items = items

    def scalars(self) -> _ScalarResult:
        return _ScalarResult(self._items)


@pytest.mark.asyncio
async def test_delete_own_account_creates_cleanup_request_and_anonymizes_user() -> None:
    user = _make_user()
    subscription = _make_subscription()
    db = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(
        side_effect=[
            SimpleNamespace(rowcount=2),
            SimpleNamespace(rowcount=1),
            SimpleNamespace(rowcount=3),
        ]
    )

    cart_service = MagicMock()
    cart_service.delete_user_cart = AsyncMock(return_value=True)

    with (
        patch(
            'app.services.account_deletion_service.get_all_subscriptions_by_user_id',
            AsyncMock(return_value=[subscription]),
        ),
        patch('app.services.account_deletion_service.SubscriptionService') as subscription_service_cls,
        patch(
            'app.services.account_deletion_service.decrement_subscription_server_counts',
            AsyncMock(),
        ) as decrement_counts,
        patch('app.services.account_deletion_service.UserCartService', return_value=cart_service),
    ):
        result = await AccountDeletionService().delete_own_account(db, user)

    subscription_service_cls.assert_not_called()
    decrement_counts.assert_awaited_once_with(db, subscription)

    assert subscription.status == SubscriptionStatus.DISABLED.value
    assert subscription.autopay_enabled is False
    assert subscription.end_date == user.updated_at

    assert user.status == UserStatus.DELETED.value
    assert user.telegram_id is None
    assert user.email is None
    assert user.password_hash is None
    assert user.referral_code is None
    assert user.remnawave_uuid is None
    assert user.google_id is None
    assert user.vless_uuid is None

    assert result.refresh_tokens_revoked == 2
    assert result.saved_payment_methods_deactivated == 1
    assert result.subscriptions_disabled == 1
    assert result.cart_deleted is True
    cleanup_request = db.add.call_args.args[0]
    assert cleanup_request.user_id == user.id
    assert cleanup_request.status == AccountDeletionRequestStatus.PENDING.value
    assert cleanup_request.panel_uuids == ['user-panel-uuid', 'subscription-panel-uuid']
    assert cleanup_request.telegram_id == 555
    db.flush.assert_awaited_once()
    db.commit.assert_awaited_once()
    cart_service.delete_user_cart.assert_awaited_once_with(user.id)


@pytest.mark.asyncio
async def test_process_pending_panel_cleanup_marks_completed() -> None:
    deletion_request = AccountDeletionRequest(
        id=1,
        status=AccountDeletionRequestStatus.PENDING.value,
        panel_uuids=['subscription-panel-uuid'],
        telegram_id=555,
        attempt_count=0,
        max_attempts=10,
        next_retry_at=datetime.now(UTC) - timedelta(minutes=1),
        created_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ExecuteResult([deletion_request]))

    subscription_service = MagicMock()
    subscription_service.delete_remnawave_user = AsyncMock(return_value=True)
    subscription_service.disable_remnawave_user = AsyncMock(return_value=True)

    with (
        patch('app.services.account_deletion_service.SubscriptionService', return_value=subscription_service),
        patch(
            'app.services.account_deletion_service.RemnaWaveWebhookService.mark_intentional_panel_deletion',
        ) as mark_intentional_deletion,
    ):
        stats = await AccountDeletionService().process_pending_panel_cleanup(db)

    mark_intentional_deletion.assert_called_once_with(panel_uuids=['subscription-panel-uuid'], telegram_id=555)
    subscription_service.delete_remnawave_user.assert_awaited_once_with('subscription-panel-uuid')
    subscription_service.disable_remnawave_user.assert_not_awaited()
    assert deletion_request.status == AccountDeletionRequestStatus.COMPLETED.value
    assert deletion_request.completed_at is not None
    assert deletion_request.last_error is None
    assert stats.processed == 1
    assert stats.completed == 1


@pytest.mark.asyncio
async def test_process_pending_panel_cleanup_schedules_retry_on_failure() -> None:
    deletion_request = AccountDeletionRequest(
        id=1,
        status=AccountDeletionRequestStatus.PENDING.value,
        panel_uuids=['subscription-panel-uuid'],
        telegram_id=555,
        attempt_count=0,
        max_attempts=10,
        next_retry_at=datetime.now(UTC) - timedelta(minutes=1),
        created_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ExecuteResult([deletion_request]))

    subscription_service = MagicMock()
    subscription_service.delete_remnawave_user = AsyncMock(return_value=False)
    subscription_service.disable_remnawave_user = AsyncMock(return_value=False)

    with (
        patch('app.services.account_deletion_service.SubscriptionService', return_value=subscription_service),
        patch('app.services.account_deletion_service.RemnaWaveWebhookService.mark_intentional_panel_deletion'),
    ):
        stats = await AccountDeletionService().process_pending_panel_cleanup(db)

    assert deletion_request.status == AccountDeletionRequestStatus.PENDING.value
    assert deletion_request.attempt_count == 1
    assert deletion_request.last_error == (
        'Failed to remove RemnaWave users during account deletion: subscription-panel-uuid'
    )
    assert deletion_request.next_retry_at > datetime.now(UTC)
    assert stats.processed == 1
    assert stats.retried == 1


@pytest.mark.asyncio
async def test_process_pending_panel_cleanup_marks_failed_after_max_attempts() -> None:
    deletion_request = AccountDeletionRequest(
        id=1,
        status=AccountDeletionRequestStatus.PENDING.value,
        panel_uuids=['subscription-panel-uuid'],
        telegram_id=555,
        attempt_count=0,
        max_attempts=1,
        next_retry_at=datetime.now(UTC) - timedelta(minutes=1),
        created_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_ExecuteResult([deletion_request]))

    subscription_service = MagicMock()
    subscription_service.delete_remnawave_user = AsyncMock(return_value=False)
    subscription_service.disable_remnawave_user = AsyncMock(return_value=False)

    with (
        patch('app.services.account_deletion_service.SubscriptionService', return_value=subscription_service),
        patch('app.services.account_deletion_service.RemnaWaveWebhookService.mark_intentional_panel_deletion'),
    ):
        stats = await AccountDeletionService().process_pending_panel_cleanup(db)

    assert deletion_request.status == AccountDeletionRequestStatus.FAILED.value
    assert deletion_request.attempt_count == 1
    assert deletion_request.last_error == (
        'Failed to remove RemnaWave users during account deletion: subscription-panel-uuid'
    )
    assert stats.processed == 1
    assert stats.failed == 1
    assert stats.failed_request_ids == (1,)
