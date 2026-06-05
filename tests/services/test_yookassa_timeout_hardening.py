"""Regression tests for YooKassa SDK timeout hardening.

Incident (2026-05-18): bot froze entirely during YK API degradation —
``query is too old`` from aiogram, ``ConnectionTimeoutError`` to
RemnaWave. Root cause: ``yookassa.client.ApiClient.execute`` calls
``requests.Session.request(...)`` WITHOUT a timeout, so threads block
on ``socket.recv()`` indefinitely. The default ``ThreadPoolExecutor``
fills up with stuck threads, every subsequent ``run_in_executor`` queues
behind them, and the event loop starves because it can never get a
thread back to handle even trivial DNS / TCP-connect work.

Fix in ``app/services/yookassa_service.py``:
  1. Monkey-patch ``ApiClient.execute`` to pass
     ``timeout=(connect, read)`` to ``session.request``. Idempotent.
  2. Dedicated ``ThreadPoolExecutor(max_workers=4,
     thread_name_prefix='yookassa-sdk')`` so YK slowness can never
     exhaust the default pool.

Defence-in-depth fix in ``app/services/payment/yookassa.py``:
  3. ``asyncio.wait_for(get_payment_info(...), timeout=8)`` around the
     webhook cross-check call, with payload-fallback on timeout.

These tests pin the contract:
  * Patch is applied at module-import time.
  * Patch is idempotent (re-import / hot-reload does not double-wrap).
  * The patched ``execute`` actually passes a ``timeout`` to
    ``session.request`` (negative-control against regression to the
    unpatched upstream behaviour).
  * Dedicated executor exists with a bounded ``max_workers``.
  * All 4 ``run_in_executor`` call sites in ``yookassa_service.py``
    use the dedicated executor, not the default ``None``.
  * Webhook handler uses ``asyncio.wait_for`` with a tight budget.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from app.services import yookassa_service


YOOKASSA_SERVICE_PATH = Path(__file__).resolve().parents[2] / 'app' / 'services' / 'yookassa_service.py'
YOOKASSA_PAYMENT_PATH = Path(__file__).resolve().parents[2] / 'app' / 'services' / 'payment' / 'yookassa.py'


# ---------------------------------------------------------------------------
# Monkey-patch correctness.
# ---------------------------------------------------------------------------


def test_apiclient_patch_helper_exists_and_runs_at_import() -> None:
    """Source-level pin: ``_patch_yookassa_timeout`` must be DEFINED
    and CALLED at the bottom of ``yookassa_service.py`` so it runs on
    first import.

    Tested at source level rather than by inspecting ``ApiClient``
    directly because pytest's rootdir mode inserts
    ``app/services/payment`` into sys.path. That directory contains
    a file named ``yookassa.py``, which then shadows the real
    ``yookassa`` SDK package. The production process doesn't have
    this shadow — but our test setup does, so importing
    ``yookassa.client`` from inside the test process fails with
    ``'yookassa' is not a package``. Source-level verification
    sidesteps the shadow entirely and catches the actual regression
    class we care about: 'someone deleted the patch'.
    """
    source = YOOKASSA_SERVICE_PATH.read_text(encoding='utf-8')

    # Helper must exist.
    assert 'def _patch_yookassa_timeout(' in source, (
        'yookassa_service.py must define _patch_yookassa_timeout — the SDK '
        'ships with no HTTP timeout and threads block on socket.recv forever '
        'without our patch'
    )

    # And must be invoked at import time (module-level call, not
    # behind ``if __name__ == "__main__":`` or similar).
    # We allow it on any indentation == 0 line for resilience.
    invocation_lines = [line for line in source.splitlines() if line.startswith('_patch_yookassa_timeout()')]
    assert invocation_lines, (
        '_patch_yookassa_timeout() must be CALLED at module import — defining '
        'it but never calling it would silently leave the SDK unpatched'
    )


def test_patched_execute_passes_timeout_to_session_request() -> None:
    """Negative-control against upstream regression: the patched
    ``execute`` body must pass ``timeout=`` to ``session.request``.

    Source-level pin (see test_apiclient_patch_helper_exists_and_runs_at_import
    for why we avoid directly importing yookassa.client).
    """
    src = inspect.getsource(yookassa_service._patch_yookassa_timeout)
    assert 'session.request' in src, '_patch_yookassa_timeout must replace session.request behaviour'
    assert 'timeout=' in src, (
        'Patched ApiClient.execute must pass timeout= to session.request. '
        'Without it, requests.Session blocks on socket.recv indefinitely.'
    )
    # The (connect, read) tuple is the requests convention. Reject a
    # single-int hardcoded timeout that would skip operator override.
    assert '(connect_timeout, read_timeout)' in src, (
        'Patched execute must use the (connect_timeout, read_timeout) tuple '
        'so operators can tune both phases independently via env vars'
    )


def test_patch_idempotency_guard_exists() -> None:
    """The patch helper must check ``ApiClient._timeout_patched`` to
    avoid re-wrapping on hot-reload. Without this guard a second
    patch application would wrap the wrapper, doubling the stack
    depth on every YK call and risking recursion errors.
    """
    src = inspect.getsource(yookassa_service._patch_yookassa_timeout)
    assert '_timeout_patched' in src, (
        '_patch_yookassa_timeout must guard against re-wrapping via the '
        '_timeout_patched flag — pin against accidental double-wrap on reload'
    )
    assert "getattr(ApiClient, '_timeout_patched'" in src or 'ApiClient._timeout_patched' in src


def test_patch_respects_settings_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator who sets YOOKASSA_HTTP_CONNECT_TIMEOUT or
    YOOKASSA_HTTP_READ_TIMEOUT via .env should see those values in
    the patched closure. We can't easily inspect closure cells without
    fragile gc tricks, so we verify the patch helper at least READS
    the settings (i.e., doesn't hardcode the numbers).
    """
    src = inspect.getsource(yookassa_service._patch_yookassa_timeout)
    assert 'YOOKASSA_HTTP_CONNECT_TIMEOUT' in src
    assert 'YOOKASSA_HTTP_READ_TIMEOUT' in src


# ---------------------------------------------------------------------------
# Dedicated executor.
# ---------------------------------------------------------------------------


def test_dedicated_executor_exists_with_bounded_max_workers() -> None:
    """The bug-report's "обязательное" fix #2: dedicated executor with
    bounded ``max_workers`` so YK slowness can never starve the default
    pool that aiogram + DB + RemnaWave + everything else also uses.

    Default is 4; tunable via ``YOOKASSA_MAX_CONCURRENT_REQUESTS``.
    Both values are valid — we just pin that it's a small bounded
    positive integer, not the unbounded default pool.
    """
    executor = yookassa_service._yookassa_executor
    assert executor is not None
    # ThreadPoolExecutor stores max_workers as _max_workers — private
    # but stable across Python versions.
    max_workers = getattr(executor, '_max_workers', None)
    assert isinstance(max_workers, int) and max_workers >= 1, (
        'Dedicated YK executor must have a positive max_workers cap'
    )
    assert max_workers <= 32, (
        f'YK executor max_workers={max_workers} is suspiciously large — '
        f'starvation protection diminishes as the pool approaches the default pool size'
    )


def test_max_workers_resolver_respects_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """REGRESSION: ``YOOKASSA_MAX_CONCURRENT_REQUESTS`` env var must flow
    through ``_resolve_max_workers()``. Without this, high-volume
    operators can't tune burst capacity without a code patch.
    """
    from app.config import settings

    monkeypatch.setattr(settings, 'YOOKASSA_MAX_CONCURRENT_REQUESTS', 12, raising=False)
    assert yookassa_service._resolve_max_workers() == 12

    monkeypatch.setattr(settings, 'YOOKASSA_MAX_CONCURRENT_REQUESTS', 1, raising=False)
    assert yookassa_service._resolve_max_workers() == 1


def test_max_workers_resolver_floors_at_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """A misconfigured ``YOOKASSA_MAX_CONCURRENT_REQUESTS=0`` must NOT
    disable YK entirely — fall back to the default cap. Same defensive
    pattern as the timeout values.
    """
    from app.config import settings

    monkeypatch.setattr(settings, 'YOOKASSA_MAX_CONCURRENT_REQUESTS', 0, raising=False)
    assert yookassa_service._resolve_max_workers() == 4

    monkeypatch.setattr(settings, 'YOOKASSA_MAX_CONCURRENT_REQUESTS', -5, raising=False)
    assert yookassa_service._resolve_max_workers() == 1

    monkeypatch.setattr(settings, 'YOOKASSA_MAX_CONCURRENT_REQUESTS', 'garbage', raising=False)
    assert yookassa_service._resolve_max_workers() == 4


def test_dedicated_executor_thread_name_prefix() -> None:
    """Threads in the YK executor must be identifiable in py-spy /
    stack traces. Without a prefix, a hung YK thread looks identical
    to any other ThreadPoolExecutor worker — incident-response harder.
    """
    executor = yookassa_service._yookassa_executor
    # _thread_name_prefix is set via the constructor arg.
    assert getattr(executor, '_thread_name_prefix', '') == 'yookassa-sdk'


def test_all_run_in_executor_callsites_use_dedicated_pool() -> None:
    """Source-level pin: every ``run_in_executor`` in
    ``yookassa_service.py`` must pass ``_yookassa_executor`` as the
    first argument, NEVER ``None`` (which means default pool).
    """
    source = YOOKASSA_SERVICE_PATH.read_text(encoding='utf-8')

    # Find every "loop.run_in_executor(" occurrence and verify the
    # first arg is _yookassa_executor.
    lines = source.splitlines()
    offending: list[tuple[int, str]] = []
    for i, line in enumerate(lines, start=1):
        if 'loop.run_in_executor(' not in line:
            continue
        # The first arg is on the NEXT line in our codebase. Look
        # ahead until we see ',' or a closing paren — robust to either
        # one-liner or multi-line forms.
        for j in range(i, min(i + 3, len(lines))):
            window = lines[j]
            if '_yookassa_executor' in window:
                break
            if 'None,' in window or 'None ,' in window:
                offending.append((j + 1, window.strip()))
                break

    assert not offending, (
        'Every loop.run_in_executor(...) in yookassa_service.py must use '
        f'_yookassa_executor, not None. Offending lines: {offending}'
    )


# ---------------------------------------------------------------------------
# Webhook handler tight timeout + payload fallback.
# ---------------------------------------------------------------------------


def test_webhook_uses_wait_for_with_tight_budget() -> None:
    """``process_yookassa_webhook`` cross-check of payment status must
    be wrapped in ``asyncio.wait_for(timeout<=10)``. The webhook
    payload is already trusted (signature-verified upstream), so the
    API cross-check is defence-in-depth — it cannot be allowed to
    block webhook processing indefinitely.

    Pre-fix: no wait_for, the inner ``asyncio.timeout(30)`` was the
    only bound, and a single slow YK API call could pile up webhooks.
    """
    source = YOOKASSA_PAYMENT_PATH.read_text(encoding='utf-8')

    assert 'asyncio.wait_for(' in source, (
        'process_yookassa_webhook must wrap get_payment_info in asyncio.wait_for '
        'with a tight budget — otherwise YK API slowness queues webhook processing'
    )

    # Find the relevant wait_for and confirm the timeout is ≤ 10s.
    # Walk lines around 'asyncio.wait_for'.
    lines = source.splitlines()
    found_tight_budget = False
    for i, line in enumerate(lines):
        if 'asyncio.wait_for(' not in line:
            continue
        # Look up to 5 lines ahead for the timeout= kwarg.
        for k in range(i, min(i + 6, len(lines))):
            if 'timeout=' in lines[k]:
                # Extract the number after 'timeout='.
                import re

                match = re.search(r'timeout\s*=\s*(\d+)', lines[k])
                if match and int(match.group(1)) <= 10:
                    found_tight_budget = True
                    break
        if found_tight_budget:
            break

    assert found_tight_budget, (
        'asyncio.wait_for around get_payment_info must use timeout ≤ 10s '
        '(8s recommended). Larger budgets allow YK degradation to queue webhooks.'
    )


def test_webhook_handles_timeout_with_payload_fallback() -> None:
    """When the API cross-check times out, the handler must NOT raise.
    It must fall back to the webhook payload (which is already
    signature-verified). The bug report calls this out as "при таймауте
    использовать данные из payload webhook'а".
    """
    source = YOOKASSA_PAYMENT_PATH.read_text(encoding='utf-8')

    # Find the asyncio.wait_for block and confirm its except branch
    # catches TimeoutError without re-raising.
    assert 'TimeoutError' in source or 'asyncio.TimeoutError' in source, (
        'Webhook handler must catch TimeoutError from wait_for and fall back '
        'to the payload, otherwise YK API slowness propagates as 500s'
    )

    # Negative-control: must NOT have ``raise`` inside the wait_for's
    # TimeoutError handler — that would defeat the fallback.
    wait_for_idx = source.find('asyncio.wait_for(')
    assert wait_for_idx >= 0
    snippet = source[wait_for_idx : wait_for_idx + 1500]
    # The TimeoutError block must contain a logger.warning (use of
    # payload) and not contain "raise" before the next exception
    # handler.
    timeout_block_start = snippet.find('TimeoutError')
    assert timeout_block_start >= 0
    timeout_block = snippet[timeout_block_start : timeout_block_start + 500]
    # Splitting on 'except' would catch ALL except handlers; we want
    # only the TimeoutError block until the next 'except'.
    next_except = timeout_block.find('except', 10)
    if next_except > 0:
        timeout_block = timeout_block[:next_except]
    assert 'raise' not in timeout_block, (
        'TimeoutError handler must not re-raise — the payload fallback is the whole point'
    )
