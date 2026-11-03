"""Admin channel and blacklist views localize copy without treating data as HTML."""

from types import SimpleNamespace

import pytest

from app.handlers.admin import blacklist, required_channels


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_channel_buttons_preserve_operator_title_and_callbacks(language):
    channel = SimpleNamespace(id=7, channel_id='-1001234567890', title='Operator <name>', is_active=True)
    keyboard = required_channels._channels_keyboard([channel], language=language)
    assert keyboard.inline_keyboard[0][0].text == '✅ Operator <name>'
    assert keyboard.inline_keyboard[0][0].callback_data == 'reqch:view:7'


def test_blacklist_service_copy_preserves_unknown_payloads():
    assert blacklist._localize_blacklist_result('Provider {detail}', 'fa') == 'Provider {detail}'


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
async def test_channel_view_escapes_operator_fields_and_uses_account_language(monkeypatch, language):
    import inspect
    import re
    from unittest.mock import AsyncMock, MagicMock

    channel = SimpleNamespace(
        id=7,
        channel_id='-1001234567890',
        title='Operator <name>',
        channel_link='https://t.me/example?x=<tag>',
        is_active=True,
        sort_order=0,
    )
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=object())
    session.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(required_channels, 'AsyncSessionLocal', lambda: session)
    monkeypatch.setattr(required_channels, 'get_channel_by_id', AsyncMock(return_value=channel))
    callback = SimpleNamespace(
        data='reqch:view:7',
        from_user=SimpleNamespace(language_code='ru'),
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    await inspect.unwrap(required_channels.view_channel)(callback, db_user=SimpleNamespace(language=language))
    text = callback.message.edit_text.await_args.args[0]
    assert 'Operator &lt;name&gt;' in text
    assert 'https://t.me/example?x=&lt;tag&gt;' in text
    if language in {'en', 'zh', 'fa'}:
        assert not re.search('[А-Яа-яЁё]', text)


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
async def test_blacklist_view_escapes_url_and_does_not_write_settings(monkeypatch, language):
    import inspect
    import re
    from unittest.mock import AsyncMock

    monkeypatch.setattr(blacklist.blacklist_service, 'is_blacklist_check_enabled', lambda: True)
    monkeypatch.setattr(
        blacklist.blacklist_service, 'get_blacklist_github_url', lambda: 'https://example.invalid/<list>'
    )
    monkeypatch.setattr(blacklist.blacklist_service, 'get_all_blacklisted_users', AsyncMock(return_value=[]))
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    await inspect.unwrap(blacklist.show_blacklist_settings)(callback, SimpleNamespace(language=language), None)
    text = callback.message.edit_text.await_args.args[0]
    assert 'https://example.invalid/&lt;list&gt;' in text
    if language in {'en', 'zh', 'fa'}:
        assert not re.search('[А-Яа-яЁё]', text)


@pytest.mark.parametrize('language', ['en', 'ua', 'zh', 'fa'])
def test_blacklist_known_result_is_localized(language):
    message = 'Черный список обновлен успешно. Записей: 12'
    result = blacklist._localize_blacklist_result(message, language)
    assert result != message
    assert '12' in result
