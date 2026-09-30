"""API-key payment method discovery against real PostgreSQL and provider flags."""

from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import PaymentMethodConfig, PromoGroup, Transaction, User, UserPromoGroup, WebApiToken
from app.services.web_api_token_service import web_api_token_service
from app.webapi.app import create_web_api_app
from app.webapi.dependencies import get_db_session
from tests.fixtures.postgres_db import postgres_session


pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]

API_KEY = 'payment-methods-test-key'
TABLES = [
    PaymentMethodConfig.__table__,
    PromoGroup.__table__,
    User.__table__,
    Transaction.__table__,
    WebApiToken.__table__,
]


@pytest.fixture
def provider_settings(monkeypatch):
    for key in type(settings).model_fields:
        if key.endswith('_ENABLED'):
            monkeypatch.setattr(settings, key, False)
    for key, value in {
        'WEB_API_DEFAULT_TOKEN': None,
        'TELEGRAM_STARS_ENABLED': True,
        'PLATEGA_ENABLED': True,
        'PLATEGA_MERCHANT_ID': 'test-merchant',
        'PLATEGA_SECRET': 'provider-secret',
        'PLATEGA_ACTIVE_METHODS': '2,11,13',
        'PLATEGA_DISPLAY_NAME': 'Platega',
        'CISPAY_ENABLED': True,
        'CISPAY_SHOP_ID': 'test-shop',
        'CISPAY_API_KEY': 'another-provider-secret',
        'CISPAY_DISPLAY_NAME': 'CisPay',
        'CISPAY_SBP_ENABLED': True,
        'CISPAY_SBP_DISPLAY_NAME': 'СБП (CisPay)',
    }.items():
        monkeypatch.setattr(settings, key, value)


@asynccontextmanager
async def payment_client(postgres_database, configs=()):
    async with postgres_session(postgres_database, TABLES) as db:
        db.add(
            WebApiToken(
                name='Payment methods integration',
                token_hash=web_api_token_service.hash_token(API_KEY),
                token_prefix=API_KEY[:8],
                is_active=True,
            )
        )
        db.add_all(configs)
        await db.commit()

        app = create_web_api_app()

        async def get_test_db():
            yield db

        app.dependency_overrides[get_db_session] = get_test_db
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            yield db, client


async def test_live_catalog_uses_database_names_order_and_enabled_suboptions(postgres_database, provider_settings):
    platega = PaymentMethodConfig(
        method_id='platega',
        sort_order=20,
        display_name='Platega',
        description='must-not-leak',
        sub_options={'2': True, '11': False, '13': True},
    )
    cispay = PaymentMethodConfig(method_id='cispay', sort_order=10, display_name='Быстрое СБП')
    disabled = PaymentMethodConfig(method_id='telegram_stars', sort_order=0, is_enabled=False)
    unconfigured = PaymentMethodConfig(method_id='cryptobot', sort_order=1, is_enabled=True)
    async with payment_client(postgres_database, [platega, cispay, disabled, unconfigured]) as (db, client):
        response = await client.get('/payment-methods', headers={'X-API-Key': API_KEY})
        assert response.status_code == 200
        assert response.json() == ['Быстрое СБП', 'СБП (QR) (Platega)', 'Криптовалюта (Platega)']
        assert 'must-not-leak' not in response.text
        assert 'provider-secret' not in response.text

        cispay.is_enabled = False
        platega.display_name = 'Другой провайдер'
        platega.sub_options = {'2': False, '11': True, '13': False}
        await db.commit()
        response = await client.get('/payment-methods', headers={'X-API-Key': API_KEY})
        assert response.json() == ['Карты (RUB) (Другой провайдер)']


async def test_empty_catalog_and_missing_provider_credentials(postgres_database, provider_settings, monkeypatch):
    async with payment_client(postgres_database) as (db, client):
        response = await client.get('/payment-methods', headers={'X-API-Key': API_KEY})
        assert response.status_code == 200
        assert response.json() == []
        db.add(PaymentMethodConfig(method_id='platega'))
        await db.commit()
        monkeypatch.setattr(settings, 'PLATEGA_SECRET', None)
        response = await client.get('/payment-methods', headers={'X-API-Key': API_KEY})
        assert response.json() == []


@pytest.mark.parametrize('key', [None, 'invalid-api-key'])
async def test_api_key_is_required(postgres_database, provider_settings, key):
    async with payment_client(postgres_database, [PaymentMethodConfig(method_id='cispay')]) as (_, client):
        response = await client.get('/payment-methods', headers={'X-API-Key': key} if key else {})
    assert response.status_code == 401
    assert 'СБП' not in response.text


async def test_user_type_and_completed_deposit_filters_match_cabinet(postgres_database, provider_settings):
    first_topup_only = PaymentMethodConfig(
        method_id='cispay',
        first_topup_filter='yes',
        user_type_filter='telegram',
        sub_options={'card': False, 'sbp': True},
    )
    email_only = PaymentMethodConfig(method_id='platega', user_type_filter='email', sub_options={'2': True})
    async with payment_client(postgres_database, [first_topup_only, email_only]) as (db, client):
        telegram_user = User(telegram_id=100, balance_kopeks=5000)
        email_user = User(email='payer@example.invalid', auth_type='email')
        db.add_all([telegram_user, email_user])
        await db.commit()

        response = await client.get(
            '/payment-methods', params={'user_id': telegram_user.id}, headers={'X-API-Key': API_KEY}
        )
        assert response.json() == ['СБП (CisPay)']
        db.add(Transaction(user_id=telegram_user.id, type='deposit', amount_kopeks=1000, is_completed=False))
        await db.commit()
        response = await client.get(
            '/payment-methods', params={'user_id': telegram_user.id}, headers={'X-API-Key': API_KEY}
        )
        assert response.json() == ['СБП (CisPay)']

        deposit = await db.scalar(select(Transaction).where(Transaction.user_id == telegram_user.id))
        deposit.is_completed = True
        await db.commit()
        response = await client.get(
            '/payment-methods', params={'user_id': telegram_user.id}, headers={'X-API-Key': API_KEY}
        )
        assert response.json() == []
        response = await client.get(
            '/payment-methods', params={'user_id': email_user.id}, headers={'X-API-Key': API_KEY}
        )
        assert response.json() == ['СБП (QR) (Platega)', 'Карты (RUB) (Platega)', 'Криптовалюта (Platega)']
        assert telegram_user.balance_kopeks == 5000


async def test_user_promo_group_filter_uses_legacy_and_m2m_membership(postgres_database, provider_settings):
    group = PromoGroup(name='Allowed payers')
    restricted = PaymentMethodConfig(
        method_id='cispay',
        promo_group_filter_mode='selected',
        allowed_promo_groups=[group],
        sub_options={'card': False, 'sbp': True},
    )
    async with payment_client(postgres_database, [restricted]) as (db, client):
        legacy_user = User(telegram_id=101, promo_group=group)
        m2m_user = User(telegram_id=102)
        outsider = User(telegram_id=103)
        db.add_all([legacy_user, m2m_user, outsider])
        await db.flush()
        db.add(UserPromoGroup(user_id=m2m_user.id, promo_group_id=group.id))
        await db.commit()
        for user, expected in ((legacy_user, ['СБП (CisPay)']), (m2m_user, ['СБП (CisPay)']), (outsider, [])):
            response = await client.get('/payment-methods', params={'user_id': user.id}, headers={'X-API-Key': API_KEY})
            assert response.status_code == 200
            assert response.json() == expected


@pytest.mark.parametrize('user_id,status_code', [(9999, 404), (0, 422), (-1, 422), ('invalid', 422)])
async def test_invalid_user_never_falls_back_to_global_catalog(
    postgres_database, provider_settings, user_id, status_code
):
    async with payment_client(postgres_database, [PaymentMethodConfig(method_id='cispay')]) as (_, client):
        response = await client.get('/payment-methods', params={'user_id': user_id}, headers={'X-API-Key': API_KEY})
    assert response.status_code == status_code
    assert 'СБП' not in response.text
