"""Пользователь сам выбирает подписку для дней награды.

Награда приходит асинхронно, на чужом пополнении, и подписку подбирал бот —
платную с самым поздним сроком. При нескольких подписках это угадывание: человек
хотел продлить другую. Здесь проверяется его собственный выбор и, главное, что
негодный выбор не уводит награду в чужую подписку и не отменяет её вовсе.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.database.models import Base, Subscription, SubscriptionStatus, Tariff, User
from app.services.referral_reward_service import _resolve_days_target, grant_reward_days
from tests.fixtures.sqlite_memory import ensure_real_aiosqlite, memory_session


TABLES = list(Base.metadata.sorted_tables)


def _user(uid: int, *, chosen: int | None = None) -> User:
    return User(
        id=uid,
        telegram_id=1000 + uid,
        first_name=f'U{uid}',
        language='ru',
        balance_kopeks=0,
        referral_days_subscription_id=chosen,
    )


def _sub(sub_id: int, user_id: int, *, days: int, trial: bool = False, tariff_id: int | None = None) -> Subscription:
    return Subscription(
        id=sub_id,
        user_id=user_id,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=trial,
        start_date=datetime.now(UTC) - timedelta(days=1),
        end_date=datetime.now(UTC) + timedelta(days=days),
        tariff_id=tariff_id,
        traffic_limit_gb=0,
        device_limit=1,
        # У колонки уникальный индекс и генератор по умолчанию: без явного
        # значения две подписки в одном тесте получают один идентификатор.
        remnawave_short_id=f'short-{sub_id}',
    )


@pytest.fixture
def choice_on(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
    monkeypatch.setattr(settings, 'REFERRAL_ALLOW_DAYS_TARGET_CHOICE', True)


@pytest.mark.asyncio
async def test_chosen_subscription_wins_over_the_automatic_pick(monkeypatch, choice_on):
    """Автоподбор взял бы подписку с самым поздним сроком — выбор важнее."""
    ensure_real_aiosqlite(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1, chosen=10))
        db.add_all([_sub(10, 1, days=5), _sub(11, 1, days=90)])
        await db.commit()

        target, blocked = await _resolve_days_target(db, await db.get(User, 1), None)

        assert blocked is None
        assert target.id == 10, 'дни должны лечь в выбранную подписку, а не в самую долгую'


@pytest.mark.asyncio
async def test_without_the_setting_the_choice_is_ignored(monkeypatch, choice_on):
    """Выключенная настройка возвращает прежний автоподбор — целиком."""
    ensure_real_aiosqlite(monkeypatch)
    monkeypatch.setattr(settings, 'REFERRAL_ALLOW_DAYS_TARGET_CHOICE', False)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1, chosen=10))
        db.add_all([_sub(10, 1, days=5), _sub(11, 1, days=90)])
        await db.commit()

        target, _blocked = await _resolve_days_target(db, await db.get(User, 1), None)

        assert target.id == 11


@pytest.mark.asyncio
async def test_a_foreign_subscription_is_never_used(monkeypatch, choice_on):
    """Ссылка живёт в строке пользователя и переживает что угодно.

    Отдать награду в чужую подписку из-за устаревшей или подделанной ссылки
    нельзя — принадлежность проверяется запросом, а не доверием.
    """
    ensure_real_aiosqlite(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add_all([_user(1, chosen=20), _user(2)])
        db.add_all([_sub(10, 1, days=5), _sub(20, 2, days=90)])
        await db.commit()

        target, _blocked = await _resolve_days_target(db, await db.get(User, 1), None)

        assert target.id == 10, 'выбор указывает на чужую подписку — должен игнорироваться'


@pytest.mark.asyncio
async def test_a_stale_choice_falls_back_instead_of_refusing(monkeypatch, choice_on):
    """Удалённая подписка не должна отменять награду.

    Человек не обязан замечать, что подписки, которую он когда-то выбрал, больше
    нет; отказ вместо автоподбора выглядел бы как молча пропавшая награда.
    """
    ensure_real_aiosqlite(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1, chosen=999))
        db.add(_sub(10, 1, days=5))
        await db.commit()

        target, blocked = await _resolve_days_target(db, await db.get(User, 1), None)

        assert blocked is None
        assert target.id == 10


@pytest.mark.asyncio
async def test_the_rule_tariff_still_wins_over_the_choice(monkeypatch, choice_on):
    """Тариф в правиле — указание админа, куда дни обязаны лечь.

    Перебивать его пользовательским выбором значило бы менять само правило.
    """
    ensure_real_aiosqlite(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(Tariff(id=7, name='Про', is_active=True))
        db.add(_user(1, chosen=10))
        db.add_all([_sub(10, 1, days=5), _sub(11, 1, days=90, tariff_id=7)])
        await db.commit()

        target, _blocked = await _resolve_days_target(db, await db.get(User, 1), 7)

        assert target.id == 11


@pytest.mark.asyncio
async def test_days_actually_land_in_the_chosen_subscription(monkeypatch, choice_on):
    """Сквозная проверка: не только выбор цели, но и само продление."""
    ensure_real_aiosqlite(monkeypatch)
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(1, chosen=10))
        db.add_all([_sub(10, 1, days=5), _sub(11, 1, days=90)])
        await db.commit()

        before = (await db.get(Subscription, 10)).end_date
        grant = await grant_reward_days(db, await db.get(User, 1), 14, None)
        await db.commit()

        assert grant.days == 14
        assert grant.subscription_id == 10
        after = (await db.get(Subscription, 10)).end_date
        assert (after - before).days == 14
        # Соседняя подписка не тронута.
        assert (await db.get(Subscription, 11)).end_date.date() == (datetime.now(UTC) + timedelta(days=90)).date()
