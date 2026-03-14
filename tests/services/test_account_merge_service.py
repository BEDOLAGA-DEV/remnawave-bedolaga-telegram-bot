"""Tests for app.services.account_merge_service."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import account_merge_service
from app.services.account_merge_service import (
    _build_subscription_preview,
    _build_user_preview,
    compute_auth_methods,
    execute_merge,
    get_merge_preview,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(
    *,
    id: int = 1,
    telegram_id: int | None = None,
    email: str | None = None,
    email_verified: bool = False,
    email_verified_at: datetime | None = None,
    password_hash: str | None = None,
    google_id: str | None = None,
    yandex_id: str | None = None,
    discord_id: str | None = None,
    vk_id: int | None = None,
    balance_kopeks: int = 0,
    username: str | None = None,
    first_name: str | None = None,
    status: str = 'active',
    partner_status: str = 'none',
    referral_code: str | None = None,
    referral_commission_percent: int | None = None,
    referred_by_id: int | None = None,
    remnawave_uuid: str | None = None,
    subscription: object | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=id,
        telegram_id=telegram_id,
        email=email,
        email_verified=email_verified,
        email_verified_at=email_verified_at,
        password_hash=password_hash,
        google_id=google_id,
        yandex_id=yandex_id,
        discord_id=discord_id,
        vk_id=vk_id,
        balance_kopeks=balance_kopeks,
        username=username,
        first_name=first_name,
        status=status,
        partner_status=partner_status,
        referral_code=referral_code,
        referral_commission_percent=referral_commission_percent,
        referred_by_id=referred_by_id,
        remnawave_uuid=remnawave_uuid,
        subscription=subscription,
        created_at=created_at or datetime(2024, 1, 1, tzinfo=UTC),
        updated_at=updated_at or datetime(2024, 1, 1, tzinfo=UTC),
    )


def _make_subscription(
    *,
    user_id: int = 1,
    status: str = 'active',
    is_trial: bool = False,
    end_date: datetime | None = None,
    traffic_limit_gb: float = 100.0,
    traffic_used_gb: float = 10.0,
    device_limit: int = 3,
    tariff_name: str = 'Basic',
    autopay_enabled: bool = False,
) -> SimpleNamespace:
    tariff = SimpleNamespace(name=tariff_name)
    return SimpleNamespace(
        user_id=user_id,
        status=status,
        is_trial=is_trial,
        end_date=end_date or datetime(2025, 1, 1, tzinfo=UTC),
        traffic_limit_gb=traffic_limit_gb,
        traffic_used_gb=traffic_used_gb,
        device_limit=device_limit,
        tariff=tariff,
        autopay_enabled=autopay_enabled,
    )


def _make_db() -> SimpleNamespace:
    return SimpleNamespace(
        execute=AsyncMock(),
        delete=AsyncMock(),
        flush=AsyncMock(),
    )


# ---------------------------------------------------------------------------
# compute_auth_methods
# ---------------------------------------------------------------------------


class TestComputeAuthMethods:
    def test_no_methods(self):
        user = _make_user()
        assert compute_auth_methods(user) == []

    def test_telegram_only(self):
        user = _make_user(telegram_id=12345)
        assert compute_auth_methods(user) == ['telegram']

    def test_email_only(self):
        user = _make_user(email='test@example.com', password_hash='hash123')
        assert compute_auth_methods(user) == ['email']

    def test_email_without_password_not_counted(self):
        user = _make_user(email='test@example.com')
        assert compute_auth_methods(user) == []

    def test_all_methods(self):
        user = _make_user(
            telegram_id=12345,
            email='test@example.com',
            password_hash='hash',
            google_id='g123',
            yandex_id='y123',
            discord_id='d123',
            vk_id=99999,
        )
        assert compute_auth_methods(user) == ['telegram', 'email', 'google', 'yandex', 'discord', 'vk']

    def test_oauth_only(self):
        user = _make_user(google_id='g123', discord_id='d123')
        assert compute_auth_methods(user) == ['google', 'discord']


# ---------------------------------------------------------------------------
# _build_subscription_preview
# ---------------------------------------------------------------------------


class TestBuildSubscriptionPreview:
    def test_none_subscription(self):
        assert _build_subscription_preview(None) is None

    def test_valid_subscription(self):
        sub = _make_subscription(tariff_name='Premium')
        result = _build_subscription_preview(sub)
        assert result['tariff_name'] == 'Premium'
        assert result['status'] == 'active'
        assert result['is_trial'] is False
        assert result['device_limit'] == 3

    def test_subscription_without_tariff(self):
        sub = _make_subscription()
        sub.tariff = None
        result = _build_subscription_preview(sub)
        assert result['tariff_name'] is None


# ---------------------------------------------------------------------------
# _build_user_preview
# ---------------------------------------------------------------------------


class TestBuildUserPreview:
    def test_basic_user(self):
        user = _make_user(id=42, username='alice', email='a@b.com', balance_kopeks=5000)
        result = _build_user_preview(user)
        assert result['id'] == 42
        assert result['username'] == 'alice'
        assert result['balance_kopeks'] == 5000
        assert result['subscription'] is None

    def test_user_with_subscription(self):
        sub = _make_subscription(user_id=1)
        user = _make_user(id=1, subscription=sub)
        result = _build_user_preview(user)
        assert result['subscription'] is not None
        assert result['subscription']['status'] == 'active'


# ---------------------------------------------------------------------------
# get_merge_preview
# ---------------------------------------------------------------------------


class TestGetMergePreview:
    async def test_same_user_ids_raises(self):
        db = _make_db()
        with pytest.raises(ValueError, match='не могут совпадать'):
            await get_merge_preview(db, 1, 1)

    async def test_primary_not_found_raises(self, monkeypatch):
        db = _make_db()
        monkeypatch.setattr(account_merge_service, 'get_user_by_id', AsyncMock(return_value=None))
        with pytest.raises(ValueError, match='Основной пользователь'):
            await get_merge_preview(db, 1, 2)

    async def test_secondary_not_found_raises(self, monkeypatch):
        db = _make_db()
        primary = _make_user(id=1)
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, None]),
        )
        with pytest.raises(ValueError, match='Вторичный пользователь'):
            await get_merge_preview(db, 1, 2)

    async def test_success(self, monkeypatch):
        db = _make_db()
        primary = _make_user(id=1, username='primary', telegram_id=111)
        secondary = _make_user(id=2, username='secondary', google_id='g123')
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        result = await get_merge_preview(db, 1, 2)
        assert result['primary']['id'] == 1
        assert result['secondary']['id'] == 2
        assert 'telegram' in result['primary']['auth_methods']
        assert 'google' in result['secondary']['auth_methods']


# ---------------------------------------------------------------------------
# execute_merge — validation
# ---------------------------------------------------------------------------


class TestExecuteMergeValidation:
    async def test_same_ids_raises(self):
        db = _make_db()
        with pytest.raises(ValueError, match='не могут совпадать'):
            await execute_merge(db, 1, 1)

    async def test_primary_not_found_raises(self, monkeypatch):
        db = _make_db()
        monkeypatch.setattr(account_merge_service, 'get_user_by_id', AsyncMock(return_value=None))
        with pytest.raises(ValueError, match='Основной пользователь'):
            await execute_merge(db, 1, 2)

    async def test_secondary_not_found_raises(self, monkeypatch):
        db = _make_db()
        primary = _make_user(id=1)
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, None]),
        )
        with pytest.raises(ValueError, match='Вторичный пользователь'):
            await execute_merge(db, 1, 2)

    async def test_deleted_secondary_raises(self, monkeypatch):
        db = _make_db()
        primary = _make_user(id=1)
        secondary = _make_user(id=2, status='deleted')
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with pytest.raises(ValueError, match='уже удалён'):
            await execute_merge(db, 1, 2)

    async def test_deleted_primary_raises(self, monkeypatch):
        db = _make_db()
        primary = _make_user(id=1, status='deleted')
        secondary = _make_user(id=2)
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with pytest.raises(ValueError, match='удалён'):
            await execute_merge(db, 1, 2)

    async def test_invalid_keep_subscription_from_raises(self):
        db = _make_db()
        with pytest.raises(ValueError, match=r'primary.*secondary'):
            await execute_merge(db, 1, 2, keep_subscription_from='invalid')


# ---------------------------------------------------------------------------
# execute_merge — data transfer
# ---------------------------------------------------------------------------


def _patch_remnawave_delete():
    return patch.object(
        account_merge_service,
        '_delete_remnawave_user_with_fallback',
        new_callable=AsyncMock,
    )


class TestExecuteMergeOAuthTransfer:
    async def test_transfers_oauth_ids(self, monkeypatch):
        db = _make_db()
        primary = _make_user(id=1, google_id='g_primary')
        secondary = _make_user(id=2, yandex_id='y_sec', discord_id='d_sec', vk_id=12345)
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete():
            result = await execute_merge(db, 1, 2)

        # google_id stays on primary (already set)
        assert result.google_id == 'g_primary'
        # transferred from secondary
        assert result.yandex_id == 'y_sec'
        assert result.discord_id == 'd_sec'
        assert result.vk_id == 12345
        # cleared on secondary
        assert secondary.yandex_id is None
        assert secondary.discord_id is None
        assert secondary.vk_id is None

    async def test_does_not_overwrite_existing_oauth(self, monkeypatch):
        db = _make_db()
        primary = _make_user(id=1, google_id='g_primary')
        secondary = _make_user(id=2, google_id='g_secondary')
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete():
            result = await execute_merge(db, 1, 2)

        # Primary keeps its own google_id
        assert result.google_id == 'g_primary'
        # Secondary's conflicting google_id is cleared (unique constraint cleanup)
        assert secondary.google_id is None


class TestExecuteMergeTelegramTransfer:
    async def test_transfers_telegram_id(self, monkeypatch):
        db = _make_db()
        primary = _make_user(id=1)
        secondary = _make_user(id=2, telegram_id=99999)
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete():
            result = await execute_merge(db, 1, 2)

        assert result.telegram_id == 99999
        assert secondary.telegram_id is None

    async def test_does_not_overwrite_telegram_id(self, monkeypatch):
        db = _make_db()
        primary = _make_user(id=1, telegram_id=11111)
        secondary = _make_user(id=2, telegram_id=22222)
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete():
            result = await execute_merge(db, 1, 2)

        assert result.telegram_id == 11111
        # Secondary's telegram_id cleared during cleanup (unique constraint)
        assert secondary.telegram_id is None


class TestExecuteMergeEmailTransfer:
    async def test_transfers_email_and_password(self, monkeypatch):
        db = _make_db()
        primary = _make_user(id=1)
        secondary = _make_user(
            id=2,
            email='sec@example.com',
            email_verified=True,
            email_verified_at=datetime(2024, 6, 1, tzinfo=UTC),
            password_hash='hash_sec',
        )
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete():
            result = await execute_merge(db, 1, 2)

        assert result.email == 'sec@example.com'
        assert result.email_verified is True
        assert result.password_hash == 'hash_sec'
        # secondary cleared
        assert secondary.email is None
        assert secondary.password_hash is None

    async def test_does_not_overwrite_existing_email(self, monkeypatch):
        db = _make_db()
        primary = _make_user(id=1, email='pri@example.com', password_hash='hash_pri')
        secondary = _make_user(id=2, email='sec@example.com', password_hash='hash_sec')
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete():
            result = await execute_merge(db, 1, 2)

        assert result.email == 'pri@example.com'


class TestExecuteMergeBalance:
    async def test_sums_balances(self, monkeypatch):
        db = _make_db()
        primary = _make_user(id=1, balance_kopeks=5000)
        secondary = _make_user(id=2, balance_kopeks=3000)
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete():
            result = await execute_merge(db, 1, 2)

        assert result.balance_kopeks == 8000
        assert secondary.balance_kopeks == 0

    async def test_negative_balance_transferred(self, monkeypatch):
        """Negative balance (debt) must be transferred, not silently discarded."""
        db = _make_db()
        primary = _make_user(id=1, balance_kopeks=5000)
        secondary = _make_user(id=2, balance_kopeks=-2000)
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete():
            result = await execute_merge(db, 1, 2)

        assert result.balance_kopeks == 3000
        assert secondary.balance_kopeks == 0

    async def test_zero_secondary_balance_unchanged(self, monkeypatch):
        db = _make_db()
        primary = _make_user(id=1, balance_kopeks=5000)
        secondary = _make_user(id=2, balance_kopeks=0)
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete():
            result = await execute_merge(db, 1, 2)

        assert result.balance_kopeks == 5000


class TestExecuteMergePartnerStatus:
    async def test_higher_priority_transferred(self, monkeypatch):
        db = _make_db()
        primary = _make_user(id=1, partner_status='none')
        secondary = _make_user(id=2, partner_status='approved')
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete():
            result = await execute_merge(db, 1, 2)

        assert result.partner_status == 'approved'

    async def test_lower_priority_not_overwritten(self, monkeypatch):
        db = _make_db()
        primary = _make_user(id=1, partner_status='approved')
        secondary = _make_user(id=2, partner_status='pending')
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete():
            result = await execute_merge(db, 1, 2)

        assert result.partner_status == 'approved'

    async def test_pending_beats_rejected(self, monkeypatch):
        """Pending application should not be overwritten by rejected status."""
        db = _make_db()
        primary = _make_user(id=1, partner_status='pending')
        secondary = _make_user(id=2, partner_status='rejected')
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete():
            result = await execute_merge(db, 1, 2)

        assert result.partner_status == 'pending'

    async def test_rejected_does_not_beat_pending(self, monkeypatch):
        """Rejected on secondary should not overwrite pending on primary."""
        db = _make_db()
        primary = _make_user(id=1, partner_status='rejected')
        secondary = _make_user(id=2, partner_status='pending')
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete():
            result = await execute_merge(db, 1, 2)

        assert result.partner_status == 'pending'


class TestExecuteMergeReferralCommission:
    async def test_transfers_if_primary_has_none(self, monkeypatch):
        db = _make_db()
        primary = _make_user(id=1, referral_commission_percent=None)
        secondary = _make_user(id=2, referral_commission_percent=15)
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete():
            result = await execute_merge(db, 1, 2)

        assert result.referral_commission_percent == 15

    async def test_does_not_overwrite_if_primary_has_value(self, monkeypatch):
        db = _make_db()
        primary = _make_user(id=1, referral_commission_percent=20)
        secondary = _make_user(id=2, referral_commission_percent=15)
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete():
            result = await execute_merge(db, 1, 2)

        assert result.referral_commission_percent == 20


# ---------------------------------------------------------------------------
# execute_merge — secondary marked as deleted
# ---------------------------------------------------------------------------


class TestExecuteMergeSecondaryDeleted:
    async def test_secondary_marked_deleted(self, monkeypatch):
        db = _make_db()
        primary = _make_user(id=1)
        secondary = _make_user(id=2, referral_code='REF123', email='sec@e.com')
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete():
            await execute_merge(db, 1, 2)

        assert secondary.status == 'deleted'
        assert secondary.referral_code is None
        assert secondary.remnawave_uuid is None
        assert secondary.email is None

    async def test_all_unique_fields_cleared_on_secondary(self, monkeypatch):
        """All unique constraint fields must be cleared on secondary after merge."""
        db = _make_db()
        # Primary has its own OAuth + telegram, so secondary's won't transfer
        primary = _make_user(id=1, telegram_id=111, google_id='g1', yandex_id='y1')
        secondary = _make_user(
            id=2,
            telegram_id=222,
            google_id='g2',
            yandex_id='y2',
            discord_id='d2',
            vk_id=999,
            email='sec@e.com',
            referral_code='REF',
            remnawave_uuid='rw-sec',
        )
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete():
            await execute_merge(db, 1, 2)

        # ALL unique fields cleared on secondary
        assert secondary.telegram_id is None
        assert secondary.google_id is None
        assert secondary.yandex_id is None
        assert secondary.discord_id is None
        assert secondary.vk_id is None
        assert secondary.email is None
        assert secondary.referral_code is None
        assert secondary.remnawave_uuid is None

    async def test_db_flush_called(self, monkeypatch):
        db = _make_db()
        primary = _make_user(id=1)
        secondary = _make_user(id=2)
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete():
            await execute_merge(db, 1, 2)

        db.flush.assert_awaited_once()


# ---------------------------------------------------------------------------
# execute_merge — subscription merge scenarios
# ---------------------------------------------------------------------------


class TestExecuteMergeSubscription:
    async def test_neither_has_subscription(self, monkeypatch):
        db = _make_db()
        primary = _make_user(id=1)
        secondary = _make_user(id=2)
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete() as mock_del:
            await execute_merge(db, 1, 2)
            mock_del.assert_not_awaited()

    async def test_only_primary_has_subscription(self, monkeypatch):
        db = _make_db()
        sub = _make_subscription(user_id=1)
        primary = _make_user(id=1, subscription=sub, remnawave_uuid='rw-primary')
        secondary = _make_user(id=2, remnawave_uuid='rw-secondary')
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete() as mock_del:
            await execute_merge(db, 1, 2)
            mock_del.assert_awaited_once_with('rw-secondary')

        # secondary remnawave_uuid cleared
        assert secondary.remnawave_uuid is None

    async def test_only_secondary_has_subscription(self, monkeypatch):
        db = _make_db()
        sub = _make_subscription(user_id=2)
        primary = _make_user(id=1)
        secondary = _make_user(id=2, subscription=sub, remnawave_uuid='rw-secondary')
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete():
            await execute_merge(db, 1, 2)

        # Subscription transferred to primary
        assert sub.user_id == 1
        assert primary.remnawave_uuid == 'rw-secondary'
        assert secondary.remnawave_uuid is None

    async def test_both_have_subscription_keep_primary(self, monkeypatch):
        db = _make_db()
        sub_p = _make_subscription(user_id=1)
        sub_s = _make_subscription(user_id=2)
        primary = _make_user(id=1, subscription=sub_p, remnawave_uuid='rw-primary')
        secondary = _make_user(id=2, subscription=sub_s, remnawave_uuid='rw-secondary')
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete() as mock_del:
            await execute_merge(db, 1, 2, keep_subscription_from='primary')
            mock_del.assert_awaited_once_with('rw-secondary')

        db.delete.assert_awaited_once_with(sub_s)

    async def test_both_have_subscription_keep_secondary(self, monkeypatch):
        db = _make_db()
        sub_p = _make_subscription(user_id=1)
        sub_s = _make_subscription(user_id=2)
        primary = _make_user(id=1, subscription=sub_p, remnawave_uuid='rw-primary')
        secondary = _make_user(id=2, subscription=sub_s, remnawave_uuid='rw-secondary')
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete() as mock_del:
            await execute_merge(db, 1, 2, keep_subscription_from='secondary')
            mock_del.assert_awaited_once_with('rw-primary')

        db.delete.assert_awaited_once_with(sub_p)
        # Secondary subscription transferred
        assert sub_s.user_id == 1
        assert primary.remnawave_uuid == 'rw-secondary'


# ---------------------------------------------------------------------------
# execute_merge — bulk updates called
# ---------------------------------------------------------------------------


class TestExecuteMergeBulkUpdates:
    async def test_execute_called_for_transactions_and_payments(self, monkeypatch):
        db = _make_db()
        primary = _make_user(id=1)
        secondary = _make_user(id=2)
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete():
            await execute_merge(db, 1, 2)

        # Minimum: Transaction(1) + payment models(10) + cross-referral DELETE(1)
        # + referral_earnings(2) + referral chain(1) + withdrawal_requests(1) + refresh tokens(1) = 17
        assert db.execute.await_count >= 17


# ---------------------------------------------------------------------------
# execute_merge — self-referral prevention
# ---------------------------------------------------------------------------


class TestExecuteMergeSelfReferralPrevention:
    async def test_primary_referred_by_secondary_cleared(self, monkeypatch):
        """If primary was referred by secondary, referred_by_id must be cleared."""
        db = _make_db()
        primary = _make_user(id=1, referred_by_id=2)
        secondary = _make_user(id=2)
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete():
            result = await execute_merge(db, 1, 2)

        assert result.referred_by_id is None

    async def test_primary_referred_by_other_preserved(self, monkeypatch):
        """If primary was referred by a third user, referred_by_id stays."""
        db = _make_db()
        primary = _make_user(id=1, referred_by_id=99)
        secondary = _make_user(id=2)
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete():
            result = await execute_merge(db, 1, 2)

        assert result.referred_by_id == 99

    async def test_secondary_referred_by_id_cleared(self, monkeypatch):
        """Secondary's referred_by_id must be cleared during cleanup."""
        db = _make_db()
        primary = _make_user(id=1)
        secondary = _make_user(id=2, referred_by_id=99)
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete():
            await execute_merge(db, 1, 2)

        assert secondary.referred_by_id is None

    async def test_secondary_referrer_transferred_to_primary(self, monkeypatch):
        """If primary has no referrer but secondary does, transfer it."""
        db = _make_db()
        primary = _make_user(id=1, referred_by_id=None)
        secondary = _make_user(id=2, referred_by_id=99)
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete():
            result = await execute_merge(db, 1, 2)

        assert result.referred_by_id == 99

    async def test_secondary_referrer_not_transferred_if_primary_has_one(self, monkeypatch):
        """If primary already has a referrer, secondary's is not transferred."""
        db = _make_db()
        primary = _make_user(id=1, referred_by_id=50)
        secondary = _make_user(id=2, referred_by_id=99)
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete():
            result = await execute_merge(db, 1, 2)

        assert result.referred_by_id == 50

    async def test_secondary_referrer_pointing_to_primary_not_transferred(self, monkeypatch):
        """If secondary was referred by primary, don't create self-referral."""
        db = _make_db()
        primary = _make_user(id=1, referred_by_id=None)
        secondary = _make_user(id=2, referred_by_id=1)
        monkeypatch.setattr(
            account_merge_service,
            'get_user_by_id',
            AsyncMock(side_effect=[primary, secondary]),
        )
        with _patch_remnawave_delete():
            result = await execute_merge(db, 1, 2)

        assert result.referred_by_id is None
