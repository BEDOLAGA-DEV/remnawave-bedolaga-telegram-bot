"""Поиск двойника по ящику выполняется реальным запросом — здесь он и проверяется.

Чистые тесты канонизации в ``tests/utils/test_email_alias.py`` ничего не знают о
диалекте БД, а вся суть фичи в SQL-условии. Первая версия использовала
``split_part``: в PostgreSQL он есть, в SQLite — нет, и на ``DATABASE_MODE=sqlite``
(а ``auto`` скатывается туда без настроенного PostgreSQL) регистрация с любого
gmail-адреса падала бы 500. Поэтому запрос гоняется по настоящей SQLite.
"""

from __future__ import annotations

import pytest

from app.database.crud.user import get_user_by_email_alias, is_email_taken
from app.database.models import User
from tests.fixtures.sqlite_memory import memory_session


async def _add(session, email: str, telegram_id: int) -> User:
    user = User(telegram_id=telegram_id, email=email, referral_code=f'ref{telegram_id}', language='ru')
    session.add(user)
    await session.commit()
    return user


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'stored,candidate',
    [
        ('user@gmail.com', 'user+vpn@gmail.com'),
        ('user@gmail.com', 'u.s.e.r@gmail.com'),
        ('user@gmail.com', 'USER+1@GoogleMail.com'),
        ('u.ser+old@googlemail.com', 'user@gmail.com'),
        ('user@yandex.ru', 'user+tag@ya.ru'),
        ('user@icloud.com', 'user+tag@me.com'),
    ],
)
async def test_alias_of_an_existing_mailbox_is_found(monkeypatch, stored: str, candidate: str) -> None:
    async with memory_session(monkeypatch, [User.__table__]) as session:
        owner = await _add(session, stored, telegram_id=1)

        found = await get_user_by_email_alias(session, candidate)

    assert found is not None
    assert found.id == owner.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'stored,candidate',
    [
        # Точки значимы везде, кроме Gmail
        ('user@yandex.ru', 'u.s.e.r@yandex.ru'),
        # Незнакомый домен не трогаем: «+» там может быть частью имени ящика
        ('user@corp.example', 'user+vpn@corp.example'),
        # Разные ящики одного провайдера
        ('user@gmail.com', 'user2@gmail.com'),
        # Префикс — не алиас: «user» не должен цепляться за «username»
        ('username@gmail.com', 'user@gmail.com'),
    ],
)
async def test_different_mailboxes_are_not_matched(monkeypatch, stored: str, candidate: str) -> None:
    async with memory_session(monkeypatch, [User.__table__]) as session:
        await _add(session, stored, telegram_id=1)

        assert await get_user_by_email_alias(session, candidate) is None


@pytest.mark.asyncio
async def test_like_wildcards_in_the_local_part_are_escaped(monkeypatch) -> None:
    """«_» — обычный символ в адресе, но джокер в LIKE."""
    async with memory_session(monkeypatch, [User.__table__]) as session:
        await _add(session, 'axb@gmail.com', telegram_id=1)

        assert await get_user_by_email_alias(session, 'a_b+tag@gmail.com') is None


@pytest.mark.asyncio
async def test_own_alias_is_not_taken_even_next_to_a_stranger(monkeypatch) -> None:
    """LIMIT 1 без исключения себя мог вернуть своего же юзера и скрыть чужого."""
    async with memory_session(monkeypatch, [User.__table__]) as session:
        owner = await _add(session, 'user@gmail.com', telegram_id=1)

        assert await is_email_taken(session, 'user+vpn@gmail.com', exclude_user_id=owner.id) is False

        stranger = await _add(session, 'other@gmail.com', telegram_id=2)

        assert await is_email_taken(session, 'other+vpn@gmail.com', exclude_user_id=owner.id) is True
        assert await is_email_taken(session, 'other+vpn@gmail.com', exclude_user_id=stranger.id) is False


@pytest.mark.asyncio
async def test_degenerate_and_unknown_addresses_do_not_query(monkeypatch) -> None:
    async with memory_session(monkeypatch, [User.__table__]) as session:
        await _add(session, '+tag@gmail.com', telegram_id=1)

        # Локальной части не остаётся — сравнивать не с чем
        assert await get_user_by_email_alias(session, '+other@gmail.com') is None
        assert await get_user_by_email_alias(session, 'not-an-email') is None
        assert await get_user_by_email_alias(session, '') is None
