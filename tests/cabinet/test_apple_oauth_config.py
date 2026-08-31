from typing import Any

import pytest

from app.config import Settings


def _settings(**overrides: Any) -> Settings:
    return Settings(BOT_TOKEN='test-token', _env_file=None, **overrides)


def test_apple_ios_client_ids_keep_default_and_add_replacement() -> None:
    settings = _settings(
        OAUTH_APPLE_IOS_CLIENT_ID=[
            'com.example.legacy',
            'com.example.replacement',
        ],
    )

    assert settings.get_oauth_apple_ios_client_ids() == [
        'com.example.legacy',
        'com.example.replacement',
    ]


def test_apple_ios_client_ids_trim_deduplicate_and_drop_empty_values() -> None:
    settings = _settings(
        OAUTH_APPLE_IOS_CLIENT_ID=[
            ' com.example.legacy ',
            'com.example.replacement',
            '',
            'com.example.legacy',
            ' com.example.replacement ',
        ],
    )

    assert settings.get_oauth_apple_ios_client_ids() == [
        'com.example.legacy',
        'com.example.replacement',
    ]


def test_apple_ios_client_ids_parse_json_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        'OAUTH_APPLE_IOS_CLIENT_ID',
        '["com.example.legacy","com.example.replacement"]',
    )

    settings = _settings()

    assert settings.get_oauth_apple_ios_client_ids() == [
        'com.example.legacy',
        'com.example.replacement',
    ]


def test_apple_ios_client_ids_treat_blank_environment_as_empty_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('OAUTH_APPLE_IOS_CLIENT_ID', '')

    settings = _settings()

    assert settings.get_oauth_apple_ios_client_ids() == []
