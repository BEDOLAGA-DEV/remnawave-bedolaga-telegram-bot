"""Счётчики сегментов промопредложений: SQL COUNT обязан совпадать с Python-фильтрами.

get_target_users_count считает получателей запросом, get_target_users отбирает их
в Python и именно по нему уходит рассылка. Кабинет показывает первое, а получает
второе — если ветки разъедутся, админ увидит охват, которого не будет.
"""

from datetime import UTC, datetime, timedelta

from app.database.models import (
    PromoGroup,
    Subscription,
    SubscriptionEvent,
    SubscriptionStatus,
    Tariff,
    User,
    UserStatus,
)
from app.handlers.admin.messages import get_target_users, get_target_users_count
from tests.fixtures.sqlite_memory import memory_session


PROMO_SEGMENTS = (
    'expired',
    'trial_ending',
    'autopay_failed',
    'low_balance',
    'inactive_30d',
    'inactive_60d',
    'inactive_90d',
)

SEGMENT_TABLES = (
    User.__table__,
    Subscription.__table__,
    SubscriptionEvent.__table__,
    Tariff.__table__,
    PromoGroup.__table__,
)


def _user(telegram_id: int, **kwargs) -> User:
    defaults = {
        'telegram_id': telegram_id,
        'username': f'user{telegram_id}',
        'first_name': f'User {telegram_id}',
        'status': UserStatus.ACTIVE.value,
        'balance_kopeks': 0,
        'last_activity': datetime.now(UTC),
    }
    defaults.update(kwargs)
    return User(**defaults)


def _subscription(user_id: int, *, status: str, end_date: datetime, is_trial: bool = False) -> Subscription:
    return Subscription(
        user_id=user_id,
        status=status,
        is_trial=is_trial,
        start_date=datetime.now(UTC) - timedelta(days=30),
        end_date=end_date,
        # Колонка unique + server_default='' — в тестовой БД проставляем сами
        remnawave_short_id=f'short{user_id}',
    )


async def _seed(db) -> None:
    now = datetime.now(UTC)

    paid = _user(1, balance_kopeks=50000)
    expired_low_balance = _user(2, balance_kopeks=5000, last_activity=now - timedelta(days=40))
    trial_ending = _user(3, balance_kopeks=20000)
    long_gone = _user(4, balance_kopeks=0, last_activity=now - timedelta(days=100))
    autopay_broken = _user(5, balance_kopeks=100)
    # Заблокированный пользователь не должен попадать ни в один сегмент
    blocked = _user(6, status=UserStatus.BLOCKED.value, balance_kopeks=5000, last_activity=now - timedelta(days=200))
    # Edge-case'ы сегмента expired, на которых SQL-ветка и Python-фильтр исторически расходились:
    # ACTIVE-статус с прошедшей датой — получатель (подписка фактически истекла)
    stale_active = _user(7)
    # LIMITED с будущей датой — НЕ получатель (не истёк, просто исчерпал трафик)
    limited_future = _user(8)
    # Платное прошлое без единой подписки — получатель
    paid_no_subs = _user(9, has_had_paid_subscription=True)

    db.add_all(
        [
            paid,
            expired_low_balance,
            trial_ending,
            long_gone,
            autopay_broken,
            blocked,
            stale_active,
            limited_future,
            paid_no_subs,
        ]
    )
    await db.commit()

    db.add_all(
        [
            _subscription(paid.id, status=SubscriptionStatus.ACTIVE.value, end_date=now + timedelta(days=10)),
            _subscription(
                expired_low_balance.id,
                status=SubscriptionStatus.EXPIRED.value,
                end_date=now - timedelta(days=5),
            ),
            _subscription(
                trial_ending.id,
                status=SubscriptionStatus.ACTIVE.value,
                end_date=now + timedelta(days=2),
                is_trial=True,
            ),
            _subscription(
                autopay_broken.id,
                status=SubscriptionStatus.ACTIVE.value,
                end_date=now + timedelta(days=20),
            ),
            _subscription(
                blocked.id,
                status=SubscriptionStatus.EXPIRED.value,
                end_date=now - timedelta(days=15),
            ),
            _subscription(
                stale_active.id,
                status=SubscriptionStatus.ACTIVE.value,
                end_date=now - timedelta(days=1),
            ),
            _subscription(
                limited_future.id,
                status=SubscriptionStatus.LIMITED.value,
                end_date=now + timedelta(days=10),
            ),
        ]
    )
    db.add_all(
        [
            SubscriptionEvent(
                user_id=autopay_broken.id,
                event_type='autopay_failed',
                occurred_at=now - timedelta(days=3),
            ),
            # Старая поломка автоплатежа вне недельного окна — в сегмент не берём
            SubscriptionEvent(
                user_id=paid.id,
                event_type='autopay_failed',
                occurred_at=now - timedelta(days=30),
            ),
        ]
    )
    await db.commit()


async def test_promo_segment_counts_match_recipient_lists(monkeypatch):
    """Для каждого промо-сегмента SQL-счётчик равен длине списка получателей."""
    async with memory_session(monkeypatch, SEGMENT_TABLES) as db:
        await _seed(db)

        for segment in PROMO_SEGMENTS:
            counted = await get_target_users_count(db, segment)
            recipients = await get_target_users(db, segment)
            assert counted == len(recipients), f'сегмент {segment}: счётчик {counted} != получателей {len(recipients)}'


async def test_promo_segment_counts_are_not_all_zero(monkeypatch):
    """Явные ожидания — паритет сам по себе прошёл бы и на двух одинаково пустых ветках."""
    async with memory_session(monkeypatch, SEGMENT_TABLES) as db:
        await _seed(db)

        # expired_low_balance + stale_active (ACTIVE с прошедшей датой) + paid_no_subs;
        # limited_future с будущей датой в сегмент не входит
        assert await get_target_users_count(db, 'expired') == 3
        assert await get_target_users_count(db, 'trial_ending') == 1
        assert await get_target_users_count(db, 'autopay_failed') == 1
        assert await get_target_users_count(db, 'low_balance') == 2
        assert await get_target_users_count(db, 'inactive_30d') == 2
        assert await get_target_users_count(db, 'inactive_60d') == 1
        assert await get_target_users_count(db, 'inactive_90d') == 1


async def test_expired_segment_edge_cases(monkeypatch):
    """Каждый расходившийся случай — в отдельной БД, чтобы ошибки не сокращались.

    На общем сиде недосчёт одного случая маскируется пересчётом другого (паритет
    длин совпадает), поэтому здесь каждый случай проверяется изолированно.
    """
    now = datetime.now(UTC)

    # ACTIVE-статус с прошедшей датой: подписка фактически истекла — получатель
    async with memory_session(monkeypatch, SEGMENT_TABLES) as db:
        user = _user(1)
        db.add(user)
        await db.commit()
        db.add(_subscription(user.id, status=SubscriptionStatus.ACTIVE.value, end_date=now - timedelta(days=1)))
        await db.commit()
        assert await get_target_users_count(db, 'expired') == 1
        assert len(await get_target_users(db, 'expired')) == 1

    # LIMITED с будущей датой: не истёк, просто исчерпал трафик — НЕ получатель
    async with memory_session(monkeypatch, SEGMENT_TABLES) as db:
        user = _user(1)
        db.add(user)
        await db.commit()
        db.add(_subscription(user.id, status=SubscriptionStatus.LIMITED.value, end_date=now + timedelta(days=10)))
        await db.commit()
        assert await get_target_users_count(db, 'expired') == 0
        assert len(await get_target_users(db, 'expired')) == 0

    # Платное прошлое без единой подписки — получатель
    async with memory_session(monkeypatch, SEGMENT_TABLES) as db:
        user = _user(1, has_had_paid_subscription=True)
        db.add(user)
        await db.commit()
        assert await get_target_users_count(db, 'expired') == 1
        assert len(await get_target_users(db, 'expired')) == 1


async def test_unknown_segment_counts_zero(monkeypatch):
    """Неизвестный ключ не роняет запрос и не показывает мусорный охват."""
    async with memory_session(monkeypatch, SEGMENT_TABLES) as db:
        await _seed(db)

        assert await get_target_users_count(db, 'definitely_not_a_segment') == 0
