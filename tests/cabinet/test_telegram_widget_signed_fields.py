"""Подпись Telegram Login Widget проверяется только по полям, которые подписал Telegram.

На экране согласия кабинет повторяет вход тем же payload виджета, добавляя
accepted_legal_documents. Поле попадало в data-check-string, хэш не сходился —
новый пользователь видел «Invalid or expired Telegram authentication data»
и не мог зарегистрироваться через Telegram.
"""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from app.cabinet.auth.telegram_auth import validate_telegram_login_widget
from app.cabinet.routes.auth import _telegram_widget_signed_data
from app.cabinet.schemas.auth import TelegramWidgetAuthRequest
from app.config import settings


BOT_TOKEN = '123456:test-token'


def _signed_widget_payload() -> dict:
    data = {'id': 42, 'first_name': 'Тест', 'username': 'tester', 'auth_date': int(time.time())}
    check = '\n'.join(f'{k}={v}' for k, v in sorted(data.items()))
    secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
    data['hash'] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return data


@pytest.fixture(autouse=True)
def _bot_token(monkeypatch):
    monkeypatch.setattr(settings, 'BOT_TOKEN', BOT_TOKEN)


@pytest.mark.parametrize(
    'extra',
    [
        {},
        {'accepted_legal_documents': ['public_offer', 'privacy_policy']},
        {'campaign_slug': 'promo', 'referral_code': 'ref1', 'accepted_legal_documents': ['public_offer']},
    ],
)
def test_widget_signature_survives_cabinet_only_fields(extra):
    request = TelegramWidgetAuthRequest(**_signed_widget_payload(), **extra)

    assert validate_telegram_login_widget(_telegram_widget_signed_data(request))


def test_tampered_signed_field_still_rejected():
    request = TelegramWidgetAuthRequest(**{**_signed_widget_payload(), 'username': 'someone_else'})

    assert not validate_telegram_login_widget(_telegram_widget_signed_data(request))


def test_only_telegram_fields_are_passed():
    request = TelegramWidgetAuthRequest(**_signed_widget_payload(), accepted_legal_documents=['public_offer'])

    assert set(_telegram_widget_signed_data(request)) <= {
        'id',
        'first_name',
        'last_name',
        'username',
        'photo_url',
        'auth_date',
        'hash',
    }
