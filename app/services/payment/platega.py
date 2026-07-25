"""Mixin для интеграции платежей Platega."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, Subscription, TransactionType
from app.services.platega_service import PlategaService
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


def _parse_next_charge(raw: Any) -> datetime | None:
    """Парсит ``NextChargeAt`` коллбека Platega в aware datetime.

    Значение приходит с суффиксом ``Z`` (UTC), который ``datetime.fromisoformat``
    понимает только после замены на ``+00:00``. Отсутствие поля или невалидный
    формат не фатальны — следующее списание останется без даты до следующего
    коллбека.
    """
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class PlategaPaymentMixin:
    """Логика создания и обработки платежей Platega."""

    _SUCCESS_STATUSES = {'CONFIRMED'}
    _FAILED_STATUSES = {'FAILED', 'CANCELED', 'EXPIRED'}
    _PENDING_STATUSES = {'PENDING', 'INPROGRESS'}

    async def create_platega_payment(
        self,
        db: AsyncSession,
        *,
        user_id: int | None,
        amount_kopeks: int,
        description: str,
        language: str,
        payment_method_code: int,
        return_url: str | None = None,
        failed_url: str | None = None,
    ) -> dict[str, Any] | None:
        service: PlategaService | None = getattr(self, 'platega_service', None)
        if not service or not service.is_configured:
            logger.error('Platega сервис не инициализирован')
            return None

        if amount_kopeks < settings.PLATEGA_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'Сумма Platega меньше минимальной: <',
                amount_kopeks=amount_kopeks,
                PLATEGA_MIN_AMOUNT_KOPEKS=settings.PLATEGA_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.PLATEGA_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'Сумма Platega больше максимальной: >',
                amount_kopeks=amount_kopeks,
                PLATEGA_MAX_AMOUNT_KOPEKS=settings.PLATEGA_MAX_AMOUNT_KOPEKS,
            )
            return None

        correlation_id = uuid.uuid4().hex
        payload_token = f'platega:{correlation_id}'

        amount_value = amount_kopeks / 100

        effective_return_url = return_url or settings.get_platega_return_url()
        effective_failed_url = failed_url or settings.get_platega_failed_url()

        try:
            response = await service.create_payment(
                payment_method=payment_method_code,
                amount=amount_value,
                currency=settings.PLATEGA_CURRENCY,
                description=description,
                return_url=effective_return_url,
                failed_url=effective_failed_url,
                payload=payload_token,
            )
        except Exception as error:  # pragma: no cover - network errors
            logger.exception('Ошибка Platega при создании платежа', error=error)
            return None

        if not response:
            logger.error('Platega вернул пустой ответ при создании платежа')
            return None

        transaction_id = response.get('transactionId') or response.get('id')
        redirect_url = PlategaService.parse_redirect_url(response)
        status = str(response.get('status') or 'PENDING').upper()
        expires_at = PlategaService.parse_expires_at(response.get('expiresIn'))

        metadata = {
            'raw_response': response,
            'language': language,
            'selected_method': payment_method_code,
        }

        payment_module = import_module('app.services.payment_service')

        payment = await payment_module.create_platega_payment(
            db,
            user_id=user_id,
            amount_kopeks=amount_kopeks,
            currency=settings.PLATEGA_CURRENCY,
            description=description,
            status=status,
            payment_method_code=payment_method_code,
            correlation_id=correlation_id,
            platega_transaction_id=transaction_id,
            redirect_url=redirect_url,
            return_url=effective_return_url,
            failed_url=effective_failed_url,
            payload=payload_token,
            metadata=metadata,
            expires_at=expires_at,
        )

        logger.info(
            'Создан Platega платеж для пользователя (метод , сумма ₽)',
            transaction_id=transaction_id or payment.id,
            user_id=user_id,
            payment_method_code=payment_method_code,
            amount_value=amount_value,
        )

        return {
            'local_payment_id': payment.id,
            'transaction_id': transaction_id,
            'redirect_url': redirect_url,
            'status': status,
            'expires_at': expires_at,
            'correlation_id': correlation_id,
            'payload': payload_token,
        }

    async def create_platega_sbp_subscription(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        subscription: Any,
        tariff: Any,
    ) -> dict[str, Any]:
        """Создаёт рекуррентную СБП-подписку Platega, привязанную к тарифу подписки.

        Каденс списания выбирается по той же иерархии периодов, что и
        balance-autopay (см. ``monitoring_service._process_autopayments``):
        выбор пользователя → глобальный дефолт → самый короткий период тарифа
        → жёсткий fallback 30 дней. Сумма — базовая цена тарифа за период
        списания (``get_purchasable_price_for_period``), а не
        ``calculate_renewal_price`` — мы намеренно не замораживаем временные
        промо-скидки в повторяющемся списании.

        После создания подписки выключает ``subscription.autopay_enabled``:
        Platega-рекуррент и баланс-автосписание — взаимоисключающие движки
        продления одной подписки.
        """
        # Ленивый импорт: monitoring_service импортирует платёжный слой,
        # прямой импорт на уровне модуля создал бы циклическую зависимость.
        from app.database.crud import platega_subscription as sub_crud
        from app.services.monitoring_service import resolve_autopay_period_candidate
        from app.services.platega_recurrent import resolve_platega_interval

        existing = await sub_crud.get_active_platega_subscription_by_subscription(db, subscription.id)
        if existing:
            # Идемпотентный повтор обязан ТОЖЕ восстановить взаимоисключение:
            # если balance-autopay успели включить обратно (например, пока фича
            # была выключена), оба движка продления остались бы взведёнными.
            if getattr(subscription, 'autopay_enabled', False):
                subscription.autopay_enabled = False
                await db.commit()
            return {
                'local_id': existing.id,
                'platega_subscription_id': existing.platega_subscription_id,
                'redirect_url': existing.redirect_url,
                'status': existing.status,
            }

        period_days = (
            resolve_autopay_period_candidate(getattr(subscription, 'autopay_period_days', None), tariff)
            or resolve_autopay_period_candidate(getattr(settings, 'DEFAULT_AUTOPAY_PERIOD_DAYS', 0), tariff)
            or (tariff.get_shortest_period() if tariff else None)
            or 30
        )
        is_daily = bool(getattr(tariff, 'is_daily', False))
        interval, charge_days = resolve_platega_interval(period_days, is_daily)

        amount_kopeks = tariff.get_purchasable_price_for_period(charge_days)
        # Нулевая цена отклоняется наравне с отсутствующей: подписка Platega на
        # 0 ₽ бессмысленна и вела бы к пустым регулярным «списаниям».
        if not amount_kopeks:
            raise ValueError(f'Тариф не имеет цены за период {charge_days} дней — СБП-автопродление недоступно')

        response = await self.platega_service.create_subscription(
            amount=amount_kopeks / 100,
            currency=settings.PLATEGA_CURRENCY,
            interval=interval,
            description=getattr(tariff, 'name', None) or 'Подписка',
        )

        platega_id = (response or {}).get('transactionId')
        if not platega_id:
            raise RuntimeError('Platega не вернула transactionId при создании подписки')
        redirect_url = (response or {}).get('redirect')

        try:
            record = await sub_crud.create_platega_subscription(
                db,
                user_id=user_id,
                subscription_id=subscription.id,
                tariff_id=getattr(tariff, 'id', None),
                interval=interval,
                charge_days=charge_days,
                amount_kopeks=amount_kopeks,
                redirect_url=redirect_url,
                platega_subscription_id=platega_id,
            )
        except IntegrityError:
            # Гонка конкурентного enable: оба запроса прошли идемпотентную
            # проверку выше, победитель вставился первым, нас отбил partial
            # unique uq_platega_subscriptions_alive. Наша remote-подписка уже
            # создана и осталась бы сиротой — отменяем best-effort и
            # возвращаем запись победителя.
            await db.rollback()
            try:
                await self.platega_service.cancel_subscription(platega_id)
            except Exception as orphan_error:  # pragma: no cover - best-effort
                logger.warning(
                    'Не удалось отменить осиротевшую Platega-подписку после гонки enable',
                    platega_subscription_id=platega_id,
                    error=str(orphan_error),
                )
            winner = await sub_crud.get_active_platega_subscription_by_subscription(db, subscription.id)
            if winner is None:  # pragma: no cover - победитель успел отмениться
                raise RuntimeError('Platega-подписка не создана: конкурентная гонка enable') from None
            logger.info(
                'Гонка конкурентного enable СБП: возвращаю запись победителя',
                subscription_id=subscription.id,
                winner_local_id=winner.id,
                orphan_platega_id=platega_id,
            )
            return {
                'local_id': winner.id,
                'platega_subscription_id': winner.platega_subscription_id,
                'redirect_url': winner.redirect_url,
                'status': winner.status,
            }

        # Взаимоисключение: у подписки может быть только один движок продления.
        subscription.autopay_enabled = False
        await db.commit()

        return {
            'local_id': record.id,
            'platega_subscription_id': platega_id,
            'redirect_url': redirect_url,
            'status': (response or {}).get('status', 'PENDING'),
        }

    async def cancel_platega_sbp_subscription(
        self,
        db: AsyncSession,
        *,
        local_id: int,
        commit: bool = True,
    ) -> bool:
        """Отменяет одну СБП-подписку Platega по локальному id записи.

        Идемпотентна: уже отменённая запись возвращает ``True`` без обращения
        к Platega API. Сама отмена на стороне Platega — best-effort: сетевая
        ошибка логируется, но не мешает пометить запись ``CANCELLED``
        локально — иначе временная недоступность Platega блокировала бы
        отмену навсегда, а повторная отмена уже отменённой Platega-подписки
        безопасна (идемпотентна на их стороне), так что рассинхрон не страшен.
        """
        from app.database.crud import platega_subscription as sub_crud

        record = await sub_crud.get_platega_subscription_by_id(db, local_id)
        if not record:
            return False

        if record.status == 'CANCELLED':
            return True

        if record.platega_subscription_id:
            try:
                result = await self.platega_service.cancel_subscription(record.platega_subscription_id)
                if result is None:
                    # _request сам глотает HTTP-ошибки и возвращает None — сюда
                    # попадает КАЖДЫЙ реальный сбой провайдера. Локально запись
                    # всё равно станет CANCELLED, а reconciler повторит удалённую
                    # отмену (свип CANCELLED-записей), чтобы Platega не продолжила
                    # списывать по «отменённой» у нас подписке.
                    logger.warning(
                        'Отмена Platega-подписки не подтверждена провайдером — reconciler повторит',
                        platega_subscription_id=record.platega_subscription_id,
                    )
            except Exception as error:  # pragma: no cover - network errors
                logger.warning(
                    'Не удалось отменить Platega-подписку на стороне провайдера',
                    platega_subscription_id=record.platega_subscription_id,
                    error=str(error),
                )

        record.status = 'CANCELLED'
        if commit:
            await db.commit()
        else:
            # commit=False — вызывающий держит собственную транзакцию (например,
            # мерж аккаунтов с flush-секвенированием): CANCELLED войдёт в неё и
            # закоммитится/откатится вместе с остальным. При откате remote уже
            # отменён — reconciler добьёт локальный статус по remote-состоянию.
            await db.flush()
        return True

    async def cancel_platega_recurring_for_subscription(
        self,
        db: AsyncSession,
        subscription_id: int,
        *,
        commit: bool = True,
    ) -> None:
        """Best-effort отмена активной СБП-подписки Platega, привязанной к
        ``subscription_id``.

        Вызывается из путей удаления/замены подписки (Task 11) — никогда не
        должна бросать исключение и блокировать удаление сбоем Platega.
        """
        from app.database.crud import platega_subscription as sub_crud

        try:
            record = await sub_crud.get_active_platega_subscription_by_subscription(db, subscription_id)
            if not record:
                return
            await self.cancel_platega_sbp_subscription(db, local_id=record.id, commit=commit)
        except Exception as error:  # pragma: no cover - best-effort cleanup
            logger.warning(
                'Не удалось отменить СБП-автопродление Platega по подписке',
                subscription_id=subscription_id,
                error=str(error),
            )

    async def process_platega_subscription_callback(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> None:
        """Обрабатывает коллбек Platega по рекуррентной СБП-подписке.

        Один и тот же PascalCase-пейлоад Platega шлёт и на списание (charge), и
        на смену статуса самой подписки — маршрутизация обоих коллбеков сюда
        сделана в Task 9. Баланс пользователя НЕ трогается: продление —
        прямой вызов ``Subscription.extend_subscription``, а не начисление
        суммы платежа на баланс (в отличие от balance-autopay).

        Идемпотентность успешных списаний — по паре
        ``(platega_subscription_id, charge Id)`` через сохранённый
        ``last_charge_external_id``: повторная доставка того же коллбека
        (ретрай Platega) не должна продлевать подписку дважды.
        """
        from app.database.crud import platega_subscription as sub_crud
        from app.services import platega_recurrent as pr

        status = payload.get('Status')
        platega_id = payload.get('SubscriptionId')
        charge_id = payload.get('Id')

        if not platega_id:
            logger.warning('Platega subscription callback без SubscriptionId', status=status)
            return

        found = await sub_crud.get_platega_subscription_by_platega_id(db, platega_id)
        if not found:
            logger.warning(
                'Platega subscription callback: подписка не найдена',
                platega_subscription_id=platega_id,
                status=status,
            )
            return

        # Блокируем строку перед изменением статуса/счётчиков — конкурентные
        # коллбеки (ретрай Platega) не должны продлевать/учитывать дважды.
        record = await sub_crud.get_platega_subscription_by_id_for_update(db, found.id)
        if not record:
            logger.warning(
                'Platega subscription callback: запись исчезла после блокировки',
                platega_subscription_id=platega_id,
                status=status,
            )
            return

        if status == pr.SUB_ACTIVATED:
            record.status = 'ACTIVE'
            await db.commit()
            await self._notify_sbp_recurring(db, record, 'activated')
            return

        if status in pr.CHARGE_SUCCESS:
            if not charge_id:
                # CONFIRMED без Id доверять нельзя: без id идемпотентность ниже
                # не сработает, и каждый повтор такого коллбека продлевал бы
                # подписку заново. Поэтому не продлеваем, а просто выходим.
                logger.warning(
                    'Platega subscription callback: CONFIRMED без charge Id',
                    platega_subscription_id=platega_id,
                )
                return

            if record.last_charge_external_id == charge_id:
                logger.info(
                    'Platega subscription callback: списание уже обработано',
                    platega_subscription_id=platega_id,
                    charge_id=charge_id,
                )
                return

            # Полноисторийная идемпотентность: last_charge_external_id хранит
            # только ПОСЛЕДНИЙ charge Id, поэтому поздний ределивери старого
            # списания (N после N+1) прошёл бы мимо быстрой проверки выше и
            # продлил бы подписку второй раз. Каждый обработанный чардж уже
            # записан в transactions.external_id — сверяемся с ним.
            from sqlalchemy import select as sa_select

            from app.database.models import Transaction

            duplicate_tx = (
                await db.execute(
                    sa_select(Transaction.id).where(
                        Transaction.external_id == charge_id,
                        Transaction.payment_method == PaymentMethod.PLATEGA.value,
                    )
                )
            ).scalar_one_or_none()
            if duplicate_tx is not None:
                logger.info(
                    'Platega subscription callback: поздний ределивери старого списания',
                    platega_subscription_id=platega_id,
                    charge_id=charge_id,
                )
                return

            from app.database.crud.transaction import create_transaction

            subscription = await db.get(Subscription, record.subscription_id)
            if subscription is None:
                # Не отмечаем charge_id/счётчики — реального продления не было,
                # репортить успех нельзя. Практически недостижимо (CASCADE FK на
                # subscription_id сносит и саму запись record), но на случай гонки
                # молча притворяться успехом хуже, чем залогировать и выйти.
                logger.error(
                    'Platega subscription callback: Subscription не найдена для продления',
                    subscription_id=record.subscription_id,
                    platega_subscription_id=platega_id,
                )
                return

            subscription.extend_subscription(record.charge_days)

            # Списание по локально ОТМЕНЁННОЙ записи = удалённая отмена не
            # прошла (сбой Platega в момент cancel). Деньги взяты — продлеваем
            # честно, но запись НЕ воскрешаем в ACTIVE (иначе отмена юзера
            # молча стирается и цикл продолжается вечно) и тут же повторяем
            # удалённую отмену.
            was_cancelled = record.status == 'CANCELLED'

            record.last_charge_external_id = charge_id
            if not was_cancelled:
                record.status = 'ACTIVE'
            record.last_charge_at = datetime.now(UTC)
            record.charges_success += 1
            record.next_charge_at = _parse_next_charge(payload.get('NextChargeAt'))

            tx = await create_transaction(
                db,
                user_id=record.user_id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                amount_kopeks=record.amount_kopeks,
                description='СБП-автопродление Platega',
                payment_method=PaymentMethod.PLATEGA,
                external_id=charge_id,
                commit=False,
            )

            await db.commit()

            # Emit deferred side-effects after the atomic commit — mirrors
            # _finalize_platega_payment's DEPOSIT flow. Fires transaction.created,
            # promo-group auto-assignment and referral-contest tracking; never
            # raises (swallows its own exceptions), so no extra guarding needed.
            from app.database.crud.transaction import emit_transaction_side_effects

            await emit_transaction_side_effects(
                db,
                tx,
                amount_kopeks=record.amount_kopeks,
                user_id=record.user_id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                payment_method=PaymentMethod.PLATEGA,
                external_id=charge_id,
                description='СБП-автопродление Platega',
            )

            if was_cancelled:
                logger.error(
                    'Platega списала по отменённой СБП-подписке — повторяю удалённую отмену',
                    platega_subscription_id=platega_id,
                    charge_id=charge_id,
                )
                if record.platega_subscription_id:
                    try:
                        await self.platega_service.cancel_subscription(record.platega_subscription_id)
                    except Exception as recancel_error:  # pragma: no cover - best-effort
                        logger.warning(
                            'Повторная отмена Platega-подписки не удалась',
                            platega_subscription_id=record.platega_subscription_id,
                            error=str(recancel_error),
                        )

            await self._notify_sbp_recurring(db, record, 'confirmed')

            # Синк панели RemnaWave — лучшее-усилие, намеренно ПОСЛЕДНИЙ шаг
            # ветки: продление уже закоммичено в БД, так же поступает
            # balance-autopay (см. monitoring_service._process_autopayments,
            # комментарий "Синк панели — лучшее-усилие"). Без этого шага панель
            # продолжает отдавать пользователю СТАРЫЙ end_date — бот говорит
            # "продлено", а VPN всё равно обрывается по старой дате истечения.
            # Порядок важен: при сбое update_remnawave_user делает db.rollback()
            # изнутри, который экспайрит ВСЕ атрибуты объектов сессии (включая
            # PK) — если синк стоит раньше emit_transaction_side_effects/notify,
            # их чтения record.amount_kopeks/record.user_id падают
            # MissingGreenlet вместо тихого best-effort. Поэтому синк — последним,
            # а id для лога берём заранее, до вызова, чтобы сам except не упал.
            subscription_id_for_log = subscription.id
            try:
                from app.services.subscription_service import SubscriptionService

                await SubscriptionService().update_remnawave_user(
                    db,
                    subscription,
                    reset_traffic=settings.RESET_TRAFFIC_ON_PAYMENT,
                    reset_reason='СБП-автопродление',
                )
            except Exception as sync_error:  # best-effort: продление уже в БД, не откатываем
                logger.warning(
                    'Синк панели после СБП-автопродления не удался',
                    error=str(sync_error),
                    subscription_id=subscription_id_for_log,
                )
            return

        if status in pr.CHARGE_FAILED:
            record.status = 'PAST_DUE'
            record.charges_failed += 1
            await db.commit()
            await self._notify_sbp_recurring(db, record, 'failed')
            return

        if status == pr.SUB_PAST_DUE:
            record.status = 'PAST_DUE'
            await db.commit()
            await self._notify_sbp_recurring(db, record, 'past_due')
            return

        if status == pr.SUB_CANCELLED:
            record.status = 'CANCELLED'
            await db.commit()
            await self._notify_sbp_recurring(db, record, 'cancelled')
            return

        if status == pr.SUB_FAILED:
            record.status = 'FAILED'
            await db.commit()
            await self._notify_sbp_recurring(db, record, 'failed')
            return

        logger.warning(
            'Platega subscription callback: неизвестный статус',
            status=status,
            platega_subscription_id=platega_id,
        )

    async def _notify_sbp_recurring(
        self,
        db: AsyncSession,
        record: Any,
        kind: str,
    ) -> None:
        """Best-effort уведомление пользователя о событии СБП-автопродления.

        Никогда не бросает исключение наружу — коллбек Platega должен
        завершиться 200 OK независимо от того, доставилось ли сообщение в
        Telegram.
        """
        try:
            from app.cabinet.routes.websocket import cabinet_ws_manager

            await cabinet_ws_manager.send_to_user(
                record.user_id,
                {
                    'type': f'sbp_recurring.{kind}',
                    'status': record.status,
                    'amount_kopeks': record.amount_kopeks,
                    'amount_rubles': record.amount_kopeks / 100,
                    'next_charge_at': record.next_charge_at.isoformat() if record.next_charge_at else None,
                    'subscription_id': record.subscription_id,
                },
            )
        except Exception as ws_error:  # pragma: no cover — best-effort
            logger.warning('Не удалось отправить WS-событие СБП-автопродления', error=str(ws_error), kind=kind)

        bot = getattr(self, 'bot', None)
        if not bot:
            return
        try:
            from app.database.models import User
            from app.localization.texts import get_texts

            user = await db.get(User, record.user_id)
            if not user or not user.telegram_id:
                return

            texts = get_texts(user.language)
            messages = {
                'confirmed': texts.t('SBP_RECURRING_NOTIFY_CONFIRMED', '✅ Подписка продлена автосписанием через СБП.'),
                'failed': texts.t('SBP_RECURRING_NOTIFY_FAILED', '⚠️ Не удалось списать оплату по СБП-автопродлению.'),
                'past_due': texts.t('SBP_RECURRING_NOTIFY_PAST_DUE', '⚠️ СБП-автопродление просрочено.'),
                'cancelled': texts.t('SBP_RECURRING_NOTIFY_CANCELLED', 'ℹ️ СБП-автопродление отменено.'),
                'activated': texts.t('SBP_RECURRING_NOTIFY_ACTIVATED', '✅ СБП-автопродление активировано.'),
            }
            text = messages.get(kind)
            if text:
                await bot.send_message(chat_id=user.telegram_id, text=text)
        except Exception as error:  # pragma: no cover - best-effort notify
            logger.warning('Не удалось отправить уведомление о СБП-автопродлении', error=str(error), kind=kind)

    async def process_platega_webhook(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        payment_module = import_module('app.services.payment_service')

        transaction_id = str(payload.get('id') or '').strip()
        payload_token = payload.get('payload')

        payment = None
        if transaction_id:
            payment = await payment_module.get_platega_payment_by_transaction_id(db, transaction_id)
        if not payment and payload_token:
            payment = await payment_module.get_platega_payment_by_correlation_id(
                db, str(payload_token).replace('platega:', '')
            )

        if not payment:
            logger.warning('Platega webhook: платеж не найден', transaction_id=transaction_id)
            return False

        # Lock payment row immediately to prevent concurrent webhook processing (TOCTOU race)
        platega_crud = import_module('app.database.crud.platega')
        locked = await platega_crud.get_platega_payment_by_id_for_update(db, payment.id)
        if not locked:
            logger.error('Platega: не удалось заблокировать платёж', payment_id=payment.id)
            return False
        payment = locked

        status_raw = str(payload.get('status') or '').upper()
        if not status_raw:
            logger.warning('Platega webhook без статуса для платежа', payment_id=payment.id)
            return False

        if status_raw in self._SUCCESS_STATUSES:
            if payment.is_paid:
                logger.info('Platega платеж уже помечен как оплачен', correlation_id=payment.correlation_id)
                # Update callback payload without releasing the lock prematurely
                payment.callback_payload = payload
                if transaction_id and not payment.platega_transaction_id:
                    payment.platega_transaction_id = transaction_id
                payment.updated_at = datetime.now(UTC)
                await db.commit()
                return True

            # Inline field updates — NO intermediate commit that would release FOR UPDATE lock
            payment.status = status_raw
            payment.callback_payload = payload
            if transaction_id and not payment.platega_transaction_id:
                payment.platega_transaction_id = transaction_id
            payment.updated_at = datetime.now(UTC)
            await db.flush()

            result = await self._finalize_platega_payment(db, payment, payload)
            if result is None:
                logger.error('Platega webhook: финализация не удалась', payment_id=payment.id)
                return False
            return True

        if status_raw in self._FAILED_STATUSES:
            await payment_module.update_platega_payment(
                db,
                payment=payment,
                status=status_raw,
                callback_payload=payload,
                platega_transaction_id=transaction_id or None,
                is_paid=False,
            )
            logger.info('Platega платеж перешёл в статус', correlation_id=payment.correlation_id, status_raw=status_raw)
            return True

        await payment_module.update_platega_payment(
            db,
            payment=payment,
            status=status_raw,
            callback_payload=payload,
            platega_transaction_id=transaction_id or None,
        )
        return True

    async def get_platega_payment_status(
        self,
        db: AsyncSession,
        local_payment_id: int,
    ) -> dict[str, Any] | None:
        payment_module = import_module('app.services.payment_service')
        payment = await payment_module.get_platega_payment_by_id(db, local_payment_id)
        if not payment:
            return None

        service: PlategaService | None = getattr(self, 'platega_service', None)
        remote_status: str | None = None
        remote_payload: dict[str, Any] | None = None

        if service and payment.platega_transaction_id:
            try:
                remote_payload = await service.get_transaction(payment.platega_transaction_id)
            except Exception as error:  # pragma: no cover - network errors
                logger.error(
                    'Ошибка Platega при получении транзакции',
                    platega_transaction_id=payment.platega_transaction_id,
                    error=error,
                )

        if remote_payload:
            remote_status = str(remote_payload.get('status') or '').upper()
            status_changed = remote_status and remote_status != payment.status

            if remote_status in self._SUCCESS_STATUSES and not payment.is_paid:
                # Lock payment row before finalization to prevent concurrent double-processing
                platega_crud = import_module('app.database.crud.platega')
                locked = await platega_crud.get_platega_payment_by_id_for_update(db, payment.id)
                if not locked:
                    logger.error('Platega status check: не удалось заблокировать платёж', payment_id=payment.id)
                elif locked.is_paid:
                    # Another concurrent handler already processed — skip
                    logger.info('Platega платеж уже оплачен после блокировки', correlation_id=locked.correlation_id)
                    payment = locked
                else:
                    payment = locked
                    payment.status = remote_status
                    payment.callback_payload = remote_payload
                    payment.metadata_json = {
                        **(getattr(payment, 'metadata_json', {}) or {}),
                        'remote_status': remote_payload,
                    }
                    payment.updated_at = datetime.now(UTC)
                    await db.flush()
                    result = await self._finalize_platega_payment(db, payment, remote_payload)
                    if result is not None:
                        payment = result
            elif status_changed:
                # Non-success status change — safe to persist without lock
                payment.status = remote_status
                payment.metadata_json = {
                    **(getattr(payment, 'metadata_json', {}) or {}),
                    'remote_status': remote_payload,
                }
                payment.updated_at = datetime.now(UTC)
                await db.commit()

        return {
            'payment': payment,
            'status': payment.status,
            'is_paid': payment.is_paid,
            'remote': remote_payload,
        }

    async def _finalize_platega_payment(
        self,
        db: AsyncSession,
        payment: Any,
        payload: dict[str, Any] | None,
    ) -> Any:
        payment_module = import_module('app.services.payment_service')

        paid_at = None
        if isinstance(payload, dict):
            paid_at_raw = payload.get('paidAt') or payload.get('confirmedAt')
            if paid_at_raw:
                try:
                    paid_at_parsed = datetime.fromisoformat(str(paid_at_raw))
                    paid_at = paid_at_parsed if paid_at_parsed.tzinfo else paid_at_parsed.replace(tzinfo=UTC)
                except ValueError:
                    paid_at = None

        # FOR UPDATE lock already acquired by caller — just check idempotency
        if payment.transaction_id:
            logger.info(
                'Platega платеж уже связан с транзакцией',
                correlation_id=payment.correlation_id,
                transaction_id=payment.transaction_id,
            )
            return payment

        # Read fresh metadata AFTER lock to avoid stale data
        metadata = dict(getattr(payment, 'metadata_json', {}) or {})

        # --- Guest purchase flow (landing page) ---
        from app.services.payment.common import try_fulfill_guest_purchase

        guest_result = await try_fulfill_guest_purchase(
            db,
            metadata=metadata,
            payment_amount_kopeks=payment.amount_kopeks,
            provider_payment_id=payment.correlation_id,
            provider_name='platega',
        )
        if guest_result is not None:
            return payment

        if payload is not None:
            metadata['webhook'] = payload

        # Inline field assignments instead of update_platega_payment() which commits
        # and would release the FOR UPDATE lock prematurely
        payment.status = 'CONFIRMED'
        payment.is_paid = True
        if paid_at is not None:
            payment.paid_at = paid_at
        payment.metadata_json = metadata
        if payload is not None:
            payment.callback_payload = payload
        payment.updated_at = datetime.now(UTC)

        balance_already_credited = bool(metadata.get('balance_credited'))

        invoice_message = metadata.get('invoice_message') or {}
        if getattr(self, 'bot', None):
            chat_id = invoice_message.get('chat_id')
            message_id = invoice_message.get('message_id')
            if chat_id and message_id:
                try:
                    await self.bot.delete_message(chat_id, message_id)
                except Exception as delete_error:  # pragma: no cover - depends on bot rights
                    logger.warning('Не удалось удалить Platega счёт', message_id=message_id, delete_error=delete_error)
                else:
                    metadata.pop('invoice_message', None)

        user = await payment_module.get_user_by_id(db, payment.user_id)
        if not user:
            logger.error('Пользователь не найден для Platega', user_id=payment.user_id)
            return payment

        # Убеждаемся, что промогруппы загружены в асинхронном контексте,
        # чтобы избежать попыток ленивой загрузки без greenlet
        await db.refresh(user, attribute_names=['promo_group', 'user_promo_groups'])
        for user_promo_group in getattr(user, 'user_promo_groups', []):
            await db.refresh(user_promo_group, attribute_names=['promo_group'])

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)

        transaction_external_id = (
            str(payload.get('id'))
            if isinstance(payload, dict) and payload.get('id')
            else payment.platega_transaction_id
        )

        existing_transaction = None
        if transaction_external_id:
            existing_transaction = await payment_module.get_transaction_by_external_id(
                db,
                transaction_external_id,
                PaymentMethod.PLATEGA,
            )

        platega_name = settings.get_platega_display_name()
        method_display = settings.get_platega_method_display_name(payment.payment_method_code)
        description = (
            f'Пополнение через {platega_name} ({method_display})'
            if method_display
            else f'Пополнение через {platega_name}'
        )

        transaction = existing_transaction
        created_transaction = False

        if not transaction:
            transaction = await payment_module.create_transaction(
                db,
                user_id=payment.user_id,
                type=TransactionType.DEPOSIT,
                amount_kopeks=payment.amount_kopeks,
                description=description,
                payment_method=PaymentMethod.PLATEGA,
                external_id=transaction_external_id or payment.correlation_id,
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )
            created_transaction = True

        await payment_module.link_platega_payment_to_transaction(db, payment=payment, transaction_id=transaction.id)

        should_credit_balance = created_transaction or not balance_already_credited

        if not should_credit_balance:
            logger.info('Platega платеж уже зачислил баланс ранее', correlation_id=payment.correlation_id)
            return payment

        # Lock user row to prevent concurrent balance race conditions
        from app.database.crud.user import lock_user_for_update

        user = await lock_user_for_update(db, user)

        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup

        user.balance_kopeks += payment.amount_kopeks
        user.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(user)

        # Emit deferred side-effects after atomic commit
        from app.database.crud.transaction import emit_transaction_side_effects

        await emit_transaction_side_effects(
            db,
            transaction,
            amount_kopeks=payment.amount_kopeks,
            user_id=payment.user_id,
            type=TransactionType.DEPOSIT,
            payment_method=PaymentMethod.PLATEGA,
            external_id=transaction_external_id or payment.correlation_id,
        )

        topup_status = '🆕 Первое пополнение' if was_first_topup else '🔄 Пополнение'

        try:
            from app.services.referral_service import process_referral_topup

            await process_referral_topup(
                db,
                user.id,
                payment.amount_kopeks,
                getattr(self, 'bot', None),
            )
        except Exception as error:
            logger.error('Ошибка обработки реферального пополнения Platega', error=error)

        if was_first_topup and not user.has_made_first_topup and not user.referred_by_id:
            user.has_made_first_topup = True
            await db.commit()
            await db.refresh(user)

        if getattr(self, 'bot', None):
            try:
                from app.services.admin_notification_service import AdminNotificationService

                notification_service = AdminNotificationService(self.bot)
                await notification_service.send_balance_topup_notification(
                    user,
                    transaction,
                    old_balance,
                    topup_status=topup_status,
                    referrer_info=referrer_info,
                    subscription=subscription,
                    promo_group=promo_group,
                    db=db,
                )
            except Exception as error:
                logger.error('Ошибка отправки админ уведомления Platega', error=error)

        method_title = settings.get_platega_method_display_title(payment.payment_method_code)

        if getattr(self, 'bot', None) and user.telegram_id:
            try:
                keyboard = await self.build_topup_success_keyboard(user)
                await self.bot.send_message(
                    user.telegram_id,
                    (
                        '✅ <b>Пополнение успешно!</b>\n\n'
                        f'💰 Сумма: {settings.format_price(payment.amount_kopeks)}\n'
                        f'🦊 Способ: {method_title}\n'
                        f'🆔 Транзакция: {transaction.id}\n\n'
                        'Баланс пополнен автоматически!'
                    ),
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except Exception as error:
                logger.error('Ошибка отправки уведомления пользователю Platega', error=error)

        try:
            from app.services.payment.common import send_cart_notification_after_topup

            await send_cart_notification_after_topup(user, payment.amount_kopeks, db, getattr(self, 'bot', None))
        except Exception as error:
            logger.error(
                'Ошибка при работе с сохраненной корзиной для пользователя',
                user_id=payment.user_id,
                error=error,
                exc_info=True,
            )

        metadata['balance_change'] = {
            'old_balance': old_balance,
            'new_balance': user.balance_kopeks,
            'credited_at': datetime.now(UTC).isoformat(),
        }
        metadata['balance_credited'] = True

        await payment_module.update_platega_payment(
            db,
            payment=payment,
            metadata=metadata,
        )

        logger.info(
            '✅ Обработан Platega платеж для пользователя',
            correlation_id=payment.correlation_id,
            user_id=payment.user_id,
        )

        return payment


class _PlategaSbpAgent(PlategaPaymentMixin):
    """Минимальный носитель ``PlategaPaymentMixin`` для модульных точек входа
    СБП-автопродления Platega: несёт только ``platega_service``, в отличие от
    полного ``PaymentService`` с ещё 23 провайдерами, которые для СБП-автопродления
    не нужны. Обслуживает и создание (``enable_platega_sbp_recurring``), и отмену/
    отзыв подписки (Task 11, ``cancel_platega_recurring_for_subscription_safe``) —
    несёт весь ``PlategaPaymentMixin``, а не только его часть."""

    def __init__(self) -> None:
        self.platega_service = PlategaService()


async def enable_platega_sbp_recurring(
    db: AsyncSession,
    *,
    user_id: int,
    subscription: Any,
    tariff: Any,
) -> dict[str, Any]:
    """Создать СБП-автопродление. Возвращает {local_id, platega_subscription_id, redirect_url, status}.

    НЕ best-effort: пробрасывает ValueError (нет цены за период) / RuntimeError
    (Platega не отдала transactionId), чтобы UI показал ошибку. Гейт на фичу.
    """
    if not settings.is_platega_recurrent_enabled():
        raise RuntimeError('Platega recurrent is disabled')
    return await _PlategaSbpAgent().create_platega_sbp_subscription(
        db, user_id=user_id, subscription=subscription, tariff=tariff
    )


async def purchase_tariff_with_sbp_recurring(
    db: AsyncSession,
    *,
    user: Any,
    tariff: Any,
) -> dict[str, Any]:
    """Оформление подписки на тариф с оплатой через СБП-автопродление Platega.

    Первое списание Platega = подтверждение привязки в банке, поэтому «купить
    с автооплатой» — это привязка + первый чардж, который продлит/оживит
    подписку по каденс-правилу (месячный тариф — помесячно, 90 дней —
    помесячно по цене месяца, суточный — по дням).

    Флоу по состоянию подписки юзера на этот тариф:
    - непройденной подписки нет → создаётся EXPIRED-заготовка
      (``create_sbp_pending_subscription``): доступа нет, панели нет; первый
      CONFIRMED продлит и активирует, синк панели создаст panel-юзера;
    - есть непройденная (active/expired, не триал) → привязка на неё;
    - триал (живой или истёкший) → отказ ValueError: конверсию триала в
      платную делает только balance-покупка (revive/convert), а активация
      второй строки того же тарифа при живом триале упёрлась бы в partial
      unique index прямо в чардж-коллбеке — деньги взяты, продление упало;
    - в single-sub режиме подписка другого тарифа → отказ ValueError:
      чарджи привязки продлевали бы подписку ЧУЖОГО тарифа по цене
      выбранного (смену тарифа делает balance-покупка).

    Возвращает результат enable + ``subscription_id``. Пробрасывает
    ValueError/RuntimeError — UI показывает сообщение.
    """
    if not settings.is_platega_recurrent_enabled():
        raise RuntimeError('Platega recurrent is disabled')

    from app.database.crud.subscription import (
        create_sbp_pending_subscription,
        get_subscription_by_user_and_tariff,
        get_subscription_by_user_id,
    )

    if settings.is_multi_tariff_enabled():
        subscription = await get_subscription_by_user_and_tariff(db, user.id, tariff.id, include_inactive=True)
    else:
        subscription = await get_subscription_by_user_id(db, user.id)
        if subscription is not None and subscription.tariff_id != tariff.id:
            raise ValueError('СБП-оформление недоступно при подписке другого тарифа — оплатите с баланса')

    if subscription is not None:
        if getattr(subscription, 'is_trial', False):
            raise ValueError('СБП-оформление недоступно для триальной подписки — оплатите с баланса')
        # DISABLED чардж не оживит (extend_subscription активирует только
        # EXPIRED/LIMITED) — деньги списались бы без выдачи доступа. PENDING —
        # чужой незавершённый платёжный флоу (миниапп-ордер), не влезаем.
        if subscription.status in ('disabled', 'pending'):
            raise ValueError('СБП-оформление недоступно для этой подписки — оплатите с баланса')

    if subscription is None:
        subscription = await create_sbp_pending_subscription(db, user.id, tariff)

    result = await enable_platega_sbp_recurring(db, user_id=user.id, subscription=subscription, tariff=tariff)
    return {**result, 'subscription_id': subscription.id}


async def replay_missed_platega_charges(
    db: AsyncSession,
    record: Any,
    remote: dict[str, Any] | None,
) -> int:
    """Доначисляет списания, чьи CONFIRMED-коллбеки потерялись.

    Remote ``chargeMetrics.chargesSuccess`` > локального счётчика = юзер
    заплатил, а продление не применилось (коллбек не дошёл) — доступ истёк бы
    несмотря на успешное списание. Каждое пропущенное списание проигрывается
    через штатный ``process_platega_subscription_callback`` с синтетическим
    charge Id ``{platega_id}:replay:{ordinal}`` — вся идемпотентность
    (полноисторийная сверка по transactions.external_id), продление, панель и
    уведомления переиспользуются 1:1.

    Гард 2 часа от remote lastChargeAt: запоздавший НАСТОЯЩИЙ коллбек имеет
    другой Id и прошёл бы мимо дедупа — даём ему окно доехать. Худший случай
    гонки после окна — двойное ПРОДЛЕНИЕ (подарок дней юзеру), не двойное
    списание. Возвращает число доначисленных списаний.
    """
    metrics = (remote or {}).get('chargeMetrics') or {}
    try:
        remote_success = int(metrics.get('chargesSuccess') or 0)
    except (TypeError, ValueError):
        return 0
    local_success = int(record.charges_success or 0)
    missed = remote_success - local_success
    if missed <= 0 or not record.platega_subscription_id:
        return 0

    last_charge_at = _parse_next_charge(metrics.get('lastChargeAt') or (remote or {}).get('lastChargeAt'))
    if last_charge_at is None or (datetime.now(UTC) - last_charge_at) < timedelta(hours=2):
        return 0

    next_charge_raw = metrics.get('nextChargeAt') or (remote or {}).get('nextChargeAt')

    # Снапшот ДО реплеев: best-effort панель-синк внутри коллбека может сделать
    # rollback, который экспайрит атрибуты record — повторное чтение ORM-полей
    # после первого реплея упало бы MissingGreenlet.
    platega_id = record.platega_subscription_id

    agent = _PlategaSbpAgent()
    replayed = 0
    # Кап на проход — защита от бесконечного цикла при рассинхроне счётчиков;
    # остаток доначислит следующий цикл мониторинга.
    for ordinal in range(local_success + 1, local_success + 1 + min(missed, 12)):
        payload = {
            'Status': 'CONFIRMED',
            'Id': f'{platega_id}:replay:{ordinal}',
            'SubscriptionId': platega_id,
            'NextChargeAt': next_charge_raw,
        }
        await agent.process_platega_subscription_callback(db, payload)
        replayed += 1

    if replayed:
        logger.warning(
            'Platega: доначислены пропущенные списания (коллбеки не дошли)',
            platega_subscription_id=platega_id,
            replayed=replayed,
            remote_charges_success=remote_success,
        )
    return replayed


async def cancel_platega_recurring_for_subscription_safe(
    db: AsyncSession,
    subscription_id: int,
    *,
    commit: bool = True,
) -> None:
    """Точка входа для путей удаления/отзыва подписки: отменяет активную
    СБП-автоподписку Platega, привязанную к ``subscription_id``.

    Best-effort и никогда не бросает исключение — вызывающий код (удаление
    подписки администратором, пользователем из кабинета и т.д.) не должен
    блокироваться недоступностью Platega.

    НЕ гейтится флагом рекуррента НАМЕРЕННО: отмена — операция безопасности,
    а не фича. Выключение PLATEGA_RECURRENT_ENABLED при живых привязках не
    останавливает списания Platega — коллбеки продолжают приходить; если бы
    отмена гейтилась, юзер с выключенной фичей не мог бы остановить списания
    ни с одной поверхности (двойной биллинг при повторном включении autopay).
    Быстрый выход при выключенном Platega целиком не нужен: без активной
    записи в БД функция завершается одним дешёвым SELECT.

    ``commit=False`` — для вызова внутри чужой транзакции (мерж аккаунтов):
    локальный CANCELLED уходит flush'ем и коммитится вместе с транзакцией
    вызывающего.
    """
    try:
        await _PlategaSbpAgent().cancel_platega_recurring_for_subscription(db, subscription_id, commit=commit)
    except Exception as error:  # pragma: no cover - defensive; mixin already best-effort
        logger.warning(
            'Не удалось отменить СБП-автопродление при удалении подписки',
            error=str(error),
            subscription_id=subscription_id,
        )
