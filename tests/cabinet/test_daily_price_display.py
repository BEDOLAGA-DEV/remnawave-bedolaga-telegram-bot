"""Суточная цена в ответах кабинета — только с групповой скидкой, без промокода.

Контракт кабинета: сервер отдаёт цену со скидкой промогруппы, а активную скидку
промокода накладывает клиент для показа и сервер — при списании. Периоды так и
жили, а суточная цена в опциях покупки вкладывала промокод ещё на сервере —
карточка накладывала его второй раз: при промокоде 20 % человек видел −36 %
(9,60 ₽ из 15 ₽), экран активации — 12 ₽. Та же цена в ответе о подписке должна
совпадать с тем, что реально списывается каждый день (только группа).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.pricing_engine import PricingEngine


def _user(*, group_pct: int, offer_pct: int) -> SimpleNamespace:
    group = SimpleNamespace(id=7, name='Новый пользователь', get_discount_percent=lambda _cat, _days: group_pct)
    return SimpleNamespace(
        id=1,
        language='ru',
        promo_group=None,
        get_primary_promo_group=lambda: group if group_pct else None,
        promo_offer_discount_percent=offer_pct,
        promo_offer_discount_expires_at=None,
    )


class TestDailyGroupPrice:
    def test_only_group_discount_applies(self):
        price, pct = PricingEngine.daily_group_price(1500, _user(group_pct=20, offer_pct=20))
        assert (price, pct) == (1200, 20)

    def test_promo_offer_alone_changes_nothing(self):
        assert PricingEngine.daily_group_price(1500, _user(group_pct=0, offer_pct=20)) == (1500, 0)

    def test_no_user(self):
        assert PricingEngine.daily_group_price(1500, None) == (1500, 0)


class TestPurchaseOptionsDailyPrice:
    @pytest.mark.asyncio
    async def test_daily_price_excludes_promo_offer(self, monkeypatch):
        from app.cabinet.routes.subscription_modules.purchase import _build_tariff_response
        from app.database.models import Tariff

        tariff = Tariff(
            id=3,
            name='Суточный',
            description='',
            is_active=True,
            is_daily=True,
            daily_price_kopeks=1500,
            period_prices={},
            traffic_limit_gb=2,
            device_limit=1,
            allowed_squads=[],
            display_order=1,
        )
        data = await _build_tariff_response(SimpleNamespace(), tariff, user=_user(group_pct=0, offer_pct=20))
        assert data['daily_price_kopeks'] == 1500, 'промокод накладывает клиент, а не сервер'
        assert 'original_daily_price_kopeks' not in data
        assert 'daily_discount_percent' not in data

    @pytest.mark.asyncio
    async def test_daily_price_with_group_discount(self, monkeypatch):
        from app.cabinet.routes.subscription_modules.purchase import _build_tariff_response
        from app.database.models import Tariff

        tariff = Tariff(
            id=3,
            name='Суточный',
            description='',
            is_active=True,
            is_daily=True,
            daily_price_kopeks=1500,
            period_prices={},
            traffic_limit_gb=2,
            device_limit=1,
            allowed_squads=[],
            display_order=1,
        )
        data = await _build_tariff_response(SimpleNamespace(), tariff, user=_user(group_pct=20, offer_pct=20))
        assert data['daily_price_kopeks'] == 1200
        assert data['original_daily_price_kopeks'] == 1500
        assert data['daily_discount_percent'] == 20
