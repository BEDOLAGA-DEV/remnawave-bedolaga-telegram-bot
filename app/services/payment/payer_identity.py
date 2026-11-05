"""Данные плательщика для шлюзов, которые требуют их в каждом платеже.

Platega (docs.platega.io, обновление 2026-10-30): ``metadata.user_email``
обязателен в каждом платеже и должен обозначать покупателя, а не заказ.
``userId`` обязателен для отдельных категорий; ``userName`` дополнительный.

MulenPay (письмо провайдера, 2026-09-15): поле ``client`` «требуется заполнять
почтой, телефоном или ТГ ид и т.п.». Документация формата не задаёт — в примере
только почта.

Контакт MulenPay сохраняет прежние правила и резервный идентификатор при
ошибке БД. Platega требует отдельный email-идентификатор: при отсутствии
достоверных данных запрос к провайдеру не отправляем.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass

import structlog
from pydantic import EmailStr, TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)

#: Предел выбран защитно (шлюзы длину не документируют) и совпадает с колонками
#: users.email / users.username, чтобы обрезка была недостижима на живых данных.
PAYER_FIELD_MAX_LENGTH = 255
_EMAIL_ADAPTER = TypeAdapter(EmailStr)


@dataclass(frozen=True, slots=True)
class PayerIdentity:
    """Кто платит, в трёх видах, которые спрашивают шлюзы."""

    #: Стабильный идентификатор в нашей системе (Platega ``userId``).
    user_id: str
    #: Как человека назвать (Platega ``userName``).
    user_name: str
    #: Как с ним связаться (MulenPay ``client``).
    contact: str

    #: Только идентификация Platega; синтетический адрес нельзя использовать для чеков.
    platega_email: str | None = None

    def platega_metadata(self) -> dict[str, str]:
        email = self.platega_email
        if not email or any(c.isspace() or c in '<>' or unicodedata.category(c) in ('Cc', 'Cf', 'Cs') for c in email):
            raise ValueError('Platega: отсутствует корректный идентификатор плательщика')
        try:
            _EMAIL_ADAPTER.validate_python(email)
        except ValueError:
            # Не включать контакт в ошибку: она может попасть в журнал.
            raise ValueError('Platega: некорректный email плательщика') from None
        return {'userId': self.user_id, 'userName': self.user_name, 'user_email': email}


@dataclass(frozen=True, slots=True)
class PayerRecord:
    """Поля пользователя, из которых собирается плательщик."""

    id: int
    telegram_id: int | None
    username: str | None
    first_name: str | None
    last_name: str | None
    email: str | None
    email_verified: bool


def _clean(value: object) -> str | None:
    """Строка без обрезанных эмодзи и управляющих символов; пустая — ``None``.

    Имена из Telegram бывают с одиноким суррогатом (обрезанный эмодзи): в JSON он
    уходит экранированной половинкой символа, и строгий разборщик шлюза отбивает
    весь платёж.
    """
    if value is None:
        return None
    text = ''.join(
        char
        for char in str(value)
        if not 0xD800 <= ord(char) <= 0xDFFF and unicodedata.category(char) not in ('Cc', 'Cf')
    )
    text = ' '.join(text.split())
    return text[:PAYER_FIELD_MAX_LENGTH] or None


def _first(*candidates: str | None) -> str:
    return next(candidate for candidate in candidates if candidate)


def _by_id(identifier: str) -> PayerIdentity:
    return PayerIdentity(user_id=identifier, user_name=identifier, contact=identifier)


def payer_from_user(record: PayerRecord) -> PayerIdentity:
    """Плательщик-пользователь.

    ``userName``: @username → имя → почта → id<telegram_id>. Контакт: только
    подтверждённая почта (адрес назначается до подтверждения, а MulenPay
    фискализирует платёж — чек чужому ящику не нужен), иначе Telegram ID.
    """
    telegram_id = str(record.telegram_id) if record.telegram_id else None
    user_id = telegram_id or f'user-{record.id}'
    username = _clean((record.username or '').lstrip('@'))
    full_name = _clean(' '.join(part for part in (record.first_name, record.last_name) if part))
    email = _clean(record.email)
    return PayerIdentity(
        user_id=user_id,
        user_name=_first(
            f'@{username}'[:PAYER_FIELD_MAX_LENGTH] if username else None,
            full_name,
            email,
            f'id{telegram_id}' if telegram_id else None,
            user_id,
        ),
        contact=_first(email if record.email_verified else None, telegram_id, user_id),
        # Telegram ID не меняется при смене username/привязке почты. Для web-only
        # аккаунтов передаём исходную почту, без обрезки или исправления адреса.
        platega_email=f'{telegram_id}@telegram.org' if telegram_id else (record.email or f'{user_id}@gmail.com'),
    )


def guest_payer_id(purchase_token: str) -> str:
    """Идентификатор гостя: сам токен покупки наружу не отдаём — по нему забирают подписку."""
    return 'guest-' + hashlib.sha256(purchase_token.encode()).hexdigest()[:16]


def payer_from_guest(purchase_token: str, *, contact_type: str | None, contact_value: str | None) -> PayerIdentity:
    """Гость лендинга: аккаунта нет, есть контакт, который он оставил (почта или Telegram)."""
    user_id = guest_payer_id(purchase_token)
    contact = _clean(contact_value) if contact_type in ('email', 'telegram') else None
    platega_email = None
    if contact_type == 'email' and contact_value:
        platega_email = contact_value
        user_id = 'guest-' + hashlib.sha256(('email:' + contact_value).encode(errors='replace')).hexdigest()[:32]
    # Гостевой username не подтверждает постоянный Telegram ID: его можно
    # сменить/передать. Для Platega требуется email либо аккаунт покупателя.
    return PayerIdentity(
        user_id=user_id,
        user_name=_first(contact, user_id),
        contact=_first(contact, user_id),
        platega_email=platega_email,
    )


async def resolve_user_payer(db: AsyncSession, user_id: int) -> PayerIdentity:
    """Плательщик по id пользователя. Точечный select — это горячий путь оплаты."""
    from app.database.models import User

    try:
        result = await db.execute(
            select(
                User.id,
                User.telegram_id,
                User.username,
                User.first_name,
                User.last_name,
                User.email,
                User.email_verified,
            ).where(User.id == user_id)
        )
        row = result.first()
    except Exception as error:
        logger.warning('Не удалось прочитать плательщика — отправляем только id', user_id=user_id, error=str(error))
        row = None
    if row is None:
        return _by_id(f'user-{user_id}')
    return payer_from_user(
        PayerRecord(
            id=row.id,
            telegram_id=row.telegram_id,
            username=row.username,
            first_name=row.first_name,
            last_name=row.last_name,
            email=row.email,
            email_verified=bool(row.email_verified),
        )
    )


async def resolve_guest_payer(db: AsyncSession, purchase_token: str, *, for_platega: bool = False) -> PayerIdentity:
    """Плательщик-гость по токену покупки лендинга."""
    try:
        from app.database.crud.landing import get_purchase_by_token

        purchase = await get_purchase_by_token(db, purchase_token)
    except Exception as error:
        logger.warning('Не удалось прочитать гостевую покупку — отправляем только id', error=str(error))
        purchase = None
    # У подарка user_id принадлежит получателю. Идентификатор плательщика —
    # исключительно buyer_user_id; он должен совпадать с обычным пополнением.
    if for_platega and purchase is not None and purchase.buyer_user_id is not None:
        return await resolve_user_payer(db, purchase.buyer_user_id)
    return payer_from_guest(
        purchase_token,
        contact_type=getattr(purchase, 'contact_type', None),
        contact_value=getattr(purchase, 'contact_value', None),
    )
