"""Cross-channel acceptance integration tests for gift lifecycle (Task 7).

Acceptance Matrix (Step 1):
1. Bot purchase -> Bot activation
2. Bot purchase -> Cabinet activation
3. Cabinet balance purchase -> Bot activation
4. Cabinet gateway purchase (after paid webhook) -> Bot activation
5. Cabinet purchase -> Cabinet activation
6. Recovery in bot "My gifts" across all purchase origins
7. Self-claim and competing-claimant rejection across channels
8. Idempotency on repeated same-user claim across channels

Backward Compatibility (Step 2):
1. Historical full-token gifts receive canonical representation without migration
2. Legacy short cabinet codes (8-char, 12-char, GIFT-<12>) succeed in cabinet
3. Strict 48-char prefix enforcement in Telegram bot (short codes rejected)
4. Directed gift activation via claim_bound_gift_for_user
5. Public email / guest landing gifts claimed via canonical code or web URL
6. Legacy API response fields preserved alongside new canonical fields
"""

from __future__ import annotations

import html
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, User as TgUser
from fastapi import HTTPException
from sqlalchemy import select

from app.cabinet.routes import gift as cabinet_gift_routes
from app.cabinet.routes.landing import _build_purchase_status_response
from app.cabinet.schemas.gift import ActivateGiftRequest, GiftPurchaseRequest
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
from app.handlers.subscription.gift import (
    handle_gift_code_input,
    handle_gift_my,
    handle_gift_my_open,
)
from app.services.gift_claim_service import (
    GiftClaimAlreadyOwnedError,
    GiftClaimSelfActivationError,
    claim_bound_gift_for_user,
)
from app.services.gift_history_service import list_sender_gifts
from app.services.gift_purchase_service import (
    GIFT_ENABLED_KEY,
    purchase_gift_from_balance,
    quote_gift_purchase,
)
from app.states import GiftActivationStates
from app.utils.gift_links import (
    build_gift_public_code,
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


def _callbacks(keyboard: InlineKeyboardMarkup) -> list[str]:
    """Extract callback_data from an inline keyboard."""
    return [button.callback_data for row in keyboard.inline_keyboard for button in row if button.callback_data]


def _urls(keyboard: InlineKeyboardMarkup) -> list[str]:
    """Extract URLs from all url buttons in an inline keyboard."""
    return [button.url for row in keyboard.inline_keyboard for button in row if button.url]


def _make_fsm_context(user_id: int, chat_id: int | None = None) -> FSMContext:
    storage = MemoryStorage()
    c_id = chat_id if chat_id is not None else user_id
    key = StorageKey(bot_id=1, chat_id=c_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


def _make_message(text: str | None, user_id: int = 12345, username: str = 'tester') -> Message:
    msg = MagicMock(spec=Message)
    msg.message_id = 100
    msg.text = text
    msg.from_user = TgUser(id=user_id, is_bot=False, first_name='TestUser', username=username)
    msg.chat = Chat(id=user_id, type='private')
    msg.answer = AsyncMock()
    msg.reply = AsyncMock()
    msg.bot = AsyncMock()
    return msg


def _make_callback(user: User, data: str = 'gift_my') -> AsyncMock:
    callback = AsyncMock(spec=CallbackQuery)
    callback.from_user = TgUser(
        id=user.telegram_id or 99999,
        is_bot=False,
        first_name='TestUser',
        username=user.username or 'buyer',
    )
    callback.message = AsyncMock(spec=Message)
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    callback.data = data
    callback.bot = AsyncMock()
    callback.bot.get_me = AsyncMock(return_value=MagicMock(username='test_vpn_bot'))
    return callback


async def _seed_scenario(db) -> tuple[Tariff, User, User, User]:
    """Seeds system settings, active gift tariff, buyer, and two claimants."""
    setting = SystemSetting(key=GIFT_ENABLED_KEY, value='true')
    tariff = Tariff(
        id=1,
        name='FastVPN Ultimate',
        is_active=True,
        show_in_gift=True,
        device_limit=3,
        traffic_limit_gb=150,
        period_prices={'30': 30000, '90': 80000},
        display_order=1,
    )
    buyer = User(
        id=10,
        telegram_id=10001,
        username='buyer_user',
        balance_kopeks=200000,
        email='buyer@example.com',
        language='ru',
    )
    claimant1 = User(
        id=20,
        telegram_id=20002,
        username='claimant_alice',
        balance_kopeks=0,
        email='alice@example.com',
        language='ru',
    )
    claimant2 = User(
        id=30,
        telegram_id=30003,
        username='claimant_bob',
        balance_kopeks=0,
        email='bob@example.com',
        language='ru',
    )
    db.add_all([setting, tariff, buyer, claimant1, claimant2])
    await db.commit()
    await db.refresh(tariff)
    await db.refresh(buyer)
    await db.refresh(claimant1)
    await db.refresh(claimant2)
    return tariff, buyer, claimant1, claimant2


# ── Acceptance Matrix Tests (Step 1) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_lifecycle_bot_purchase_to_bot_activation(monkeypatch):
    """1. Bot purchase -> Bot activation:
    Buyer purchases with balance in bot, claimant activates via bot message input.
    Verifies subscription delivery, no balance debit for claimant, and rejection of self/competing claims.
    """
    async with memory_session(monkeypatch, _TABLES) as db:
        tariff, buyer, claimant1, claimant2 = await _seed_scenario(db)

        # Buyer purchases gift in bot
        quote = await quote_gift_purchase(db, buyer=buyer, tariff_id=tariff.id, period_days=30)
        p_res = await purchase_gift_from_balance(
            db,
            buyer_id=buyer.id,
            tariff_id=tariff.id,
            period_days=30,
            expected_price_kopeks=quote.final_price_kopeks,
            idempotency_key='lifecycle_bot_to_bot_1',
        )
        token = p_res.purchase.token
        canonical_code = build_gift_public_code(token)

        # 1a. Buyer attempts self-claim in bot -> rejected
        buyer_state = _make_fsm_context(buyer.telegram_id)
        await buyer_state.set_state(GiftActivationStates.waiting_for_code)
        buyer_msg = _make_message(text=canonical_code, user_id=buyer.telegram_id, username=buyer.username)
        await handle_gift_code_input(buyer_msg, buyer, db, buyer_state)

        buyer_msg.answer.assert_awaited_once()
        ans_text = buyer_msg.answer.call_args[0][0]
        assert 'свой собственный подарок' in ans_text
        assert token not in ans_text

        # Purchase remains in PAID status, unassigned
        p_db = (await db.execute(select(GuestPurchase).where(GuestPurchase.token == token))).scalar_one()
        assert p_db.status == GuestPurchaseStatus.PAID.value
        assert p_db.user_id is None

        # 1b. Claimant 1 activates in bot -> succeeds
        claimant_state = _make_fsm_context(claimant1.telegram_id)
        await claimant_state.set_state(GiftActivationStates.waiting_for_code)
        claimant_msg = _make_message(text=canonical_code, user_id=claimant1.telegram_id, username=claimant1.username)

        with patch(
            'app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()
        ) as mock_prov:
            await handle_gift_code_input(claimant_msg, claimant1, db, claimant_state)
            mock_prov.assert_awaited_once()

        assert await claimant_state.get_state() is None
        claimant_msg.answer.assert_awaited_once()
        success_text = claimant_msg.answer.call_args[0][0]
        assert 'Подарок активирован' in success_text
        assert html.escape(tariff.name) in success_text

        # Invariants: purchase is DELIVERED, claimant balance unchanged
        await db.refresh(claimant1)
        assert claimant1.balance_kopeks == 0
        p_db = (await db.execute(select(GuestPurchase).where(GuestPurchase.token == token))).scalar_one()
        assert p_db.status == GuestPurchaseStatus.DELIVERED.value
        assert p_db.user_id == claimant1.id
        assert p_db.delivered_at is not None

        # 1c. Competing Claimant 2 attempts activation -> rejected
        c2_state = _make_fsm_context(claimant2.telegram_id)
        await c2_state.set_state(GiftActivationStates.waiting_for_code)
        c2_msg = _make_message(text=canonical_code, user_id=claimant2.telegram_id, username=claimant2.username)
        await handle_gift_code_input(c2_msg, claimant2, db, c2_state)

        c2_msg.answer.assert_awaited_once()
        c2_text = c2_msg.answer.call_args[0][0]
        assert 'уже был активирован' in c2_text
        assert token not in c2_text

        # 1d. Claimant 1 re-activates (idempotency) -> succeeds without double-provisioning
        c1_repeat_state = _make_fsm_context(claimant1.telegram_id)
        await c1_repeat_state.set_state(GiftActivationStates.waiting_for_code)
        c1_repeat_msg = _make_message(text=canonical_code, user_id=claimant1.telegram_id, username=claimant1.username)

        with patch(
            'app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()
        ) as mock_repeat_prov:
            await handle_gift_code_input(c1_repeat_msg, claimant1, db, c1_repeat_state)
            mock_repeat_prov.assert_not_called()

        c1_repeat_msg.answer.assert_awaited_once()
        repeat_text = c1_repeat_msg.answer.call_args[0][0]
        assert 'Подарок активирован' in repeat_text


@pytest.mark.asyncio
async def test_lifecycle_bot_purchase_to_cabinet_activation(monkeypatch):
    """2. Bot purchase -> Cabinet activation:
    Buyer purchases in bot, claimant activates via cabinet /gift/activate endpoint.
    Verifies self-claim rejection, successful delivery, and competing claim rejection.
    """
    monkeypatch.setattr('app.utils.cache.RateLimitCache.is_rate_limited', AsyncMock(return_value=False))
    fake_activate = AsyncMock()
    monkeypatch.setattr('app.services.guest_purchase_service.activate_purchase', fake_activate)

    async with memory_session(monkeypatch, _TABLES) as db:
        tariff, buyer, claimant1, claimant2 = await _seed_scenario(db)

        # Buyer purchases in bot
        quote = await quote_gift_purchase(db, buyer=buyer, tariff_id=tariff.id, period_days=30)
        p_res = await purchase_gift_from_balance(
            db,
            buyer_id=buyer.id,
            tariff_id=tariff.id,
            period_days=30,
            expected_price_kopeks=quote.final_price_kopeks,
            idempotency_key='lifecycle_bot_to_cab_1',
        )
        token = p_res.purchase.token
        canonical_code = build_gift_public_code(token)

        # 2a. Buyer attempts self-claim in cabinet -> 400
        with pytest.raises(HTTPException) as exc_self:
            await cabinet_gift_routes.activate_gift_by_code(
                body=ActivateGiftRequest(code=canonical_code),
                user=buyer,
                db=db,
            )
        assert exc_self.value.status_code == 400
        assert exc_self.value.detail == 'Cannot activate your own gift'

        # 2b. Claimant 1 activates in cabinet -> succeeds
        res = await cabinet_gift_routes.activate_gift_by_code(
            body=ActivateGiftRequest(code=canonical_code),
            user=claimant1,
            db=db,
        )
        assert res.status == 'activated'
        assert res.tariff_name == tariff.name
        assert res.period_days == 30
        fake_activate.assert_awaited_once()

        # Database is updated
        p_db = (await db.execute(select(GuestPurchase).where(GuestPurchase.token == token))).scalar_one()
        assert p_db.user_id == claimant1.id

        # 2c. Competing Claimant 2 attempts activation in cabinet -> 404 (prevents token existence oracle)
        with pytest.raises(HTTPException) as exc_comp:
            await cabinet_gift_routes.activate_gift_by_code(
                body=ActivateGiftRequest(code=canonical_code),
                user=claimant2,
                db=db,
            )
        assert exc_comp.value.status_code == 404
        assert exc_comp.value.detail == 'Gift not found'

        # 2d. Claimant 1 re-activates in cabinet -> returns activated (idempotent)
        res_repeat = await cabinet_gift_routes.activate_gift_by_code(
            body=ActivateGiftRequest(code=canonical_code),
            user=claimant1,
            db=db,
        )
        assert res_repeat.status == 'activated'


@pytest.mark.asyncio
async def test_lifecycle_cabinet_balance_purchase_to_bot_activation(monkeypatch):
    """3. Cabinet balance purchase -> Bot activation:
    Buyer purchases via cabinet /gift/purchase with balance mode, claimant activates in bot via deep link.
    """
    from app.config import settings

    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.com')
    monkeypatch.setattr(settings, 'BOT_USERNAME', 'test_vpn_bot')
    monkeypatch.setattr('app.utils.cache.RateLimitCache.is_rate_limited', AsyncMock(return_value=False))

    async with memory_session(monkeypatch, _TABLES) as db:
        tariff, buyer, claimant1, claimant2 = await _seed_scenario(db)

        # Buyer purchases in cabinet using balance
        cab_req = GiftPurchaseRequest(
            tariff_id=tariff.id,
            period_days=30,
            payment_mode='balance',
            gift_message='Enjoy your VPN access!',
        )
        cab_resp = await cabinet_gift_routes.create_gift_purchase(body=cab_req, user=buyer, db=db)
        assert cab_resp.status == 'ok'
        assert cab_resp.gift_code is not None
        assert cab_resp.bot_claim_url is not None
        assert cab_resp.cabinet_claim_url is not None

        # Verify purchase token
        p_db = (
            await db.execute(
                select(GuestPurchase).where(
                    GuestPurchase.buyer_user_id == buyer.id, GuestPurchase.status == GuestPurchaseStatus.PAID.value
                )
            )
        ).scalar_one()
        assert p_db.source == 'cabinet'
        assert p_db.gift_message == 'Enjoy your VPN access!'

        # Claimant 1 activates in bot via the deep-link URL generated by cabinet
        c1_state = _make_fsm_context(claimant1.telegram_id)
        await c1_state.set_state(GiftActivationStates.waiting_for_code)
        c1_msg = _make_message(text=cab_resp.bot_claim_url, user_id=claimant1.telegram_id, username=claimant1.username)

        with patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()):
            await handle_gift_code_input(c1_msg, claimant1, db, c1_state)

        # Verification
        await db.refresh(p_db)
        assert p_db.status == GuestPurchaseStatus.DELIVERED.value
        assert p_db.user_id == claimant1.id

        c1_msg.answer.assert_awaited_once()
        assert 'Подарок активирован' in c1_msg.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_lifecycle_cabinet_gateway_purchase_after_webhook_to_bot_activation(monkeypatch):
    """4. Cabinet gateway purchase after paid webhook -> Bot activation:
    Buyer purchases via gateway (starts PENDING), webhook sets PAID, claimant activates in bot.
    """
    from app.config import settings

    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.com')
    monkeypatch.setattr(settings, 'BOT_USERNAME', 'test_vpn_bot')
    monkeypatch.setattr('app.utils.cache.RateLimitCache.is_rate_limited', AsyncMock(return_value=False))

    fake_payment_service = MagicMock()
    fake_payment_service.create_guest_payment = AsyncMock(
        return_value={'payment_url': 'https://pay.gateway.example/inv_777'}
    )
    monkeypatch.setattr('app.services.payment_service.PaymentService', lambda **kw: fake_payment_service)

    async with memory_session(monkeypatch, _TABLES) as db:
        tariff, buyer, claimant1, _ = await _seed_scenario(db)

        # Step 1: Buyer initiates gateway gift purchase
        req = GiftPurchaseRequest(
            tariff_id=tariff.id,
            period_days=30,
            payment_mode='gateway',
            payment_method='yookassa',
        )
        resp = await cabinet_gift_routes.create_gift_purchase(body=req, user=buyer, db=db)
        assert resp.status == 'created'
        assert resp.gift_code is None  # Pending -> no claim fields

        # Purchase in DB is PENDING
        p_db = (await db.execute(select(GuestPurchase).where(GuestPurchase.buyer_user_id == buyer.id))).scalar_one()
        assert p_db.status == GuestPurchaseStatus.PENDING.value

        # Step 2: Claimant attempts activation before payment -> rejected (unactivatable)
        canonical_code = build_gift_public_code(p_db.token)
        c1_state = _make_fsm_context(claimant1.telegram_id)
        await c1_state.set_state(GiftActivationStates.waiting_for_code)
        c1_msg = _make_message(text=canonical_code, user_id=claimant1.telegram_id, username=claimant1.username)

        await handle_gift_code_input(c1_msg, claimant1, db, c1_state)
        c1_msg.answer.assert_awaited_once()
        assert 'невозможно активировать' in c1_msg.answer.call_args[0][0]

        # Step 3: Payment gateway webhook marks purchase PAID
        p_db.status = GuestPurchaseStatus.PAID.value
        await db.commit()

        # Step 4: Status endpoint now returns canonical fields
        status_resp = await cabinet_gift_routes.get_gift_purchase_status(token=p_db.token, user=buyer, db=db)
        assert status_resp.status == 'paid'
        assert status_resp.is_claimable is True
        assert status_resp.gift_code == canonical_code

        # Step 5: Claimant activates in bot -> succeeds
        c1_state2 = _make_fsm_context(claimant1.telegram_id)
        await c1_state2.set_state(GiftActivationStates.waiting_for_code)
        c1_msg2 = _make_message(text=canonical_code, user_id=claimant1.telegram_id, username=claimant1.username)

        with patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()):
            await handle_gift_code_input(c1_msg2, claimant1, db, c1_state2)

        await db.refresh(p_db)
        assert p_db.status == GuestPurchaseStatus.DELIVERED.value
        assert p_db.user_id == claimant1.id
        c1_msg2.answer.assert_awaited_once()
        assert 'Подарок активирован' in c1_msg2.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_lifecycle_cabinet_purchase_to_cabinet_activation(monkeypatch):
    """5. Cabinet purchase -> Cabinet activation:
    Full cabinet round-trip: purchased via cabinet balance, activated via cabinet code endpoint.
    """
    monkeypatch.setattr('app.utils.cache.RateLimitCache.is_rate_limited', AsyncMock(return_value=False))
    fake_activate = AsyncMock()
    monkeypatch.setattr('app.services.guest_purchase_service.activate_purchase', fake_activate)

    async with memory_session(monkeypatch, _TABLES) as db:
        tariff, buyer, claimant1, claimant2 = await _seed_scenario(db)

        # Buyer purchases in cabinet
        req = GiftPurchaseRequest(
            tariff_id=tariff.id,
            period_days=90,
            payment_mode='balance',
        )
        resp = await cabinet_gift_routes.create_gift_purchase(body=req, user=buyer, db=db)
        assert resp.status == 'ok'
        canonical_code = resp.gift_code

        # Self-claim rejected
        with pytest.raises(HTTPException) as exc_self:
            await cabinet_gift_routes.activate_gift_by_code(
                body=ActivateGiftRequest(code=canonical_code),
                user=buyer,
                db=db,
            )
        assert exc_self.value.status_code == 400
        assert exc_self.value.detail == 'Cannot activate your own gift'

        # Claimant 1 activates
        res = await cabinet_gift_routes.activate_gift_by_code(
            body=ActivateGiftRequest(code=canonical_code),
            user=claimant1,
            db=db,
        )
        assert res.status == 'activated'
        assert res.period_days == 90

        # Competing claim rejected with 404 (prevents token existence oracle)
        with pytest.raises(HTTPException) as exc_comp:
            await cabinet_gift_routes.activate_gift_by_code(
                body=ActivateGiftRequest(code=canonical_code),
                user=claimant2,
                db=db,
            )
        assert exc_comp.value.status_code == 404
        assert exc_comp.value.detail == 'Gift not found'


@pytest.mark.asyncio
async def test_recovery_of_all_purchase_origins_in_bot_my_gifts(monkeypatch):
    """6. Recovery of each successful source in bot "My gifts":
    A buyer with bot-purchased, cabinet-balance-purchased, and cabinet-gateway-purchased gifts
    can view all of them in bot "My gifts", inspect details, and copy canonical credentials without re-debit.
    """
    from app.config import settings

    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.com')
    monkeypatch.setattr(settings, 'BOT_USERNAME', 'test_vpn_bot')

    async with memory_session(monkeypatch, _TABLES) as db:
        tariff, buyer, claimant1, _ = await _seed_scenario(db)

        # 1. Bot-origin purchase (PAID / unactivated)
        token_bot = generate_purchase_token()
        p_bot = GuestPurchase(
            token=token_bot,
            contact_type='telegram',
            contact_value=str(buyer.telegram_id),
            tariff_id=tariff.id,
            period_days=30,
            amount_kopeks=30000,
            is_gift=True,
            status=GuestPurchaseStatus.PAID.value,
            buyer_user_id=buyer.id,
            source='bot',
        )

        # 2. Cabinet balance-origin purchase (PAID / unactivated)
        token_cab = generate_purchase_token()
        p_cab = GuestPurchase(
            token=token_cab,
            contact_type='email',
            contact_value=buyer.email,
            tariff_id=tariff.id,
            period_days=90,
            amount_kopeks=80000,
            is_gift=True,
            status=GuestPurchaseStatus.PAID.value,
            buyer_user_id=buyer.id,
            source='cabinet',
            gift_message='From Cabinet!',
        )

        # 3. Delivered gift (claimed by claimant1)
        token_deliv = generate_purchase_token()
        p_deliv = GuestPurchase(
            token=token_deliv,
            contact_type='telegram',
            contact_value=str(buyer.telegram_id),
            tariff_id=tariff.id,
            period_days=30,
            amount_kopeks=30000,
            is_gift=True,
            status=GuestPurchaseStatus.DELIVERED.value,
            buyer_user_id=buyer.id,
            user_id=claimant1.id,
            source='bot',
        )

        db.add_all([p_bot, p_cab, p_deliv])
        await db.commit()

        # Buyer opens "My gifts" in bot
        items, total = await list_sender_gifts(db, buyer_id=buyer.id)
        assert total == 3
        assert len(items) == 3

        # Open list in bot handler
        cb_list = _make_callback(user=buyer, data='gift_my')
        state = _make_fsm_context(buyer.telegram_id)
        await handle_gift_my(cb_list, buyer, db, state)

        cb_list.message.edit_text.assert_awaited_once()
        list_kb = cb_list.message.edit_text.call_args[1].get('reply_markup')
        callbacks = _callbacks(list_kb)
        assert f'gift_my_open:{p_bot.id}' in callbacks
        assert f'gift_my_open:{p_cab.id}' in callbacks
        assert f'gift_my_open:{p_deliv.id}' in callbacks

        # Open unactivated bot gift detail
        cb_open_bot = _make_callback(user=buyer, data=f'gift_my_open:{p_bot.id}')
        await handle_gift_my_open(cb_open_bot, buyer, db, state)

        cb_open_bot.message.edit_text.assert_awaited_once()
        bot_detail_text = cb_open_bot.message.edit_text.call_args[0][0]
        bot_detail_kb = cb_open_bot.message.edit_text.call_args[1].get('reply_markup')

        canonical_bot_code = build_gift_public_code(token_bot)
        assert canonical_bot_code in bot_detail_text
        assert f'<code>{canonical_bot_code}</code>' in bot_detail_text
        assert any('t.me/share/url' in u for u in _urls(bot_detail_kb))

        # Open unactivated cabinet gift detail
        cb_open_cab = _make_callback(user=buyer, data=f'gift_my_open:{p_cab.id}')
        await handle_gift_my_open(cb_open_cab, buyer, db, state)

        cb_open_cab.message.edit_text.assert_awaited_once()
        cab_detail_text = cb_open_cab.message.edit_text.call_args[0][0]
        canonical_cab_code = build_gift_public_code(token_cab)
        assert canonical_cab_code in cab_detail_text

        # Open delivered gift detail
        cb_open_deliv = _make_callback(user=buyer, data=f'gift_my_open:{p_deliv.id}')
        await handle_gift_my_open(cb_open_deliv, buyer, db, state)

        cb_open_deliv.message.edit_text.assert_awaited_once()
        deliv_text = cb_open_deliv.message.edit_text.call_args[0][0]
        deliv_kb = cb_open_deliv.message.edit_text.call_args[1].get('reply_markup')
        assert 'Активирован' in deliv_text
        assert build_gift_public_code(token_deliv) not in deliv_text
        assert _urls(deliv_kb) == []


# ── Backward Compatibility Acceptance Tests (Step 2) ─────────────────────────


@pytest.mark.asyncio
async def test_backward_compat_historical_full_token_derives_canonical_representation(monkeypatch):
    """Historical full-token gifts receive canonical representation without database migration."""
    from app.config import settings

    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.com')
    monkeypatch.setattr(settings, 'BOT_USERNAME', 'test_vpn_bot')
    monkeypatch.setattr('app.utils.cache.RateLimitCache.is_rate_limited', AsyncMock(return_value=False))

    async with memory_session(monkeypatch, _TABLES) as db:
        tariff, buyer, _, _ = await _seed_scenario(db)

        # Historical row with raw 64-character token
        historical_token = '11223344556677889900aabbccddeeff11223344556677889900aabbccddeeff'
        purchase = GuestPurchase(
            token=historical_token,
            contact_type='email',
            contact_value=buyer.email,
            tariff_id=tariff.id,
            period_days=30,
            amount_kopeks=30000,
            is_gift=True,
            status=GuestPurchaseStatus.PAID.value,
            buyer_user_id=buyer.id,
        )
        db.add(purchase)
        await db.commit()

        # 1. Cabinet purchase status endpoint returns canonical fields
        status_resp = await cabinet_gift_routes.get_gift_purchase_status(token=historical_token, user=buyer, db=db)
        expected_code = f'GIFT_{historical_token[:59]}'
        assert status_resp.gift_code == expected_code
        assert status_resp.bot_claim_url == f'https://t.me/test_vpn_bot?start={expected_code}'
        assert status_resp.cabinet_claim_url == f'https://cabinet.example.com/buy/gift/{historical_token}'

        # 2. Cabinet /gift/sent list returns canonical fields
        sent_resp = await cabinet_gift_routes.get_sent_gifts(user=buyer, db=db)
        assert len(sent_resp) == 1
        assert sent_resp[0].gift_code == expected_code
        assert sent_resp[0].token == historical_token[:12]  # legacy 12-char token preserved


@pytest.mark.asyncio
async def test_backward_compat_legacy_short_codes_in_cabinet_and_strict_in_bot(monkeypatch):
    """Legacy short codes (8-char, 12-char, GIFT-<12>) succeed in cabinet but are rejected in bot."""
    monkeypatch.setattr('app.utils.cache.RateLimitCache.is_rate_limited', AsyncMock(return_value=False))
    fake_activate = AsyncMock()
    monkeypatch.setattr('app.services.guest_purchase_service.activate_purchase', fake_activate)

    async with memory_session(monkeypatch, _TABLES) as db:
        tariff, buyer, claimant1, _ = await _seed_scenario(db)

        token = 'abcdefghij1234567890abcdefghijklmnopqrstuvwxyz0123456789abcdef01'
        purchase = GuestPurchase(
            token=token,
            contact_type='email',
            contact_value=buyer.email,
            tariff_id=tariff.id,
            period_days=30,
            amount_kopeks=30000,
            is_gift=True,
            status=GuestPurchaseStatus.PAID.value,
            buyer_user_id=buyer.id,
        )
        db.add(purchase)
        await db.commit()

        # In bot: 8-char and 12-char prefixes are rejected (strict 48-char prefix enforcement)
        bot_state = _make_fsm_context(claimant1.telegram_id)
        await bot_state.set_state(GiftActivationStates.waiting_for_code)
        bot_msg = _make_message(text=token[:12], user_id=claimant1.telegram_id, username=claimant1.username)

        await handle_gift_code_input(bot_msg, claimant1, db, bot_state)
        bot_msg.answer.assert_awaited_once()
        assert 'не найден' in bot_msg.answer.call_args[0][0].lower()
        assert await bot_state.get_state() == GiftActivationStates.waiting_for_code.state  # retryable

        # In cabinet: 8-char prefix succeeds (allow_legacy_short=True)
        res_8 = await cabinet_gift_routes.activate_gift_by_code(
            body=ActivateGiftRequest(code=token[:8]),
            user=claimant1,
            db=db,
        )
        assert res_8.status == 'activated'

        # Reset purchase
        purchase.status = GuestPurchaseStatus.PAID.value
        purchase.user_id = None
        await db.commit()

        # In cabinet: GIFT-<12> format succeeds
        res_legacy_prefix = await cabinet_gift_routes.activate_gift_by_code(
            body=ActivateGiftRequest(code=f'GIFT-{token[:12]}'),
            user=claimant1,
            db=db,
        )
        assert res_legacy_prefix.status == 'activated'


@pytest.mark.asyncio
async def test_backward_compat_directed_gift_callbacks_and_landing_public_email(monkeypatch):
    """Directed gift callbacks (claim_bound_gift_for_user) and public landing email gifts work seamlessly."""
    from app.config import settings

    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.com')
    monkeypatch.setattr(settings, 'BOT_USERNAME', 'test_vpn_bot')

    async with memory_session(monkeypatch, _TABLES) as db:
        tariff, buyer, claimant1, claimant2 = await _seed_scenario(db)

        # 1. Directed gift bound to claimant1 in PENDING_ACTIVATION status
        directed_token = generate_purchase_token()
        directed_p = GuestPurchase(
            token=directed_token,
            contact_type='telegram',
            contact_value=str(claimant1.telegram_id),
            tariff_id=tariff.id,
            period_days=30,
            amount_kopeks=30000,
            is_gift=True,
            status=GuestPurchaseStatus.PENDING_ACTIVATION.value,
            buyer_user_id=buyer.id,
            user_id=claimant1.id,
        )
        db.add(directed_p)
        await db.commit()

        # Claimant2 or buyer cannot claim bound directed gift
        with pytest.raises(GiftClaimAlreadyOwnedError):
            await claim_bound_gift_for_user(db, claimant_user_id=claimant2.id, purchase_id=directed_p.id)

        with pytest.raises(GiftClaimSelfActivationError):
            await claim_bound_gift_for_user(db, claimant_user_id=buyer.id, purchase_id=directed_p.id)

        # Correct claimant claims directed gift
        with patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()):
            claimed_dir = await claim_bound_gift_for_user(db, claimant_user_id=claimant1.id, purchase_id=directed_p.id)
        assert claimed_dir.status == GuestPurchaseStatus.DELIVERED.value
        assert claimed_dir.user_id == claimant1.id

        # 2. Public email gift from guest landing
        email_token = generate_purchase_token()
        email_p = GuestPurchase(
            token=email_token,
            contact_type='email',
            contact_value='public_buyer@example.com',
            tariff=tariff,
            tariff_id=tariff.id,
            period_days=30,
            amount_kopeks=30000,
            is_gift=True,
            status=GuestPurchaseStatus.PAID.value,
        )
        db.add(email_p)
        await db.commit()

        # Landing purchase status retains legacy fields + adds canonical fields
        landing_resp = _build_purchase_status_response(email_p)
        assert landing_resp.is_claimable is True
        assert landing_resp.claim_url == f'https://cabinet.example.com/buy/gift/{email_token}'
        assert landing_resp.bot_claim_link == f'https://t.me/test_vpn_bot?start=GIFT_{email_token[:59]}'
        assert landing_resp.gift_code == f'GIFT_{email_token[:59]}'
        assert landing_resp.bot_claim_url == f'https://t.me/test_vpn_bot?start=GIFT_{email_token[:59]}'
        assert landing_resp.cabinet_claim_url == f'https://cabinet.example.com/buy/gift/{email_token}'
