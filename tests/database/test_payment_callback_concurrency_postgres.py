"""Настоящие обработчики вебхуков под конкуренцией на настоящем PostgreSQL.

Соседний файл ``test_payment_locking_postgres.py`` проверяет, что блокировка
работает, но повторяет критическую секцию своим кодом. Этого мало: если бы в
боевой функции проверки стояли не в том порядке — например, ``processed_events``
записывался бы после ``commit``, снимающего блокировку, — те тесты остались бы
зелёными. Здесь вызывается сам ``process_*_callback``, целиком, с настоящими
пользователем, транзакцией и балансом.

Единственная подмена — обёртка вокруг захвата строки: она не меняет поведение,
а задерживает первую доставку до того момента, когда база увидит вторую в
очереди за блокировкой. Без этого две задачи могли бы выполниться подряд, и
гонки в тесте просто не возникло бы.

Чего эти тесты НЕ доказывают — установлено мутациями, а не предположено:

* Повторная проверка ``processed_events`` под блокировкой и страж
  ``payment.transaction_id`` в точке зачисления оказались избыточными: их
  перекрывает ``if payment.is_paid`` внутри ``_apply_*_success``. Снятие любого
  из них не роняет ни одного теста. Убирать их не нужно — они дёшевы и держат
  оборону при правках соседнего кода. Но говорить про них «покрыто» нельзя.
* Гонка вебхука со сверкой по API проходит и БЕЗ блокировки строки: там от
  двойного зачисления спасают ранние проверки ``payment.is_paid`` в
  ``check_*_payment_status``. Блокировка в этой паре — не единственный рубеж.
* Проверка подписи, ответ 200 до зачисления и фоновая задача из
  ``app/webserver/payments.py`` сюда не входят: здесь вызывается уже
  распакованный обработчик.
* Настоящий API провайдера не участвует. В тесте сверки подменён только поход
  к нему; формат ответа взят из документации и живым обменом не подтверждён.
* Покупка с лендинга (``try_fulfill_guest_purchase``) уходит другой веткой и
  здесь не проверяется.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import func, select

import app.database.crud.paritypay as paritypay_crud
import app.database.crud.tabpay as tabpay_crud
from app.database.models import ParityPayPayment, PaymentMethod, TabPayPayment, Transaction, User
from app.services.payment_service import PaymentService
from tests.fixtures.postgres_db import lock_waiter_appeared, postgres_sessions


pytestmark = pytest.mark.postgres


AMOUNT_KOPEKS = 125000

TABLES = [
    TabPayPayment.__table__,
    ParityPayPayment.__table__,
    Transaction.__table__,
    User.__table__,
]


class Gateway:
    """Различия двух шлюзов, сведённые к одному описанию."""

    def __init__(
        self,
        *,
        name: str,
        crud: Any,
        model: Any,
        method: PaymentMethod,
        callback_attr: str,
        check_attr: str,
        lock_attr: str,
        paid_status: str,
        pending_status: str,
    ) -> None:
        self.name = name
        self.crud = crud
        self.model = model
        self.method = method
        self.callback_attr = callback_attr
        self.check_attr = check_attr
        self.lock_attr = lock_attr
        self.paid_status = paid_status
        self.pending_status = pending_status

    def payload(self, *, order_id: str, external_id: str, status: str) -> dict[str, Any]:
        if self.name == 'tabpay':
            return {
                'id': external_id,
                'orderId': order_id,
                'status': status,
                'amountKopecks': AMOUNT_KOPEKS,
                'test': False,
            }
        # ParityPay присылает сумму в рублях строкой — это часть проверяемого пути.
        return {
            'id': external_id,
            'order_id': order_id,
            'status': status,
            'amount': f'{AMOUNT_KOPEKS / 100:.2f}',
            'credited': AMOUNT_KOPEKS / 100,
        }

    def stub_provider_api(self, monkeypatch: pytest.MonkeyPatch, *, order_id: str, external_id: str) -> None:
        """Подменяет ТОЛЬКО поход к провайдеру: сети в тестах нет.

        Всё остальное в пути сверки — настоящее, включая разбор суммы,
        блокировку строки и зачисление на баланс.
        """
        answer = self.payload(order_id=order_id, external_id=external_id, status=self.paid_status)

        if self.name == 'tabpay':
            from app.services import tabpay_service as module

            async def fake_get_payment(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
                return answer

            monkeypatch.setattr(module.tabpay_service, 'get_payment', fake_get_payment)
            monkeypatch.setattr(module.tabpay_service, 'get_payment_by_order_id', fake_get_payment)
            return

        from app.services import paritypay_service as module

        async def fake_get_invoice(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return answer

        monkeypatch.setattr(module.paritypay_service, 'get_invoice', fake_get_invoice)

    async def create_payment(self, db: Any, *, user_id: int, order_id: str, external_id: str) -> Any:
        return await getattr(self.crud, f'create_{self.name}_payment')(
            db=db,
            user_id=user_id,
            order_id=order_id,
            amount_kopeks=AMOUNT_KOPEKS,
            description='Пополнение баланса',
            payment_url='https://pay.example/x',
            payment_method='sbp',
            **{f'{self.name}_payment_id': external_id},
        )

    async def reread(self, db: Any, payment_id: int) -> Any:
        return await getattr(self.crud, f'get_{self.name}_payment_by_id')(db, payment_id)


GATEWAYS = [
    pytest.param(
        Gateway(
            name='tabpay',
            crud=tabpay_crud,
            model=TabPayPayment,
            method=PaymentMethod.TABPAY,
            callback_attr='process_tabpay_callback',
            check_attr='check_tabpay_payment_status',
            lock_attr='get_tabpay_payment_by_id_for_update',
            paid_status='SUCCESS',
            pending_status='EXPIRED',
        ),
        id='tabpay',
    ),
    pytest.param(
        Gateway(
            name='paritypay',
            crud=paritypay_crud,
            model=ParityPayPayment,
            method=PaymentMethod.PARITYPAY,
            callback_attr='process_paritypay_callback',
            check_attr='check_paritypay_payment_status',
            lock_attr='get_paritypay_payment_by_id_for_update',
            paid_status='PAID',
            pending_status='EXPIRED',
        ),
        id='paritypay',
    ),
]


async def _create_user(db: Any, telegram_id: int = 3000001) -> User:
    user = User(telegram_id=telegram_id, first_name='Тест', language='ru', balance_kopeks=0)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _balance(db: Any, user_id: int) -> int:
    result = await db.execute(select(User.balance_kopeks).where(User.id == user_id))
    return result.scalar_one()


async def _deposit_count(db: Any, user_id: int, method: PaymentMethod) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(Transaction)
        .where(Transaction.user_id == user_id, Transaction.payment_method == method.value)
    )
    return result.scalar_one()


async def _deliver(handler: Any, session: Any, payload: dict[str, Any]) -> bool:
    """Одна доставка вебхука с тем же жизненным циклом сессии, что в проде.

    ``_process_payment_service_callback`` в ``app/webserver/payments.py`` берёт
    на каждую доставку свою сессию и закрывает её в ``finally``. Это не деталь
    оформления: обработчик выходит из «событие уже обработано» под блокировкой,
    НЕ снимая её — строку освобождает именно закрытие сессии. Тест, который
    держит сессию открытой до своего конца, встаёт намертво, потому что
    победитель дописывает ``metadata_json`` уже ПОСЛЕ своего commit, то есть без
    блокировки, и упирается в дубликат.
    """
    try:
        return await handler(session, dict(payload))
    finally:
        await session.rollback()


async def _reconcile(checker: Any, session: Any, order_id: str) -> Any:
    """Один проход фоновой сверки, сессия закрывается так же, как у вебхука."""
    try:
        return await checker(session, order_id)
    finally:
        await session.rollback()


# ---------------------------------------------------------------------------


@pytest.mark.parametrize('gateway', GATEWAYS)
async def test_two_simultaneous_deliveries_credit_balance_once(postgres_database, gateway: Gateway) -> None:
    """Провайдер доставил один вебхук дважды одновременно — баланс вырос один раз.

    Проверяется не модель обработчика, а сам обработчик: два вызова
    ``process_*_callback`` в разных сессиях, с одинаковым телом, с настоящим
    зачислением на баланс в конце.
    """
    order_id = f'{gateway.name}_e2e_1'
    external_id = 'ext-e2e-1'
    payload = gateway.payload(order_id=order_id, external_id=external_id, status=gateway.paid_status)

    service = PaymentService(bot=None)

    async with postgres_sessions(postgres_database, TABLES, count=3) as (first, second, watcher):
        user = await _create_user(first)
        user_id = user.id
        payment = await gateway.create_payment(first, user_id=user_id, order_id=order_id, external_id=external_id)
        payment_id = payment.id

        assert await _balance(watcher, user_id) == 0

        # Инструментовка захвата строки: первая доставка не отпускает её, пока
        # база не увидит вторую в очереди. Настоящая функция вызывается как есть.
        #
        # Отсутствие соперника в очереди тест НЕ роняет на месте, а запоминает.
        # Иначе снятая блокировка ломала бы синхронизацию, тест падал бы с
        # жалобой инструментовки — и из отчёта пропадало бы то единственное,
        # ради чего он написан: «баланс зачислен дважды».
        original_lock = getattr(gateway.crud, gateway.lock_attr)
        holder_has_the_row = asyncio.Event()
        entries = 0
        race_observed = False

        async def instrumented_lock(db: Any, target_id: int) -> Any:
            nonlocal entries, race_observed
            entries += 1
            if entries == 1:
                row = await original_lock(db, target_id)
                holder_has_the_row.set()
                race_observed = await lock_waiter_appeared(watcher)
                return row
            await holder_has_the_row.wait()
            return await original_lock(db, target_id)

        setattr(gateway.crud, gateway.lock_attr, instrumented_lock)
        try:
            handler = getattr(service, gateway.callback_attr)
            results = await asyncio.gather(_deliver(handler, first, payload), _deliver(handler, second, payload))
        finally:
            setattr(gateway.crud, gateway.lock_attr, original_lock)

        # Денежное утверждение идёт ПЕРВЫМ: оно и есть смысл теста.
        assert await _balance(watcher, user_id) == AMOUNT_KOPEKS, 'баланс зачислен не один раз'
        assert await _deposit_count(watcher, user_id, gateway.method) == 1, 'создано больше одной транзакции'
        assert results == [True, True], f'обе доставки должны быть подтверждены: {results}'

        watcher.expunge_all()
        stored = await gateway.reread(watcher, payment_id)
        assert stored.is_paid is True
        assert stored.status == 'success'
        assert stored.processed_events.count(f'{external_id}:{gateway.paid_status}') == 1
        assert stored.transaction_id is not None

        # И только теперь — что гонка вообще состоялась. Если нет, тест выше
        # ничего не доказал, и молчать об этом нельзя.
        assert entries == 2, 'обе доставки обязаны дойти до захвата строки'
        assert race_observed, 'вторая доставка не встала в очередь за блокировкой — конкуренции не возникло'


@pytest.mark.parametrize('gateway', GATEWAYS)
async def test_repeated_delivery_after_success_changes_nothing(postgres_database, gateway: Gateway) -> None:
    """Обычный повтор доставки — не гонка, а частый случай: провайдер шлёт до семи раз."""
    order_id = f'{gateway.name}_e2e_2'
    external_id = 'ext-e2e-2'
    payload = gateway.payload(order_id=order_id, external_id=external_id, status=gateway.paid_status)

    service = PaymentService(bot=None)

    async with postgres_sessions(postgres_database, TABLES, count=2) as (db, watcher):
        user = await _create_user(db)
        user_id = user.id
        await gateway.create_payment(db, user_id=user_id, order_id=order_id, external_id=external_id)

        handler = getattr(service, gateway.callback_attr)
        assert await handler(db, dict(payload)) is True
        assert await _balance(watcher, user_id) == AMOUNT_KOPEKS

        for _ in range(3):
            assert await handler(db, dict(payload)) is True

        assert await _balance(watcher, user_id) == AMOUNT_KOPEKS, 'повтор доставки зачислил баланс ещё раз'
        assert await _deposit_count(watcher, user_id, gateway.method) == 1


@pytest.mark.parametrize('gateway', GATEWAYS)
async def test_late_payment_after_expiry_is_credited_once(postgres_database, gateway: Gateway) -> None:
    """Поздняя оплата: сначала EXPIRED, следом настоящая оплата.

    Оба шлюза намеренно не считают EXPIRED окончательным статусом — QR СБП
    живёт дольше таймаута. Проверяется, что деньги при этом всё-таки доходят,
    и ровно один раз.
    """
    order_id = f'{gateway.name}_e2e_3'
    external_id = 'ext-e2e-3'
    service = PaymentService(bot=None)

    async with postgres_sessions(postgres_database, TABLES, count=2) as (db, watcher):
        user = await _create_user(db)
        user_id = user.id
        await gateway.create_payment(db, user_id=user_id, order_id=order_id, external_id=external_id)

        handler = getattr(service, gateway.callback_attr)

        expired = gateway.payload(order_id=order_id, external_id=external_id, status=gateway.pending_status)
        assert await handler(db, expired) is True
        assert await _balance(watcher, user_id) == 0, 'истёкший счёт не должен зачислять баланс'

        paid = gateway.payload(order_id=order_id, external_id=external_id, status=gateway.paid_status)
        assert await handler(db, paid) is True

        assert await _balance(watcher, user_id) == AMOUNT_KOPEKS, 'поздняя оплата не зачислена'
        assert await _deposit_count(watcher, user_id, gateway.method) == 1


@pytest.mark.parametrize('gateway', GATEWAYS)
async def test_wrong_amount_never_credits_balance(postgres_database, gateway: Gateway) -> None:
    """Сумма из уведомления не сошлась со счётом — баланс не трогаем.

    Статус ``amount_mismatch`` необратим, поэтому важно, что до баланса дело
    не доходит вовсе, а не «зачислили и потом поправим».
    """
    order_id = f'{gateway.name}_e2e_4'
    external_id = 'ext-e2e-4'
    payload = gateway.payload(order_id=order_id, external_id=external_id, status=gateway.paid_status)
    if gateway.name == 'tabpay':
        payload['amountKopecks'] = AMOUNT_KOPEKS - 1
    else:
        payload['amount'] = f'{(AMOUNT_KOPEKS - 100) / 100:.2f}'

    service = PaymentService(bot=None)

    async with postgres_sessions(postgres_database, TABLES, count=2) as (db, watcher):
        user = await _create_user(db)
        user_id = user.id
        payment = await gateway.create_payment(db, user_id=user_id, order_id=order_id, external_id=external_id)

        handler = getattr(service, gateway.callback_attr)
        assert await handler(db, dict(payload)) is False

        assert await _balance(watcher, user_id) == 0
        assert await _deposit_count(watcher, user_id, gateway.method) == 0

        watcher.expunge_all()
        stored = await gateway.reread(watcher, payment.id)
        assert stored.status == 'amount_mismatch'
        assert stored.is_paid is False


@pytest.mark.parametrize('gateway', GATEWAYS)
async def test_webhook_and_api_reconciliation_do_not_double_credit(
    postgres_database,
    gateway: Gateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Вебхук и фоновая сверка по API столкнулись на одном платеже.

    Сценарий не выдуманный: сверка существует ровно на случай потерянного
    вебхука и ходит по расписанию, а «потерянный» вебхук может прийти с
    опозданием именно в этот момент. Пути в коде разные, сторожа разные, а
    зачисление одно и то же — и здесь оно обязано случиться один раз.
    """
    order_id = f'{gateway.name}_race_api'
    external_id = 'ext-race-api'
    payload = gateway.payload(order_id=order_id, external_id=external_id, status=gateway.paid_status)

    gateway.stub_provider_api(monkeypatch, order_id=order_id, external_id=external_id)
    service = PaymentService(bot=None)

    async with postgres_sessions(postgres_database, TABLES, count=3) as (first, second, watcher):
        user = await _create_user(first)
        user_id = user.id
        payment = await gateway.create_payment(first, user_id=user_id, order_id=order_id, external_id=external_id)
        payment_id = payment.id

        original_lock = getattr(gateway.crud, gateway.lock_attr)
        holder_has_the_row = asyncio.Event()
        entries = 0

        async def instrumented_lock(db: Any, target_id: int) -> Any:
            nonlocal entries
            entries += 1
            if entries == 1:
                row = await original_lock(db, target_id)
                holder_has_the_row.set()
                assert await lock_waiter_appeared(watcher), 'сверка не встала в очередь за блокировкой'
                return row
            await holder_has_the_row.wait()
            return await original_lock(db, target_id)

        setattr(gateway.crud, gateway.lock_attr, instrumented_lock)
        try:
            webhook = getattr(service, gateway.callback_attr)
            reconcile = getattr(service, gateway.check_attr)
            await asyncio.gather(
                _deliver(webhook, first, payload),
                _reconcile(reconcile, second, order_id),
            )
        finally:
            setattr(gateway.crud, gateway.lock_attr, original_lock)

        assert entries == 2, 'вебхук и сверка обязаны оба дойти до захвата строки'

        assert await _balance(watcher, user_id) == AMOUNT_KOPEKS, 'баланс зачислен не один раз'
        assert await _deposit_count(watcher, user_id, gateway.method) == 1, 'создано больше одной транзакции'

        watcher.expunge_all()
        stored = await gateway.reread(watcher, payment_id)
        assert stored.is_paid is True
        assert stored.transaction_id is not None
