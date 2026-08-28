"""Regression test for Telegram bug #652234 (promo-offer broadcast).

The broadcast committed an offer per recipient and then sent Telegram notifications to
everyone synchronously inside the HTTP request; a large fan-out overran the proxy timeout,
so the cabinet showed an error while offers were already created and notifications kept
going. Delivery now goes through the broadcast service: it takes plain telegram_id values
and a keyboard factory closing over plain offer ids — not ORM objects bound to the request
session — so it keeps running after the request (and its session) is gone.

This pins that contract: only recipients with a telegram_id are handed over, the payload
carries no ORM objects, and nothing is queued when there is nobody to notify.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.cabinet.routes.admin_promo_offers import PromoOfferBroadcastRequest, broadcast_offer
from app.database.models import (
    BroadcastHistory,
    DiscountOffer,
    PromoGroup,
    Subscription,
    SubscriptionEvent,
    SubscriptionStatus,
    Tariff,
    User,
    UserStatus,
)
from tests.fixtures.sqlite_memory import memory_session


NOTIFY_TABLES = (
    User.__table__,
    Subscription.__table__,
    SubscriptionEvent.__table__,
    Tariff.__table__,
    PromoGroup.__table__,
    DiscountOffer.__table__,
    BroadcastHistory.__table__,
)


def _admin() -> User:
    return User(id=99, telegram_id=99, username='admin', status=UserStatus.ACTIVE.value)


def _payload(**kwargs) -> PromoOfferBroadcastRequest:
    defaults = {
        'notification_type': 'extend_discount',
        'valid_hours': 24,
        'discount_percent': 10,
        'target': 'active',
        'send_notification': True,
        'message_text': 'hi',
    }
    defaults.update(kwargs)
    return PromoOfferBroadcastRequest(**defaults)


async def _seed_subscriber(
    db,
    *,
    telegram_id: int | None,
    email: str | None = None,
    notification_settings: dict | None = None,
) -> User:
    now = datetime.now(UTC)
    user = User(
        telegram_id=telegram_id,
        email=email,
        email_verified=bool(email),
        username=f'user{telegram_id or email}',
        first_name='User',
        status=UserStatus.ACTIVE.value,
        balance_kopeks=0,
        last_activity=now,
        notification_settings=notification_settings,
    )
    db.add(user)
    await db.commit()

    db.add(
        Subscription(
            user_id=user.id,
            status=SubscriptionStatus.ACTIVE.value,
            is_trial=False,
            start_date=now - timedelta(days=5),
            end_date=now + timedelta(days=25),
            remnawave_short_id=f'short{user.id}',
        )
    )
    await db.commit()
    return user


async def test_delivery_runs_off_plain_ids(monkeypatch):
    """В сервис рассылок уходят голые telegram_id, без ORM-объектов сессии запроса."""
    started: list[object] = []

    async def fake_start_broadcast(broadcast_id, config):
        started.append(config)

    monkeypatch.setattr(
        'app.cabinet.routes.admin_promo_offers.broadcast_service.start_broadcast',
        fake_start_broadcast,
    )

    async with memory_session(monkeypatch, NOTIFY_TABLES) as db:
        await _seed_subscriber(db, telegram_id=111)
        await _seed_subscriber(db, telegram_id=222)
        # Email-only получатель: оффер ему создаётся, но в Telegram-доставку он не идёт
        await _seed_subscriber(db, telegram_id=None, email='mail@example.com')

        response = await broadcast_offer(_payload(), admin=_admin(), db=db)

        assert response.created_offers == 3
        assert response.telegram_recipients == 2

        assert len(started) == 1
        config = started[0]
        assert sorted(config.recipient_ids) == [111, 222]
        assert all(isinstance(telegram_id, int) for telegram_id in config.recipient_ids)


async def test_nothing_queued_without_telegram_recipients(monkeypatch):
    """Некому слать в Telegram — запись рассылки не заводится."""

    async def fail_start_broadcast(broadcast_id, config):
        raise AssertionError('рассылку не должны были запускать')

    monkeypatch.setattr(
        'app.cabinet.routes.admin_promo_offers.broadcast_service.start_broadcast',
        fail_start_broadcast,
    )

    async with memory_session(monkeypatch, NOTIFY_TABLES) as db:
        await _seed_subscriber(db, telegram_id=None, email='mail@example.com')

        response = await broadcast_offer(_payload(), admin=_admin(), db=db)

        assert response.created_offers == 1
        assert response.telegram_recipients == 0
        assert response.broadcast_id is None


async def test_promo_preferences_filter_telegram_and_email_notifications(monkeypatch):
    started: list[object] = []

    async def fail_send_emails(*args, **kwargs):
        raise AssertionError('email notification must be filtered')

    monkeypatch.setattr(
        'app.cabinet.routes.admin_promo_offers._send_promo_email_notifications',
        fail_send_emails,
    )

    async def fake_start_broadcast(broadcast_id, config):
        started.append(config)

    monkeypatch.setattr(
        'app.cabinet.routes.admin_promo_offers.broadcast_service.start_broadcast',
        fake_start_broadcast,
    )

    async with memory_session(monkeypatch, NOTIFY_TABLES) as db:
        await _seed_subscriber(
            db,
            telegram_id=111,
            notification_settings={'promo_offers_enabled': False},
        )
        await _seed_subscriber(
            db,
            telegram_id=None,
            email='disabled@example.com',
            notification_settings={'promo_offers_enabled': False},
        )

        response = await broadcast_offer(_payload(), admin=_admin(), db=db)

        assert response.created_offers == 2
        assert response.telegram_recipients == 0
        assert response.broadcast_id is None
        assert started == []
