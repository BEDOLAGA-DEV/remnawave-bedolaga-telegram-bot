"""Source-level pin: cabinet ``/purchase-tariff`` must compensate (refund) when
the subscription fails to persist AFTER the balance charge is committed.

Background — the bug this defends against (#3031)
-------------------------------------------------
``subtract_user_balance`` and ``create_transaction`` commit immediately; the
subscription is created/extended afterwards. Any failure in between — an
IntegrityError from a tariff-switch race that the narrow create-branch handler
does not catch (e.g. raised by ``extend_subscription``), a transient DB/driver
error ("Single-row INSERT ... did not produce a new primary key result"), a
FlushError — used to bubble into the route-level ``except Exception``, which
returns HTTP 500 WITHOUT compensation. Prod evidence: ``transactions`` has the
``subscription_payment`` row, ``subscriptions`` has nothing — the user paid and
got nothing.

Fix shape
---------
The charge→persist section is wrapped in a guard inside ``purchase_tariff``:

- ``except HTTPException: raise`` — the 409 "already active" branch refunds
  itself; re-raising prevents a double refund;
- ``except Exception`` — log, ``db.rollback()``, refund via ``_refund_charge``,
  re-raise as HTTP 500.

``_refund_charge`` re-fetches the user with ``get_user_by_id`` because after
``db.rollback()`` ORM instances are expired and synchronous attribute access in
an async context dies with MissingGreenlet — a refund path must never depend on
live instances.

Post-persist steps (trial bonus seconds, daily-charge marker, panel sync) stay
OUTSIDE the guard on purpose: once the subscription is committed the product is
delivered, and a later failure must not hand the money back on top of it.

A full integration test would need a real DB + FastAPI deps; these tests pin
the SOURCE-LEVEL structure via AST so the guard can't silently disappear.
"""

from __future__ import annotations

import ast
from pathlib import Path


PURCHASE_PATH = (
    Path(__file__).resolve().parents[2] / 'app' / 'cabinet' / 'routes' / 'subscription_modules' / 'purchase.py'
)
SUBSCRIPTION_CRUD_PATH = Path(__file__).resolve().parents[2] / 'app' / 'database' / 'crud' / 'subscription.py'


def _find_async_function(tree: ast.AST, name: str) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f'async function {name!r} not found in cabinet purchase.py')


def _call_names(node: ast.AST) -> set[str]:
    """Collect the names of all calls (plain and attribute) under ``node``."""
    names: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            func = n.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _handler_type_names(handler: ast.ExceptHandler) -> set[str]:
    if handler.type is None:
        return set()
    if isinstance(handler.type, ast.Name):
        return {handler.type.id}
    if isinstance(handler.type, ast.Tuple):
        return {elt.id for elt in handler.type.elts if isinstance(elt, ast.Name)}
    return set()


def _find_refund_guard(func: ast.AsyncFunctionDef) -> ast.Try:
    """The Try whose ``except Exception`` handler performs the compensation.

    Distinguished from the route-level Try (whose Exception handler only logs
    and re-raises 500) by the ``_refund_charge`` call inside the handler.
    """
    for node in ast.walk(func):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            if 'Exception' in _handler_type_names(handler) and '_refund_charge' in _call_names(handler):
                return node
    raise AssertionError(
        'purchase_tariff must wrap the charge→persist section in a guard whose '
        'except Exception handler calls _refund_charge — without it any failure '
        'between the committed balance charge and the subscription commit loses '
        "the user's money (#3031)"
    )


def test_persistence_wrapped_in_refund_guard() -> None:
    """REGRESSION: both persistence branches (extend + create) must sit inside
    the refund guard, and the guard must re-raise HTTPException untouched so the
    self-refunding 409 branch is not compensated twice.
    """
    tree = ast.parse(PURCHASE_PATH.read_text(encoding='utf-8'))
    func = _find_async_function(tree, 'purchase_tariff')
    guard = _find_refund_guard(func)

    body_calls = _call_names(ast.Module(body=guard.body, type_ignores=[]))
    assert 'extend_subscription' in body_calls, (
        'extend_subscription must run inside the refund guard: an IntegrityError '
        'from a tariff-switch race there is NOT caught by the create-branch '
        'handler and previously lost the charge'
    )
    assert 'create_paid_subscription' in body_calls, (
        'create_paid_subscription must run inside the refund guard: non-'
        'IntegrityError failures (FlushError, driver errors) previously lost the charge'
    )

    http_handlers = [h for h in guard.handlers if 'HTTPException' in _handler_type_names(h)]
    assert http_handlers, 'the refund guard must have an "except HTTPException: raise" handler'
    assert all(
        len(h.body) == 1 and isinstance(h.body[0], ast.Raise) and h.body[0].exc is None for h in http_handlers
    ), (
        'the HTTPException handler must be a bare re-raise: the 409 "already '
        'active" branch refunds itself, compensating again would double-refund'
    )

    exception_handler = next(h for h in guard.handlers if 'Exception' in _handler_type_names(h))
    handler_calls = _call_names(exception_handler)
    assert 'rollback' in handler_calls, (
        'the except Exception handler must roll back the broken transaction before refunding'
    )
    raises_http = any(
        isinstance(n, ast.Raise)
        and isinstance(n.exc, ast.Call)
        and isinstance(n.exc.func, ast.Name)
        and n.exc.func.id == 'HTTPException'
        for n in ast.walk(exception_handler)
    )
    assert raises_http, 'after refunding, the guard must surface the failure as an HTTPException (500)'


def test_refund_helper_uses_fresh_user_and_refund_transaction() -> None:
    """REGRESSION: ``_refund_charge`` must re-fetch the user via
    ``get_user_by_id`` (post-rollback ORM instances are expired → MissingGreenlet
    on attribute access) and must credit via ``add_user_balance`` with a REFUND
    transaction so the compensation is visible in the transaction history.
    """
    tree = ast.parse(PURCHASE_PATH.read_text(encoding='utf-8'))
    func = _find_async_function(tree, 'purchase_tariff')
    helper = _find_async_function(func, '_refund_charge')

    helper_calls = _call_names(helper)
    assert 'get_user_by_id' in helper_calls, (
        '_refund_charge must re-fetch the user with get_user_by_id — the closure '
        'User instance is expired after db.rollback() and dies with MissingGreenlet'
    )

    add_balance_calls = [
        n
        for n in ast.walk(helper)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'add_user_balance'
    ]
    assert add_balance_calls, '_refund_charge must credit the user via add_user_balance'
    call = add_balance_calls[0]
    keywords = {kw.arg: kw.value for kw in call.keywords}
    assert 'transaction_type' in keywords, 'refund must set an explicit transaction_type'
    tx_type = keywords['transaction_type']
    assert isinstance(tx_type, ast.Attribute) and tx_type.attr == 'REFUND', (
        'refund must be recorded as TransactionType.REFUND'
    )
    create_tx = keywords.get('create_transaction')
    assert isinstance(create_tx, ast.Constant) and create_tx.value is True, (
        'refund must create a transaction record (create_transaction=True)'
    )


def test_refund_helper_records_failed_refund_when_credit_fails() -> None:
    """REGRESSION: ``add_user_balance`` swallows its own errors and returns False
    instead of raising, so ``_refund_charge`` must inspect the return value and
    persist a ``FAILED_REFUND`` record when the credit did not apply — otherwise a
    failed refund is lost silently, re-introducing the #3031 payment loss.
    """
    tree = ast.parse(PURCHASE_PATH.read_text(encoding='utf-8'))
    func = _find_async_function(tree, 'purchase_tariff')
    helper = _find_async_function(func, '_refund_charge')

    # The add_user_balance result must be captured (not discarded), so its False
    # return can be acted on rather than ignored.
    result_captured = any(
        isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Await)
        and isinstance(n.value.value, ast.Call)
        and isinstance(n.value.value.func, ast.Name)
        and n.value.value.func.id == 'add_user_balance'
        for n in ast.walk(helper)
    )
    assert result_captured, (
        '_refund_charge must capture the add_user_balance return value — it returns '
        'False (not an exception) when the credit fails, so ignoring it loses the refund'
    )
    assert '_persist_failed_refund' in _call_names(helper), (
        '_refund_charge must persist a FAILED_REFUND record when the credit does not '
        'apply, so an unapplied refund is recoverable instead of silently lost'
    )

    module_calls = _call_names(tree)
    assert '_persist_failed_refund' in {
        node.name for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)
    }, 'the module must define _persist_failed_refund'
    assert (
        'FAILED_REFUND' in {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        or 'FAILED_REFUND' in module_calls
    ), 'the failed-refund record must use TransactionType.FAILED_REFUND'


def test_extend_subscription_post_commit_cleanup_is_best_effort() -> None:
    """REGRESSION: ``extend_subscription`` commits the extension, then runs
    ``clear_notifications``. Because ``purchase_tariff`` wraps ``extend_subscription``
    in a compensating refund guard, a raise from that post-commit cleanup would make
    the guard refund an already-delivered extension (free extension). The cleanup must
    therefore be best-effort (wrapped so it cannot propagate) — pinned here because the
    guard lives in a different module and cannot defend against it.
    """
    tree = ast.parse(SUBSCRIPTION_CRUD_PATH.read_text(encoding='utf-8'))
    func = _find_async_function(tree, 'extend_subscription')

    clear_calls = [
        n
        for n in ast.walk(func)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'clear_notifications'
    ]
    assert clear_calls, 'extend_subscription must clear notifications after extending'

    try_nodes = [n for n in ast.walk(func) if isinstance(n, ast.Try)]

    def _within(inner: ast.AST, outer: ast.AST) -> bool:
        return any(child is inner for child in ast.walk(outer))

    for call in clear_calls:
        assert any(_within(call, t) and t.handlers for t in try_nodes), (
            'the post-commit clear_notifications in extend_subscription must be wrapped '
            'in try/except: a failure there must not propagate into a caller-level refund '
            'guard and refund an already-committed extension'
        )


def test_charge_precedes_guard_and_delivery_steps_stay_outside() -> None:
    """REGRESSION: the guard must start AFTER the committed charge (so it covers
    everything that can lose it) and must end BEFORE the daily-charge marker —
    once the subscription is committed the product is delivered, and a failure
    in post-persist bookkeeping must NOT refund a delivered subscription.
    """
    tree = ast.parse(PURCHASE_PATH.read_text(encoding='utf-8'))
    func = _find_async_function(tree, 'purchase_tariff')
    guard = _find_refund_guard(func)

    charge_calls = [
        n
        for n in ast.walk(func)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'subtract_user_balance'
    ]
    assert charge_calls, 'purchase_tariff must charge the balance via subtract_user_balance'
    assert charge_calls[0].lineno < guard.lineno, (
        'the balance charge must happen before the refund guard: the guard exists '
        'to compensate the already-committed charge'
    )

    daily_marker_assigns = [
        n
        for n in ast.walk(func)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == 'last_daily_charge_at' for t in n.targets)
    ]
    assert daily_marker_assigns, 'daily tariffs must still set last_daily_charge_at after purchase'
    guard_end = guard.end_lineno or guard.lineno
    assert all(a.lineno > guard_end for a in daily_marker_assigns), (
        'post-persist bookkeeping (last_daily_charge_at) must stay OUTSIDE the '
        'refund guard: the subscription is already committed at that point, '
        'refunding there would hand back money for a delivered product'
    )


def test_trial_conversion_stays_enabled_in_extend_branch() -> None:
    """REGRESSION: the ``extend_subscription`` call must NOT pass
    ``convert_trial=False``. The purchase flow deliberately EXCLUDES the resolved
    subscription from ``deactivate_user_trial_subscriptions``; that is only safe
    because ``extend_subscription`` (convert_trial=True by default) converts the
    trial to paid in place. Disabling the conversion here would leave a paying
    user flagged as trial (#3031's report scenario).
    """
    tree = ast.parse(PURCHASE_PATH.read_text(encoding='utf-8'))
    func = _find_async_function(tree, 'purchase_tariff')

    extend_calls = [
        n
        for n in ast.walk(func)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'extend_subscription'
    ]
    assert extend_calls, 'purchase_tariff must extend the resolved subscription via extend_subscription'
    for call in extend_calls:
        for kw in call.keywords:
            if kw.arg == 'convert_trial':
                assert isinstance(kw.value, ast.Constant) and kw.value.value is True, (
                    'extend_subscription in purchase_tariff must keep trial '
                    'conversion enabled — the trial is excluded from '
                    'deactivate_user_trial_subscriptions and relies on this call '
                    'to drop the is_trial flag'
                )
