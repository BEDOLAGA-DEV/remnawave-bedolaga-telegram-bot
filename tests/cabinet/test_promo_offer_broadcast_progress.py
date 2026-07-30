"""Рассылка промопредложений заводит запись рассылки, по которой кабинет видит доставку.

Раньше отправка на большую аудиторию детачилась в фон и о её судьбе нельзя было
узнать ничего: ни сколько дошло, ни сколько человек заблокировало бота.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.cabinet.routes.admin_promo_offers import (
    TARGET_SEGMENTS,
    PromoOfferBroadcastRequest,
    broadcast_offer,
    list_segments,
)
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


PROMO_TABLES = (
    User.__table__,
    Subscription.__table__,
    SubscriptionEvent.__table__,
    Tariff.__table__,
    PromoGroup.__table__,
    DiscountOffer.__table__,
    BroadcastHistory.__table__,
)


async def _seed_active_subscribers(db, count: int) -> list[User]:
    now = datetime.now(UTC)
    users = [
        User(
            telegram_id=1000 + index,
            username=f'user{index}',
            first_name=f'User {index}',
            status=UserStatus.ACTIVE.value,
            balance_kopeks=0,
            last_activity=now,
        )
        for index in range(count)
    ]
    db.add_all(users)
    await db.commit()

    db.add_all(
        [
            Subscription(
                user_id=user.id,
                status=SubscriptionStatus.ACTIVE.value,
                is_trial=False,
                start_date=now - timedelta(days=5),
                end_date=now + timedelta(days=25),
                remnawave_short_id=f'short{user.id}',
            )
            for user in users
        ]
    )
    await db.commit()
    return users


def _admin() -> User:
    return User(id=99, telegram_id=99, username='admin', status=UserStatus.ACTIVE.value)


def _payload(**kwargs) -> PromoOfferBroadcastRequest:
    defaults = {
        'notification_type': 'extend_discount',
        'valid_hours': 24,
        'discount_percent': 20,
        'target': 'active',
        'send_notification': True,
        'message_text': 'Скидка {discount_percent}% на продление',
        'button_text': '🎁 Забрать',
    }
    defaults.update(kwargs)
    return PromoOfferBroadcastRequest(**defaults)


async def test_broadcast_creates_tracked_delivery(monkeypatch):
    """Отправка сегменту заводит запись рассылки с числом получателей и возвращает её id."""
    started: list[tuple[int, object]] = []

    async def fake_start_broadcast(broadcast_id, config):
        started.append((broadcast_id, config))

    monkeypatch.setattr(
        'app.cabinet.routes.admin_promo_offers.broadcast_service.start_broadcast',
        fake_start_broadcast,
    )

    async with memory_session(monkeypatch, PROMO_TABLES) as db:
        users = await _seed_active_subscribers(db, 3)

        response = await broadcast_offer(_payload(), admin=_admin(), db=db)

        assert response.created_offers == 3
        assert response.telegram_recipients == 3
        assert response.broadcast_id is not None

        broadcast = await db.get(BroadcastHistory, response.broadcast_id)
        assert broadcast is not None
        assert broadcast.total_count == 3
        assert broadcast.status == 'queued'
        assert broadcast.category == 'promo'
        assert broadcast.target_type == 'active'
        # Плейсхолдеры раскрыты до постановки в очередь — в Telegram уходит готовый текст
        assert broadcast.message_text == 'Скидка 20% на продление'

        assert len(started) == 1
        broadcast_id, config = started[0]
        assert broadcast_id == response.broadcast_id
        assert sorted(config.recipient_ids) == sorted(user.telegram_id for user in users)

        # Клавиатура персональная: в callback_data зашит оффер конкретного получателя
        offers = (await db.execute(select(DiscountOffer))).scalars().all()
        offer_by_user = {offer.user_id: offer.id for offer in offers}
        for user in users:
            keyboard = config.keyboard_factory(user.telegram_id)
            assert keyboard.inline_keyboard[0][0].callback_data == f'claim_discount_{offer_by_user[user.id]}'


async def test_broadcast_without_notification_skips_delivery_record(monkeypatch):
    """Без send_notification офферы создаются, но рассылку заводить не за чем."""
    started: list[int] = []

    async def fake_start_broadcast(broadcast_id, config):
        started.append(broadcast_id)

    monkeypatch.setattr(
        'app.cabinet.routes.admin_promo_offers.broadcast_service.start_broadcast',
        fake_start_broadcast,
    )

    async with memory_session(monkeypatch, PROMO_TABLES) as db:
        await _seed_active_subscribers(db, 2)

        response = await broadcast_offer(_payload(send_notification=False), admin=_admin(), db=db)

        assert response.created_offers == 2
        assert response.broadcast_id is None
        assert started == []
        assert (await db.execute(select(BroadcastHistory))).scalars().first() is None


async def test_segments_endpoint_returns_counts_for_every_segment(monkeypatch):
    """Кабинет получает охват по каждому сегменту, а не только по выбранному."""
    async with memory_session(monkeypatch, PROMO_TABLES) as db:
        await _seed_active_subscribers(db, 4)

        response = await list_segments(admin=_admin(), db=db)

        assert [segment.key for segment in response.segments] == list(TARGET_SEGMENTS)
        counts = {segment.key: segment.count for segment in response.segments}
        assert counts['all'] == 4
        assert counts['active'] == 4
        assert counts['trial'] == 0
