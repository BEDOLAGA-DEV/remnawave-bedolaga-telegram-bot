"""Бот, «Доходы за период»: «сегодня» и «вчера» — календарные дни settings.TIMEZONE (#3136).

Зона выбирается так, чтобы в момент прогона её дата отличалась от даты UTC:
тогда код «сегодня = дата по UTC» гарантированно не находит ни одной строки.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.handlers.admin import statistics
from tests.fixtures.local_day import (  # noqa: F401
    reset_local_timezone_cache,
    use_timezone,
    zone_where_local_date_differs_from_utc,
)


def _callback(period: str):
    message = SimpleNamespace(edit_text=AsyncMock())
    return SimpleNamespace(
        data=f'period_{period}', message=message, answer=AsyncMock(), from_user=SimpleNamespace(id=1)
    )


@pytest.fixture
def local_days(monkeypatch, reset_local_timezone_cache):
    tz = use_timezone(monkeypatch, zone_where_local_date_differs_from_utc())
    today = datetime.now(tz).date()
    revenue = AsyncMock(
        return_value=[
            {'date': today - timedelta(days=1), 'amount_kopeks': 200},
            {'date': today, 'amount_kopeks': 400},
        ]
    )
    monkeypatch.setattr(statistics, 'get_revenue_by_period', revenue)
    return revenue


@pytest.mark.asyncio
async def test_today_is_the_local_calendar_day(local_days):
    callback = _callback('today')

    await inspect.unwrap(statistics.show_revenue_by_period)(callback, db_user=SimpleNamespace(id=1), db=None)

    text = callback.message.edit_text.await_args.args[0]
    assert f'Общий доход: {settings.format_price(400)}' in text
    assert 'Дней с данными: 1' in text


@pytest.mark.asyncio
async def test_yesterday_is_the_previous_local_day_and_needs_two_days_of_data(local_days):
    callback = _callback('yesterday')

    await inspect.unwrap(statistics.show_revenue_by_period)(callback, db_user=SimpleNamespace(id=1), db=None)

    # За один день данных «вчера» пусто по построению: нужно запрашивать два.
    assert local_days.await_args.args[1] == 2
    text = callback.message.edit_text.await_args.args[0]
    assert f'Общий доход: {settings.format_price(200)}' in text
