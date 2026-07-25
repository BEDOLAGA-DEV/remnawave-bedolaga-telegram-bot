"""Тесты СБП-подписки Platega через миксин: создание (персист + отключение
balance-autopay) и обработка коллбеков (продление, идемпотентность, статусы).
"""

import contextlib
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.crud import platega_subscription as sub_crud
from app.database.models import Base, PlategaSubscription, Subscription, Transaction


def _ensure_real_aiosqlite(monkeypatch) -> None:
    """Снять фиктивный sys.modules['aiosqlite'] перед созданием реального engine.

    ``tests/conftest.py`` подставляет пустой ``ModuleType('aiosqlite')`` через
    ``sys.modules.setdefault(...)`` для окружений без реального пакета. Но эта
    строчка выполняется раньше, чем что-либо успевает импортировать настоящий
    aiosqlite, поэтому заглушка перекрывает и физически установленный пакет —
    ``create_async_engine('sqlite+aiosqlite:///:memory:')`` падает с
    ``AttributeError: module 'aiosqlite' has no attribute 'Connection'``.
    Удаляем нерабочую заглушку (нет ``connect``): следующий ``import aiosqlite``
    внутри диалекта SQLAlchemy подтянет настоящий установленный модуль.
    """
    stub = sys.modules.get('aiosqlite')
    if stub is not None and not hasattr(stub, 'connect'):
        monkeypatch.delitem(sys.modules, 'aiosqlite', raising=False)


@contextlib.asynccontextmanager
async def _memory_session(monkeypatch):
    """Реальная in-memory SQLite сессия с таблицами platega_subscriptions,
    subscriptions и transactions (последние две нужны коллбек-тестам — продление
    через ``Subscription.extend_subscription`` и аудит через ``create_transaction``).

    Полный create_all не годится (другие таблицы используют JSONB, SQLite не
    компилирует), а FK в SQLite по умолчанию не форсятся — поэтому user_id/
    subscription_id можно ставить произвольными; реальная строка Subscription
    нужна только тестам, которые проверяют фактическое продление end_date.
    """
    _ensure_real_aiosqlite(monkeypatch)
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(
                c, tables=[PlategaSubscription.__table__, Subscription.__table__, Transaction.__table__]
            )
        )
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            yield session
    finally:
        await engine.dispose()


async def test_create_sbp_subscription_persists_and_disables_autopay(monkeypatch):
    """create_platega_sbp_subscription: сохраняет запись, выключает autopay_enabled,
    возвращает redirect/status из ответа Platega.
    """
    from app.services.payment.platega import PlategaPaymentMixin

    subscription = SimpleNamespace(id=1, autopay_enabled=True, autopay_period_days=30)
    tariff = SimpleNamespace(
        id=5,
        is_daily=False,
        name='Стандарт',
        get_available_periods=lambda: [30, 90],
        get_shortest_period=lambda: 30,
        get_purchasable_price_for_period=lambda d: 19900,
    )

    class Svc(PlategaPaymentMixin):
        def __init__(self):
            self.platega_service = SimpleNamespace(
                create_subscription=AsyncMock(
                    return_value={'transactionId': 'tx-9', 'redirect': 'https://pay/9', 'status': 'PENDING'}
                )
            )

    async with _memory_session(monkeypatch) as db:
        result = await Svc().create_platega_sbp_subscription(db, user_id=777, subscription=subscription, tariff=tariff)

        assert result['platega_subscription_id'] == 'tx-9'
        assert result['redirect_url'] == 'https://pay/9'
        assert result['status'] == 'PENDING'
        assert subscription.autopay_enabled is False

        stored = await sub_crud.get_active_platega_subscription_by_subscription(db, subscription.id)
        assert stored is not None
        assert stored.id == result['local_id']
        assert stored.interval == 3
        assert stored.charge_days == 30
        assert stored.amount_kopeks == 19900


async def test_create_sbp_subscription_is_idempotent_on_repeat_call(monkeypatch):
    """Повторный вызов (двойной тап / ретрай клиента) не должен плодить вторую
    Platega-подписку и не должен повторно звать Platega API — иначе пользователя
    спишут дважды за цикл, а первая привязка станет невидимой для отмены
    (get_active_platega_subscription_by_subscription отдаёт только последнюю запись).
    """
    from app.services.payment.platega import PlategaPaymentMixin

    subscription = SimpleNamespace(id=1, autopay_enabled=True, autopay_period_days=30)
    tariff = SimpleNamespace(
        id=5,
        is_daily=False,
        name='Стандарт',
        get_available_periods=lambda: [30, 90],
        get_shortest_period=lambda: 30,
        get_purchasable_price_for_period=lambda d: 19900,
    )

    class Svc(PlategaPaymentMixin):
        def __init__(self):
            self.platega_service = SimpleNamespace(
                create_subscription=AsyncMock(
                    return_value={'transactionId': 'tx-9', 'redirect': 'https://pay/9', 'status': 'PENDING'}
                )
            )

    async with _memory_session(monkeypatch) as db:
        svc = Svc()
        stub = svc.platega_service

        first = await svc.create_platega_sbp_subscription(db, user_id=777, subscription=subscription, tariff=tariff)
        second = await svc.create_platega_sbp_subscription(db, user_id=777, subscription=subscription, tariff=tariff)

        assert stub.create_subscription.await_count == 1
        assert second['platega_subscription_id'] == first['platega_subscription_id']
        assert second['local_id'] == first['local_id']

        rows = await sub_crud.list_platega_subscriptions_by_statuses(db, ['PENDING', 'ACTIVE', 'PAST_DUE'])
        assert len([r for r in rows if r.subscription_id == subscription.id]) == 1


# --- process_platega_subscription_callback ---
#
# Во всех тестах ниже Svc — миксин без атрибута ``bot``. ``_notify_sbp_recurring``
# сначала best-effort шлёт WS-событие в кабинет (без зарегистрированных
# подключений ``cabinet_ws_manager.send_to_user`` — no-op, БД не трогает), а
# затем возвращается на bot/telegram_id early-return
# (``getattr(self, 'bot', None)`` -> None) и не требует таблицы users.


async def _create_recurring_record(db, *, platega_subscription_id: str, status: str = 'ACTIVE'):
    """Создаёт запись Platega-подписки без реальной привязанной Subscription —
    для тестов статусных переходов, которым продление не нужно.
    """
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


async def test_confirmed_charge_extends_subscription_and_is_idempotent(monkeypatch):
    """CONFIRMED: продлевает Subscription.end_date на charge_days, пишет аудитную
    транзакцию SUBSCRIPTION_PAYMENT, инкрементит charges_success и сохраняет
    next_charge_at. Повтор того же charge Id (ретрай доставки коллбека от
    Platega) не должен продлевать подписку повторно.
    """
    from app.services.payment.platega import PlategaPaymentMixin

    class Svc(PlategaPaymentMixin):
        """Без атрибута bot."""

    async with _memory_session(monkeypatch) as db:
        end0 = datetime.now(UTC) + timedelta(days=2)
        subscription = Subscription(id=1, user_id=1, status='active', end_date=end0)
        db.add(subscription)
        await db.commit()

        rec = await sub_crud.create_platega_subscription(
            db,
            user_id=1,
            subscription_id=1,
            tariff_id=None,
            interval=3,
            charge_days=30,
            amount_kopeks=19900,
            redirect_url=None,
            platega_subscription_id='ps-1',
            status='ACTIVE',
        )

        svc = Svc()
        payload = {
            'Status': 'CONFIRMED',
            'Id': 'charge-1',
            'Amount': 199,
            'Currency': 'RUB',
            'PaymentMethod': 6,
            'SubscriptionId': 'ps-1',
            'NextChargeAt': '2026-09-01T00:00:00Z',
        }
        await svc.process_platega_subscription_callback(db, payload)

        await db.refresh(subscription)
        await db.refresh(rec)
        assert subscription.end_date >= end0 + timedelta(days=29)
        assert rec.charges_success == 1
        assert rec.next_charge_at is not None
        assert rec.status == 'ACTIVE'
        assert rec.last_charge_external_id == 'charge-1'

        transactions = (await db.execute(select(Transaction))).scalars().all()
        assert len(transactions) >= 1

        end_after_first_charge = subscription.end_date

        # Реплей того же коллбека — идемпотентно, без повторного продления.
        await svc.process_platega_subscription_callback(db, payload)
        await db.refresh(rec)
        await db.refresh(subscription)
        assert rec.charges_success == 1
        assert subscription.end_date == end_after_first_charge


async def test_confirmed_charge_syncs_remnawave_panel_after_extension(monkeypatch):
    """CONFIRMED: после коммита продления должен best-effort синкнуться в панель
    RemnaWave через ``SubscriptionService.update_remnawave_user`` — иначе панель
    продолжает отдавать пользователю старый end_date, и оплативший продление
    через СБП пользователь всё равно отваливается по старой дате истечения,
    хотя бот репортит "продлено". Зеркалит balance-autopay
    (``monitoring_service._process_autopayments``, синк после extend_subscription).
    """
    from app.services.payment.platega import PlategaPaymentMixin
    from app.services.subscription_service import SubscriptionService

    class Svc(PlategaPaymentMixin):
        """Без атрибута bot."""

    mock_sync = AsyncMock(return_value=None)
    monkeypatch.setattr(SubscriptionService, 'update_remnawave_user', mock_sync)

    async with _memory_session(monkeypatch) as db:
        end0 = datetime.now(UTC) + timedelta(days=2)
        subscription = Subscription(id=1, user_id=1, status='active', end_date=end0)
        db.add(subscription)
        await db.commit()

        await sub_crud.create_platega_subscription(
            db,
            user_id=1,
            subscription_id=1,
            tariff_id=None,
            interval=3,
            charge_days=30,
            amount_kopeks=19900,
            redirect_url=None,
            platega_subscription_id='ps-sync',
            status='ACTIVE',
        )

        payload = {
            'Status': 'CONFIRMED',
            'Id': 'charge-sync-1',
            'SubscriptionId': 'ps-sync',
        }
        await Svc().process_platega_subscription_callback(db, payload)

        mock_sync.assert_awaited_once()
        call = mock_sync.await_args
        assert call.args[0] is db
        assert call.args[1] is subscription
        assert call.kwargs.get('reset_reason') == 'СБП-автопродление'


async def test_confirmed_charge_panel_sync_failure_is_best_effort(monkeypatch):
    """Сбой синка панели (Remnawave недоступна) — best-effort: не должен
    пробрасывать исключение наружу (коллбек обязан вернуть 200 OK), а
    продление в БД (end_date, счётчики, last_charge_external_id) уже
    закоммичено и не откатывается синком, упавшим ПОСЛЕ commit.
    """
    from app.services.payment.platega import PlategaPaymentMixin
    from app.services.subscription_service import SubscriptionService

    class Svc(PlategaPaymentMixin):
        """Без атрибута bot."""

    monkeypatch.setattr(SubscriptionService, 'update_remnawave_user', AsyncMock(side_effect=RuntimeError('panel down')))

    async with _memory_session(monkeypatch) as db:
        end0 = datetime.now(UTC) + timedelta(days=2)
        subscription = Subscription(id=1, user_id=1, status='active', end_date=end0)
        db.add(subscription)
        await db.commit()

        rec = await sub_crud.create_platega_subscription(
            db,
            user_id=1,
            subscription_id=1,
            tariff_id=None,
            interval=3,
            charge_days=30,
            amount_kopeks=19900,
            redirect_url=None,
            platega_subscription_id='ps-sync-fail',
            status='ACTIVE',
        )

        payload = {
            'Status': 'CONFIRMED',
            'Id': 'charge-sync-fail-1',
            'SubscriptionId': 'ps-sync-fail',
        }
        await Svc().process_platega_subscription_callback(db, payload)  # не должен бросить

        await db.refresh(subscription)
        await db.refresh(rec)
        assert subscription.end_date >= end0 + timedelta(days=29)
        assert rec.charges_success == 1
        assert rec.last_charge_external_id == 'charge-sync-fail-1'

        transactions = (await db.execute(select(Transaction))).scalars().all()
        assert len(transactions) >= 1


async def test_confirmed_charge_with_empty_id_does_not_extend(monkeypatch):
    """CONFIRMED без Id (или с пустым Id) — недоверенный коллбек: без id
    идемпотентность по ``last_charge_external_id`` не сработает, поэтому
    каждый повтор продлевал бы подписку заново. Такой коллбек не должен
    продлевать подписку и не должен засчитываться как успешное списание.
    """
    from app.services.payment.platega import PlategaPaymentMixin

    class Svc(PlategaPaymentMixin):
        """Без атрибута bot."""

    async with _memory_session(monkeypatch) as db:
        end0 = datetime.now(UTC) + timedelta(days=2)
        subscription = Subscription(id=1, user_id=1, status='active', end_date=end0)
        db.add(subscription)
        await db.commit()

        rec = await sub_crud.create_platega_subscription(
            db,
            user_id=1,
            subscription_id=1,
            tariff_id=None,
            interval=3,
            charge_days=30,
            amount_kopeks=19900,
            redirect_url=None,
            platega_subscription_id='ps-empty-id',
            status='ACTIVE',
        )

        svc = Svc()
        for missing_id in (None, ''):
            payload = {'Status': 'CONFIRMED', 'Id': missing_id, 'SubscriptionId': 'ps-empty-id'}
            await svc.process_platega_subscription_callback(db, payload)

        await db.refresh(subscription)
        await db.refresh(rec)
        assert subscription.end_date == end0
        assert rec.charges_success == 0
        assert rec.last_charge_external_id is None

        transactions = (await db.execute(select(Transaction))).scalars().all()
        assert len(transactions) == 0


async def test_confirmed_charge_missing_subscription_does_not_report_false_success(monkeypatch):
    """CONFIRMED, но привязанная Subscription отсутствует (гонка/рассинхрон,
    практически недостижимо благодаря CASCADE FK на subscription_id) — не
    должен инкрементить charges_success, писать аудитную транзакцию или
    помечать charge_id как обработанный: реального продления не произошло,
    репортить успех нельзя.
    """
    from app.services.payment.platega import PlategaPaymentMixin

    class Svc(PlategaPaymentMixin):
        """Без атрибута bot."""

    async with _memory_session(monkeypatch) as db:
        # subscription_id=1 указывает на несуществующую Subscription — FK в
        # SQLite не форсится (см. docstring _memory_session).
        rec = await _create_recurring_record(db, platega_subscription_id='ps-9')

        await Svc().process_platega_subscription_callback(
            db, {'Status': 'CONFIRMED', 'Id': 'charge-9', 'SubscriptionId': 'ps-9'}
        )

        await db.refresh(rec)
        assert rec.charges_success == 0
        assert rec.last_charge_external_id is None
        assert rec.status == 'ACTIVE'

        transactions = (await db.execute(select(Transaction))).scalars().all()
        assert len(transactions) == 0


async def test_canceled_charge_marks_past_due_and_counts_failure(monkeypatch):
    from app.services.payment.platega import PlategaPaymentMixin

    class Svc(PlategaPaymentMixin):
        """Без атрибута bot."""

    async with _memory_session(monkeypatch) as db:
        rec = await _create_recurring_record(db, platega_subscription_id='ps-2')

        await Svc().process_platega_subscription_callback(
            db, {'Status': 'CANCELED', 'Id': 'charge-fail-1', 'SubscriptionId': 'ps-2'}
        )

        await db.refresh(rec)
        assert rec.status == 'PAST_DUE'
        assert rec.charges_failed == 1


async def test_subscription_past_due_status_transition(monkeypatch):
    from app.services.payment.platega import PlategaPaymentMixin

    class Svc(PlategaPaymentMixin):
        """Без атрибута bot."""

    async with _memory_session(monkeypatch) as db:
        rec = await _create_recurring_record(db, platega_subscription_id='ps-3')

        await Svc().process_platega_subscription_callback(
            db, {'Status': 'SUBSCRIPTION_PAST_DUE', 'SubscriptionId': 'ps-3'}
        )

        await db.refresh(rec)
        assert rec.status == 'PAST_DUE'


async def test_subscription_cancelled_status_transition(monkeypatch):
    from app.services.payment.platega import PlategaPaymentMixin

    class Svc(PlategaPaymentMixin):
        """Без атрибута bot."""

    async with _memory_session(monkeypatch) as db:
        rec = await _create_recurring_record(db, platega_subscription_id='ps-4')

        await Svc().process_platega_subscription_callback(
            db, {'Status': 'SUBSCRIPTION_CANCELLED', 'SubscriptionId': 'ps-4'}
        )

        await db.refresh(rec)
        assert rec.status == 'CANCELLED'


async def test_subscription_failed_status_transition(monkeypatch):
    from app.services.payment.platega import PlategaPaymentMixin

    class Svc(PlategaPaymentMixin):
        """Без атрибута bot."""

    async with _memory_session(monkeypatch) as db:
        rec = await _create_recurring_record(db, platega_subscription_id='ps-5')

        await Svc().process_platega_subscription_callback(
            db, {'Status': 'SUBSCRIPTION_FAILED', 'SubscriptionId': 'ps-5'}
        )

        await db.refresh(rec)
        assert rec.status == 'FAILED'


async def test_subscription_activated_status_transition(monkeypatch):
    from app.services.payment.platega import PlategaPaymentMixin

    class Svc(PlategaPaymentMixin):
        """Без атрибута bot."""

    async with _memory_session(monkeypatch) as db:
        rec = await _create_recurring_record(db, platega_subscription_id='ps-6', status='PENDING')

        await Svc().process_platega_subscription_callback(
            db, {'Status': 'SUBSCRIPTION_ACTIVATED', 'SubscriptionId': 'ps-6'}
        )

        await db.refresh(rec)
        assert rec.status == 'ACTIVE'


# --- WS-события sbp_recurring.* для кабинета ---
#
# Кабинет-фронтенд слушает WS-события ``sbp_recurring.{kind}`` — единственный
# канал для email-only пользователей без telegram_id (у которых нет доступа к
# сообщениям бота). Эмиссия должна происходить ДО early-return по
# bot/telegram_id и не должна ломать коллбек при сбое отправки.


async def test_confirmed_charge_emits_ws_event_to_cabinet(monkeypatch):
    """CONFIRMED-коллбек должен отправить WS-событие sbp_recurring.confirmed
    с полной формой сообщения — включая next_charge_at из коллбека Platega.
    """
    from app.cabinet.routes.websocket import cabinet_ws_manager
    from app.services.payment.platega import PlategaPaymentMixin

    class Svc(PlategaPaymentMixin):
        """Без атрибута bot — email-only путь."""

    mock_send = AsyncMock(return_value=None)
    monkeypatch.setattr(cabinet_ws_manager, 'send_to_user', mock_send)

    async with _memory_session(monkeypatch) as db:
        end0 = datetime.now(UTC) + timedelta(days=2)
        subscription = Subscription(id=1, user_id=1, status='active', end_date=end0)
        db.add(subscription)
        await db.commit()

        await sub_crud.create_platega_subscription(
            db,
            user_id=1,
            subscription_id=1,
            tariff_id=None,
            interval=3,
            charge_days=30,
            amount_kopeks=19900,
            redirect_url=None,
            platega_subscription_id='ps-ws-1',
            status='ACTIVE',
        )

        payload = {
            'Status': 'CONFIRMED',
            'Id': 'charge-ws-1',
            'SubscriptionId': 'ps-ws-1',
            'NextChargeAt': '2026-09-01T00:00:00Z',
        }
        await Svc().process_platega_subscription_callback(db, payload)

    mock_send.assert_awaited_once()
    call = mock_send.await_args
    assert call.args[0] == 1  # record.user_id
    message = call.args[1]
    assert message == {
        'type': 'sbp_recurring.confirmed',
        'status': 'ACTIVE',
        'amount_kopeks': 19900,
        'amount_rubles': 199.0,
        'next_charge_at': '2026-09-01T00:00:00+00:00',
        'subscription_id': 1,
    }


async def test_ws_emission_failure_does_not_break_callback(monkeypatch):
    """Сбой отправки WS-события (соединения нет / менеджер упал) — best-effort,
    не должен прерывать обработку коллбека: продление подписки и счётчики
    должны примениться как обычно.
    """
    from app.cabinet.routes.websocket import cabinet_ws_manager
    from app.services.payment.platega import PlategaPaymentMixin

    class Svc(PlategaPaymentMixin):
        """Без атрибута bot."""

    monkeypatch.setattr(cabinet_ws_manager, 'send_to_user', AsyncMock(side_effect=RuntimeError('ws down')))

    async with _memory_session(monkeypatch) as db:
        end0 = datetime.now(UTC) + timedelta(days=2)
        subscription = Subscription(id=1, user_id=1, status='active', end_date=end0)
        db.add(subscription)
        await db.commit()

        rec = await sub_crud.create_platega_subscription(
            db,
            user_id=1,
            subscription_id=1,
            tariff_id=None,
            interval=3,
            charge_days=30,
            amount_kopeks=19900,
            redirect_url=None,
            platega_subscription_id='ps-ws-2',
            status='ACTIVE',
        )

        payload = {
            'Status': 'CONFIRMED',
            'Id': 'charge-ws-2',
            'SubscriptionId': 'ps-ws-2',
        }
        await Svc().process_platega_subscription_callback(db, payload)  # не должен бросить

        await db.refresh(subscription)
        await db.refresh(rec)
        assert subscription.end_date >= end0 + timedelta(days=29)
        assert rec.charges_success == 1


async def test_ws_emission_fires_without_bot_attribute(monkeypatch):
    """Эмиссия WS-события должна происходить ДО early-return по
    ``bot``/``telegram_id`` в ``_notify_sbp_recurring`` — иначе email-only
    пользователи кабинета (без Telegram) не получают вообще ничего.
    """
    from app.cabinet.routes.websocket import cabinet_ws_manager
    from app.services.payment.platega import PlategaPaymentMixin

    class Svc(PlategaPaymentMixin):
        """Без атрибута bot — воспроизводит email-only сервис-инстанс."""

    mock_send = AsyncMock(return_value=None)
    monkeypatch.setattr(cabinet_ws_manager, 'send_to_user', mock_send)

    async with _memory_session(monkeypatch) as db:
        rec = await _create_recurring_record(db, platega_subscription_id='ps-ws-3')

        svc = Svc()
        assert not hasattr(svc, 'bot')

        await svc.process_platega_subscription_callback(
            db, {'Status': 'SUBSCRIPTION_PAST_DUE', 'SubscriptionId': 'ps-ws-3'}
        )

        await db.refresh(rec)
        assert rec.status == 'PAST_DUE'

    mock_send.assert_awaited_once()
    message = mock_send.await_args.args[1]
    assert message['type'] == 'sbp_recurring.past_due'


async def test_callback_noops_on_missing_or_unknown_subscription_id(monkeypatch):
    """Отсутствующий/неизвестный SubscriptionId и нераспознанный статус — не
    должны бросать исключение (коллбек обязан вернуть 200 OK на любой валидный
    JSON от Platega), просто ничего не меняют.
    """
    from app.services.payment.platega import PlategaPaymentMixin

    class Svc(PlategaPaymentMixin):
        """Без атрибута bot."""

    async with _memory_session(monkeypatch) as db:
        rec = await _create_recurring_record(db, platega_subscription_id='ps-7')
        svc = Svc()

        # Без SubscriptionId — просто лог + выход.
        await svc.process_platega_subscription_callback(db, {'Status': 'CONFIRMED', 'Id': 'x'})

        # Неизвестный SubscriptionId — запись не найдена.
        await svc.process_platega_subscription_callback(
            db, {'Status': 'CONFIRMED', 'Id': 'x', 'SubscriptionId': 'ps-missing'}
        )

        # Нераспознанный статус для существующей записи.
        await svc.process_platega_subscription_callback(db, {'Status': 'SOMETHING_ELSE', 'SubscriptionId': 'ps-7'})

        await db.refresh(rec)
        assert rec.status == 'ACTIVE'
        assert rec.charges_success == 0
        assert rec.charges_failed == 0


# --- Аудит-фиксы: воскрешение отменённой записи, поздний ределивери, словарь провалов ---


async def test_confirmed_charge_on_cancelled_record_extends_but_stays_cancelled(monkeypatch):
    """Списание по локально ОТМЕНЁННОЙ записи (удалённая отмена не прошла):
    деньги взяты — подписка продлевается честно, но запись НЕ воскрешается в
    ACTIVE (иначе отмена юзера молча стирается и цикл продолжается вечно), и
    удалённая отмена немедленно повторяется.
    """
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services.payment.platega import PlategaPaymentMixin

    class Svc(PlategaPaymentMixin):
        def __init__(self):
            self.platega_service = SimpleNamespace(cancel_subscription=AsyncMock(return_value={'status': 'cancelled'}))

    async with _memory_session(monkeypatch) as db:
        end0 = datetime.now(UTC) + timedelta(days=2)
        subscription = Subscription(id=1, user_id=1, status='active', end_date=end0)
        db.add(subscription)
        await db.commit()

        rec = await _create_recurring_record(db, platega_subscription_id='ps-res', status='CANCELLED')

        svc = Svc()
        await svc.process_platega_subscription_callback(
            db, {'Status': 'CONFIRMED', 'Id': 'charge-res-1', 'SubscriptionId': 'ps-res'}
        )

        await db.refresh(subscription)
        await db.refresh(rec)
        assert subscription.end_date >= end0 + timedelta(days=29)  # деньги взяты — продлено
        assert rec.status == 'CANCELLED'  # но запись не воскрешена
        assert rec.charges_success == 1
        svc.platega_service.cancel_subscription.assert_awaited_once_with('ps-res')


async def test_confirmed_late_redelivery_of_older_charge_is_skipped(monkeypatch):
    """last_charge_external_id хранит только ПОСЛЕДНИЙ charge Id: поздний
    ределивери СТАРОГО списания (N после N+1) проходит мимо быстрой проверки —
    его должна поймать полноисторийная сверка с transactions.external_id.
    """
    from datetime import UTC, datetime, timedelta

    from app.database.models import PaymentMethod, TransactionType
    from app.services.payment.platega import PlategaPaymentMixin

    class Svc(PlategaPaymentMixin):
        """Без атрибута bot."""

    async with _memory_session(monkeypatch) as db:
        end0 = datetime.now(UTC) + timedelta(days=32)
        subscription = Subscription(id=1, user_id=1, status='active', end_date=end0)
        db.add(subscription)
        # Старое списание charge-N уже обработано и записано в transactions,
        # но last_charge_external_id уже перезаписан более новым charge-N+1.
        db.add(
            Transaction(
                user_id=1,
                type=TransactionType.SUBSCRIPTION_PAYMENT.value,
                amount_kopeks=19900,
                description='СБП-автопродление Platega',
                payment_method=PaymentMethod.PLATEGA.value,
                external_id='charge-N',
            )
        )
        await db.commit()

        rec = await _create_recurring_record(db, platega_subscription_id='ps-late')
        rec.last_charge_external_id = 'charge-N+1'
        await db.commit()

        await Svc().process_platega_subscription_callback(
            db, {'Status': 'CONFIRMED', 'Id': 'charge-N', 'SubscriptionId': 'ps-late'}
        )

        await db.refresh(subscription)
        await db.refresh(rec)
        assert subscription.end_date == end0  # повторного продления не было
        assert rec.charges_success == 0
        assert rec.last_charge_external_id == 'charge-N+1'  # не затёрт старым Id


async def test_failed_and_expired_charge_statuses_mark_past_due(monkeypatch):
    """Словарь провального списания не ограничен CANCELED: разовые платежи
    Platega знают FAILED и EXPIRED — оба должны давать PAST_DUE + счётчик,
    а не проваливаться в 'неизвестный статус' (молча теряя провал списания).
    """
    from app.services.payment.platega import PlategaPaymentMixin

    class Svc(PlategaPaymentMixin):
        """Без атрибута bot."""

    for i, status in enumerate(('FAILED', 'EXPIRED')):
        async with _memory_session(monkeypatch) as db:
            rec = await _create_recurring_record(db, platega_subscription_id=f'ps-fail-{i}')

            await Svc().process_platega_subscription_callback(
                db, {'Status': status, 'Id': f'charge-{status}', 'SubscriptionId': f'ps-fail-{i}'}
            )

            await db.refresh(rec)
            assert rec.status == 'PAST_DUE', status
            assert rec.charges_failed == 1, status


async def test_create_sbp_subscription_rejects_zero_price(monkeypatch):
    """Нулевая цена отклоняется наравне с отсутствующей: подписка Platega на
    0 ₽ бессмысленна и вела бы к пустым регулярным «списаниям». Platega API
    при этом вызываться не должен.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import pytest

    from app.services.payment.platega import PlategaPaymentMixin

    subscription = SimpleNamespace(id=1, autopay_enabled=False, autopay_period_days=30)
    tariff = SimpleNamespace(
        id=5,
        is_daily=False,
        name='Бесплатный',
        get_available_periods=lambda: [30],
        get_shortest_period=lambda: 30,
        get_purchasable_price_for_period=lambda d: 0,
    )

    class Svc(PlategaPaymentMixin):
        def __init__(self):
            self.platega_service = SimpleNamespace(create_subscription=AsyncMock())

    async with _memory_session(monkeypatch) as db:
        svc = Svc()
        with pytest.raises(ValueError, match='не имеет цены'):
            await svc.create_platega_sbp_subscription(db, user_id=777, subscription=subscription, tariff=tariff)

        svc.platega_service.create_subscription.assert_not_awaited()
        rows = await sub_crud.list_platega_subscriptions_by_statuses(db, ['PENDING', 'ACTIVE', 'PAST_DUE'])
        assert rows == []


async def test_create_sbp_short_circuit_reenforces_autopay_off(monkeypatch):
    """Идемпотентный повтор create при живой записи: если между вызовами юзер
    успел включить balance-autopay, short-circuit обязан заново выключить его —
    иначе оба движка продления останутся включёнными одновременно.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services.payment.platega import PlategaPaymentMixin

    subscription = SimpleNamespace(id=1, autopay_enabled=True, autopay_period_days=30)
    tariff = SimpleNamespace(
        id=5,
        is_daily=False,
        name='Стандарт',
        get_available_periods=lambda: [30],
        get_shortest_period=lambda: 30,
        get_purchasable_price_for_period=lambda d: 19900,
    )

    class Svc(PlategaPaymentMixin):
        def __init__(self):
            self.platega_service = SimpleNamespace(
                create_subscription=AsyncMock(
                    return_value={'transactionId': 'tx-re', 'redirect': 'https://pay/re', 'status': 'PENDING'}
                )
            )

    async with _memory_session(monkeypatch) as db:
        svc = Svc()
        await svc.create_platega_sbp_subscription(db, user_id=777, subscription=subscription, tariff=tariff)
        assert subscription.autopay_enabled is False

        subscription.autopay_enabled = True  # юзер включил balance-autopay между вызовами
        await svc.create_platega_sbp_subscription(db, user_id=777, subscription=subscription, tariff=tariff)

        assert subscription.autopay_enabled is False  # short-circuit заново выключил
        assert svc.platega_service.create_subscription.await_count == 1


# --- purchase_tariff_with_sbp_recurring (СБП-оформление покупки) ---


def _purchase_tariff(**overrides):
    from types import SimpleNamespace

    base = dict(
        id=5,
        is_active=True,
        is_daily=False,
        name='Стандарт',
        allowed_squads=['squad-a'],
        traffic_limit_gb=100,
        device_limit=3,
        get_available_periods=lambda: [30],
        get_shortest_period=lambda: 30,
        get_purchasable_price_for_period=lambda d: 19900,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


async def test_sbp_purchase_creates_expired_stub_for_new_tariff(monkeypatch):
    """Нет подписки этого тарифа → создаётся EXPIRED-заготовка (без доступа),
    привязка вешается на неё; ответ несёт subscription_id для поллинга."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.config import settings
    from app.database.models import SubscriptionStatus
    from app.services.payment import platega as platega_module

    for key, value in {
        'PLATEGA_ENABLED': True,
        'PLATEGA_MERCHANT_ID': 'm',
        'PLATEGA_SECRET': 's',
        'PLATEGA_RECURRENT_ENABLED': True,
    }.items():
        monkeypatch.setattr(settings, key, value, raising=False)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)

    tariff = _purchase_tariff()
    user = SimpleNamespace(id=777)

    created = {}

    async def fake_create_stub(db, user_id, t):
        from app.database.models import Subscription

        stub = Subscription(
            id=91,
            user_id=user_id,
            tariff_id=t.id,
            status=SubscriptionStatus.EXPIRED.value,
            is_trial=False,
        )
        created['stub'] = stub
        return stub

    mock_enable = AsyncMock(return_value={'status': 'PENDING', 'redirect_url': 'https://pay/x', 'local_id': 1})
    monkeypatch.setattr('app.database.crud.subscription.create_sbp_pending_subscription', fake_create_stub)
    monkeypatch.setattr(
        'app.database.crud.subscription.get_subscription_by_user_and_tariff', AsyncMock(return_value=None)
    )
    monkeypatch.setattr(platega_module, 'enable_platega_sbp_recurring', mock_enable)

    async with _memory_session(monkeypatch) as db:
        result = await platega_module.purchase_tariff_with_sbp_recurring(db, user=user, tariff=tariff)

    assert result['subscription_id'] == 91
    assert result['redirect_url'] == 'https://pay/x'
    mock_enable.assert_awaited_once_with(db, user_id=777, subscription=created['stub'], tariff=tariff)


async def test_sbp_purchase_binds_to_existing_expired_subscription(monkeypatch):
    """Есть истёкшая подписка тарифа → привязка на неё, заготовка не создаётся."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.config import settings
    from app.services.payment import platega as platega_module

    for key, value in {
        'PLATEGA_ENABLED': True,
        'PLATEGA_MERCHANT_ID': 'm',
        'PLATEGA_SECRET': 's',
        'PLATEGA_RECURRENT_ENABLED': True,
    }.items():
        monkeypatch.setattr(settings, key, value, raising=False)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)

    tariff = _purchase_tariff()
    user = SimpleNamespace(id=777)
    existing = SimpleNamespace(id=55, is_trial=False, status='expired', tariff_id=5)

    mock_enable = AsyncMock(return_value={'status': 'PENDING', 'redirect_url': 'https://pay/y', 'local_id': 2})
    mock_create_stub = AsyncMock()
    monkeypatch.setattr(
        'app.database.crud.subscription.get_subscription_by_user_and_tariff', AsyncMock(return_value=existing)
    )
    monkeypatch.setattr('app.database.crud.subscription.create_sbp_pending_subscription', mock_create_stub)
    monkeypatch.setattr(platega_module, 'enable_platega_sbp_recurring', mock_enable)

    async with _memory_session(monkeypatch) as db:
        result = await platega_module.purchase_tariff_with_sbp_recurring(db, user=user, tariff=tariff)

    assert result['subscription_id'] == 55
    mock_create_stub.assert_not_awaited()


async def test_sbp_purchase_refuses_trial_disabled_and_foreign_tariff(monkeypatch):
    """Отказы: триал (конверсию делает только balance-покупка), disabled/pending
    (чардж не активирует), в single-режиме — подписка другого тарифа."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import pytest

    from app.config import settings
    from app.services.payment import platega as platega_module

    for key, value in {
        'PLATEGA_ENABLED': True,
        'PLATEGA_MERCHANT_ID': 'm',
        'PLATEGA_SECRET': 's',
        'PLATEGA_RECURRENT_ENABLED': True,
    }.items():
        monkeypatch.setattr(settings, key, value, raising=False)

    tariff = _purchase_tariff()
    user = SimpleNamespace(id=777)

    cases = [
        (SimpleNamespace(id=1, is_trial=True, status='active', tariff_id=5), 'триал'),
        (SimpleNamespace(id=2, is_trial=False, status='disabled', tariff_id=5), 'этой подписки'),
        (SimpleNamespace(id=3, is_trial=False, status='pending', tariff_id=5), 'этой подписки'),
    ]
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    for sub, _label in cases:
        monkeypatch.setattr(
            'app.database.crud.subscription.get_subscription_by_user_and_tariff', AsyncMock(return_value=sub)
        )
        async with _memory_session(monkeypatch) as db:
            with pytest.raises(ValueError):
                await platega_module.purchase_tariff_with_sbp_recurring(db, user=user, tariff=tariff)

    # single-sub режим: подписка другого тарифа
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    foreign = SimpleNamespace(id=4, is_trial=False, status='active', tariff_id=99)
    monkeypatch.setattr('app.database.crud.subscription.get_subscription_by_user_id', AsyncMock(return_value=foreign))
    async with _memory_session(monkeypatch) as db:
        with pytest.raises(ValueError):
            await platega_module.purchase_tariff_with_sbp_recurring(db, user=user, tariff=tariff)


async def test_sbp_purchase_gate_off_raises(monkeypatch):
    from types import SimpleNamespace

    import pytest

    from app.config import settings
    from app.services.payment import platega as platega_module

    monkeypatch.setattr(settings, 'PLATEGA_RECURRENT_ENABLED', False, raising=False)

    async with _memory_session(monkeypatch) as db:
        with pytest.raises(RuntimeError):
            await platega_module.purchase_tariff_with_sbp_recurring(
                db, user=SimpleNamespace(id=1), tariff=_purchase_tariff()
            )


async def test_concurrent_enable_race_returns_winner_and_cancels_orphan(monkeypatch):
    """Гонка конкурентного enable: оба прошли идемпотентную проверку, второй
    insert отбивается partial unique uq_platega_subscriptions_alive —
    проигравший отменяет свою осиротевшую remote-подписку и возвращает
    запись победителя."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services.payment.platega import PlategaPaymentMixin

    subscription = SimpleNamespace(id=1, autopay_enabled=False, autopay_period_days=30)
    tariff = SimpleNamespace(
        id=5,
        is_daily=False,
        name='Стандарт',
        get_available_periods=lambda: [30],
        get_shortest_period=lambda: 30,
        get_purchasable_price_for_period=lambda d: 19900,
    )

    class Svc(PlategaPaymentMixin):
        def __init__(self):
            self.platega_service = SimpleNamespace(
                create_subscription=AsyncMock(
                    return_value={'transactionId': 'tx-loser', 'redirect': 'https://pay/l', 'status': 'PENDING'}
                ),
                cancel_subscription=AsyncMock(return_value={'status': 'cancelled'}),
            )

    async with _memory_session(monkeypatch) as db:
        # «Победитель» уже вставился (конкурентный запрос).
        winner = await sub_crud.create_platega_subscription(
            db,
            user_id=1,
            subscription_id=1,
            tariff_id=5,
            interval=3,
            charge_days=30,
            amount_kopeks=19900,
            redirect_url='https://pay/w',
            platega_subscription_id='tx-winner',
            status='PENDING',
        )

        # Симулируем гонку: идемпотентная проверка «проигравшего» прошла до
        # вставки победителя — первый вызов lookup'а возвращает None.
        real_lookup = sub_crud.get_active_platega_subscription_by_subscription
        calls = {'n': 0}

        async def racy_lookup(inner_db, sub_id):
            calls['n'] += 1
            if calls['n'] == 1:
                return None
            return await real_lookup(inner_db, sub_id)

        monkeypatch.setattr(sub_crud, 'get_active_platega_subscription_by_subscription', racy_lookup)

        svc = Svc()
        result = await svc.create_platega_sbp_subscription(db, user_id=1, subscription=subscription, tariff=tariff)

        assert result['local_id'] == winner.id
        assert result['platega_subscription_id'] == 'tx-winner'
        svc.platega_service.cancel_subscription.assert_awaited_once_with('tx-loser')

        rows = await sub_crud.list_platega_subscriptions_by_statuses(db, ['PENDING', 'ACTIVE', 'PAST_DUE'])
        assert len([r for r in rows if r.subscription_id == 1]) == 1  # сирота не вставилась


# --- replay_missed_platega_charges (доначисление потерянных CONFIRMED) ---


async def test_replay_missed_charges_extends_and_advances_counters(monkeypatch):
    """Remote chargesSuccess > локального = потерянные коллбеки: подписка
    продлевается на charge_days за каждое пропущенное списание через штатный
    callback-процессор (аудит-транзакции с синтетическими Id, счётчики)."""
    from datetime import UTC, datetime, timedelta

    from app.services.payment.platega import replay_missed_platega_charges

    async with _memory_session(monkeypatch) as db:
        end0 = datetime.now(UTC) + timedelta(days=2)
        subscription = Subscription(id=1, user_id=1, status='active', end_date=end0)
        db.add(subscription)
        await db.commit()

        rec = await _create_recurring_record(db, platega_subscription_id='ps-replay')

        remote = {
            'status': 'Active',
            'lastChargeAt': (datetime.now(UTC) - timedelta(hours=3)).isoformat(),
            'chargeMetrics': {
                'chargesSuccess': 2,
                'chargesFailed': 0,
                'nextChargeAt': '2026-09-01T00:00:00Z',
            },
        }

        replayed = await replay_missed_platega_charges(db, rec, remote)

        assert replayed == 2
        await db.refresh(subscription)
        await db.refresh(rec)
        assert subscription.end_date >= end0 + timedelta(days=59)  # 2 × 30 дней
        assert rec.charges_success == 2
        assert rec.next_charge_at is not None

        tx_ids = {t.external_id for t in (await db.execute(select(Transaction))).scalars().all()}
        assert 'ps-replay:replay:1' in tx_ids
        assert 'ps-replay:replay:2' in tx_ids

        # Повторный проход — идемпотентен: счётчики уже сошлись.
        assert await replay_missed_platega_charges(db, rec, remote) == 0


async def test_replay_waits_out_fresh_charge_window(monkeypatch):
    """lastChargeAt свежее 2 часов — настоящий коллбек ещё может доехать
    (у него другой Id, дедуп его не поймает) — replay откладывается."""
    from datetime import UTC, datetime, timedelta

    from app.services.payment.platega import replay_missed_platega_charges

    async with _memory_session(monkeypatch) as db:
        rec = await _create_recurring_record(db, platega_subscription_id='ps-fresh')
        remote = {
            'lastChargeAt': (datetime.now(UTC) - timedelta(minutes=30)).isoformat(),
            'chargeMetrics': {'chargesSuccess': 1},
        }
        assert await replay_missed_platega_charges(db, rec, remote) == 0


async def test_replay_noop_without_metrics_or_deficit(monkeypatch):
    from app.services.payment.platega import replay_missed_platega_charges

    async with _memory_session(monkeypatch) as db:
        rec = await _create_recurring_record(db, platega_subscription_id='ps-even')
        assert await replay_missed_platega_charges(db, rec, None) == 0
        assert await replay_missed_platega_charges(db, rec, {'chargeMetrics': {}}) == 0
        rec.charges_success = 3
        assert await replay_missed_platega_charges(db, rec, {'chargeMetrics': {'chargesSuccess': 3}}) == 0
