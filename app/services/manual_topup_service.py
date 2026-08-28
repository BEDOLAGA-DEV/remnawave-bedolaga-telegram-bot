"""Ручное пополнение баланса: то же самое, что платёж от шлюза, но инициированное человеком.

Пополнение через шлюз — это не просто «прибавить число к балансу». Следом идут
реферальная комиссия рефереру, отметка первого пополнения, уведомления
пользователю и админам, возобновление приостановленной суточной подписки и
автопокупка сохранённой корзины. Ручные же начисления (админка бота, кабинет,
веб-API) делали только первую часть — поэтому «поддержка начислила вручную» и
«деньги пришли от шлюза» оставляли аккаунт в РАЗНЫХ состояниях: суточная подписка
так и не включалась, корзина оставалась недокупленной, и человек возвращался в
поддержку со словами «деньги на балансе, а подписка не работает».

Здесь конвейер собран целиком, чтобы ручное пополнение было неотличимо от
настоящего, плюс добавлена идемпотентность: внешний вызывающий — в первую очередь
AI-агент поддержки — может безопасно повторить запрос после таймаута или сетевой
ошибки, не начислив деньги дважды.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from aiogram import Bot
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.transaction import (
    create_transaction,
    emit_transaction_side_effects,
    get_transaction_by_external_id,
)
from app.database.crud.user import get_user_by_id, lock_user_for_update
from app.database.models import PaymentMethod, Transaction, TransactionType, User


logger = structlog.get_logger(__name__)


# Пространство external_id для ручных начислений. Остальные ручные операции
# (админка бота, кабинет, корректировка через /balance) пишут external_id=NULL,
# поэтому префикс ни с чем не конфликтует, а уникальный индекс
# uq_transaction_external_id_method (external_id, payment_method) превращает ключ
# идемпотентности в защиту на уровне БД, а не только в проверку в коде.
MANUAL_TOPUP_EXTERNAL_ID_PREFIX = 'manual:'


class ManualTopupKeyConflict(Exception):
    """Ключ идемпотентности уже занят ДРУГОЙ операцией — другой суммой или другим юзером.

    Это ошибка вызывающего, а не безобидный ретрай, и молчать о ней нельзя: ответ
    «дубликат, всё в порядке» агент прочитал бы как успех и решил бы, что деньги ушли,
    хотя по этому запросу не ушло ничего. Особенно важно для чужого пользователя:
    ключи глобальны, и переиспользованный номер тикета иначе «зачислил» бы деньги
    на чей-то чужой счёт — в ответе, но не в реальности.
    """

    def __init__(self, transaction: Transaction) -> None:
        self.transaction = transaction
        super().__init__(
            f'idempotency key already used by transaction {transaction.id} '
            f'(user {transaction.user_id}, {transaction.amount_kopeks} kopeks)'
        )


@dataclass(slots=True)
class ManualTopupResult:
    """Итог ручного начисления.

    ``duplicate=True`` — запрос с таким ключом идемпотентности уже был обработан
    ранее: баланс НЕ менялся, ``transaction`` указывает на исходное начисление.
    """

    transaction: Transaction
    old_balance_kopeks: int
    new_balance_kopeks: int
    duplicate: bool


def build_manual_topup_external_id(idempotency_key: str) -> str:
    return f'{MANUAL_TOPUP_EXTERNAL_ID_PREFIX}{idempotency_key}'


def _duplicate_result(
    transaction: Transaction,
    balance_kopeks: int,
    *,
    user_pk: int,
    amount_kopeks: int,
) -> ManualTopupResult:
    if transaction.user_id != user_pk or transaction.amount_kopeks != amount_kopeks:
        raise ManualTopupKeyConflict(transaction)

    return ManualTopupResult(
        transaction=transaction,
        old_balance_kopeks=balance_kopeks,
        new_balance_kopeks=balance_kopeks,
        duplicate=True,
    )


async def credit_manual_topup(
    db: AsyncSession,
    user: User,
    amount_kopeks: int,
    *,
    description: str,
    idempotency_key: str | None = None,
    bot: Bot | None = None,
    notify_user: bool = True,
    apply_topup_bonuses: bool = True,
) -> ManualTopupResult:
    """Зачислить деньги на баланс так же, как это делает платёжный шлюз.

    ``idempotency_key`` — произвольная строка вызывающего (номер тикета, uuid попытки).
    Повторный вызов с тем же ключом ничего не начислит и вернёт исходную транзакцию
    с ``duplicate=True``.

    ``apply_topup_bonuses=False`` — начислить деньги, но не запускать реферальную
    комиссию и авто-действия (возобновление суточной подписки, автопокупка корзины).
    """
    if amount_kopeks <= 0:
        raise ValueError('amount_kopeks must be positive')

    external_id = build_manual_topup_external_id(idempotency_key) if idempotency_key else None

    if external_id:
        existing = await get_transaction_by_external_id(db, external_id, PaymentMethod.MANUAL)
        if existing is not None:
            return _duplicate_result(existing, user.balance_kopeks, user_pk=user.id, amount_kopeks=amount_kopeks)

    locked = await lock_user_for_update(db, user)

    # Повторная проверка уже ПОД блокировкой строки: между первой проверкой и
    # захватом строки параллельный запрос с тем же ключом мог успеть начислить.
    if external_id:
        existing = await get_transaction_by_external_id(db, external_id, PaymentMethod.MANUAL)
        if existing is not None:
            return _duplicate_result(existing, locked.balance_kopeks, user_pk=locked.id, amount_kopeks=amount_kopeks)

    user_pk = locked.id
    old_balance = locked.balance_kopeks
    was_first_topup = not locked.has_made_first_topup

    locked.balance_kopeks += amount_kopeks
    locked.updated_at = datetime.now(UTC)

    try:
        # commit=False — баланс и транзакция должны лечь ОДНИМ коммитом: иначе краш
        # между ними либо теряет деньги, либо занимает ключ идемпотентности
        # начислением, которого не было.
        transaction = await create_transaction(
            db=db,
            user_id=user_pk,
            type=TransactionType.DEPOSIT,
            amount_kopeks=amount_kopeks,
            description=description,
            payment_method=PaymentMethod.MANUAL,
            external_id=external_id,
            is_completed=True,
            commit=False,
        )
        await db.commit()
    except IntegrityError:
        # Гонка по ключу идемпотентности: параллельный запрос закоммитился первым.
        # Уникальный индекс — последний рубеж на случай, когда блокировка строки не
        # спасла (пользователь прочитан другой сессией, реплика, ретрай пула).
        # INSERT уходит уже на flush внутри create_transaction, поэтому ловим ошибку
        # вокруг обоих вызовов, а не только вокруг commit.
        await db.rollback()
        if external_id:
            existing = await get_transaction_by_external_id(db, external_id, PaymentMethod.MANUAL)
            if existing is not None:
                fresh = await get_user_by_id(db, user_pk)
                return _duplicate_result(
                    existing,
                    fresh.balance_kopeks if fresh else old_balance,
                    user_pk=user_pk,
                    amount_kopeks=amount_kopeks,
                )
        raise

    await db.refresh(locked)
    new_balance = locked.balance_kopeks

    logger.info(
        '💰 Ручное пополнение баланса',
        user_id=user_pk,
        telegram_id=locked.telegram_id,
        amount_kopeks=amount_kopeks,
        old_balance=old_balance,
        new_balance=new_balance,
        transaction_id=transaction.id,
        external_id=external_id,
    )

    # Деньги уже зафиксированы. Дальше идут только побочные эффекты, и ни один из них
    # не имеет права превратить успешное зачисление в ошибку: вызывающий (агент
    # поддержки) прочитал бы её как «не начислилось» и повторил бы запрос — а без
    # ключа идемпотентности это второе списание из кассы.
    try:
        await emit_transaction_side_effects(
            db,
            transaction,
            amount_kopeks=amount_kopeks,
            user_id=user_pk,
            type=TransactionType.DEPOSIT,
            payment_method=PaymentMethod.MANUAL,
            external_id=external_id,
            description=description,
        )

        if apply_topup_bonuses:
            await _apply_referral_topup(db, locked, amount_kopeks, was_first_topup=was_first_topup, bot=bot)

        await _notify_admins(db, locked, transaction, old_balance, bot=bot)

        if notify_user:
            await _notify_user(locked, amount_kopeks, transaction, bot=bot)

        if apply_topup_bonuses:
            from app.services.payment.common import send_cart_notification_after_topup

            # notify_email=False — письмо для юзеров без Telegram уже ушло из
            # _notify_user, под общим гейтом notify_user.
            await send_cart_notification_after_topup(locked, amount_kopeks, db, bot, notify_email=False)
    except Exception as error:
        logger.error(
            'Ошибка пост-обработки ручного пополнения (деньги зачислены)',
            user_id=user_pk,
            transaction_id=transaction.id,
            error=error,
            exc_info=True,
        )

    return ManualTopupResult(
        transaction=transaction,
        old_balance_kopeks=old_balance,
        new_balance_kopeks=new_balance,
        duplicate=False,
    )


async def _apply_referral_topup(
    db: AsyncSession,
    user: User,
    amount_kopeks: int,
    *,
    was_first_topup: bool,
    bot: Bot | None,
) -> None:
    try:
        from app.services.referral_service import process_referral_topup

        await process_referral_topup(db, user.id, amount_kopeks, bot)
    except Exception as error:
        logger.error('Ошибка реферальной обработки ручного пополнения', user_id=user.id, error=error)

    # Для рефералов флаг «первое пополнение» выставляет сам process_referral_topup —
    # там он завязан на выдачу отложенного бонуса. Здесь добираем только тех, у кого
    # реферера нет: иначе бонус реферера остался бы невыданным навсегда.
    if was_first_topup and not user.has_made_first_topup and not user.referred_by_id:
        try:
            user.has_made_first_topup = True
            await db.commit()
            await db.refresh(user)
        except Exception as error:
            await db.rollback()
            logger.error('Не удалось отметить первое пополнение', user_id=user.id, error=error)


async def _notify_admins(
    db: AsyncSession,
    user: User,
    transaction: Transaction,
    old_balance: int,
    *,
    bot: Bot | None,
) -> None:
    if bot is None:
        return

    try:
        from app.services.admin_notification_service import AdminNotificationService
        from app.utils.user_utils import format_referrer_info

        await AdminNotificationService(bot).send_balance_topup_notification(
            user,
            transaction,
            old_balance,
            topup_status='🧾 Ручное пополнение',
            referrer_info=format_referrer_info(user),
            subscription=getattr(user, 'subscription', None),
            promo_group=user.get_primary_promo_group(),
            db=db,
        )
    except Exception as error:
        logger.error('Ошибка админ-уведомления о ручном пополнении', user_id=user.id, error=error)


async def _notify_user(
    user: User,
    amount_kopeks: int,
    transaction: Transaction,
    *,
    bot: Bot | None,
) -> None:
    if not settings.is_notifications_enabled():
        return

    if bot is not None and user.telegram_id:
        keyboard = None
        try:
            from app.services.payment.common import PaymentCommonMixin

            keyboard = await PaymentCommonMixin().build_topup_success_keyboard(user)
        except Exception as error:
            logger.warning('Не удалось собрать клавиатуру уведомления о пополнении', user_id=user.id, error=error)

        try:
            await bot.send_message(
                user.telegram_id,
                (
                    '✅ <b>Баланс пополнен</b>\n\n'
                    f'💰 Сумма: {settings.format_price(amount_kopeks)}\n'
                    f'💳 Текущий баланс: {settings.format_price(user.balance_kopeks)}\n'
                    f'🆔 Транзакция: {transaction.id}'
                ),
                parse_mode='HTML',
                reply_markup=keyboard,
            )
        except Exception as error:
            logger.error('Не удалось уведомить пользователя о ручном пополнении', user_id=user.id, error=error)

    # Юзеры без Telegram (email-only) — отдельным каналом, как в общем пост-топап пути.
    try:
        from app.services.payment.common import notify_email_user_topup

        await notify_email_user_topup(user, amount_kopeks)
    except Exception as error:
        logger.error('Не удалось отправить письмо о ручном пополнении', user_id=user.id, error=error)
