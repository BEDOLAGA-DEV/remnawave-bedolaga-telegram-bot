"""Необязательная принадлежность тарифа к коммерческому семейству.

Поле пока не включает взаимное исключение подписок и не меняет продажи.
"""

import pytest
from pydantic import ValidationError

from app.cabinet.schemas.tariffs import TariffCreateRequest, TariffUpdateRequest
from app.database.crud.tariff import create_tariff, normalize_entitlement_family, update_tariff
from app.database.models import PromoGroup, Tariff, tariff_promo_groups
from tests.fixtures.sqlite_memory import memory_session


TABLES = (PromoGroup.__table__, Tariff.__table__, tariff_promo_groups)


@pytest.mark.parametrize('family', ['main', 'metered_bypass', 'vpn-2', 'a'])
def test_valid_family_keys(family):
    assert normalize_entitlement_family(family) == family


@pytest.mark.parametrize('family', ['', 'Main', 'a/b', 'a b', '1main', 'm' * 65])
def test_invalid_family_keys(family):
    with pytest.raises(ValueError, match='entitlement_family'):
        normalize_entitlement_family(family)


@pytest.mark.asyncio
async def test_family_is_opt_in_and_can_be_cleared(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        old = await create_tariff(db, 'Обычный тариф')
        assert old.entitlement_family is None

        tagged = await create_tariff(db, 'VPN', entitlement_family='main')
        assert tagged.entitlement_family == 'main'
        await db.refresh(tagged)
        assert tagged.entitlement_family == 'main'

        tagged = await update_tariff(db, tagged, name='VPN+')
        assert tagged.entitlement_family == 'main'

        tagged = await update_tariff(db, tagged, entitlement_family='metered_bypass')
        assert tagged.entitlement_family == 'metered_bypass'

        tagged = await update_tariff(db, tagged, entitlement_family=None)
        assert tagged.entitlement_family is None

        await db.refresh(old)
        assert old.entitlement_family is None


@pytest.mark.asyncio
async def test_invalid_family_rejected_before_create_or_update(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        with pytest.raises(ValueError, match='entitlement_family'):
            await create_tariff(db, 'Неверный', entitlement_family='Main')

        tariff = await create_tariff(db, 'Верный', entitlement_family='main')
        with pytest.raises(ValueError, match='entitlement_family'):
            await update_tariff(db, tariff, entitlement_family='no spaces')
        assert tariff.entitlement_family == 'main'


def test_admin_schemas_validate_family_and_preserve_unset():
    created = TariffCreateRequest.model_validate({'name': 'VPN', 'period_prices': []})
    assert created.entitlement_family is None

    update = TariffUpdateRequest(name='VPN')
    assert 'entitlement_family' not in update.model_fields_set
    assert update.entitlement_family is None

    clear = TariffUpdateRequest(entitlement_family=None)
    assert 'entitlement_family' in clear.model_fields_set

    with pytest.raises(ValidationError):
        TariffUpdateRequest(entitlement_family='Bad Family')
