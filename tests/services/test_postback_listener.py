"""Тесты централизованного S2S postback listener'а."""

from unittest.mock import patch

import pytest

from app.services import postback_listener
from app.services.postback_listener import (
    _is_revenue_deposit,
    _on_payment_completed,
    register_postback_listeners,
)


# ---------- _is_revenue_deposit ----------


@pytest.mark.parametrize(
    'payload,expected',
    [
        ({'payment_method': 'yookassa'}, True),
        ({'payment_method': 'YOOKASSA'}, True),  # case-insensitive
        ({'payment_method': 'cryptobot', 'description': 'top-up via Bitcoin'}, True),
        ({}, False),  # missing payment_method — admin manual top-up
        ({'payment_method': None}, False),
        ({'payment_method': ''}, False),
        ({'payment_method': 'unknown_provider'}, False),  # not in allow-list
        ({'payment_method': 'yookassa', 'description': 'refund for #123'}, False),
        ({'payment_method': 'yookassa', 'description': 'Возврат средств'}, False),
    ],
)
def test_is_revenue_deposit(payload: dict, expected: bool) -> None:
    assert _is_revenue_deposit(payload) is expected


# ---------- _on_payment_completed guards ----------


@pytest.fixture
def settings_enabled(monkeypatch):
    monkeypatch.setattr(postback_listener.settings, 'S2S_POSTBACK_ENABLED', True, raising=False)
    monkeypatch.setattr(
        postback_listener.settings,
        'S2S_POSTBACK_PURCHASE_URL',
        'https://tracker.example.com/p?sub={subid}',
        raising=False,
    )


@pytest.mark.asyncio
async def test_feature_flag_off_skips_postback(monkeypatch) -> None:
    monkeypatch.setattr(postback_listener.settings, 'S2S_POSTBACK_ENABLED', False, raising=False)
    with patch.object(postback_listener, 'spawn_bg') as mock_spawn:
        await _on_payment_completed({'payload': {'user_id': 1, 'amount_rubles': 100}})
    mock_spawn.assert_not_called()


@pytest.mark.asyncio
async def test_purchase_url_empty_skips_postback(monkeypatch) -> None:
    monkeypatch.setattr(postback_listener.settings, 'S2S_POSTBACK_ENABLED', True, raising=False)
    monkeypatch.setattr(postback_listener.settings, 'S2S_POSTBACK_PURCHASE_URL', '', raising=False)
    with patch.object(postback_listener, 'spawn_bg') as mock_spawn:
        await _on_payment_completed({'payload': {'user_id': 1, 'amount_rubles': 100}})
    mock_spawn.assert_not_called()


@pytest.mark.asyncio
async def test_is_completed_false_skips_postback(settings_enabled) -> None:
    with patch.object(postback_listener, 'spawn_bg') as mock_spawn:
        await _on_payment_completed(
            {'payload': {'user_id': 1, 'amount_rubles': 100, 'is_completed': False, 'payment_method': 'yookassa'}},
        )
    mock_spawn.assert_not_called()


@pytest.mark.asyncio
async def test_zero_amount_skips_postback(settings_enabled) -> None:
    with patch.object(postback_listener, 'spawn_bg') as mock_spawn:
        await _on_payment_completed({'payload': {'user_id': 1, 'amount_rubles': 0, 'payment_method': 'yookassa'}})
        await _on_payment_completed(
            {'payload': {'user_id': 1, 'amount_rubles': -50, 'payment_method': 'yookassa'}},
        )
    mock_spawn.assert_not_called()


@pytest.mark.asyncio
async def test_missing_user_id_skips_postback(settings_enabled) -> None:
    with patch.object(postback_listener, 'spawn_bg') as mock_spawn:
        await _on_payment_completed({'payload': {'amount_rubles': 100, 'payment_method': 'yookassa'}})
    mock_spawn.assert_not_called()


@pytest.mark.asyncio
async def test_admin_manual_topup_skipped(settings_enabled) -> None:
    """DEPOSIT without payment_method = admin manual adjustment — no partner commission."""
    with patch.object(postback_listener, 'spawn_bg') as mock_spawn:
        await _on_payment_completed({'payload': {'user_id': 1, 'amount_rubles': 100}})
    mock_spawn.assert_not_called()


@pytest.mark.asyncio
async def test_refund_description_skipped(settings_enabled) -> None:
    with patch.object(postback_listener, 'spawn_bg') as mock_spawn:
        await _on_payment_completed(
            {
                'payload': {
                    'user_id': 1,
                    'amount_rubles': 100,
                    'payment_method': 'yookassa',
                    'description': 'refund for tx-9999',
                },
            },
        )
    mock_spawn.assert_not_called()


@pytest.mark.asyncio
async def test_valid_revenue_deposit_schedules_postback(settings_enabled) -> None:
    def _close_coro(coro):
        # Suppress 'coroutine was never awaited' since spawn_bg is mocked here.
        coro.close()

    with patch.object(postback_listener, 'spawn_bg', side_effect=_close_coro) as mock_spawn:
        await _on_payment_completed(
            {
                'payload': {
                    'user_id': 42,
                    'amount_rubles': 299,
                    'transaction_id': 11590,
                    'payment_method': 'yookassa',
                    'is_completed': True,
                },
            },
        )
    mock_spawn.assert_called_once()


# ---------- Idempotent registration ----------


def test_register_postback_listeners_is_idempotent(monkeypatch) -> None:
    """Calling twice must register the handler exactly once."""
    # Reset module state.
    monkeypatch.setattr(postback_listener, '_registered', False, raising=False)
    on_calls: list = []

    class _FakeEmitter:
        def on(self, event_name, handler):
            on_calls.append((event_name, handler))

    monkeypatch.setattr(postback_listener, 'event_emitter', _FakeEmitter())

    register_postback_listeners()
    register_postback_listeners()
    register_postback_listeners()

    assert len(on_calls) == 1
    assert on_calls[0][0] == 'payment.completed'
