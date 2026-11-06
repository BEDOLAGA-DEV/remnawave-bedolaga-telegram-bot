"""Recurring purchase must select an owned row before any provider side effect."""

from datetime import UTC, datetime, timedelta
from importlib import import_module
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import select

from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.models import Base, Subscription, Tariff, Transaction, User
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres
PROVIDERS = [
    ('platega', 'sbp', 'enable_platega_sbp_recurring'),
    ('lava', 'lava', 'enable_lava_recurring'),
    ('cashera', 'cashera', 'enable_cashera_recurring'),
]


@pytest.mark.parametrize(('provider', 'purchase_name', 'enable_name'), PROVIDERS)
@pytest.mark.parametrize(
    'case', ['selected', 'foreign', 'missing', 'legacy', 'other_tariff', 'trial', 'disabled', 'active_collision']
)
@pytest.mark.parametrize('entry', ['service', 'route'])
@pytest.mark.parametrize('multi', [False, True])
async def test_selected_row_validated_before_provider(
    postgres_database, monkeypatch, provider, purchase_name, enable_name, case, entry, multi
):
    module = import_module(f'app.services.payment.{provider}')
    purchase = getattr(module, f'purchase_tariff_with_{purchase_name}_recurring')
    monkeypatch.setattr(type(settings), f'is_{provider}_recurrent_enabled', lambda self: True)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', multi)
    enable = AsyncMock(return_value={'status': 'pending', 'redirect_url': 'https://payments.example/test'})
    monkeypatch.setattr(module, enable_name, enable)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add_all(
            [
                User(id=1, telegram_id=101, balance_kopeks=10000),
                User(id=2, telegram_id=202),
                Tariff(id=1, name='Target', is_active=True, device_limit=1),
                Tariff(id=2, name='Other', is_active=True, device_limit=2),
            ]
        )
        now = datetime.now(UTC)
        for sub_id, owner, tariff_id, cap in [
            (10, 1, None if case == 'legacy' else 2 if case == 'other_tariff' else 1, 3),
            (20, 1, 1, 7),
            (21, 2, 1, 5),
        ]:
            db.add(
                Subscription(
                    id=sub_id,
                    user_id=owner,
                    tariff_id=tariff_id,
                    device_limit=cap,
                    is_trial=case == 'trial' and sub_id == 10,
                    status='disabled'
                    if case == 'disabled' and sub_id == 10
                    else 'expired'
                    if sub_id == 10 or (sub_id == 20 and case != 'active_collision')
                    else 'active',
                    end_date=now + timedelta(days=-1 if sub_id == 10 else 20),
                    remnawave_short_id=f'test-{sub_id}',
                )
            )
        await db.commit()
        user = await get_user_by_id(db, 1)
        tariff = await db.get(Tariff, 1)
        selected = 21 if case == 'foreign' else 999 if case == 'missing' else 10

        async def invoke():
            if entry == 'service':
                return await purchase(db, user=user, tariff=tariff, subscription_id=selected)
            route = import_module(f'app.cabinet.routes.subscription_modules.{provider}_recurrent')
            if entry == 'http':
                app = FastAPI()
                app.add_api_route('/purchase', getattr(route, f'purchase_with_{provider}_recurrent'), methods=['POST'])

                async def current_user():
                    return user

                async def current_db():
                    return db

                app.dependency_overrides[route.get_current_cabinet_user] = current_user
                app.dependency_overrides[route.get_cabinet_db] = current_db
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url='https://cabinet.example'
                ) as client:
                    response = await client.post('/purchase', params={'tariff_id': 1, 'subscription_id': selected})
                assert response.status_code == 200, response.text
                return response.json()
            return await getattr(route, f'purchase_with_{provider}_recurrent')(
                tariff_id=1,
                subscription_id=selected,
                user=user,
                db=db,
            )

        if case == 'selected':
            result = await invoke()
            assert result['subscription_id'] == 10
            assert enable.await_args.kwargs['subscription'].id == 10
        else:
            with pytest.raises(ValueError if entry == 'service' else HTTPException) as error:
                await invoke()
            if entry == 'route':
                assert error.value.status_code == 400
            enable.assert_not_awaited()
        assert len((await db.scalars(select(Subscription))).all()) == 3
        assert user.balance_kopeks == 10000
        assert (await db.scalars(select(Transaction))).all() == []
        assert (await db.get(Subscription, 10)).device_limit == 3
        assert (await db.get(Subscription, 20)).device_limit == 7


@pytest.mark.parametrize(('provider', 'purchase_name', 'enable_name'), PROVIDERS)
async def test_http_query_reaches_owned_selected_row(
    postgres_database, monkeypatch, provider, purchase_name, enable_name
):
    await test_selected_row_validated_before_provider(
        postgres_database,
        monkeypatch,
        provider,
        purchase_name,
        enable_name,
        'selected',
        'http',
        True,
    )
