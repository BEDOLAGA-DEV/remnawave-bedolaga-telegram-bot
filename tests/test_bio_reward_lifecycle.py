"""Lifecycle tests for bio-reward fixes.

Covers: _wl panel guard for bio subs, extend/revoke guards against
converted-to-paid rows, panel push on extend/revoke, transient
fetch-failure handling in check_user.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace


class FakeDb:
    """Minimal async-session stand-in: identity get() map + commit counter."""

    def __init__(self, get_map: dict | None = None):
        self._get_map = get_map or {}
        self.commits = 0

    async def get(self, model, pk):
        return self._get_map.get(pk)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        pass

    def add(self, obj):
        pass


class FakeApi:
    """Records RemnaWave API calls; `existing` maps username -> user obj."""

    def __init__(self, existing: dict | None = None):
        self.existing = existing or {}
        self.created: list[dict] = []
        self.updated: list[tuple] = []
        self.deleted: list[str] = []

    async def get_user_by_username(self, username):
        return self.existing.get(username)

    async def create_user(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(uuid=f'uuid-{len(self.created)}')

    async def update_user(self, uuid, **kwargs):
        self.updated.append((uuid, kwargs))
        return SimpleNamespace(uuid=uuid)

    async def delete_user(self, uuid):
        self.deleted.append(uuid)
        return True


def _sub(**over):
    base = dict(
        id=101,
        user_id=7,
        is_bio_reward=True,
        is_trial=True,
        status='active',
        start_date=datetime.now(UTC) - timedelta(days=1),
        end_date=datetime.now(UTC) + timedelta(days=2),
        wl_traffic_limit_gb=None,
        wl_traffic_used_gb=0.0,
        wl_purchased_traffic_gb=0,
        wl_traffic_reset_at=None,
        device_limit=1,
        tariff=None,
        tariff_id=None,
        frozen_at=None,
        connected_squads=[],
        remnawave_uuid=None,
        bio_reward_discount_percent=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _user(**over):
    base = dict(
        id=7,
        telegram_id=555,
        username='tg_user',
        full_name='Test User',
        email=None,
        remnawave_uuid='main-uuid',
        referral_code='refABC',
        balance_kopeks=0,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _participant(**over):
    base = dict(
        id=1,
        user_id=7,
        status='active',
        bypass_check=False,
        bio_snapshot='',
        last_bio_seen_at=None,
        last_check_at=None,
        grace_started_at=None,
        cooldown_until=None,
        revoked_at=None,
        opted_in_at=None,
        free_subscription_id=None,
        user=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _bio_cfg(**over):
    base = dict(
        enabled=True,
        discount_percent=10,
        accepted_bio_strings=[],
        match_personal_referral_link=False,
        grace_period_hours=3,
        cooldown_hours=48,
        check_interval_minutes=60,
        free_sub_window_days=3,
        free_sub_squad_uuid=None,
        free_sub_traffic_gb_per_day=1,
        free_sub_device_limit=1,
        notify_on_activate=False,
        notify_on_grace=False,
        notify_on_revoke=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _patch_subscription_service(monkeypatch):
    """Replace SubscriptionService with a recorder; returns list of pushed subs."""
    import app.services.subscription_service as ss_module

    calls: list = []

    class FakeSvc:
        async def update_remnawave_user(
            self, db, sub, *, reset_traffic=False, reset_reason=None, sync_squads=False
        ):
            calls.append(sub)
            return SimpleNamespace(uuid='pushed')

    monkeypatch.setattr(ss_module, 'SubscriptionService', FakeSvc)
    return calls


# ---------- Fix 1: _ensure_wl_user_synced bio guard ----------


async def test_wl_sync_skips_bio_sub_and_deletes_leftover():
    from app.services.subscription_service import SubscriptionService

    svc = SubscriptionService()
    api = FakeApi(existing={'u_555_wl': SimpleNamespace(uuid='wl-uuid')})
    await svc._ensure_wl_user_synced(
        api, _user(), _sub(is_bio_reward=True), True, main_username='u_555'
    )
    assert api.created == []
    assert api.updated == []
    assert api.deleted == ['wl-uuid']


async def test_wl_sync_skips_bio_sub_without_leftover():
    from app.services.subscription_service import SubscriptionService

    svc = SubscriptionService()
    api = FakeApi()
    await svc._ensure_wl_user_synced(
        api, _user(), _sub(is_bio_reward=True), True, main_username='u_555'
    )
    assert api.created == []
    assert api.updated == []
    assert api.deleted == []


async def test_wl_sync_still_creates_wl_for_paid_sub():
    from app.services.subscription_service import SubscriptionService

    svc = SubscriptionService()
    api = FakeApi()
    await svc._ensure_wl_user_synced(
        api,
        _user(),
        _sub(is_bio_reward=False, is_trial=False, wl_traffic_limit_gb=5),
        True,
        main_username='u_555',
    )
    assert len(api.created) == 1
    assert api.created[0]['username'] == 'u_555_wl'
    assert api.deleted == []


# ---------- Fix 6: transient fetch failure ----------


def _patch_get_config(monkeypatch, cfg):
    from app.database.crud import bio_reward as bio_crud_module

    async def fake_get_config(db):
        return cfg

    monkeypatch.setattr(bio_crud_module, 'get_config', fake_get_config)


async def test_check_user_fetch_failure_keeps_state(monkeypatch):
    from app.database.models import BioRewardStatus
    from app.services.bio_reward_service import BioRewardService

    _patch_get_config(monkeypatch, _bio_cfg())
    svc = BioRewardService()  # bot не установлен -> _fetch_bio вернёт None
    participant = _participant(status=BioRewardStatus.ACTIVE.value, bio_snapshot='keep-me')
    outcome = await svc.check_user(FakeDb(), participant, user=_user())
    assert outcome == 'fetch_failed'
    assert participant.status == BioRewardStatus.ACTIVE.value
    assert participant.bio_snapshot == 'keep-me'
    assert participant.grace_started_at is None


async def test_check_user_fetch_failure_in_grace_does_not_revoke(monkeypatch):
    from app.database.models import BioRewardStatus
    from app.services.bio_reward_service import BioRewardService

    _patch_get_config(monkeypatch, _bio_cfg(grace_period_hours=3))
    svc = BioRewardService()  # bot не установлен -> _fetch_bio вернёт None
    grace_started = datetime.now(UTC) - timedelta(hours=10)  # дедлайн давно прошёл
    participant = _participant(
        status=BioRewardStatus.GRACE.value, grace_started_at=grace_started
    )
    outcome = await svc.check_user(FakeDb(), participant, user=_user())
    assert outcome == 'fetch_failed'
    assert participant.status == BioRewardStatus.GRACE.value
    assert participant.grace_started_at == grace_started
    assert participant.revoked_at is None


async def test_check_user_empty_bio_still_starts_grace(monkeypatch):
    from app.database.models import BioRewardStatus
    from app.services.bio_reward_service import BioRewardService

    _patch_get_config(monkeypatch, _bio_cfg())
    svc = BioRewardService()

    async def fake_fetch(telegram_id):
        return ''  # bio реально пуст — это НЕ ошибка запроса

    svc._fetch_bio = fake_fetch
    participant = _participant(status=BioRewardStatus.ACTIVE.value)
    outcome = await svc.check_user(FakeDb(), participant, user=_user())
    assert outcome == 'grace_started'
    assert participant.status == BioRewardStatus.GRACE.value


async def test_check_user_fetch_failure_with_bypass_still_matches(monkeypatch):
    from app.database.models import BioRewardStatus
    from app.services.bio_reward_service import BioRewardService

    _patch_get_config(monkeypatch, _bio_cfg())
    svc = BioRewardService()
    participant = _participant(
        status=BioRewardStatus.ACTIVE.value, bypass_check=True, free_subscription_id=None
    )
    outcome = await svc.check_user(FakeDb(), participant, user=_user())
    assert outcome == 'extended'


# ---------- Fix 3 + 5: _extend_free_sub ----------


async def test_extend_detaches_converted_paid_sub(monkeypatch):
    from app.services.bio_reward_service import BioRewardService

    calls = _patch_subscription_service(monkeypatch)
    svc = BioRewardService()
    sub = _sub(is_bio_reward=False, status='active', end_date=datetime.now(UTC) + timedelta(hours=5))
    old_end = sub.end_date
    participant = _participant(free_subscription_id=101, user=_user())
    await svc._extend_free_sub(FakeDb(get_map={101: sub}), participant, _bio_cfg())
    assert participant.free_subscription_id is None
    assert sub.end_date == old_end
    assert sub.status == 'active'
    assert calls == []


async def test_extend_pushes_end_date_to_panel(monkeypatch):
    from app.services.bio_reward_service import BioRewardService

    calls = _patch_subscription_service(monkeypatch)
    svc = BioRewardService()
    sub = _sub(is_bio_reward=True, end_date=datetime.now(UTC) + timedelta(days=1))
    participant = _participant(free_subscription_id=101, user=_user())
    await svc._extend_free_sub(FakeDb(get_map={101: sub}), participant, _bio_cfg(free_sub_window_days=3))
    assert sub.end_date > datetime.now(UTC) + timedelta(days=2)
    assert calls == [sub]


async def test_extend_no_push_when_nothing_changed(monkeypatch):
    from app.services.bio_reward_service import BioRewardService

    calls = _patch_subscription_service(monkeypatch)
    svc = BioRewardService()
    sub = _sub(is_bio_reward=True, end_date=datetime.now(UTC) + timedelta(days=10))
    participant = _participant(free_subscription_id=101, user=_user())
    await svc._extend_free_sub(FakeDb(get_map={101: sub}), participant, _bio_cfg(free_sub_window_days=3))
    assert calls == []


async def test_extend_never_resurrects_disabled_bio_sub(monkeypatch):
    from app.database.models import SubscriptionStatus
    from app.services.bio_reward_service import BioRewardService

    calls = _patch_subscription_service(monkeypatch)
    svc = BioRewardService()
    old_end = datetime.now(UTC) - timedelta(days=1)  # отозвана в прошлом
    sub = _sub(
        is_bio_reward=True, status=SubscriptionStatus.DISABLED.value, end_date=old_end
    )
    participant = _participant(free_subscription_id=101, user=_user())
    await svc._extend_free_sub(FakeDb(get_map={101: sub}), participant, _bio_cfg())
    assert sub.status == SubscriptionStatus.DISABLED.value
    assert sub.end_date == old_end
    assert calls == []


# ---------- Fix 4: _revoke ----------


def _patch_no_active_paid(monkeypatch):
    import app.database.crud.subscription as sub_crud

    async def no_paid(db, user_id):
        return []

    monkeypatch.setattr(sub_crud, 'get_active_subscriptions_by_user_id', no_paid)


async def test_revoke_does_not_disable_converted_paid_sub(monkeypatch):
    from app.services.bio_reward_service import BioRewardService

    _patch_no_active_paid(monkeypatch)
    calls = _patch_subscription_service(monkeypatch)
    svc = BioRewardService()
    sub = _sub(is_bio_reward=False, status='active')
    participant = _participant(free_subscription_id=101)
    await svc._revoke(FakeDb(get_map={101: sub}), participant, _user(), _bio_cfg())
    assert sub.status == 'active'
    assert participant.free_subscription_id is None
    assert calls == []


async def test_revoke_disables_bio_sub_pushes_and_detaches(monkeypatch):
    from app.database.models import SubscriptionStatus
    from app.services.bio_reward_service import BioRewardService

    _patch_no_active_paid(monkeypatch)
    calls = _patch_subscription_service(monkeypatch)
    svc = BioRewardService()
    sub = _sub(is_bio_reward=True, status='active')
    participant = _participant(free_subscription_id=101)
    await svc._revoke(FakeDb(get_map={101: sub}), participant, _user(), _bio_cfg())
    assert sub.status == SubscriptionStatus.DISABLED.value
    assert calls == [sub]
    assert participant.free_subscription_id is None
