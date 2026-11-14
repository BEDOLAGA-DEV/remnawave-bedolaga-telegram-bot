"""Execute isolated startup blocks without launching payments, DB or Telegram."""

import ast
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteWebhook
from aiogram.utils.backoff import BackoffConfig

from app.config import settings
from app.utils.telegram_polling import start_polling_with_retry, telegram_startup_request_or_stop


def _main_body():
    tree = ast.parse(Path('main.py').read_text())
    main = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'main')
    return next(node for node in main.body if isinstance(node, ast.Try)).body


def _execute_block(body, namespace):
    function = ast.AsyncFunctionDef(
        name='run',
        args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=body,
        decorator_list=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    exec(compile(module, 'main.py', 'exec'), namespace)  # noqa: S102 - trusted local startup code only
    return namespace['run']()


def _mode_block():
    body = _main_body()
    start = next(
        i
        for i, node in enumerate(body)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == 'bot_run_mode' for t in node.targets)
    )
    end = next(i for i in range(start + 1, len(body)) if isinstance(body[i], ast.AsyncWith))
    return body[start:end]


def _polling_block():
    return next(
        node
        for node in _main_body()
        if isinstance(node, ast.AsyncWith)
        and isinstance(node.items[0].context_expr, ast.Call)
        and node.items[0].context_expr.args
        and isinstance(node.items[0].context_expr.args[0], ast.Constant)
        and node.items[0].context_expr.args[0].value == 'Запуск polling'
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('url', ['', '   '])
async def test_webhook_without_url_rejects_startup(monkeypatch, url):
    monkeypatch.setattr(settings, 'BOT_RUN_MODE', 'webhook')
    monkeypatch.setattr(settings, 'WEBHOOK_URL', url)
    with pytest.raises(ValueError, match='WEBHOOK_URL'):
        await _execute_block(_mode_block(), {'settings': settings})


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,url', [('polling', ''), ('webhook', 'https://bot.example')])
async def test_valid_transport_configuration_is_accepted(monkeypatch, mode, url):
    monkeypatch.setattr(settings, 'BOT_RUN_MODE', mode)
    monkeypatch.setattr(settings, 'WEBHOOK_URL', url)
    await _execute_block(_mode_block(), {'settings': settings})


async def _run_polling(enabled=True, delete_error=None, stop=False):
    updates = ['pending-start']
    state = {'webhook': True, 'received': []}
    errors = [delete_error] if delete_error else []

    async def delete_webhook(*, drop_pending_updates):
        if errors:
            raise errors.pop()
        state['webhook'] = False
        if drop_pending_updates:
            updates.clear()

    async def start_polling(bot, **kwargs):
        if state['webhook']:
            raise RuntimeError('409: webhook is active')
        state['received'].extend(updates)

    @asynccontextmanager
    async def stage(*args, **kwargs):
        yield Mock()

    bot = SimpleNamespace(delete_webhook=AsyncMock(side_effect=delete_webhook), me=AsyncMock())
    dp = SimpleNamespace(start_polling=AsyncMock(side_effect=start_polling))
    tasks = []

    def create_task(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    try:
        await _execute_block(
            [_polling_block()],
            {
                'polling_enabled': enabled,
                'bot': bot,
                'dp': dp,
                'start_polling_with_retry': start_polling_with_retry,
                'telegram_startup_request_or_stop': _fast_request,
                'killer': SimpleNamespace(exit=stop),
                'timeline': SimpleNamespace(stage=stage),
                'asyncio': SimpleNamespace(create_task=create_task),
            },
        )
        if tasks:
            await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
    return state, bot, dp


@pytest.mark.asyncio
async def test_switch_to_polling_receives_pending_updates_from_old_webhook():
    state, _, _ = await _run_polling()
    assert state['received'] == ['pending-start']


@pytest.mark.asyncio
async def test_polling_leaves_signals_to_the_main_loop():
    _, bot, dp = await _run_polling()
    dp.start_polling.assert_awaited_once_with(bot, handle_signals=False, skip_updates=False)


@pytest.mark.asyncio
async def test_webhook_mode_does_not_delete_webhook_or_start_polling():
    state, bot, dp = await _run_polling(enabled=False)
    assert state['webhook'] is True
    bot.delete_webhook.assert_not_awaited()
    dp.start_polling.assert_not_awaited()


@pytest.mark.asyncio
async def test_webhook_deletion_fatal_failure_aborts_startup():
    with pytest.raises(TelegramBadRequest):
        await _run_polling(delete_error=TelegramBadRequest(method=DeleteWebhook(), message='Bad request'))


async def _fast_request(request, should_stop, *, operation):
    return await telegram_startup_request_or_stop(
        request,
        should_stop,
        operation=operation,
        backoff_config=BackoffConfig(min_delay=0.001, max_delay=0.002, factor=1.5, jitter=0),
        poll_interval=0.001,
    )


@pytest.mark.asyncio
async def test_webhook_deletion_network_failure_retries_before_polling():
    state, bot, dp = await _run_polling(delete_error=OSError('Telegram unavailable'))
    assert bot.delete_webhook.await_count == 2
    assert all(call.kwargs == {'drop_pending_updates': False} for call in bot.delete_webhook.await_args_list)
    assert state['received'] == ['pending-start']
    dp.start_polling.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_before_webhook_deletion_does_not_start_polling():
    _, bot, dp = await _run_polling(stop=True)
    bot.delete_webhook.assert_not_awaited()
    dp.start_polling.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('stop', [False, True])
async def test_webhook_setup_retries_with_same_parameters_and_obeys_stop(stop):
    block = next(
        node
        for node in _main_body()
        if isinstance(node, ast.AsyncWith)
        and isinstance(node.items[0].context_expr, ast.Call)
        and node.items[0].context_expr.args
        and isinstance(node.items[0].context_expr.args[0], ast.Constant)
        and node.items[0].context_expr.args[0].value == 'Telegram webhook'
    )

    @asynccontextmanager
    async def stage(*args, **kwargs):
        yield Mock()

    bot = SimpleNamespace(set_webhook=AsyncMock(side_effect=[OSError('reset'), True]))
    await _execute_block(
        [block],
        {
            'telegram_webhook_enabled': True,
            'bot': bot,
            'dp': SimpleNamespace(resolve_used_update_types=lambda: ['message', 'callback_query']),
            'settings': SimpleNamespace(
                get_telegram_webhook_url=lambda: 'https://bot.example/webhook',
                WEBHOOK_SECRET_TOKEN='test-secret',
                WEBHOOK_IP='203.0.113.1',
            ),
            'timeline': SimpleNamespace(stage=stage),
            'telegram_startup_request_or_stop': _fast_request,
            'killer': SimpleNamespace(exit=stop),
        },
    )
    if stop:
        bot.set_webhook.assert_not_awaited()
    else:
        assert bot.set_webhook.await_count == 2
        assert all(
            call.kwargs
            == {
                'url': 'https://bot.example/webhook',
                'secret_token': 'test-secret',
                'drop_pending_updates': False,
                'allowed_updates': ['message', 'callback_query'],
                'ip_address': '203.0.113.1',
            }
            for call in bot.set_webhook.await_args_list
        )
