"""Режим рангов: один получатель, один уровень, выбор по числу рефералов.

В режиме ``REFERRAL_LEVELS_MODE='tiers'`` номер уровня означает не глубину
цепочки, а ступень самого партнёра. Отсюда три инварианта, каждый из которых
здесь и проверяется: платят ТОЛЬКО прямому пригласившему, применяется РОВНО ОДИН
уровень, и выбирает его число рефералов партнёра.

Отдельно закреплено, что режим по умолчанию — ``chain``: включение схемы уровней
на живой установке не должно менять получателей выплат без ведома админа.
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
    describe_referee_bonus,
    resolve_tier_progress,
    select_tier_config,
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
def tiers(monkeypatch):
    """Режим рангов, цепочка 4 → 3 → 2 → 1 и управляемое число рефералов."""
    users = {
        1: _user(1),
        2: _user(2, referred_by=1),
        3: _user(3, referred_by=2),
        4: _user(4, referred_by=3),
    }
    counts: dict[int, dict[bool, int]] = {}

    async def fake_get_user_by_id(_db, uid):
        return users.get(uid)

    async def fake_count_referrals(_db, user_id, *, active_only):
        return counts.get(user_id, {}).get(active_only, 0)

    monkeypatch.setattr(engine, 'get_user_by_id', fake_get_user_by_id)
    monkeypatch.setattr(engine, 'count_referrals', fake_count_referrals)
    monkeypatch.setattr(engine, 'count_level_payments', _no_prior_payments)
    # Поля pydantic-настроек патчатся на ЭКЗЕМПЛЯРЕ: на классе это дескриптор.
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
    monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', 'tiers')
    monkeypatch.setattr(settings, 'REFERRAL_MAX_LEVEL_DEPTH', 3)
    return SimpleNamespace(users=users, counts=counts)


async def _no_prior_payments(_db, _referrer_id, _referral_id, _level, since=None):
    return 0


def _install(monkeypatch, configs: dict[int, LevelConfig]):
    monkeypatch.setattr(
        ReferralRewardLevelService,
        'get_all',
        classmethod(lambda cls, db: _as_coro(configs)),
    )
    monkeypatch.setattr(
        ReferralRewardLevelService,
        'get_level',
        classmethod(lambda cls, db, level: _as_coro(configs.get(level))),
    )


async def _as_coro(value):
    return value


LADDER = {
    1: _level(1, referrer_percent=5, required_referrals=0),
    2: _level(2, referrer_percent=10, required_referrals=10),
    3: _level(3, referrer_percent=20, required_referrals=25),
}


class TestDefaultIsChain:
    def test_mode_defaults_to_chain(self):
        """Значение по умолчанию менять нельзя: оно определяет, кому идут деньги."""
        assert type(settings).model_fields['REFERRAL_LEVELS_MODE'].default == 'chain'

    def test_unknown_value_is_not_tiers(self, monkeypatch):
        """Опечатка в .env не должна включать другую схему выплат."""
        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
        for bad in ('tier', 'ranks', 'TIERS!', '', None):
            monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', bad)
            assert settings.get_referral_levels_mode() == 'chain'
            assert settings.is_referral_tier_levels() is False

    def test_mode_alone_does_nothing_under_legacy_scheme(self, monkeypatch):
        """Режим — уточнение внутри схемы уровней, а не самостоятельный выключатель."""
        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'legacy')
        monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', 'tiers')
        assert settings.is_referral_tier_levels() is False


class TestSelection:
    @pytest.mark.asyncio
    async def test_picks_highest_reached_threshold(self, tiers, monkeypatch):
        _install(monkeypatch, LADDER)
        tiers.counts[3] = {True: 12, False: 12}

        chosen = await select_tier_config(None, LADDER, 3)
        assert chosen.level == 2

    @pytest.mark.asyncio
    async def test_below_every_threshold_gives_nothing(self, tiers, monkeypatch):
        """Лестница без стартовой ступени не платит никому — это видно в расчёте."""
        ladder = {2: _level(2, referrer_percent=10, required_referrals=10)}
        _install(monkeypatch, ladder)
        tiers.counts[3] = {True: 4, False: 4}

        assert await select_tier_config(None, ladder, 3) is None

    @pytest.mark.asyncio
    async def test_inactive_tier_is_skipped(self, tiers, monkeypatch):
        ladder = {
            1: _level(1, referrer_percent=5, required_referrals=0),
            2: _level(2, referrer_percent=10, required_referrals=10, is_active=False),
        }
        _install(monkeypatch, ladder)
        tiers.counts[3] = {True: 50, False: 50}

        assert (await select_tier_config(None, ladder, 3)).level == 1

    @pytest.mark.asyncio
    async def test_threshold_wins_over_level_number(self, tiers, monkeypatch):
        """Номера расставляет админ руками, лестницу задаёт порог, а не нумерация."""
        ladder = {
            2: _level(2, referrer_percent=10, required_referrals=25),
            3: _level(3, referrer_percent=20, required_referrals=5),
        }
        _install(monkeypatch, ladder)
        tiers.counts[3] = {True: 7, False: 7}

        assert (await select_tier_config(None, ladder, 3)).level == 3

    @pytest.mark.asyncio
    async def test_active_only_flag_is_honoured_per_level(self, tiers, monkeypatch):
        """Порог по «любым приглашённым» берётся накруткой пустых аккаунтов."""
        ladder = {
            1: _level(1, referrer_percent=5, required_referrals=0),
            2: _level(2, referrer_percent=10, required_referrals=10, required_referrals_active_only=True),
            3: _level(3, referrer_percent=20, required_referrals=10, required_referrals_active_only=False),
        }
        _install(monkeypatch, ladder)
        # Приглашено 30, пополнил один: ступень «с пополнением» не взята.
        tiers.counts[3] = {True: 1, False: 30}

        assert (await select_tier_config(None, ladder, 3)).level == 3


class TestPayout:
    @pytest.mark.asyncio
    async def test_only_direct_referrer_is_paid(self, tiers, monkeypatch):
        """Ключевое отличие режима: цепочка не обходится вовсе."""
        _install(monkeypatch, LADDER)
        tiers.counts[3] = {True: 12, False: 12}

        components = await build_reward_components(
            None, tiers.users[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=100_00
        )

        assert [c.recipient_id for c in components] == [3]
        assert components[0].level == 2
        assert components[0].money_kopeks == 10_00

    @pytest.mark.asyncio
    async def test_exactly_one_level_applies(self, tiers, monkeypatch):
        """Достигнутые ступени не складываются: 5% + 10% не превращаются в 15%."""
        _install(monkeypatch, LADDER)
        tiers.counts[3] = {True: 30, False: 30}

        components = await build_reward_components(
            None, tiers.users[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=100_00
        )

        assert len(components) == 1
        assert components[0].money_kopeks == 20_00

    @pytest.mark.asyncio
    async def test_personal_percent_beats_any_tier(self, tiers, monkeypatch):
        """Личный процент — про работу с ПРЯМЫМИ приглашёнными, а в рангах они все прямые.

        Привязка к ``level == 1`` отменяла бы ручную ставку админа любому партнёру
        выше стартового ранга — молча и именно у самых активных.
        """
        _install(monkeypatch, LADDER)
        tiers.users[3].referral_commission_percent = 40
        tiers.counts[3] = {True: 30, False: 30}

        components = await build_reward_components(
            None, tiers.users[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=100_00
        )

        assert components[0].money_kopeks == 40_00

    @pytest.mark.asyncio
    async def test_nothing_when_no_tier_reached(self, tiers, monkeypatch):
        ladder = {2: _level(2, referrer_percent=10, required_referrals=10)}
        _install(monkeypatch, ladder)
        tiers.counts[3] = {True: 0, False: 0}

        assert (
            await build_reward_components(
                None, tiers.users[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=100_00
            )
            == []
        )

    @pytest.mark.asyncio
    async def test_user_without_referrer_gets_nothing(self, tiers, monkeypatch):
        _install(monkeypatch, LADDER)
        assert (
            await build_reward_components(
                None, tiers.users[1], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=100_00
            )
            == []
        )

    @pytest.mark.asyncio
    async def test_self_referral_is_refused(self, tiers, monkeypatch):
        """Битые данные не должны платить пользователю за собственное пополнение."""
        _install(monkeypatch, LADDER)
        broken = _user(9, referred_by=9)
        tiers.users[9] = broken
        tiers.counts[9] = {True: 50, False: 50}

        assert (
            await build_reward_components(None, broken, event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=100_00)
            == []
        )

    @pytest.mark.asyncio
    async def test_depth_setting_does_not_limit_tiers(self, tiers, monkeypatch):
        """Ранг 5 работает при глубине 3: в рангах цепочки нет, ограничивать нечего."""
        ladder = {5: _level(5, referrer_percent=30, required_referrals=10)}
        _install(monkeypatch, ladder)
        monkeypatch.setattr(settings, 'REFERRAL_MAX_LEVEL_DEPTH', 3)
        tiers.counts[3] = {True: 20, False: 20}

        components = await build_reward_components(
            None, tiers.users[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=100_00
        )

        assert components[0].level == 5
        assert components[0].money_kopeks == 30_00

    @pytest.mark.asyncio
    async def test_rank_does_not_change_with_the_event(self, tiers, monkeypatch):
        """Ранг — свойство партнёра, а не повода начисления.

        Прежде выбирался лучший ПОДХОДЯЩИЙ событию ранг: щедрее, но тогда на
        пополнение действовала одна ступень, а на регистрацию другая — и
        отметить в лестнице «ваш ранг» становилось нечего. Теперь повод входит в
        само правило ступени: не подходит — не платит никто.
        """
        ladder = {
            1: _level(1, referrer_fixed_kopeks=500, trigger='registration', required_referrals=0),
            2: _level(2, referrer_percent=10, trigger='every_topup', required_referrals=10),
        }
        _install(monkeypatch, ladder)
        tiers.counts[3] = {True: 30, False: 30}

        # Ранг партнёра — 2, и он настроен только на пополнения.
        topup = await build_reward_components(
            None, tiers.users[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=100_00
        )
        assert [c.level for c in topup] == [2]

        registration = await build_reward_components(
            None, tiers.users[4], event=RewardEvent.REGISTRATION, topup_amount_kopeks=0
        )
        assert registration == [], 'ступень ниже не должна подменять собой ранг партнёра'

    @pytest.mark.asyncio
    async def test_marked_rank_is_the_paying_rank(self, tiers, monkeypatch):
        """Отмеченный в лестнице ранг обязан быть тем же, что и платящий.

        Иначе экран говорит «ваш ранг 2 — вам не начисляется», а деньги приходят
        по рангу 1, показанному без отметки.
        """
        ladder = {
            1: _level(1, referrer_percent=10, trigger='every_topup', required_referrals=0),
            2: _level(2, referrer_fixed_kopeks=0, trigger='registration', required_referrals=3),
        }
        _install(monkeypatch, ladder)
        tiers.counts[3] = {True: 3, False: 3}

        lines = await describe_active_levels(None, viewer=tiers.users[3], language='ru')
        marked = [line for line in lines if 'ваш уровень' in line]
        paid = await build_reward_components(
            None, tiers.users[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=100_00
        )

        assert len(marked) == 1 and 'Уровень 2' in marked[0]
        assert paid == [], 'ранг 2 не настроен на пополнения — значит и платить по ним нечему'

    @pytest.mark.asyncio
    async def test_max_payments_counts_per_tier(self, tiers, monkeypatch):
        """Лимит принадлежит рангу: общий счёт по паре брал бы чужую историю.

        Денежные строки классической схемы и прежних выплат по цепочке лежат в
        тех же колонках и от ранговых неотличимы — «всё по паре» исчерпывало бы
        лимит нового ранга при рождении.
        """
        seen: list[int] = []

        async def spy(_db, _referrer_id, _referral_id, level, since=None):
            seen.append(level)
            return 5

        monkeypatch.setattr(engine, 'count_level_payments', spy)
        ladder = {2: _level(2, referrer_percent=10, required_referrals=10, max_payments=5)}
        _install(monkeypatch, ladder)
        tiers.counts[3] = {True: 30, False: 30}

        components = await build_reward_components(
            None, tiers.users[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=100_00
        )

        assert seen == [2], 'лимит обязан считаться по своему рангу, а не по всей паре'
        assert components == []


class TestReferee:
    @pytest.mark.asyncio
    async def test_referee_bonus_comes_from_inviter_tier(self, tiers, monkeypatch):
        """Обещанное приглашённому обязано совпасть с начисленным.

        Бонус задаётся правилом ранга ПРИГЛАСИВШЕГО: описание, не знающее его,
        обещало бы стартовый ранг, а расчёт выдал бы ранг повыше.
        """
        ladder = {
            1: _level(1, referee_fixed_kopeks=100_00, required_referrals=0),
            2: _level(2, referee_fixed_kopeks=300_00, required_referrals=10),
        }
        _install(monkeypatch, ladder)
        tiers.counts[3] = {True: 30, False: 30}

        components = await build_reward_components(
            None, tiers.users[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=100_00
        )
        paid = next(c for c in components if not c.is_referrer)

        promised = await describe_referee_bonus(None, referrer=tiers.users[3])
        assert paid.money_kopeks == 300_00
        assert '300' in promised, f'обещано «{promised}», а начислено 300 ₽'

    @pytest.mark.asyncio
    async def test_without_inviter_the_base_tier_is_promised(self, tiers, monkeypatch):
        """Аноним видит гарантированный минимум, а не чужое достижение."""
        ladder = {
            1: _level(1, referee_fixed_kopeks=100_00, required_referrals=0),
            2: _level(2, referee_fixed_kopeks=300_00, required_referrals=10),
        }
        _install(monkeypatch, ladder)

        promised = await describe_referee_bonus(None)
        assert '100' in promised


class TestLadderText:
    @pytest.mark.asyncio
    async def test_marks_the_viewer_tier(self, tiers, monkeypatch):
        """Без отметки лестница читается как список складывающихся наград."""
        _install(monkeypatch, LADDER)
        tiers.counts[3] = {True: 12, False: 12}

        lines = await describe_active_levels(None, viewer=tiers.users[3], language='ru')

        marked = [line for line in lines if 'ваш уровень' in line]
        assert len(marked) == 1
        assert 'Уровень 2' in marked[0]

    @pytest.mark.asyncio
    async def test_ladder_is_ordered_by_threshold(self, tiers, monkeypatch):
        ladder = {
            2: _level(2, referrer_percent=10, required_referrals=25),
            3: _level(3, referrer_percent=20, required_referrals=5),
        }
        _install(monkeypatch, ladder)

        lines = await describe_active_levels(None, language='ru')
        assert lines[0].startswith('Уровень 3'), lines

    @pytest.mark.asyncio
    async def test_tiers_beyond_depth_are_described(self, tiers, monkeypatch):
        """Ранг 5 при глубине 3 платит — значит, обязан и описываться."""
        ladder = {5: _level(5, referrer_percent=30, required_referrals=10)}
        _install(monkeypatch, ladder)
        monkeypatch.setattr(settings, 'REFERRAL_MAX_LEVEL_DEPTH', 3)

        lines = await describe_active_levels(None, language='ru')
        assert any('Уровень 5' in line for line in lines), lines


class TestProgress:
    @pytest.mark.asyncio
    async def test_reports_current_and_nearest_next(self, tiers, monkeypatch):
        _install(monkeypatch, LADDER)
        tiers.counts[3] = {True: 12, False: 12}

        progress = await resolve_tier_progress(None, tiers.users[3])

        assert progress.current_level == 2
        assert progress.next_level == 3
        assert progress.next_remaining == 13

    @pytest.mark.asyncio
    async def test_nearest_next_is_by_distance_not_number(self, tiers, monkeypatch):
        """«Ещё 2 до ранга 4» полезнее, чем «ещё 40 до ранга 2»."""
        ladder = {
            1: _level(1, required_referrals=0, referrer_percent=5),
            2: _level(2, required_referrals=50, referrer_percent=10),
            4: _level(4, required_referrals=12, referrer_percent=20),
        }
        _install(monkeypatch, ladder)
        tiers.counts[3] = {True: 10, False: 10}

        progress = await resolve_tier_progress(None, tiers.users[3])
        assert (progress.next_level, progress.next_remaining) == (4, 2)

    @pytest.mark.asyncio
    async def test_none_outside_tier_mode(self, tiers, monkeypatch):
        """В цепочке ранга не существует — и показывать нечего."""
        _install(monkeypatch, LADDER)
        monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', 'chain')

        assert await resolve_tier_progress(None, tiers.users[3]) is None

    @pytest.mark.asyncio
    async def test_counts_referrals_once_per_flag(self, tiers, monkeypatch):
        """Десять рангов не должны давать десять одинаковых COUNT(*) на пополнение."""
        calls: list[bool] = []

        async def counting(_db, _user_id, *, active_only):
            calls.append(active_only)
            return 30

        monkeypatch.setattr(engine, 'count_referrals', counting)
        ladder = {n: _level(n, referrer_percent=n, required_referrals=n) for n in range(1, 11)}
        _install(monkeypatch, ladder)

        await select_tier_config(None, ladder, 3)
        assert calls == [True], f'ожидался один запрос на флаг, получено {len(calls)}'

    @pytest.mark.asyncio
    async def test_progress_does_not_count_twice(self, tiers, monkeypatch):
        """Экран прогресса и выбор ранга считают рефералов одним и тем же счётчиком.

        Без общего счётчика те же COUNT(*) выполнялись дважды на каждом открытии
        партнёрского экрана — один раз при выборе ранга, один раз для «сколько
        осталось».
        """
        calls: list[bool] = []

        async def counting(_db, _user_id, *, active_only):
            calls.append(active_only)
            return 12

        monkeypatch.setattr(engine, 'count_referrals', counting)
        _install(monkeypatch, LADDER)

        await resolve_tier_progress(None, tiers.users[3])

        assert sorted(calls) == [False, True], f'ожидалось по одному запросу на флаг, получено {calls}'


class TestTextMatchesPayout:
    """Найдено состязательным аудитом: показанное расходилось с начисленным."""

    @pytest.mark.asyncio
    async def test_personal_rate_is_named_when_it_overrides_the_tier(self, tiers, monkeypatch):
        """«Ранг 3: 20% ← ваш ранг» — утверждение лично о смотрящем, и оно было ложным.

        Личный процент партнёра перебивает процент любого ранга (получатель здесь
        всегда прямой), поэтому лестница обязана назвать его, иначе партнёр видит
        20%, а получает свои 5%.
        """
        _install(monkeypatch, LADDER)
        viewer = tiers.users[3]
        viewer.referral_commission_percent = 5
        tiers.counts[3] = {True: 30, False: 30}

        lines = await describe_active_levels(None, viewer=viewer, language='ru')
        components = await build_reward_components(
            None, tiers.users[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )

        assert components[0].money_kopeks == 50_00
        assert any('индивидуальная ставка 5%' in line for line in lines), lines

    @pytest.mark.asyncio
    async def test_no_personal_rate_no_note(self, tiers, monkeypatch):
        _install(monkeypatch, LADDER)
        tiers.counts[3] = {True: 30, False: 30}

        lines = await describe_active_levels(None, viewer=tiers.users[3], language='ru')
        assert not any('индивидуальная ставка' in line for line in lines), lines

    @pytest.mark.asyncio
    async def test_anonymous_is_promised_only_the_base_tier(self, tiers, monkeypatch):
        """Аноним не должен получать обещание ступени, до которой никто не дошёл.

        Перебор всей лестницы возвращал первый ранг, у которого бонус вообще
        задан: «500 ₽ новому пользователю» при условии в 25 рефералов у
        пригласившего, которых ни у кого ещё нет.
        """
        ladder = {
            1: _level(1, referrer_percent=5),
            2: _level(2, required_referrals=25, referee_fixed_kopeks=500_00),
        }
        _install(monkeypatch, ladder)

        assert await describe_referee_bonus(None, language='ru') is None

    @pytest.mark.asyncio
    async def test_referee_promise_matches_the_inviter_rank_exactly(self, tiers, monkeypatch):
        """Обещанное приглашённому берётся из ранга пригласившего — и только из него.

        Ранг ниже с бонусом за регистрацию к делу не относится: он партнёру уже
        не действует, и обещать его бонус значило бы обещать чужое правило.
        """
        ladder = {
            1: _level(1, trigger='registration', referee_fixed_kopeks=100_00),
            2: _level(2, required_referrals=5, referrer_percent=20, trigger='every_topup'),
        }
        _install(monkeypatch, ladder)
        tiers.counts[3] = {True: 5, False: 5}

        promised = await describe_referee_bonus(None, referrer=tiers.users[3], language='ru')
        paid = await build_reward_components(
            None, tiers.users[4], event=RewardEvent.REGISTRATION, topup_amount_kopeks=0
        )

        assert paid == [], 'ранг партнёра настроен на пополнения — за регистрацию не платит'
        assert promised is None, f'обещать нечего, а обещано «{promised}»'

    @pytest.mark.asyncio
    async def test_next_tier_is_never_below_the_current_one(self, tiers, monkeypatch):
        """Печаталось «Ваш ранг: 3» и следом «До ранга 2: ещё 6».

        У правил с разными ``required_referrals_active_only`` счётчики разные,
        поэтому ступень с меньшим порогом остаётся недостигнутой, когда бо́льшая
        уже взята.
        """
        ladder = {
            1: _level(1, referrer_percent=1),
            2: _level(2, required_referrals=8, required_referrals_active_only=True, referrer_percent=10),
            3: _level(3, required_referrals=10, required_referrals_active_only=False, referrer_percent=20),
        }
        _install(monkeypatch, ladder)
        tiers.counts[3] = {True: 2, False: 12}

        progress = await resolve_tier_progress(None, tiers.users[3])

        assert progress.current_level == 3
        assert progress.next_level is None, f'ранг {progress.next_level} ниже текущего'

    @pytest.mark.asyncio
    async def test_current_tier_stays_in_the_ladder_even_when_it_pays_nothing(self, tiers, monkeypatch):
        """Ранг без наград пригласившему выпадал из перечня вместе с меткой «ваш ранг».

        Он при этом остаётся выбранным и обнуляет доход — то есть исчезала ровно
        та строка, которая объясняет, почему платить перестали.
        """
        ladder = {
            1: _level(1, referrer_percent=5),
            2: _level(2, required_referrals=10, referrer_percent=10),
            3: _level(3, required_referrals=25, referee_fixed_kopeks=300_00),
        }
        _install(monkeypatch, ladder)
        tiers.counts[3] = {True: 30, False: 30}

        lines = await describe_active_levels(None, viewer=tiers.users[3], language='ru')
        marked = [line for line in lines if 'ваш уровень' in line]

        assert len(marked) == 1
        assert 'Уровень 3' in marked[0] and 'не начисляется' in marked[0], marked

    @pytest.mark.asyncio
    async def test_upcoming_level_that_pays_nothing_is_visible(self, tiers, monkeypatch):
        """Ступень без награды пригласившему видна ЗАРАНЕЕ, а не только когда стала своей.

        Прогресс зовёт к ней («До уровня 2: ещё 10»), а её достижение обнуляет
        доход: она замещает собой платящую. Пока она была скрыта, обрыв дохода
        объяснить по экрану было нельзя — уровня, который его обнулил, там не было.
        """
        ladder = {
            1: _level(1, referrer_percent=5),
            2: _level(2, required_referrals=10, referee_fixed_kopeks=300_00),
        }
        _install(monkeypatch, ladder)
        tiers.counts[3] = {True: 0, False: 0}

        lines = await describe_active_levels(None, viewer=tiers.users[3], language='ru')
        upcoming = [line for line in lines if 'Уровень 2' in line]

        assert len(upcoming) == 1, lines
        assert 'не начисляется' in upcoming[0], upcoming[0]

    @pytest.mark.asyncio
    async def test_chain_still_hides_a_level_that_adds_nothing(self, tiers, monkeypatch):
        """В цепочке такая ступень ничего не замещает — показывать нечего."""
        ladder = {
            1: _level(1, referrer_percent=5),
            2: _level(2, referee_fixed_kopeks=300_00),
        }
        _install(monkeypatch, ladder)
        monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', 'chain')

        lines = await describe_active_levels(None, viewer=tiers.users[3], language='ru')
        assert not any('Уровень 2' in line for line in lines), lines

    @pytest.mark.asyncio
    async def test_next_tier_is_the_one_that_will_actually_apply(self, tiers, monkeypatch):
        """При равных порогах применяется БОЛЬШИЙ номер — прогресс звал к меньшему.

        «До ранга 2: ещё 5», а на пятом реферале включался ранг 3 с другой ставкой.
        """
        ladder = {
            1: _level(1, referrer_percent=1),
            2: _level(2, required_referrals=10, referrer_percent=10),
            3: _level(3, required_referrals=10, referrer_percent=5),
        }
        _install(monkeypatch, ladder)
        tiers.counts[3] = {True: 5, False: 5}

        progress = await resolve_tier_progress(None, tiers.users[3])
        assert progress.next_level == 3

        # И действительно применится он же.
        tiers.counts[3] = {True: 10, False: 10}
        assert (await select_tier_config(None, ladder, 3)).level == 3


class TestChainModeIsUntouchedByTheNewArguments:
    """Новые аргументы описаний в режиме цепочки обязаны быть no-op.

    Мутация `if tier_mode and viewer is not None:` → `if viewer is not None:`
    выживала: в режиме по умолчанию в лестницу начинала добавляться отметка
    «← ваш ранг» и выполнялся лишний COUNT(*) на каждое открытие экрана.
    """

    @pytest.mark.asyncio
    async def test_viewer_without_personal_rate_changes_nothing_under_chain(self, tiers, monkeypatch):
        """У партнёра без личной ставки цепочка выглядит ровно как без смотрящего.

        Отметка «ваш уровень» существует только в режиме за приглашённых, и
        протечь в режим по умолчанию она не должна.
        """
        _install(monkeypatch, LADDER)
        monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', 'chain')
        tiers.counts[3] = {True: 30, False: 30}
        tiers.users[3].referral_commission_percent = None

        without = await describe_active_levels(None, language='ru')
        with_viewer = await describe_active_levels(None, viewer=tiers.users[3], language='ru')

        assert without == with_viewer
        assert not any('ваш уровень' in line for line in with_viewer)

    @pytest.mark.asyncio
    async def test_chain_shows_the_personal_rate_on_level_one(self, tiers, monkeypatch):
        """В цепочке личная ставка перебивает процент уровня 1 — и обязана быть названа.

        Экран печатал процент правила, а начислялась личная ставка: партнёру,
        одобренному со своей ставкой, показывали чужое число. И оговорка должна
        говорить «на первом уровне», а не «на любом»: глубже платят не за его
        приглашённых.
        """
        _install(monkeypatch, LADDER)
        monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', 'chain')
        viewer = tiers.users[3]
        viewer.referral_commission_percent = 40

        lines = await describe_active_levels(None, viewer=viewer, language='ru')
        components = await build_reward_components(
            None, tiers.users[4], event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=1000_00
        )
        direct = next(c for c in components if c.referrer_id == viewer.id and c.is_referrer)

        assert direct.percent == 40
        assert lines[0].startswith('Уровень 1: 40%'), lines[0]
        assert any('на первом уровне' in line for line in lines), lines

    @pytest.mark.asyncio
    async def test_viewer_costs_no_extra_queries_under_chain(self, tiers, monkeypatch):
        calls: list[bool] = []

        async def counting(_db, _user_id, *, active_only):
            calls.append(active_only)
            return 30

        monkeypatch.setattr(engine, 'count_referrals', counting)
        _install(monkeypatch, LADDER)
        monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', 'chain')

        await describe_active_levels(None, viewer=tiers.users[3], language='ru')
        assert calls == [], 'в цепочке ранг не считается — запросов быть не должно'

    @pytest.mark.asyncio
    async def test_referrer_changes_nothing_under_chain(self, tiers, monkeypatch):
        ladder = {
            1: _level(1, referee_fixed_kopeks=100_00),
            2: _level(2, required_referrals=10, referee_fixed_kopeks=300_00),
        }
        _install(monkeypatch, ladder)
        monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', 'chain')
        tiers.counts[3] = {True: 30, False: 30}

        assert await describe_referee_bonus(None, language='ru') == await describe_referee_bonus(
            None, language='ru', referrer=tiers.users[3]
        )


class TestProgressFormatting:
    """format_tier_progress: обе строки можно было вырезать и остаться зелёным."""

    def test_renders_rank_and_distance(self):
        from app.services.referral_reward_service import TierProgress, format_tier_progress

        lines = format_tier_progress(
            TierProgress(current_level=2, referrals_any=12, referrals_active=12, next_level=3, next_remaining=13),
            'ru',
        )
        assert any('2' in line and 'уровень' in line.lower() for line in lines), lines
        assert any('3' in line and '13' in line for line in lines), lines

    def test_says_so_when_no_rank_reached(self):
        from app.services.referral_reward_service import TierProgress, format_tier_progress

        lines = format_tier_progress(TierProgress(current_level=None, referrals_any=0, referrals_active=0), 'ru')
        assert lines and 'не открыт' in lines[0], lines

    def test_top_rank_has_no_next_line(self):
        from app.services.referral_reward_service import TierProgress, format_tier_progress

        lines = format_tier_progress(
            TierProgress(current_level=3, referrals_any=99, referrals_active=99, next_level=None), 'ru'
        )
        assert len(lines) == 1, lines

    def test_outside_tier_mode_renders_nothing(self):
        from app.services.referral_reward_service import format_tier_progress

        assert format_tier_progress(None, 'ru') == []


class TestModeNormalisation:
    """Нормализация значения: без неё 'TIERS' и ' tiers ' молча стали бы 'chain'."""

    @pytest.mark.parametrize('raw', ['TIERS', ' tiers ', 'Tiers', '\ttiers\n'])
    def test_valid_value_in_any_shape_is_accepted(self, monkeypatch, raw):
        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
        monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', raw)
        assert settings.get_referral_levels_mode() == 'tiers'
        assert settings.is_referral_tier_levels() is True

    @pytest.mark.parametrize('raw', ['CHAIN', ' chain '])
    def test_chain_in_any_shape_stays_chain(self, monkeypatch, raw):
        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
        monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', raw)
        assert settings.get_referral_levels_mode() == 'chain'


class TestChainRefereePromise:
    """Цепочка: приглашённому нельзя обещать бонус закрытого уровня.

    Уровень, порог которого пригласивший не набрал, не платит ни ему, ни
    приглашённому — правило не действует целиком. Обещание при этом уходило в
    первом же сообщении, которое человек видит после перехода по ссылке.
    """

    @pytest.mark.asyncio
    async def test_locked_level_is_not_promised(self, tiers, monkeypatch):
        ladder = {1: _level(1, required_referrals=5, referee_fixed_kopeks=500_00, trigger='first_topup')}
        _install(monkeypatch, ladder)
        monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', 'chain')
        tiers.counts[3] = {True: 0, False: 0}

        promised = await describe_referee_bonus(None, referrer=tiers.users[3], language='ru')
        paid = await build_reward_components(
            None, tiers.users[4], event=RewardEvent.FIRST_TOPUP, topup_amount_kopeks=100_00
        )

        assert paid == [], 'уровень закрыт порогом — платить нечему'
        assert promised is None, f'обещано «{promised}», а не начислится ничего'

    @pytest.mark.asyncio
    async def test_unlocked_level_is_still_promised(self, tiers, monkeypatch):
        """Контроль: набранный порог обещание не отменяет."""
        ladder = {1: _level(1, required_referrals=5, referee_fixed_kopeks=500_00, trigger='first_topup')}
        _install(monkeypatch, ladder)
        monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', 'chain')
        tiers.counts[3] = {True: 5, False: 5}

        promised = await describe_referee_bonus(None, referrer=tiers.users[3], language='ru')
        paid = await build_reward_components(
            None, tiers.users[4], event=RewardEvent.FIRST_TOPUP, topup_amount_kopeks=100_00
        )

        assert any(not c.is_referrer and c.money_kopeks == 500_00 for c in paid)
        assert promised is not None and '500' in promised

    @pytest.mark.asyncio
    async def test_anonymous_caller_still_sees_the_terms(self, tiers, monkeypatch):
        """Без пригласившего порог проверять не по кому — условия остаются видимы."""
        ladder = {1: _level(1, required_referrals=5, referee_fixed_kopeks=500_00, trigger='first_topup')}
        _install(monkeypatch, ladder)
        monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', 'chain')

        assert await describe_referee_bonus(None, language='ru') is not None


class TestRestoreInvalidatesLevelCache:
    """Восстановление из бэкапа обязано сбрасывать кэш уровней.

    Восстановление пишет строки НАПРЯМУЮ, минуя crud, а сброс кэша живёт в crud.
    Без него экран показывает восстановленную лестницу, а начисляется доресторная
    — и так до перезапуска бота.
    """

    def test_restore_paths_invalidate_the_cache(self):
        import ast
        import inspect

        from app.services import backup_service

        tree = ast.parse(inspect.getsource(backup_service))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == '_invalidate_restored_caches'
        ]
        # Две ветки восстановления: полный бэкап и снимок данных.
        assert len(calls) >= 2, f'сброс кэша вызывается {len(calls)} раз, а веток восстановления две'

    def test_helper_actually_clears_the_cache(self, monkeypatch):
        from app.services.backup_service import BackupService

        ReferralRewardLevelService._cache = {1: _level(1)}
        BackupService._invalidate_restored_caches()

        assert ReferralRewardLevelService._cache is None


class TestImpossiblePercentIsNotPromised:
    """Процент на поводе «за регистрацию» не начислится никогда — и не обещается.

    На этом событии пополнения нет, а деньги считаются от суммы. Экран печатал
    «Уровень 1: 20% от суммы за регистрацию», а начислялось ноль — в обоих режимах.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize('mode', ['chain', 'tiers'])
    async def test_registration_percent_is_not_shown(self, tiers, monkeypatch, mode):
        ladder = {1: _level(1, trigger='registration', referrer_percent=20)}
        _install(monkeypatch, ladder)
        monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', mode)
        tiers.counts[3] = {True: 0, False: 0}

        lines = await describe_active_levels(None, viewer=tiers.users[3], language='ru')
        paid = await build_reward_components(
            None, tiers.users[4], event=RewardEvent.REGISTRATION, topup_amount_kopeks=0
        )

        assert not any(c.money_kopeks for c in paid), 'процент на регистрации начислиться не может'
        assert not any('%' in line for line in lines), lines

    @pytest.mark.asyncio
    async def test_fixed_amount_at_registration_is_still_promised(self, tiers, monkeypatch):
        """Контроль: фиксированная сумма на регистрации работает и обязана обещаться."""
        ladder = {1: _level(1, trigger='registration', referrer_fixed_kopeks=500_00)}
        _install(monkeypatch, ladder)
        monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', 'chain')

        lines = await describe_active_levels(None, language='ru')
        paid = await build_reward_components(
            None, tiers.users[4], event=RewardEvent.REGISTRATION, topup_amount_kopeks=0
        )

        assert any(c.money_kopeks == 500_00 for c in paid)
        assert any('500' in line for line in lines), lines

    @pytest.mark.asyncio
    async def test_percent_on_a_topup_trigger_is_still_promised(self, tiers, monkeypatch):
        ladder = {1: _level(1, trigger='every_topup', referrer_percent=20)}
        _install(monkeypatch, ladder)
        monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', 'chain')

        lines = await describe_active_levels(None, language='ru')
        assert any('20%' in line for line in lines), lines
