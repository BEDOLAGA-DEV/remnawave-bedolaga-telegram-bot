"""Выдача дней подписки за реферала — на настоящих подписках в базе.

До этого файла у ``grant_reward_days`` не было ни одного поведенческого теста:
проверялось лишь, что вызывающий код её дёргает. Между тем именно здесь награда
превращается во что-то, что видит пользователь, и здесь же живут два дорогих
класса ошибок — дни, ушедшие не в ту подписку, и триал, случайно ставший платным.

Работа идёт на SQLite в памяти: подмены здесь бесполезны, вопрос ровно в том, что
окажется в строках после вызова.
"""

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.database.models import Base, Subscription, SubscriptionStatus, Tariff, User
from app.services import referral_reward_service as engine
from tests.fixtures.sqlite_memory import memory_session


# Всё дерево таблиц целиком: extend_subscription попутно чистит пакеты трафика,
# гасит уведомления и трогает автоплатёж, а create_paid_subscription — сквады и
# промогруппы. Перечислять их поимённо значит ловить «no such table» по одной на
# каждый вызов; с зарегистрированным компилятором JSONB создаётся вся схема.
TABLES = list(Base.metadata.sorted_tables)

PRO_TARIFF_ID = 1
OTHER_TARIFF_ID = 2


@pytest.fixture(autouse=True)
def no_panel_sync(monkeypatch):
    """Remnawave в тестах не поднимается — синхронизация подменяется."""

    async def noop(self, db, subscription, **kwargs):
        return None

    monkeypatch.setattr('app.services.subscription_service.SubscriptionService.update_remnawave_user', noop)


def _tariff(tariff_id: int, name: str) -> Tariff:
    return Tariff(
        id=tariff_id,
        name=name,
        description='',
        is_active=True,
        traffic_limit_gb=100,
        device_limit=3,
        allowed_squads=[],
        display_order=tariff_id,
    )


def _user(user_id: int) -> User:
    return User(
        id=user_id,
        telegram_id=1000 + user_id,
        first_name=f'User {user_id}',
        language='ru',
        status='active',
        balance_kopeks=0,
    )


_short_ids = itertools.count(1)


def _subscription(user_id: int, *, tariff_id: int | None, is_trial: bool = False, days_left: int = 10) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        # remnawave_short_id уникален; в проде его выдаёт generate_unique_short_id.
        remnawave_short_id=f'test{next(_short_ids)}',
        user_id=user_id,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=is_trial,
        start_date=now,
        end_date=now + timedelta(days=days_left),
        traffic_limit_gb=100,
        device_limit=3,
        tariff_id=tariff_id,
        connected_squads=[],
    )


async def _seed(db, subscriptions):
    db.add(_user(1))
    db.add_all([_tariff(PRO_TARIFF_ID, 'Pro'), _tariff(OTHER_TARIFF_ID, 'Lite')])
    db.add_all(subscriptions)
    await db.commit()


async def _reload(db, subscription_id):
    from sqlalchemy import select

    result = await db.execute(select(Subscription).where(Subscription.id == subscription_id))
    return result.scalar_one()


class TestDaysLandWhereConfigured:
    @pytest.mark.asyncio
    async def test_extends_the_subscription_of_the_named_tariff(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            target = _subscription(1, tariff_id=PRO_TARIFF_ID)
            other = _subscription(1, tariff_id=OTHER_TARIFF_ID)
            await _seed(db, [target, other])
            before_target = target.end_date
            before_other = other.end_date

            grant = await engine.grant_reward_days(db, await db.get(User, 1), 7, PRO_TARIFF_ID)

            assert grant.days == 7
            assert grant.tariff_name == 'Pro'
            assert (await _reload(db, target.id)).end_date == before_target + timedelta(days=7)
            assert (await _reload(db, other.id)).end_date == before_other, 'чужая подписка не должна двигаться'

    @pytest.mark.asyncio
    async def test_without_tariff_extends_the_primary_subscription(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            primary = _subscription(1, tariff_id=PRO_TARIFF_ID)
            await _seed(db, [primary])
            before = primary.end_date

            grant = await engine.grant_reward_days(db, await db.get(User, 1), 5, None)

            assert grant.days == 5
            assert (await _reload(db, primary.id)).end_date == before + timedelta(days=5)

    @pytest.mark.asyncio
    async def test_zero_days_is_a_no_op(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            primary = _subscription(1, tariff_id=PRO_TARIFF_ID)
            await _seed(db, [primary])
            before = primary.end_date

            grant = await engine.grant_reward_days(db, await db.get(User, 1), 0, PRO_TARIFF_ID)

            assert grant.days == 0
            assert (await _reload(db, primary.id)).end_date == before


class TestMissingSubscription:
    @pytest.mark.asyncio
    async def test_no_subscription_and_no_tariff_grants_nothing(self, monkeypatch):
        """Продлевать нечего, а создать подписку не из чего — параметры взять неоткуда."""
        async with memory_session(monkeypatch, TABLES) as db:
            await _seed(db, [])

            grant = await engine.grant_reward_days(db, await db.get(User, 1), 7, None)

            assert grant.days == 0
            assert grant.failure == 'no_subscription'

    @pytest.mark.asyncio
    async def test_no_subscription_with_tariff_creates_one(self, monkeypatch):
        """Тариф в правиле и есть ответ на вопрос «куда попадут дни»."""
        async with memory_session(monkeypatch, TABLES) as db:
            await _seed(db, [])

            grant = await engine.grant_reward_days(db, await db.get(User, 1), 7, PRO_TARIFF_ID)

            assert grant.days == 7
            created = await _reload(db, grant.subscription_id)
            assert created.tariff_id == PRO_TARIFF_ID
            assert created.is_trial is False, 'бесплатная награда не должна создавать триал'
            assert created.traffic_limit_gb == 100, 'лимиты берутся из тарифа'
            assert created.device_limit == 3
            # Срок ровно на выданные дни, а не на период тарифа.
            assert (created.end_date - datetime.now(UTC)).days == 6

    @pytest.mark.asyncio
    async def test_unknown_tariff_grants_nothing(self, monkeypatch):
        """Тариф-призрак означал бы дни, которым некуда лечь."""
        async with memory_session(monkeypatch, TABLES) as db:
            await _seed(db, [])

            grant = await engine.grant_reward_days(db, await db.get(User, 1), 7, 999)

            assert grant.days == 0
            assert grant.failure == 'no_subscription'


class TestTrialIsNeverConverted:
    """Награда за реферала — не покупка.

    Снятие триального флага без оплаты выключает подписку из авто-продления и
    превращает её в фантомную платную (класс бага #629889). Проверяется на обоих
    путях: продление существующего триала и «нет подписки нужного тарифа».
    """

    @pytest.mark.asyncio
    async def test_extending_a_trial_keeps_it_a_trial(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            trial = _subscription(1, tariff_id=PRO_TARIFF_ID, is_trial=True)
            await _seed(db, [trial])
            before = trial.end_date

            grant = await engine.grant_reward_days(db, await db.get(User, 1), 7, PRO_TARIFF_ID)

            assert grant.days == 7
            reloaded = await _reload(db, trial.id)
            assert reloaded.is_trial is True, 'триал обязан остаться триалом'
            assert reloaded.tariff_id == PRO_TARIFF_ID
            assert reloaded.end_date == before + timedelta(days=7)

    @pytest.mark.asyncio
    async def test_alive_trial_of_another_tariff_is_not_converted(self, monkeypatch):
        """Подписки нужного тарифа нет, но живой триал есть — создавать нельзя.

        ``create_paid_subscription`` умеет конвертировать живой триал в платную
        подписку. Вызвать его здесь означало бы бесплатно снять с человека
        триальный статус.
        """
        async with memory_session(monkeypatch, TABLES) as db:
            trial = _subscription(1, tariff_id=OTHER_TARIFF_ID, is_trial=True)
            await _seed(db, [trial])

            await engine.grant_reward_days(db, await db.get(User, 1), 7, PRO_TARIFF_ID)

            reloaded = await _reload(db, trial.id)
            assert reloaded.is_trial is True, 'живой триал не должен стать платной подпиской'
            assert reloaded.tariff_id == OTHER_TARIFF_ID


class TestPanelSyncFailure:
    @pytest.mark.asyncio
    async def test_days_survive_a_panel_error(self, monkeypatch):
        """Расхождение с панелью чинится следующей синхронизацией, потеря дней — нет."""
        async with memory_session(monkeypatch, TABLES) as db:
            target = _subscription(1, tariff_id=PRO_TARIFF_ID)
            await _seed(db, [target])
            before = target.end_date

            async def boom(self, db_, subscription, **kwargs):
                raise RuntimeError('panel is down')

            monkeypatch.setattr('app.services.subscription_service.SubscriptionService.update_remnawave_user', boom)

            grant = await engine.grant_reward_days(db, await db.get(User, 1), 7, PRO_TARIFF_ID)

            assert grant.days == 7, 'сбой панели не отменяет выданные дни'
            assert (await _reload(db, target.id)).end_date == before + timedelta(days=7)


class TestSubscriptionOnAnotherTariff:
    """Подписка есть, но не того тарифа, который задан в правиле.

    Прежнее условие «нет ни одной подписки» было шире реальной опасности: человек
    с платной подпиской на другом тарифе молча не получал настроенные админом дни.
    Опасность — только живой триал: его ``create_paid_subscription`` конвертирует
    в платную подписку, бесплатно сняв триальный статус.
    """

    @pytest.mark.asyncio
    async def test_paid_subscription_elsewhere_does_not_block_the_reward(self, monkeypatch):
        monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)

        async with memory_session(monkeypatch, TABLES) as db:
            other = _subscription(1, tariff_id=OTHER_TARIFF_ID)
            await _seed(db, [other])

            grant = await engine.grant_reward_days(db, await db.get(User, 1), 7, PRO_TARIFF_ID)

            assert grant.days == 7, 'дни настроенного тарифа не должны теряться'
            created = await _reload(db, grant.subscription_id)
            assert created.tariff_id == PRO_TARIFF_ID
            assert created.id != other.id
            assert created.is_trial is False

    @pytest.mark.asyncio
    async def test_alive_trial_elsewhere_still_blocks_creation(self, monkeypatch):
        monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)

        async with memory_session(monkeypatch, TABLES) as db:
            trial = _subscription(1, tariff_id=OTHER_TARIFF_ID, is_trial=True)
            await _seed(db, [trial])

            grant = await engine.grant_reward_days(db, await db.get(User, 1), 7, PRO_TARIFF_ID)

            assert grant.days == 0
            assert (await _reload(db, trial.id)).is_trial is True

    @pytest.mark.asyncio
    async def test_classic_mode_does_not_create_a_second_subscription(self, monkeypatch):
        """Вне мультитарифа подписка одна — вторая сломала бы инварианты режима."""
        monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)

        async with memory_session(monkeypatch, TABLES) as db:
            other = _subscription(1, tariff_id=OTHER_TARIFF_ID)
            await _seed(db, [other])

            grant = await engine.grant_reward_days(db, await db.get(User, 1), 7, PRO_TARIFF_ID)

            assert grant.days == 0
            assert grant.failure == 'no_subscription'


class TestStatesWhereDaysMustNotLand:
    """Не всякая найденная подписка годится для награды."""

    @pytest.mark.asyncio
    async def test_pending_draft_never_receives_days(self, monkeypatch):
        """Активация черновика перепишет срок — выданные дни исчезнут.

        Хуже потери самих дней то, что в ledger'е останется запись о доставленной
        награде: пользователь числится получившим то, чего у него нет.
        """
        async with memory_session(monkeypatch, TABLES) as db:
            draft = _subscription(1, tariff_id=PRO_TARIFF_ID)
            draft.status = SubscriptionStatus.PENDING.value
            await _seed(db, [draft])
            before = draft.end_date

            grant = await engine.grant_reward_days(db, await db.get(User, 1), 7, None)

            assert grant.days == 0
            assert grant.failure == 'pending_draft'
            assert (await _reload(db, draft.id)).end_date == before

    @pytest.mark.asyncio
    async def test_pending_draft_of_the_tariff_blocks_creating_a_duplicate(self, monkeypatch):
        """Вторая подписка того же тарифа сломает пользователю оплату черновика."""
        monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)

        async with memory_session(monkeypatch, TABLES) as db:
            draft = _subscription(1, tariff_id=PRO_TARIFF_ID)
            draft.status = SubscriptionStatus.PENDING.value
            await _seed(db, [draft])

            grant = await engine.grant_reward_days(db, await db.get(User, 1), 7, PRO_TARIFF_ID)

            assert grant.days == 0
            from sqlalchemy import func, select

            count = await db.execute(select(func.count(Subscription.id)).where(Subscription.tariff_id == PRO_TARIFF_ID))
            assert count.scalar() == 1, 'дубликат подписки того же тарифа создавать нельзя'

    @pytest.mark.asyncio
    async def test_disabled_subscription_is_not_resurrected(self, monkeypatch):
        """extend_subscription поднимает DISABLED в ACTIVE.

        Подписку отключили осознанно — бесплатная награда не должна отменять
        чужое решение и возвращать доступ.
        """
        async with memory_session(monkeypatch, TABLES) as db:
            disabled = _subscription(1, tariff_id=PRO_TARIFF_ID)
            disabled.status = SubscriptionStatus.DISABLED.value
            await _seed(db, [disabled])
            before = disabled.end_date

            grant = await engine.grant_reward_days(db, await db.get(User, 1), 7, PRO_TARIFF_ID)

            assert grant.days == 0
            assert grant.failure == 'subscription_disabled'
            reloaded = await _reload(db, disabled.id)
            assert reloaded.status == SubscriptionStatus.DISABLED.value
            assert reloaded.end_date == before

    @pytest.mark.asyncio
    async def test_expired_subscription_is_still_revived(self, monkeypatch):
        """Истёкшая — обычный случай «подписка кончилась, вот дни»; её оживляем."""
        async with memory_session(monkeypatch, TABLES) as db:
            expired = _subscription(1, tariff_id=PRO_TARIFF_ID, days_left=-5)
            expired.status = SubscriptionStatus.EXPIRED.value
            await _seed(db, [expired])

            grant = await engine.grant_reward_days(db, await db.get(User, 1), 7, PRO_TARIFF_ID)

            assert grant.days == 7
            assert (await _reload(db, expired.id)).status == SubscriptionStatus.ACTIVE.value


class TestManySubscriptionsInMultiTariff:
    """Мультитариф: у человека несколько подписок и тариф в правиле не задан.

    Штатная выборка основной подписки сортирует по СТАТУСУ и не смотрит на
    ``is_trial``, поэтому награда могла уйти в триал при живой оплаченной
    подписке. Здесь закреплён явный выбор.
    """

    @pytest.fixture(autouse=True)
    def multi_tariff(self, monkeypatch):
        monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)

    @pytest.mark.asyncio
    async def test_paid_wins_over_trial(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            # Триал заканчивается позже — по сроку он «выиграл бы» у платной.
            trial = _subscription(1, tariff_id=OTHER_TARIFF_ID, is_trial=True, days_left=90)
            paid = _subscription(1, tariff_id=PRO_TARIFF_ID, days_left=10)
            await _seed(db, [trial, paid])
            paid_before = paid.end_date
            trial_before = trial.end_date

            grant = await engine.grant_reward_days(db, await db.get(User, 1), 7, None)

            assert grant.days == 7
            assert grant.subscription_id == paid.id, 'дни обязаны идти в оплаченную подписку'
            assert (await _reload(db, paid.id)).end_date == paid_before + timedelta(days=7)
            assert (await _reload(db, trial.id)).end_date == trial_before

    @pytest.mark.asyncio
    async def test_choice_among_many_paid_is_deterministic(self, monkeypatch):
        """При нескольких платных выбирается одна и та же — иначе дни не найти."""
        async with memory_session(monkeypatch, TABLES) as db:
            subs = [
                _subscription(1, tariff_id=PRO_TARIFF_ID, days_left=5),
                _subscription(1, tariff_id=OTHER_TARIFF_ID, days_left=40),
                _subscription(1, tariff_id=None, days_left=20),
            ]
            await _seed(db, subs)
            longest = subs[1]

            first = await engine.grant_reward_days(db, await db.get(User, 1), 3, None)
            second = await engine.grant_reward_days(db, await db.get(User, 1), 3, None)

            assert first.subscription_id == longest.id, 'выбирается подписка с самым поздним сроком'
            assert second.subscription_id == first.subscription_id, 'выбор обязан быть повторяемым'

    @pytest.mark.asyncio
    async def test_trial_only_user_still_gets_the_reward(self, monkeypatch):
        """Владелец одного лишь триала не должен остаться без награды."""
        async with memory_session(monkeypatch, TABLES) as db:
            trial = _subscription(1, tariff_id=PRO_TARIFF_ID, is_trial=True)
            await _seed(db, [trial])
            before = trial.end_date

            grant = await engine.grant_reward_days(db, await db.get(User, 1), 7, None)

            assert grant.days == 7
            reloaded = await _reload(db, trial.id)
            assert reloaded.end_date == before + timedelta(days=7)
            assert reloaded.is_trial is True

    @pytest.mark.asyncio
    async def test_blocked_subscriptions_are_not_candidates(self, monkeypatch):
        """Отключённая и неоплаченный черновик не должны перехватывать награду."""
        async with memory_session(monkeypatch, TABLES) as db:
            disabled = _subscription(1, tariff_id=OTHER_TARIFF_ID, days_left=90)
            disabled.status = SubscriptionStatus.DISABLED.value
            draft = _subscription(1, tariff_id=None, days_left=60)
            draft.status = SubscriptionStatus.PENDING.value
            paid = _subscription(1, tariff_id=PRO_TARIFF_ID, days_left=10)
            await _seed(db, [disabled, draft, paid])

            grant = await engine.grant_reward_days(db, await db.get(User, 1), 7, None)

            assert grant.subscription_id == paid.id


class TestClassicMode:
    """Классический режим: подписка одна, тарифов у неё нет."""

    @pytest.fixture(autouse=True)
    def classic(self, monkeypatch):
        monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)

    @pytest.mark.asyncio
    async def test_days_extend_the_single_subscription(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            only = _subscription(1, tariff_id=None)
            await _seed(db, [only])
            before = only.end_date

            grant = await engine.grant_reward_days(db, await db.get(User, 1), 7, None)

            assert grant.days == 7
            assert grant.subscription_id == only.id
            assert (await _reload(db, only.id)).end_date == before + timedelta(days=7)

    @pytest.mark.asyncio
    async def test_tariff_configured_in_classic_mode_grants_nothing(self, monkeypatch):
        """В классическом режиме у подписок нет тарифа — правило с тарифом не сработает.

        Молчание здесь не «баг движка», а следствие несовместимой настройки, и
        админку надо предупреждать об этом на экране уровня.
        """
        async with memory_session(monkeypatch, TABLES) as db:
            only = _subscription(1, tariff_id=None)
            await _seed(db, [only])
            before = only.end_date

            grant = await engine.grant_reward_days(db, await db.get(User, 1), 7, PRO_TARIFF_ID)

            assert grant.days == 0
            assert (await _reload(db, only.id)).end_date == before
