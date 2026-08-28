"""Фоновая обработка платёжных вебхуков не должна теряться при остановке.

Вебхук EtoPlatezhi отвечает 200 сразу после проверки подписи и дорабатывает
платёж в фоне. После 200 провайдер считает коллбек доставленным и повторять
его не будет, поэтому незавершённая фоновая задача на момент выката — это
потерянное зачисление: деньги у провайдера прошли, у нас нет.
"""

from __future__ import annotations

import asyncio

import pytest

from app.webserver import payments


@pytest.fixture(autouse=True)
def _clean_task_set():
    payments._webhook_bg_tasks.clear()
    yield
    payments._webhook_bg_tasks.clear()


@pytest.mark.asyncio
async def test_spawned_task_is_held_until_it_finishes():
    """Ссылка на задачу живёт, пока та работает, и снимается после."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def work():
        started.set()
        await release.wait()

    payments._spawn_webhook_bg(work())
    await started.wait()

    # без сильной ссылки сборщик мог бы убить задачу на полпути
    assert len(payments._webhook_bg_tasks) == 1

    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not payments._webhook_bg_tasks


@pytest.mark.asyncio
async def test_drain_waits_for_unfinished_processing():
    """Дренаж не отпускает остановку, пока платёж дорабатывается."""
    release = asyncio.Event()
    finished = False

    async def work():
        nonlocal finished
        await release.wait()
        finished = True

    payments._spawn_webhook_bg(work())
    await asyncio.sleep(0)

    drain = asyncio.create_task(payments.drain_webhook_bg_tasks(timeout=5))
    await asyncio.sleep(0)
    assert not drain.done(), 'дренаж завершился, не дождавшись обработки'

    release.set()
    # Дренаж ждём с внешним таймаутом, а не голым `await drain`: иначе тест
    # полагается на таймаут той самой функции, которую и проверяет, и при её
    # поломке зависает вместо того, чтобы упасть. Соседний тест ниже уже
    # ограничивает дренаж так же.
    await asyncio.wait_for(drain, timeout=1)
    assert finished


@pytest.mark.asyncio
async def test_drain_returns_immediately_without_tasks():
    """Пустой набор — выкат не задерживается."""
    await asyncio.wait_for(payments.drain_webhook_bg_tasks(timeout=5), timeout=1)


@pytest.mark.asyncio
async def test_drain_gives_up_by_timeout_and_shouts(monkeypatch):
    """Застрявшая задача не держит выкат вечно, но и не уходит молча."""
    errors: list[tuple] = []
    monkeypatch.setattr(payments.logger, 'error', lambda msg, **kw: errors.append((msg, kw)))

    async def stuck():
        await asyncio.Event().wait()

    payments._spawn_webhook_bg(stuck())
    await asyncio.sleep(0)

    await asyncio.wait_for(payments.drain_webhook_bg_tasks(timeout=0.05), timeout=2)

    assert errors, 'застрявшая обработка ушла молча — платёж не найти'
    assert errors[0][1]['count'] == 1

    for task in list(payments._webhook_bg_tasks):
        task.cancel()


@pytest.mark.asyncio
async def test_drain_is_registered_before_the_other_shutdowns(monkeypatch):
    """Дренаж обязан идти раньше остановки telegram-процессора и БД.

    Фоновая обработка шлёт пользователю уведомление о зачислении — если
    процессор уже остановлен, платёж применится молча.
    """
    import inspect

    from app.webserver import unified_app

    source = inspect.getsource(unified_app.create_unified_app)
    drain_at = source.index('shutdown_handlers.append(payments.drain_webhook_bg_tasks)')
    telegram_at = source.index('shutdown_handlers.append(telegram_processor.stop)')

    assert drain_at < telegram_at


@pytest.mark.asyncio
async def test_webhook_acks_before_processing_finishes(monkeypatch):
    """Ответ 200 уходит НЕ дожидаясь обработки платежа.

    Ради этого весь фикс и делался: под пачкой коллбеков синхронная обработка
    упиралась в семафор, клиент EtoPlatezhi отваливался по таймауту, а
    платформа считала доставку неудачной и слала всё заново.
    """
    from types import SimpleNamespace

    from app.config import settings

    monkeypatch.setattr(type(settings), 'is_etoplatezhi_configured', lambda self: True)

    router = payments.create_payment_router(SimpleNamespace(), SimpleNamespace())
    route = next(r for r in router.routes if r.path == settings.ETOPLATEZHI_WEBHOOK_PATH and 'POST' in r.methods)

    monkeypatch.setattr(
        'app.services.etoplatezhi_service.etoplatezhi_service.verify_callback_signature',
        lambda payload: True,
    )

    release = asyncio.Event()
    processed = False

    async def slow_callback(service, payload, method_name):
        nonlocal processed
        await release.wait()
        processed = True
        return True

    monkeypatch.setattr(payments, '_process_payment_service_callback', slow_callback)

    class _Req:
        async def body(self):
            return b'{"payment": {"id": "p-1"}}'

    response = await asyncio.wait_for(route.endpoint(_Req()), timeout=2)

    assert response.status_code == 200
    assert not processed, 'ответ дождался обработки — смысл фикса потерян'

    release.set()
    await payments.drain_webhook_bg_tasks(timeout=5)
    assert processed
