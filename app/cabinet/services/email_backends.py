"""Pluggable email transport backends.

Usage:
    EMAIL_BACKEND=smtp          # default, uses smtplib
    EMAIL_BACKEND=custom        # loads class from EMAIL_BACKEND_CLASS

    EMAIL_BACKEND_CLASS=package.module:ClassName
"""

from __future__ import annotations

import importlib
import smtplib
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from typing import Protocol

import structlog

from app.config import settings

logger = structlog.get_logger(__name__)

# Dedup flags — log config errors once, not on every _get_backend() call
_backend_error_logged = False
_custom_error_logged = False


@dataclass(frozen=True, slots=True)
class EmailMessage:
    """Structured email payload passed to backends."""

    from_email: str
    from_name: str
    to_email: str
    subject: str
    body_html: str
    body_text: str | None = None


class EmailBackend(Protocol):
    """Interface that every email backend must implement."""

    def is_configured(self) -> bool: ...

    def send(self, message: EmailMessage) -> bool: ...


class DisabledBackend:
    """Returned when backend cannot be resolved. Never sends."""

    def is_configured(self) -> bool:
        return False

    def send(self, message: EmailMessage) -> bool:
        return False


class SmtpBackend:
    """Email backend using Python's built-in smtplib."""

    def is_configured(self) -> bool:
        return settings.is_smtp_configured()

    def _get_connection(self) -> smtplib.SMTP:
        smtp = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30)
        smtp.ehlo()

        if settings.SMTP_USE_TLS:
            smtp.starttls()
            smtp.ehlo()

        if settings.SMTP_USER and settings.SMTP_PASSWORD:
            if smtp.has_extn('auth'):
                smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            else:
                logger.debug(
                    'SMTP server does not support AUTH, skipping authentication',
                    host=settings.SMTP_HOST,
                )

        return smtp

    def send(self, message: EmailMessage) -> bool:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = message.subject
        msg['From'] = f'{message.from_name} <{message.from_email}>'
        msg['To'] = message.to_email
        msg['Date'] = formatdate(localtime=False)
        msg['Message-ID'] = make_msgid(domain=message.from_email.split('@')[-1])

        if message.body_text is not None:
            msg.attach(MIMEText(message.body_text, 'plain', 'utf-8'))
        msg.attach(MIMEText(message.body_html, 'html', 'utf-8'))

        with self._get_connection() as smtp:
            smtp.sendmail(message.from_email, message.to_email, msg.as_string())

        return True


def _load_custom_backend() -> EmailBackend:
    global _custom_error_logged

    class_path = settings.EMAIL_BACKEND_CLASS
    if not class_path:
        if not _custom_error_logged:
            logger.error('EMAIL_BACKEND=custom but EMAIL_BACKEND_CLASS is empty, email disabled')
            _custom_error_logged = True
        return DisabledBackend()

    try:
        module_path, class_name = class_path.rsplit(':', 1)
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        instance = cls()
    except Exception as e:
        if not _custom_error_logged:
            logger.error(
                'Failed to load custom email backend, email disabled',
                class_path=class_path,
                error=e,
            )
            _custom_error_logged = True
        return DisabledBackend()

    if not callable(getattr(instance, 'is_configured', None)) or not callable(
        getattr(instance, 'send', None)
    ):
        if not _custom_error_logged:
            logger.error(
                'Custom email backend missing is_configured/send methods, email disabled',
                class_path=class_path,
            )
            _custom_error_logged = True
        return DisabledBackend()

    return instance


def get_backend() -> EmailBackend:
    """Instantiate the email backend selected by EMAIL_BACKEND setting."""
    global _backend_error_logged

    name = (settings.EMAIL_BACKEND or 'smtp').lower()
    if name == 'smtp':
        return SmtpBackend()
    if name == 'custom':
        return _load_custom_backend()

    if not _backend_error_logged:
        logger.error('Unknown EMAIL_BACKEND, email disabled', backend=name)
        _backend_error_logged = True
    return DisabledBackend()
