"""Данные плательщика для шлюзов, которые требуют их в каждом платеже.

Platega (docs.platega.io, «Создание платежной ссылки с заданным методом»):
``metadata.userId`` и ``metadata.userName`` — строки, обязательные для части
категорий магазинов; без ``userId`` отключается антифрод, магазин могут
отключить. MulenPay (требование провайдера, 2026-09-15): ``client`` — почта,
телефон, Telegram ID и т.п. Все поля всегда непустые.
"""

from __future__ import annotations

import hashlib

import pytest

from app.database.models import Base, GuestPurchase, User
from app.services.payment.payer_identity import (
    PAYER_FIELD_MAX_LENGTH,
    PayerIdentity,
    PayerRecord,
    payer_from_guest,
    payer_from_user,
    resolve_guest_payer,
    resolve_user_payer,
)
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)
TOKEN = 'a' * 64
GUEST_ID = 'guest-' + hashlib.sha256(TOKEN.encode()).hexdigest()[:16]


def _record(**overrides) -> PayerRecord:
    values = {
        'id': 7,
        'telegram_id': 555,
        'username': None,
        'first_name': None,
        'last_name': None,
        'email': None,
        'email_verified': False,
    }
    values.update(overrides)
    return PayerRecord(**values)


def _assert_filled(payer: PayerIdentity) -> None:
    for value in (payer.user_id, payer.user_name, payer.contact):
        assert isinstance(value, str)
        assert value.strip() == value and value
        assert len(value) <= PAYER_FIELD_MAX_LENGTH


# --- пользователь бота -------------------------------------------------------


def test_telegram_user_is_identified_by_telegram_id_and_username() -> None:
    payer = payer_from_user(_record(username='neo', first_name='Томас'))

    assert payer.user_id == '555'
    assert payer.user_name == '@neo'
    assert payer.contact == '555', 'MulenPay: Telegram ID, если подтверждённой почты нет'


def test_username_is_not_prefixed_twice() -> None:
    assert payer_from_user(_record(username='@neo')).user_name == '@neo'


def test_name_is_used_without_username() -> None:
    assert payer_from_user(_record(first_name='Иван', last_name='Петров')).user_name == 'Иван Петров'


def test_telegram_user_without_any_names_is_named_by_id() -> None:
    payer = payer_from_user(_record())

    assert payer.user_name == 'id555'
    _assert_filled(payer)


def test_verified_email_is_the_contact() -> None:
    payer = payer_from_user(_record(email='neo@example.com', email_verified=True))

    assert payer.contact == 'neo@example.com'


def test_email_user_is_identified_by_internal_id() -> None:
    payer = payer_from_user(_record(telegram_id=None, email='mail@example.com', email_verified=True))

    assert payer.user_id == 'user-7'
    assert payer.user_name == 'mail@example.com'
    assert payer.contact == 'mail@example.com'


def test_unverified_email_is_never_the_contact() -> None:
    """Адрес назначается до подтверждения, MulenPay фискализирует платёж — чек чужому ящику не нужен."""
    payer = payer_from_user(_record(telegram_id=None, email='typo@example.com', email_verified=False))

    assert payer.contact == 'user-7'
    assert payer.user_name == 'typo@example.com'


def test_broken_telegram_name_does_not_reach_the_provider() -> None:
    """Обрезанный эмодзи (одинокий суррогат) и управляющие символы из имени не уходят в запрос."""
    payer = payer_from_user(_record(first_name='Ива\ud83dн\x00', last_name='  '))

    assert payer.user_name == 'Иван'
    payer.user_name.encode('utf-8')


def test_whitespace_only_fields_fall_through() -> None:
    payer = payer_from_user(_record(username='  ', first_name='\n', email='   ', email_verified=True))

    assert payer.user_name == 'id555'
    assert payer.contact == '555'


def test_overlong_values_are_cut() -> None:
    payer = payer_from_user(_record(username='n' * 400, email='e' * 400 + '@x.ru', email_verified=True))

    _assert_filled(payer)


def test_platega_metadata_has_both_string_fields() -> None:
    metadata = payer_from_user(_record(username='neo')).platega_metadata()

    assert metadata == {'userId': '555', 'userName': '@neo'}


# --- гость лендинга ----------------------------------------------------------


def test_guest_with_email_contact() -> None:
    payer = payer_from_guest(TOKEN, contact_type='email', contact_value=' guest@example.com ')

    assert payer.user_id == GUEST_ID
    assert payer.user_name == 'guest@example.com'
    assert payer.contact == 'guest@example.com'


def test_guest_with_telegram_contact() -> None:
    payer = payer_from_guest(TOKEN, contact_type='telegram', contact_value='@someone')

    assert payer.user_name == '@someone'
    assert payer.contact == '@someone'


def test_guest_without_contact_is_still_filled() -> None:
    payer = payer_from_guest(TOKEN, contact_type=None, contact_value=None)

    assert payer.user_id == GUEST_ID
    assert payer.user_name == GUEST_ID
    assert payer.contact == GUEST_ID


# --- чтение из базы ----------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_user_payer_reads_the_user(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(User(id=7, telegram_id=555, username='neo', first_name='T', language='ru', status='active'))
        await db.commit()

        payer = await resolve_user_payer(db, 7)

    assert payer == PayerIdentity(user_id='555', user_name='@neo', contact='555')


@pytest.mark.asyncio
async def test_resolve_user_payer_without_the_user_is_still_filled(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        payer = await resolve_user_payer(db, 404)

    assert payer == PayerIdentity(user_id='user-404', user_name='user-404', contact='user-404')


@pytest.mark.asyncio
async def test_resolve_user_payer_survives_a_database_error() -> None:
    """Данные плательщика не имеют права сорвать создание платежа."""

    class _Broken:
        async def execute(self, *_args, **_kwargs):
            raise RuntimeError('БД недоступна')

    payer = await resolve_user_payer(_Broken(), 42)

    assert payer.user_id == 'user-42'
    _assert_filled(payer)


@pytest.mark.asyncio
async def test_resolve_guest_payer_reads_the_purchase(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(
            GuestPurchase(
                token=TOKEN,
                contact_type='email',
                contact_value='guest@example.com',
                amount_kopeks=10_000,
                period_days=30,
                payment_method='platega',
                status='pending',
            )
        )
        await db.commit()

        payer = await resolve_guest_payer(db, TOKEN)

    assert payer == PayerIdentity(user_id=GUEST_ID, user_name='guest@example.com', contact='guest@example.com')


@pytest.mark.asyncio
async def test_resolve_guest_payer_without_the_purchase_is_still_filled(monkeypatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        payer = await resolve_guest_payer(db, TOKEN)

    assert payer.user_id == GUEST_ID
    _assert_filled(payer)
