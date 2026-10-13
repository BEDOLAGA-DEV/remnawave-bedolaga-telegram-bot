"""Feature (#2): admin "reset subscription" — fully zero out a subscription
("as if the user never spammed it") WITHOUT deleting the user from the bot DB,
so support tickets survive. The RemnaWave panel user is DISABLED (not deleted).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.subscription_service as ss
from app.cabinet.routes import admin_users
from app.cabinet.schemas.users import ResetSubscriptionRequest
from app.config import Settings
from app.database.crud.subscription import reset_subscription
from app.database.models import SubscriptionStatus
from app.services.panel_sync import BULK_SNAPSHOT, project_onto_subscription, read_panel_user


def _set_multi_tariff(monkeypatch, enabled: bool):
    # is_multi_tariff_enabled is a method on the pydantic Settings class.
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: enabled)


def _sub(**kw) -> SimpleNamespace:
    base = dict(
        id=7,
        user_id=1,
        status=SubscriptionStatus.ACTIVE.value,
        end_date=datetime.now(UTC) + timedelta(days=1000),  # spammed far into the future
        connected_squads=['sq1', 'sq2'],
        traffic_used_gb=42.5,
        autopay_enabled=True,
        # RemnaWave 3.0.0: панельный юзер адресуется числовым id, а не uuid.
        remnawave_id=7001,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _record_extinguish(monkeypatch) -> list[int]:
    """Гашение даты в панели — отдельные тесты ниже; здесь только куда оно ушло."""
    extinguished: list[int] = []

    async def fake_extinguish(self, _db, _user, _subscription, panel_user_id):
        extinguished.append(panel_user_id)
        return True

    monkeypatch.setattr(ss.SubscriptionService, 'extinguish_remnawave_expiry', fake_extinguish)
    return extinguished


async def test_reset_subscription_zeroes_fields():
    sub = _sub()
    before = datetime.now(UTC)

    await reset_subscription(AsyncMock(), sub, commit=False)

    assert sub.status == SubscriptionStatus.DISABLED.value
    # spammed days gone: end_date snapped to ~now
    assert before <= sub.end_date <= datetime.now(UTC)
    assert sub.connected_squads == []
    assert sub.traffic_used_gb == 0.0
    assert sub.autopay_enabled is False


async def test_reset_with_panel_disables_subscription_panel_id(monkeypatch):
    disabled: list[int] = []

    async def fake_disable(self, panel_user_id):
        disabled.append(panel_user_id)
        return True

    monkeypatch.setattr(ss.SubscriptionService, 'disable_remnawave_user', fake_disable)
    extinguished = _record_extinguish(monkeypatch)
    _set_multi_tariff(monkeypatch, True)

    sub = _sub(remnawave_id=7001)
    user = SimpleNamespace(id=1, remnawave_id=9001)

    result = await ss.reset_subscription_with_panel(AsyncMock(), user, sub)

    assert disabled == [7001]  # per-subscription panel id (never the user-level one)
    assert extinguished == [7001]  # дату гасим у того же аккаунта
    assert result['panel_disabled'] is True
    assert result['panel_user_id'] == 7001
    assert sub.status == SubscriptionStatus.DISABLED.value  # DB reset applied too


async def test_reset_with_panel_multitariff_no_sub_panel_id_skips_panel(monkeypatch):
    """Multi-tariff + no per-sub panel id → must NOT fall back to user.remnawave_id
    (that legacy id could belong to a different active sub). Panel is skipped."""
    disabled: list[int] = []

    async def fake_disable(self, panel_user_id):
        disabled.append(panel_user_id)
        return True

    monkeypatch.setattr(ss.SubscriptionService, 'disable_remnawave_user', fake_disable)
    _set_multi_tariff(monkeypatch, True)

    sub = _sub(remnawave_id=None)
    user = SimpleNamespace(id=1, remnawave_id=9001)

    result = await ss.reset_subscription_with_panel(AsyncMock(), user, sub)

    assert disabled == []  # no fallback in multi-tariff
    assert result['panel_disabled'] is False
    assert sub.status == SubscriptionStatus.DISABLED.value  # bot-side reset still applied


async def test_reset_with_panel_singletariff_falls_back_to_user_panel_id(monkeypatch):
    disabled: list[int] = []

    async def fake_disable(self, panel_user_id):
        disabled.append(panel_user_id)
        return True

    monkeypatch.setattr(ss.SubscriptionService, 'disable_remnawave_user', fake_disable)
    extinguished = _record_extinguish(monkeypatch)
    _set_multi_tariff(monkeypatch, False)

    sub = _sub(remnawave_id=None)
    user = SimpleNamespace(id=1, remnawave_id=9001)

    await ss.reset_subscription_with_panel(AsyncMock(), user, sub)

    assert disabled == [9001]  # legacy single-tariff fallback is correct here
    assert extinguished == [9001]


async def test_reset_with_panel_no_panel_id_skips_panel(monkeypatch):
    called: list[int] = []

    async def fake_disable(self, panel_user_id):
        called.append(panel_user_id)
        return True

    monkeypatch.setattr(ss.SubscriptionService, 'disable_remnawave_user', fake_disable)

    sub = _sub(remnawave_id=None)
    user = SimpleNamespace(id=1, remnawave_id=None)

    result = await ss.reset_subscription_with_panel(AsyncMock(), user, sub)

    assert called == []  # nothing to disable
    assert result['panel_disabled'] is False
    assert sub.status == SubscriptionStatus.DISABLED.value  # bot-side reset still applied


async def test_reset_with_panel_survives_panel_error(monkeypatch):
    """A panel disable failure must not block the bot-side reset (best effort)."""

    async def boom(self, panel_user_id):
        raise RuntimeError('panel down')

    monkeypatch.setattr(ss.SubscriptionService, 'disable_remnawave_user', boom)
    _record_extinguish(monkeypatch)

    sub = _sub(remnawave_id=7001)
    user = SimpleNamespace(id=1, remnawave_id=None)

    result = await ss.reset_subscription_with_panel(AsyncMock(), user, sub)

    assert result['panel_disabled'] is False
    assert sub.status == SubscriptionStatus.DISABLED.value


def _owner(**kw) -> SimpleNamespace:
    base = dict(id=1, full_name='Owner', username=None, telegram_id=1000, email=None, status='active')
    base.update(kw)
    return SimpleNamespace(**base)


def _panel_keeps_paid_date(monkeypatch, *, paid_until: datetime) -> tuple[list[dict], dict]:
    """Панель держит оплаченный срок, пока его не перепишут PATCH-ем.

    Отключение дату не трогает — как в настоящей панели. Возвращает отправленные
    PATCH-и и состояние панели.
    """
    updates: list[dict] = []
    panel = {'expire_at': paid_until}

    async def fake_disable(self, panel_user_id):
        return True

    async def update_panel_user(_api, _subscription_id, **kwargs):
        updates.append(kwargs)
        if 'expire_at' in kwargs:
            panel['expire_at'] = kwargs['expire_at']
        return SimpleNamespace(
            id=kwargs['user_id'],
            expire_at=panel['expire_at'],
            subscription_url=None,
            happ_crypto_link=None,
            short_uuid=None,
        )

    async def get_user_by_id(user_id):
        return SimpleNamespace(id=user_id, expire_at=panel['expire_at'])

    async def create_user(**_kwargs):
        raise AssertionError('аккаунт в панели есть — создавать нечего')

    @asynccontextmanager
    async def api_client(_self):
        yield SimpleNamespace(get_user_by_id=get_user_by_id, create_user=create_user)

    monkeypatch.setattr(ss.SubscriptionService, 'disable_remnawave_user', fake_disable)
    monkeypatch.setattr(ss.SubscriptionService, 'get_api_client', api_client)
    monkeypatch.setattr('app.services.grace_access_runtime.update_panel_user_grace_safe', update_panel_user)
    _set_multi_tariff(monkeypatch, True)
    return updates, panel


async def test_reset_with_panel_extinguishes_paid_date_in_panel(monkeypatch):
    """Обнуление гасило срок только в боте: отключение дату в панели не трогает,
    и там оставался оплаченный срок."""
    now = datetime.now(UTC)
    updates, panel = _panel_keeps_paid_date(monkeypatch, paid_until=now + timedelta(days=30))
    sub = _sub(remnawave_id=7001, end_date=now + timedelta(days=30))

    result = await ss.reset_subscription_with_panel(AsyncMock(), _owner(), sub)

    assert result['panel_expiry_extinguished'] is True
    # Один PATCH тому же аккаунту, что отключали: дата и статус вместе, без эха со старой датой.
    assert [update['user_id'] for update in updates] == [7001]
    assert now < panel['expire_at'] < datetime.now(UTC) + timedelta(minutes=20)


async def test_panel_truth_sync_after_reset_does_not_return_paid_days(monkeypatch):
    """Сверка «панель — истина» переносит дату панели в бота при любом статусе.

    С оплаченным сроком в панели первый же полный проход возвращал его в
    обнулённую подписку, а реактивация (возврат в обязательный канал,
    ``user.enabled``) снова включала её до этой даты.
    """
    now = datetime.now(UTC)
    _updates, panel = _panel_keeps_paid_date(monkeypatch, paid_until=now + timedelta(days=30))
    sub = _sub(remnawave_id=7001, end_date=now + timedelta(days=30))

    await ss.reset_subscription_with_panel(AsyncMock(), _owner(), sub)
    snapshot = read_panel_user({'status': 'DISABLED', 'expireAt': panel['expire_at'].isoformat()})
    project_onto_subscription(sub, snapshot, policy=BULK_SNAPSHOT)

    assert sub.status == SubscriptionStatus.DISABLED.value
    assert sub.end_date < datetime.now(UTC) + timedelta(minutes=20)


async def test_reset_with_panel_survives_extinguish_error(monkeypatch):
    """Ошибка панели при гашении даты не отменяет обнуления в боте."""

    async def fake_disable(self, panel_user_id):
        return True

    @asynccontextmanager
    async def broken_client(_self):
        raise RuntimeError('panel down')
        yield

    monkeypatch.setattr(ss.SubscriptionService, 'disable_remnawave_user', fake_disable)
    monkeypatch.setattr(ss.SubscriptionService, 'get_api_client', broken_client)
    _set_multi_tariff(monkeypatch, True)

    sub = _sub(remnawave_id=7001)
    result = await ss.reset_subscription_with_panel(AsyncMock(), _owner(), sub)

    assert result['panel_disabled'] is True
    assert result['panel_expiry_extinguished'] is False
    assert sub.status == SubscriptionStatus.DISABLED.value


async def test_user_modified_does_not_resurrect_disabled_end_date():
    """A user.modified webhook carrying a stale FUTURE expireAt must NOT restore the
    end_date of a subscription the bot deliberately DISABLED (e.g. after reset) —
    otherwise the spammed days would silently come back."""
    import app.services.remnawave_webhook_service as rw

    svc = rw.RemnaWaveWebhookService(MagicMock())
    svc._stamp_webhook_update = MagicMock()

    now = datetime.now(UTC)
    reset_end = now
    sub = SimpleNamespace(
        id=7,
        status=SubscriptionStatus.DISABLED.value,
        end_date=reset_end,
        traffic_limit_gb=0,
        traffic_used_gb=0.0,
        connected_squads=[],
        subscription_url='https://x',
        subscription_crypto_link='crypto',
        updated_at=now,
    )
    user = SimpleNamespace(id=1, telegram_id=123)
    data = {'expireAt': (now + timedelta(days=900)).isoformat(), 'status': 'DISABLED'}

    await svc._handle_user_modified(AsyncMock(), user, sub, data)

    assert sub.end_date == reset_end  # NOT pushed back to +900 days
    assert sub.status == SubscriptionStatus.DISABLED.value


async def test_user_modified_still_syncs_end_date_for_active():
    """Regression guard: ACTIVE subs still get end_date synced from the panel."""
    import app.services.remnawave_webhook_service as rw

    svc = rw.RemnaWaveWebhookService(MagicMock())
    svc._stamp_webhook_update = MagicMock()

    now = datetime.now(UTC)
    sub = SimpleNamespace(
        id=7,
        status=SubscriptionStatus.ACTIVE.value,
        end_date=now + timedelta(days=10),
        traffic_limit_gb=0,
        traffic_used_gb=0.0,
        connected_squads=[],
        subscription_url='https://x',
        subscription_crypto_link='crypto',
        updated_at=now,
    )
    user = SimpleNamespace(id=1, telegram_id=123)
    new_expiry = now + timedelta(days=30)
    data = {'expireAt': new_expiry.isoformat(), 'status': 'ACTIVE'}

    await svc._handle_user_modified(AsyncMock(), user, sub, data)

    assert abs((sub.end_date - new_expiry).total_seconds()) < 2  # synced from panel


async def test_user_level_reset_deletes_all_three_current_subscriptions(monkeypatch):
    """The user reset remains one user-scoped operation, not a selected-sub reset."""
    subscriptions = [_sub(id=sub_id, remnawave_id=7000 + sub_id) for sub_id in (7, 8, 9)]
    user = SimpleNamespace(id=1, subscriptions=subscriptions, updated_at=None, remnawave_id=None)
    db = AsyncMock()
    grace_checks: list[tuple[int, ...]] = []
    cancelled: list[int] = []

    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))

    async def ensure_no_open_grace(_db, subscription_ids):
        grace_checks.append(subscription_ids)

    async def cancel_recurrent(_db, subscription_id):
        cancelled.append(subscription_id)

    monkeypatch.setattr(
        'app.services.grace_access_runtime.ensure_no_open_grace_for_subscriptions', ensure_no_open_grace
    )
    monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', cancel_recurrent)
    monkeypatch.setattr('app.services.payment.lava.cancel_lava_recurring_for_subscription_safe', cancel_recurrent)

    result = await admin_users.reset_user_subscription(
        1,
        ResetSubscriptionRequest(deactivate_in_panel=False),
        admin=SimpleNamespace(id=99),
        db=db,
    )

    assert result.subscription_deleted is True
    assert grace_checks == [(7, 8, 9), (7, 8, 9)]
    # Отменяются оба рекуррента (Platega и Lava) по каждой подписке — иначе живая
    # привязка спишет деньги и воскресит только что сброшенную подписку.
    assert cancelled == [7, 7, 8, 8, 9, 9]
    deletes = [call.args[0] for call in db.execute.await_args_list if str(call.args[0]).startswith('DELETE')]
    assert len(deletes) == 4  # three server cleanups plus one user-scoped subscription deletion
    delete_statement = deletes[-1]
    compiled_delete = delete_statement.compile()
    assert 'DELETE FROM subscriptions' in str(delete_statement)
    assert 'subscriptions.user_id' in str(delete_statement)
    assert 1 in compiled_delete.params.values()
    assert db.commit.await_count == 1


@pytest.mark.parametrize(
    'outcomes',
    [(True, False), (RuntimeError('panel down'),)],
    ids=['mixed-false-result', 'exception'],
)
async def test_user_level_reset_panel_failure_preserves_all_subscription_retry_identities(monkeypatch, outcomes):
    subscriptions = [_sub(id=sub_id, remnawave_id=7000 + sub_id) for sub_id in (7, 8)]
    user = SimpleNamespace(id=1, subscriptions=subscriptions, updated_at=None, remnawave_id=9001)
    db = AsyncMock()
    panel_calls: list[int] = []
    configured_outcomes = iter(outcomes)

    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    _set_multi_tariff(monkeypatch, True)

    async def ensure_no_open_grace(_db, _subscription_ids):
        return None

    async def cancel_recurrent(_db, _subscription_id):
        return None

    class PanelService:
        async def disable_remnawave_user(self, panel_user_id, *, db):
            panel_calls.append(panel_user_id)
            outcome = next(configured_outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    monkeypatch.setattr(
        'app.services.grace_access_runtime.ensure_no_open_grace_for_subscriptions', ensure_no_open_grace
    )
    monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', cancel_recurrent)
    monkeypatch.setattr('app.services.payment.lava.cancel_lava_recurring_for_subscription_safe', cancel_recurrent)
    monkeypatch.setattr('app.services.subscription_service.SubscriptionService', PanelService)

    result = await admin_users.reset_user_subscription(
        1,
        ResetSubscriptionRequest(deactivate_in_panel=True),
        admin=SimpleNamespace(id=99),
        db=db,
    )

    assert result.success is False
    assert result.subscription_deleted is False
    assert result.panel_deactivated is False
    assert panel_calls == ([7007, 7008] if len(outcomes) == 2 else [7007])
    # Панельные id подписок обязаны уцелеть: только по ним админ повторит операцию.
    assert [sub.remnawave_id for sub in subscriptions] == [7007, 7008]
    assert not [call for call in db.execute.await_args_list if str(call.args[0]).startswith('DELETE')], (
        'при незавершённой деактивации в панели ничего удалять нельзя'
    )
    db.commit.assert_not_awaited()
