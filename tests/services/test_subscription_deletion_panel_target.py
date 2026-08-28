"""Какой панельный аккаунт уходит вместе с удалённой подпиской.

Адресация аккаунта в двух режимах устроена по-разному, и удаление подписки —
единственное место, где ошибка адресации не откатывается: либо у человека
остаётся живой VPN после того, как админ снял доступ, либо наоборот умирает
доступ по соседней, ещё действующей подписке.

* мультитариф — аккаунт у каждой подписки свой (``subscriptions.remnawave_id``);
* однотарифный — аккаунт общий на пользователя (``users.remnawave_id``), а
  колонка подписки заполнена максимум у одной строки: индекс
  ``uq_subscriptions_remnawave_id`` частично уникален, поэтому второй строке
  id не пишется (см. subscription_service), а синхронизация кабинета по
  email/OAuth создаёт строку с ``remnawave_id=None`` намеренно.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import (
    DiscountOffer,
    LavaSubscription,
    PlategaSubscription,
    SentNotification,
    Subscription,
    SubscriptionEvent,
    SubscriptionServer,
    SubscriptionStatus,
    SubscriptionTemporaryAccess,
    TrafficPurchase,
    User,
    UserStatus,
)
from app.services import subscription_deletion_service as deletion
from tests.fixtures.sqlite_memory import memory_session


# db.delete() подтягивает все связи подписки — без их таблиц падает на SELECT.
TABLES = (
    User.__table__,
    Subscription.__table__,
    DiscountOffer.__table__,
    SubscriptionTemporaryAccess.__table__,
    TrafficPurchase.__table__,
    PlategaSubscription.__table__,
    LavaSubscription.__table__,
    SentNotification.__table__,
    SubscriptionEvent.__table__,
    SubscriptionServer.__table__,
)

PANEL_ID = 4242


def _user(*, remnawave_id: int | None) -> User:
    return User(
        id=10,
        telegram_id=10,
        username='client',
        status=UserStatus.ACTIVE.value,
        remnawave_id=remnawave_id,
    )


def _sub(sub_id: int, *, status: str, remnawave_id: int | None = None) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        id=sub_id,
        user_id=10,
        status=status,
        is_trial=False,
        remnawave_id=remnawave_id,
        # колонка nullable=False + unique, дефолт '' — двум строкам нужны разные
        remnawave_short_id=f'short{sub_id}',
        start_date=now - timedelta(days=30),
        end_date=now + timedelta(days=30),
    )


@pytest.mark.asyncio
async def test_multi_tariff_takes_the_subscriptions_own_account(monkeypatch):
    """Мультитариф: у подписки свой аккаунт, его и удаляем."""
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(remnawave_id=999))
        target = _sub(1, status=SubscriptionStatus.EXPIRED.value, remnawave_id=PANEL_ID)
        db.add(target)
        await db.commit()

        assert await deletion._resolve_panel_target(db, target) == (PANEL_ID, True)


@pytest.mark.asyncio
async def test_single_tariff_falls_back_to_the_user_account(monkeypatch):
    """Однотарифный: колонка подписки пуста, но аккаунт есть — и его надо снять.

    Без этого админ жмёт «удалить», строка исчезает, а VPN у человека
    продолжает работать до конца оплаченного периода.
    """
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(remnawave_id=PANEL_ID))
        target = _sub(1, status=SubscriptionStatus.ACTIVE.value, remnawave_id=None)
        db.add(target)
        await db.commit()

        panel_user_id, deletable = await deletion._resolve_panel_target(db, target)

    assert panel_user_id == PANEL_ID
    # Аккаунт общий на пользователя — отключаем, но не сносим.
    assert deletable is False


@pytest.mark.asyncio
async def test_single_tariff_spares_account_of_a_live_sibling(monkeypatch):
    """Однотарифный: живая соседка сидит на том же аккаунте — не трогаем его.

    Иначе уборка отработавшей строки убивает действующий доступ.
    """
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(remnawave_id=PANEL_ID))
        target = _sub(1, status=SubscriptionStatus.EXPIRED.value, remnawave_id=PANEL_ID)
        db.add(target)
        db.add(_sub(2, status=SubscriptionStatus.ACTIVE.value, remnawave_id=None))
        await db.commit()

        assert await deletion._resolve_panel_target(db, target) == (None, False)


@pytest.mark.asyncio
async def test_single_tariff_ignores_dead_sibling(monkeypatch):
    """Мёртвая соседка ничего не держит — аккаунт всё равно отключаем."""
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(remnawave_id=PANEL_ID))
        target = _sub(1, status=SubscriptionStatus.ACTIVE.value, remnawave_id=None)
        db.add(target)
        db.add(_sub(2, status=SubscriptionStatus.EXPIRED.value, remnawave_id=PANEL_ID))
        await db.commit()

        assert await deletion._resolve_panel_target(db, target) == (PANEL_ID, False)


@pytest.mark.asyncio
async def test_single_tariff_uses_subscription_id_when_user_has_none(monkeypatch):
    """Историческая строка: id остался только на подписке — работаем по нему."""
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(remnawave_id=None))
        target = _sub(1, status=SubscriptionStatus.EXPIRED.value, remnawave_id=PANEL_ID)
        db.add(target)
        await db.commit()

        assert await deletion._resolve_panel_target(db, target) == (PANEL_ID, True)


class _PanelSpy:
    """Счётчик того, что реально ушло в панель."""

    def __init__(self) -> None:
        self.deleted: list[int] = []
        self.disabled: list[int] = []
        self.marked: list[int] = []
        self.decremented: list[int] = []
        self.order: list[str] = []

    def install(self, monkeypatch) -> None:
        from app.services.remnawave_webhook_service import RemnaWaveWebhookService
        from app.services.subscription_service import SubscriptionService

        async def fake_delete(_self, panel_user_id):
            self.deleted.append(panel_user_id)
            self.order.append('panel_delete')
            return True

        async def fake_disable(_self, panel_user_id, db=None):
            self.disabled.append(panel_user_id)
            self.order.append('panel_disable')
            return True

        monkeypatch.setattr(SubscriptionService, 'delete_remnawave_user', fake_delete)
        monkeypatch.setattr(SubscriptionService, 'disable_remnawave_user', fake_disable)
        monkeypatch.setattr(
            RemnaWaveWebhookService,
            'mark_intentional_panel_deletion',
            staticmethod(lambda panel_user_ids: self.marked.extend(panel_user_ids)),
        )

        def _record(label):
            async def recorder(*args, **kwargs):
                self.order.append(label)

            return recorder

        async def fake_decrement(_db, subscription):
            self.decremented.append(subscription.id)
            self.order.append('decrement')

        monkeypatch.setattr(
            'app.services.grace_access_runtime.ensure_no_open_grace_for_subscriptions', _record('grace')
        )
        monkeypatch.setattr(
            'app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', _record('platega')
        )
        monkeypatch.setattr('app.services.payment.lava.cancel_lava_recurring_for_subscription_safe', _record('lava'))
        monkeypatch.setattr(deletion, 'decrement_subscription_server_counts', fake_decrement)


@pytest.mark.asyncio
async def test_single_tariff_deletion_disables_shared_account(monkeypatch):
    """Однотарифный, соседок нет: доступ снят, аккаунт остался живым для новой покупки."""
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    spy = _PanelSpy()
    spy.install(monkeypatch)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(remnawave_id=PANEL_ID))
        target = _sub(1, status=SubscriptionStatus.ACTIVE.value, remnawave_id=None)
        db.add(target)
        await db.commit()

        await deletion.delete_subscription_record(db, target, deleted_by='admin:1')

        remaining = await db.scalar(select(Subscription.id).where(Subscription.id == 1))

    assert spy.disabled == [PANEL_ID]
    assert spy.deleted == []
    assert spy.marked == []
    assert remaining is None
    # Счётчики серверов не должны остаться завышенными после удаления строки
    assert spy.decremented == [1]


@pytest.mark.asyncio
async def test_single_tariff_deletion_leaves_live_sibling_alone(monkeypatch):
    """Уборка отработавшей строки не должна трогать аккаунт живой соседки."""
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    spy = _PanelSpy()
    spy.install(monkeypatch)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(remnawave_id=PANEL_ID))
        target = _sub(1, status=SubscriptionStatus.EXPIRED.value, remnawave_id=PANEL_ID)
        db.add(target)
        db.add(_sub(2, status=SubscriptionStatus.ACTIVE.value, remnawave_id=None))
        await db.commit()

        await deletion.delete_subscription_record(db, target, deleted_by='admin:1')

    assert spy.deleted == []
    assert spy.disabled == []


@pytest.mark.asyncio
async def test_multi_tariff_deletion_deletes_own_account(monkeypatch):
    """Мультитариф: аккаунт подписки удаляется и помечается намеренным."""
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    spy = _PanelSpy()
    spy.install(monkeypatch)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(remnawave_id=None))
        target = _sub(1, status=SubscriptionStatus.EXPIRED.value, remnawave_id=PANEL_ID)
        db.add(target)
        await db.commit()

        await deletion.delete_subscription_record(db, target, deleted_by='user')

    assert spy.deleted == [PANEL_ID]
    assert spy.disabled == []
    # без пометки прилетевший user.deleted снёс бы соседние живые подписки
    assert spy.marked == [PANEL_ID]


@pytest.mark.asyncio
async def test_open_grace_aborts_before_anything_irreversible(monkeypatch):
    """Грейс-гард обязан пробрасываться наружу, а не глохнуть внутри сервиса.

    Проглоченное исключение означало бы удаление подписки из-под открытого
    временного доступа — ровно то, ради чего гард и стоит.
    """
    from app.services.grace_access_runtime import GraceAccessDeletionBlocked

    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    spy = _PanelSpy()
    spy.install(monkeypatch)

    async def blocked(_db, subscription_ids):
        raise GraceAccessDeletionBlocked(tuple(subscription_ids))

    monkeypatch.setattr('app.services.grace_access_runtime.ensure_no_open_grace_for_subscriptions', blocked)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(remnawave_id=None))
        target = _sub(1, status=SubscriptionStatus.EXPIRED.value, remnawave_id=PANEL_ID)
        db.add(target)
        await db.commit()

        with pytest.raises(GraceAccessDeletionBlocked):
            await deletion.delete_subscription_record(db, target, deleted_by='admin:1')

        survived = await db.scalar(select(Subscription.id).where(Subscription.id == 1))

    assert survived == 1
    assert spy.deleted == []
    assert spy.disabled == []
    # Ни один шаг после гарда не начался: иначе автоплатёж оказался бы снят
    # у подписки, которую в итоге так и не удалили.
    assert spy.order == []


@pytest.mark.asyncio
async def test_step_order_is_pinned(monkeypatch):
    """Порядок шагов удаления — не косметика, каждый стоит там не случайно.

    Гард идёт ПЕРВЫМ: если отмена автоплатежей случится раньше проверки, у
    заблокированной грейсом подписки автоплатёж уже будет снят, а сама она
    останется жить. Второй гард закрывает окно, которое открывает коммит
    отмены, отпускающий advisory-lock.
    """
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    spy = _PanelSpy()
    spy.install(monkeypatch)

    async with memory_session(monkeypatch, TABLES) as db:
        db.add(_user(remnawave_id=None))
        target = _sub(1, status=SubscriptionStatus.EXPIRED.value, remnawave_id=PANEL_ID)
        db.add(target)
        await db.commit()

        await deletion.delete_subscription_record(db, target, deleted_by='user')

    assert spy.order == ['grace', 'platega', 'lava', 'grace', 'panel_delete', 'decrement']
