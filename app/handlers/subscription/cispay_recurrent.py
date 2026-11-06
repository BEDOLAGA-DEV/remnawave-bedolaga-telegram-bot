"""Explicit CisPay consent; every callback carries or resolves an owned target."""

from uuid import uuid4

from aiogram import types
from sqlalchemy import select

from app.database.crud.cispay_subscriptions import LIVE_STATUSES
from app.database.models import Subscription, Tariff
from app.localization.texts import get_texts
from app.services.cispay_recurrent import get_cispay_recurrent_policy
from app.services.cispay_recurrent_checkout import (
    get_cispay_agreement_state,
    purchase_cispay_quote,
    quote_cispay_purchase,
)
from app.services.cispay_recurrent_service import cancel_cispay_agreement
from app.utils.formatting import format_price_kopeks


def _button(text, data=None, url=None):
    return types.InlineKeyboardButton(text=text, callback_data=data, url=url)


def format_cispay_status(info, texts):
    from datetime import datetime

    from app.utils.formatters import format_datetime

    message = texts.t('CISREC_TITLE', 'Автоплатёж CisPay') + '\n\n'
    message += texts.t('CISREC_STATE_' + info['status'].upper(), 'Проверяем состояние автоплатежа.')
    if info.get('amount_kopeks') is not None:
        message += '\n' + texts.t('CISREC_TERMS', '{amount} каждые {days} дн.').format(
            amount=format_price_kopeks(info['amount_kopeks']), days=info['period_days']
        )
    if info.get('next_charge_at'):
        message += '\n' + texts.t('CISREC_NEXT', 'Следующее списание: {date}').format(
            date=format_datetime(datetime.fromisoformat(info['next_charge_at']))
        )
    if info.get('reason'):
        message += '\n' + texts.t('CISREC_REASON_' + info['reason'].upper(), 'Проверяем состояние автоплатежа.')
    return message


def cispay_error_text(texts, error):
    keys = {
        'disable_balance_autopay_first': 'BALANCEAUTOPAY',
        'recurrent_already_exists': 'EXISTING',
        'quote_changed': 'QUOTECHANGED',
        'quote_expired': 'QUOTECHANGED',
        'subscription_disabled': 'DISABLEDSUBSCRIPTION',
        'tariff_switch_requires_balance': 'CHANGETARIFF',
        'device_selection_not_available': 'DEVICES',
        'amount_out_of_range': 'AMOUNT',
        'subscription_not_found': 'MISSINGSUBSCRIPTION',
        'select_existing_subscription': 'SELECTSUBSCRIPTION',
    }
    default = texts.t(
        'CISREC_UNAVAILABLE',
        'Не удалось подключить автоплатёж. Выберите тариф и период заново. CisPay поддерживает от 7 до 360 дней.',
    )
    return texts.t('CISREC_ERROR_' + keys[str(error)], default) if str(error) in keys else default


def tariff_has_cispay_period(tariff):
    if tariff is None or not tariff.is_active or tariff.is_daily:
        return False
    if any(7 <= day <= 360 for day in tariff.get_available_periods()):
        return True
    return tariff.can_purchase_custom_days() and max(7, tariff.min_days) <= min(360, tariff.max_days)


async def cispay_menu_rows(db, user, subscription, texts):
    info = await get_cispay_agreement_state(db, user_id=user.id, subscription_id=subscription.id)
    policy = await get_cispay_recurrent_policy(db, user=user, channel='telegram')
    tariff = await db.get(Tariff, subscription.tariff_id) if subscription.tariff_id else None
    if info['status'] in LIVE_STATUSES or (
        policy['enabled'] and subscription.status != 'disabled' and tariff_has_cispay_period(tariff)
    ):
        return [[_button(texts.t('CISREC_TITLE', 'Автоплатёж CisPay'), f'cisrec:menu:{subscription.id}')]]
    return []


async def _show_quote(callback, user, db, state, **selection):
    texts = get_texts(user.language)
    quote = await quote_cispay_purchase(db, user=user, channel='telegram', **selection)
    token = uuid4().hex[:16]
    await state.update_data(cispay_consent={'token': token, 'quote_id': quote['quote_id'], 'checkout_id': str(uuid4())})
    await callback.message.edit_text(
        texts.t(
            'CISREC_CONSENT',
            'Автоплатёж CisPay\n\nСейчас: {amount} за {days} дн.\nДалее: {amount} каждые {days} дн.\n\n'
            'Первый платёж оплачивает период и подключает автопродление. Оплаченный остаток сохраняется. '
            'Расписание списаний начинается с первой оплаты, а не с окончания подписки.',
        ).format(amount=format_price_kopeks(quote['amount_kopeks']), days=quote['period_days']),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [_button(texts.t('CISREC_PAY', 'Оплатить период и подключить'), f'cisrec:pay:{token}')],
                [_button(texts.BACK, 'menu_subscription')],
            ]
        ),
    )


async def purchase_cispay_from_tariff(callback, db_user, db, state):
    texts = get_texts(db_user.language)
    try:
        tariff_id = int(callback.data.split(':')[1])
        data = await state.get_data()
        cart = (data.get('restored_tariff_checkout') or {}).get('cart') or {}
        if cart.get('tariff_id') == tariff_id:
            from app.services.subscription_auto_purchase_service import _saved_tariff_checkout_external_id

            identity = data['restored_tariff_checkout'].get('identity') or cart
            expected = _saved_tariff_checkout_external_id(db_user.id, identity).rsplit(':', 1)[-1][:24]
            if callback.data.split(':')[2:] != [expected] or cart.get('custom_traffic_gb') is not None:
                raise ValueError('stale_selection')
            days, device, target = (
                cart.get('period_days'),
                cart.get('selected_device_limit'),
                cart.get('subscription_id'),
            )
        elif data.get('selected_tariff_id') == tariff_id:
            days = data.get('custom_days') or data.get('selected_period')
            choice = data.get('tariff_device_selection') or {}
            device = choice.get('limit') if choice.get('tariff_id') == tariff_id else None
            target = (
                data.get('target_subscription_id') or data.get('active_subscription_id') or data.get('subscription_id')
            )
        else:
            raise ValueError('stale_selection')
        await _show_quote(
            callback,
            db_user,
            db,
            state,
            tariff_id=tariff_id,
            period_days=days,
            device_limit=device,
            subscription_id=target,
        )
    except (ValueError, KeyError, TypeError) as error:
        await db.rollback()
        await callback.answer(
            cispay_error_text(texts, error),
            show_alert=True,
        )
        return
    await callback.answer()


async def handle_cispay_recurrent(callback, db_user, db, state):
    texts = get_texts(db_user.language)
    try:
        parts = callback.data.split(':')
        action = parts[1]
        if action == 'pay':
            consent = (await state.get_data()).get('cispay_consent') or {}
            if consent.get('token') != parts[2]:
                raise ValueError('stale_consent')
            result = await purchase_cispay_quote(
                db, user=db_user, quote_id=consent['quote_id'], checkout_id=consent['checkout_id'], channel='telegram'
            )
            target_id = result['subscription_id']
        else:
            target_id = int(parts[2])
            target = await db.scalar(
                select(Subscription).where(Subscription.id == target_id, Subscription.user_id == db_user.id)
            )
            if target is None:
                raise ValueError('subscription_not_found')
            if action == 'cancel':
                await cancel_cispay_agreement(db, user_id=db_user.id, subscription_id=target_id, reason='customer')
            elif action == 'select':
                from app.handlers.subscription.tariff_purchase import _proceed_with_selected_tariff

                await state.update_data(target_subscription_id=target.id, active_subscription_id=target.id)
                await _proceed_with_selected_tariff(callback, db_user, db, state, target.tariff_id)
                return
            elif action == 'quote':
                await _show_quote(
                    callback,
                    db_user,
                    db,
                    state,
                    tariff_id=target.tariff_id,
                    period_days=int(parts[3]),
                    device_limit=None if target.is_trial else target.device_limit,
                    subscription_id=target_id,
                )
                await callback.answer()
                return
            elif action != 'menu':
                raise ValueError('unknown_action')
        info = await get_cispay_agreement_state(db, user_id=db_user.id, subscription_id=target_id)
        rows = []
        status = info['status']
        message = format_cispay_status(info, texts)
        if status == 'PENDING' and info.get('redirect_url'):
            rows.append([_button(texts.t('CISREC_CONTINUE', 'Перейти к оплате'), url=info['redirect_url'])])
        if status in LIVE_STATUSES:
            rows.append([_button(texts.t('CISREC_CANCEL', 'Отключить автоплатёж'), f'cisrec:cancel:{target_id}')])
        else:
            target = await db.get(Subscription, target_id)
            tariff = await db.get(Tariff, target.tariff_id) if target.tariff_id else None
            policy = await get_cispay_recurrent_policy(db, user=db_user, channel='telegram')
            if policy['enabled'] and target.status != 'disabled' and tariff_has_cispay_period(tariff):
                if tariff.can_purchase_custom_days():
                    rows.append(
                        [_button(texts.t('CISREC_SELECT_PERIOD', 'Выбрать срок'), f'cisrec:select:{target_id}')]
                    )
                for days in tariff.get_available_periods():
                    if 7 <= days <= 360:
                        rows.append(
                            [
                                _button(
                                    texts.t('CISREC_PERIOD', '{days} дн.').format(days=days),
                                    f'cisrec:quote:{target_id}:{days}',
                                )
                            ]
                        )
        rows.append([_button(texts.t('CISREC_REFRESH', 'Обновить'), f'cisrec:menu:{target_id}')])
        rows.append([_button(texts.BACK, 'menu_subscription')])
        await callback.message.edit_text(message, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows))
    except (ValueError, KeyError, IndexError, TypeError) as error:
        await db.rollback()
        await callback.answer(
            cispay_error_text(texts, error),
            show_alert=True,
        )
        return
    await callback.answer()
