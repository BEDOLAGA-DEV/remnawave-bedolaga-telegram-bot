"""Utilities for validating Telegram WebApp initialization data."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import parse_qsl


class TelegramWebAppAuthError(Exception):
    """Raised when Telegram WebApp init data fails validation."""


def parse_webapp_init_data(
    init_data: str,
    bot_token: str | list[str],
    *,
    max_age_seconds: int = 86400,
) -> dict[str, Any]:
    """Validate and parse Telegram WebApp init data.

    Args:
        init_data: Raw init data string provided by Telegram WebApp.
        bot_token: Bot token used to verify the signature.
        max_age_seconds: Maximum allowed age for the payload. Defaults to 24 hours.

    Returns:
        Parsed init data as a dictionary.

    Raises:
        TelegramWebAppAuthError: If validation fails.
    """

    if not init_data:
        raise TelegramWebAppAuthError('Missing init data')

    tokens: list[str]
    if isinstance(bot_token, str):
        normalized = bot_token.strip()
        tokens = [normalized] if normalized else []
    else:
        tokens = [token.strip() for token in bot_token if token and token.strip()]

    if not tokens:
        raise TelegramWebAppAuthError('Bot token is not configured')

    parsed_pairs = parse_qsl(init_data, strict_parsing=True, keep_blank_values=True)
    data: dict[str, Any] = {key: value for key, value in parsed_pairs}

    received_hash = data.pop('hash', None)
    if not received_hash:
        raise TelegramWebAppAuthError('Missing init data signature')

    data_check_string = '\n'.join(f'{key}={value}' for key, value in sorted(data.items()))

    signature_is_valid = False
    for token in tokens:
        secret_key = hmac.new(
            key=b'WebAppData',
            msg=token.encode('utf-8'),
            digestmod=hashlib.sha256,
        ).digest()

        computed_hash = hmac.new(
            key=secret_key,
            msg=data_check_string.encode('utf-8'),
            digestmod=hashlib.sha256,
        ).hexdigest()

        if hmac.compare_digest(computed_hash, received_hash):
            signature_is_valid = True
            break

    if not signature_is_valid:
        raise TelegramWebAppAuthError('Invalid init data signature')

    auth_date_raw = data.get('auth_date')
    if auth_date_raw is not None:
        try:
            auth_date = int(auth_date_raw)
        except (TypeError, ValueError):
            raise TelegramWebAppAuthError('Invalid auth_date value') from None

        if max_age_seconds and auth_date:
            current_ts = int(time.time())
            if current_ts - auth_date > max_age_seconds:
                raise TelegramWebAppAuthError('Init data is too old')

        data['auth_date'] = auth_date

    user_payload = data.get('user')
    if user_payload is not None:
        try:
            data['user'] = json.loads(user_payload)
        except json.JSONDecodeError as error:
            raise TelegramWebAppAuthError('Invalid user payload') from error

    return data
