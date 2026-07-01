"""Source-level pin: cabinet ``/purchase-tariff`` must resolve an EXPIRED
trial of the SAME tariff when renewing, so it converts the trial in place
(same Remnawave user/link) instead of spawning a new subscription + new link.

Background — the bug this defends against
-----------------------------------------
Prod report (2026-06): a user whose 3-day trial has EXPIRED renews the same
("Базовый") tariff via the cabinet. ``purchase_tariff`` resolved the existing
subscription via ``get_subscription_by_user_and_tariff(user, tariff)`` WITHOUT
``include_inactive`` → that lookup only matches ACTIVE/TRIAL/LIMITED, so the
expired trial (``status='expired'``) was invisible → the trial got killed by
``deactivate_user_trial_subscriptions`` and a fresh ``create_paid_subscription``
ran → a NEW Remnawave user + NEW subscription link → the user has to re-add all
devices. 7 affected users on prod.

Fix shape
---------
Pass ``include_inactive=True`` to the tariff-level lookup so an EXPIRED (or
disabled) same-tariff subscription is found. It then flows into the existing
extend-in-place branch (``extend_subscription`` clears the trial flag) and
``update_remnawave_user`` (the row already carries ``remnawave_uuid``) → SAME
link. Picking a DIFFERENT tariff still returns ``None`` for that tariff → a new
subscription, which is the intended "same-tariff only" semantic.

A full integration test would need a real DB + FastAPI deps; this pins the
SOURCE-LEVEL contract — the bug class ("drop include_inactive") is grep-detectable.
"""

from __future__ import annotations

import ast
from pathlib import Path


PURCHASE_PATH = (
    Path(__file__).resolve().parents[2] / 'app' / 'cabinet' / 'routes' / 'subscription_modules' / 'purchase.py'
)


def _find_async_function(tree: ast.Module, name: str) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f'async function {name!r} not found in cabinet purchase.py')


def _function_source(source: str, func: ast.AsyncFunctionDef) -> str:
    lines = source.splitlines(keepends=True)
    end_line = func.end_lineno or len(lines)
    return ''.join(lines[func.lineno - 1 : end_line])


def test_purchase_tariff_tariff_lookup_includes_inactive() -> None:
    """REGRESSION: the ``get_subscription_by_user_and_tariff`` fallback CALL
    inside ``purchase_tariff`` must pass ``include_inactive=True`` so an EXPIRED
    same-tariff trial is found and converted in place rather than missed →
    killed → re-created with a new Remnawave link.
    """
    source = PURCHASE_PATH.read_text(encoding='utf-8')
    tree = ast.parse(source)
    func = _find_async_function(tree, 'purchase_tariff')
    body = _function_source(source, func)

    # The CALL (paren) — not the import line, which mentions the name without a
    # paren. The call may wrap across lines, so inspect a window over its args.
    call_idx = body.find('get_subscription_by_user_and_tariff(')
    assert call_idx >= 0, (
        'purchase_tariff must resolve the existing subscription by (user, tariff) '
        'via get_subscription_by_user_and_tariff'
    )
    call_window = body[call_idx : call_idx + 200]
    assert 'include_inactive=True' in call_window, (
        'purchase_tariff must call get_subscription_by_user_and_tariff(..., '
        'include_inactive=True) so an EXPIRED trial of the same tariff is found and '
        'converted in place. Without it the expired trial is missed, killed, and a '
        'new subscription with a new Remnawave link is created (prod bug 2026-06).'
    )
