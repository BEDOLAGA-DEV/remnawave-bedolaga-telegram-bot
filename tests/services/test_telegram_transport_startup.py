"""Execute isolated startup blocks without launching payments, DB or Telegram."""

import ast
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.config import settings


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


async def _run_polling(enabled=True, delete_error=None):
    updates = ['pending-start']
    state = {'webhook': True, 'received': []}

    async def delete_webhook(*, drop_pending_updates):
        if delete_error:
            raise delete_error
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

    bot = SimpleNamespace(delete_webhook=AsyncMock(side_effect=delete_webhook))
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
async def test_webhook_mode_does_not_delete_webhook_or_start_polling():
    state, bot, dp = await _run_polling(enabled=False)
    assert state['webhook'] is True
    bot.delete_webhook.assert_not_awaited()
    dp.start_polling.assert_not_awaited()


@pytest.mark.asyncio
async def test_webhook_deletion_failure_aborts_startup():
    with pytest.raises(OSError, match='Telegram unavailable'):
        await _run_polling(delete_error=OSError('Telegram unavailable'))
