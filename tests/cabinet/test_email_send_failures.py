"""Что происходит с письмом, когда SMTP-сервер недоступен или отказывает.

Из лога рассылки на 36 адресов было видно только «Failed to send email to
error=OSError(101, 'Network is unreachable')» и полотно трейсбека — на каждый
адрес. По нему нельзя понять даже, к какому серверу шли, а ждать таймаут
соединения на каждом следующем адресе бессмысленно: отказ относится ко всей
пачке, а не к получателю.

Отдельно проверяется ловушка иерархии smtplib: ``SMTPException`` наследуется от
``OSError``, поэтому широкий ``except OSError`` перехватывает и отказ по одному
адресу — и заглушил бы почту всем.
"""

import smtplib
import sys
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from app.cabinet.services.email_service import EmailService


# Пакет реэкспортирует синглтон под именем модуля, поэтому точечный путь
# 'app.cabinet.services.email_service' резолвится в объект, а не в модуль.
email_service_module = sys.modules['app.cabinet.services.email_service']


@pytest.fixture
def service(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, 'SMTP_HOST', 'smtp.example.com')
    monkeypatch.setattr(settings, 'SMTP_PORT', 587)
    monkeypatch.setattr(settings, 'SMTP_USER', 'bot@example.com')
    monkeypatch.setattr(settings, 'SMTP_PASSWORD', 'secret')
    monkeypatch.setattr(settings, 'SMTP_USE_TLS', True)
    monkeypatch.setattr(settings, 'SMTP_USE_SSL', False)
    monkeypatch.setattr(settings, 'SMTP_FROM_EMAIL', 'bot@example.com')
    monkeypatch.setattr(settings, 'SMTP_FROM_NAME', 'Bot')

    instance = EmailService()
    assert instance.is_configured(), 'фикстура не настроила SMTP'
    return instance


def _fail_with(service, monkeypatch, error: BaseException) -> list[int]:
    """Подменяет соединение на падающее; возвращает счётчик попыток."""
    attempts: list[int] = []

    def broken_connection():
        attempts.append(1)
        raise error

    monkeypatch.setattr(service, '_get_smtp_connection', broken_connection)
    return attempts


def _succeed(service, monkeypatch) -> list[str]:
    """Подменяет соединение на рабочее; возвращает список отправленных адресов."""
    sent: list[str] = []

    @contextmanager
    def working_connection():
        smtp = MagicMock()
        smtp.sendmail.side_effect = lambda _sender, to_email, _body: sent.append(to_email)
        yield smtp

    monkeypatch.setattr(service, '_get_smtp_connection', working_connection)
    return sent


def _send(service, to_email='user@example.com') -> bool:
    return service.send_email(to_email=to_email, subject='Тема', body_html='<p>Текст</p>')


class TestUnreachableServer:
    def test_second_address_does_not_wait_for_the_same_timeout(self, service, monkeypatch):
        """Один отказ соединения доказывает недоступность для всей пачки."""
        attempts = _fail_with(service, monkeypatch, OSError(101, 'Network is unreachable'))

        assert _send(service, 'first@example.com') is False
        assert _send(service, 'second@example.com') is False

        assert len(attempts) == 1

    def test_cooldown_expires(self, service, monkeypatch):
        attempts = _fail_with(service, monkeypatch, OSError(101, 'Network is unreachable'))

        assert _send(service) is False
        # Окно прошло — пробуем снова, а не молчим вечно.
        service._unreachable_until = 0.0
        assert _send(service) is False

        assert len(attempts) == 2

    def test_success_clears_the_cooldown(self, service, monkeypatch):
        _fail_with(service, monkeypatch, TimeoutError('timed out'))
        assert _send(service) is False
        assert service._cooldown_left() > 0

        sent = _succeed(service, monkeypatch)
        service._unreachable_until = 0.0
        assert _send(service, 'ok@example.com') is True

        assert sent == ['ok@example.com']
        assert service._cooldown_left() == 0

    def test_log_names_the_endpoint_and_omits_the_traceback(self, service, monkeypatch):
        """Без адреса сервера «Network is unreachable» не говорит ничего."""
        records: list[tuple[str, dict]] = []

        class _Logger:
            def warning(self, event, **kwargs):
                records.append((event, kwargs))

            def debug(self, *_args, **_kwargs):
                pass

            def info(self, *_args, **_kwargs):
                pass

            def error(self, *_args, **_kwargs):
                records.append(('ERROR', _kwargs))

        monkeypatch.setattr(email_service_module, 'logger', _Logger())
        _fail_with(service, monkeypatch, OSError(101, 'Network is unreachable'))

        assert _send(service) is False

        assert len(records) == 1
        _event, fields = records[0]
        assert fields['smtp_host'] == 'smtp.example.com'
        assert fields['smtp_port'] == 587
        assert fields['smtp_mode'] == 'starttls'
        assert 'Network is unreachable' in fields['reason']
        # Объект исключения в kwarg заставляет логгер приложить трейсбек —
        # здесь он всегда один и тот же и только забивает лог на рассылке.
        assert not any(isinstance(value, BaseException) for value in fields.values())


class TestServerRefusal:
    def test_rejected_recipient_does_not_mute_the_rest(self, service, monkeypatch):
        """SMTPException наследуется от OSError: широкий except глушил бы всех."""
        attempts = _fail_with(
            service,
            monkeypatch,
            smtplib.SMTPRecipientsRefused({'bad@example.com': (550, b'No such user')}),
        )

        assert _send(service, 'bad@example.com') is False
        assert service._cooldown_left() == 0

        assert _send(service, 'good@example.com') is False
        assert len(attempts) == 2, 'вторая попытка не состоялась — сработало остывание'

    def test_auth_failure_is_not_a_connection_failure(self, service, monkeypatch):
        _fail_with(service, monkeypatch, smtplib.SMTPAuthenticationError(535, b'Bad credentials'))

        assert _send(service) is False
        assert service._cooldown_left() == 0

    def test_connect_error_is_a_connection_failure(self, service, monkeypatch):
        """SMTPConnectError — тоже SMTPException, но соединение не состоялось."""
        _fail_with(service, monkeypatch, smtplib.SMTPConnectError(421, b'Cannot connect'))

        assert _send(service) is False
        assert service._cooldown_left() > 0


class TestUnexpectedErrors:
    def test_a_code_bug_still_reports_a_traceback(self, service, monkeypatch):
        """Ошибка сборки письма — не работа сети, и трейсбек по ней нужен."""
        records: list[str] = []

        class _Logger:
            def error(self, event, **_kwargs):
                records.append(event)

            def warning(self, *_args, **_kwargs):
                records.append('WARNING')

            def debug(self, *_args, **_kwargs):
                pass

            def info(self, *_args, **_kwargs):
                pass

        monkeypatch.setattr(email_service_module, 'logger', _Logger())
        _fail_with(service, monkeypatch, ValueError('шаблон сломан'))

        assert _send(service) is False

        assert records == ['Failed to send email to']
        assert service._cooldown_left() == 0
