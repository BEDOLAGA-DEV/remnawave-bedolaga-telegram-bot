"""Сервис принудительного отключения всех рекуррентных подписок пользователя
через API всех платёжных систем.
"""

from __future__ import annotations

from typing import Any
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AntilopayPayment,
    AntilopayRecurrent,
    LavaSubscription,
    PlategaSubscription,
    SavedPaymentMethod,
    Subscription,
    User,
)

logger = structlog.get_logger(__name__)


async def cancel_all_user_recurring_subscriptions(
    db: AsyncSession,
    user_id: int,
) -> dict[str, Any]:
    """Принудительно отключает все рекуррентные подписки для пользователя
    через API всех подключённых платёжных шлюзов (Platega, Lava, Antilopay),
    деактивирует сохранённые карты YooKassa и внутреннее автопродление бота,
    независимо от локального статуса в базе данных.

    Возвращает детализированный отчёт по каждому действию.
    """
    user = await db.get(User, user_id)
    if not user:
        return {
            'success': False,
            'summary': {'total_actions': 0, 'success_count': 0, 'failed_count': 0},
            'results': [],
            'message': f'Пользователь #{user_id} не найден',
        }

    results: list[dict[str, Any]] = []

    # Подписки пользователя в боте
    user_subs_result = await db.execute(
        select(Subscription).where(Subscription.user_id == user_id)
    )
    user_subscriptions = list(user_subs_result.scalars().all())
    user_sub_ids = [sub.id for sub in user_subscriptions]

    # =========================================================================
    # 1. Platega (СБП рекуррент)
    # =========================================================================
    try:
        from app.services.platega_service import platega_service

        platega_query = select(PlategaSubscription).where(
            (PlategaSubscription.user_id == user_id)
            | (
                PlategaSubscription.subscription_id.in_(user_sub_ids)
                if user_sub_ids
                else False
            )
        )
        platega_res = await db.execute(platega_query)
        platega_records = list(platega_res.scalars().all())

        for p_rec in platega_records:
            target_id = p_rec.platega_subscription_id or f'ID #{p_rec.id}'
            old_status = p_rec.status

            if p_rec.platega_subscription_id:
                if not platega_service.is_configured:
                    results.append({
                        'provider': 'platega',
                        'provider_title': 'Platega (СБП)',
                        'target_id': target_id,
                        'status': 'error',
                        'message': f'Platega API не настроен: невозможно вызвать cancel для {target_id}',
                        'error': 'Platega service is not configured',
                    })
                else:
                    try:
                        resp = await platega_service.cancel_subscription(
                            p_rec.platega_subscription_id, return_status=True
                        )
                        if isinstance(resp, tuple):
                            api_res, http_status = resp
                        else:
                            api_res, http_status = resp, None

                        if (http_status is None or http_status < 400) and api_res is not None:
                            results.append({
                                'provider': 'platega',
                                'provider_title': 'Platega (СБП)',
                                'target_id': target_id,
                                'status': 'success',
                                'message': f'Подписка {target_id} успешно отменена через API Platega (статус в БД был: {old_status})',
                            })
                        elif http_status == 404 or (
                            isinstance(api_res, dict)
                            and any(
                                k in str(api_res).lower()
                                for k in ['not found', 'already', 'не найдена', 'отменена']
                            )
                        ):
                            results.append({
                                'provider': 'platega',
                                'provider_title': 'Platega (СБП)',
                                'target_id': target_id,
                                'status': 'success',
                                'message': f'Подписка {target_id} уже отменена или не найдена в Platega',
                            })
                        else:
                            err_detail = None
                            if isinstance(api_res, dict):
                                err_detail = api_res.get('message') or str(api_res)
                            elif http_status:
                                err_detail = f'HTTP {http_status}'
                            results.append({
                                'provider': 'platega',
                                'provider_title': 'Platega (СБП)',
                                'target_id': target_id,
                                'status': 'error',
                                'message': f'API Platega вернул ошибку при отмене {target_id}: {err_detail or "Unknown error"}',
                                'error': err_detail or 'Platega API error',
                            })
                    except Exception as exc:
                        logger.warning('Ошибка вызова API Platega cancel_subscription', error=str(exc))
                        results.append({
                            'provider': 'platega',
                            'provider_title': 'Platega (СБП)',
                            'target_id': target_id,
                            'status': 'error',
                            'message': f'Ошибка при вызове API Platega для {target_id}: {exc}',
                            'error': str(exc),
                        })
            else:
                results.append({
                    'provider': 'platega',
                    'provider_title': 'Platega (СБП)',
                    'target_id': target_id,
                    'status': 'success',
                    'message': f'Локальная запись Platega {target_id} помечена как отменённая (внешний ID отсутствует)',
                })

            p_rec.status = 'CANCELLED'

    except Exception as exc:
        logger.exception('Непредвиденная ошибка при обработке Platega', error=str(exc))
        results.append({
            'provider': 'platega',
            'provider_title': 'Platega (СБП)',
            'target_id': 'all',
            'status': 'error',
            'message': f'Исключение при обработке Platega: {exc}',
            'error': str(exc),
        })

    # =========================================================================
    # 2. Lava (Lava Business рекуррент)
    # =========================================================================
    try:
        from app.services.lava_service import lava_service

        lava_query = select(LavaSubscription).where(
            (LavaSubscription.user_id == user_id)
            | (
                LavaSubscription.subscription_id.in_(user_sub_ids)
                if user_sub_ids
                else False
            )
        )
        lava_res = await db.execute(lava_query)
        lava_records = list(lava_res.scalars().all())

        for l_rec in lava_records:
            target_id = l_rec.lava_subscription_id or l_rec.order_id or f'ID #{l_rec.id}'
            old_status = l_rec.status

            if l_rec.lava_subscription_id or l_rec.order_id:
                if not lava_service.is_configured:
                    results.append({
                        'provider': 'lava',
                        'provider_title': 'Lava',
                        'target_id': target_id,
                        'status': 'error',
                        'message': f'Lava API не настроен: невозможно вызвать unsubscribe для {target_id}',
                        'error': 'Lava service is not configured',
                    })
                else:
                    try:
                        await lava_service.unsubscribe_recurrent(
                            subscription_id=l_rec.lava_subscription_id,
                            order_id=None if l_rec.lava_subscription_id else l_rec.order_id,
                        )
                        results.append({
                            'provider': 'lava',
                            'provider_title': 'Lava',
                            'target_id': target_id,
                            'status': 'success',
                            'message': f'Подписка {target_id} успешно отменена через API Lava (статус в БД был: {old_status})',
                        })
                    except Exception as exc:
                        err_str = str(exc)
                        logger.warning('Ошибка вызова API Lava unsubscribe_recurrent', error=err_str)
                        if any(
                            keyword in err_str.lower()
                            for keyword in ['already', 'not found', 'отменен', 'не найден', 'не актив']
                        ):
                            results.append({
                                'provider': 'lava',
                                'provider_title': 'Lava',
                                'target_id': target_id,
                                'status': 'success',
                                'message': f'Подписка {target_id} уже отменена в Lava ({err_str})',
                            })
                        else:
                            results.append({
                                'provider': 'lava',
                                'provider_title': 'Lava',
                                'target_id': target_id,
                                'status': 'error',
                                'message': f'Ошибка при вызове API Lava для {target_id}: {err_str}',
                                'error': err_str,
                            })
            else:
                results.append({
                    'provider': 'lava',
                    'provider_title': 'Lava',
                    'target_id': target_id,
                    'status': 'success',
                    'message': f'Локальная запись Lava {target_id} помечена как отменённая (внешний ID отсутствует)',
                })

            l_rec.status = 'CANCELLED'

    except Exception as exc:
        logger.exception('Непредвиденная ошибка при обработке Lava', error=str(exc))
        results.append({
            'provider': 'lava',
            'provider_title': 'Lava',
            'target_id': 'all',
            'status': 'error',
            'message': f'Исключение при обработке Lava: {exc}',
            'error': str(exc),
        })

    # =========================================================================
    # 3. Antilopay (Antilopay рекуррент)
    # =========================================================================
    try:
        from app.services.antilopay_service import antilopay_service

        ant_recurrents_res = await db.execute(
            select(AntilopayRecurrent).where(AntilopayRecurrent.user_id == user_id)
        )
        ant_recurrents = list(ant_recurrents_res.scalars().all())

        ant_payments_res = await db.execute(
            select(AntilopayPayment).where(
                AntilopayPayment.user_id == user_id,
                AntilopayPayment.recurrent_id.isnot(None),
            )
        )
        ant_payments = list(ant_payments_res.scalars().all())

        # Собираем уникальные идентификаторы рекуррентов
        targets_to_cancel: list[tuple[str | None, str | None]] = []
        seen_recurrent_ids: set[str] = set()

        for rec in ant_recurrents:
            if rec.recurrent_id and rec.recurrent_id not in seen_recurrent_ids:
                seen_recurrent_ids.add(rec.recurrent_id)
                targets_to_cancel.append((rec.recurrent_id, rec.initial_payment_id))

        for pmt in ant_payments:
            if pmt.recurrent_id and pmt.recurrent_id not in seen_recurrent_ids:
                seen_recurrent_ids.add(pmt.recurrent_id)
                targets_to_cancel.append(
                    (pmt.recurrent_id, pmt.antilopay_payment_id or pmt.order_id)
                )

        if targets_to_cancel:
            if not antilopay_service.is_configured:
                for rec_id, tx_id in targets_to_cancel:
                    target_id = rec_id or tx_id or 'unknown'
                    results.append({
                        'provider': 'antilopay',
                        'provider_title': 'Antilopay',
                        'target_id': target_id,
                        'status': 'error',
                        'message': f'Antilopay API не настроен: невозможно отменить рекуррент {target_id}',
                        'error': 'Antilopay service is not configured',
                    })
            else:
                for rec_id, tx_id in targets_to_cancel:
                    target_id = rec_id or tx_id or 'unknown'
                    try:
                        await antilopay_service.cancel_recurrent_payment(
                            recurrent_id=rec_id,
                            transaction_id=tx_id,
                        )
                        results.append({
                            'provider': 'antilopay',
                            'provider_title': 'Antilopay',
                            'target_id': target_id,
                            'status': 'success',
                            'message': f'Рекуррент {target_id} успешно отменён через API Antilopay',
                        })
                    except Exception as exc:
                        err_str = str(exc)
                        logger.warning('Ошибка вызова API Antilopay cancel_recurrent_payment', error=err_str)
                        if any(
                            keyword in err_str.lower()
                            for keyword in ['already', 'not active', 'уже отменен', 'не актив']
                        ):
                            results.append({
                                'provider': 'antilopay',
                                'provider_title': 'Antilopay',
                                'target_id': target_id,
                                'status': 'success',
                                'message': f'Рекуррент {target_id} уже не активен в Antilopay ({err_str})',
                            })
                        else:
                            results.append({
                                'provider': 'antilopay',
                                'provider_title': 'Antilopay',
                                'target_id': target_id,
                                'status': 'error',
                                'message': f'Ошибка при отмене рекуррента Antilopay {target_id}: {err_str}',
                                'error': err_str,
                            })

        # Помечаем все локальные записи Antilopay как неактивные
        for rec in ant_recurrents:
            rec.is_active = False
            rec.status = 'CANCEL'

    except Exception as exc:
        logger.exception('Непредвиденная ошибка при обработке Antilopay', error=str(exc))
        results.append({
            'provider': 'antilopay',
            'provider_title': 'Antilopay',
            'target_id': 'all',
            'status': 'error',
            'message': f'Исключение при обработке Antilopay: {exc}',
            'error': str(exc),
        })

    # =========================================================================
    # 4. YooKassa (Сохранённые методы оплаты / карты)
    # =========================================================================
    try:
        saved_pm_res = await db.execute(
            select(SavedPaymentMethod).where(SavedPaymentMethod.user_id == user_id)
        )
        saved_payment_methods = list(saved_pm_res.scalars().all())

        if saved_payment_methods:
            active_count = sum(1 for sm in saved_payment_methods if sm.is_active)
            for sm in saved_payment_methods:
                sm.is_active = False

            card_labels = [
                sm.title or f'{sm.card_type or sm.method_type} •••• {sm.card_last4 or "????"}'
                for sm in saved_payment_methods
            ]
            results.append({
                'provider': 'yookassa',
                'provider_title': 'YooKassa (Сохранённые методы)',
                'target_id': f'{len(saved_payment_methods)} методов',
                'status': 'success',
                'message': (
                    f'Деактивировано {len(saved_payment_methods)} сохранённых методов YooKassa '
                    f'(активных было: {active_count}): {", ".join(card_labels)}'
                ),
            })
    except Exception as exc:
        logger.exception('Непредвиденная ошибка при деактивации методов YooKassa', error=str(exc))
        results.append({
            'provider': 'yookassa',
            'provider_title': 'YooKassa (Сохранённые методы)',
            'target_id': 'all',
            'status': 'error',
            'message': f'Исключение при деактивации методов YooKassa: {exc}',
            'error': str(exc),
        })

    # =========================================================================
    # 5. Внутреннее автопродление бота (Subscription.autopay_enabled)
    # =========================================================================
    try:
        autopay_subs = [s for s in user_subscriptions if s.autopay_enabled]
        if autopay_subs:
            for s in autopay_subs:
                s.autopay_enabled = False
            sub_ids_str = ', '.join(str(s.id) for s in autopay_subs)
            results.append({
                'provider': 'bot_autopay',
                'provider_title': 'Автопродление бота',
                'target_id': f'{len(autopay_subs)} подписок',
                'status': 'success',
                'message': f'Отключено внутреннее автопродление бота для {len(autopay_subs)} подписок (ID: {sub_ids_str})',
            })
    except Exception as exc:
        logger.exception('Непредвиденная ошибка при отключении автопродления бота', error=str(exc))
        results.append({
            'provider': 'bot_autopay',
            'provider_title': 'Автопродление бота',
            'target_id': 'all',
            'status': 'error',
            'message': f'Исключение при отключении автопродления бота: {exc}',
            'error': str(exc),
        })

    # Сохраняем все изменения в БД
    await db.commit()

    total_actions = len(results)
    success_count = sum(1 for r in results if r['status'] == 'success')
    failed_count = sum(1 for r in results if r['status'] == 'error')

    if total_actions == 0:
        main_message = 'У пользователя не найдено привязанных рекуррентных подписок или методов оплаты'
    elif failed_count == 0:
        main_message = f'Все рекуррентные подписки и методы оплаты успешно отключены ({success_count} действий)'
    elif success_count == 0:
        main_message = f'Не удалось отключить рекуррентные подписки ({failed_count} ошибок)'
    else:
        main_message = f'Отключение выполнено частично: {success_count} успешно, {failed_count} с ошибками'

    logger.info(
        'Принудительное отключение рекуррентов пользователя завершено',
        user_id=user_id,
        total_actions=total_actions,
        success_count=success_count,
        failed_count=failed_count,
    )

    return {
        'success': failed_count == 0,
        'summary': {
            'total_actions': total_actions,
            'success_count': success_count,
            'failed_count': failed_count,
        },
        'results': results,
        'message': main_message,
    }
