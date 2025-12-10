import pytest
from unittest.mock import AsyncMock, MagicMock
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User, Subscription


DEFAULT_USER_ID = 12345
DEFAULT_USER_TELEGRAM_ID = 12345
DEFAULT_USER_BALANCE_KOPEKS = 50000
DEFAULT_PERIOD_DAYS = 30
DEFAULT_TOTAL_PRICE = 15000
DEFAULT_MONTHS_IN_PERIOD = 1

TEST_COUNTRY_UUID_1 = "country-uuid-1"
TEST_COUNTRY_UUID_2 = "country-uuid-2"

ERROR_COUNTRIES_REQUIRED = "❌ Нельзя отключить все страны. Должна быть подключена хотя бы одна страна."


BASE_STATE_DATA = {
    'period_days': DEFAULT_PERIOD_DAYS,
    'devices': 3,
    'traffic_gb': 200,
    'total_price': DEFAULT_TOTAL_PRICE,
    'base_price': DEFAULT_TOTAL_PRICE,
    'base_price_original': DEFAULT_TOTAL_PRICE,
    'base_discount_percent': 0,
    'base_discount_total': 0,
    'months_in_period': DEFAULT_MONTHS_IN_PERIOD,
    'server_prices_for_period': [],
    'total_servers_price': 0,
    'servers_price_per_month': 0,
    'servers_discounted_price_per_month': 0,
    'servers_discount_total': 0,
    'servers_discount_percent': 0,
    'devices_price_per_month': 0,
    'devices_discounted_price_per_month': 0,
    'devices_discount_total': 0,
    'devices_discount_percent': 0,
    'traffic_price_per_month': 0,
    'traffic_discounted_price_per_month': 0,
    'traffic_discount_total': 0,
    'traffic_discount_percent': 0,
    'total_price_before_promo_offer': DEFAULT_TOTAL_PRICE,
    'promo_offer_discount_value': 0,
    'discounted_monthly_additions': 0,
}


@pytest.fixture
def mock_callback_query():
    callback = AsyncMock(spec=CallbackQuery)
    callback.message = AsyncMock(spec=Message)
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    callback.data = "subscription_confirm"
    callback.bot = AsyncMock()
    return callback


@pytest.fixture
def mock_user():
    user = AsyncMock(spec=User)
    user.id = DEFAULT_USER_ID
    user.telegram_id = DEFAULT_USER_TELEGRAM_ID
    user.language = "ru"
    user.balance_kopeks = DEFAULT_USER_BALANCE_KOPEKS
    user.subscription = None
    user.promo_group_id = None
    user.get_promo_discount = MagicMock(return_value=0)
    user.promo_offer_discount_percent = 0
    user.promo_offer_discount_expires_at = None
    return user


@pytest.fixture
def mock_user_with_subscription(mock_user):
    subscription = AsyncMock(spec=Subscription)
    subscription.is_trial = False
    subscription.status = "active"
    subscription.connected_squads = [TEST_COUNTRY_UUID_1]
    subscription.start_date = None
    subscription.end_date = None
    subscription.traffic_limit_gb = 100
    subscription.device_limit = 3
    subscription.traffic_used_gb = 0.0
    
    mock_user.subscription = subscription
    return mock_user


@pytest.fixture
def mock_db():
    db = AsyncMock(spec=AsyncSession)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


@pytest.fixture
def mock_state_with_countries():
    state = AsyncMock(spec=FSMContext)
    state_data = {
        **BASE_STATE_DATA,
        'countries': [TEST_COUNTRY_UUID_1, TEST_COUNTRY_UUID_2],
    }
    state.get_data = AsyncMock(return_value=state_data)
    return state


@pytest.fixture
def mock_state_without_countries():
    state = AsyncMock(spec=FSMContext)
    state_data = {
        **BASE_STATE_DATA,
        'countries': [],
    }
    state.get_data = AsyncMock(return_value=state_data)
    return state
