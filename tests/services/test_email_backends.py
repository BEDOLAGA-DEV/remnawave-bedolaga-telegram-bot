"""Tests for email backend abstraction and EmailService delegation."""

import os
import sys
import types as _types
from pathlib import Path
from unittest.mock import MagicMock

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Pre-import stubs for compatibility when running alongside auth flow tests
if 'redis.exceptions' not in sys.modules:
    _redis_exc = _types.ModuleType('redis.exceptions')
    _redis_exc.NoScriptError = type('NoScriptError', (Exception,), {})
    _redis_exc.ConnectionError = type('ConnectionError', (Exception,), {})
    sys.modules['redis.exceptions'] = _redis_exc
    if 'redis' not in sys.modules or not hasattr(sys.modules['redis'], 'exceptions'):
        _redis_mod = sys.modules.get('redis') or _types.ModuleType('redis')
        _redis_mod.exceptions = _redis_exc
        sys.modules['redis'] = _redis_mod

os.environ.setdefault('BACKUP_LOCATION', str(Path('data/backups').resolve()))
Path('data/backups').mkdir(parents=True, exist_ok=True)

from app.cabinet.services.email_backends import (
    DisabledBackend,
    EmailMessage,
    SmtpBackend,
    get_backend,
    _load_custom_backend,
)
from app.cabinet.services.email_service import EmailService
from app.config import settings


# --- get_backend() tests ---


def test_default_backend_is_smtp(monkeypatch):
    monkeypatch.setattr(settings, 'EMAIL_BACKEND', 'smtp')
    backend = get_backend()
    assert isinstance(backend, SmtpBackend)


def test_empty_backend_defaults_to_smtp(monkeypatch):
    monkeypatch.setattr(settings, 'EMAIL_BACKEND', '')
    backend = get_backend()
    assert isinstance(backend, SmtpBackend)


def test_unknown_backend_returns_disabled(monkeypatch):
    import app.cabinet.services.email_backends as mod
    monkeypatch.setattr(settings, 'EMAIL_BACKEND', 'nonexistent')
    monkeypatch.setattr(mod, '_backend_error_logged', False)
    backend = get_backend()
    assert isinstance(backend, DisabledBackend)


def test_unknown_backend_does_not_crash_is_configured(monkeypatch):
    """EMAIL_BACKEND=nonexistent -> is_configured() returns False, no exception."""
    import app.cabinet.services.email_backends as mod
    monkeypatch.setattr(settings, 'EMAIL_BACKEND', 'nonexistent')
    monkeypatch.setattr(mod, '_backend_error_logged', False)
    service = EmailService()
    assert service.is_configured() is False


def test_custom_backend_empty_class_returns_disabled(monkeypatch):
    import app.cabinet.services.email_backends as mod
    monkeypatch.setattr(settings, 'EMAIL_BACKEND', 'custom')
    monkeypatch.setattr(settings, 'EMAIL_BACKEND_CLASS', '')
    monkeypatch.setattr(mod, '_custom_error_logged', False)
    backend = get_backend()
    assert isinstance(backend, DisabledBackend)


def test_custom_backend_bad_import_returns_disabled(monkeypatch):
    import app.cabinet.services.email_backends as mod
    monkeypatch.setattr(settings, 'EMAIL_BACKEND', 'custom')
    monkeypatch.setattr(settings, 'EMAIL_BACKEND_CLASS', 'nonexistent.module:Cls')
    monkeypatch.setattr(mod, '_custom_error_logged', False)
    backend = get_backend()
    assert isinstance(backend, DisabledBackend)


def test_custom_backend_missing_contract_returns_disabled(monkeypatch):
    """Class exists but doesn't implement is_configured/send."""
    import app.cabinet.services.email_backends as mod
    monkeypatch.setattr(settings, 'EMAIL_BACKEND', 'custom')
    monkeypatch.setattr(settings, 'EMAIL_BACKEND_CLASS', 'builtins:object')
    monkeypatch.setattr(mod, '_custom_error_logged', False)
    backend = get_backend()
    assert isinstance(backend, DisabledBackend)


def test_custom_backend_valid_class_loaded(monkeypatch):
    import app.cabinet.services.email_backends as mod
    monkeypatch.setattr(settings, 'EMAIL_BACKEND', 'custom')
    monkeypatch.setattr(
        settings, 'EMAIL_BACKEND_CLASS',
        'app.cabinet.services.email_backends:DisabledBackend',
    )
    monkeypatch.setattr(mod, '_custom_error_logged', False)
    backend = get_backend()
    assert isinstance(backend, DisabledBackend)
    assert backend.is_configured() is False


# --- DisabledBackend tests ---


def test_disabled_backend_is_not_configured():
    backend = DisabledBackend()
    assert backend.is_configured() is False


def test_disabled_backend_send_returns_false():
    backend = DisabledBackend()
    msg = EmailMessage(
        from_email='a@b.com', from_name='Test',
        to_email='c@d.com', subject='Hi', body_html='<p>hi</p>',
    )
    assert backend.send(msg) is False


# --- EmailService delegation tests ---


def test_send_email_returns_false_when_not_configured(monkeypatch):
    """send_email() returns False when backend is not configured."""
    import app.cabinet.services.email_backends as mod
    monkeypatch.setattr(settings, 'EMAIL_BACKEND', 'nonexistent')
    monkeypatch.setattr(mod, '_backend_error_logged', False)
    service = EmailService()
    result = service.send_email('to@test.com', 'Subject', '<p>body</p>')
    assert result is False


def test_backend_send_exception_returns_false(monkeypatch):
    """If backend.send() raises, send_email() catches and returns False."""
    class ExplodingBackend:
        def is_configured(self):
            return True
        def send(self, message):
            raise RuntimeError('boom')

    monkeypatch.setattr(settings, 'SMTP_FROM_EMAIL', 'from@test.com')
    monkeypatch.setattr(settings, 'SMTP_FROM_NAME', 'Test')

    service = EmailService()
    service._get_backend = lambda: ExplodingBackend()
    result = service.send_email('to@test.com', 'Subject', '<p>body</p>')
    assert result is False


def test_send_email_delegates_to_backend(monkeypatch):
    """send_email() builds EmailMessage and passes it to backend.send()."""
    captured = {}

    class SpyBackend:
        def is_configured(self):
            return True
        def send(self, message):
            captured['message'] = message
            return True

    monkeypatch.setattr(settings, 'SMTP_FROM_EMAIL', 'from@test.com')
    monkeypatch.setattr(settings, 'SMTP_FROM_NAME', 'Sender')

    service = EmailService()
    service._get_backend = lambda: SpyBackend()
    result = service.send_email('to@test.com', 'Hello', '<p>world</p>')

    assert result is True
    msg = captured['message']
    assert isinstance(msg, EmailMessage)
    assert msg.from_email == 'from@test.com'
    assert msg.from_name == 'Sender'
    assert msg.to_email == 'to@test.com'
    assert msg.subject == 'Hello'
    assert msg.body_html == '<p>world</p>'
    assert msg.body_text is not None  # auto-generated from HTML


# --- Log dedup tests ---


def test_log_dedup_only_logs_once(monkeypatch):
    """Config error should only be logged once, not on every call."""
    import app.cabinet.services.email_backends as mod
    monkeypatch.setattr(settings, 'EMAIL_BACKEND', 'nonexistent')
    monkeypatch.setattr(mod, '_backend_error_logged', False)

    log_calls = []
    monkeypatch.setattr(mod.logger, 'error', lambda *a, **kw: log_calls.append(1))

    get_backend()
    get_backend()
    get_backend()

    assert len(log_calls) == 1
