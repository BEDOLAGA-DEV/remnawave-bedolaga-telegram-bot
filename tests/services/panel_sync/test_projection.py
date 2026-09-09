"""Единственный маппер «панель → подписка».

Раньше это делали шесть независимых мапперов с разными наборами полей и разными
правилами. Здесь закреплены правила, которые они должны были соблюдать все.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.database.models import SubscriptionStatus
from app.services.panel_sync import PanelSnapshot, project_onto_subscription, read_panel_user


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def _sub(**kw):
    base = dict(
        id=1,
        status=SubscriptionStatus.ACTIVE.value,
        end_date=NOW + timedelta(days=30),
        traffic_used_gb=1.0,
        traffic_limit_gb=100,
        device_limit=3,
        connected_squads=['squad-a'],
        remnawave_short_uuid='abc',
        subscription_url='https://old',
        subscription_crypto_link='old-crypto',
        grace_candidate_reason=None,
        grace_candidate_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ==================== разбор ответа панели ====================


def test_reads_the_dictionary_shape_of_the_panel():
    snapshot = read_panel_user(
        {
            'status': 'ACTIVE',
            'expireAt': '2026-10-09T12:00:00.000Z',
            'usedTrafficBytes': 2 * 1024**3,
            'activeInternalSquads': [{'uuid': 'squad-b'}, 'squad-c'],
            'shortUuid': 'short-1',
            'subscriptionUrl': 'https://panel/sub',
            'happ': {'cryptoLink': 'crypto-1'},
        }
    )

    assert snapshot.status == 'ACTIVE'
    assert snapshot.expire_at == datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    assert snapshot.traffic_used_gb == 2.0
    assert snapshot.squads == ('squad-b', 'squad-c')
    assert snapshot.short_uuid == 'short-1'
    assert snapshot.subscription_url == 'https://panel/sub'
    assert snapshot.crypto_link == 'crypto-1'


def test_reads_the_parsed_object_shape_of_the_client():
    snapshot = read_panel_user(
        SimpleNamespace(
            status='DISABLED',
            expire_at=datetime(2026, 1, 1, tzinfo=UTC),
            used_traffic_bytes=1024**3,
            active_internal_squads=[{'uuid': 'squad-d'}],
            short_uuid='short-2',
            subscription_url='https://panel/two',
            happ_crypto_link='crypto-2',
        )
    )

    assert snapshot.status == 'DISABLED'
    assert snapshot.expire_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert snapshot.traffic_used_gb == 1.0
    assert snapshot.squads == ('squad-d',)
    assert snapshot.crypto_link == 'crypto-2'


def test_unparsable_date_does_not_explode():
    assert read_panel_user({'expireAt': 'позавчера'}).expire_at is None


# ==================== дата окончания ====================


def test_active_panel_moves_the_end_date_in_both_directions():
    later = _sub()
    project_onto_subscription(later, PanelSnapshot(status='ACTIVE', expire_at=NOW + timedelta(days=60)), now=NOW)
    earlier = _sub()
    project_onto_subscription(earlier, PanelSnapshot(status='ACTIVE', expire_at=NOW + timedelta(days=10)), now=NOW)

    assert later.end_date == NOW + timedelta(days=60)
    assert earlier.end_date == NOW + timedelta(days=10)


def test_disabled_panel_never_touches_the_end_date():
    """У отключённого в панели может лежать «сейчас плюс минута» от старых версий."""
    subscription = _sub()
    original = subscription.end_date

    project_onto_subscription(subscription, PanelSnapshot(status='DISABLED', expire_at=NOW), now=NOW)

    assert subscription.end_date == original


def test_a_few_seconds_of_difference_are_ignored():
    subscription = _sub()
    original = subscription.end_date

    project_onto_subscription(
        subscription,
        PanelSnapshot(status='ACTIVE', expire_at=original + timedelta(seconds=30)),
        now=NOW,
    )

    assert subscription.end_date == original


# ==================== статус ====================


def test_limited_in_the_panel_becomes_limited_in_the_bot():
    subscription = _sub()

    project_onto_subscription(subscription, PanelSnapshot(status='LIMITED'), now=NOW)

    assert subscription.status == SubscriptionStatus.LIMITED.value
    assert subscription.grace_candidate_reason == SubscriptionStatus.LIMITED.value
    assert subscription.grace_candidate_at == NOW


def test_expired_by_date_marks_a_grace_candidate():
    subscription = _sub(status=SubscriptionStatus.TRIAL.value, end_date=NOW - timedelta(days=1))

    project_onto_subscription(subscription, PanelSnapshot(status='DISABLED', expire_at=None), now=NOW)

    assert subscription.status == SubscriptionStatus.DISABLED.value


def test_sync_never_expires_a_subscription_that_is_active_in_the_bot():
    """Защита от гонки: продление могло случиться между чтением панели и записью."""
    subscription = _sub(status=SubscriptionStatus.ACTIVE.value, end_date=NOW - timedelta(minutes=1))

    project_onto_subscription(subscription, PanelSnapshot(status=None), now=NOW)

    assert subscription.status == SubscriptionStatus.ACTIVE.value


def test_expired_trial_becomes_expired():
    subscription = _sub(status=SubscriptionStatus.TRIAL.value, end_date=NOW - timedelta(days=1))

    project_onto_subscription(subscription, PanelSnapshot(status=None), now=NOW)

    assert subscription.status == SubscriptionStatus.EXPIRED.value


# ==================== трафик, сквады, ссылки ====================


def test_traffic_is_carried_over():
    subscription = _sub()

    project_onto_subscription(subscription, PanelSnapshot(status='ACTIVE', traffic_used_gb=7.5), now=NOW)

    assert subscription.traffic_used_gb == 7.5


def test_traffic_jitter_is_ignored():
    subscription = _sub(traffic_used_gb=1.0)

    project_onto_subscription(subscription, PanelSnapshot(status='ACTIVE', traffic_used_gb=1.005), now=NOW)

    assert subscription.traffic_used_gb == 1.0


def test_limits_are_never_read_from_the_panel():
    """Лимиты трафика и устройств задаёт тариф в боте, а не правка в панели."""
    subscription = _sub()

    project_onto_subscription(subscription, PanelSnapshot(status='ACTIVE'), now=NOW)

    assert subscription.traffic_limit_gb == 100
    assert subscription.device_limit == 3


def test_squads_come_from_the_panel():
    subscription = _sub()

    project_onto_subscription(subscription, PanelSnapshot(status='ACTIVE', squads=('squad-x',)), now=NOW)

    assert subscription.connected_squads == ['squad-x']


def test_empty_squad_list_means_the_panel_does_not_know():
    subscription = _sub()

    project_onto_subscription(subscription, PanelSnapshot(status='ACTIVE', squads=()), now=NOW)

    assert subscription.connected_squads == ['squad-a']


def test_links_are_refreshed():
    subscription = _sub()

    changed = project_onto_subscription(
        subscription,
        PanelSnapshot(
            status='ACTIVE',
            short_uuid='new-short',
            subscription_url='https://new',
            crypto_link='new-crypto',
        ),
        now=NOW,
    )

    assert subscription.remnawave_short_uuid == 'new-short'
    assert subscription.subscription_url == 'https://new'
    assert subscription.subscription_crypto_link == 'new-crypto'
    assert {'remnawave_short_uuid', 'subscription_url', 'subscription_crypto_link'} <= changed


# ==================== грейс ====================


def test_open_grace_freezes_the_billing_state_but_keeps_links():
    subscription = _sub()

    changed = project_onto_subscription(
        subscription,
        PanelSnapshot(status='DISABLED', expire_at=NOW, traffic_used_gb=99.0, short_uuid='new-short'),
        now=NOW,
        grace_open=True,
    )

    assert subscription.status == SubscriptionStatus.ACTIVE.value
    assert subscription.traffic_used_gb == 1.0
    assert subscription.remnawave_short_uuid == 'new-short'
    assert changed == {'remnawave_short_uuid'}
