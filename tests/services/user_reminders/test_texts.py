from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.user_reminders.texts import (
    pick_text,
    render_bot_message,
    render_card,
    validate_button,
    validate_texts,
)


TEXTS = {
    'ru': {'title': 'Заголовок <b>', 'body': 'Текст\nвторая строка', 'button': 'Открыть'},
    'en': {'title': 'Title', 'body': 'Body'},
}


def _reminder(**kw):
    base = dict(id=7, texts=TEXTS, button_kind='cabinet', button_target='/profile/accounts')
    base.update(kw)
    return SimpleNamespace(**base)


def test_language_falls_back_to_ru_and_button_to_ru_button():
    assert pick_text(TEXTS, 'en') == {'title': 'Title', 'body': 'Body', 'button': 'Открыть'}
    assert pick_text(TEXTS, 'zh')['title'] == 'Заголовок <b>'
    assert pick_text(TEXTS, 'en-US')['title'] == 'Title'
    assert pick_text(TEXTS, None)['title'] == 'Заголовок <b>'


def test_bot_message_is_escaped_with_bold_title(monkeypatch):
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'https://cab.example')
    text, markup = render_bot_message(_reminder(), 'ru')
    assert text == '<b>Заголовок &lt;b&gt;</b>\n\nТекст\nвторая строка'
    button = markup.inline_keyboard[0][0]
    assert button.text == 'Открыть'
    assert button.web_app.url == 'https://cab.example/profile/accounts'


def test_cabinet_button_is_dropped_without_cabinet_url(monkeypatch):
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', '')
    _, markup = render_bot_message(_reminder(), 'ru')
    assert markup is None


def test_url_button_and_no_button():
    _, markup = render_bot_message(_reminder(button_kind='url', button_target='https://x.example/a'), 'ru')
    assert markup.inline_keyboard[0][0].url == 'https://x.example/a'
    _, markup = render_bot_message(_reminder(button_kind='none', button_target=None), 'ru')
    assert markup is None


def test_card_keeps_plain_text():
    card = render_card(_reminder(), 'en')
    assert card == {
        'id': 7,
        'title': 'Title',
        'body': 'Body',
        'button': {'kind': 'cabinet', 'target': '/profile/accounts', 'text': 'Открыть'},
    }


@pytest.mark.parametrize(
    'raw',
    [
        {},
        {'en': {'title': 't', 'body': 'b'}},
        {'ru': {'title': '', 'body': 'b'}},
        {'ru': {'title': 't' * 81, 'body': 'b'}},
        {'ru': {'title': 't', 'body': 'b' * 1001}},
        {'ru': {'title': 't', 'body': 'b', 'button': 'x' * 41}},
        {'ru': {'title': 't', 'body': 'b'}, 'de': {'title': 't', 'body': 'b'}},
    ],
)
def test_invalid_texts(raw):
    with pytest.raises(ValueError):
        validate_texts(raw)


@pytest.mark.parametrize(
    ('kind', 'target', 'ok'),
    [
        ('none', None, True),
        ('none', '/x', False),
        ('cabinet', '/profile/accounts', True),
        ('cabinet', 'profile', False),
        ('cabinet', '//evil.example', False),
        ('cabinet', '/a b', False),
        ('url', 'https://x.example', True),
        ('url', 'http://x.example', False),
        ('url', 'javascript:alert(1)', False),
        ('url', None, False),
    ],
)
def test_button_validation(kind, target, ok):
    texts = {'ru': {'title': 't', 'body': 'b', 'button': 'Go'}}
    if ok:
        validate_button(kind, target, texts)
    else:
        with pytest.raises(ValueError):
            validate_button(kind, target, texts)


def test_button_text_required_when_there_is_a_button():
    with pytest.raises(ValueError):
        validate_button('url', 'https://x.example', {'ru': {'title': 't', 'body': 'b'}})


def test_card_drops_button_for_unknown_stored_kind():
    """ReminderCardButton.kind — Literal['cabinet', 'url']: неизвестный сохранённый
    button_kind не должен собирать кнопку и валить /cabinet/reminders/active 500-й.
    """
    card = render_card(_reminder(button_kind='weird', button_target='/x'), 'ru')
    assert card['button'] is None


def test_bot_message_has_no_markup_for_unknown_stored_kind(monkeypatch):
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'https://cab.example')
    _, markup = render_bot_message(_reminder(button_kind='weird', button_target='/x'), 'ru')
    assert markup is None
