"""Explicit HOT Pay consent; every callback carries or resolves an owned target."""

from html import escape
from uuid import uuid4

from aiogram import types
from sqlalchemy import select

from app.database.models import Subscription, Tariff
from app.localization.texts import get_texts
from app.services.payhot_recurrent_checkout import (
    get_payhot_agreement_state,
    purchase_payhot_quote,
    quote_payhot_purchase,
)
from app.services.payhot_recurrent_guard import LIVE_STATUSES
from app.services.payhot_recurrent_policy import get_payhot_recurrent_policy
from app.services.payhot_recurrent_service import cancel_payhot_agreement
from app.states import SubscriptionStates
from app.utils.formatting import format_price_kopeks


def _button(text, data=None, url=None):
    return types.InlineKeyboardButton(text=text, callback_data=data, url=url)


def format_payhot_status(info, texts):
    from datetime import datetime

    from app.utils.formatters import format_datetime

    message = texts.t('HOTREC_TITLE', 'Автоплатёж HOT Pay') + '\n\n'
    message += texts.t('HOTREC_STATE_' + info['status'].upper(), 'Проверяем состояние автоплатежа.')
    if info.get('amount_kopeks') is not None:
        message += '\n' + texts.t('HOTREC_TERMS', '{amount} каждые {days} дн.').format(
            amount=format_price_kopeks(info['amount_kopeks']), days=info['period_days']
        )
    if info.get('next_charge_at'):
        message += '\n' + texts.t('HOTREC_NEXT', 'Следующее списание: {date}').format(
            date=format_datetime(datetime.fromisoformat(info['next_charge_at']))
        )
    if info.get('reason'):
        message += '\n' + texts.t('HOTREC_REASON_' + info['reason'], 'Проверяем состояние автоплатежа.')
    return message


def payhot_error_text(texts, error):
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
        'HOTREC_UNAVAILABLE',
        'Не удалось подключить автоплатёж. Выберите тариф и период заново. HOT Pay поддерживает периоды, доступные для выбранной подписки.',
    )
    return texts.t('HOTREC_ERROR_' + keys[str(error)], default) if str(error) in keys else default


def tariff_has_payhot_period(tariff):
    if tariff is None:
        return True
    if not tariff.is_active:
        return False
    if tariff.is_daily or any(day > 0 for day in tariff.get_available_periods()):
        return True
    return tariff.can_purchase_custom_days() and 0 < tariff.min_days <= tariff.max_days


async def payhot_menu_rows(db, user, subscription, texts):
    info = await get_payhot_agreement_state(db, user_id=user.id, subscription_id=subscription.id)
    policy = await get_payhot_recurrent_policy(db, user=user, channel='telegram')
    tariff = await db.get(Tariff, subscription.tariff_id) if subscription.tariff_id else None
    if info['status'] in LIVE_STATUSES or (
        policy['enabled'] and subscription.status != 'disabled' and tariff_has_payhot_period(tariff)
    ):
        return [[_button(texts.t('HOTREC_TITLE', 'Автоплатёж HOT Pay'), f'hotrec:menu:{subscription.id}')]]
    return []


async def _choose_method(reply, user, db, state, **selection):
    texts = get_texts(user.language)
    policy = await get_payhot_recurrent_policy(db, user=user, channel='telegram')
    if not policy['enabled']:
        raise ValueError('disabled')
    target = selection.get('subscription_id')
    token = uuid4().hex[:12]
    await state.update_data(payhot_selection={**selection, 'token': token})
    rows = [
        [_button(texts.t('HOTREC_METHOD_' + method.upper(), method), f'hotrec:method:{target or 0}:{method}:{token}')]
        for method in policy['methods']
    ]
    rows.append([_button(texts.BACK, f'hotrec:menu:{target}' if target else 'menu_subscription')])
    await reply(
        texts.t('HOTREC_METHOD', 'Выберите способ автоплатежа HOT Pay'),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _show_quote(callback, user, db, state, **selection):
    await _present_quote(callback.message.edit_text, user, db, state, **selection)


async def _present_quote(reply, user, db, state, **selection):
    texts = get_texts(user.language)
    quote = await quote_payhot_purchase(db, user=user, channel='telegram', **selection)
    target_id = quote['source_subscription_id']
    target_text = (
        texts.t('HOTREC_TARGET_EXISTING', 'Текущая подписка')
        if target_id is not None
        else texts.t('HOTREC_TARGET_NEW', 'Новая подписка')
    )
    tariff = await db.get(Tariff, quote['tariff_id']) if quote['tariff_id'] is not None else None
    tariff_name = tariff.name if tariff else texts.t('HOTREC_CLASSIC', 'Классическая подписка')
    devices = (
        texts.t('HOTREC_UNLIMITED', 'Без ограничений') if quote['device_limit'] == 0 else str(quote['device_limit'])
    )
    details = '\n'.join(
        (
            escape(target_text),
            texts.t('HOTREC_TARIFF', 'Тариф: {tariff}').format(tariff=escape(tariff_name)),
            texts.t('HOTREC_DEVICES', 'Устройства: {devices}').format(devices=escape(devices)),
            texts.t('HOTREC_PAYMENT_METHOD', 'Способ оплаты: {method}').format(
                method=texts.t('HOTREC_METHOD_' + quote['payment_method'].upper())
            ),
        )
    )
    token = uuid4().hex[:16]
    await state.update_data(payhot_consent={'token': token, 'quote_id': quote['quote_id'], 'checkout_id': str(uuid4())})
    await reply(
        texts.t(
            'HOTREC_CONSENT_DAILY' if quote.get('is_daily') else 'HOTREC_CONSENT',
            'Автоплатёж HOT Pay\n\nСейчас: {amount} за {days} дн.\nДалее: {amount} каждые {days} дн.\n\n'
            'Первый платёж оплачивает период и подключает автопродление. Оплаченный остаток сохраняется. '
            'Следующее списание — по окончании оплаченного срока. Комиссия, если есть, показана на странице оплаты.',
        ).format(amount=format_price_kopeks(quote['amount_kopeks']), days=quote['period_days'])
        + '\n\n'
        + details,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [_button(texts.t('HOTREC_PAY', 'Оплатить период и подключить'), f'hotrec:pay:{token}')],
                [_button(texts.BACK, 'menu_subscription')],
            ]
        ),
    )


async def purchase_payhot_from_tariff(callback, db_user, db, state):
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
        await _choose_method(
            callback.message.edit_text,
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
            payhot_error_text(texts, error),
            show_alert=True,
        )
        return
    await callback.answer()


async def handle_payhot_recurrent(callback, db_user, db, state):
    texts = get_texts(db_user.language)
    try:
        parts = callback.data.split(':')
        action = parts[1]
        if (
            action in ('menu', 'quote', 'cancel')
            and await state.get_state() == SubscriptionStates.entering_payhot_period.state
        ):
            await state.set_state(None)
            await state.update_data(payhot_period_subscription_id=None)
        if action == 'pay':
            consent = (await state.get_data()).get('payhot_consent') or {}
            if consent.get('token') != parts[2]:
                raise ValueError('stale_consent')
            result = await purchase_payhot_quote(
                db, user=db_user, quote_id=consent['quote_id'], checkout_id=consent['checkout_id'], channel='telegram'
            )
            target_id = result['subscription_id']
        elif action == 'method':
            target_id = int(parts[2]) or None
            choice = (await state.get_data()).get('payhot_selection') or {}
            if choice.get('subscription_id') != target_id or choice.get('token') != parts[4]:
                raise ValueError('stale_selection')
            if (
                target_id is not None
                and await db.scalar(
                    select(Subscription.id).where(Subscription.id == target_id, Subscription.user_id == db_user.id)
                )
                is None
            ):
                raise ValueError('subscription_not_found')
            selection = {k: v for k, v in choice.items() if k != 'token'}
            await _show_quote(callback, db_user, db, state, payment_method=parts[3], **selection)
            await callback.answer()
            return
        else:
            target_id = int(parts[2])
            target = await db.scalar(
                select(Subscription).where(Subscription.id == target_id, Subscription.user_id == db_user.id)
            )
            if target is None:
                raise ValueError('subscription_not_found')
            if action == 'cancel':
                await cancel_payhot_agreement(db, user_id=db_user.id, subscription_id=target_id, reason='customer')
            elif action == 'select':
                tariff = await db.get(Tariff, target.tariff_id) if target.tariff_id else None
                policy = await get_payhot_recurrent_policy(db, user=db_user, channel='telegram')
                if (
                    not policy['enabled']
                    or target.status == 'disabled'
                    or not tariff_has_payhot_period(tariff)
                    or (tariff is None or not tariff.can_purchase_custom_days())
                ):
                    raise ValueError('period_unavailable')
                minimum, maximum = tariff.min_days, tariff.max_days
                if minimum > maximum:
                    raise ValueError('period_unavailable')
                await state.update_data(payhot_period_subscription_id=target.id, payhot_consent=None)
                await state.set_state(SubscriptionStates.entering_payhot_period)
                await callback.message.edit_text(
                    texts.t('HOTREC_ENTER_DAYS', 'Введите целое число дней от {min} до {max}.').format(
                        min=minimum, max=maximum
                    ),
                    reply_markup=types.InlineKeyboardMarkup(
                        inline_keyboard=[[_button(texts.BACK, f'hotrec:menu:{target.id}')]]
                    ),
                )
                await callback.answer()
                return
            elif action == 'quote':
                await _choose_method(
                    callback.message.edit_text,
                    db_user,
                    db,
                    state,
                    tariff_id=target.tariff_id,
                    period_days=int(parts[3]),
                    device_limit=None,
                    subscription_id=target_id,
                )
                await callback.answer()
                return
            elif action != 'menu':
                raise ValueError('unknown_action')
        info = await get_payhot_agreement_state(db, user_id=db_user.id, subscription_id=target_id)
        rows = []
        status = info['status']
        message = format_payhot_status(info, texts)
        if status in ('CREATING', 'CREATION_UNKNOWN', 'PENDING') and info.get('redirect_url'):
            rows.append([_button(texts.t('HOTREC_CONTINUE', 'Перейти к оплате'), url=info['redirect_url'])])
        if status in LIVE_STATUSES:
            rows.append([_button(texts.t('HOTREC_CANCEL', 'Отключить автоплатёж'), f'hotrec:cancel:{target_id}')])
        else:
            target = await db.get(Subscription, target_id)
            tariff = await db.get(Tariff, target.tariff_id) if target.tariff_id else None
            policy = await get_payhot_recurrent_policy(db, user=db_user, channel='telegram')
            if policy['enabled'] and target.status != 'disabled' and tariff_has_payhot_period(tariff):
                if tariff is not None and not tariff.is_daily and tariff.can_purchase_custom_days():
                    rows.append(
                        [_button(texts.t('HOTREC_SELECT_PERIOD', 'Выбрать срок'), f'hotrec:select:{target_id}')]
                    )
                from app.config import settings

                periods = (
                    ([1] if tariff.is_daily else tariff.get_available_periods())
                    if tariff
                    else settings.get_available_renewal_periods()
                )
                for days in periods:
                    if days > 0:
                        rows.append(
                            [
                                _button(
                                    texts.t('HOTREC_PERIOD', '{days} дн.').format(days=days),
                                    f'hotrec:quote:{target_id}:{days}',
                                )
                            ]
                        )
        rows.append([_button(texts.t('HOTREC_REFRESH', 'Обновить'), f'hotrec:menu:{target_id}')])
        rows.append([_button(texts.BACK, 'menu_subscription')])
        await callback.message.edit_text(message, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows))
    except (ValueError, KeyError, IndexError, TypeError) as error:
        await db.rollback()
        await callback.answer(
            payhot_error_text(texts, error),
            show_alert=True,
        )
        return
    await callback.answer()


async def process_payhot_period(message, db_user, db, state):
    """Custom recurring period for an owned subscription, not a new tariff purchase."""
    texts = get_texts(db_user.language)
    try:
        target_id = (await state.get_data()).get('payhot_period_subscription_id')
        target = await db.scalar(
            select(Subscription).where(Subscription.id == target_id, Subscription.user_id == db_user.id)
        )
        if target is None:
            raise ValueError('subscription_not_found')
        tariff = await db.get(Tariff, target.tariff_id) if target.tariff_id else None
        if not tariff_has_payhot_period(tariff) or (tariff is None or not tariff.can_purchase_custom_days()):
            raise ValueError('period_unavailable')
        minimum, maximum = tariff.min_days, tariff.max_days
        if minimum > maximum:
            raise ValueError('period_unavailable')
        value = (message.text or '').strip()
        if not value.isdecimal() or len(value) > 7 or not minimum <= int(value) <= maximum:
            await message.answer(
                texts.t('HOTREC_ENTER_DAYS', 'Введите целое число дней от {min} до {max}.').format(
                    min=minimum, max=maximum
                )
            )
            return
        await _choose_method(
            message.answer,
            db_user,
            db,
            state,
            tariff_id=target.tariff_id,
            period_days=int(value),
            device_limit=None,
            subscription_id=target.id,
        )
        await state.set_state(None)
        await state.update_data(payhot_period_subscription_id=None)
    except (ValueError, KeyError, TypeError) as error:
        await db.rollback()
        await message.answer(payhot_error_text(texts, error))
