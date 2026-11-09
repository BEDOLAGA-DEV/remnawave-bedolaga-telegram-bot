"""Tribute «цифровой товар»: в списке способов кабинета суммы берутся из TRIBUTE_PRODUCT_LINKS,
свободный ввод отключается флагом `fixed_amounts_only`."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.schemas.balance import PaymentMethodResponse
from app.config import settings
from app.services import payment_method_config_service as service


LINKS = (
    '100=https://web.tribute.tg/p/AAA,150=https://web.tribute.tg/p/BBB,'
    '420=https://web.tribute.tg/p/CCC,780=https://web.tribute.tg/p/DDD'
)


def _config(**overrides):
    values = {
        'method_id': 'tribute',
        'is_enabled': True,
        'user_type_filter': 'all',
        'first_topup_filter': 'any',
        'promo_group_filter_mode': 'all',
        'allowed_promo_groups': [],
        'display_name': None,
        'description': None,
        'min_amount_kopeks': None,
        'max_amount_kopeks': None,
        'quick_amounts': None,
        'sub_options': None,
        'sort_order': 2,
        'open_url_direct': False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def tribute_methods(monkeypatch):
    """Вызов get_enabled_methods_for_user с настройками Tribute и одной строкой конфигурации."""

    async def call(*, links, donate_link='https://t.me/tribute/app?startapp=dDON', config=None):
        monkeypatch.setattr(settings, 'TRIBUTE_ENABLED', True)
        monkeypatch.setattr(settings, 'TRIBUTE_DONATE_LINK', donate_link)
        monkeypatch.setattr(settings, 'TRIBUTE_PRODUCT_LINKS', links)
        monkeypatch.setattr(service, 'get_all_configs', AsyncMock(return_value=[config or _config()]))
        return await service.get_enabled_methods_for_user(db=None, user=None)

    return call


@pytest.mark.asyncio
async def test_products_define_amounts_limits_and_fixed_flag(tribute_methods):
    [method] = await tribute_methods(links=LINKS)

    assert method['quick_amounts'] == [10000, 15000, 42000, 78000]
    assert method['min_amount_kopeks'] == 10000
    assert method['max_amount_kopeks'] == 78000
    assert method['fixed_amounts_only'] is True


@pytest.mark.asyncio
async def test_products_override_db_quick_amounts_and_limits(tribute_methods):
    config = _config(quick_amounts=[10000, 30000, 50000], min_amount_kopeks=100, max_amount_kopeks=99999999)

    [method] = await tribute_methods(links=LINKS, config=config)

    assert method['quick_amounts'] == [10000, 15000, 42000, 78000]
    assert (method['min_amount_kopeks'], method['max_amount_kopeks']) == (10000, 78000)


@pytest.mark.asyncio
async def test_donation_mode_keeps_free_input(tribute_methods):
    config = _config(quick_amounts=[10000, 30000], min_amount_kopeks=100, max_amount_kopeks=5000000)

    [method] = await tribute_methods(links=None, config=config)

    assert method['fixed_amounts_only'] is False
    assert method['quick_amounts'] == [10000, 30000]
    assert (method['min_amount_kopeks'], method['max_amount_kopeks']) == (100, 5000000)


@pytest.mark.asyncio
async def test_all_links_broken_falls_back_to_donation_mode(tribute_methods):
    [method] = await tribute_methods(links='мусор')

    assert method['fixed_amounts_only'] is False


@pytest.mark.asyncio
async def test_method_is_listed_with_products_only_and_no_donation_link(tribute_methods):
    methods = await tribute_methods(links=LINKS, donate_link=None)

    assert [m['id'] for m in methods] == ['tribute']


@pytest.mark.asyncio
async def test_method_is_hidden_without_donation_link_and_products(tribute_methods):
    assert await tribute_methods(links=None, donate_link=None) == []


def test_response_schema_defaults_to_free_input():
    method = PaymentMethodResponse(id='x', name='X', min_amount_kopeks=100, max_amount_kopeks=1000)

    assert method.fixed_amounts_only is False
    assert PaymentMethodResponse(
        id='x', name='X', min_amount_kopeks=100, max_amount_kopeks=1000, fixed_amounts_only=True
    ).model_dump()['fixed_amounts_only'] is True
