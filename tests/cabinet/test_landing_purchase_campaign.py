"""Захват слага рекламной кампании при создании гостевой покупки.

Реклама ведёт на лендинг, а покупка оформляется гостем — без auth-флоу,
в котором кампания привязывается обычно. Слаг приезжает либо в теле запроса
(лендинг на любом домене), либо в куке ``campaign``, если лендинг стоит на
соседнем поддомене. Оплату подтверждает вебхук платёжки, где куки уже нет,
поэтому слаг обязан осесть в самой покупке.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.cabinet.routes.landing import _extract_campaign_slug


def _request(cookies: dict[str, str] | None = None) -> SimpleNamespace:
    """Минимальный дубль starlette Request — нужен только ``.cookies``."""
    return SimpleNamespace(cookies=cookies or {})


def test_body_slug_wins_over_cookie() -> None:
    assert _extract_campaign_slug('from_body', _request({'campaign': 'from_cookie'})) == 'from_body'


def test_cookie_is_used_when_body_has_no_slug() -> None:
    assert _extract_campaign_slug(None, _request({'campaign': 'from_cookie'})) == 'from_cookie'


def test_returns_none_without_any_source() -> None:
    assert _extract_campaign_slug(None, _request()) is None


@pytest.mark.parametrize(
    'bad',
    [
        '',
        'has space',
        'semi;colon',
        'слаг',
        'a' * 65,
        'trailing_newline\n',
    ],
)
def test_invalid_cookie_is_ignored(bad: str) -> None:
    assert _extract_campaign_slug(None, _request({'campaign': bad})) is None


def test_invalid_body_slug_does_not_fall_back_to_cookie() -> None:
    """Мусор в теле — ошибка вызывающей стороны, а не повод молча подставить
    другой источник и записать покупку не на ту кампанию."""
    assert _extract_campaign_slug('bad slug', _request({'campaign': 'good_one'})) is None


@pytest.mark.parametrize('blank', ['', '   '], ids=['empty', 'whitespace'])
def test_blank_body_slug_still_lets_the_cookie_work(blank: str) -> None:
    """Пустое поле — «не прислали», а не «прислали мусор».

    Фронтенд, который всегда заполняет поле из хранилища, шлёт пустую строку,
    когда кампании нет. Считать её мусором значит выключить куку ровно на той
    инсталляции, ради которой она и заведена.
    """
    assert _extract_campaign_slug(blank, _request({'campaign': 'good_one'})) == 'good_one'


def test_trailing_newline_in_body_is_rejected() -> None:
    """re-шный «$» пропускает \\n в конце, pydantic-паттерн кабинета — нет.

    Без fullmatch «одинаковая» валидация слага разъезжается между ручками, и в
    purchase оседает значение, которое кампании уже не найдёт.
    """
    assert _extract_campaign_slug('promo\n', _request()) is None
