"""REGRESSION: the tariff purchase confirm PREVIEW must never lock the user out
of the device +/- controls.

Bug (2026-06-21): the confirm screen is re-rendered by `build_period_confirm`
on every device +/- press. The old code switched to the insufficient-balance
screen (top-up-only keyboard, no device controls) AND saved a cart whenever the
selected device count pushed the price above the balance. Raising devices past
your balance therefore trapped you — you could not lower them again, and were
asked to top up even though you only wanted to browse prices.

Fix: `build_period_confirm` is a pure preview — it always returns the
device-selection confirm keyboard and never saves a cart. Balance is enforced
only at confirm time (`confirm_tariff_purchase`), which shows an alert and
leaves the screen (with device controls) in place.

Source-level guard (a full behavioural test needs DB + Redis + aiogram FSM).
"""

from __future__ import annotations

import ast
from pathlib import Path


TARIFF_PURCHASE_PATH = Path(__file__).resolve().parents[2] / 'app' / 'handlers' / 'subscription' / 'tariff_purchase.py'


def _function_source(name: str) -> str:
    source = TARIFF_PURCHASE_PATH.read_text(encoding='utf-8')
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            lines = source.splitlines(keepends=True)
            return ''.join(lines[node.lineno - 1 : node.end_lineno or len(lines)])
    raise AssertionError(f'async function {name!r} not found')


def test_preview_never_uses_topup_only_keyboard() -> None:
    body = _function_source('build_period_confirm')
    assert 'get_tariff_insufficient_balance_keyboard' not in body, (
        'build_period_confirm (purchase preview) must NOT swap to the top-up-only '
        'keyboard — it strips the device +/- controls and traps the user. Always '
        'render the device-selection confirm keyboard; enforce balance at confirm.'
    )


def test_preview_does_not_save_cart() -> None:
    body = _function_source('build_period_confirm')
    assert 'save_user_cart' not in body, (
        'build_period_confirm (purchase preview) must NOT save a cart while the '
        'user is still browsing/adjusting devices — saving on every over-balance '
        'preview is the bug that produced the misleading "Корзина сохранена".'
    )


def test_confirm_still_guards_balance() -> None:
    # The balance check must live at confirm time so the preview can stay open.
    body = _function_source('confirm_tariff_purchase')
    assert 'user_balance < final_price' in body, (
        'confirm_tariff_purchase must keep the balance guard — that is where '
        'insufficient funds are rejected once the preview moved the check out.'
    )
