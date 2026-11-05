"""Fresh source selection in the cabinet must not weaken server validation."""

import hashlib
import hmac
import json
from datetime import UTC, datetime
from urllib.parse import urlencode

import pytest

from app.cabinet.auth import telegram_auth


NOW = datetime(2026, 10, 25, 9, tzinfo=UTC)
TOKEN = 'masked-test-bot-token'
USER = {'id': 42, 'first_name': 'Test'}


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


def _signed(age: int) -> str:
    payload = {'auth_date': str(int(NOW.timestamp()) - age), 'user': json.dumps(USER)}
    secret = hmac.new(b'WebAppData', TOKEN.encode(), hashlib.sha256).digest()
    data = '\n'.join(f'{key}={value}' for key, value in sorted(payload.items()))
    payload['hash'] = hmac.new(secret, data.encode(), hashlib.sha256).hexdigest()
    return urlencode(payload)


@pytest.mark.parametrize('max_age', [86400, 86400 * 30])
@pytest.mark.parametrize('offset,accepted', [(-1, True), (0, True), (1, False)])
def test_signed_init_data_age_boundary(monkeypatch, max_age, offset, accepted):
    monkeypatch.setattr(telegram_auth, 'datetime', _Clock)
    monkeypatch.setattr(telegram_auth.settings, 'BOT_TOKEN', TOKEN)
    result = telegram_auth.validate_telegram_init_data(_signed(max_age + offset), max_age_seconds=max_age)
    assert result == (USER if accepted else None)


@pytest.mark.parametrize('age', [0, -301, 86400 * 31])
def test_forged_or_expired_data_never_authenticates(monkeypatch, age):
    monkeypatch.setattr(telegram_auth, 'datetime', _Clock)
    monkeypatch.setattr(telegram_auth.settings, 'BOT_TOKEN', TOKEN)
    signed = _signed(age)
    if age == 0:
        signed = signed.replace('hash=', 'hash=0')
    assert telegram_auth.validate_telegram_init_data(signed, max_age_seconds=86400 * 30) is None
