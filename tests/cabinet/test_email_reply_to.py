"""Заголовок ``Reply-To`` в исходящих письмах.

Адрес отправителя часто живёт на поддомене без MX (`noreply@mail.example.com`
у Resend/SES/Postmark): ответ клиента на такое письмо просто отбивается. Без
``Reply-To`` человек, нажавший «Ответить», уходит в никуда, а провайдеры
считают отсутствие обратного канала признаком массовой рассылки.
"""

from __future__ import annotations

import email as email_mod
from email.utils import parseaddr
from typing import Self

import pytest

from app.cabinet.services.email_service import email_service


class _FakeSMTP:
    """Ловит сырое письмо, отданное в ``sendmail``, и работает как CM."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def sendmail(self, _from_addr: str, _to_addr: str, msg: str) -> None:
        self.messages.append(msg)


@pytest.fixture
def smtp_ready(monkeypatch):
    from app.config import settings

    fake = _FakeSMTP()
    monkeypatch.setattr(type(settings), 'is_smtp_configured', lambda self: True)
    monkeypatch.setattr(settings, 'SMTP_FROM_EMAIL', 'noreply@mail.example.com')
    monkeypatch.setattr(settings, 'SMTP_FROM_NAME', 'Example VPN')
    monkeypatch.setattr(email_service, '_get_smtp_connection', lambda: fake)
    return fake


def _send(monkeypatch, smtp, reply_to: str) -> email_mod.message.Message:
    from app.config import settings

    monkeypatch.setattr(settings, 'SMTP_REPLY_TO', reply_to)
    assert email_service.send_email(to_email='user@example.com', subject='Тема', body_html='<p>hi</p>') is True
    return email_mod.message_from_string(smtp.messages[0])


def test_reply_to_is_set_when_configured(monkeypatch, smtp_ready):
    """Настроенный адрес попадает в Reply-To, From остаётся прежним."""
    msg = _send(monkeypatch, smtp_ready, 'support@example.com')

    assert parseaddr(msg['Reply-To'])[1] == 'support@example.com'
    assert parseaddr(msg['From'])[1] == 'noreply@mail.example.com'


def test_no_reply_to_header_by_default(monkeypatch, smtp_ready):
    """Пустая настройка — поведение как раньше, лишнего заголовка нет."""
    msg = _send(monkeypatch, smtp_ready, '')

    assert msg['Reply-To'] is None


@pytest.mark.parametrize('bad', ['not-an-email', 'support@example.com\r\nBcc: victim@example.com', '   '])
def test_broken_reply_to_is_dropped(monkeypatch, smtp_ready, bad):
    """Мусор из .env не должен ни ломать письмо, ни дописывать чужой заголовок."""
    msg = _send(monkeypatch, smtp_ready, bad)

    assert msg['Reply-To'] is None
    assert msg['Bcc'] is None


def test_reply_to_is_trimmed(monkeypatch, smtp_ready):
    """Пробелы вокруг адреса обязаны срезаться до сборки заголовка.

    Без этого formataddr соберёт «Example VPN <  support@example.com  >» —
    заголовок формально есть, но адрес в нём разберут не все клиенты, и
    обратный канал молча не работает, то есть ровно то, ради чего PR и нужен.
    """
    msg = _send(monkeypatch, smtp_ready, '  support@example.com  ')

    assert parseaddr(msg['Reply-To'])[1] == 'support@example.com'
    assert '  support@example.com  ' not in msg['Reply-To']
