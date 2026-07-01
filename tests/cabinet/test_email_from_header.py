"""Regression: the email ``From`` header must encode a non-ASCII display name
correctly (PR #3005).

With an f-string (``f'{name} <{addr}>'``) and a non-ASCII SMTP_FROM_NAME, Python's
Compat32 policy RFC2047-encodes the WHOLE header — addr-spec included — into one
encoded-word, leaving no valid address. Clients then render the address twice
("Name <a@b> <a@b>") and DMARC/parsing/spam scoring suffer. ``formataddr`` encodes
only the display name and keeps a proper addr-spec.
"""

from __future__ import annotations

import email as email_mod
from email.header import decode_header, make_header
from email.utils import parseaddr
from typing import Self

from app.cabinet.services.email_service import email_service


class _FakeSMTP:
    """Captures the raw message handed to ``sendmail`` and acts as a CM."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def sendmail(self, _from_addr: str, _to_addr: str, msg: str) -> None:
        self.messages.append(msg)


def _send_and_capture(monkeypatch, from_name: str) -> str:
    from app.config import settings

    monkeypatch.setattr(type(settings), 'is_smtp_configured', lambda self: True)
    monkeypatch.setattr(type(settings), 'get_smtp_from_email', lambda self: 'sender@gmail.com')
    monkeypatch.setattr(settings, 'SMTP_FROM_NAME', from_name, raising=False)

    fake = _FakeSMTP()
    monkeypatch.setattr(email_service, '_get_smtp_connection', lambda: fake)

    ok = email_service.send_email('to@example.com', 'Subj', '<b>hi</b>')
    assert ok is True
    assert fake.messages, 'send_email did not hand a message to sendmail'

    parsed = email_mod.message_from_string(fake.messages[0])
    return parsed['From']


def test_non_ascii_from_name_keeps_single_valid_address(monkeypatch):
    from_hdr = _send_and_capture(monkeypatch, 'Василиса Стор❤️')

    name, addr = parseaddr(from_hdr)
    # A valid addr-spec survives (the whole header was NOT swallowed into one word).
    assert addr == 'sender@gmail.com'
    # …exactly once — no duplication appended by the client/MTA.
    assert from_hdr.count('sender@gmail.com') == 1
    # …and the display name round-trips to the original Cyrillic brand.
    assert str(make_header(decode_header(name))) == 'Василиса Стор❤️'


def test_ascii_from_name_unaffected(monkeypatch):
    from_hdr = _send_and_capture(monkeypatch, 'Support')
    name, addr = parseaddr(from_hdr)
    assert addr == 'sender@gmail.com'
    assert name == 'Support'
    assert from_hdr.count('sender@gmail.com') == 1
