"""Многоуровневый расчёт наград: деньги, дни, глубина, лимиты.

Расчёт (``build_reward_components``) намеренно отделён от выдачи, поэтому здесь
он проверяется без базы, платежей и Remnawave. Выдачу (``award_referral_rewards``)
проверяет отдельный блок ниже — на подменённых начислениях.

Главный инвариант, который здесь защищается: пока ``REFERRAL_REWARD_SCHEME`` в
``legacy``, движок не выдаёт ничего вовсе. Иначе обновление бота изменило бы
денежные начисления на живых установках без ведома админа.
"""

from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import referral_reward_service as engine
from app.services.referral_reward_service import (
    LevelConfig,
    ReferralRewardLevelService,
    RewardEvent,
    build_reward_components,
    resolve_referrer_chain,
)


def _user(uid: int, *, referred_by: int | None = None, percent: int | None = None):
    return SimpleNamespace(
        id=uid,
        telegram_id=1000 + uid,
        full_name=f'User {uid}',
        language='ru',
        referred_by_id=referred_by,
        referral_commission_percent=percent,
        balance_kopeks=0,
        has_made_first_topup=False,
    )


def _level(level: int, **kwargs) -> LevelConfig:
    base = {
        'is_active': True,
        'reward_mode': 'money',
        'trigger': 'every_topup',
        'referrer_percent': None,
        'referrer_fixed_kopeks': None,
        'referrer_days': 0,
        'referrer_tariff_id': None,
        'referee_fixed_kopeks': None,
        'referee_days': 0,
        'referee_tariff_id': None,
        'max_payments': 0,
        'required_referrals': 0,
        'required_referrals_active_only': True,
    }
    base.update(kwargs)
    return LevelConfig(level=level, **base)


@pytest.fixture
def chain(monkeypatch):
    """Цепочка 4 → 3 → 2 → 1 и схема 'levels'."""
    users = {
        1: _user(1),
        2: _user(2, referred_by=1),
        3: _user(3, referred_by=2),
        4: _user(4, referred_by=3),
    }

    async def fake_get_user_by_id(_db, uid):
        return users.get(uid)

    monkeypatch.setattr(engine, 'get_user_by_id', fake_get_user_by_id)
    # Поля pydantic-настроек патчатся на ЭКЗЕМПЛЯРЕ: на классе это дескриптор
    # модели, и setattr туда до значения не доходит.
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
    monkeypatch.setattr(settings, 'REFERRAL_MAX_LEVEL_DEPTH', 3)
    return users


def _install_levels(monkeypatch, configs: dict[int, LevelConfig]):
    async def fake_get_level(_db, level):
        return configs.get(level)

    monkeypatch.setattr(
        ReferralRewardLevelService, 'get_level', classmethod(lambda cls, db, level: fake_get_level(db, level))
    )


async def _no_prior_payments(_db, _referrer_id, _referral_id, _level, since=None):
    return 0


class TestChainWalk:
    @pytest.mark.asyncio
    async def test_walks_up_to_configured_depth(self, chain):
        result = await resolve_referrer_chain(None, chain[4], 3)
        assert [(lvl, u.id) for lvl, u in result] == [(1, 3), (2, 2), (3, 1)]

    @pytest.mark.asyncio
    async def test_depth_limit_truncates(self, chain):
        result = await resolve_referrer_chain(None, chain[4], 1)
        assert [(lvl, u.id) for lvl, u in result] == [(1, 3)]

    @pytest.mark.asyncio
    async def test_cycle_does_not_hang(self, chain, monkeypatch):
        """A→B→A: без защиты обход крутился бы до предела глубины по кругу."""
        chain[1].referred_by_id = 4
        result = await resolve_referrer_chain(None, chain[4], 10)
        # Дошли до 1 и остановились: следующим был бы сам инициатор.
        assert [u.id for _, u in result] == [3, 2, 1]

    @pytest.mark.asyncio
    async def test_self_referral_yields_nothing(self, chain):
        chain[4].referred_by_id = 4
        assert await resolve_referrer_chain(None, chain[4], 3) == []


class TestSchemeGate:
    @pytest.mark.asyncio
    async def test_legacy_scheme_awards_nothing(self, chain, monkeypatch):
        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'legacy')
        _install_levels(monkeypatch, {1: _level(1, referrer_percent=50)})
        components = await build_reward_components(
            None, chain[4], event=RewardEvent.FIRST_TOPUP, topup_amount_kopeks=100_00
        )
        assert components == []


class TestMoneyPerLevel:
    @pytest.mark.asyncio
    async def test_percent_applies_at_each_configured_level(self, chain, monkeypatch):
        _install_levels(
            monkeypatch,
            {
                1: _level(1, referrer_percent=10),
                2: _level(2, referrer_percent=5),
                3: _level(3, referrer_percent=2),
            },
        )
        monkeypatch.setattr(engine, 'count_level_payments', _no_prior_payments)
        components = await build_reward_components(
            None, chain[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        assert [(c.recipient_id, c.level, c.money_kopeks) for c in components] == [
            (3, 1, 100_00),
            (2, 2, 50_00),
            (1, 3, 20_00),
        ]

    @pytest.mark.asyncio
    async def test_unconfigured_deep_level_pays_nothing(self, chain, monkeypatch):
        """Уровень без строки в БД молчит — глобальный процент туда не протекает."""
        _install_levels(monkeypatch, {1: _level(1, referrer_percent=10)})
        monkeypatch.setattr(engine, 'count_level_payments', _no_prior_payments)
        components = await build_reward_components(
            None, chain[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        assert [c.level for c in components] == [1]

    @pytest.mark.asyncio
    async def test_level_row_without_percent_pays_nothing_deep(self, chain, monkeypatch):
        """Строка уровня 2 есть, но процент не задан — платить нечем, а не 25% по умолчанию."""
        _install_levels(monkeypatch, {1: _level(1, referrer_percent=10), 2: _level(2, referrer_percent=None)})
        monkeypatch.setattr(engine, 'count_level_payments', _no_prior_payments)
        components = await build_reward_components(
            None, chain[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        assert [(c.level, c.money_kopeks) for c in components] == [(1, 100_00)]

    @pytest.mark.asyncio
    async def test_personal_percent_overrides_level_one_only(self, chain, monkeypatch):
        """Личный процент партнёра — про его прямых приглашённых, не про всю пирамиду."""
        chain[3].referral_commission_percent = 40
        chain[2].referral_commission_percent = 40
        _install_levels(monkeypatch, {1: _level(1, referrer_percent=10), 2: _level(2, referrer_percent=5)})
        monkeypatch.setattr(engine, 'count_level_payments', _no_prior_payments)
        components = await build_reward_components(
            None, chain[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        assert [(c.recipient_id, c.money_kopeks) for c in components] == [(3, 400_00), (2, 50_00)]

    @pytest.mark.asyncio
    async def test_fixed_amount_adds_to_percent(self, chain, monkeypatch):
        _install_levels(monkeypatch, {1: _level(1, referrer_percent=10, referrer_fixed_kopeks=500_00)})
        monkeypatch.setattr(engine, 'count_level_payments', _no_prior_payments)
        components = await build_reward_components(
            None, chain[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        assert components[0].money_kopeks == 600_00

    @pytest.mark.asyncio
    async def test_max_payments_stops_money_but_not_days(self, chain, monkeypatch):
        _install_levels(
            monkeypatch,
            {1: _level(1, reward_mode='both', referrer_percent=10, referrer_days=7, max_payments=2)},
        )

        async def already_paid(_db, _referrer_id, _referral_id, _level, since=None):
            return 2

        monkeypatch.setattr(engine, 'count_level_payments', already_paid)
        components = await build_reward_components(
            None, chain[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        assert len(components) == 1
        assert components[0].money_kopeks == 0
        assert components[0].days == 7


class TestTriggers:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ('trigger', 'event', 'fires'),
        [
            ('registration', RewardEvent.REGISTRATION, True),
            ('registration', RewardEvent.FIRST_TOPUP, False),
            ('registration', RewardEvent.REPEAT_TOPUP, False),
            ('first_topup', RewardEvent.REGISTRATION, False),
            ('first_topup', RewardEvent.FIRST_TOPUP, True),
            ('first_topup', RewardEvent.REPEAT_TOPUP, False),
            ('every_topup', RewardEvent.REGISTRATION, False),
            ('every_topup', RewardEvent.FIRST_TOPUP, True),
            ('every_topup', RewardEvent.REPEAT_TOPUP, True),
        ],
    )
    async def test_trigger_matrix(self, chain, monkeypatch, trigger, event, fires):
        _install_levels(monkeypatch, {1: _level(1, trigger=trigger, referrer_fixed_kopeks=100_00)})
        monkeypatch.setattr(engine, 'count_level_payments', _no_prior_payments)
        components = await build_reward_components(None, chain[4], event=event, topup_amount_kopeks=1000_00)
        assert bool(components) is fires

    @pytest.mark.asyncio
    async def test_inactive_level_never_fires(self, chain, monkeypatch):
        _install_levels(monkeypatch, {1: _level(1, is_active=False, referrer_fixed_kopeks=100_00)})
        monkeypatch.setattr(engine, 'count_level_payments', _no_prior_payments)
        assert (
            await build_reward_components(None, chain[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00)
            == []
        )


class TestActiveBonusSelection:
    """reward_mode — это и есть выбор активных бонусов за реферала."""

    @pytest.mark.asyncio
    async def test_money_only_ignores_configured_days(self, chain, monkeypatch):
        _install_levels(monkeypatch, {1: _level(1, reward_mode='money', referrer_percent=10, referrer_days=7)})
        monkeypatch.setattr(engine, 'count_level_payments', _no_prior_payments)
        components = await build_reward_components(
            None, chain[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        assert (components[0].money_kopeks, components[0].days) == (100_00, 0)

    @pytest.mark.asyncio
    async def test_days_only_ignores_configured_money(self, chain, monkeypatch):
        _install_levels(monkeypatch, {1: _level(1, reward_mode='days', referrer_percent=10, referrer_days=7)})
        monkeypatch.setattr(engine, 'count_level_payments', _no_prior_payments)
        components = await build_reward_components(
            None, chain[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        assert (components[0].money_kopeks, components[0].days) == (0, 7)

    @pytest.mark.asyncio
    async def test_both_grants_money_and_days(self, chain, monkeypatch):
        _install_levels(monkeypatch, {1: _level(1, reward_mode='both', referrer_percent=10, referrer_days=7)})
        monkeypatch.setattr(engine, 'count_level_payments', _no_prior_payments)
        components = await build_reward_components(
            None, chain[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        assert (components[0].money_kopeks, components[0].days) == (100_00, 7)


class TestRefereeSide:
    @pytest.mark.asyncio
    async def test_referee_paid_once_across_levels(self, chain, monkeypatch):
        """Три уровня с бонусом приглашённому не должны выдать ему бонус трижды."""
        _install_levels(
            monkeypatch,
            {
                1: _level(1, referee_fixed_kopeks=100_00),
                2: _level(2, referee_fixed_kopeks=100_00),
                3: _level(3, referee_fixed_kopeks=100_00),
            },
        )
        monkeypatch.setattr(engine, 'count_level_payments', _no_prior_payments)
        components = await build_reward_components(
            None, chain[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        referee_parts = [c for c in components if not c.is_referrer]
        assert len(referee_parts) == 1
        assert referee_parts[0].money_kopeks == 100_00

    @pytest.mark.asyncio
    async def test_referee_days_carry_their_own_tariff(self, chain, monkeypatch):
        _install_levels(
            monkeypatch,
            {
                1: _level(
                    1, reward_mode='both', referrer_days=3, referrer_tariff_id=7, referee_days=5, referee_tariff_id=9
                )
            },
        )
        monkeypatch.setattr(engine, 'count_level_payments', _no_prior_payments)
        components = await build_reward_components(
            None, chain[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        by_role = {c.is_referrer: c for c in components}
        assert (by_role[True].days, by_role[True].tariff_id) == (3, 7)
        assert (by_role[False].days, by_role[False].tariff_id) == (5, 9)

    @pytest.mark.asyncio
    async def test_referee_component_names_its_level_referrer(self, chain, monkeypatch):
        """Пара для ledger'а берётся из компонента, а не пересчитывается заново."""
        _install_levels(monkeypatch, {1: _level(1, referee_fixed_kopeks=100_00)})
        monkeypatch.setattr(engine, 'count_level_payments', _no_prior_payments)
        components = await build_reward_components(
            None, chain[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        referee_part = next(c for c in components if not c.is_referrer)
        assert referee_part.referrer_id == 3


class _FakeSession:
    """Сессия-заглушка: движок выдачи её только прокидывает дальше."""

    async def commit(self):
        return None

    async def execute(self, *_args, **_kwargs):
        raise AssertionError('выдача наград не должна ходить в базу напрямую')


@pytest.fixture
def granting(chain, monkeypatch):
    """Обвязка выдачи: начисления и записи ledger'а собираются в списки."""
    balance_credits: list[dict] = []
    earnings: list[dict] = []
    day_grants: list[dict] = []

    async def fake_add_user_balance(_db, user, amount, description, **kwargs):
        balance_credits.append({'user_id': user.id, 'amount': amount, 'type': kwargs.get('transaction_type')})
        return True

    async def fake_create_referral_earning(**kwargs):
        earnings.append(kwargs)
        return SimpleNamespace(id=len(earnings))

    async def fake_get_campaign(_db, _user_id):
        return None

    async def fake_grant_days(_db, user, days, tariff_id):
        day_grants.append({'user_id': user.id, 'days': days, 'tariff_id': tariff_id})
        return engine.DaysGrant(days=days, subscription_id=500 + user.id, tariff_name='Про')

    monkeypatch.setattr('app.database.crud.user.add_user_balance', fake_add_user_balance)
    monkeypatch.setattr('app.database.crud.referral.create_referral_earning', fake_create_referral_earning)
    monkeypatch.setattr('app.database.crud.referral.get_user_campaign_id', fake_get_campaign)
    monkeypatch.setattr(engine, 'grant_reward_days', fake_grant_days)
    monkeypatch.setattr(engine, 'count_level_payments', _no_prior_payments)
    return SimpleNamespace(balance_credits=balance_credits, earnings=earnings, day_grants=day_grants, users=chain)


class TestGranting:
    @pytest.mark.asyncio
    async def test_money_reaches_referrer_and_ledger(self, granting, monkeypatch):
        _install_levels(monkeypatch, {1: _level(1, referrer_percent=10)})
        outcomes = await engine.award_referral_rewards(
            _FakeSession(), granting.users[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        assert [(c['user_id'], c['amount']) for c in granting.balance_credits] == [(3, 100_00)]
        assert len(granting.earnings) == 1
        row = granting.earnings[0]
        assert (row['user_id'], row['referral_id'], row['amount_kopeks']) == (3, 4, 100_00)
        assert (row['reward_type'], row['level'], row['reason']) == ('money', 1, engine.REASON_COMMISSION)
        assert outcomes[0].money_credited == 100_00

    @pytest.mark.asyncio
    async def test_first_topup_uses_legacy_reason(self, granting, monkeypatch):
        """Причина совпадает с легаси-строкой: на ней стоит вся существующая статистика."""
        _install_levels(monkeypatch, {1: _level(1, trigger='first_topup', referrer_percent=10)})
        await engine.award_referral_rewards(
            _FakeSession(), granting.users[4], event=RewardEvent.FIRST_TOPUP, topup_amount_kopeks=1000_00
        )
        assert granting.earnings[0]['reason'] == engine.REASON_FIRST_TOPUP

    @pytest.mark.asyncio
    async def test_days_row_carries_zero_money(self, granting, monkeypatch):
        """Дни не должны попасть в денежную сумму — на ней считается вывод средств."""
        _install_levels(monkeypatch, {1: _level(1, reward_mode='days', referrer_days=7, referrer_tariff_id=42)})
        await engine.award_referral_rewards(
            _FakeSession(), granting.users[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        assert granting.balance_credits == []
        row = granting.earnings[0]
        assert (row['amount_kopeks'], row['days_granted'], row['reward_type']) == (0, 7, 'days')
        assert (row['level'], row['tariff_id'], row['reason']) == (1, 42, engine.REASON_DAYS_REFERRER)
        assert granting.day_grants == [{'user_id': 3, 'days': 7, 'tariff_id': 42}]

    @pytest.mark.asyncio
    async def test_referee_money_never_enters_ledger(self, granting, monkeypatch):
        """Деньги приглашённому идут транзакцией, как и раньше: иначе раздуется его реф. доход."""
        _install_levels(monkeypatch, {1: _level(1, referee_fixed_kopeks=300_00)})
        await engine.award_referral_rewards(
            _FakeSession(), granting.users[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        assert [(c['user_id'], c['amount']) for c in granting.balance_credits] == [(4, 300_00)]
        assert granting.earnings == []

    @pytest.mark.asyncio
    async def test_referee_days_row_belongs_to_referee(self, granting, monkeypatch):
        """Строка принадлежит ПОЛУЧАТЕЛЮ дней, иначе дни припишутся пригласившему.

        Полсотни выборок фильтруют ledger по ``user_id = :я``. Оставь строку за
        пригласившим — и каждая из них покажет ему дни, выданные другому человеку.
        """
        _install_levels(monkeypatch, {1: _level(1, reward_mode='days', referee_days=5, referee_tariff_id=9)})
        await engine.award_referral_rewards(
            _FakeSession(), granting.users[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        row = granting.earnings[0]
        assert (row['user_id'], row['referral_id']) == (4, 3)
        assert row['reason'] == engine.REASON_DAYS_REFEREE
        assert engine.is_referee_directed(row['reason']) is True
        assert granting.day_grants == [{'user_id': 4, 'days': 5, 'tariff_id': 9}]

    @pytest.mark.asyncio
    async def test_referrer_days_row_keeps_classic_orientation(self, granting, monkeypatch):
        """Награда пригласившему обязана лечь ровно как раньше: ничего не чинится."""
        _install_levels(monkeypatch, {1: _level(1, reward_mode='days', referrer_days=3)})
        await engine.award_referral_rewards(
            _FakeSession(), granting.users[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        row = granting.earnings[0]
        assert (row['user_id'], row['referral_id']) == (3, 4)
        assert engine.is_referee_directed(row['reason']) is False

    @pytest.mark.asyncio
    async def test_days_granted_before_money(self, granting, monkeypatch):
        """Деньги умеют триггерить авто-продление — дни обязаны лечь раньше."""
        order: list[str] = []

        async def track_days(_db, user, days, tariff_id):
            order.append('days')
            return engine.DaysGrant(days=days, subscription_id=1, tariff_name=None)

        async def track_money(_db, user, amount, description, **kwargs):
            order.append('money')
            return True

        monkeypatch.setattr(engine, 'grant_reward_days', track_days)
        monkeypatch.setattr('app.database.crud.user.add_user_balance', track_money)
        _install_levels(monkeypatch, {1: _level(1, reward_mode='both', referrer_percent=10, referrer_days=7)})
        await engine.award_referral_rewards(
            _FakeSession(), granting.users[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        assert order == ['days', 'money']

    @pytest.mark.asyncio
    async def test_missing_subscription_skips_ledger_row(self, granting, monkeypatch):
        """Ненайденная подписка не должна порождать запись о «выданных» днях."""

        async def failed_grant(_db, _user, _days, _tariff_id):
            return engine.DaysGrant(failure='no_subscription')

        monkeypatch.setattr(engine, 'grant_reward_days', failed_grant)
        _install_levels(monkeypatch, {1: _level(1, reward_mode='days', referrer_days=7)})
        outcomes = await engine.award_referral_rewards(
            _FakeSession(), granting.users[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        assert granting.earnings == []
        assert outcomes == []

    @pytest.mark.asyncio
    async def test_whole_chain_paid_in_one_pass(self, granting, monkeypatch):
        _install_levels(
            monkeypatch,
            {
                1: _level(1, referrer_percent=10),
                2: _level(2, referrer_percent=5),
                3: _level(3, referrer_fixed_kopeks=50_00),
            },
        )
        await engine.award_referral_rewards(
            _FakeSession(), granting.users[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        assert [(c['user_id'], c['amount']) for c in granting.balance_credits] == [(3, 100_00), (2, 50_00), (1, 50_00)]
        assert [(r['user_id'], r['level']) for r in granting.earnings] == [(3, 1), (2, 2), (1, 3)]


class TestNullPercentIsZero:
    """Пустой процент — ноль, а не глобальный REFERRAL_COMMISSION_PERCENT."""

    @pytest.mark.asyncio
    async def test_level_one_without_percent_pays_referrer_nothing(self, chain, monkeypatch):
        _install_levels(monkeypatch, {1: _level(1, referrer_percent=None, referee_fixed_kopeks=300_00)})
        monkeypatch.setattr(engine, 'count_level_payments', _no_prior_payments)
        components = await build_reward_components(
            None, chain[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        assert [(c.recipient_id, c.money_kopeks) for c in components] == [(4, 300_00)]

    @pytest.mark.asyncio
    async def test_personal_percent_still_applies_on_level_one(self, chain, monkeypatch):
        """Личный процент партнёра — явное решение админа и работает без строки уровня."""
        chain[3].referral_commission_percent = 40
        _install_levels(monkeypatch, {1: _level(1, referrer_percent=None)})
        monkeypatch.setattr(engine, 'count_level_payments', _no_prior_payments)
        components = await build_reward_components(
            None, chain[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        assert [(c.recipient_id, c.money_kopeks) for c in components] == [(3, 400_00)]


class TestLevelNotifications:
    """Текст уведомления обязан описывать то, что произошло на самом деле."""

    def test_registration_trigger_does_not_claim_a_topup(self):
        from app.services.referral_service import _level_event_phrase

        phrase = _level_event_phrase(RewardEvent.REGISTRATION, 1, 'Иван')
        assert 'пополн' not in phrase.lower(), 'при триггере регистрации пополнения не было'
        assert 'зарегистрировал' in phrase

    def test_deep_level_does_not_call_payer_your_referral(self):
        """На уровне 2 платит реферал реферала — получатель его не приглашал."""
        from app.services.referral_service import _level_event_phrase

        phrase = _level_event_phrase(RewardEvent.REPEAT_TOPUP, 2, 'Иван')
        assert 'Иван' not in phrase
        assert 'уровень 2' in phrase

    def test_first_level_names_the_referral(self):
        from app.services.referral_service import _level_event_phrase

        phrase = _level_event_phrase(RewardEvent.FIRST_TOPUP, 1, 'Иван')
        assert 'Иван' in phrase
        assert 'первое пополнение' in phrase

    @pytest.mark.asyncio
    async def test_days_only_reward_reaches_email_channel(self, monkeypatch):
        """Иначе письмо о семи выданных днях уходит как «+0.00 ₽»."""
        from app.services import referral_service

        captured = {}

        async def fake_notify(**kwargs):
            captured.update(kwargs)
            return True

        monkeypatch.setattr(referral_service.notification_delivery_service, 'notify_referral_bonus', fake_notify)
        monkeypatch.setattr(settings, 'REFERRAL_NOTIFICATIONS_ENABLED', True)

        email_user = SimpleNamespace(id=9, telegram_id=None, full_name='Почтовый', language='ru')
        await referral_service.send_referral_notification(
            None, None, 'текст', user=email_user, bonus_kopeks=0, bonus_days=7, tariff_name='Про', level=2
        )

        assert captured.get('bonus_days') == 7
        assert captured.get('tariff_name') == 'Про'
        assert captured.get('level') == 2

    @pytest.mark.asyncio
    async def test_email_context_describes_days_not_zero_rubles(self, monkeypatch):
        """formatted_reward — единственное поле, верное и для денег, и для дней."""
        from app.services.notification_delivery_service import notification_delivery_service as svc

        captured = {}

        async def fake_send(**kwargs):
            captured.update(kwargs)
            return True

        monkeypatch.setattr(svc, 'send_notification', fake_send)
        await svc.notify_referral_bonus(
            user=SimpleNamespace(id=1, language='ru'),
            bonus_kopeks=0,
            referral_name='Иван',
            bonus_days=7,
            tariff_name='Про',
        )

        context = captured['context']
        assert context['bonus_days'] == 7
        assert '7 дн.' in context['formatted_reward']
        assert 'Про' in context['formatted_reward']
        # Денежное поле остаётся нулевым — дни в сумму не подмешиваются.
        assert context['bonus_kopeks'] == 0

    @pytest.mark.asyncio
    async def test_email_context_keeps_money_wording_for_money_reward(self, monkeypatch):
        from app.services.notification_delivery_service import notification_delivery_service as svc

        captured = {}

        async def fake_send(**kwargs):
            captured.update(kwargs)
            return True

        monkeypatch.setattr(svc, 'send_notification', fake_send)
        await svc.notify_referral_bonus(
            user=SimpleNamespace(id=1, language='ru'), bonus_kopeks=250_00, referral_name='Иван'
        )

        context = captured['context']
        assert context['formatted_reward'] == context['formatted_bonus']


class TestRewardFormatting:
    @pytest.mark.parametrize(
        ('money', 'days', 'expected_fragments', 'forbidden'),
        [
            (250_00, 0, ['250'], ['дн.']),
            (0, 14, ['14 дн.'], ['0 ₽']),
            (250_00, 7, ['250', '7 дн.'], []),
            (0, 0, ['0'], ['дн.']),
        ],
    )
    def test_reward_total_names_each_currency(self, money, days, expected_fragments, forbidden):
        """«0 ₽» на программе, платящей днями, — ложь; «0 ₽ + 14 дн.» — шум."""
        from app.services.referral_reward_service import format_reward_total

        rendered = format_reward_total(money, days)
        for fragment in expected_fragments:
            assert fragment in rendered
        for fragment in forbidden:
            assert fragment not in rendered


class TestProgramDescription:
    """Описание программы обязано идти из того же источника, что и расчёт."""

    @pytest.mark.asyncio
    async def test_describes_each_active_level(self, chain, monkeypatch):
        from app.services.referral_reward_service import describe_active_levels

        configs = {
            1: _level(1, reward_mode='both', trigger='every_topup', referrer_percent=10, referrer_days=3),
            2: _level(2, referrer_percent=5, trigger='first_topup'),
            3: _level(3, is_active=False, referrer_percent=99),
        }

        async def fake_all(_db):
            return configs

        monkeypatch.setattr(ReferralRewardLevelService, 'get_all', classmethod(lambda cls, db: fake_all(db)))

        lines = await describe_active_levels(None, tariff_names={})
        assert len(lines) == 2, 'выключенный уровень описывать нельзя — он не платит'
        assert '10%' in lines[0] and '3 дн.' in lines[0]
        assert 'с каждого пополнения' in lines[0]
        assert 'за первое пополнение' in lines[1]

    @pytest.mark.asyncio
    async def test_names_the_tariff_days_land_in(self, chain, monkeypatch):
        """«7 дн. подписки» без тарифа умалчивает ровно то, что настроил админ."""
        from app.services.referral_reward_service import describe_active_levels

        configs = {1: _level(1, reward_mode='days', referrer_days=7, referrer_tariff_id=42)}

        async def fake_all(_db):
            return configs

        monkeypatch.setattr(ReferralRewardLevelService, 'get_all', classmethod(lambda cls, db: fake_all(db)))

        lines = await describe_active_levels(None, tariff_names={42: 'Про'})
        assert 'Про' in lines[0]

    @pytest.mark.asyncio
    async def test_referee_bonus_taken_from_first_matching_level(self, chain, monkeypatch):
        """Приглашённому платят один раз — описание обязано говорить то же самое."""
        from app.services.referral_reward_service import describe_referee_bonus

        configs = {
            1: _level(1, referee_fixed_kopeks=100_00),
            2: _level(2, referee_fixed_kopeks=999_00),
        }

        async def fake_all(_db):
            return configs

        monkeypatch.setattr(ReferralRewardLevelService, 'get_all', classmethod(lambda cls, db: fake_all(db)))

        described = await describe_referee_bonus(None, tariff_names={})
        assert '100' in described
        assert '999' not in described

    @pytest.mark.asyncio
    async def test_nothing_configured_describes_nothing(self, chain, monkeypatch):
        from app.services.referral_reward_service import describe_active_levels, describe_referee_bonus

        async def fake_all(_db):
            return {}

        monkeypatch.setattr(ReferralRewardLevelService, 'get_all', classmethod(lambda cls, db: fake_all(db)))

        assert await describe_active_levels(None) == []
        assert await describe_referee_bonus(None) is None


class TestInvitePromise:
    """Приглашение обязано обещать то, что реально начислят.

    Легаси-ключ REFERRAL_FIRST_TOPUP_BONUS_KOPEKS в многоуровневой схеме ничем не
    управляет: бонус приглашённому задаётся уровнем. Пообещать по старому ключу —
    значит отправить другу неправду от имени пользователя.
    """

    @pytest.mark.asyncio
    async def test_levels_scheme_promises_level_bonus(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        import app.handlers.referral as screen

        captured = {}

        async def fake_edit(_callback, text, _keyboard):
            captured['text'] = text

        monkeypatch.setattr(screen, 'edit_or_answer_photo', fake_edit)
        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
        monkeypatch.setattr(settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 999_00)
        monkeypatch.setattr(
            type(screen.settings), 'get_bot_referral_link', lambda self, code, bot: 'https://t.me/b?start=x'
        )
        monkeypatch.setattr(type(screen.settings), 'get_cabinet_referral_link', lambda self, code: '')

        async def fake_referee_bonus(_db, tariff_names=None, language=None, referrer=None):
            return '7 дн. подписки (Про) за регистрацию'

        async def fake_tariffs(_db):
            return {}

        monkeypatch.setattr('app.services.referral_reward_service.describe_referee_bonus', fake_referee_bonus)
        monkeypatch.setattr(screen, '_reward_tariff_names', fake_tariffs)

        bot = MagicMock()
        bot.get_me = AsyncMock(return_value=SimpleNamespace(username='b'))
        callback = MagicMock()
        callback.bot = bot
        callback.answer = AsyncMock()

        await screen.create_invite_message(callback, SimpleNamespace(referral_code='X', language='ru'), None)

        assert '7 дн. подписки' in captured['text']
        assert '999' not in captured['text'], 'легаси-бонус в многоуровневой схеме не начисляется'

    @pytest.mark.asyncio
    async def test_legacy_scheme_keeps_its_promise(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        import app.handlers.referral as screen

        captured = {}

        async def fake_edit(_callback, text, _keyboard):
            captured['text'] = text

        monkeypatch.setattr(screen, 'edit_or_answer_photo', fake_edit)
        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'legacy')
        monkeypatch.setattr(settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 500_00)
        monkeypatch.setattr(settings, 'REFERRAL_MINIMUM_TOPUP_KOPEKS', 100_00)
        monkeypatch.setattr(
            type(screen.settings), 'get_bot_referral_link', lambda self, code, bot: 'https://t.me/b?start=x'
        )
        monkeypatch.setattr(type(screen.settings), 'get_cabinet_referral_link', lambda self, code: '')

        bot = MagicMock()
        bot.get_me = AsyncMock(return_value=SimpleNamespace(username='b'))
        callback = MagicMock()
        callback.bot = bot
        callback.answer = AsyncMock()

        await screen.create_invite_message(callback, SimpleNamespace(referral_code='X', language='ru'), None)

        assert '500' in captured['text']


class TestDepthHonesty:
    """Описание не должно обещать уровни, до которых движок не доходит."""

    @pytest.mark.asyncio
    async def test_levels_beyond_depth_are_not_advertised(self, chain, monkeypatch):
        from app.services.referral_reward_service import describe_active_levels

        configs = {
            1: _level(1, referrer_percent=10),
            2: _level(2, referrer_percent=5),
            5: _level(5, referrer_percent=1),
        }

        async def fake_all(_db):
            return configs

        monkeypatch.setattr(ReferralRewardLevelService, 'get_all', classmethod(lambda cls, db: fake_all(db)))
        monkeypatch.setattr(settings, 'REFERRAL_MAX_LEVEL_DEPTH', 2)

        lines = await describe_active_levels(None, tariff_names={})
        assert len(lines) == 2
        assert not any('Уровень 5' in line for line in lines)

    @pytest.mark.asyncio
    async def test_referee_bonus_ignores_levels_beyond_depth(self, chain, monkeypatch):
        from app.services.referral_reward_service import describe_referee_bonus

        configs = {5: _level(5, referee_fixed_kopeks=100_00)}

        async def fake_all(_db):
            return configs

        monkeypatch.setattr(ReferralRewardLevelService, 'get_all', classmethod(lambda cls, db: fake_all(db)))
        monkeypatch.setattr(settings, 'REFERRAL_MAX_LEVEL_DEPTH', 2)

        assert await describe_referee_bonus(None) is None


class TestUngrantablePromises:
    """Обещать награду, которая не может быть выдана, хуже, чем не обещать."""

    @staticmethod
    def _levels(monkeypatch, configs):
        async def fake_all(_db):
            return configs

        monkeypatch.setattr(ReferralRewardLevelService, 'get_all', classmethod(lambda cls, db: fake_all(db)))

    @pytest.mark.asyncio
    async def test_registration_days_without_tariff_are_not_promised(self, chain, monkeypatch):
        """У только что созданного пользователя подписки нет: без тарифа дни не лягут никуда."""
        from app.services.referral_reward_service import describe_referee_bonus

        self._levels(
            monkeypatch,
            {1: _level(1, reward_mode='days', trigger='registration', referee_days=7, referee_tariff_id=None)},
        )
        assert await describe_referee_bonus(None) is None

    @pytest.mark.asyncio
    async def test_registration_days_with_tariff_are_promised(self, chain, monkeypatch):
        """С тарифом подписка будет создана — обещание честное."""
        from app.services.referral_reward_service import describe_referee_bonus

        self._levels(
            monkeypatch,
            {1: _level(1, reward_mode='days', trigger='registration', referee_days=7, referee_tariff_id=9)},
        )
        described = await describe_referee_bonus(None, tariff_names={9: 'Про'})
        assert described is not None
        assert 'Про' in described

    @pytest.mark.asyncio
    async def test_topup_days_without_tariff_stay_promised(self, chain, monkeypatch):
        """На пополнении подписка у получателя обычно уже есть — обещание остаётся."""
        from app.services.referral_reward_service import describe_active_levels

        self._levels(
            monkeypatch,
            {1: _level(1, reward_mode='days', trigger='every_topup', referrer_days=3, referrer_tariff_id=None)},
        )
        lines = await describe_active_levels(None)
        assert lines and '3 дн.' in lines[0]


class TestRefereeRowsStayOutOfReferrerTotals:
    """Строка награды приглашённому принадлежит ему, а не пригласившему.

    Для денег это было неважно — их сумма нулевая. С появлением SUM(days_granted)
    отсутствие фильтра даёт пользователю, не пригласившему никого,
    «Приглашено: 0 · Заработано: 7 дн.», где вторая сторона пары — его же
    пригласивший.
    """

    def test_every_summary_query_calls_the_predicate(self):
        """Проверка ВЫЗОВА, не поведения.

        Поведение самого предиката проверяется на реальных строках в
        tests/crud/test_referral_earnings_filter.py — подсчёт вхождений в
        исходнике этого не умеет и однажды уже пропустил подменённое тело.
        Здесь же ловится другое: забытый предикат в одной из четырёх выборок.
        """
        import inspect

        from app.utils.user_utils import get_user_referral_summary

        source = inspect.getsource(get_user_referral_summary)
        assert source.count('not_referee_directed()') >= 4, (
            'сводка заработка должна отбрасывать строки наград приглашённому '
            'во всех выборках: итог, месяц, последние начисления, разбивка по типам'
        )


class TestGeneratedTextIsLocalized:
    """Сгенерированное описание вставляется в локализованные экраны.

    Русская вставка внутри английского экрана — не косметика: этим же текстом
    пользователь приглашает друга, то есть отправляет его от своего имени.
    """

    @pytest.fixture
    def one_level(self, monkeypatch):
        config = _level(1, reward_mode='both', trigger='every_topup', referrer_percent=10, referrer_days=7)

        async def fake_all(_db):
            return {1: config}

        monkeypatch.setattr(ReferralRewardLevelService, 'get_all', classmethod(lambda cls, db: fake_all(db)))
        monkeypatch.setattr(settings, 'REFERRAL_MAX_LEVEL_DEPTH', 3)

    @pytest.mark.asyncio
    async def test_english_description_has_no_russian(self, one_level):
        from app.services.referral_reward_service import describe_active_levels

        line = (await describe_active_levels(None, language='en'))[0]
        assert 'Level 1' in line
        assert 'on every top-up' in line
        assert not any('Ѐ' <= ch <= 'ӿ' for ch in line), f'кириллица в английском описании: {line}'

    @pytest.mark.asyncio
    async def test_russian_description_is_unchanged(self, one_level):
        from app.services.referral_reward_service import describe_active_levels

        line = (await describe_active_levels(None, language='ru'))[0]
        assert line.startswith('Уровень 1')
        assert 'с каждого пополнения' in line

    def test_reward_total_days_label_is_localized(self):
        from app.services.referral_reward_service import format_reward_total

        assert 'days' in format_reward_total(0, 7, 'en')
        assert 'дн.' in format_reward_total(0, 7, 'ru')

    @pytest.mark.asyncio
    async def test_referrer_registration_days_stay_promised(self, chain, monkeypatch):
        """Пригласивший в системе давно — его дни лягут в основную подписку.

        Глушить его описание по тому же признаку, что и приглашённого, значит
        умолчать о награде, которая реально приходит.
        """
        from app.services.referral_reward_service import describe_active_levels

        configs = {1: _level(1, reward_mode='days', trigger='registration', referrer_days=5, referrer_tariff_id=None)}

        async def fake_all(_db):
            return configs

        monkeypatch.setattr(ReferralRewardLevelService, 'get_all', classmethod(lambda cls, db: fake_all(db)))
        lines = await describe_active_levels(None)
        assert lines and '5 дн.' in lines[0]


class TestLegacyImportPercent:
    """Классический процент задают три ключа, а не один.

    Перенос делается с поводом «первое пополнение», значит верный источник —
    процент первого платежа, когда он задан. Ступени комиссии уровнем невыразимы
    вовсе, и молча их потерять хуже, чем сказать об этом.
    """

    def test_plain_percent_is_taken_when_nothing_overrides_it(self, monkeypatch):
        from app.services.referral_reward_service import legacy_percent_for_import

        monkeypatch.setattr(settings, 'REFERRAL_COMMISSION_PERCENT', 25)
        monkeypatch.setattr(settings, 'REFERRAL_FIRST_PAYMENT_COMMISSION_PERCENT', None)
        monkeypatch.setattr(settings, 'REFERRAL_RECURRING_COMMISSION_TIERS', '')

        percent, notes = legacy_percent_for_import()
        assert percent == 25
        assert notes == []

    def test_first_payment_percent_wins_and_is_announced(self, monkeypatch):
        from app.services.referral_reward_service import legacy_percent_for_import

        monkeypatch.setattr(settings, 'REFERRAL_COMMISSION_PERCENT', 25)
        monkeypatch.setattr(settings, 'REFERRAL_FIRST_PAYMENT_COMMISSION_PERCENT', 40)
        monkeypatch.setattr(settings, 'REFERRAL_RECURRING_COMMISSION_TIERS', '')

        percent, notes = legacy_percent_for_import()
        assert percent == 40, 'повод уровня — первое пополнение, значит и ставка его'
        assert any('40' in note for note in notes)

    def test_tiers_cannot_be_expressed_and_are_reported(self, monkeypatch):
        from app.services.referral_reward_service import legacy_percent_for_import

        monkeypatch.setattr(settings, 'REFERRAL_COMMISSION_PERCENT', 25)
        monkeypatch.setattr(settings, 'REFERRAL_FIRST_PAYMENT_COMMISSION_PERCENT', None)
        monkeypatch.setattr(settings, 'REFERRAL_RECURRING_COMMISSION_TIERS', '0:10,10:15')

        percent, notes = legacy_percent_for_import()
        assert percent == 25
        assert any('TIERS' in note for note in notes), 'потерянные ступени обязаны быть названы'

    def test_both_editors_import_identically(self, monkeypatch):
        """Результат не должен зависеть от того, откуда нажали кнопку."""
        import inspect

        from app.cabinet.routes import admin_partners
        from app.handlers.admin import referral_levels

        for handler in (admin_partners.import_legacy_referral_settings, referral_levels.import_legacy_settings):
            source = inspect.getsource(handler)
            assert 'legacy_percent_for_import()' in source
            # Именно обращение к настройке, а не упоминание её имени в докстринге.
            assert 'settings.REFERRAL_COMMISSION_PERCENT' not in source, (
                'ставка обязана браться общим расчётом, иначе два переноса разойдутся'
            )


class TestDepthOnThePayoutPath:
    """Глубина обязана резать ВЫПЛАТЫ, а не только описание.

    Проверки глубины были только на тексте условий: подмена предела на 999
    оставляла весь набор зелёным, хотя платить начали бы все уровни подряд.
    """

    @pytest.mark.asyncio
    async def test_levels_beyond_depth_pay_nothing(self, chain, monkeypatch):
        _install_levels(
            monkeypatch,
            {
                1: _level(1, referrer_percent=10),
                2: _level(2, referrer_percent=5),
                3: _level(3, referrer_percent=2),
            },
        )
        monkeypatch.setattr(engine, 'count_level_payments', _no_prior_payments)
        monkeypatch.setattr(settings, 'REFERRAL_MAX_LEVEL_DEPTH', 2)

        components = await build_reward_components(
            None, chain[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        assert [c.level for c in components] == [1, 2], 'третий уровень настроен, но цепочка до него не идёт'

    @pytest.mark.asyncio
    async def test_depth_one_pays_only_the_direct_referrer(self, chain, monkeypatch):
        _install_levels(
            monkeypatch,
            {1: _level(1, referrer_percent=10), 2: _level(2, referrer_percent=5)},
        )
        monkeypatch.setattr(engine, 'count_level_payments', _no_prior_payments)
        monkeypatch.setattr(settings, 'REFERRAL_MAX_LEVEL_DEPTH', 1)

        components = await build_reward_components(
            None, chain[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        assert [(c.recipient_id, c.level) for c in components] == [(3, 1)]


class TestThresholdGatesThePayout:
    """Порог обязан резать ВЫПЛАТЫ, а не только карточку в админке.

    Проверка самого предиката в изоляции этого не показывает: убери его вызов из
    расчёта — и она останется зелёной, а закрытый уровень начнёт платить.
    """

    @staticmethod
    def _counts(monkeypatch, per_user):
        async def fake_count(_db, user_id, *, active_only):
            return per_user.get(user_id, 0)

        monkeypatch.setattr(engine, 'count_referrals', fake_count)

    @pytest.mark.asyncio
    async def test_closed_level_pays_nothing(self, chain, monkeypatch):
        _install_levels(
            monkeypatch,
            {
                1: _level(1, referrer_percent=10),
                2: _level(2, referrer_percent=5, required_referrals=10),
            },
        )
        monkeypatch.setattr(engine, 'count_level_payments', _no_prior_payments)
        # У реферера уровня 2 (id=2) всего 3 реферала — порог не взят.
        self._counts(monkeypatch, {2: 3, 3: 50})

        components = await build_reward_components(
            None, chain[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        assert [(c.recipient_id, c.level) for c in components] == [(3, 1)]

    @pytest.mark.asyncio
    async def test_level_opens_once_the_threshold_is_reached(self, chain, monkeypatch):
        _install_levels(
            monkeypatch,
            {
                1: _level(1, referrer_percent=10),
                2: _level(2, referrer_percent=5, required_referrals=10),
            },
        )
        monkeypatch.setattr(engine, 'count_level_payments', _no_prior_payments)
        self._counts(monkeypatch, {2: 10, 3: 50})

        components = await build_reward_components(
            None, chain[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        assert [(c.recipient_id, c.level) for c in components] == [(3, 1), (2, 2)]

    @pytest.mark.asyncio
    async def test_closed_level_pays_the_referee_nothing_either(self, chain, monkeypatch):
        """Закрытый уровень не действует целиком, включая бонус приглашённому."""
        _install_levels(
            monkeypatch,
            {1: _level(1, referrer_percent=10, referee_fixed_kopeks=300_00, required_referrals=10)},
        )
        monkeypatch.setattr(engine, 'count_level_payments', _no_prior_payments)
        self._counts(monkeypatch, {3: 2})

        components = await build_reward_components(
            None, chain[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        assert components == []
