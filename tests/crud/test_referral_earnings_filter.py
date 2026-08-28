"""Поведение предиката ``not_referee_directed()`` на настоящих строках.

Предыдущая проверка считала вхождения строки ``not_referee_directed()`` в
исходнике сводки. Она подтверждала, что предикат ВЫЗЫВАЮТ, и ничего не говорила
о том, что он делает: подмена его тела на всегда-истинное условие оставляла весь
набор тестов зелёным. Ровно это и произошло — мутация дожила до коммита.

Здесь предикат исполняется в SQLite на реальных строках ledger'а, поэтому
сломанное тело роняет тест немедленно.
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.database.crud.referral import not_referee_directed
from app.database.models import ReferralEarning
from tests.fixtures.sqlite_memory import memory_session


REFERRER_ID = 1
REFEREE_ID = 2


def _earning(**kwargs) -> ReferralEarning:
    base = {
        'user_id': REFERRER_ID,
        'referral_id': REFEREE_ID,
        'amount_kopeks': 0,
        'reason': 'referral_commission_topup',
        'reward_type': 'money',
        'level': 1,
        'days_granted': 0,
    }
    base.update(kwargs)
    return ReferralEarning(**base)


@pytest.mark.asyncio
async def test_referee_directed_days_are_excluded(monkeypatch):
    """Строка награды приглашённому не должна попадать в заработок владельца user_id."""
    async with memory_session(monkeypatch, [ReferralEarning.__table__]) as db:
        db.add_all(
            [
                # Заработок пригласившего: деньги и дни.
                _earning(amount_kopeks=10_000, reason='referral_commission_topup'),
                _earning(days_granted=5, reward_type='days', reason='referral_days_reward'),
                # Награда ПРИГЛАШЁННОМУ: принадлежит ему, пара зеркалирована.
                ReferralEarning(
                    user_id=REFEREE_ID,
                    referral_id=REFERRER_ID,
                    amount_kopeks=0,
                    reason='referral_days_bonus',
                    reward_type='days',
                    level=1,
                    days_granted=7,
                ),
            ]
        )
        await db.commit()

        # Заработок пригласившего: обе его строки, чужая не считается.
        result = await db.execute(
            select(
                func.coalesce(func.sum(ReferralEarning.amount_kopeks), 0),
                func.coalesce(func.sum(ReferralEarning.days_granted), 0),
            ).where(ReferralEarning.user_id == REFERRER_ID, not_referee_directed())
        )
        money, days = result.one()
        assert (money, days) == (10_000, 5)

        # У приглашённого, не пригласившего никого, заработка нет.
        result = await db.execute(
            select(func.coalesce(func.sum(ReferralEarning.days_granted), 0)).where(
                ReferralEarning.user_id == REFEREE_ID, not_referee_directed()
            )
        )
        assert result.scalar() == 0, 'бонус приглашённому — не его реферальный заработок'

        # Без предиката он бы засчитался: это и есть цена ошибки.
        result = await db.execute(
            select(func.coalesce(func.sum(ReferralEarning.days_granted), 0)).where(
                ReferralEarning.user_id == REFEREE_ID
            )
        )
        assert result.scalar() == 7


@pytest.mark.asyncio
async def test_predicate_keeps_every_referrer_reason(monkeypatch):
    """Предикат обязан отбрасывать ТОЛЬКО награды приглашённому.

    Слишком широкое условие тихо обнулило бы часть заработка пригласившего.
    """
    async with memory_session(monkeypatch, [ReferralEarning.__table__]) as db:
        reasons = [
            'referral_first_topup',
            'referral_commission_topup',
            'referral_registration_reward',
            'referral_days_reward',
            'referral_registration_pending',
        ]
        db.add_all([_earning(reason=reason, amount_kopeks=100) for reason in reasons])
        await db.commit()

        result = await db.execute(
            select(func.count(ReferralEarning.id)).where(ReferralEarning.user_id == REFERRER_ID, not_referee_directed())
        )
        assert result.scalar() == len(reasons)


@pytest.mark.asyncio
async def test_distinct_referral_id_does_not_count_own_inviter(monkeypatch):
    """«Сколько у меня рефералов» через DISTINCT referral_id.

    Зеркалированная строка ставит в ``referral_id`` пригласившего: без предиката
    пользователь получает в свои рефералы того, кто пригласил его самого.
    """
    async with memory_session(monkeypatch, [ReferralEarning.__table__]) as db:
        db.add(
            ReferralEarning(
                user_id=REFEREE_ID,
                referral_id=REFERRER_ID,
                amount_kopeks=0,
                reason='referral_days_bonus',
                reward_type='days',
                level=1,
                days_granted=7,
            )
        )
        await db.commit()

        result = await db.execute(
            select(func.count(func.distinct(ReferralEarning.referral_id))).where(
                ReferralEarning.user_id == REFEREE_ID, not_referee_directed()
            )
        )
        assert result.scalar() == 0

        result = await db.execute(
            select(func.count(func.distinct(ReferralEarning.referral_id))).where(ReferralEarning.user_id == REFEREE_ID)
        )
        assert result.scalar() == 1, 'без предиката пригласивший попадает в собственные рефералы'


class TestLevelPaymentCap:
    """Запрос лимита исполняется на реальных строках.

    Все проверки ограничения подменяли ``count_level_payments`` целиком, поэтому
    сам запрос ни разу не выполнялся: перепутанный фильтр остался бы незамеченным.
    """

    @staticmethod
    def _row(**kwargs):
        base = {
            'user_id': REFERRER_ID,
            'referral_id': REFEREE_ID,
            'amount_kopeks': 10_000,
            'reason': 'referral_commission_topup',
            'reward_type': 'money',
            'level': 1,
            'days_granted': 0,
        }
        base.update(kwargs)
        return ReferralEarning(**base)

    @pytest.mark.asyncio
    async def test_counts_only_this_pair_this_level_and_only_money(self, monkeypatch):
        from app.services.referral_reward_service import count_level_payments

        async with memory_session(monkeypatch, [ReferralEarning.__table__]) as db:
            db.add_all(
                [
                    self._row(),  # считается
                    self._row(),  # считается
                    self._row(level=2),  # чужой уровень
                    self._row(referral_id=99),  # другая пара
                    self._row(user_id=99),  # другой реферер
                    self._row(amount_kopeks=0, days_granted=5, reward_type='days'),  # дни
                    self._row(amount_kopeks=0),  # нулевая сумма
                ]
            )
            await db.commit()

            assert await count_level_payments(db, REFERRER_ID, REFEREE_ID, 1) == 2
            assert await count_level_payments(db, REFERRER_ID, REFEREE_ID, 2) == 1

    @pytest.mark.asyncio
    async def test_rows_older_than_the_level_do_not_consume_the_cap(self, monkeypatch):
        """Начисления классической схемы бэкфиллены в level=1 и по причине неотличимы.

        Без границы по дате установка, год проработавшая на классической схеме,
        при переключении получала бы лимит, исчерпанный до того, как админ его
        задал: «не больше 5 выплат на реферала» и ни одной выплаты.
        """
        from datetime import UTC, datetime, timedelta

        from app.services.referral_reward_service import count_level_payments

        async with memory_session(monkeypatch, [ReferralEarning.__table__]) as db:
            level_created = datetime.now(UTC)
            old = self._row()
            old.created_at = level_created - timedelta(days=30)
            fresh = self._row()
            fresh.created_at = level_created + timedelta(minutes=1)
            db.add_all([old, fresh])
            await db.commit()

            assert await count_level_payments(db, REFERRER_ID, REFEREE_ID, 1) == 2
            assert await count_level_payments(db, REFERRER_ID, REFEREE_ID, 1, since=level_created) == 1

    @pytest.mark.asyncio
    async def test_reward_type_filter_stands_on_its_own(self, monkeypatch):
        """Фильтр по типу не должен держаться на том, что у дней сумма нулевая.

        Сегодня ``amount_kopeks > 0`` отсекает дневные строки и сам по себе, но
        это совпадение двух инвариантов, а не одно правило. Строка с ненулевой
        суммой и типом ``days`` в проде не появляется — здесь она заведена
        нарочно, чтобы условие по типу проверялось независимо и не выглядело
        лишним при следующем рефакторинге.
        """
        from app.services.referral_reward_service import count_level_payments

        async with memory_session(monkeypatch, [ReferralEarning.__table__]) as db:
            db.add_all([self._row(), self._row(reward_type='days', days_granted=5)])
            await db.commit()

            assert await count_level_payments(db, REFERRER_ID, REFEREE_ID, 1) == 1


class TestLevelUnlockThreshold:
    """Порог открытия уровня — на настоящих пользователях в базе.

    Уровень отвечает на вопрос «чьё пополнение приносит награду», порог — «с
    какого момента партнёр начинает получать доход с этого звена». Считать надо
    именно рефералов с пополнением: порог по всем регистрациям берётся накруткой
    пустых аккаунтов, и уровень открывается, не принеся ничего.
    """

    @staticmethod
    def _user(uid: int, *, referred_by: int | None = None, paid: bool = False):
        from app.database.models import User

        return User(
            id=uid,
            telegram_id=1000 + uid,
            first_name=f'User {uid}',
            language='ru',
            status='active',
            balance_kopeks=0,
            referred_by_id=referred_by,
            has_made_first_topup=paid,
        )

    @pytest.mark.asyncio
    async def test_counts_split_active_and_inactive(self, monkeypatch):
        from app.database.models import User
        from app.services.referral_reward_service import count_referrals

        async with memory_session(monkeypatch, [User.__table__]) as db:
            db.add(self._user(1))
            db.add_all(
                [
                    self._user(2, referred_by=1, paid=True),
                    self._user(3, referred_by=1, paid=True),
                    self._user(4, referred_by=1, paid=False),
                    self._user(5, referred_by=1, paid=False),
                    self._user(6, referred_by=99, paid=True),  # чужой реферал
                ]
            )
            await db.commit()

            assert await count_referrals(db, 1, active_only=False) == 4
            assert await count_referrals(db, 1, active_only=True) == 2

    @pytest.mark.asyncio
    async def test_level_opens_only_at_the_threshold(self, monkeypatch):
        from app.database.models import User
        from app.services.referral_reward_service import LevelConfig, is_level_unlocked

        def _config(required: int, active_only: bool = True) -> LevelConfig:
            return LevelConfig(
                level=2,
                is_active=True,
                reward_mode='money',
                trigger='every_topup',
                referrer_percent=5,
                referrer_fixed_kopeks=None,
                referrer_days=0,
                referrer_tariff_id=None,
                referee_fixed_kopeks=None,
                referee_days=0,
                referee_tariff_id=None,
                max_payments=0,
                required_referrals=required,
                required_referrals_active_only=active_only,
            )

        async with memory_session(monkeypatch, [User.__table__]) as db:
            db.add(self._user(1))
            db.add_all([self._user(uid, referred_by=1, paid=uid <= 3) for uid in range(2, 6)])
            await db.commit()
            # У партнёра 4 реферала, из них 2 с пополнением.

            assert await is_level_unlocked(db, _config(0), 1) is True, 'порог 0 — открыт сразу'
            assert await is_level_unlocked(db, _config(2), 1) is True
            assert await is_level_unlocked(db, _config(3), 1) is False, 'третьего с пополнением ещё нет'
            # По всем регистрациям тот же порог уже взят — в этом и разница.
            assert await is_level_unlocked(db, _config(3, active_only=False), 1) is True

    @pytest.mark.asyncio
    async def test_zero_threshold_costs_no_query(self, monkeypatch):
        """Обычная конфигурация не должна платить лишним запросом на каждом пополнении."""
        from app.services.referral_reward_service import LevelConfig, is_level_unlocked

        async def explode(*_args, **_kwargs):
            raise AssertionError('при пороге 0 запрос выполняться не должен')

        config = LevelConfig(
            level=1,
            is_active=True,
            reward_mode='money',
            trigger='every_topup',
            referrer_percent=10,
            referrer_fixed_kopeks=None,
            referrer_days=0,
            referrer_tariff_id=None,
            referee_fixed_kopeks=None,
            referee_days=0,
            referee_tariff_id=None,
            max_payments=0,
            required_referrals=0,
        )
        assert await is_level_unlocked(SimpleNamespace(execute=explode), config, 1) is True
