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
