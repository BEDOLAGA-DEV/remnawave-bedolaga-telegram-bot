from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.balance.shkeeper import process_shkeeper_payment_amount, start_shkeeper_payment


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.mark.anyio('asyncio')
async def test_start_shkeeper_payment_blocks_restricted_user() -> None:
    callback = SimpleNamespace(
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    db_user = SimpleNamespace(
        language='ru',
        restriction_topup=True,
        restriction_reason='Ограничено администратором',
    )
    state = AsyncMock()

    await start_shkeeper_payment(callback, db_user, state)

    callback.message.edit_text.assert_awaited_once()
    state.set_state.assert_not_awaited()


@pytest.mark.anyio('asyncio')
async def test_process_shkeeper_payment_amount_blocks_restricted_user() -> None:
    message = SimpleNamespace(answer=AsyncMock())
    db_user = SimpleNamespace(
        language='ru',
        restriction_topup=True,
        restriction_reason='Ограничено администратором',
    )
    state = AsyncMock()

    await process_shkeeper_payment_amount(
        message=message,
        db_user=db_user,
        db=object(),
        amount_kopeks=10000,
        state=state,
    )

    message.answer.assert_awaited_once()
    state.clear.assert_awaited_once()
