"""Tribute «цифровой товар» — ссылки на товары по суммам (TRIBUTE_PRODUCT_LINKS)."""

import pytest

from app.config import settings
from app.external.tribute import TributeService, format_product_amounts, get_product_links, parse_product_links

LINK_100 = 'https://t.me/tribute/app?startapp=pAAA'
LINK_300 = 'https://t.me/tribute/app?startapp=pBBB'


def test_parse_basic_and_sorted_by_amount():
    raw = f'300={LINK_300},100={LINK_100}'

    assert parse_product_links(raw) == {10000: LINK_100, 30000: LINK_300}
    assert list(parse_product_links(raw)) == [10000, 30000]


@pytest.mark.parametrize('separator', [',', ';', '\n', ' ', ', ', ',\n'])
def test_parse_accepts_separators(separator):
    assert parse_product_links(f'100={LINK_100}{separator}300={LINK_300}') == {10000: LINK_100, 30000: LINK_300}


def test_parse_keeps_url_with_equals_and_query():
    url = 'https://web.tribute.tg/p/123?a=1&b=2'

    assert parse_product_links(f'500={url}') == {50000: url}


@pytest.mark.parametrize('raw', [None, '', '   ', '\n'])
def test_parse_empty(raw):
    assert parse_product_links(raw) == {}


@pytest.mark.parametrize(
    'bad',
    [
        'abc=https://t.me/x',  # сумма не число
        '0=https://t.me/x',  # нулевая сумма
        '-100=https://t.me/x',  # отрицательная
        '100=http://t.me/x',  # не https
        '100=',  # нет ссылки
        '100',  # нет '='
        '=https://t.me/x',  # нет суммы
        '100.5=https://t.me/x',  # дробная сумма
    ],
)
def test_parse_skips_bad_entries_but_keeps_good_ones(bad):
    assert parse_product_links(f'{bad},300={LINK_300}') == {30000: LINK_300}


def test_format_product_amounts():
    assert format_product_amounts({30000: LINK_300, 10000: LINK_100}) == '100, 300'


def test_get_product_links_reads_settings(monkeypatch):
    monkeypatch.setattr(settings, 'TRIBUTE_PRODUCT_LINKS', f'100={LINK_100}')

    assert get_product_links() == {10000: LINK_100}


@pytest.fixture
def tribute(monkeypatch):
    monkeypatch.setattr(settings, 'TRIBUTE_ENABLED', True)
    monkeypatch.setattr(settings, 'TRIBUTE_DONATE_LINK', 'https://t.me/tribute/app?startapp=dDON')
    monkeypatch.setattr(settings, 'TRIBUTE_PRODUCT_LINKS', f'100={LINK_100},300={LINK_300}')
    return TributeService()


@pytest.mark.asyncio
async def test_product_mode_returns_link_for_exact_amount(tribute):
    assert await tribute.create_payment_link(user_id=42, amount_kopeks=30000) == LINK_300
    assert await tribute.create_payment_link(user_id=42, amount_kopeks=10000) == LINK_100


@pytest.mark.asyncio
@pytest.mark.parametrize('amount', [0, None, 5000, 10001, 20000, 999999])
async def test_product_mode_rejects_amount_without_product(tribute, amount):
    assert await tribute.create_payment_link(user_id=42, amount_kopeks=amount) is None


@pytest.mark.asyncio
async def test_without_products_donation_link_is_used_as_before(tribute, monkeypatch):
    monkeypatch.setattr(settings, 'TRIBUTE_PRODUCT_LINKS', None)

    url = await tribute.create_payment_link(user_id=42, amount_kopeks=0)

    assert url == 'https://t.me/tribute/app?startapp=dDON&user_id=42'


@pytest.mark.asyncio
async def test_all_entries_broken_falls_back_to_donation(tribute, monkeypatch):
    monkeypatch.setattr(settings, 'TRIBUTE_PRODUCT_LINKS', 'мусор')

    assert await tribute.create_payment_link(user_id=42) == 'https://t.me/tribute/app?startapp=dDON&user_id=42'
