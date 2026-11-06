"""Certificate UI translation must not alter files, secrets or upload validation."""

import inspect
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.admin import overpay_certificate as ui
from app.localization.texts import get_texts


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
@pytest.mark.parametrize('uploaded', [False, True])
def test_status_language_preserves_escaping_and_actions(monkeypatch, language, uploaded):
    monkeypatch.setattr(
        ui.cert_service,
        'get_status',
        lambda: dict(
            uploaded=uploaded,
            valid=True,
            subject='CN=<Example & test>',
            not_valid_after='2030-01-01',
            path='/certificates/example.p12',
            env_locked_path=True,
            env_locked_passphrase=False,
        ),
    )
    text, keyboard = ui._status_view(language)
    assert 'ADMIN_OVERPAY_CERT_' not in text
    if uploaded:
        assert 'CN=&lt;Example &amp; test&gt;' in text
        assert '/certificates/example.p12' in text
    if language in {'en', 'zh', 'fa'}:
        assert not re.search('[А-Яа-яЁё]', text)
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert callbacks == [
        'overpay_cert:upload',
        *(['overpay_cert:delete'] if uploaded else []),
        'admin_submenu_settings',
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
async def test_rejected_upload_uses_account_language_without_storing_file(language):
    message = SimpleNamespace(
        document=SimpleNamespace(file_name='example.txt', file_size=5),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(update_data=AsyncMock(), set_state=AsyncMock())
    await inspect.unwrap(ui.process_certificate_file)(message, state, db_user=SimpleNamespace(language=language))
    assert message.answer.await_args.args[0] == get_texts(language).t('ADMIN_OVERPAY_CERT_014')
    state.update_data.assert_not_awaited()
    state.set_state.assert_not_awaited()


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_only_known_validation_errors_are_translated(language):
    texts = get_texts(language)
    assert ui._certificate_error_text('Файл сертификата больше 1 МБ', texts) == texts.t('ADMIN_OVERPAY_CERT_ERROR_SIZE')
    assert ui._certificate_error_text('External diagnostic: example', texts) == 'External diagnostic: example'
