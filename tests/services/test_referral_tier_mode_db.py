"""Режим рангов на настоящей БД: пороги и лимиты считаются запросами, а не моками.

Проверки в ``test_referral_tier_mode.py`` подменяют ``count_referrals`` и
``count_level_payments``, поэтому сами SQL-условия там не исполняются. А ошибиться
можно ровно в них: порог сверяется с числом строк ``User.referred_by_id``, а
«активность» реферала — с ``has_made_first_topup``. Здесь и то и другое проходит
через реальный SQLite.
"""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from app.config import settings
from app.database.models import Base, ReferralEarning, ReferralRewardLevel, ReferralRewardType, User
from app.services.referral_reward_service import (
    ReferralRewardLevelService,
    RewardEvent,
    build_reward_components,
    count_referrals,
    resolve_tier_progress,
)
from tests.fixtures.sqlite_memory import ensure_real_aiosqlite, memory_session


# Полный набор, а не точечный список: get_user_by_id подтягивает подписки, и
# перечислять таблицы вручную значит ловить «no such table» на каждой новой связи.
TABLES = list(Base.metadata.sorted_tables)


def _user(uid: int, *, referred_by: int | None = None, topped_up: bool = False) -> User:
    return User(
        id=uid,
        telegram_id=1000 + uid,
        first_name=f'User {uid}',
        language='ru',
        referred_by_id=referred_by,
        has_made_first_topup=topped_up,
        balance_kopeks=0,
    )


def _level(level: int, **kwargs) -> ReferralRewardLevel:
    base = {
        'level': level,
        'is_active': True,
        'reward_mode': 'money',
        'trigger': 'every_topup',
        'referrer_percent': 10,
        'referrer_days': 0,
        'referee_days': 0,
        'max_payments': 0,
        'required_referrals': 0,
        'required_referrals_active_only': True,
    }
    base.update(kwargs)
    return ReferralRewardLevel(**base)


@pytest.fixture
def tier_mode(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
    monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', 'tiers')
    monkeypatch.setattr(settings, 'REFERRAL_MAX_LEVEL_DEPTH', 3)
    ReferralRewardLevelService.invalidate_cache()
    yield
    ReferralRewardLevelService.invalidate_cache()


@pytest.mark.asyncio
async def test_threshold_counts_only_direct_referrals(monkeypatch, tier_mode):
    """Порог отвечает на «насколько вырос сам партнёр», а не «сколько под ним всего»."""
    ensure_real_aiosqlite(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        # Двое приглашены партнёром, третий — приглашённым партнёра.
        db.add_all([_user(2, referred_by=1), _user(3, referred_by=1), _user(4, referred_by=2)])
        await db.commit()

        assert await count_referrals(db, 1, active_only=False) == 2


@pytest.mark.asyncio
async def test_active_only_counts_those_who_topped_up(monkeypatch, tier_mode):
    ensure_real_aiosqlite(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        db.add_all(
            [
                _user(2, referred_by=1, topped_up=True),
                _user(3, referred_by=1, topped_up=False),
                _user(4, referred_by=1, topped_up=False),
            ]
        )
        await db.commit()

        assert await count_referrals(db, 1, active_only=False) == 3
        assert await count_referrals(db, 1, active_only=True) == 1


@pytest.mark.asyncio
async def test_tier_is_chosen_from_real_rows(monkeypatch, tier_mode):
    """Сквозной путь: строки БД → выбор ранга → начисленная сумма."""
    ensure_real_aiosqlite(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        # Десять приглашённых с пополнением открывают второй ранг.
        for uid in range(2, 12):
            db.add(_user(uid, referred_by=1, topped_up=True))
        db.add_all(
            [
                _level(1, referrer_percent=5, required_referrals=0),
                _level(2, referrer_percent=15, required_referrals=10),
                _level(3, referrer_percent=25, required_referrals=25),
            ]
        )
        await db.commit()

        referee = await db.get(User, 2)
        components = await build_reward_components(
            db, referee, event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=200_00
        )

        assert len(components) == 1, 'применяться должен ровно один ранг'
        assert components[0].recipient_id == 1
        assert components[0].level == 2
        assert components[0].money_kopeks == 30_00


@pytest.mark.asyncio
async def test_inactive_referrals_do_not_open_a_tier(monkeypatch, tier_mode):
    """Порог по «с пополнением» не берётся накруткой пустых аккаунтов."""
    ensure_real_aiosqlite(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        for uid in range(2, 32):
            db.add(_user(uid, referred_by=1, topped_up=False))
        db.add_all(
            [
                _level(1, referrer_percent=5, required_referrals=0),
                _level(2, referrer_percent=15, required_referrals=10, required_referrals_active_only=True),
            ]
        )
        await db.commit()

        referee = await db.get(User, 2)
        components = await build_reward_components(
            db, referee, event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=200_00
        )

        assert components[0].level == 1
        assert components[0].money_kopeks == 10_00


@pytest.mark.asyncio
async def test_payment_cap_reads_this_tier_only(monkeypatch, tier_mode):
    """Строки ДРУГИХ уровней не должны исчерпывать лимит ранга.

    На установке с историей денежные строки классической схемы лежат в level=1;
    если бы лимит считался по всей паре, новый ранг был бы исчерпан при рождении.

    Даты проставлены руками, и это существенно. Граница ``since`` — момент
    создания правила, а SQLite хранит datetime строкой и сравнивает её как
    строку: у одинаковых до секунды значений форматы бинда и хранения расходятся,
    и фильтр молча отсекает ВСЁ. Тест на дефолтных датах остался бы зелёным, даже
    если убрать фильтр по уровню, — то есть не проверял бы ровно то, ради чего
    написан. Здесь правило заведомо старше выплат, поэтому отсечь их может только
    условие ``level``.
    """
    ensure_real_aiosqlite(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        long_ago = datetime.now(UTC) - timedelta(days=30)
        recently = datetime.now(UTC) - timedelta(days=1)

        db.add(_user(1))
        for uid in range(2, 13):
            db.add(_user(uid, referred_by=1, topped_up=True))
        db.add(_level(2, referrer_percent=15, required_referrals=10, max_payments=2, created_at=long_ago))
        # Пять выплат по уровню 1 — из прежней схемы, но ПОСЛЕ появления правила.
        for _ in range(5):
            db.add(
                ReferralEarning(
                    user_id=1,
                    referral_id=2,
                    amount_kopeks=100,
                    reason='referral_commission_topup',
                    level=1,
                    reward_type=ReferralRewardType.MONEY.value,
                    created_at=recently,
                )
            )
        await db.commit()

        referee = await db.get(User, 2)
        components = await build_reward_components(
            db, referee, event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=200_00
        )

        assert components and components[0].money_kopeks == 30_00, 'чужие строки не должны съедать лимит ранга'


@pytest.mark.asyncio
async def test_payment_cap_does_stop_this_tier(monkeypatch, tier_mode):
    """Контроль к предыдущему: свои строки лимит исчерпывают.

    Без этой пары первый тест доказывал бы лишь то, что лимит не срабатывает
    никогда — а это выглядит одинаково.
    """
    ensure_real_aiosqlite(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        long_ago = datetime.now(UTC) - timedelta(days=30)
        recently = datetime.now(UTC) - timedelta(days=1)

        db.add(_user(1))
        for uid in range(2, 13):
            db.add(_user(uid, referred_by=1, topped_up=True))
        db.add(_level(2, referrer_percent=15, required_referrals=10, max_payments=2, created_at=long_ago))
        for _ in range(2):
            db.add(
                ReferralEarning(
                    user_id=1,
                    referral_id=2,
                    amount_kopeks=100,
                    reason='referral_level_topup',
                    level=2,
                    reward_type=ReferralRewardType.MONEY.value,
                    created_at=recently,
                )
            )
        await db.commit()

        referee = await db.get(User, 2)
        components = await build_reward_components(
            db, referee, event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=200_00
        )

        assert components == [], 'лимит ранга исчерпан — деньги начисляться не должны'


@pytest.mark.asyncio
async def test_progress_reports_real_counts(monkeypatch, tier_mode):
    ensure_real_aiosqlite(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        for uid in range(2, 15):
            db.add(_user(uid, referred_by=1, topped_up=uid <= 5))
        db.add_all(
            [
                _level(1, referrer_percent=5, required_referrals=0),
                _level(2, referrer_percent=15, required_referrals=10),
            ]
        )
        await db.commit()

        partner = await db.get(User, 1)
        progress = await resolve_tier_progress(db, partner)

        assert progress.referrals_any == 13
        assert progress.referrals_active == 4
        assert progress.current_level == 1, 'по «с пополнением» второй ранг ещё не набран'
        assert (progress.next_level, progress.next_remaining) == (2, 6)


@pytest.mark.asyncio
async def test_chain_mode_still_pays_the_whole_chain(monkeypatch):
    """Контроль: режим по умолчанию не изменился ни на строку."""
    ensure_real_aiosqlite(monkeypatch)
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
    monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', 'chain')
    monkeypatch.setattr(settings, 'REFERRAL_MAX_LEVEL_DEPTH', 3)
    ReferralRewardLevelService.invalidate_cache()

    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by=1), _user(3, referred_by=2)])
        db.add_all([_level(1, referrer_percent=10), _level(2, referrer_percent=5)])
        await db.commit()

        referee = await db.get(User, 3)
        components = await build_reward_components(
            db, referee, event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=100_00
        )

        assert sorted((c.recipient_id, c.money_kopeks) for c in components) == [(1, 5_00), (2, 10_00)]

    ReferralRewardLevelService.invalidate_cache()


@pytest.mark.asyncio
async def test_level_repair_would_flatten_a_rank(monkeypatch, tier_mode):
    """Показывает УЩЕРБ, от которого защищает гейт ниже.

    ``_repair_referral_levels`` написана в терминах цепочки: она переписывает
    ``level`` в расстояние между парой. В режиме рангов расстояние всегда 1, так
    что ранг 2 превратился бы в 1 — а вместе с ним обнулился бы и учёт лимита
    выплат, ведь ``count_level_payments`` считает по номеру.
    """
    ensure_real_aiosqlite(monkeypatch)
    from app.services.account_merge_service import _repair_referral_levels

    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1), _user(2, referred_by=1)])
        db.add(
            ReferralEarning(
                user_id=1,
                referral_id=2,
                amount_kopeks=100_00,
                reason='referral_level_topup',
                level=2,
                reward_type=ReferralRewardType.MONEY.value,
            )
        )
        await db.commit()

        await _repair_referral_levels(db, await db.get(User, 1))
        await db.commit()

        row = (await db.execute(sa.select(ReferralEarning))).scalar_one()
        assert row.level == 1, 'пересчёт по цепочке действительно уплощает ранг — гейт обязателен'


def test_merge_gates_level_repair_on_chain_mode():
    """Сторож на вызов: пересчёт уровней запускается только в режиме цепочки.

    Проверяется исходник, а не поведение: полный ``merge_accounts`` тянет пол-бота,
    а повторить здесь условие вызова значило бы проверять собственную копию, а не
    код — такой тест остаётся зелёным, даже когда гейт из вызова убрали.
    """
    import ast
    import inspect

    from app.services import account_merge_service

    tree = ast.parse(inspect.getsource(account_merge_service))
    guards = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and any(
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id == '_repair_referral_levels'
            for inner in ast.walk(node)
        )
    ]

    assert guards, 'вызов _repair_referral_levels должен стоять под условием'
    condition = ast.unparse(guards[0].test)
    assert 'is_referral_tier_levels' in condition, f'режим рангов не исключён из пересчёта: {condition}'
    assert 'not ' in condition, f'условие должно ИСКЛЮЧАТЬ режим рангов: {condition}'


@pytest.mark.asyncio
async def test_average_income_counts_every_rank(monkeypatch, tier_mode):
    """«Средний доход с реферала» брал срез level==1 — верный только для цепочки.

    В рангах level это ступень, а доход и так приходит от прямых рефералов, так
    что срез занижал среднее в разы у всех, кто поднялся выше стартовой ступени.
    """
    ensure_real_aiosqlite(monkeypatch)
    from app.services.partner_stats_service import PartnerStatsService

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        for uid in range(2, 12):
            db.add(_user(uid, referred_by=1, topped_up=True))
        # Два начисления на стартовом ранге и восемь на втором.
        for uid in range(2, 4):
            db.add(
                ReferralEarning(
                    user_id=1,
                    referral_id=uid,
                    amount_kopeks=100_00,
                    reason='referral_level_topup',
                    level=1,
                    reward_type=ReferralRewardType.MONEY.value,
                )
            )
        for uid in range(4, 12):
            db.add(
                ReferralEarning(
                    user_id=1,
                    referral_id=uid,
                    amount_kopeks=900_00,
                    reason='referral_level_topup',
                    level=2,
                    reward_type=ReferralRewardType.MONEY.value,
                )
            )
        await db.commit()

        stats = await PartnerStatsService.get_referrer_detailed_stats(db, 1)
        average = stats['summary']['avg_earnings_per_referral_kopeks']

    # Весь доход 7400 ₽ на 10 оплативших рефералов. Срез по level=1 дал бы 20 ₽.
    assert average > 100_00, f'среднее {average} посчитано только по стартовому рангу'


@pytest.mark.asyncio
async def test_overview_average_counts_every_rank_too(monkeypatch, tier_mode):
    """Вторая точка того же среза по level==1 — в сводке по всем партнёрам.

    Первую закрыл test_average_income_counts_every_rank, а эта осталась: мутация
    «вернуть срез по level==1» здесь выживала.
    """
    ensure_real_aiosqlite(monkeypatch)
    from app.services.partner_stats_service import PartnerStatsService

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        for uid in range(2, 12):
            db.add(_user(uid, referred_by=1, topped_up=True))
        for uid in range(2, 4):
            db.add(
                ReferralEarning(
                    user_id=1,
                    referral_id=uid,
                    amount_kopeks=100_00,
                    reason='referral_level_topup',
                    level=1,
                    reward_type=ReferralRewardType.MONEY.value,
                )
            )
        for uid in range(4, 12):
            db.add(
                ReferralEarning(
                    user_id=1,
                    referral_id=uid,
                    amount_kopeks=900_00,
                    reason='referral_level_topup',
                    level=2,
                    reward_type=ReferralRewardType.MONEY.value,
                )
            )
        await db.commit()

        stats = await PartnerStatsService.get_global_partner_stats(db)
        average = stats['summary']['avg_earnings_per_referral_kopeks']

    assert average > 100_00, f'среднее {average} посчитано только по стартовому рангу'


@pytest.mark.asyncio
async def test_cap_applies_to_an_earning_made_in_the_same_second(monkeypatch, tier_mode):
    """Лимит обязан считать начисление, сделанное сразу после создания правила.

    SQLite хранит datetime строкой и сравнивает строки: у значения без долей
    секунды и у связанного aware-значения форматы расходятся, поэтому строка,
    созданная в ТУ ЖЕ секунду, что и правило, отбрасывалась отсечкой — лимит к
    ней не применялся, и партнёр получал выплату сверх настроенной. Типичный
    случай: админ завёл уровень, и тут же пришло пополнение.
    """
    ensure_real_aiosqlite(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1))
        for uid in range(2, 13):
            db.add(_user(uid, referred_by=1, topped_up=True))
        db.add(_level(2, referrer_percent=15, required_referrals=10, max_payments=1))
        await db.commit()

        # Первая выплата — в ту же секунду, что и правило.
        db.add(
            ReferralEarning(
                user_id=1,
                referral_id=2,
                amount_kopeks=100_00,
                reason='referral_level_topup',
                level=2,
                reward_type=ReferralRewardType.MONEY.value,
            )
        )
        await db.commit()

        referee = await db.get(User, 2)
        components = await build_reward_components(
            db, referee, event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=200_00
        )

        assert components == [], 'лимит в 1 выплату уже исчерпан — начислять нечего'


@pytest.mark.asyncio
async def test_margin_does_not_swallow_older_history(monkeypatch, tier_mode):
    """Контроль: запас на границе не должен втягивать историю прежних схем.

    Отсечка для того и существует: денежные строки классической схемы лежат в
    тех же колонках, и без границы новый уровень был бы исчерпан при рождении.
    Запас измеряется секундами, история — днями.
    """
    ensure_real_aiosqlite(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        recently = datetime.now(UTC) - timedelta(minutes=5)
        long_ago = datetime.now(UTC) - timedelta(days=30)

        db.add(_user(1))
        for uid in range(2, 13):
            db.add(_user(uid, referred_by=1, topped_up=True))
        db.add(_level(2, referrer_percent=15, required_referrals=10, max_payments=1, created_at=recently))
        for _ in range(5):
            db.add(
                ReferralEarning(
                    user_id=1,
                    referral_id=2,
                    amount_kopeks=100_00,
                    reason='referral_commission_topup',
                    level=2,
                    reward_type=ReferralRewardType.MONEY.value,
                    created_at=long_ago,
                )
            )
        await db.commit()

        referee = await db.get(User, 2)
        components = await build_reward_components(
            db, referee, event=RewardEvent.REPEAT_TOPUP, topup_amount_kopeks=200_00
        )

        assert components and components[0].money_kopeks == 30_00, 'старая история не должна съедать лимит'
