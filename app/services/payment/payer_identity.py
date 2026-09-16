"""Данные плательщика для шлюзов, которые требуют их в каждом платеже.

Platega (docs.platega.io, «Создание платежной ссылки с заданным методом»):
``metadata.userId`` и ``metadata.userName`` — строки, обязательные для части
категорий магазинов. «Отсутствие ``metadata.userId`` при наличии требования
отключает антифрод-защиту и может привести к отключению магазина».

MulenPay (письмо провайдера, 2026-09-15): поле ``client`` «требуется заполнять
почтой, телефоном или ТГ ид и т.п.». Документация формата не задаёт — в примере
только почта.

Все поля всегда непустые: у любого плательщика есть хотя бы внутренний id —
Telegram ID, ``user-<id>`` у пользователей без Telegram, ``guest-<хеш токена>`` у
гостей лендинга. Получение данных не имеет права сорвать платёж: любая ошибка
чтения даёт плательщика по одному id.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)

#: Предел выбран защитно (шлюзы длину не документируют) и совпадает с колонками
#: users.email / users.username, чтобы обрезка была недостижима на живых данных.
PAYER_FIELD_MAX_LENGTH = 255


@dataclass(frozen=True, slots=True)
class PayerIdentity:
    """Кто платит, в трёх видах, которые спрашивают шлюзы."""

    #: Стабильный идентификатор в нашей системе (Platega ``userId``).
    user_id: str
    #: Как человека назвать (Platega ``userName``).
    user_name: str
    #: Как с ним связаться (MulenPay ``client``).
    contact: str

    def platega_metadata(self) -> dict[str, str]:
        return {'userId': self.user_id, 'userName': self.user_name}


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
    )


def guest_payer_id(purchase_token: str) -> str:
    """Идентификатор гостя: сам токен покупки наружу не отдаём — по нему забирают подписку."""
    return 'guest-' + hashlib.sha256(purchase_token.encode()).hexdigest()[:16]


def payer_from_guest(purchase_token: str, *, contact_type: str | None, contact_value: str | None) -> PayerIdentity:
    """Гость лендинга: аккаунта нет, есть контакт, который он оставил (почта или Telegram)."""
    user_id = guest_payer_id(purchase_token)
    contact = _clean(contact_value) if contact_type in ('email', 'telegram') else None
    return PayerIdentity(user_id=user_id, user_name=_first(contact, user_id), contact=_first(contact, user_id))


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


async def resolve_guest_payer(db: AsyncSession, purchase_token: str) -> PayerIdentity:
    """Плательщик-гость по токену покупки лендинга."""
    try:
        from app.database.crud.landing import get_purchase_by_token

        purchase = await get_purchase_by_token(db, purchase_token)
    except Exception as error:
        logger.warning('Не удалось прочитать гостевую покупку — отправляем только id', error=str(error))
        purchase = None
    return payer_from_guest(
        purchase_token,
        contact_type=getattr(purchase, 'contact_type', None),
        contact_value=getattr(purchase, 'contact_value', None),
    )
