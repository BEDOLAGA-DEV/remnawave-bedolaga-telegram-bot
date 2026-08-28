"""Характеризующие тесты текущего расчёта реферальной награды.

На реферальную систему в репозитории не было ни одного теста, при том что это
1055 строк денежной логики. Эти тесты фиксируют поведение ДО перехода на
многоуровневую схему: они описывают то, что есть, чтобы рефакторинг был виден как
осознанное изменение, а не как тихая финансовая регрессия.

Разбор веток process_referral_topup:
  A — реферал ещё не платил, сумма НИЖЕ порога: только комиссия рефереру;
  B — первое пополнение ОТ порога: рефералу фикс-бонус, рефереру фикс + комиссия;
  C — все последующие пополнения: только комиссия.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.database.models import TransactionType
from app.services import referral_service


def _user(uid: int, *, referred_by: int | None = None, first_topup: bool = False):
    return SimpleNamespace(
        id=uid,
        telegram_id=1000 + uid,
        full_name=f'User {uid}',
        language='ru',
        referred_by_id=referred_by,
        has_made_first_topup=first_topup,
        referral_commission_percent=None,
        balance_kopeks=0,
    )


@pytest.fixture
def wired(monkeypatch):
    """Общая обвязка: реферал → реферер, без похода в БД и Telegram."""
    referral = _user(2, referred_by=1)
    referrer = _user(1)
    users = {1: referrer, 2: referral}

    earnings: list[dict] = []
    balance_credits: list[dict] = []

    async def fake_get_user_by_id(_db, uid):
        return users.get(uid)

    async def fake_add_user_balance(_db, user, amount, description, **kwargs):
        balance_credits.append({'user_id': user.id, 'amount': amount, 'type': kwargs.get('transaction_type')})
        return True

    async def fake_create_referral_earning(**kwargs):
        earnings.append(kwargs)
        return SimpleNamespace(id=len(earnings))

    monkeypatch.setattr(referral_service, 'get_user_by_id', fake_get_user_by_id)
    monkeypatch.setattr(referral_service, 'add_user_balance', fake_add_user_balance)
    monkeypatch.setattr(referral_service, 'create_referral_earning', fake_create_referral_earning)
    monkeypatch.setattr(referral_service, 'get_user_campaign_id', AsyncMock(return_value=None))
    monkeypatch.setattr(referral_service, 'get_referral_reward_payment_count', AsyncMock(return_value=0))
    monkeypatch.setattr(referral_service, '_is_commission_limit_reached', AsyncMock(return_value=False))
    monkeypatch.setattr(referral_service, 'send_referral_notification', AsyncMock(return_value=True))
    monkeypatch.setattr(
        referral_service.notification_delivery_service, 'send_notification', AsyncMock(return_value=True), raising=False
    )

    monkeypatch.setattr(settings, 'REFERRAL_MINIMUM_TOPUP_KOPEKS', 10000, raising=False)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 5000, raising=False)
    monkeypatch.setattr(settings, 'REFERRAL_INVITER_BONUS_KOPEKS', 7000, raising=False)
    monkeypatch.setattr(settings, 'REFERRAL_COMMISSION_PERCENT', 25, raising=False)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_PAYMENT_COMMISSION_PERCENT', None, raising=False)
    monkeypatch.setattr(settings, 'REFERRAL_RECURRING_COMMISSION_TIERS', '', raising=False)
    monkeypatch.setattr(settings, 'REFERRAL_MAX_COMMISSION_PAYMENTS', 0, raising=False)

    return SimpleNamespace(referral=referral, referrer=referrer, earnings=earnings, balance_credits=balance_credits)


class TestTopupBranches:
    async def test_below_threshold_pays_commission_only(self, wired):
        """Ветка A: порог не взят — фикс-бонусов нет, комиссия всё равно идёт."""
        await referral_service.process_referral_topup(AsyncMock(), 2, 4000)

        assert [c['amount'] for c in wired.balance_credits] == [1000]  # 25% от 4000
        assert wired.balance_credits[0]['user_id'] == wired.referrer.id
        assert [e['reason'] for e in wired.earnings] == ['referral_commission_topup']
        # Порог не взят — флаг первого пополнения не выставляется.
        assert wired.referral.has_made_first_topup is False

    async def test_first_topup_pays_referee_fixed_and_referrer_fixed_plus_commission(self, wired):
        """Ветка B: рефереру именно СУММА фикса и комиссии, а не максимум из них."""
        await referral_service.process_referral_topup(AsyncMock(), 2, 20000)

        by_user = {c['user_id']: c['amount'] for c in wired.balance_credits}
        assert by_user[wired.referral.id] == 5000  # REFERRAL_FIRST_TOPUP_BONUS_KOPEKS
        assert by_user[wired.referrer.id] == 7000 + 5000  # INVITER_BONUS + 25% от 20000
        assert wired.referral.has_made_first_topup is True

        # Награда реферала фиксируется в балансе, но НЕ попадает в ledger рефералки.
        assert [e['reason'] for e in wired.earnings] == ['referral_first_topup']
        assert wired.earnings[0]['user_id'] == wired.referrer.id

    async def test_subsequent_topup_pays_commission_only(self, wired):
        """Ветка C: фикс-бонусы больше не повторяются."""
        wired.referral.has_made_first_topup = True

        await referral_service.process_referral_topup(AsyncMock(), 2, 20000)

        assert [c['amount'] for c in wired.balance_credits] == [5000]
        assert [e['reason'] for e in wired.earnings] == ['referral_commission_topup']

    async def test_reward_is_credited_as_referral_reward_transaction(self, wired):
        await referral_service.process_referral_topup(AsyncMock(), 2, 20000)

        assert all(c['type'] == TransactionType.REFERRAL_REWARD for c in wired.balance_credits)

    async def test_user_without_referrer_is_skipped(self, wired):
        wired.referral.referred_by_id = None

        assert await referral_service.process_referral_topup(AsyncMock(), 2, 20000) is True
        assert wired.balance_credits == []


class TestCommissionPercent:
    """Ставка: индивидуальная у партнёра, отдельная на первый платёж, ступени."""

    async def test_personal_partner_percent_wins_over_global(self, monkeypatch):
        monkeypatch.setattr(settings, 'REFERRAL_COMMISSION_PERCENT', 25, raising=False)
        monkeypatch.setattr(settings, 'REFERRAL_FIRST_PAYMENT_COMMISSION_PERCENT', None, raising=False)
        monkeypatch.setattr(settings, 'REFERRAL_RECURRING_COMMISSION_TIERS', '', raising=False)
        referrer = _user(1)
        referrer.referral_commission_percent = 40

        percent = await referral_service.calculate_referral_commission_percent(
            AsyncMock(), referrer, is_first_payment=False
        )

        assert percent == 40

    async def test_first_payment_percent_overrides_when_configured(self, monkeypatch):
        monkeypatch.setattr(settings, 'REFERRAL_COMMISSION_PERCENT', 25, raising=False)
        monkeypatch.setattr(settings, 'REFERRAL_FIRST_PAYMENT_COMMISSION_PERCENT', 50, raising=False)

        percent = await referral_service.calculate_referral_commission_percent(
            AsyncMock(), _user(1), is_first_payment=True
        )

        assert percent == 50

    @pytest.mark.parametrize(
        ('paid_referrals', 'expected'),
        [(0, 10), (9, 10), (10, 15), (60, 20), (200, 25)],
    )
    async def test_recurring_tiers_pick_by_paid_referrals_count(self, monkeypatch, paid_referrals, expected):
        """Ступени REFERRAL_RECURRING_COMMISSION_TIERS — это НЕ уровни сети.

        Порог здесь — количество оплативших прямых рефералов, а не глубина цепочки.
        """
        monkeypatch.setattr(settings, 'REFERRAL_RECURRING_COMMISSION_TIERS', '0:10,10:15,50:20,100:25', raising=False)
        monkeypatch.setattr(settings, 'REFERRAL_FIRST_PAYMENT_COMMISSION_PERCENT', None, raising=False)
        monkeypatch.setattr(referral_service, 'get_paid_referrals_count', AsyncMock(return_value=paid_referrals))

        percent = await referral_service.calculate_referral_commission_percent(
            AsyncMock(), _user(1), is_first_payment=False
        )

        assert percent == expected


class TestNoMultiLevelYet:
    """Фиксируем отсутствие многоуровневости: награду получает только прямой пригласивший."""

    async def test_grandparent_gets_nothing(self, wired, monkeypatch):
        grandparent = _user(0)
        wired.referrer.referred_by_id = grandparent.id

        async def fake_get_user_by_id(_db, uid):
            return {0: grandparent, 1: wired.referrer, 2: wired.referral}.get(uid)

        monkeypatch.setattr(referral_service, 'get_user_by_id', fake_get_user_by_id)

        await referral_service.process_referral_topup(AsyncMock(), 2, 20000)

        assert grandparent.id not in {c['user_id'] for c in wired.balance_credits}
