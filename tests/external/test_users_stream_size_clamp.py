"""`/api/users/stream` валидирует size вилкой 1..1000 (zod-контракт панели 2.8.x).

Регрессия: TRAFFIC_CHECK_BATCH_SIZE настраиваемый и со времён offset-пагинации
мог быть >1000 — панель отвечала «Validation failed», и мониторинг трафика
падал на первой же странице. Клиент обязан клэмпить size в допустимую вилку.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.external.remnawave_api import RemnaWaveAPI


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('requested', 'sent'),
    [
        (5000, 1000),  # старые конфиги с батчем больше панельного максимума
        (1000, 1000),
        (500, 500),
        (0, 1),
        (-5, 1),
    ],
)
async def test_stream_size_clamped_to_panel_contract(requested: int, sent: int):
    api = RemnaWaveAPI(base_url='http://panel', api_key='key')
    api._make_request = AsyncMock(return_value={'response': {'users': [], 'nextCursor': None, 'hasMore': False}})

    await api.get_all_users_page_stream(size=requested)

    params = api._make_request.await_args.kwargs.get('params') or api._make_request.await_args.args[-1]
    assert params['size'] == sent
