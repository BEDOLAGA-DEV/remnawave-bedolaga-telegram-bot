"""Ручное пополнение баланса через API.

Главное здесь — идемпотентность: эндпоинт рассчитан на автоматического вызывающего
(AI-агент поддержки), а тот ретраит по таймауту. Без ключа каждый ретрай был бы
вторым начислением. Плюс проверяем, что деньги и транзакция ложатся одним коммитом
и что запускается тот же пост-топап конвейер, что и у настоящего платежа.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.database.models import (
    PaymentMethod,
    PromoGroup,
    Subscription,
    Tariff,
    Transaction,
    TransactionType,
    User,
    UserPromoGroup,
    UserStatus,
    tariff_promo_groups,
)
from app.services import manual_topup_service
from app.services.manual_topup_service import credit_manual_topup
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    User.__table__,
    Subscription.__table__,
    Tariff.__table__,
    PromoGroup.__table__,
    UserPromoGroup.__table__,
    tariff_promo_groups,
    Transaction.__table__,
)


@pytest.fixture(autouse=True)
def _mute_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Побочные эффекты (рефералка, уведомления, корзина) тестируются отдельно."""

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(manual_topup_service, '_apply_referral_topup', _noop)
    monkeypatch.setattr(manual_topup_service, '_notify_admins', _noop)
    monkeypatch.setattr(manual_topup_service, '_notify_user', _noop)
    monkeypatch.setattr(manual_topup_service, 'emit_transaction_side_effects', _noop)


async def _seed_user(db, balance_kopeks: int = 0) -> User:
    user = User(
        telegram_id=555001,
        username='deposit_target',
        first_name='Target',
        status=UserStatus.ACTIVE.value,
        language='ru',
        balance_kopeks=balance_kopeks,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _transactions_of(db, user_id: int) -> list[Transaction]:
    result = await db.execute(select(Transaction).where(Transaction.user_id == user_id).order_by(Transaction.id))
    return list(result.scalars().all())


async def test_credits_balance_and_records_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _seed_user(db, balance_kopeks=5000)

        result = await credit_manual_topup(db, user, 15000, description='Компенсация по тикету 42')

        assert result.duplicate is False
        assert result.old_balance_kopeks == 5000
        assert result.new_balance_kopeks == 20000

        transactions = await _transactions_of(db, user.id)
        assert len(transactions) == 1
        assert transactions[0].type == TransactionType.DEPOSIT.value
        assert transactions[0].amount_kopeks == 15000
        assert transactions[0].payment_method == PaymentMethod.MANUAL.value
        assert transactions[0].description == 'Компенсация по тикету 42'


async def test_repeat_with_same_key_does_not_credit_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ретрай агента после таймаута не должен стать вторым начислением."""
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _seed_user(db)

        first = await credit_manual_topup(db, user, 10000, description='Пополнение', idempotency_key='ticket-42')
        second = await credit_manual_topup(db, user, 10000, description='Пополнение', idempotency_key='ticket-42')

        assert first.duplicate is False
        assert second.duplicate is True
        assert second.transaction.id == first.transaction.id
        assert second.new_balance_kopeks == 10000

        assert len(await _transactions_of(db, user.id)) == 1

        refreshed = await db.get(User, user.id)
        assert refreshed.balance_kopeks == 10000


async def test_same_key_with_other_amount_is_a_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _seed_user(db)

        await credit_manual_topup(db, user, 10000, description='Пополнение', idempotency_key='ticket-42')

        with pytest.raises(manual_topup_service.ManualTopupKeyConflict):
            await credit_manual_topup(db, user, 20000, description='Пополнение', idempotency_key='ticket-42')

        refreshed = await db.get(User, user.id)
        assert refreshed.balance_kopeks == 10000


async def test_same_key_for_another_user_is_a_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ключи глобальны: переиспользованный номер тикета не должен «зачислить» чужому."""
    async with memory_session(monkeypatch, TABLES) as db:
        first_user = await _seed_user(db)
        second_user = User(
            telegram_id=555002,
            username='other',
            first_name='Other',
            status=UserStatus.ACTIVE.value,
            language='ru',
            balance_kopeks=0,
        )
        db.add(second_user)
        await db.commit()
        await db.refresh(second_user)

        await credit_manual_topup(db, first_user, 10000, description='Пополнение', idempotency_key='ticket-42')

        with pytest.raises(manual_topup_service.ManualTopupKeyConflict):
            await credit_manual_topup(db, second_user, 10000, description='Пополнение', idempotency_key='ticket-42')

        assert (await db.get(User, second_user.id)).balance_kopeks == 0
        assert (await db.get(User, first_user.id)).balance_kopeks == 10000


async def test_different_keys_credit_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _seed_user(db)

        await credit_manual_topup(db, user, 10000, description='Раз', idempotency_key='a')
        await credit_manual_topup(db, user, 10000, description='Два', idempotency_key='b')

        refreshed = await db.get(User, user.id)
        assert refreshed.balance_kopeks == 20000
        assert len(await _transactions_of(db, user.id)) == 2


async def test_without_key_every_call_credits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без ключа защиты нет — это осознанный режим, а не забытая проверка."""
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _seed_user(db)

        await credit_manual_topup(db, user, 10000, description='Пополнение')
        await credit_manual_topup(db, user, 10000, description='Пополнение')

        refreshed = await db.get(User, user.id)
        assert refreshed.balance_kopeks == 20000
        assert len(await _transactions_of(db, user.id)) == 2


async def test_key_is_namespaced_and_does_not_clash_with_plain_manual_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Обычные ручные корректировки пишут external_id=NULL и не мешают ключам."""
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _seed_user(db)

        from app.database.crud.transaction import create_transaction

        for _ in range(2):
            await create_transaction(
                db=db,
                user_id=user.id,
                type=TransactionType.DEPOSIT,
                amount_kopeks=100,
                description='Корректировка через веб-API',
                payment_method=PaymentMethod.MANUAL,
            )

        result = await credit_manual_topup(db, user, 10000, description='Пополнение', idempotency_key='ticket-7')

        assert result.duplicate is False
        assert result.transaction.external_id == 'manual:ticket-7'


async def test_rejects_non_positive_amount(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _seed_user(db)

        for amount in (0, -100):
            with pytest.raises(ValueError):
                await credit_manual_topup(db, user, amount, description='Пополнение')

        refreshed = await db.get(User, user.id)
        assert refreshed.balance_kopeks == 0


async def test_notification_failure_does_not_fail_the_deposit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Упавший Telegram не должен ни откатывать деньги, ни отдавать вызывающему ошибку.

    Ошибка после зачисления читается агентом как «не начислилось» → ретрай → двойное
    списание из кассы, если ключа идемпотентности не передали.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _seed_user(db)

        async def _boom(*args, **kwargs):
            raise RuntimeError('telegram is down')

        monkeypatch.setattr(manual_topup_service, '_notify_admins', _boom)

        result = await credit_manual_topup(db, user, 10000, description='Пополнение')

        assert result.duplicate is False
        assert result.new_balance_kopeks == 10000

        refreshed = await db.get(User, user.id)
        assert refreshed.balance_kopeks == 10000
        assert len(await _transactions_of(db, user.id)) == 1


async def test_bonuses_disabled_skips_post_topup_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _seed_user(db)
        calls: list[str] = []

        async def _referral(*args, **kwargs):
            calls.append('referral')

        monkeypatch.setattr(manual_topup_service, '_apply_referral_topup', _referral)

        await credit_manual_topup(db, user, 10000, description='Пополнение', apply_topup_bonuses=False)

        assert calls == []

        await credit_manual_topup(db, user, 10000, description='Пополнение', apply_topup_bonuses=True)
        assert calls == ['referral']


async def test_notify_user_gate_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _seed_user(db)
        calls: list[str] = []

        async def _notify(*args, **kwargs):
            calls.append('notify')

        monkeypatch.setattr(manual_topup_service, '_notify_user', _notify)

        await credit_manual_topup(db, user, 10000, description='Пополнение', notify_user=False)
        assert calls == []

        await credit_manual_topup(db, user, 10000, description='Пополнение', notify_user=True)
        assert calls == ['notify']


async def test_email_notification_not_duplicated_by_cart_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """Письмо шлёт _notify_user; общий пост-топап хелпер не должен слать второе."""
    async with memory_session(monkeypatch, TABLES) as db:
        user = await _seed_user(db)
        seen: list[bool] = []

        async def _fake_cart(_user, _amount, _db, _bot, *, notify_email=True):
            seen.append(notify_email)
            return False

        import app.services.payment.common as payment_common

        monkeypatch.setattr(payment_common, 'send_cart_notification_after_topup', _fake_cart)

        await credit_manual_topup(db, user, 10000, description='Пополнение')

        assert seen == [False]


def test_external_id_fits_the_column() -> None:
    """Ключ из схемы (<=200) плюс префикс обязан влезать в external_id String(255)."""
    from app.webapi.schemas.users import BalanceDepositRequest

    max_key_length = next(
        meta.max_length
        for meta in BalanceDepositRequest.model_fields['idempotency_key'].metadata
        if getattr(meta, 'max_length', None)
    )
    built = manual_topup_service.build_manual_topup_external_id('x' * max_key_length)

    assert len(built) <= Transaction.__table__.c.external_id.type.length


def test_deposit_route_is_registered() -> None:
    """Роут должен реально висеть на users-роутере (он включается с префиксом /users)."""
    from app.webapi.routes import users as users_route

    registered = {(tuple(sorted(route.methods)), route.path) for route in users_route.router.routes}

    assert (('POST',), '/{user_id}/deposit') in registered
    # Прежняя корректировка баланса остаётся на месте — её семантику мы не меняли.
    assert (('POST',), '/{user_id}/balance') in registered


async def test_deposit_route_rejects_amount_above_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    from app.config import settings
    from app.webapi.routes import users as users_route
    from app.webapi.schemas.users import BalanceDepositRequest

    monkeypatch.setattr(settings, 'WEB_API_MANUAL_DEPOSIT_MAX_KOPEKS', 100000)

    payload = BalanceDepositRequest(amount_kopeks=100001)

    with pytest.raises(HTTPException) as exc:
        await users_route.deposit_balance(user_id=1, payload=payload, token=None, db=SimpleNamespace())

    assert exc.value.status_code == 400


async def test_deposit_route_conflicts_on_key_reuse_with_other_amount(monkeypatch: pytest.MonkeyPatch) -> None:
    """Тот же ключ с другой суммой — ошибка вызывающего, а не тихий «дубликат»."""
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from app.config import settings
    from app.webapi.routes import users as users_route
    from app.webapi.schemas.users import BalanceDepositRequest

    monkeypatch.setattr(settings, 'WEB_API_MANUAL_DEPOSIT_MAX_KOPEKS', 0)
    monkeypatch.setattr(users_route, '_open_bot', lambda: None)
    monkeypatch.setattr(
        users_route,
        '_get_user_by_id_or_telegram_id',
        AsyncMock(return_value=SimpleNamespace(id=1, telegram_id=555001)),
    )
    monkeypatch.setattr(
        users_route,
        'credit_manual_topup',
        AsyncMock(
            side_effect=manual_topup_service.ManualTopupKeyConflict(
                SimpleNamespace(id=9, user_id=1, amount_kopeks=50000)
            )
        ),
    )

    payload = BalanceDepositRequest(amount_kopeks=10000, idempotency_key='ticket-42')

    with pytest.raises(HTTPException) as exc:
        await users_route.deposit_balance(user_id=1, payload=payload, token=None, db=SimpleNamespace())

    assert exc.value.status_code == 409
