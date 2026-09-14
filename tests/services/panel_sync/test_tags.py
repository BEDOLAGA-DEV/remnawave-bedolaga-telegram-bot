"""Тег панельного пользователя: у тарифа свой, иначе общие теги из настроек.

Владелец: «если есть тег на тарифе — победа, если нет — ставим те, что сейчас есть».
Тег описывает тариф, поэтому побеждает и над триальным. Правила формата — те же,
что у панели (и у глобальных тегов): до 16 символов, A–Z, 0–9, подчёркивание.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.database.models import SubscriptionStatus
from app.services.panel_sync import build_panel_payload, tags as tags_module
from app.services.panel_sync.tags import normalize_panel_tag, resolve_panel_user_tag


NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _global_tags(monkeypatch):
    monkeypatch.setattr(
        tags_module,
        'settings',
        SimpleNamespace(get_trial_user_tag=lambda: 'TRIAL', get_paid_subscription_user_tag=lambda: 'PAID'),
    )


def _tariff(**kw):
    base = dict(
        panel_tag=None, external_squad_uuid=None, traffic_reset_mode=None, device_limit=3, max_device_limit=None
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _sub(**kw):
    base = dict(
        id=101,
        status=SubscriptionStatus.ACTIVE.value,
        end_date=NOW + timedelta(days=30),
        traffic_limit_gb=50,
        connected_squads=['squad-a'],
        tariff=None,
        is_trial=False,
        remnawave_id=None,
        remnawave_short_id='ab12cd',
        device_limit=3,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _user():
    return SimpleNamespace(
        id=10, telegram_id=555, username='tg', full_name='Иван', email=None, status='active', remnawave_id=None
    )


# ---- normalize_panel_tag ----


@pytest.mark.parametrize(
    ('raw', 'expected'), [('vip', 'VIP'), ('  gold_1 ', 'GOLD_1'), ('', None), ('   ', None), (None, None)]
)
def test_normalize_upper_cases_and_treats_blank_as_absent(raw, expected):
    assert normalize_panel_tag(raw) == expected


@pytest.mark.parametrize('raw', ['v-i-p', 'a' * 17, 'тариф', 'my tag', 'x.y'])
def test_normalize_rejects_what_the_panel_rejects(raw):
    with pytest.raises(ValueError):
        normalize_panel_tag(raw)


# ---- resolve_panel_user_tag ----


def test_tariff_tag_wins_for_paid_subscription():
    assert resolve_panel_user_tag(_sub(tariff=_tariff(panel_tag='VIP'))) == 'VIP'


def test_tariff_tag_wins_over_trial_tag_too():
    assert resolve_panel_user_tag(_sub(tariff=_tariff(panel_tag='VIP'), is_trial=True)) == 'VIP'


def test_without_tariff_tag_trial_uses_global_trial_tag():
    assert resolve_panel_user_tag(_sub(tariff=_tariff(), is_trial=True)) == 'TRIAL'


def test_without_tariff_tag_paid_uses_global_paid_tag():
    assert resolve_panel_user_tag(_sub(tariff=_tariff())) == 'PAID'
    assert resolve_panel_user_tag(_sub(tariff=None)) == 'PAID'


def test_blank_tariff_tag_counts_as_absent():
    assert resolve_panel_user_tag(_sub(tariff=_tariff(panel_tag='  '))) == 'PAID'


# ---- build_panel_payload берёт тег сам, если вызывающий его не передал ----


def test_payload_resolves_tag_from_tariff_when_caller_passed_none():
    payload = build_panel_payload(_user(), _sub(tariff=_tariff(panel_tag='VIP')), multi_tariff=False, now=NOW)

    assert payload.tag == 'VIP'
    assert payload.create_kwargs()['tag'] == 'VIP'


def test_payload_keeps_explicit_tag_from_caller():
    payload = build_panel_payload(
        _user(), _sub(tariff=_tariff(panel_tag='VIP')), multi_tariff=False, user_tag='PAID', now=NOW
    )

    assert payload.tag == 'PAID'
