"""Тесты канонизации почтовых адресов из app.utils.email_alias.

Проверка «email занят» сравнивала адреса с точностью до регистра, поэтому
`user+1@gmail.com` и `u.ser@gmail.com` заводили отдельные аккаунты, хотя письма
идут в один ящик — и каждый такой аккаунт получал свою пробную подписку.
"""

import pytest

from app.utils.email_alias import canonical_email, email_domain, has_alias_forms, is_email_alias_of


class TestCanonicalEmail:
    @pytest.mark.parametrize(
        ('raw', 'expected'),
        [
            ('User@Gmail.com', 'user@gmail.com'),
            ('  user@gmail.com  ', 'user@gmail.com'),
            ('user+vpn@gmail.com', 'user@gmail.com'),
            ('user+a+b@gmail.com', 'user@gmail.com'),
            ('u.s.e.r@gmail.com', 'user@gmail.com'),
            ('u.s.e.r+tag@gmail.com', 'user@gmail.com'),
            ('user@googlemail.com', 'user@gmail.com'),
            ('user+tag@yandex.ru', 'user@yandex.ru'),
            ('user@ya.ru', 'user@yandex.ru'),
            ('user@yandex.com', 'user@yandex.ru'),
            ('user+tag@outlook.com', 'user@outlook.com'),
        ],
    )
    def test_aliases_collapse_to_one_mailbox(self, raw: str, expected: str) -> None:
        assert canonical_email(raw) == expected

    def test_dots_matter_outside_gmail(self) -> None:
        """Точки игнорирует только Gmail — у остальных это разные ящики."""
        assert canonical_email('u.ser@yandex.ru') != canonical_email('user@yandex.ru')

    def test_unknown_domain_is_left_alone(self) -> None:
        """Незнакомый домен трогать нельзя: «+» там может быть частью имени."""
        assert canonical_email('user+tag@corp.example') == 'user+tag@corp.example'
        assert canonical_email('u.ser@corp.example') == 'u.ser@corp.example'

    @pytest.mark.parametrize('raw', ['', None, 'not-an-email', 'two@at@signs.com'])
    def test_malformed_input_survives(self, raw: str | None) -> None:
        """Мусор на входе отвергает валидация выше — здесь он не должен падать."""
        assert canonical_email(raw) == (raw or '').strip().lower()

    def test_local_part_cannot_become_empty(self) -> None:
        """«+tag@gmail.com» свернулось бы в «@gmail.com» — общий ящик для всех."""
        assert canonical_email('+tag@gmail.com') == '+tag@gmail.com'


class TestIsEmailAliasOf:
    def test_detects_alias(self) -> None:
        assert is_email_alias_of('user+1@gmail.com', 'user@gmail.com') is True
        assert is_email_alias_of('u.ser@gmail.com', 'user@gmail.com') is True

    def test_same_address_is_not_an_alias(self) -> None:
        """Точное совпадение — задача обычной проверки, здесь оно не «алиас»."""
        assert is_email_alias_of('user@gmail.com', 'User@GMAIL.com') is False

    def test_different_mailboxes(self) -> None:
        assert is_email_alias_of('other@gmail.com', 'user@gmail.com') is False
        assert is_email_alias_of('user@gmail.com', 'user@yandex.ru') is False

    @pytest.mark.parametrize(('a', 'b'), [(None, 'user@gmail.com'), ('user@gmail.com', None), (None, None)])
    def test_missing_values(self, a: str | None, b: str | None) -> None:
        assert is_email_alias_of(a, b) is False


class TestHelpers:
    def test_has_alias_forms(self) -> None:
        assert has_alias_forms('user@gmail.com') is True
        assert has_alias_forms('user@yandex.ru') is True
        assert has_alias_forms('user@corp.example') is False
        assert has_alias_forms('') is False

    def test_email_domain_merges_twins(self) -> None:
        assert email_domain('user@ya.ru') == 'yandex.ru'
        assert email_domain('user@googlemail.com') == 'gmail.com'
        assert email_domain('broken') == ''
