from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.handlers.balance.overpay import (
    OVERPAY_OPTION_MAP,
    OVERPAY_PAYMENT_METHODS,
    _available_options,
    process_overpay_payment_amount,
)
from app.localization.texts import get_texts


def _enable_overpay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'OVERPAY_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_USERNAME', 'login', raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_PASSWORD', 'secret', raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_PROJECT_ID', 'project', raising=False)


def test_option_map_covers_all_payment_methods() -> None:
    assert set(OVERPAY_OPTION_MAP) == OVERPAY_PAYMENT_METHODS
    assert OVERPAY_OPTION_MAP['overpay'] is None
    assert OVERPAY_OPTION_MAP['overpay_fps'] == 'fps'
    assert OVERPAY_OPTION_MAP['overpay_card'] == 'card'
    assert OVERPAY_OPTION_MAP['overpay_int'] == 'int'


def test_available_options_without_int(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_overpay(monkeypatch)
    monkeypatch.setattr(settings, 'OVERPAY_INT_ENABLED', False, raising=False)

    methods = [method for method, _ in _available_options(get_texts('ru'))]
    assert methods == ['overpay_fps', 'overpay_card']


def test_available_options_with_int(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_overpay(monkeypatch)
    monkeypatch.setattr(settings, 'OVERPAY_INT_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_RUB_PER_EUR', 100.0, raising=False)

    methods = [method for method, _ in _available_options(get_texts('ru'))]
    assert methods == ['overpay_fps', 'overpay_card', 'overpay_int']


@pytest.mark.anyio('asyncio')
async def test_int_disabled_mid_flow_rejects_and_clears_state(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_overpay(monkeypatch)
    monkeypatch.setattr(settings, 'OVERPAY_INT_ENABLED', False, raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_MIN_AMOUNT_KOPEKS', 10000, raising=False)
    monkeypatch.setattr(settings, 'OVERPAY_MAX_AMOUNT_KOPEKS', 10000000, raising=False)

    message = MagicMock()
    message.answer = AsyncMock()
    db_user = MagicMock()
    db_user.language = 'ru'
    db_user.restriction_topup = False
    state = MagicMock()
    state.get_data = AsyncMock(return_value={'payment_method': 'overpay_int'})
    state.clear = AsyncMock()

    await process_overpay_payment_amount(message, db_user, MagicMock(), 50000, state)

    state.clear.assert_awaited_once()
    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert text == get_texts('ru').t('OVERPAY_OPTION_UNAVAILABLE', 'Способ оплаты недоступен.')
