"""Продление возвращает то, что грейс-оверлей успел затереть в подписке.

Жалоба 2026-09-15 №2 (сторонняя установка, v4.11.0): «после оплаты подписка
продлилась, трафик сбросился, дата обновилась, но исходные сквады не
восстановились — остался сквад грейса». До оплаты мониторинг v4.10–4.11 принял
оверлей грейса за продление и записал его в подписку: сквад грейса, лимит
«расход + 1 ГБ», дату конца грейса; сессию закрыл «человек продлил».

Решение владельца (2026-09-15): такие аккаунты лечит продление, отдельной починки
не нужно. Но продление сквады не трогало вовсе: «Продлить» в кабинете, кнопка
продления в боте, автоплатёж, админские «+N дней» передают только дни — сквад
грейса оставался навсегда. В классическом режиме так же оставался и лимит.

Что было до грейса, записано в грейс-сессии (``billing_before``). Продление
возвращает оттуда ровно те поля, в которых сейчас стоит оверлей, и считает
новый срок от настоящего окончания, а не от конца грейса.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.database.crud.subscription import extend_subscription, reconcile_tariff_traffic_limit
from app.database.models import Base, ServerSquad, Subscription, SubscriptionStatus, Tariff, User
from app.services.grace_access_runtime import _session_to_model
from app.services.grace_access_service import (
    GraceAccessSession,
    GraceBillingState,
    GraceCompletionReason,
    GracePanelOverlay,
    GracePanelSnapshot,
    GraceReason,
    GraceSessionState,
)
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)
GIB = 1024**3
PANEL_ID = 174
SRV_A = 'aaaaaaaa-0000-0000-0000-000000000001'
SRV_B = 'bbbbbbbb-0000-0000-0000-000000000002'
GRACE = 'eeeeeeee-0000-0000-0000-00000000000e'
USED_GB = 7


def _billing(*, end_at: datetime, squads: tuple[str, ...], limit_gb: int, status: str = 'expired') -> GraceBillingState:
    return GraceBillingState(
        subscription_id=10,
        remnawave_id=PANEL_ID,
        status=status,
        end_at=end_at,
        traffic_limit_bytes=limit_gb * GIB,
        used_traffic_bytes=USED_GB * GIB,
        device_limit=3,
        squad_uuids=squads,
    )


def _session(
    *,
    started_at: datetime,
    billing_before: GraceBillingState,
    completion: GraceCompletionReason | None = GraceCompletionReason.PAID,
) -> GraceAccessSession:
    grace_until = started_at + timedelta(hours=72)
    return GraceAccessSession(
        id=f'00000000-0000-0000-0000-{int(started_at.timestamp()):012d}',
        subscription_id=10,
        remnawave_id=PANEL_ID,
        reason=GraceReason.EXPIRED,
        incident_key=f'expired:{billing_before.end_at.isoformat()}',
        state=GraceSessionState.COMPLETED if completion else GraceSessionState.ACTIVE,
        billing_before=billing_before,
        panel_before=GracePanelSnapshot(
            remnawave_id=PANEL_ID,
            status='EXPIRED',
            expire_at=billing_before.end_at,
            traffic_limit_bytes=billing_before.traffic_limit_bytes,
            used_traffic_bytes=USED_GB * GIB,
            squad_uuids=billing_before.squad_uuids,
        ),
        overlay=GracePanelOverlay(
            status='ACTIVE',
            expire_at=grace_until,
            traffic_limit_bytes=(USED_GB + 1) * GIB,
            squad_uuids=(GRACE,),
        ),
        started_at=started_at,
        grace_until=grace_until,
        updated_at=started_at,
        completion_reason=completion,
        completed_at=started_at + timedelta(minutes=30) if completion else None,
    )


def _tariff(*, limit_gb: int) -> Tariff:
    return Tariff(
        id=1,
        name='Стартовый',
        description='',
        is_active=True,
        traffic_limit_gb=limit_gb,
        device_limit=3,
        allowed_squads=[SRV_A],
        period_prices={'30': 10_000},
        display_order=1,
    )


def _leaked_subscription(*, grace_until: datetime, tariff_id: int | None) -> Subscription:
    """Подписка после v4.11.0: в ней оверлей — дата конца грейса, сквад грейса, «расход + 1 ГБ»."""
    return Subscription(
        id=10,
        user_id=1,
        remnawave_short_id='sub10',
        remnawave_id=PANEL_ID,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        start_date=grace_until - timedelta(days=33),
        end_date=grace_until,
        traffic_limit_gb=USED_GB + 1,
        traffic_used_gb=float(USED_GB),
        purchased_traffic_gb=0,
        device_limit=3,
        tariff_id=tariff_id,
        connected_squads=[GRACE],
    )


@pytest.fixture(autouse=True)
def _mode(monkeypatch):
    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', True)
    monkeypatch.setattr(settings, 'TRAFFIC_SELECTION_MODE', 'selectable')


def _server(squad_uuid: str, *, is_available: bool) -> ServerSquad:
    """Сервер в списке бота. Синхронизация с панелью заносит туда все сквады, новые — скрытыми."""
    return ServerSquad(squad_uuid=squad_uuid, display_name=squad_uuid[:8], is_available=is_available, price_kopeks=1000)


def _renewed_on_old_code(*, grace_until: datetime, tariff_id: int | None) -> Subscription:
    """Жалоба №2 как она есть: на v4.11 человек уже заплатил, и продление ушло от конца грейса.

    Дата — «конец грейса + 30 дней», сквад грейса остался. Опорная дата оверлея
    сдвинута оплатой, остаётся сам сквад грейса.
    """
    subscription = _leaked_subscription(grace_until=grace_until, tariff_id=tariff_id)
    subscription.end_date = grace_until + timedelta(days=30)
    return subscription


async def _seed(db, *rows) -> None:
    db.add(User(id=1, telegram_id=1001, first_name='U', language='ru', status='active', balance_kopeks=0))
    for row in rows:
        db.add(row)
    await db.commit()


@pytest.mark.asyncio
async def test_tariff_renewal_returns_the_squad_and_limit_the_grace_overlay_overwrote(monkeypatch) -> None:
    """Сценарий жалобы №2: «Продлить» в кабинете — только дни."""
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    now = datetime.now(UTC)
    real_end = now - timedelta(hours=1)
    started = real_end + timedelta(seconds=30)
    session = _session(started_at=started, billing_before=_billing(end_at=real_end, squads=(SRV_A,), limit_gb=0))
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, _tariff(limit_gb=0))
        subscription = _leaked_subscription(grace_until=session.grace_until, tariff_id=1)
        await _seed_rows(db, subscription, _session_to_model(session))

        await extend_subscription(db, subscription, 30)
        await db.refresh(subscription)

    assert subscription.connected_squads == [SRV_A], 'сквад грейса остался после оплаты'
    assert subscription.traffic_limit_gb == 0, 'безлимит тарифа не вернулся'
    new_end = subscription.end_date.replace(tzinfo=UTC)
    assert abs((new_end - (now + timedelta(days=30))).total_seconds()) < 120, (
        f'срок считается от конца грейса: {new_end}'
    )
    assert subscription.status == SubscriptionStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_classic_renewal_returns_the_chosen_servers_and_their_limit(monkeypatch) -> None:
    """Классика: серверы выбирал человек, лимит не из тарифа — вернуть можно только из сессии."""
    monkeypatch.setattr(settings, 'SALES_MODE', 'classic')
    now = datetime.now(UTC)
    real_end = now - timedelta(hours=1)
    session = _session(
        started_at=real_end + timedelta(seconds=30),
        billing_before=_billing(end_at=real_end, squads=(SRV_A, SRV_B), limit_gb=100),
    )
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        subscription = _leaked_subscription(grace_until=session.grace_until, tariff_id=None)
        await _seed_rows(db, subscription, _session_to_model(session))

        await extend_subscription(db, subscription, 30)
        await db.refresh(subscription)

    assert sorted(subscription.connected_squads) == sorted([SRV_A, SRV_B])
    assert subscription.traffic_limit_gb == 100, 'в классике лимит грейса остался бы навсегда'


@pytest.mark.asyncio
async def test_second_grace_that_snapshotted_the_echo_does_not_become_the_source(monkeypatch) -> None:
    """Не продлил сразу: у «даты грейса» выдан второй грейс, и его снимок уже с оверлеем.

    Источник — последний снимок, который сам не оверлей; иначе продление «вернуло»
    бы сквад грейса.
    """
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    now = datetime.now(UTC)
    real_end = now - timedelta(days=4)
    first = _session(
        started_at=real_end + timedelta(seconds=30),
        billing_before=_billing(end_at=real_end, squads=(SRV_A,), limit_gb=0),
    )
    echo_end = first.grace_until
    second = _session(
        started_at=echo_end + timedelta(seconds=30),
        billing_before=_billing(end_at=echo_end, squads=(GRACE,), limit_gb=USED_GB + 1),
        completion=GraceCompletionReason.TIMEOUT,
    )
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, _tariff(limit_gb=0))
        subscription = _leaked_subscription(grace_until=echo_end, tariff_id=1)
        subscription.status = SubscriptionStatus.EXPIRED.value
        await _seed_rows(db, subscription, _session_to_model(first), _session_to_model(second))

        await extend_subscription(db, subscription, 30)
        await db.refresh(subscription)

    assert subscription.connected_squads == [SRV_A]
    assert subscription.traffic_limit_gb == 0


@pytest.mark.asyncio
async def test_cart_that_already_set_the_tariff_squads_still_counts_from_the_real_end(monkeypatch) -> None:
    """Автопокупка из корзины кабинета ставит сквады тарифа ДО продления.

    Стенд обновления v4.11.0 → новая (2026-09-15): по сквадам подписку уже не узнать,
    а дата конца грейса в ней осталась — новый срок считался от неё (+3 дня даром).
    Дата конца грейса — самостоятельный признак.
    """
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    now = datetime.now(UTC)
    real_end = now - timedelta(hours=1)
    session = _session(
        started_at=real_end + timedelta(seconds=30),
        billing_before=_billing(end_at=real_end, squads=(SRV_A,), limit_gb=0),
    )
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, _tariff(limit_gb=0))
        subscription = _leaked_subscription(grace_until=session.grace_until, tariff_id=1)
        subscription.connected_squads = [SRV_A]  # корзина уже поставила сквады тарифа
        await _seed_rows(db, subscription, _session_to_model(session))

        await extend_subscription(db, subscription, 30)
        await db.refresh(subscription)

    new_end = subscription.end_date.replace(tzinfo=UTC)
    assert abs((new_end - (now + timedelta(days=30))).total_seconds()) < 120, f'срок от конца грейса: {new_end}'
    assert subscription.connected_squads == [SRV_A]
    assert subscription.traffic_limit_gb == 0


@pytest.mark.asyncio
async def test_squads_changed_after_grace_are_left_alone(monkeypatch) -> None:
    """Сквады уже не оверлей (админ поменял, человек купил другой тариф) — продление их не трогает."""
    monkeypatch.setattr(settings, 'SALES_MODE', 'classic')
    now = datetime.now(UTC)
    real_end = now - timedelta(days=10)
    session = _session(
        started_at=real_end + timedelta(seconds=30),
        billing_before=_billing(end_at=real_end, squads=(SRV_A,), limit_gb=100),
    )
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        subscription = _leaked_subscription(grace_until=now + timedelta(days=5), tariff_id=None)
        subscription.connected_squads = [SRV_B]
        subscription.traffic_limit_gb = 200
        await _seed_rows(db, subscription, _session_to_model(session))

        await extend_subscription(db, subscription, 30)
        await db.refresh(subscription)

    assert subscription.connected_squads == [SRV_B]
    assert subscription.traffic_limit_gb == 200
    assert subscription.end_date.replace(tzinfo=UTC) > now + timedelta(days=34), 'живой срок продлён от своей даты'


@pytest.mark.asyncio
async def test_recurring_charge_order_counts_the_new_period_from_the_real_end(monkeypatch) -> None:
    """Lava и Platega продлевают методом модели: починка идёт до него, как в самих шлюзах."""
    from app.services.grace_access_echo import undo_grace_overlay_echo

    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    now = datetime.now(UTC)
    real_end = now - timedelta(hours=1)
    session = _session(
        started_at=real_end + timedelta(seconds=30),
        billing_before=_billing(end_at=real_end, squads=(SRV_A,), limit_gb=0),
    )
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, _tariff(limit_gb=0))
        subscription = _leaked_subscription(grace_until=session.grace_until, tariff_id=1)
        await _seed_rows(db, subscription, _session_to_model(session))

        await undo_grace_overlay_echo(db, subscription)
        subscription.extend_subscription(30)
        await reconcile_tariff_traffic_limit(db, subscription)
        await db.commit()
        await db.refresh(subscription)

    assert subscription.connected_squads == [SRV_A]
    assert subscription.traffic_limit_gb == 0
    new_end = subscription.end_date.replace(tzinfo=UTC)
    assert abs((new_end - (now + timedelta(days=30))).total_seconds()) < 120


@pytest.mark.asyncio
async def test_fractional_usage_limit_echo_is_recognised(monkeypatch) -> None:
    """Ревью 2026-09-15: лимит приходил в подписку округлённым вниз — «расход 7,3 + 1» = 8 ГБ.

    На стенде расход был ровно 7 ГБ, и сравнение в байтах срабатывало; в жизни
    расход дробный, и в классике лимит грейса оставался навсегда.
    """
    monkeypatch.setattr(settings, 'SALES_MODE', 'classic')
    now = datetime.now(UTC)
    real_end = now - timedelta(hours=1)
    session = _session(
        started_at=real_end + timedelta(seconds=30),
        billing_before=_billing(end_at=real_end, squads=(SRV_A, SRV_B), limit_gb=100),
    )
    session = replace(session, overlay=replace(session.overlay, traffic_limit_bytes=int(8.3 * GIB)))
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        subscription = _leaked_subscription(grace_until=session.grace_until, tariff_id=None)
        subscription.traffic_limit_gb = 8  # int(8,3 ГиБ) — как его переносил импорт
        await _seed_rows(db, subscription, _session_to_model(session))

        await extend_subscription(db, subscription, 30)
        await db.refresh(subscription)

    assert subscription.traffic_limit_gb == 100


@pytest.mark.asyncio
async def test_grace_squad_that_is_also_sold_does_not_trigger_a_false_repair(monkeypatch) -> None:
    """Ревью 2026-09-15: сквад грейса — обычный продаваемый сервер, человек честно на нём.

    Был на «премиуме» (A), истёк, получил грейс (сквад G), потом честно перешёл на
    тариф с G. Сквады совпадают с оверлеем, но дата — нет: это не эхо, трогать нельзя.
    """
    monkeypatch.setattr(settings, 'SALES_MODE', 'classic')
    now = datetime.now(UTC)
    real_end = now - timedelta(days=10)
    session = _session(
        started_at=real_end + timedelta(seconds=30),
        billing_before=_billing(end_at=real_end, squads=(SRV_A,), limit_gb=100),
    )
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, _server(GRACE, is_available=True))
        subscription = _leaked_subscription(grace_until=now + timedelta(days=12), tariff_id=None)
        subscription.traffic_limit_gb = 50
        await _seed_rows(db, subscription, _session_to_model(session))

        await extend_subscription(db, subscription, 30)
        await db.refresh(subscription)

    assert subscription.connected_squads == [GRACE], 'честно купленный сервер отобран'
    assert subscription.traffic_limit_gb == 50


@pytest.mark.asyncio
async def test_server_added_by_a_purchase_stays_next_to_the_returned_ones(monkeypatch) -> None:
    """Простая покупка добавляет свой сервер к сквадам подписки: {G, X} → свои серверы + X."""
    from app.services.grace_access_echo import undo_grace_overlay_echo

    monkeypatch.setattr(settings, 'SALES_MODE', 'classic')
    now = datetime.now(UTC)
    real_end = now - timedelta(hours=1)
    session = _session(
        started_at=real_end + timedelta(seconds=30),
        billing_before=_billing(end_at=real_end, squads=(SRV_A,), limit_gb=100),
    )
    extra = 'cccccccc-0000-0000-0000-000000000003'
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db)
        subscription = _leaked_subscription(grace_until=session.grace_until, tariff_id=None)
        subscription.connected_squads = [GRACE, extra]
        await _seed_rows(db, subscription, _session_to_model(session))

        changed = await undo_grace_overlay_echo(db, subscription)

    assert subscription.connected_squads == [SRV_A, extra]
    assert {'connected_squads', 'end_date'} <= changed


async def _seed_rows(db, *rows) -> None:
    for row in rows:
        db.add(row)
    await db.commit()


@pytest.mark.asyncio
async def test_classic_renewal_price_counts_the_real_servers_not_the_grace_squad(monkeypatch) -> None:
    """Цена считается до продления: по скваду грейса она выходила без своих серверов.

    Стенд обновления (2026-09-15): «Сервер не найден в БД» на скваде грейса — человек
    платил только базу, а продление возвращало ему оба сервера.
    """
    from app.database.models import ServerSquad
    from app.services.pricing_engine import pricing_engine

    monkeypatch.setattr(settings, 'SALES_MODE', 'classic')
    now = datetime.now(UTC)
    real_end = now - timedelta(hours=1)
    session = _session(
        started_at=real_end + timedelta(seconds=30),
        billing_before=_billing(end_at=real_end, squads=(SRV_A, SRV_B), limit_gb=100),
    )
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(
            db,
            ServerSquad(squad_uuid=SRV_A, display_name='A', is_available=True, price_kopeks=5000),
            ServerSquad(squad_uuid=SRV_B, display_name='B', is_available=True, price_kopeks=7000),
        )
        leaked = _leaked_subscription(grace_until=session.grace_until, tariff_id=None)
        await _seed_rows(db, leaked, _session_to_model(session))
        leaked_price = await pricing_engine.calculate_renewal_price(db, leaked, 30)

        clean = _leaked_subscription(grace_until=now + timedelta(days=3), tariff_id=None)
        clean.id, clean.remnawave_short_id, clean.remnawave_id = 11, 'sub11', PANEL_ID + 1
        clean.connected_squads, clean.traffic_limit_gb = [SRV_A, SRV_B], 100
        await _seed_rows(db, clean)
        clean_price = await pricing_engine.calculate_renewal_price(db, clean, 30)

    assert leaked_price.final_total == clean_price.final_total
    assert leaked_price.final_total > 0


@pytest.mark.asyncio
@pytest.mark.parametrize('sales_mode', ['classic', 'tariffs'])
async def test_paid_on_old_code_the_next_renewal_returns_the_servers(monkeypatch, sales_mode) -> None:
    """Жалоба №2: заплатил ещё на v4.11 — сквад грейса остался, дата уже не грейса.

    Узнаём по скваду: список серверов — ровно сквад грейса, а его нигде не продают
    (в списке бота он скрытый, как его заносит синхронизация с панелью, и ни в одном
    тарифе). Получить такой сквад подписка могла только от грейса. Дату не трогаем:
    её сдвинула оплата, эти дни человек купил.
    """
    monkeypatch.setattr(settings, 'SALES_MODE', sales_mode)
    now = datetime.now(UTC)
    real_end = now - timedelta(days=5)
    session = _session(
        started_at=real_end + timedelta(seconds=30),
        billing_before=_billing(end_at=real_end, squads=(SRV_A,), limit_gb=0 if sales_mode == 'tariffs' else 100),
    )
    tariff_id = 1 if sales_mode == 'tariffs' else None
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, _tariff(limit_gb=0), _server(SRV_A, is_available=True), _server(GRACE, is_available=False))
        subscription = _renewed_on_old_code(grace_until=session.grace_until, tariff_id=tariff_id)
        paid_end = subscription.end_date
        await _seed_rows(db, subscription, _session_to_model(session))

        await extend_subscription(db, subscription, 30)
        await db.refresh(subscription)

    assert subscription.connected_squads == [SRV_A], 'сквад грейса остался и после второй оплаты'
    assert subscription.traffic_limit_gb == (0 if sales_mode == 'tariffs' else 100)
    new_end = subscription.end_date.replace(tzinfo=UTC)
    assert abs((new_end - (paid_end + timedelta(days=30))).total_seconds()) < 5, f'оплаченные дни пропали: {new_end}'


@pytest.mark.asyncio
async def test_grace_squad_that_a_tariff_sells_is_left_alone(monkeypatch) -> None:
    """Сквад грейса входит в тариф — на нём можно быть честно, по скваду не узнать."""
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    now = datetime.now(UTC)
    real_end = now - timedelta(days=5)
    session = _session(
        started_at=real_end + timedelta(seconds=30),
        billing_before=_billing(end_at=real_end, squads=(SRV_A,), limit_gb=0),
    )
    selling = _tariff(limit_gb=0)
    selling.id, selling.name, selling.allowed_squads = 2, 'Телеграм', [GRACE]
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, _tariff(limit_gb=0), selling, _server(GRACE, is_available=False))
        subscription = _renewed_on_old_code(grace_until=session.grace_until, tariff_id=2)
        await _seed_rows(db, subscription, _session_to_model(session))

        await extend_subscription(db, subscription, 30)
        await db.refresh(subscription)

    assert subscription.connected_squads == [GRACE], 'сквад, который продаёт тариф, отобран'


@pytest.mark.asyncio
async def test_grace_squad_next_to_a_bought_server_is_not_recognised_without_the_date(monkeypatch) -> None:
    """Серверы — не ровно сквад грейса: без даты оверлея это уже не эхо, трогать нельзя."""
    monkeypatch.setattr(settings, 'SALES_MODE', 'classic')
    now = datetime.now(UTC)
    real_end = now - timedelta(days=5)
    session = _session(
        started_at=real_end + timedelta(seconds=30),
        billing_before=_billing(end_at=real_end, squads=(SRV_A,), limit_gb=100),
    )
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed(db, _server(SRV_B, is_available=True), _server(GRACE, is_available=False))
        subscription = _renewed_on_old_code(grace_until=session.grace_until, tariff_id=None)
        subscription.connected_squads = [GRACE, SRV_B]
        await _seed_rows(db, subscription, _session_to_model(session))

        await extend_subscription(db, subscription, 30)
        await db.refresh(subscription)

    assert set(subscription.connected_squads) == {GRACE, SRV_B}
