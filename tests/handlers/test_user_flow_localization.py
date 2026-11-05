"""User-visible validation messages must follow the selected language."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import types

from app.handlers.promocode import handle_promo_subscription_select
from app.handlers.start import process_webauth_confirm
from app.localization.texts import get_texts


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.parametrize('data', ['broken', 'promo_sub:not-a-number:CODE'])
async def test_promo_validation_uses_selected_language(language, data):
    callback = SimpleNamespace(data=data, answer=AsyncMock())
    await handle_promo_subscription_select(callback, SimpleNamespace(language=language), AsyncMock(), AsyncMock())
    callback.answer.assert_awaited_once_with(get_texts(language).t('PROMOCODE_INVALID_SELECTION'), show_alert=True)


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
async def test_web_auth_cancellation_uses_account_language(language):
    message = MagicMock(spec=types.Message)
    message.edit_text = AsyncMock()
    callback = SimpleNamespace(
        data='webauth_deny',
        message=message,
        answer=AsyncMock(),
        from_user=SimpleNamespace(language_code='en'),
    )
    db = AsyncMock()
    await process_webauth_confirm(callback, db, db_user=SimpleNamespace(language=language))
    message.edit_text.assert_awaited_once_with(get_texts(language).t('WEB_AUTH_CANCELLED'))
    db.execute.assert_not_awaited()


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
async def test_invalid_poll_uses_selected_language(language):
    from app.handlers.polls import handle_poll_start

    callback = SimpleNamespace(data='broken', answer=AsyncMock())
    db = AsyncMock()
    await handle_poll_start(callback, SimpleNamespace(language=language), db)
    callback.answer.assert_awaited_once_with(get_texts(language).t('POLL_NOT_FOUND'), show_alert=True)
    db.execute.assert_not_awaited()


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
async def test_delete_notification_uses_selected_language(language):
    from app.handlers.common import handle_delete_ban_notification

    callback = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(delete=AsyncMock()))
    await handle_delete_ban_notification(callback, SimpleNamespace(language=language))
    callback.answer.assert_awaited_once_with(get_texts(language).t('NOTIFICATION_DELETED'))


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
async def test_invalid_gift_uses_selected_language(language):
    from app.handlers.gift_activation import handle_gift_activate

    callback = SimpleNamespace(data='gift_activate:bad', answer=AsyncMock(), message=MagicMock(spec=types.Message))
    await handle_gift_activate(callback, SimpleNamespace(language=language))
    callback.answer.assert_awaited_once_with(get_texts(language).t('GIFT_ACTIVATION_NOT_FOUND'), show_alert=True)


def test_reviewed_user_handlers_do_not_send_direct_russian_literals():
    """Guard the reviewed handlers; this is not a proof for variable-built messages."""
    import ast
    import re
    from pathlib import Path

    handlers = Path(__file__).resolve().parents[2] / 'app' / 'handlers'
    failures = []
    for name in ['start', 'promocode', 'menu', 'common', 'contests', 'polls', 'webhooks', 'gift_activation']:
        tree = ast.parse((handlers / f'{name}.py').read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            method = node.func.attr if isinstance(node.func, ast.Attribute) else ''
            if method not in {'answer', 'edit_text', 'reply', 'InlineKeyboardButton', 'KeyboardButton'}:
                continue
            arguments = list(node.args[:1]) + [kw.value for kw in node.keywords if kw.arg == 'text']
            for argument in arguments:
                values = argument.values if isinstance(argument, ast.JoinedStr) else [argument]
                for value in values:
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        if re.search('[А-Яа-яЁё]', value.value):
                            failures.append(f'{name}:{node.lineno}')
    assert failures == []
