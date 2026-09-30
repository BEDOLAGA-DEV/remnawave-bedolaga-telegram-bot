"""оплата Tribute не теряется.

Раньше транзакция коммитилась до зачисления, а любая ошибка глоталась и вебхук отвечал 200: сбой между
шагами оставлял транзакцию без денег, повтор считался «уже обработанным», Tribute не повторял вовсе.
Теперь транзакция и баланс — один коммит, ошибка уходит наружу (вебхук отвечает 5xx, Tribute повторяет),
повтор после сбоя зачисляет ровно один раз, повтор после успеха — ничего. Оплату, которую некому зачислить,
не повторяем, а сообщаем логгером, который доходит до журнала ошибок и админ-чата.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select

from app.database.models import (
    PromoGroup,
    Subscription,
    Tariff,
    Transaction,
    User,
    UserPromoGroup,
    tariff_promo_groups,
)
from app.services.tribute_service import TributeService
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    User.__table__,
    Transaction.__table__,
    Subscription.__table__,
    Tariff.__table__,
    PromoGroup.__table__,
    UserPromoGroup.__table__,
    tariff_promo_groups,
)

TG_ID = 5551234


def _donation(telegram_user_id: int = TG_ID, amount: int = 15000) -> str:
    return json.dumps(
        {
            'name': 'new_donation',
            'created_at': '2026-09-16T10:00:00Z',
            'sent_at': '2026-09-16T10:00:01Z',
            'payload': {'donation_request_id': 777, 'amount': amount, 'telegram_user_id': telegram_user_id},
        }
    )


async def _state(db) -> tuple[int, int]:
    """(баланс, число транзакций) — после отката, как увидит следующая доставка."""
    await db.rollback()
    balance = (await db.execute(select(User.balance_kopeks).where(User.telegram_id == TG_ID))).scalar_one()
    count = (await db.execute(select(func.count()).select_from(Transaction))).scalar_one()
    return balance, count


@pytest.fixture
def service_on(monkeypatch):
    """TributeService над тестовой сессией; уведомления и рефералка заглушены."""

    def make(db):
        async def fake_get_db():
            yield db

        monkeypatch.setattr('app.services.tribute_service.get_db', fake_get_db)
        side_effects = AsyncMock()
        monkeypatch.setattr('app.services.tribute_service.emit_transaction_side_effects', side_effects)
        monkeypatch.setattr('app.services.referral_service.process_referral_topup', AsyncMock())
        monkeypatch.setattr(
            'app.services.admin_notification_service.AdminNotificationService', MagicMock(return_value=AsyncMock())
        )
        alert = MagicMock()
        monkeypatch.setattr('app.services.tribute_service.alert_logger', alert)
        service = TributeService(bot=AsyncMock())
        monkeypatch.setattr(service, '_cleanup_invoice_message', AsyncMock())
        monkeypatch.setattr(service, '_send_success_notification', AsyncMock())
        return service, side_effects, alert

    return make


async def _seed_user(db) -> None:
    db.add(User(telegram_id=TG_ID, username='payer', balance_kopeks=0, language='ru'))
    await db.commit()


async def test_failure_before_commit_leaves_nothing_and_retry_credits_once(monkeypatch, service_on):
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed_user(db)
        service, side_effects, alert = service_on(db)

        # Сбой после записи транзакции, до зачисления
        with patch('app.database.crud.user.lock_user_for_update', AsyncMock(side_effect=RuntimeError('db down'))):
            with pytest.raises(RuntimeError):
                await service.process_webhook(_donation())
        assert await _state(db) == (0, 0)
        side_effects.assert_not_awaited()
        alert.error.assert_called_once()  # сбой виден в журнале ошибок и админ-чате, а не только в stdout

        # Повтор доставки Tribute — зачисляется
        assert (await service.process_webhook(_donation()))['status'] == 'ok'
        assert await _state(db) == (15000, 1)
        side_effects.assert_awaited_once()

        # Ещё один повтор — идемпотентно
        assert (await service.process_webhook(_donation()))['status'] == 'ok'
        assert await _state(db) == (15000, 1)


async def test_unknown_user_raises_alert_and_writes_nothing(monkeypatch, service_on):
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed_user(db)
        service, _, alert = service_on(db)

        assert (await service.process_webhook(_donation(telegram_user_id=999)))['status'] == 'ok'
        assert (await service.process_webhook(json.dumps({'name': 'new_donation', 'payload': {'amount': 500}})))[
            'status'
        ] == 'ignored'

        assert await _state(db) == (0, 0)
    assert alert.error.call_count == 2
    assert alert.error.call_args_list[0].kwargs['amount_kopeks'] == 15000


def test_alert_logger_is_not_silenced_as_payment_logger():
    """Логгеры tribute_service отрезаны от админ-чата, журнала ошибок и файлов — тревога идёт мимо этих фильтров."""
    import inspect
    from pathlib import Path

    from app.logging_handler import IGNORED_LOGGER_PREFIXES
    from app.utils.log_handlers import ExcludePaymentFilter

    name = 'app.tribute_alert'
    assert f"structlog.get_logger('{name}')" in Path(inspect.getfile(TributeService)).read_text(encoding='utf-8')
    assert not name.startswith(IGNORED_LOGGER_PREFIXES)
    assert not name.startswith(ExcludePaymentFilter.PAYMENT_MODULES)


def _donation_without_created_at(amount: int = 15000) -> str:
    """Без created_at ключ синтетический (tribute_<tg>_<amount>) и нарочно неуникальный."""
    return json.dumps(
        {'name': 'new_donation', 'payload': {'donation_request_id': 777, 'amount': amount, 'telegram_user_id': TG_ID}}
    )


async def test_failure_after_commit_answers_ok_and_is_not_credited_twice(monkeypatch, service_on):
    """Деньги уже на балансе — 5xx тут опасен: повтор Tribute с синтетическим ключом, пришедший
    позже окна дедупа в 24 ч, зачислился бы второй раз. Поэтому 200 и тревога, а не повтор."""
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed_user(db)
        service, side_effects, alert = service_on(db)
        side_effects.side_effect = RuntimeError('events bus down')

        result = await service.process_webhook(_donation_without_created_at())

        assert result['status'] == 'ok'
        assert await _state(db) == (15000, 1)
        assert 'зачислена' in alert.error.call_args.args[0]


async def test_event_without_money_does_not_raise_payment_alert(monkeypatch, service_on):
    async with memory_session(monkeypatch, TABLES) as db:
        await _seed_user(db)
        service, _, alert = service_on(db)

        result = await service.process_webhook(json.dumps({'name': 'cancelled_subscription', 'payload': {}}))

    assert result['status'] == 'ignored'
    alert.error.assert_not_called()
