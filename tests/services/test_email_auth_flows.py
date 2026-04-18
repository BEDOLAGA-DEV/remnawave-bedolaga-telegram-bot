"""Tests for auth flow email error handling (503/502 guards).

Tests the critical state transitions added by the email backend abstraction:
- 503 when email backend is not configured but verification is required
- 502 + user cleanup when send fails during standalone registration
- Retry after 502 succeeds (user was deleted, email is free)
"""

import os
import sys
import types as _types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# --- Pre-import stubs (must happen before any app imports) ---

# redis.exceptions stub
if 'redis.exceptions' not in sys.modules:
    _redis_exc = _types.ModuleType('redis.exceptions')

    class _NoScriptError(Exception):
        pass

    class _ConnectionError(Exception):
        pass

    _redis_exc.NoScriptError = _NoScriptError
    _redis_exc.ConnectionError = _ConnectionError
    sys.modules['redis.exceptions'] = _redis_exc

    if 'redis' not in sys.modules or not hasattr(sys.modules['redis'], 'exceptions'):
        _redis_mod = sys.modules.get('redis') or _types.ModuleType('redis')
        _redis_mod.exceptions = _redis_exc
        sys.modules['redis'] = _redis_mod

# Backup directory — backup_service uses /app/data/backups by default, override for tests
os.environ.setdefault('BACKUP_LOCATION', str(Path('data/backups').resolve()))
Path('data/backups').mkdir(parents=True, exist_ok=True)

import tests.conftest  # noqa: F401

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings


# ---------------------------------------------------------------------------
# Helpers to build a minimal test app with the auth router
# ---------------------------------------------------------------------------

def _make_test_app(monkeypatch, db_mock=None, current_user=None):
    """Create a FastAPI app with auth router and mocked dependencies."""
    from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
    from app.cabinet.routes.auth import router

    app = FastAPI()
    app.include_router(router, prefix='/cabinet')

    async def override_db():
        yield db_mock or AsyncMock()

    app.dependency_overrides[get_cabinet_db] = override_db

    if current_user is not None:
        app.dependency_overrides[get_current_cabinet_user] = lambda: current_user

    return app


def _mock_db():
    """Create a mock AsyncSession with commit/delete/rollback/execute."""
    db = AsyncMock()
    # Make execute().scalar_one_or_none() return None (no existing user)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    db.execute.return_value = result_mock
    return db


# ---------------------------------------------------------------------------
# Test: standalone registration returns 503 when email backend not configured
# ---------------------------------------------------------------------------

def test_register_standalone_503_when_email_unconfigured(monkeypatch):
    """Verification enabled + email not configured → 503, no user created."""
    db = _mock_db()

    monkeypatch.setattr(settings, 'CABINET_EMAIL_AUTH_ENABLED', True)
    monkeypatch.setattr(settings, 'CABINET_EMAIL_VERIFICATION_ENABLED', True)
    monkeypatch.setattr(settings, 'TEST_EMAIL', '')
    monkeypatch.setattr(settings, 'TEST_EMAIL_PASSWORD', '')

    # Disposable email check
    monkeypatch.setattr(
        'app.cabinet.routes.auth.disposable_email_service',
        SimpleNamespace(is_disposable=lambda email: False),
    )
    # Rate limiter
    monkeypatch.setattr(
        'app.cabinet.routes.auth.RateLimitCache',
        SimpleNamespace(is_ip_rate_limited=AsyncMock(return_value=False)),
    )

    # Email service is NOT configured
    mock_email_svc = SimpleNamespace(
        is_configured=lambda: False,
        send_verification_email=MagicMock(return_value=False),
    )
    monkeypatch.setattr('app.cabinet.routes.auth.email_service', mock_email_svc)

    app = _make_test_app(monkeypatch, db)
    client = TestClient(app)

    resp = client.post('/cabinet/auth/email/register/standalone', json={
        'email': 'new@test.com',
        'password': 'StrongPass123!',
    })

    assert resp.status_code == 503
    assert 'not configured' in resp.json()['detail']
    # User should NOT have been created — db.delete should not be called
    db.delete.assert_not_called()


# ---------------------------------------------------------------------------
# Test: standalone registration deletes user + returns 502 when send fails
# ---------------------------------------------------------------------------

def test_register_standalone_502_and_user_deleted_when_send_fails(monkeypatch):
    """Backend configured but send returns False → user deleted + 502."""
    db = _mock_db()

    monkeypatch.setattr(settings, 'CABINET_EMAIL_AUTH_ENABLED', True)
    monkeypatch.setattr(settings, 'CABINET_EMAIL_VERIFICATION_ENABLED', True)
    monkeypatch.setattr(settings, 'TEST_EMAIL', '')
    monkeypatch.setattr(settings, 'TEST_EMAIL_PASSWORD', '')
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://example.com')
    monkeypatch.setattr(settings, 'CABINET_EMAIL_VERIFICATION_EXPIRE_HOURS', 24)

    monkeypatch.setattr(
        'app.cabinet.routes.auth.disposable_email_service',
        SimpleNamespace(is_disposable=lambda email: False),
    )
    monkeypatch.setattr(
        'app.cabinet.routes.auth.RateLimitCache',
        SimpleNamespace(is_ip_rate_limited=AsyncMock(return_value=False)),
    )

    # Email service IS configured but send FAILS
    mock_email_svc = SimpleNamespace(
        is_configured=lambda: True,
        send_verification_email=MagicMock(return_value=False),
    )
    monkeypatch.setattr('app.cabinet.routes.auth.email_service', mock_email_svc)

    # Mock create_user_by_email to return a fake user
    fake_user = MagicMock()
    fake_user.id = 999
    fake_user.email = 'new@test.com'
    fake_user.first_name = 'Test'
    fake_user.language = 'en'
    fake_user.email_verification_token = None
    fake_user.email_verification_expires = None
    monkeypatch.setattr(
        'app.cabinet.routes.auth.create_user_by_email',
        AsyncMock(return_value=fake_user),
    )
    monkeypatch.setattr(
        'app.cabinet.routes.auth.get_rendered_override',
        AsyncMock(return_value=None),
    )

    app = _make_test_app(monkeypatch, db)
    client = TestClient(app)

    resp = client.post('/cabinet/auth/email/register/standalone', json={
        'email': 'new@test.com',
        'password': 'StrongPass123!',
    })

    assert resp.status_code == 502
    assert 'Failed to send' in resp.json()['detail']
    # User must be deleted to avoid stuck state
    db.delete.assert_called_once_with(fake_user)


# ---------------------------------------------------------------------------
# Test: retry after 502 succeeds (user was deleted, email is free)
# ---------------------------------------------------------------------------

def test_register_standalone_retry_after_502(monkeypatch):
    """After 502 (user deleted), re-registration with same email works."""
    db = _mock_db()

    monkeypatch.setattr(settings, 'CABINET_EMAIL_AUTH_ENABLED', True)
    monkeypatch.setattr(settings, 'CABINET_EMAIL_VERIFICATION_ENABLED', True)
    monkeypatch.setattr(settings, 'TEST_EMAIL', '')
    monkeypatch.setattr(settings, 'TEST_EMAIL_PASSWORD', '')
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://example.com')
    monkeypatch.setattr(settings, 'CABINET_EMAIL_VERIFICATION_EXPIRE_HOURS', 24)

    monkeypatch.setattr(
        'app.cabinet.routes.auth.disposable_email_service',
        SimpleNamespace(is_disposable=lambda email: False),
    )
    monkeypatch.setattr(
        'app.cabinet.routes.auth.RateLimitCache',
        SimpleNamespace(is_ip_rate_limited=AsyncMock(return_value=False)),
    )

    # This time send succeeds
    mock_email_svc = SimpleNamespace(
        is_configured=lambda: True,
        send_verification_email=MagicMock(return_value=True),
    )
    monkeypatch.setattr('app.cabinet.routes.auth.email_service', mock_email_svc)

    fake_user = MagicMock()
    fake_user.id = 1000
    fake_user.email = 'new@test.com'
    fake_user.first_name = 'Test'
    fake_user.language = 'en'
    fake_user.email_verification_token = None
    fake_user.email_verification_expires = None
    monkeypatch.setattr(
        'app.cabinet.routes.auth.create_user_by_email',
        AsyncMock(return_value=fake_user),
    )
    monkeypatch.setattr(
        'app.cabinet.routes.auth.get_rendered_override',
        AsyncMock(return_value=None),
    )

    app = _make_test_app(monkeypatch, db)
    client = TestClient(app)

    resp = client.post('/cabinet/auth/email/register/standalone', json={
        'email': 'new@test.com',
        'password': 'StrongPass123!',
    })

    assert resp.status_code == 200
    assert resp.json()['requires_verification'] is True
    db.delete.assert_not_called()


# ---------------------------------------------------------------------------
# Helpers for authenticated flows (link email, resend)
# ---------------------------------------------------------------------------

def _make_fake_user(**overrides):
    """Create a fake User object for authenticated endpoints."""
    user = MagicMock()
    user.id = overrides.get('id', 1)
    user.email = overrides.get('email', None)
    user.email_verified = overrides.get('email_verified', False)
    user.email_verified_at = None
    user.email_verification_token = overrides.get('email_verification_token', 'old-token')
    user.email_verification_expires = overrides.get('email_verification_expires', None)
    user.first_name = overrides.get('first_name', 'Test')
    user.language = 'en'
    user.password_hash = None
    user.telegram_id = overrides.get('telegram_id', 12345)
    user.status = MagicMock()
    user.status.value = 'active'
    return user


# ---------------------------------------------------------------------------
# Test: link email returns 503 when backend not configured
# ---------------------------------------------------------------------------

def test_link_email_503_when_email_unconfigured(monkeypatch):
    """Link email + verification enabled + backend not configured → 503, no commit."""
    db = _mock_db()
    user = _make_fake_user(telegram_id=12345)

    monkeypatch.setattr(settings, 'CABINET_EMAIL_VERIFICATION_ENABLED', True)
    monkeypatch.setattr(
        'app.cabinet.routes.auth.disposable_email_service',
        SimpleNamespace(is_disposable=lambda email: False),
    )
    monkeypatch.setattr(
        'app.cabinet.routes.auth.RateLimitCache',
        SimpleNamespace(is_ip_rate_limited=AsyncMock(return_value=False)),
    )
    monkeypatch.setattr(
        'app.cabinet.routes.auth.is_email_taken',
        AsyncMock(return_value=False),
    )

    mock_email_svc = SimpleNamespace(
        is_configured=lambda: False,
    )
    monkeypatch.setattr('app.cabinet.routes.auth.email_service', mock_email_svc)

    app = _make_test_app(monkeypatch, db, current_user=user)
    client = TestClient(app)

    resp = client.post('/cabinet/auth/email/register', json={
        'email': 'link@test.com',
        'password': 'StrongPass123!',
    })

    assert resp.status_code == 503
    assert 'not configured' in resp.json()['detail']


# ---------------------------------------------------------------------------
# Test: link email returns honest message when send fails (not 502)
# ---------------------------------------------------------------------------

def test_link_email_honest_message_when_send_fails(monkeypatch):
    """Link email + send fails → 200 with warning message, not 502."""
    db = _mock_db()
    user = _make_fake_user(telegram_id=12345)

    monkeypatch.setattr(settings, 'CABINET_EMAIL_VERIFICATION_ENABLED', True)
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://example.com')
    monkeypatch.setattr(settings, 'CABINET_EMAIL_VERIFICATION_EXPIRE_HOURS', 24)
    monkeypatch.setattr(
        'app.cabinet.routes.auth.disposable_email_service',
        SimpleNamespace(is_disposable=lambda email: False),
    )
    monkeypatch.setattr(
        'app.cabinet.routes.auth.RateLimitCache',
        SimpleNamespace(is_ip_rate_limited=AsyncMock(return_value=False)),
    )
    monkeypatch.setattr(
        'app.cabinet.routes.auth.is_email_taken',
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        'app.cabinet.routes.auth.get_rendered_override',
        AsyncMock(return_value=None),
    )

    mock_email_svc = SimpleNamespace(
        is_configured=lambda: True,
        send_verification_email=MagicMock(return_value=False),
    )
    monkeypatch.setattr('app.cabinet.routes.auth.email_service', mock_email_svc)

    app = _make_test_app(monkeypatch, db, current_user=user)
    client = TestClient(app)

    resp = client.post('/cabinet/auth/email/register', json={
        'email': 'link@test.com',
        'password': 'StrongPass123!',
    })

    assert resp.status_code == 200
    assert 'could not be sent' in resp.json()['message']


# ---------------------------------------------------------------------------
# Test: resend returns 503 when backend not configured (before token change)
# ---------------------------------------------------------------------------

def test_resend_503_when_email_unconfigured(monkeypatch):
    """Resend + backend not configured → 503, old token preserved."""
    db = _mock_db()
    user = _make_fake_user(email='user@test.com', email_verified=False)

    monkeypatch.setattr(settings, 'CABINET_EMAIL_VERIFICATION_ENABLED', True)

    mock_email_svc = SimpleNamespace(
        is_configured=lambda: False,
    )
    monkeypatch.setattr('app.cabinet.routes.auth.email_service', mock_email_svc)

    app = _make_test_app(monkeypatch, db, current_user=user)
    client = TestClient(app)

    resp = client.post('/cabinet/auth/email/resend')

    assert resp.status_code == 503
    # Old token must NOT have been replaced
    assert user.email_verification_token == 'old-token'


# ---------------------------------------------------------------------------
# Test: resend restores old token when send fails
# ---------------------------------------------------------------------------

def test_resend_restores_token_when_send_fails(monkeypatch):
    """Resend + send fails → 502, old token restored."""
    db = _mock_db()
    user = _make_fake_user(email='user@test.com', email_verified=False)

    monkeypatch.setattr(settings, 'CABINET_EMAIL_VERIFICATION_ENABLED', True)
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://example.com')
    monkeypatch.setattr(settings, 'CABINET_EMAIL_VERIFICATION_EXPIRE_HOURS', 24)
    monkeypatch.setattr(
        'app.cabinet.routes.auth.get_rendered_override',
        AsyncMock(return_value=None),
    )

    mock_email_svc = SimpleNamespace(
        is_configured=lambda: True,
        send_verification_email=MagicMock(return_value=False),
    )
    monkeypatch.setattr('app.cabinet.routes.auth.email_service', mock_email_svc)

    app = _make_test_app(monkeypatch, db, current_user=user)
    client = TestClient(app)

    resp = client.post('/cabinet/auth/email/resend')

    assert resp.status_code == 502
    # Old token must be restored
    assert user.email_verification_token == 'old-token'
