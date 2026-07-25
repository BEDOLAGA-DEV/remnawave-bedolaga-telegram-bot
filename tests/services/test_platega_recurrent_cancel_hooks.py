"""Тесты отмены СБП-подписки Platega через миксин: идемпотентная точечная
отмена (``cancel_platega_sbp_subscription``), best-effort хелпер по
``subscription_id`` (``cancel_platega_recurring_for_subscription``) и
модульная точка входа для путей удаления подписки
(``cancel_platega_recurring_for_subscription_safe``, Task 11).
"""

import contextlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database.crud import platega_subscription as sub_crud
from app.database.models import Base, PlategaSubscription
from app.services.platega_service import PlategaService


def _ensure_real_aiosqlite(monkeypatch) -> None:
    """Снять фиктивный ``sys.modules['aiosqlite']`` перед созданием реального engine.

    См. одноимённую функцию в ``test_platega_subscription_callbacks.py``:
    ``tests/conftest.py`` подставляет нерабочую заглушку модуля через
    ``sys.modules.setdefault(...)``, которая перекрывает физически
    установленный пакет и валит ``create_async_engine('sqlite+aiosqlite:///:memory:')``.
    """
    stub = sys.modules.get('aiosqlite')
    if stub is not None and not hasattr(stub, 'connect'):
        monkeypatch.delitem(sys.modules, 'aiosqlite', raising=False)


@contextlib.asynccontextmanager
async def _memory_session(monkeypatch):
    """Реальная in-memory SQLite сессия с таблицей ``platega_subscriptions``.

    Методы отмены не трогают ``Subscription``/``Transaction``, поэтому в
    отличие от ``test_platega_subscription_callbacks.py`` этих таблиц здесь
    не нужно — хватает одной ``PlategaSubscription.__table__``.
    """
    _ensure_real_aiosqlite(monkeypatch)
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[PlategaSubscription.__table__]))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            yield session
    finally:
        await engine.dispose()


async def _create_recurring_record(db, *, platega_subscription_id, status='ACTIVE'):
    return await sub_crud.create_platega_subscription(
        db,
        user_id=1,
        subscription_id=1,
        tariff_id=None,
        interval=3,
        charge_days=30,
        amount_kopeks=19900,
        redirect_url=None,
        platega_subscription_id=platega_subscription_id,
        status=status,
    )


async def test_cancel_by_subscription_calls_platega_and_marks_cancelled(monkeypatch):
    """cancel_platega_recurring_for_subscription: находит активную запись по
    subscription_id, отменяет её на стороне Platega и локально. Повторный
    вызов, когда активной записи уже нет, — no-op без исключения
    (идемпотентность на уровне subscription_id).
    """
    from app.services.payment.platega import PlategaPaymentMixin

    async with _memory_session(monkeypatch) as db:
        rec = await _create_recurring_record(db, platega_subscription_id='ps-2')

        class Svc(PlategaPaymentMixin):
            def __init__(self):
                self.platega_service = SimpleNamespace(
                    cancel_subscription=AsyncMock(return_value={'status': 'cancelled'})
                )

        svc = Svc()
        await svc.cancel_platega_recurring_for_subscription(db, rec.subscription_id)

        svc.platega_service.cancel_subscription.assert_awaited_once_with('ps-2')
        await db.refresh(rec)
        assert rec.status == 'CANCELLED'

        # Идемпотентность второго вызова: активной записи больше нет.
        await svc.cancel_platega_recurring_for_subscription(db, rec.subscription_id)
        assert svc.platega_service.cancel_subscription.await_count == 1


async def test_cancel_best_effort_swallows_platega_error(monkeypatch):
    """Ошибка Platega API при отмене (сеть/5xx) не должна пробрасываться
    наружу — вызывающие пути удаления подписки не должны блокироваться
    недоступностью Platega. Локальный статус CANCELLED всё равно применяется.
    """
    from app.services.payment.platega import PlategaPaymentMixin

    async with _memory_session(monkeypatch) as db:
        rec = await _create_recurring_record(db, platega_subscription_id='ps-3')

        class Svc(PlategaPaymentMixin):
            def __init__(self):
                self.platega_service = SimpleNamespace(
                    cancel_subscription=AsyncMock(side_effect=RuntimeError('platega down'))
                )

        await Svc().cancel_platega_recurring_for_subscription(db, rec.subscription_id)  # не должен бросить

        await db.refresh(rec)
        assert rec.status == 'CANCELLED'  # локальная отмена применена несмотря на ошибку Platega


async def test_cancel_pending_without_platega_id_skips_api(monkeypatch):
    """PENDING-запись, которая не успела получить platega_subscription_id
    (сбой между вызовом Platega и записью id) — отмена должна просто
    пометить запись CANCELLED локально, не дёргая API без id.
    """
    from app.services.payment.platega import PlategaPaymentMixin

    async with _memory_session(monkeypatch) as db:
        rec = await sub_crud.create_platega_subscription(
            db,
            user_id=1,
            subscription_id=1,
            tariff_id=None,
            interval=3,
            charge_days=30,
            amount_kopeks=19900,
            redirect_url=None,
            platega_subscription_id=None,
            status='PENDING',
        )

        class Svc(PlategaPaymentMixin):
            def __init__(self):
                self.platega_service = SimpleNamespace(cancel_subscription=AsyncMock())

        svc = Svc()
        result = await svc.cancel_platega_sbp_subscription(db, local_id=rec.id)

        assert result is True
        await db.refresh(rec)
        assert rec.status == 'CANCELLED'
        svc.platega_service.cancel_subscription.assert_not_awaited()


# --- cancel_platega_recurring_for_subscription_safe (Task 11 module-level entry point) ---


def _configure_gate_on(monkeypatch: pytest.MonkeyPatch, **overrides) -> None:
    values = {
        'PLATEGA_ENABLED': True,
        'PLATEGA_MERCHANT_ID': 'm',
        'PLATEGA_SECRET': 's',
        'PLATEGA_RECURRENT_ENABLED': True,
    }
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value, raising=False)


async def test_cancel_safe_works_even_when_gate_off(monkeypatch):
    """Гейт выключен (``PLATEGA_RECURRENT_ENABLED=False``) — отмена ВСЁ РАВНО
    выполняется: это safety-операция. Если бы отмена гейтилась, выключение
    фичи бросало бы юзеров с живыми привязками, по которым Platega
    продолжила бы списывать без возможности отписаться.
    """
    from app.services.payment.platega import cancel_platega_recurring_for_subscription_safe

    _configure_gate_on(monkeypatch, PLATEGA_RECURRENT_ENABLED=False)
    mock_cancel = AsyncMock(return_value={'status': 'cancelled'})
    monkeypatch.setattr(PlategaService, 'cancel_subscription', mock_cancel)

    async with _memory_session(monkeypatch) as db:
        rec = await _create_recurring_record(db, platega_subscription_id='ps-gate-off')

        await cancel_platega_recurring_for_subscription_safe(db, rec.subscription_id)

        mock_cancel.assert_awaited_once_with('ps-gate-off')
        await db.refresh(rec)
        assert rec.status == 'CANCELLED'


async def test_cancel_safe_commit_false_defers_to_caller_transaction(monkeypatch):
    """``commit=False`` (мерж аккаунтов): локальный CANCELLED уходит flush'ем
    без коммита — откат транзакции вызывающего откатывает и его. Remote-отмена
    при этом выполняется (её при откате добьёт reconciler по remote-статусу).
    """
    from app.services.payment.platega import cancel_platega_recurring_for_subscription_safe

    _configure_gate_on(monkeypatch)
    mock_cancel = AsyncMock(return_value={'status': 'cancelled'})
    monkeypatch.setattr(PlategaService, 'cancel_subscription', mock_cancel)

    async with _memory_session(monkeypatch) as db:
        rec = await _create_recurring_record(db, platega_subscription_id='ps-no-commit')

        await cancel_platega_recurring_for_subscription_safe(db, rec.subscription_id, commit=False)

        mock_cancel.assert_awaited_once_with('ps-no-commit')
        assert rec.status == 'CANCELLED'  # в сессии — уже отменена

        await db.rollback()  # транзакция вызывающего откатилась
        await db.refresh(rec)
        assert rec.status == 'ACTIVE'  # CANCELLED не был закоммичен хелпером


async def test_cancel_safe_gate_on_cancels_active_record(monkeypatch):
    """Гейт включён + есть активная запись + ``PlategaService.cancel_subscription``
    замокан -> запись переходит в CANCELLED. Доказывает, что модульный хелпер
    сам конструирует лёгкого ``_PlategaSbpAgent`` (только ``PlategaService``,
    без остальных провайдеров ``PaymentService``) и реально доходит до миксина.
    """
    from app.services.payment.platega import cancel_platega_recurring_for_subscription_safe

    _configure_gate_on(monkeypatch)
    mock_cancel = AsyncMock(return_value={'status': 'cancelled'})
    monkeypatch.setattr(PlategaService, 'cancel_subscription', mock_cancel)

    async with _memory_session(monkeypatch) as db:
        rec = await _create_recurring_record(db, platega_subscription_id='ps-gate-on')

        await cancel_platega_recurring_for_subscription_safe(db, rec.subscription_id)

        mock_cancel.assert_awaited_once_with('ps-gate-on')
        await db.refresh(rec)
        assert rec.status == 'CANCELLED'


async def test_cancel_safe_never_raises_on_platega_error(monkeypatch):
    """Даже если ``PlategaService.cancel_subscription`` бросает исключение
    (сеть/5xx), модульная точка входа не должна пробрасывать его дальше —
    пути удаления подписки вызывают её fire-and-forget. Локальный статус
    всё равно переходит в CANCELLED (best-effort семантика миксина).
    """
    from app.services.payment.platega import cancel_platega_recurring_for_subscription_safe

    _configure_gate_on(monkeypatch)
    monkeypatch.setattr(PlategaService, 'cancel_subscription', AsyncMock(side_effect=RuntimeError('platega down')))

    async with _memory_session(monkeypatch) as db:
        rec = await _create_recurring_record(db, platega_subscription_id='ps-error')

        await cancel_platega_recurring_for_subscription_safe(db, rec.subscription_id)  # не должен бросить

        await db.refresh(rec)
        assert rec.status == 'CANCELLED'


def _make_subscription_and_tariff() -> tuple[SimpleNamespace, SimpleNamespace]:
    """Real subscription/tariff stubs, same shape as Task 5's
    ``test_create_sbp_subscription_persists_and_disables_autopay`` — the
    ``_PlategaSbpAgent().create_platega_sbp_subscription`` path this module's
    helper delegates to needs a tariff with real pricing methods, not a bare mock.
    """
    subscription = SimpleNamespace(id=1, autopay_enabled=True, autopay_period_days=30)
    tariff = SimpleNamespace(
        id=5,
        is_daily=False,
        name='Стандарт',
        get_available_periods=lambda: [30, 90],
        get_shortest_period=lambda: 30,
        get_purchasable_price_for_period=lambda d: 19900,
    )
    return subscription, tariff


async def test_enable_sbp_recurring_raises_when_gate_off(monkeypatch):
    """enable_platega_sbp_recurring: гейт выключен -> RuntimeError сразу, до
    любого обращения к БД или Platega. В отличие от best-effort
    ``cancel_platega_recurring_for_subscription_safe``, эта функция не должна
    проглатывать ошибку — вызывающий UI (бот/кабинет) обязан показать
    пользователю, что фича недоступна, а не промолчать. ``db=None`` доказывает,
    что БД не трогается до гейт-проверки: любое обращение к ней до raise
    провалило бы тест AttributeError'ом вместо ожидаемого RuntimeError.
    """
    from app.services.payment.platega import enable_platega_sbp_recurring

    monkeypatch.setattr(settings, 'PLATEGA_RECURRENT_ENABLED', False, raising=False)
    subscription, tariff = _make_subscription_and_tariff()

    with pytest.raises(RuntimeError):
        await enable_platega_sbp_recurring(None, user_id=777, subscription=subscription, tariff=tariff)


async def test_enable_sbp_recurring_gate_on_returns_redirect_url(monkeypatch):
    """Гейт включён + ``PlategaService.create_subscription`` застаблен ->
    возвращает dict с redirect_url. Доказывает, что модульный хелпер реально
    строит ``_PlategaSbpAgent`` (несущий ПОЛНЫЙ ``PlategaPaymentMixin``, а не
    только отменяющую его часть) и доходит до ``create_platega_sbp_subscription``.
    """
    from app.services.payment.platega import enable_platega_sbp_recurring

    _configure_gate_on(monkeypatch)
    mock_create = AsyncMock(return_value={'transactionId': 'tx-42', 'redirect': 'https://pay/42', 'status': 'PENDING'})
    monkeypatch.setattr(PlategaService, 'create_subscription', mock_create)
    subscription, tariff = _make_subscription_and_tariff()

    async with _memory_session(monkeypatch) as db:
        result = await enable_platega_sbp_recurring(db, user_id=777, subscription=subscription, tariff=tariff)

        assert result['redirect_url'] == 'https://pay/42'
        assert result['platega_subscription_id'] == 'tx-42'
        assert result['status'] == 'PENDING'
        mock_create.assert_awaited_once()


async def test_cancel_safe_wiring_proof_multi_tariff_delete_subscription(monkeypatch):
    """Доказательство подключения (Task 11) на самом маленьком вызываемом шве:
    роут ``multi_tariff.delete_subscription`` резолвит зависимости через
    FastAPI ``Depends`` — вызываем его как обычную корутину с руками
    собранными аргументами и подменяем ``cancel_platega_recurring_for_subscription_safe``
    ровно там, где её резолвит ленивый импорт внутри функции (атрибут модуля
    ``app.services.payment.platega`` на момент вызова).
    """
    import app.services.payment.platega as platega_module
    from app.cabinet.routes.subscription_modules import multi_tariff
    from app.database.models import SubscriptionStatus

    recorded: list[tuple[object, int]] = []

    call_order: list[str] = []

    async def fake_cancel(db, subscription_id):
        recorded.append((db, subscription_id))
        call_order.append('platega_cancel')

    monkeypatch.setattr(platega_module, 'cancel_platega_recurring_for_subscription_safe', fake_cancel)

    class FakeSubscription(SimpleNamespace):
        pass

    subscription = FakeSubscription(
        id=42,
        status=SubscriptionStatus.EXPIRED.value,
        actual_status=SubscriptionStatus.EXPIRED.value,
        remnawave_uuid=None,
        tariff_id=None,
    )
    user = SimpleNamespace(id=1)

    async def fake_get_subscription(db, subscription_id, user_id):
        return subscription

    async def fake_ensure_no_open_grace(db, subscription_ids):
        call_order.append('grace_check')

    async def fake_decrement_counts(db, subscription):
        return None

    monkeypatch.setattr(multi_tariff, 'get_subscription_by_id_for_user', fake_get_subscription)
    monkeypatch.setattr(multi_tariff, 'decrement_subscription_server_counts', fake_decrement_counts)
    monkeypatch.setattr(
        'app.services.grace_access_runtime.ensure_no_open_grace_for_subscriptions',
        fake_ensure_no_open_grace,
    )

    db = AsyncMock()
    db.delete = AsyncMock()

    result = await multi_tariff.delete_subscription(subscription_id=42, user=user, db=db)

    assert result == {'message': 'Subscription deleted'}
    assert recorded == [(db, 42)]  # cancel called with the subscription being deleted, before db.delete
    db.delete.assert_awaited_once_with(subscription)
    # The Platega cancel commits its own transaction, releasing the grace
    # guard's advisory lock acquired by the first check — so the guard MUST
    # be re-acquired (called again) right after cancel and before db.delete,
    # closing that window before the irreversible delete. Regression guard
    # for that ordering, not just its net effect.
    assert call_order == ['grace_check', 'platega_cancel', 'grace_check']


async def test_cancel_safe_wiring_proof_my_subscriptions_delete_execute(monkeypatch):
    """Same wiring proof as the multi_tariff test above, for the second
    reachable delete path that was missed when the first three sites were
    wired: the bot's own ``my_subscriptions.py::handle_subscription_delete_execute``
    (user self-delete of an expired/disabled subscription, ``sub_del_yes:{id}``).
    Without this, a user could delete a subscription that still has an active
    Platega SBP recurring subscription — CASCADE drops the
    platega_subscriptions row with it, so the reconciler can never heal it,
    and the user keeps getting charged for a subscription that no longer
    exists.
    """
    import app.services.payment.platega as platega_module
    from app.database.models import SubscriptionStatus
    from app.handlers.subscription import my_subscriptions

    recorded: list[tuple[object, int]] = []
    call_order: list[str] = []

    async def fake_cancel(db, subscription_id):
        recorded.append((db, subscription_id))
        call_order.append('platega_cancel')

    monkeypatch.setattr(platega_module, 'cancel_platega_recurring_for_subscription_safe', fake_cancel)

    subscription = SimpleNamespace(
        id=99,
        status=SubscriptionStatus.EXPIRED.value,
        actual_status=SubscriptionStatus.EXPIRED.value,
        remnawave_uuid=None,
    )

    async def fake_get_subscription(db, sub_id, user_id):
        return subscription

    async def fake_ensure_no_open_grace(db, subscription_ids):
        call_order.append('grace_check')

    async def fake_decrement_counts(db, sub):
        return None

    monkeypatch.setattr(my_subscriptions, 'get_subscription_by_id_for_user', fake_get_subscription)
    monkeypatch.setattr(my_subscriptions, 'decrement_subscription_server_counts', fake_decrement_counts)
    monkeypatch.setattr(my_subscriptions, 'show_my_subscriptions', AsyncMock())
    monkeypatch.setattr(
        'app.services.grace_access_runtime.ensure_no_open_grace_for_subscriptions',
        fake_ensure_no_open_grace,
    )

    callback = SimpleNamespace(data='sub_del_yes:99', answer=AsyncMock())
    db_user = SimpleNamespace(id=1)
    db = AsyncMock()
    db.delete = AsyncMock()
    state = AsyncMock()

    await my_subscriptions.handle_subscription_delete_execute(callback, db_user, db, state)

    assert recorded == [(db, 99)]  # cancel called with the subscription being deleted, before db.delete
    db.delete.assert_awaited_once_with(subscription)
    callback.answer.assert_awaited_once_with('Подписка удалена', show_alert=True)
    # Same ordering constraint as multi_tariff.delete_subscription: the
    # Platega cancel commits its own transaction, releasing the grace guard's
    # advisory lock acquired by the first check, so the guard must be
    # re-acquired right after cancel and before db.delete.
    assert call_order == ['grace_check', 'platega_cancel', 'grace_check']


async def test_cancel_safe_wiring_proof_admin_bulk_delete_subscription(monkeypatch):
    """Same wiring proof, for the third reachable delete path that was
    missed: the cabinet admin bulk-actions delete-subscription path
    (``admin_bulk_actions.py::_do_delete_subscription``, "Массовые действия"
    -> delete subscription for a batch of users).
    """
    import app.services.payment.platega as platega_module
    from app.cabinet.routes import admin_bulk_actions
    from app.cabinet.schemas.bulk_actions import BulkActionParams

    recorded: list[tuple[object, int]] = []
    call_order: list[str] = []

    async def fake_cancel(db, subscription_id):
        recorded.append((db, subscription_id))
        call_order.append('platega_cancel')

    async def fake_ensure_no_open_grace(db, subscription_ids):
        call_order.append('grace_check')

    monkeypatch.setattr(platega_module, 'cancel_platega_recurring_for_subscription_safe', fake_cancel)
    monkeypatch.setattr(
        'app.services.grace_access_runtime.ensure_no_open_grace_for_subscriptions',
        fake_ensure_no_open_grace,
    )

    sub = SimpleNamespace(
        id=77,
        is_active=False,
        is_trial=False,
        tariff=None,
        remnawave_uuid=None,
    )
    user = SimpleNamespace(id=1, username='victim', subscriptions=[], remnawave_uuid=None)
    db = AsyncMock()

    result = await admin_bulk_actions._do_delete_subscription(
        db, user, BulkActionParams(), dry_run=False, sub_override=sub
    )

    assert result.success is True
    assert recorded == [(db, 77)]  # cancel called with the subscription being deleted, before its DB delete
    # Same ordering constraint as the other two sites: the Platega cancel
    # commits its own transaction, releasing the grace guard's advisory lock
    # acquired by the first check, so the guard must be re-acquired right
    # after cancel and before the irreversible delete statements.
    assert call_order == ['grace_check', 'platega_cancel', 'grace_check']


# --- Optional/lower-priority sites: full-user deletion paths ----------------
#
# UserService.delete_user_account and BlockedUsersService.delete_user_from_db
# both delete the whole user (CASCADE drops subscriptions + their
# platega_subscriptions rows) without ever cancelling on Platega's side —
# Platega keeps charging a fully-deleted user. Flagged as a lower-priority
# follow-up alongside the three primary delete sites above; wired here too
# since both insertion points turned out to be straightforward.


async def test_delete_user_account_cancels_platega_between_grace_checks(monkeypatch):
    """UserService.delete_user_account (app/services/user_service.py) already
    had its own grace-access guard before this fix. This forces the SECOND
    grace check to raise so the test doesn't need to mock the rest of this
    ~600-line function's downstream DB operations (a dozen+ nested
    ``db.begin_nested()`` blocks unrelated to Platega) — the early return on
    the second check is itself proof the cancel loop ran strictly between
    the two checks, the same grace-lock ordering Task 11 established at the
    other delete sites (the cancel call commits internally, releasing the
    advisory lock the first check acquired).
    """
    import app.services.payment.platega as platega_module
    import app.services.user_service as user_service_module
    from app.services.grace_access_runtime import GraceAccessDeletionBlocked
    from app.services.user_service import UserService

    recorded: list[tuple[object, int]] = []
    call_order: list[str] = []
    calls = {'n': 0}

    async def fake_cancel(db, subscription_id):
        recorded.append((db, subscription_id))
        call_order.append('platega_cancel')

    async def fake_ensure_no_open_grace(db, subscription_ids):
        calls['n'] += 1
        call_order.append('grace_check')
        if calls['n'] == 2:
            raise GraceAccessDeletionBlocked(tuple(subscription_ids))

    monkeypatch.setattr(platega_module, 'cancel_platega_recurring_for_subscription_safe', fake_cancel)
    monkeypatch.setattr(
        'app.services.grace_access_runtime.ensure_no_open_grace_for_subscriptions',
        fake_ensure_no_open_grace,
    )

    subs = [SimpleNamespace(id=11, remnawave_uuid=None), SimpleNamespace(id=12, remnawave_uuid=None)]
    user = SimpleNamespace(id=5, telegram_id=555, email=None, subscriptions=subs, remnawave_uuid=None)
    monkeypatch.setattr(user_service_module, 'get_user_by_id', AsyncMock(return_value=user))

    db = AsyncMock()

    result = await UserService().delete_user_account(db, user_id=5, admin_id=1)

    assert recorded == [(db, 11), (db, 12)]  # cancelled for every subscription, before the second check
    assert call_order == ['grace_check', 'platega_cancel', 'platega_cancel', 'grace_check']
    assert result.grace_blocked is True
    assert result.bot_deleted is False


async def test_delete_user_from_db_cancels_platega_for_each_subscription(monkeypatch):
    """blocked_users_service.py::delete_user_from_db (blocked-user cleanup)
    deletes the whole user, CASCADE-dropping subscriptions and their
    platega_subscriptions rows. Unlike delete_user_account, it has no
    grace-access guard, so a plain best-effort cancel loop before the
    deletes is enough — no lock to re-acquire.
    """
    import app.services.payment.platega as platega_module
    from app.services.blocked_users_service import BlockedUsersService

    recorded: list[tuple[object, int]] = []

    async def fake_cancel(db, subscription_id):
        recorded.append((db, subscription_id))

    monkeypatch.setattr(platega_module, 'cancel_platega_recurring_for_subscription_safe', fake_cancel)

    subs = [SimpleNamespace(id=21, connected_squads=None), SimpleNamespace(id=22, connected_squads=None)]
    user = SimpleNamespace(id=9, telegram_id=999, email=None, subscriptions=subs)

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = user
    result_mock.scalars.return_value.all.return_value = []

    db = AsyncMock()
    db.execute = AsyncMock(return_value=result_mock)
    db.delete = AsyncMock()
    db.commit = AsyncMock()

    service = BlockedUsersService(bot=None)
    ok = await service.delete_user_from_db(db, user_id=9)

    assert ok is True
    assert recorded == [(db, 21), (db, 22)]
    db.delete.assert_awaited_once_with(user)
    db.commit.assert_awaited_once()
