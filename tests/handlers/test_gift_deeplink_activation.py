"""End-to-end regression tests for gift deep-link activation and subscription semantics.

Covers:
- Step 1: Canonical /start GIFT_<prefix> deep-link claiming by existing and new users.
- Step 1: First-claimant binding (PAID -> PENDING_ACTIVATION -> DELIVERED).
- Step 1: Buyer self-claim refusal, duplicate claim refusal, short/invalid prefix rejection.
- Step 1: Complete token confidentiality across all bot responses.
- Step 1: Telegram notification inline callback button activation (gift_activate:{id}).
- Step 2: Multi-tariff subscription activation semantics (active extend, expired replace, new tariff create).
- Step 2: Single-tariff subscription activation semantics (active extend + limit update, expired replace, first create).
- Step 2: Remnawave provisioning invariants (zero at purchase-time, exactly once at claim-time).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User as TgUser
from sqlalchemy import select

import app.handlers.gift_activation as gift_act_mod
from app.config import settings
from app.database.crud.landing import generate_purchase_token
from app.database.models import (
    AdvertisingCampaign,
    DiscountOffer,
    GuestPurchase,
    GuestPurchaseStatus,
    MainMenuButton,
    PaymentMethodConfig,
    PinnedMessage,
    PromoGroup,
    PromoOfferLog,
    SentNotification,
    ServerSquad,
    Subscription,
    SubscriptionStatus,
    SystemSetting,
    Tariff,
    TrafficPurchase,
    Transaction,
    User,
    UserPromoGroup,
    Webhook,
    WebhookDelivery,
    tariff_promo_groups,
)
from app.handlers.start import (
    _activate_pending_gift_after_registration,
    cmd_start,
)
from app.services.gift_purchase_service import (
    GIFT_ENABLED_KEY,
    purchase_gift_from_balance,
    quote_gift_purchase,
)
from app.services.guest_purchase_service import (
    GIFT_TOKEN_MIN_PREFIX_LENGTH,
    activate_purchase,
)
from app.utils.gift_links import (
    GIFT_TOKEN_BOT_PREFIX_LENGTH,
    build_bot_gift_claim_link,
)
from tests.fixtures.sqlite_memory import memory_session


_TABLES = [
    SystemSetting.__table__,
    Tariff.__table__,
    PromoGroup.__table__,
    tariff_promo_groups,
    UserPromoGroup.__table__,
    Subscription.__table__,
    User.__table__,
    GuestPurchase.__table__,
    Transaction.__table__,
    DiscountOffer.__table__,
    PromoOfferLog.__table__,
    ServerSquad.__table__,
    MainMenuButton.__table__,
    PinnedMessage.__table__,
    AdvertisingCampaign.__table__,
    TrafficPurchase.__table__,
    SentNotification.__table__,
    Webhook.__table__,
    WebhookDelivery.__table__,
    PaymentMethodConfig.__table__,
]


# ── Helpers & Fixtures ──────────────────────────────────────────────────────


def _make_fsm_context(user_id: int, chat_id: int | None = None) -> FSMContext:
    storage = MemoryStorage()
    c_id = chat_id if chat_id is not None else user_id
    key = StorageKey(bot_id=1, chat_id=c_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


def _make_message(text: str, user_id: int, username: str = 'recipient_user') -> Message:
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.message_id = 100
    msg.from_user = TgUser(id=user_id, is_bot=False, first_name='TestUser', username=username)
    msg.chat = Chat(id=user_id, type='private')
    msg.answer = AsyncMock()
    msg.reply = AsyncMock()
    msg.bot = AsyncMock()
    return msg


async def _seed_tariffs_and_settings(db) -> tuple[Tariff, Tariff]:
    setting = SystemSetting(key=GIFT_ENABLED_KEY, value='true')
    db.add(setting)

    t1 = Tariff(
        name='Standard Plan',
        is_active=True,
        show_in_gift=True,
        device_limit=2,
        traffic_limit_gb=50,
        period_prices={'30': 30000},
        display_order=1,
    )
    t2 = Tariff(
        name='Premium Plan',
        is_active=True,
        show_in_gift=True,
        device_limit=5,
        traffic_limit_gb=100,
        period_prices={'30': 60000},
        display_order=2,
    )
    db.add_all([t1, t2])
    await db.commit()
    await db.refresh(t1)
    await db.refresh(t2)
    return t1, t2


# ── Step 1: End-to-End Deep-Link Claim Tests ────────────────────────────────


class TestGiftDeeplinkActivation:
    """End-to-end tests for /start GIFT_<prefix> deep-link claiming."""

    @pytest.mark.asyncio
    async def test_existing_user_claims_gift_via_start_deeplink(self, monkeypatch):
        """An existing user claiming /start GIFT_<prefix> transitions purchase to DELIVERED and binds claimant."""
        async with memory_session(monkeypatch, _TABLES) as db:
            t1, _ = await _seed_tariffs_and_settings(db)

            buyer = User(telegram_id=11111, username='buyer', balance_kopeks=50000)
            recipient = User(telegram_id=22222, username='recipient', balance_kopeks=0)
            db.add_all([buyer, recipient])
            await db.commit()
            await db.refresh(buyer)
            await db.refresh(recipient)

            # Buyer purchases gift
            quote = await quote_gift_purchase(db, buyer=buyer, tariff_id=t1.id, period_days=30)
            purchase_res = await purchase_gift_from_balance(
                db,
                buyer_id=buyer.id,
                tariff_id=t1.id,
                period_days=30,
                expected_price_kopeks=quote.final_price_kopeks,
                idempotency_key='chk_e2e_1',
            )
            raw_token = purchase_res.purchase.token

            # Generate canonical claim deep link
            claim_link = build_bot_gift_claim_link(raw_token, 'my_bot')
            start_param = claim_link.split('start=')[1]

            msg = _make_message(f'/start {start_param}', user_id=recipient.telegram_id, username='recipient')
            state = _make_fsm_context(recipient.telegram_id)

            with patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()):
                await cmd_start(msg, state, db, db_user=recipient)

            # Assert database state
            res = await db.execute(select(GuestPurchase).where(GuestPurchase.token == raw_token))
            updated_purchase = res.scalars().first()
            assert updated_purchase is not None
            assert updated_purchase.status == GuestPurchaseStatus.DELIVERED.value
            assert updated_purchase.user_id == recipient.id
            assert updated_purchase.delivered_at is not None

            # Assert subscription created
            sub_res = await db.execute(select(Subscription).where(Subscription.user_id == recipient.id))
            sub = sub_res.scalars().first()
            assert sub is not None
            assert sub.tariff_id == t1.id
            assert sub.status == SubscriptionStatus.ACTIVE.value

            # Assert answer contains activation text and no token leaks
            msg.answer.assert_awaited()
            answered_texts = [call.args[0] for call in msg.answer.call_args_list if call.args]
            assert any('Подарок активирован' in txt for txt in answered_texts)
            assert not any(raw_token in txt for txt in answered_texts)

    @pytest.mark.asyncio
    async def test_new_user_registration_auto_activates_pending_gift(self, monkeypatch):
        """A new user whose first interaction is /start GIFT_<prefix> has gift claimed after registration."""
        async with memory_session(monkeypatch, _TABLES) as db:
            t1, _ = await _seed_tariffs_and_settings(db)

            buyer = User(telegram_id=11111, username='buyer', balance_kopeks=50000)
            db.add(buyer)
            await db.commit()
            await db.refresh(buyer)

            quote = await quote_gift_purchase(db, buyer=buyer, tariff_id=t1.id, period_days=30)
            purchase_res = await purchase_gift_from_balance(
                db,
                buyer_id=buyer.id,
                tariff_id=t1.id,
                period_days=30,
                expected_price_kopeks=quote.final_price_kopeks,
                idempotency_key='chk_new_reg_1',
            )
            raw_token = purchase_res.purchase.token
            claim_link = build_bot_gift_claim_link(raw_token, 'my_bot')
            start_param = claim_link.split('start=')[1]

            new_tg_id = 99999
            state = _make_fsm_context(new_tg_id)

            # Step A: First touch /start stores pending_gift_token in FSM
            start_parameter = start_param
            if start_parameter.startswith('GIFT_'):
                gift_token = start_parameter[5:]
                if len(gift_token) >= GIFT_TOKEN_MIN_PREFIX_LENGTH:
                    await state.update_data(pending_gift_token=gift_token)

            state_data = await state.get_data()
            assert state_data.get('pending_gift_token') == raw_token[:GIFT_TOKEN_BOT_PREFIX_LENGTH]

            # Step B: User is registered into DB
            new_user = User(telegram_id=new_tg_id, username='brand_new_user', balance_kopeks=0)
            db.add(new_user)
            await db.commit()
            await db.refresh(new_user)

            # Step C: Registration completion calls _activate_pending_gift_after_registration
            answer_func = AsyncMock()
            with patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()):
                await _activate_pending_gift_after_registration(db, state, new_user, answer_func)

            # Verify purchase status and claimant binding
            res = await db.execute(select(GuestPurchase).where(GuestPurchase.token == raw_token))
            updated_purchase = res.scalars().first()
            assert updated_purchase.status == GuestPurchaseStatus.DELIVERED.value
            assert updated_purchase.user_id == new_user.id

            # Verify response text
            answer_func.assert_awaited_once()
            response_text = answer_func.call_args[0][0]
            assert 'Подарок активирован' in response_text
            assert raw_token not in response_text

    @pytest.mark.asyncio
    async def test_buyer_self_claim_refusal(self, monkeypatch):
        """The buyer attempting to activate their own gift is refused and gift remains unconsumed."""
        async with memory_session(monkeypatch, _TABLES) as db:
            t1, _ = await _seed_tariffs_and_settings(db)

            buyer = User(telegram_id=11111, username='buyer', balance_kopeks=50000)
            db.add(buyer)
            await db.commit()
            await db.refresh(buyer)

            quote = await quote_gift_purchase(db, buyer=buyer, tariff_id=t1.id, period_days=30)
            purchase_res = await purchase_gift_from_balance(
                db,
                buyer_id=buyer.id,
                tariff_id=t1.id,
                period_days=30,
                expected_price_kopeks=quote.final_price_kopeks,
                idempotency_key='chk_self_1',
            )
            raw_token = purchase_res.purchase.token
            claim_link = build_bot_gift_claim_link(raw_token, 'my_bot')
            start_param = claim_link.split('start=')[1]

            msg = _make_message(f'/start {start_param}', user_id=buyer.telegram_id, username='buyer')
            state = _make_fsm_context(buyer.telegram_id)

            await cmd_start(msg, state, db, db_user=buyer)

            # Assert purchase NOT delivered and still has no user_id
            res = await db.execute(select(GuestPurchase).where(GuestPurchase.token == raw_token))
            updated_purchase = res.scalars().first()
            assert updated_purchase.status == GuestPurchaseStatus.PAID.value
            assert updated_purchase.user_id is None

            # Assert refusal response sent
            answered_texts = [call.args[0] for call in msg.answer.call_args_list if call.args]
            assert any('Нельзя активировать свой собственный подарок' in txt for txt in answered_texts)
            assert not any(raw_token in txt for txt in answered_texts)

    @pytest.mark.asyncio
    async def test_duplicate_claim_refusal_already_delivered(self, monkeypatch):
        """A second user attempting to claim an already-activated gift receives an already-activated refusal."""
        async with memory_session(monkeypatch, _TABLES) as db:
            t1, _ = await _seed_tariffs_and_settings(db)

            buyer = User(telegram_id=11111, username='buyer', balance_kopeks=50000)
            recipient_1 = User(telegram_id=22222, username='rec1', balance_kopeks=0)
            recipient_2 = User(telegram_id=33333, username='rec2', balance_kopeks=0)
            db.add_all([buyer, recipient_1, recipient_2])
            await db.commit()
            await db.refresh(buyer)
            await db.refresh(recipient_1)
            await db.refresh(recipient_2)

            quote = await quote_gift_purchase(db, buyer=buyer, tariff_id=t1.id, period_days=30)
            purchase_res = await purchase_gift_from_balance(
                db,
                buyer_id=buyer.id,
                tariff_id=t1.id,
                period_days=30,
                expected_price_kopeks=quote.final_price_kopeks,
                idempotency_key='chk_dup_1',
            )
            raw_token = purchase_res.purchase.token
            claim_link = build_bot_gift_claim_link(raw_token, 'my_bot')
            start_param = claim_link.split('start=')[1]

            # Recipient 1 claims it
            msg1 = _make_message(f'/start {start_param}', user_id=recipient_1.telegram_id, username='rec1')
            state1 = _make_fsm_context(recipient_1.telegram_id)
            with patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()):
                await cmd_start(msg1, state1, db, db_user=recipient_1)

            # Assert delivered to recipient 1
            res = await db.execute(select(GuestPurchase).where(GuestPurchase.token == raw_token))
            p = res.scalars().first()
            assert p.status == GuestPurchaseStatus.DELIVERED.value
            assert p.user_id == recipient_1.id

            # Recipient 2 attempts to claim the same link
            msg2 = _make_message(f'/start {start_param}', user_id=recipient_2.telegram_id, username='rec2')
            state2 = _make_fsm_context(recipient_2.telegram_id)
            await cmd_start(msg2, state2, db, db_user=recipient_2)

            # Purchase remains bound to recipient 1
            await db.refresh(p)
            assert p.user_id == recipient_1.id

            # Recipient 2 gets "already activated" message
            answered_texts = [call.args[0] for call in msg2.answer.call_args_list if call.args]
            assert any('Этот подарок уже был активирован' in txt for txt in answered_texts)
            assert not any(raw_token in txt for txt in answered_texts)

    @pytest.mark.asyncio
    async def test_claim_refusal_when_already_bound_to_different_user(self, monkeypatch):
        """When a gift is bound to user A, user B cannot activate it."""
        async with memory_session(monkeypatch, _TABLES) as db:
            t1, _ = await _seed_tariffs_and_settings(db)

            user_a = User(telegram_id=22222, username='user_a', balance_kopeks=0)
            user_b = User(telegram_id=33333, username='user_b', balance_kopeks=0)
            db.add_all([user_a, user_b])
            await db.commit()
            await db.refresh(user_a)
            await db.refresh(user_b)

            token = generate_purchase_token()
            purchase = GuestPurchase(
                token=token,
                contact_type='telegram',
                contact_value=str(user_a.telegram_id),
                is_gift=True,
                status=GuestPurchaseStatus.PENDING_ACTIVATION.value,
                tariff_id=t1.id,
                period_days=30,
                amount_kopeks=30000,
                user_id=user_a.id,
            )
            db.add(purchase)
            await db.commit()

            state_b = _make_fsm_context(user_b.telegram_id)
            await state_b.update_data(pending_gift_token=token[:59])
            answer_mock = AsyncMock()

            await _activate_pending_gift_after_registration(db, state_b, user_b, answer_mock)

            # Purchase was NOT activated for user B
            await db.refresh(purchase)
            assert purchase.status == GuestPurchaseStatus.PENDING_ACTIVATION.value
            assert purchase.user_id == user_a.id
            # Refusal message sent to user B
            answered_texts = [call.args[0] for call in answer_mock.call_args_list if call.args]
            assert any('Этот подарок уже был активирован' in txt for txt in answered_texts)

    @pytest.mark.asyncio
    async def test_malformed_and_short_prefix_rejected(self, monkeypatch):
        """Short prefixes below GIFT_TOKEN_MIN_PREFIX_LENGTH are rejected and ignore deep link."""
        async with memory_session(monkeypatch, _TABLES) as db:
            t1, _ = await _seed_tariffs_and_settings(db)

            user = User(telegram_id=22222, username='rec', balance_kopeks=0)
            db.add(user)
            await db.commit()
            await db.refresh(user)

            # Create a paid gift
            token = generate_purchase_token()
            purchase = GuestPurchase(
                token=token,
                contact_type='telegram',
                contact_value=str(user.telegram_id),
                is_gift=True,
                status=GuestPurchaseStatus.PAID.value,
                tariff_id=t1.id,
                period_days=30,
                amount_kopeks=30000,
            )
            db.add(purchase)
            await db.commit()

            # Attempt short prefix (e.g. 12 chars)
            short_param = f'GIFT_{token[:12]}'
            msg = _make_message(f'/start {short_param}', user_id=user.telegram_id)
            state = _make_fsm_context(user.telegram_id)

            await cmd_start(msg, state, db, db_user=user)

            # Purchase remains untouched
            await db.refresh(purchase)
            assert purchase.status == GuestPurchaseStatus.PAID.value
            assert purchase.user_id is None

    @pytest.mark.asyncio
    async def test_callback_gift_activate_button(self, monkeypatch):
        """Test inline button gift_activate:{purchase_id} flow in handle_gift_activate."""
        async with memory_session(monkeypatch, _TABLES) as db:
            t1, _ = await _seed_tariffs_and_settings(db)

            recipient = User(telegram_id=22222, username='rec', balance_kopeks=0)
            db.add(recipient)
            await db.commit()
            await db.refresh(recipient)

            token = generate_purchase_token()
            purchase = GuestPurchase(
                token=token,
                contact_type='telegram',
                contact_value=str(recipient.telegram_id),
                is_gift=True,
                status=GuestPurchaseStatus.PENDING_ACTIVATION.value,
                tariff_id=t1.id,
                period_days=30,
                amount_kopeks=30000,
                user_id=recipient.id,
            )
            db.add(purchase)
            await db.commit()
            await db.refresh(purchase)

            # Mock callback query
            callback = MagicMock(spec=CallbackQuery)
            callback.data = f'gift_activate:{purchase.id}'
            callback.from_user = TgUser(id=recipient.telegram_id, is_bot=False, first_name='Rec')
            callback.message = MagicMock(spec=Message)
            callback.message.edit_text = AsyncMock()
            callback.answer = AsyncMock()

            # Mock AsyncSessionLocal used in handle_gift_activate
            class DummySessionCtx:
                async def __aenter__(self):
                    return db

                async def __aexit__(self, *args):
                    pass

            monkeypatch.setattr(gift_act_mod, 'AsyncSessionLocal', lambda: DummySessionCtx())

            with patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()):
                await gift_act_mod.handle_gift_activate(callback)

            await db.refresh(purchase)
            assert purchase.status == GuestPurchaseStatus.DELIVERED.value
            callback.message.edit_text.assert_awaited()
            last_edit = callback.message.edit_text.call_args[0][0]
            assert 'Подарок активирован' in last_edit


# ── Step 2: Multi-Tariff Subscription Activation Semantics ──────────────────


class TestGiftSubscriptionActivationMultiTariff:
    """Pins multi-tariff subscription activation semantics."""

    @pytest.mark.asyncio
    async def test_active_same_tariff_extends_preserving_remaining_time(self, monkeypatch):
        """In multi-tariff mode, claiming a gift for the SAME active tariff extends remaining time."""
        monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)

        async with memory_session(monkeypatch, _TABLES) as db:
            t1, _ = await _seed_tariffs_and_settings(db)

            recipient = User(telegram_id=22222, username='rec', balance_kopeks=0)
            db.add(recipient)
            await db.commit()
            await db.refresh(recipient)

            # Active subscription with 10 days remaining
            now = datetime.now(UTC)
            initial_end = now + timedelta(days=10)
            sub = Subscription(
                user_id=recipient.id,
                tariff_id=t1.id,
                status=SubscriptionStatus.ACTIVE.value,
                is_trial=False,
                start_date=now - timedelta(days=20),
                end_date=initial_end,
                traffic_limit_gb=50,
                device_limit=2,
            )
            db.add(sub)
            await db.commit()

            # Create gift for Tariff 1 for 30 days
            token = generate_purchase_token()
            purchase = GuestPurchase(
                token=token,
                contact_type='telegram',
                contact_value=str(recipient.telegram_id),
                is_gift=True,
                status=GuestPurchaseStatus.PENDING_ACTIVATION.value,
                tariff_id=t1.id,
                period_days=30,
                amount_kopeks=30000,
                user_id=recipient.id,
            )
            db.add(purchase)
            await db.commit()

            with patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()):
                activated = await activate_purchase(db, token, skip_notification=True)

            assert activated.status == GuestPurchaseStatus.DELIVERED.value

            # Subscription was extended: new end_date is approx initial_end + 30 days
            await db.refresh(sub)
            expected_min_end = initial_end + timedelta(days=29)
            assert sub.end_date > expected_min_end
            assert sub.status == SubscriptionStatus.ACTIVE.value

    @pytest.mark.asyncio
    async def test_expired_same_tariff_refreshes_from_current_time(self, monkeypatch):
        """In multi-tariff mode, claiming a gift for an EXPIRED same-tariff resets end date from now."""
        monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)

        async with memory_session(monkeypatch, _TABLES) as db:
            t1, _ = await _seed_tariffs_and_settings(db)

            recipient = User(telegram_id=22222, username='rec', balance_kopeks=0)
            db.add(recipient)
            await db.commit()
            await db.refresh(recipient)

            # Expired subscription (ended 5 days ago)
            now = datetime.now(UTC)
            expired_end = now - timedelta(days=5)
            sub = Subscription(
                user_id=recipient.id,
                tariff_id=t1.id,
                status=SubscriptionStatus.ACTIVE.value,
                is_trial=False,
                start_date=now - timedelta(days=35),
                end_date=expired_end,
                traffic_limit_gb=50,
                device_limit=2,
            )
            db.add(sub)
            await db.commit()

            token = generate_purchase_token()
            purchase = GuestPurchase(
                token=token,
                contact_type='telegram',
                contact_value=str(recipient.telegram_id),
                is_gift=True,
                status=GuestPurchaseStatus.PENDING_ACTIVATION.value,
                tariff_id=t1.id,
                period_days=30,
                amount_kopeks=30000,
                user_id=recipient.id,
            )
            db.add(purchase)
            await db.commit()

            with patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()):
                activated = await activate_purchase(db, token, skip_notification=True)

            assert activated.status == GuestPurchaseStatus.DELIVERED.value

            # Subscription refreshed: end_date is now + 30 days
            await db.refresh(sub)
            assert sub.end_date > now + timedelta(days=29)
            assert sub.end_date < now + timedelta(days=31)

    @pytest.mark.asyncio
    async def test_different_tariff_creates_new_subscription(self, monkeypatch):
        """In multi-tariff mode, claiming a gift for a DIFFERENT tariff creates a new subscription row."""
        monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)

        async with memory_session(monkeypatch, _TABLES) as db:
            t1, t2 = await _seed_tariffs_and_settings(db)

            recipient = User(telegram_id=22222, username='rec', balance_kopeks=0)
            db.add(recipient)
            await db.commit()
            await db.refresh(recipient)

            # Existing active sub on Tariff 1
            now = datetime.now(UTC)
            sub1 = Subscription(
                user_id=recipient.id,
                tariff_id=t1.id,
                status=SubscriptionStatus.ACTIVE.value,
                is_trial=False,
                start_date=now,
                end_date=now + timedelta(days=30),
                traffic_limit_gb=50,
                device_limit=2,
            )
            db.add(sub1)
            await db.commit()

            # Gift for Tariff 2
            token = generate_purchase_token()
            purchase = GuestPurchase(
                token=token,
                contact_type='telegram',
                contact_value=str(recipient.telegram_id),
                is_gift=True,
                status=GuestPurchaseStatus.PENDING_ACTIVATION.value,
                tariff_id=t2.id,
                period_days=30,
                amount_kopeks=60000,
                user_id=recipient.id,
            )
            db.add(purchase)
            await db.commit()

            with patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()):
                activated = await activate_purchase(db, token, skip_notification=True)

            assert activated.status == GuestPurchaseStatus.DELIVERED.value

            # User now has two distinct subscriptions
            subs_res = await db.execute(
                select(Subscription).where(Subscription.user_id == recipient.id).order_by(Subscription.id)
            )
            all_subs = subs_res.scalars().all()
            assert len(all_subs) == 2
            tariff_ids = {s.tariff_id for s in all_subs}
            assert tariff_ids == {t1.id, t2.id}


# ── Step 2: Single-Tariff Subscription Activation Semantics ─────────────────


class TestGiftSubscriptionActivationSingleTariff:
    """Pins single-tariff subscription activation semantics."""

    @pytest.mark.asyncio
    async def test_active_subscription_extends_with_gifted_tariff_and_limits(self, monkeypatch):
        """In single-tariff mode, claiming a gift extends active sub and adopts gifted tariff/limits."""
        monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)

        async with memory_session(monkeypatch, _TABLES) as db:
            t1, t2 = await _seed_tariffs_and_settings(db)

            recipient = User(telegram_id=22222, username='rec', balance_kopeks=0)
            db.add(recipient)
            await db.commit()
            await db.refresh(recipient)

            now = datetime.now(UTC)
            initial_end = now + timedelta(days=10)
            sub = Subscription(
                user_id=recipient.id,
                tariff_id=t1.id,
                status=SubscriptionStatus.ACTIVE.value,
                is_trial=False,
                start_date=now - timedelta(days=20),
                end_date=initial_end,
                traffic_limit_gb=50,
                device_limit=2,
            )
            db.add(sub)
            await db.commit()

            # Gift for Tariff 2 (Premium, 100GB, 5 devices)
            token = generate_purchase_token()
            purchase = GuestPurchase(
                token=token,
                contact_type='telegram',
                contact_value=str(recipient.telegram_id),
                is_gift=True,
                status=GuestPurchaseStatus.PENDING_ACTIVATION.value,
                tariff_id=t2.id,
                period_days=30,
                amount_kopeks=60000,
                user_id=recipient.id,
            )
            db.add(purchase)
            await db.commit()

            with patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()):
                activated = await activate_purchase(db, token, skip_notification=True)

            assert activated.status == GuestPurchaseStatus.DELIVERED.value

            # Subscription extended and upgraded to Tariff 2 limits
            await db.refresh(sub)
            assert sub.end_date > initial_end + timedelta(days=29)
            assert sub.tariff_id == t2.id
            assert sub.traffic_limit_gb == t2.traffic_limit_gb
            assert sub.device_limit == t2.device_limit

    @pytest.mark.asyncio
    async def test_expired_subscription_replaced_with_gifted_tariff(self, monkeypatch):
        """In single-tariff mode, claiming a gift on expired subscription replaces it with fresh dates."""
        monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)

        async with memory_session(monkeypatch, _TABLES) as db:
            t1, t2 = await _seed_tariffs_and_settings(db)

            recipient = User(telegram_id=22222, username='rec', balance_kopeks=0)
            db.add(recipient)
            await db.commit()
            await db.refresh(recipient)

            now = datetime.now(UTC)
            sub = Subscription(
                user_id=recipient.id,
                tariff_id=t1.id,
                status=SubscriptionStatus.ACTIVE.value,
                is_trial=False,
                start_date=now - timedelta(days=60),
                end_date=now - timedelta(days=10),
                traffic_limit_gb=50,
                device_limit=2,
            )
            db.add(sub)
            await db.commit()

            token = generate_purchase_token()
            purchase = GuestPurchase(
                token=token,
                contact_type='telegram',
                contact_value=str(recipient.telegram_id),
                is_gift=True,
                status=GuestPurchaseStatus.PENDING_ACTIVATION.value,
                tariff_id=t2.id,
                period_days=30,
                amount_kopeks=60000,
                user_id=recipient.id,
            )
            db.add(purchase)
            await db.commit()

            with patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()):
                activated = await activate_purchase(db, token, skip_notification=True)

            assert activated.status == GuestPurchaseStatus.DELIVERED.value

            await db.refresh(sub)
            assert sub.end_date > now + timedelta(days=29)
            assert sub.tariff_id == t2.id

    @pytest.mark.asyncio
    async def test_first_subscription_created_when_user_has_no_subs(self, monkeypatch):
        """In single-tariff mode, first gift claim creates initial paid subscription."""
        monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)

        async with memory_session(monkeypatch, _TABLES) as db:
            t1, _ = await _seed_tariffs_and_settings(db)

            recipient = User(telegram_id=22222, username='rec', balance_kopeks=0)
            db.add(recipient)
            await db.commit()
            await db.refresh(recipient)

            token = generate_purchase_token()
            purchase = GuestPurchase(
                token=token,
                contact_type='telegram',
                contact_value=str(recipient.telegram_id),
                is_gift=True,
                status=GuestPurchaseStatus.PENDING_ACTIVATION.value,
                tariff_id=t1.id,
                period_days=30,
                amount_kopeks=30000,
                user_id=recipient.id,
            )
            db.add(purchase)
            await db.commit()

            with patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()):
                activated = await activate_purchase(db, token, skip_notification=True)

            assert activated.status == GuestPurchaseStatus.DELIVERED.value

            subs_res = await db.execute(select(Subscription).where(Subscription.user_id == recipient.id))
            sub = subs_res.scalars().first()
            assert sub is not None
            assert sub.tariff_id == t1.id
            assert sub.status == SubscriptionStatus.ACTIVE.value


# ── Step 2: Remnawave Provisioning Invariants ───────────────────────────────


class TestGiftProvisioningInvariants:
    """Asserts Remnawave provisioning is strictly deferred until claim time."""

    @pytest.mark.asyncio
    async def test_purchase_time_never_invokes_remnawave_provisioning(self, monkeypatch):
        """Buying a gift from balance must never create or call Remnawave user APIs."""
        async with memory_session(monkeypatch, _TABLES) as db:
            t1, _ = await _seed_tariffs_and_settings(db)

            buyer = User(telegram_id=11111, username='buyer', balance_kopeks=50000)
            db.add(buyer)
            await db.commit()
            await db.refresh(buyer)

            quote = await quote_gift_purchase(db, buyer=buyer, tariff_id=t1.id, period_days=30)

            with patch(
                'app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()
            ) as mock_prov:
                purchase_res = await purchase_gift_from_balance(
                    db,
                    buyer_id=buyer.id,
                    tariff_id=t1.id,
                    period_days=30,
                    expected_price_kopeks=quote.final_price_kopeks,
                    idempotency_key='chk_prov_1',
                )

                # Invariant: zero provisioning calls at purchase time
                mock_prov.assert_not_called()
                assert purchase_res.purchase.status == GuestPurchaseStatus.PAID.value

    @pytest.mark.asyncio
    async def test_claim_time_invokes_remnawave_provisioning_exactly_once(self, monkeypatch):
        """Claiming a gift must call create_remnawave_user exactly once for the claimant."""
        async with memory_session(monkeypatch, _TABLES) as db:
            t1, _ = await _seed_tariffs_and_settings(db)

            recipient = User(telegram_id=22222, username='rec', balance_kopeks=0)
            db.add(recipient)
            await db.commit()
            await db.refresh(recipient)

            token = generate_purchase_token()
            purchase = GuestPurchase(
                token=token,
                contact_type='telegram',
                contact_value=str(recipient.telegram_id),
                is_gift=True,
                status=GuestPurchaseStatus.PENDING_ACTIVATION.value,
                tariff_id=t1.id,
                period_days=30,
                amount_kopeks=30000,
                user_id=recipient.id,
            )
            db.add(purchase)
            await db.commit()

            with patch(
                'app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()
            ) as mock_prov:
                await activate_purchase(db, token, skip_notification=True)

                # Invariant: exactly one provisioning call at claim time
                mock_prov.assert_awaited_once()
