from __future__ import annotations

from datetime import UTC, datetime

from app.services.shkeeper_service import ShkeeperService


def test_verify_callback_auth_uses_exact_match() -> None:
    service = ShkeeperService(callback_api_key='secret')
    assert service.verify_callback_auth('secret') is True
    assert service.verify_callback_auth(' secret ') is True
    assert service.verify_callback_auth('SECRET') is False
    assert service.verify_callback_auth(None) is False


def test_parse_datetime_normalizes_to_utc() -> None:
    parsed = ShkeeperService.parse_datetime('2026-03-01T12:00:00+05:00')
    assert parsed == datetime(2026, 3, 1, 7, 0, 0, tzinfo=UTC)
