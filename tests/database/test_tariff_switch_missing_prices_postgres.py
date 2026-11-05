import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
from app.cabinet.routes.subscription_modules import tariff_switch
from app.database.models import Subscription, Tariff, Transaction
from tests.database.test_multi_tariff_switch_postgres import switch_data


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('endpoint', ['preview', 'switch'])
@pytest.mark.parametrize('prices', [{}, {'30': None}])
@pytest.mark.parametrize('source_daily', [False, True])
async def test_missing_price_is_rejected_without_mutation(
    postgres_database, monkeypatch, endpoint, prices, source_daily
):
    async with switch_data(postgres_database, monkeypatch) as (db, user):
        (await db.get(Tariff, 3)).period_prices = prices
        (await db.get(Tariff, 1)).is_daily = source_daily
        await db.commit()
        before = list(
            (
                await db.execute(
                    select(Subscription.id, Subscription.tariff_id, Subscription.end_date).order_by(Subscription.id)
                )
            ).all()
        )
        app = FastAPI()
        app.include_router(tariff_switch.router, prefix='/cabinet/subscription')
        app.dependency_overrides[get_current_cabinet_user] = lambda: user
        app.dependency_overrides[get_cabinet_db] = lambda: db
        path = '/cabinet/subscription/tariff/switch' + ('/preview' if endpoint == 'preview' else '')
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url='http://cabinet.example'
        ) as client:
            response = await client.post(path, params={'subscription_id': 11}, json={'tariff_id': 3, 'period_days': 30})
        assert response.status_code == 400, response.text
        await db.refresh(user)
        assert user.balance_kopeks == 10000
        assert (
            list(
                (
                    await db.execute(
                        select(Subscription.id, Subscription.tariff_id, Subscription.end_date).order_by(Subscription.id)
                    )
                ).all()
            )
            == before
        )
        assert list(await db.scalars(select(Transaction))) == []
