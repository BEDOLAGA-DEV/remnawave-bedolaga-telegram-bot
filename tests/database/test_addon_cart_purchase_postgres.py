"""Replay saved add-on carts with real PostgreSQL financial writes (TG-031).

Redis, panel and notifications use the existing suite's boundaries. This does
not replace the separate real Redis atomic-cleanup tests.
"""

import pytest

from tests.fixtures.postgres_db import postgres_session
from tests.services import test_addon_cart_real_purchase as scenarios


pytestmark = pytest.mark.postgres
_classic_mode = scenarios._classic_mode
cart_service = scenarios.cart_service


@pytest.mark.parametrize(
    'scenario',
    [
        scenarios.test_topup_really_buys_traffic_from_saved_cart,
        scenarios.test_topup_really_buys_devices_from_saved_cart,
        scenarios.test_button_really_buys_traffic_and_says_so,
        scenarios.test_button_really_buys_devices_and_says_so,
        scenarios.test_partial_topup_keeps_cart_and_intent,
        scenarios.test_shared_topup_hook_buys_traffic_for_every_provider,
    ],
    ids=lambda scenario: scenario.__name__,
)
async def test_saved_addon_cart_postgres(postgres_database, monkeypatch, cart_service, scenario):
    monkeypatch.setattr(
        scenarios, 'memory_session', lambda _monkeypatch, tables: postgres_session(postgres_database, tables)
    )
    await scenario(monkeypatch, cart_service)
