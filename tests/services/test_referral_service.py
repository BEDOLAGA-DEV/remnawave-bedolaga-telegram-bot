import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services import referral_service


async def test_commission_accrues_before_minimum_first_topup(monkeypatch):
    user = SimpleNamespace(
        id=1,
        telegram_id=101,
        full_name='Test User',
        referred_by_id=2,
        has_made_first_topup=False,
    )
    referrer = SimpleNamespace(
        id=2,
        telegram_id=202,
        full_name='Referrer',
    )

    db = SimpleNamespace(
        commit=AsyncMock(),
        execute=AsyncMock(),
    )

    get_user_mock = AsyncMock(side_effect=[user, referrer])
    monkeypatch.setattr(referral_service, 'get_user_by_id', get_user_mock)
    add_user_balance_mock = AsyncMock()
    monkeypatch.setattr(referral_service, 'add_user_balance', add_user_balance_mock)
    create_referral_earning_mock = AsyncMock()
    monkeypatch.setattr(referral_service, 'create_referral_earning', create_referral_earning_mock)
    monkeypatch.setattr(referral_service, 'get_user_campaign_id', AsyncMock(return_value=None))

    monkeypatch.setattr(referral_service.settings, 'REFERRAL_MINIMUM_TOPUP_KOPEKS', 20000)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 5000)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_INVITER_BONUS_KOPEKS', 10000)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_COMMISSION_PERCENT', 25)

    topup_amount = 15000

    result = await referral_service.process_referral_topup(db, user.id, topup_amount)

    assert result is True
    assert user.has_made_first_topup is False

    add_user_balance_mock.assert_awaited_once()
    add_call = add_user_balance_mock.await_args
    assert add_call.args[1] is referrer
    assert add_call.args[2] == 3750
    assert 'Комиссия' in add_call.args[3]
    assert add_call.kwargs.get('bot') is None

    create_referral_earning_mock.assert_awaited_once()
    earning_call = create_referral_earning_mock.await_args
    assert earning_call.kwargs['amount_kopeks'] == 3750
    assert earning_call.kwargs['reason'] == 'referral_commission_topup'


async def test_first_topup_inviter_gets_fixed_plus_commission(monkeypatch):
    """Inviter bonus should be fixed bonus + commission, not max(fixed, commission)."""
    user = SimpleNamespace(
        id=1,
        telegram_id=101,
        full_name='Test User',
        referred_by_id=2,
        has_made_first_topup=False,
    )
    referrer = SimpleNamespace(
        id=2,
        telegram_id=202,
        full_name='Referrer',
        email=None,
    )

    db = SimpleNamespace(
        commit=AsyncMock(),
        execute=AsyncMock(),
    )

    get_user_mock = AsyncMock(side_effect=[user, referrer])
    monkeypatch.setattr(referral_service, 'get_user_by_id', get_user_mock)
    add_user_balance_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(referral_service, 'add_user_balance', add_user_balance_mock)
    create_referral_earning_mock = AsyncMock()
    monkeypatch.setattr(referral_service, 'create_referral_earning', create_referral_earning_mock)
    monkeypatch.setattr(referral_service, 'get_commission_payment_count', AsyncMock(return_value=0))
    monkeypatch.setattr(referral_service, 'get_user_campaign_id', AsyncMock(return_value=None))
    monkeypatch.setattr(referral_service, 'get_effective_referral_commission_percent', lambda u: 15)

    monkeypatch.setattr(referral_service.settings, 'REFERRAL_MINIMUM_TOPUP_KOPEKS', 10000)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 5000)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_INVITER_BONUS_KOPEKS', 5000)  # 50 rub
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_COMMISSION_PERCENT', 15)

    topup_amount = 50000  # 500 rub

    result = await referral_service.process_referral_topup(db, user.id, topup_amount)

    assert result is True
    assert user.has_made_first_topup is True

    # add_user_balance called twice: first for referral's own bonus, then for inviter bonus
    assert add_user_balance_mock.await_count == 2

    # Second call is the inviter bonus: fixed 5000 + commission 15% of 50000 = 7500 → total 12500
    inviter_call = add_user_balance_mock.await_args_list[1]
    expected_commission = int(50000 * 15 / 100)  # 7500
    expected_inviter_bonus = 5000 + expected_commission  # 12500
    assert inviter_call.args[2] == expected_inviter_bonus

    # With old max() logic, this would have been max(5000, 7500) = 7500 — wrong!
    assert expected_inviter_bonus == 12500
