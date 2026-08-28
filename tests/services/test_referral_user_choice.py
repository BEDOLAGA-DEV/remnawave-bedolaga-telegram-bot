"""Выбор пользователя: куда класть дни и что получать.

Две настройки, обе выключены по умолчанию. Пока они выключены, поведение обязано
быть прежним до последнего компонента — сохранённый выбор не должен ничего
менять, иначе включение и выключение настройки меняли бы выплаты задним числом.

Второй инвариант: показанное совпадает с начисленным. Выбор урезает награду, и
лестница обязана урезаться ровно так же — иначе экран обещает обе стороны, а
приходит одна.
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
    describe_active_levels,
    resolve_reward_preference,
)


def _user(uid: int, *, referred_by: int | None = None, preference: str | None = None, chosen_sub: int | None = None):
    return SimpleNamespace(
        id=uid,
        telegram_id=1000 + uid,
        full_name=f'U{uid}',
        language='ru',
        referred_by_id=referred_by,
        referral_commission_percent=None,
        referral_reward_preference=preference,
        referral_days_subscription_id=chosen_sub,
        balance_kopeks=0,
        has_made_first_topup=False,
    )


def _level(level: int, **kwargs) -> LevelConfig:
    base = {
        'is_active': True,
        'reward_mode': 'both',
        'trigger': 'every_topup',
        'referrer_percent': 10,
        'referrer_fixed_kopeks': None,
        'referrer_days': 7,
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


async def _no_prior(_db, _a, _b, _level, since=None):
    return 0


@pytest.fixture
def wired(monkeypatch):
    users = {1: _user(1), 2: _user(2, referred_by=1)}

    async def fake_get_user(_db, uid):
        return users.get(uid)

    async def fake_counts(_db, _uid, *, active_only):
        return 0

    monkeypatch.setattr(engine, 'get_user_by_id', fake_get_user)
    monkeypatch.setattr(engine, 'count_referrals', fake_counts)
    monkeypatch.setattr(engine, 'count_level_payments', _no_prior)
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
    monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', 'chain')
    monkeypatch.setattr(settings, 'REFERRAL_ALLOW_REWARD_KIND_CHOICE', True)
    monkeypatch.setattr(settings, 'REFERRAL_ALLOW_DAYS_TARGET_CHOICE', True)
    return users


def _install(monkeypatch, configs):
    async def as_coro(value):
        return value

    monkeypatch.setattr(ReferralRewardLevelService, 'get_all', classmethod(lambda cls, db: as_coro(configs)))
    monkeypatch.setattr(
        ReferralRewardLevelService, 'get_level', classmethod(lambda cls, db, lvl: as_coro(configs.get(lvl)))
    )


class TestDefaultsAreOff:
    """Пока настройки выключены, сохранённый выбор не значит ничего."""

    def test_both_settings_default_to_off(self):
        fields = type(settings).model_fields
        assert fields['REFERRAL_ALLOW_DAYS_TARGET_CHOICE'].default is False
        assert fields['REFERRAL_ALLOW_REWARD_KIND_CHOICE'].default is False

    def test_preference_ignored_while_the_setting_is_off(self, wired, monkeypatch):
        monkeypatch.setattr(settings, 'REFERRAL_ALLOW_REWARD_KIND_CHOICE', False)
        assert resolve_reward_preference(_user(1, preference='money')) is None

    @pytest.mark.asyncio
    async def test_payout_unchanged_while_the_setting_is_off(self, wired, monkeypatch):
        """Ключевое: выключенная настройка возвращает ПРЕЖНЮЮ награду целиком."""
        _install(monkeypatch, {1: _level(1)})
        wired[1].referral_reward_preference = 'money'
        monkeypatch.setattr(settings, 'REFERRAL_ALLOW_REWARD_KIND_CHOICE', False)

        components = await build_reward_components(
            None, wired[2], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )

        assert (components[0].money_kopeks, components[0].days) == (100_00, 7)

    def test_choice_requires_the_levels_scheme(self, wired, monkeypatch):
        """При классической схеме дни наградой не выдаются — выбирать нечего."""
        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'legacy')
        assert settings.is_referral_reward_kind_choice_enabled() is False
        assert settings.is_referral_days_target_choice_enabled() is False


class TestRewardKindChoice:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ('preference', 'expected'),
        [
            ('money', (100_00, 0)),
            ('days', (0, 7)),
            # Выбор двоичный: у не выбиравшего берутся деньги, а не обе стороны.
            (None, (100_00, 0)),
        ],
    )
    async def test_preference_trims_the_reward(self, wired, monkeypatch, preference, expected):
        _install(monkeypatch, {1: _level(1)})
        wired[1].referral_reward_preference = preference

        components = await build_reward_components(
            None, wired[2], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )

        assert (components[0].money_kopeks, components[0].days) == expected

    @pytest.mark.asyncio
    async def test_percent_is_zeroed_together_with_the_money(self, wired, monkeypatch):
        """Иначе строка ledger сообщала бы процент при нулевой сумме."""
        _install(monkeypatch, {1: _level(1)})
        wired[1].referral_reward_preference = 'days'

        components = await build_reward_components(
            None, wired[2], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )

        assert components[0].percent == 0

    @pytest.mark.asyncio
    async def test_one_sided_rule_is_not_cancelled_by_the_choice(self, wired, monkeypatch):
        """«Предпочитаю деньги» на правиле, платящем одними днями, не отменяет награду.

        Человек выбирал между двумя сторонами, а не отказывался от единственной.
        """
        _install(monkeypatch, {1: _level(1, reward_mode='days', referrer_percent=None)})
        wired[1].referral_reward_preference = 'money'

        components = await build_reward_components(
            None, wired[2], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )

        assert components[0].days == 7

    @pytest.mark.asyncio
    async def test_preference_belongs_to_the_recipient(self, wired, monkeypatch):
        """У приглашённого свой выбор, а не выбор пригласившего."""
        _install(monkeypatch, {1: _level(1, referee_fixed_kopeks=500_00, referee_days=3)})
        wired[1].referral_reward_preference = 'money'
        wired[2].referral_reward_preference = 'days'

        components = await build_reward_components(
            None, wired[2], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        referrer = next(c for c in components if c.is_referrer)
        referee = next(c for c in components if not c.is_referrer)

        assert (referrer.money_kopeks, referrer.days) == (100_00, 0)
        assert (referee.money_kopeks, referee.days) == (0, 3)

    @pytest.mark.asyncio
    async def test_unknown_preference_falls_back_to_money(self, wired, monkeypatch):
        """Опечатка не должна выбирать за человека дни.

        Деньги — то, что программа платила всегда; молча перевести человека на
        дни из-за мусора в поле значило бы сменить вид награды без его ведома.
        """
        _install(monkeypatch, {1: _level(1)})
        wired[1].referral_reward_preference = 'МОНЕТЫ'

        components = await build_reward_components(
            None, wired[2], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )

        assert (components[0].money_kopeks, components[0].days) == (100_00, 0)


class TestLadderMatchesTheChoice:
    """Показанное обязано совпадать с начисленным и здесь."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize('preference', ['money', 'days'])
    async def test_ladder_shows_only_the_chosen_side(self, wired, monkeypatch, preference):
        _install(monkeypatch, {1: _level(1)})
        viewer = wired[1]
        viewer.referral_reward_preference = preference

        lines = await describe_active_levels(None, viewer=viewer, language='ru')
        components = await build_reward_components(
            None, wired[2], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )

        shows_money = '%' in lines[0]
        shows_days = 'дн.' in lines[0]
        assert shows_money is (components[0].money_kopeks > 0), lines[0]
        assert shows_days is (components[0].days > 0), lines[0]

    @pytest.mark.asyncio
    async def test_without_a_choice_the_ladder_shows_money(self, wired, monkeypatch):
        """Не выбиравший получает деньги — лестница обязана показывать их одни."""
        _install(monkeypatch, {1: _level(1)})

        lines = await describe_active_levels(None, viewer=wired[1], language='ru')
        assert '%' in lines[0] and 'дн.' not in lines[0], lines[0]

    @pytest.mark.asyncio
    async def test_with_the_setting_off_the_ladder_shows_both(self, wired, monkeypatch):
        """Пока админ выбор не разрешил, правило платит обе стороны — так и пишем."""
        _install(monkeypatch, {1: _level(1)})
        monkeypatch.setattr(settings, 'REFERRAL_ALLOW_REWARD_KIND_CHOICE', False)

        lines = await describe_active_levels(None, viewer=wired[1], language='ru')
        assert '%' in lines[0] and 'дн.' in lines[0], lines[0]
