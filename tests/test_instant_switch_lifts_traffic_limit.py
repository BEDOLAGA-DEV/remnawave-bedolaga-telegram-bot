"""Regression: мгновенная смена тарифа оставляла подписку в ``limited``.

Трафик кончился → открылся грейс (Telegram-only оверлей) → человек мгновенно
переключился на тариф с бо́льшим лимитом. Лимит поднимался, а статус оставался
``limited``; ``update_remnawave_user`` при открытом грейсе и таком статусе
обновляет только карточку аккаунта, и оверлей держался до конца грейса.

Фикс: все три флоу мгновенной смены (бот, кабинет, Mini App) после выставления
нового лимита вызывают ``lift_traffic_limit_after_switch``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.tariff_switch_policy import lift_traffic_limit_after_switch


NOW = datetime(2026, 11, 8, 17, 0, tzinfo=UTC)


def _sub(**overrides):
    values = {
        'status': 'limited',
        'traffic_accounting_mode': 'legacy',
        'end_date': NOW + timedelta(days=4),
        'traffic_limit_gb': 300,
        'traffic_used_gb': 150.03,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_limited_with_room_on_new_tariff_becomes_active():
    sub = _sub()
    assert lift_traffic_limit_after_switch(sub, now=NOW) is True
    assert sub.status == 'active'


def test_unlimited_new_tariff_becomes_active():
    sub = _sub(traffic_limit_gb=0, traffic_used_gb=900.0)
    assert lift_traffic_limit_after_switch(sub, now=NOW) is True
    assert sub.status == 'active'


def test_new_tariff_still_exhausted_stays_limited():
    sub = _sub(traffic_limit_gb=150, traffic_used_gb=150.03)
    assert lift_traffic_limit_after_switch(sub, now=NOW) is False
    assert sub.status == 'limited'


def test_disabled_is_never_lifted():
    sub = _sub(status='disabled')
    assert lift_traffic_limit_after_switch(sub, now=NOW) is False
    assert sub.status == 'disabled'


def test_ended_subscription_is_not_lifted():
    sub = _sub(end_date=NOW - timedelta(minutes=1))
    assert lift_traffic_limit_after_switch(sub, now=NOW) is False
    assert sub.status == 'limited'


def test_naive_end_date_is_treated_as_utc():
    sub = _sub(end_date=(NOW + timedelta(days=1)).replace(tzinfo=None))
    assert lift_traffic_limit_after_switch(sub, now=NOW) is True


def test_managed_accounting_owns_quota_status():
    for mode in ('managed', 'migrating'):
        sub = _sub(traffic_accounting_mode=mode)
        assert lift_traffic_limit_after_switch(sub, now=NOW) is False
        assert sub.status == 'limited'
