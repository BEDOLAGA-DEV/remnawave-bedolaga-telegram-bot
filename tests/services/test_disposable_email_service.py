"""Одноразовые домены: ручной список оператора и совпадение по родительскому домену.

Из отчёта: скрипт регистрировал почту на домене, которого не было в скачиваемом списке.
Оператор должен уметь добавить домен сам, не дожидаясь апстрима; а сервисы с плавающими
поддоменами (x.mail.tm) должны ловиться по родителю.
"""

from __future__ import annotations

import pytest

from app.services.disposable_email_service import DisposableEmailService, parse_extra_domains


def _service(fetched: set[str]) -> DisposableEmailService:
    service = DisposableEmailService()
    service._domains = frozenset(fetched)
    return service


@pytest.fixture(autouse=True)
def _check_enabled(monkeypatch):
    from app.services import disposable_email_service as module

    monkeypatch.setattr(module.settings, 'DISPOSABLE_EMAIL_CHECK_ENABLED', True)
    monkeypatch.setattr(module.settings, 'DISPOSABLE_EMAIL_EXTRA_DOMAINS', '')


def test_parse_extra_domains_tolerates_separators_case_and_at_sign():
    assert parse_extra_domains(' Evil.TM, @other.example\nthird.io;fourth.net  ') == frozenset(
        {'evil.tm', 'other.example', 'third.io', 'fourth.net'}
    )
    assert parse_extra_domains('') == frozenset()
    assert parse_extra_domains(None) == frozenset()


def test_operator_extra_domains_block_even_without_fetched_list(monkeypatch):
    from app.services import disposable_email_service as module

    monkeypatch.setattr(module.settings, 'DISPOSABLE_EMAIL_EXTRA_DOMAINS', 'evil.tm')
    service = _service(set())
    assert service.is_disposable('bot@evil.tm') is True
    assert service.is_disposable('human@gmail.com') is False


def test_parent_domain_matches_for_both_lists(monkeypatch):
    from app.services import disposable_email_service as module

    monkeypatch.setattr(module.settings, 'DISPOSABLE_EMAIL_EXTRA_DOMAINS', 'evil.tm')
    service = _service({'tempmail.io'})
    assert service.is_disposable('a@x.evil.tm') is True
    assert service.is_disposable('a@deep.sub.tempmail.io') is True
    assert service.is_disposable('a@tempmail.io') is True
    assert service.is_disposable('a@nottempmail.io') is False


def test_disabled_flag_and_malformed_email(monkeypatch):
    from app.services import disposable_email_service as module

    monkeypatch.setattr(module.settings, 'DISPOSABLE_EMAIL_EXTRA_DOMAINS', 'evil.tm')
    service = _service({'tempmail.io'})
    assert service.is_disposable('no-at-sign') is False
    monkeypatch.setattr(module.settings, 'DISPOSABLE_EMAIL_CHECK_ENABLED', False)
    assert service.is_disposable('a@evil.tm') is False
