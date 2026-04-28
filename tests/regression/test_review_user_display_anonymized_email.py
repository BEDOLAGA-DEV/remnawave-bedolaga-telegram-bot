"""Regression: format_user_public_display must return an anonymized email
for cabinet-only users (no Telegram username, no first_name).

Before fix: such users showed as a generic 'Пользователь' in the review
channel post, with no identifier at all.
"""
from types import SimpleNamespace
from app.utils.user_utils import format_user_public_display, _anonymize_email


def test_telegram_user_uses_username():
    user = SimpleNamespace(
        id=1, username='alice', first_name='Alice', email=None,
    )
    assert format_user_public_display(user) == '@alice'


def test_telegram_user_no_username_uses_first_name():
    user = SimpleNamespace(
        id=1, username=None, first_name='Alice', email=None,
    )
    assert format_user_public_display(user) == 'Alice'


def test_cabinet_only_user_uses_anonymized_email():
    user = SimpleNamespace(
        id=42, username=None, first_name=None, email='mama05693@gmail.com',
    )
    result = format_user_public_display(user)
    assert result == 'ma***@gmail.com', f'expected anonymized email, got {result!r}'


def test_user_with_neither_falls_back_to_id():
    user = SimpleNamespace(
        id=42, username=None, first_name=None, email=None,
    )
    assert format_user_public_display(user) == 'Пользователь #42'


def test_anonymize_email_short_local_part():
    assert _anonymize_email('a@b.co') == 'a***@b.co'


def test_anonymize_email_invalid_returns_empty():
    assert _anonymize_email('') == ''
    assert _anonymize_email('no-at-sign') == ''
    assert _anonymize_email('@nodomain') == ''
