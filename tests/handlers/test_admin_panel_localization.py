"""Read-only admin panel views preserve callbacks and use account language."""

from types import SimpleNamespace

import pytest

from app.handlers.admin import monitoring, remnawave


def test_monitoring_prefers_account_language():
    event = SimpleNamespace(from_user=SimpleNamespace(language_code='ru'))
    assert monitoring._monitoring_language(event, SimpleNamespace(language='fa')) == 'fa'
    assert monitoring._monitoring_language(event) == 'ru'


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_traffic_settings_keep_callback_contract(language):
    keyboard = monitoring._build_traffic_settings_keyboard(language=language)
    russian = monitoring._build_traffic_settings_keyboard(language='ru')
    assert [b.callback_data for row in keyboard.inline_keyboard for b in row] == [
        b.callback_data for row in russian.inline_keyboard for b in row
    ]


def test_remnawave_duration_accepts_language():
    assert remnawave._format_duration(65, language='en') == '1 min 5 s'


@pytest.mark.parametrize('language', ['en', 'zh', 'fa'])
def test_monitoring_and_sync_views_have_localized_copy(language):
    import re
    from datetime import UTC, datetime, time, timedelta

    started = datetime(2026, 1, 1, tzinfo=UTC)
    status = remnawave.RemnaWaveAutoSyncStatus(
        enabled=True,
        times=[time(3)],
        next_run=started + timedelta(days=1),
        last_run_started_at=started,
        last_run_finished_at=started + timedelta(seconds=65),
        last_run_success=True,
        last_run_reason='manual',
        last_run_error=None,
        last_user_stats={'created': 1, 'updated': 2},
        last_server_stats={'created': 1, 'total': 2},
        is_running=False,
    )
    sync_text, sync_keyboard = remnawave._build_auto_sync_view(status, language=language)
    notification_text, notification_keyboard = monitoring._build_notification_settings_view(language)
    traffic_text = monitoring._build_traffic_settings_text(language=language)
    texts = [sync_text, notification_text, traffic_text]
    for keyboard in [
        sync_keyboard,
        notification_keyboard,
        monitoring._build_traffic_settings_keyboard(language=language),
    ]:
        texts.extend(button.text for row in keyboard.inline_keyboard for button in row)
    assert not re.search('[А-Яа-яЁё]', '\n'.join(texts))
    russian_keyboard = remnawave._build_auto_sync_view(status, language='ru')[1]
    assert [b.callback_data for row in sync_keyboard.inline_keyboard for b in row] == [
        b.callback_data for row in russian_keyboard.inline_keyboard for b in row
    ]


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_panel_display_vocabulary_preserves_unknown_values(language):
    from app.localization.texts import get_texts

    texts = get_texts(language)
    assert (
        remnawave._localize_panel_value(texts, 'Provider detail {name} — оператор')
        == 'Provider detail {name} — оператор'
    )
    assert remnawave._localize_panel_value(texts, None) is None
    assert '12' in remnawave._localize_panel_value(texts, 'Найдено 12 пользователей без связи с Remnawave')
    if language != 'ru':
        assert remnawave._localize_panel_value(texts, 'Рекомендуется регулярная синхронизация данных') != (
            'Рекомендуется регулярная синхронизация данных'
        )


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['en', 'ua', 'zh', 'fa'])
async def test_traffic_handler_uses_account_language(monkeypatch, language):
    import inspect
    from unittest.mock import AsyncMock

    seen = []
    original = monitoring._build_traffic_settings_text

    def render(language='ru'):
        seen.append(language)
        return original(language)

    monkeypatch.setattr(monitoring, '_build_traffic_settings_text', render)
    callback = SimpleNamespace(
        from_user=SimpleNamespace(language_code='ru'),
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    await inspect.unwrap(monitoring.admin_traffic_settings)(callback, SimpleNamespace(language=language))
    assert seen == [language]
    callback.message.edit_text.assert_awaited_once()


def test_read_only_localization_does_not_change_log_matching():
    import ast
    from pathlib import Path

    tree = ast.parse(Path(monitoring.__file__).read_text())
    patterns = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and ('Успешно обработан платеж YooKassa' in node.value or 'Чек NaloGO создан для платежа' in node.value)
    ]
    assert len(patterns) == 4
    assert all('([a-f0-9-]+)' in pattern for pattern in patterns)


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_panel_error_wrappers_keep_provider_detail_verbatim(language):
    from app.localization.texts import get_texts

    detail = 'Provider {field}: пользователь [masked-1]'
    result = remnawave._localize_panel_value(get_texts(language), f'Ошибка API: {detail}')
    assert result.endswith(detail)
    assert result.count(detail) == 1
    if language != 'ru':
        assert not result.startswith('Ошибка API: ')
