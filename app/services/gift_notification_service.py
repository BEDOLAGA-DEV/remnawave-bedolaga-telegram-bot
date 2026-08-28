"""Presentation and notification services for gift purchases (Telegram & Cabinet).

Security and Privacy Constraints:
- Full internal gift tokens must never appear as standalone credentials, in callback data,
  logs, or fallback output. They may appear only inside a canonical Cabinet claim URL.
- The canonical public gift code is safe to show to the authenticated sender in purchase
  results and gift history.
- Financial details (price, balance, discount amount, transaction id) must NEVER be included
  in recipient copy, share text, or the final gift result message.
- Dynamic fields (e.g. tariff names) must be HTML escaped when formatting HTML messages.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

import structlog
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.localization.texts import Texts, get_texts
from app.services.gift_purchase_service import GiftPurchaseResult
from app.utils.gift_links import (
    GiftClaimArtifacts,
    _normalize_bot_username,
    _normalize_cabinet_url,
    build_gift_claim_artifacts,
    build_telegram_gift_share_url,
)


if TYPE_CHECKING:
    from aiogram import Bot, types

    from app.database.models import User
    from app.services.gift_history_service import GiftHistoryItem

logger = structlog.get_logger(__name__)


async def resolve_gift_claim_channel(
    bot: Bot | None = None,
    bot_username: str | None = None,
    cabinet_url: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve and validate available claim channels (bot username and/or cabinet URL).

    Resolution precedence for bot username:
    1. Explicit `bot_username` argument if valid.
    2. Synchronized `settings.get_bot_username()` if valid.
    3. `await bot.get_me()` recovery if bot instance is supplied.

    Resolution precedence for cabinet URL:
    1. Explicit `cabinet_url` argument if valid.
    2. `settings.CABINET_URL` if configured and valid.

    Returns:
        tuple[bot_username, cabinet_url]: Normalized channel strings or None if unavailable/invalid.
    """
    resolved_bot_username = bot_username or settings.get_bot_username()
    if not resolved_bot_username and bot is not None:
        try:
            me = await bot.get_me()
            resolved_bot_username = me.username
        except Exception as err:
            logger.debug('Failed to resolve bot username via bot.get_me()', error=str(err))
            resolved_bot_username = None

    if resolved_bot_username:
        try:
            resolved_bot_username = _normalize_bot_username(resolved_bot_username)
        except Exception:
            resolved_bot_username = None
    else:
        resolved_bot_username = None

    resolved_cabinet_url = cabinet_url or getattr(settings, 'CABINET_URL', None)
    if resolved_cabinet_url:
        try:
            resolved_cabinet_url = _normalize_cabinet_url(resolved_cabinet_url)
        except Exception:
            resolved_cabinet_url = None
    else:
        resolved_cabinet_url = None

    return resolved_bot_username, resolved_cabinet_url


def _format_claim_link_and_action_buttons(
    texts: Texts,
    artifacts: GiftClaimArtifacts,
    share_url: str,
) -> tuple[str, list[list[InlineKeyboardButton]]]:
    """Format claim link display text and action buttons supporting both, bot-only, and cabinet-only configs."""
    buttons: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=texts.t('GIFT_SEND_BUTTON', '🎁 Отправить подарок'),
                url=share_url,
            )
        ]
    ]

    if artifacts.bot_claim_url and artifacts.cabinet_claim_url:
        bot_label = texts.t('GIFT_BOT_CLAIM_LINK_LABEL', '🤖 В Telegram:')
        cabinet_label = texts.t('GIFT_CABINET_CLAIM_LINK_LABEL', '🌐 В личном кабинете:')
        claim_link_display = f'{bot_label}\n{artifacts.bot_claim_url}\n\n{cabinet_label}\n{artifacts.cabinet_claim_url}'
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('GIFT_OPEN_BOT_BUTTON', '🤖 Открыть в боте'),
                    url=artifacts.bot_claim_url,
                ),
                InlineKeyboardButton(
                    text=texts.t('GIFT_OPEN_CABINET_BUTTON', '🌐 Открыть в кабинете'),
                    url=artifacts.cabinet_claim_url,
                ),
            ]
        )
    elif artifacts.bot_claim_url:
        claim_link_display = artifacts.bot_claim_url
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('GIFT_OPEN_BUTTON', '🔗 Открыть подарок'),
                    url=artifacts.bot_claim_url,
                )
            ]
        )
    elif artifacts.cabinet_claim_url:
        claim_link_display = artifacts.cabinet_claim_url
        buttons.append(
            [
                InlineKeyboardButton(
                    text=texts.t('GIFT_OPEN_BUTTON', '🔗 Открыть подарок'),
                    url=artifacts.cabinet_claim_url,
                )
            ]
        )
    else:
        raise ValueError('Cannot build gift presentation: neither bot deep link nor cabinet URL available')

    return claim_link_display, buttons


def build_gift_result_presentation(
    language: str,
    purchase_result: GiftPurchaseResult,
    bot_username: str | None = None,
    cabinet_url: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build localized text and inline keyboard for gift purchase result.

    Excludes all financial details (prices, balance, discounts, transaction IDs)
    and standalone internal bearer tokens. Dynamic strings are HTML escaped.

    Args:
        language: User language code (e.g. 'ru', 'en', 'ua', 'fa', 'zh').
        purchase_result: Result of balance purchase or idempotent replay.
        bot_username: Normalized bot username (preferred deep-link channel).
        cabinet_url: Normalized cabinet base URL (fallback channel).

    Returns:
        tuple[text, keyboard]: Localized HTML message text and inline keyboard.

    Raises:
        ValueError: If neither bot_username nor cabinet_url can form a valid claim URL.
    """
    token = purchase_result.purchase.token
    texts = get_texts(language)
    quote = purchase_result.quote

    # Localized share text for native Telegram chat-picker (NO financial details, NO standalone token)
    share_text = texts.t(
        'GIFT_SHARE_TEXT',
        '🎁 Привет! Я дарю тебе подписку на {tariff_name} ({period_days} дн.). Нажми на ссылку, чтобы активировать!',
    ).format(
        tariff_name=quote.tariff_name,
        period_days=quote.period_days,
    )

    artifacts = build_gift_claim_artifacts(
        token=token,
        bot_username=bot_username,
        cabinet_url=cabinet_url,
        share_text=share_text,
    )

    claim_link = artifacts.bot_claim_url or artifacts.cabinet_claim_url
    if not claim_link:
        raise ValueError('Cannot build gift result presentation: neither bot deep link nor cabinet URL available')

    share_url = artifacts.telegram_share_url or build_telegram_gift_share_url(claim_link, share_text)
    claim_link_display, action_buttons = _format_claim_link_and_action_buttons(texts, artifacts, share_url)

    # Localized message body (HTML formatted)
    escaped_tariff_name = html.escape(quote.tariff_name)
    traffic_str = (
        texts.format_traffic(quote.traffic_limit_gb)
        if quote.traffic_limit_gb is not None
        else texts.t('GIFT_TRAFFIC_UNLIMITED', '∞ (безлимит)')
    )
    devices_str = texts.format_device_limit(quote.device_limit)

    if purchase_result.is_idempotent_replay:
        body_template = texts.t(
            'GIFT_PURCHASE_REPLAY_TEXT',
            '🎁 <b>Подарок уже был оформлен ранее</b>\n\n'
            '📦 Тариф: <b>{tariff_name}</b>\n'
            '📅 Период: <b>{period_days} дн.</b>\n'
            '📊 Трафик: <b>{traffic}</b>\n'
            '📱 Устройства: <b>{devices}</b>\n\n'
            '🔑 Код подарка: <code>{public_code}</code>\n\n'
            '🔗 Ссылка на подарок:\n{claim_link}\n\n'
            'Отправьте эту ссылку получателю или воспользуйтесь кнопкой «Отправить подарок» ниже.',
        )
    else:
        body_template = texts.t(
            'GIFT_PURCHASE_SUCCESS_TEXT',
            '🎁 <b>Подарок успешно оформлен!</b>\n\n'
            '📦 Тариф: <b>{tariff_name}</b>\n'
            '📅 Период: <b>{period_days} дн.</b>\n'
            '📊 Трафик: <b>{traffic}</b>\n'
            '📱 Устройства: <b>{devices}</b>\n\n'
            '🔑 Код подарка: <code>{public_code}</code>\n\n'
            '🔗 Ссылка на подарок:\n{claim_link}\n\n'
            'Отправьте эту ссылку получателю или воспользуйтесь кнопкой «Отправить подарок» ниже. '
            'Получатель сможет активировать подписку в один клик.',
        )

    text = body_template.format(
        tariff_name=escaped_tariff_name,
        period_days=quote.period_days,
        traffic=traffic_str,
        devices=devices_str,
        public_code=artifacts.public_code,
        claim_link=claim_link_display,
    )

    buttons = [
        *action_buttons,
        [
            InlineKeyboardButton(
                text=texts.t('GIFT_BACK_TO_SUBSCRIPTION_BUTTON', '◀️ К подписке'),
                callback_data='menu_subscription',
            ),
            InlineKeyboardButton(
                text=texts.t('BACK_TO_MAIN_MENU_BUTTON', '⬅️ В главное меню'),
                callback_data='back_to_menu',
            ),
        ],
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    return text, keyboard


def build_gift_history_detail_presentation(
    language: str,
    item: GiftHistoryItem,
    bot_username: str | None = None,
    cabinet_url: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build localized HTML text and inline keyboard for sender gift detail view.

    Source-neutral: renders identical structure for gifts bought via Telegram bot,
    web cabinet, or landing.
    Excludes all financial details (prices, balance, discounts, transaction IDs)
    and standalone internal bearer tokens. Dynamic strings are HTML escaped.

    For claimable gifts:
    - Displays canonical public code and claim link
    - Provides share URL button and direct claim link button(s)
    - Back button with callback 'gift_my_back'

    For delivered/activated gifts:
    - Displays delivery status, delivery timestamp, safe recipient metadata
    - Omits public code, claim links, and share/open buttons
    - Back button with callback 'gift_my_back'
    """
    texts = get_texts(language)
    escaped_tariff_name = html.escape(item.tariff_name or texts.t('GIFT_TARIFF_DELETED', 'Архивный тариф'))
    traffic_str = (
        texts.format_traffic(item.traffic_limit_gb)
        if item.traffic_limit_gb is not None
        else texts.t('GIFT_TRAFFIC_UNLIMITED', '∞ (безлимит)')
    )
    devices_str = texts.format_device_limit(item.device_limit)
    created_str = item.created_at.strftime('%d.%m.%Y %H:%M') if item.created_at else '—'

    if item.is_claimable:
        share_text = texts.t(
            'GIFT_SHARE_TEXT',
            '🎁 Привет! Я дарю тебе подписку на {tariff_name} ({period_days} дн.). Нажми на ссылку, чтобы активировать!',
        ).format(
            tariff_name=item.tariff_name or texts.t('GIFT_TARIFF_DEFAULT_NAME', 'VPN'),
            period_days=item.period_days,
        )

        artifacts = build_gift_claim_artifacts(
            token=item.token,
            bot_username=bot_username,
            cabinet_url=cabinet_url,
            share_text=share_text,
        )

        claim_link = artifacts.bot_claim_url or artifacts.cabinet_claim_url
        if not claim_link:
            raise ValueError('Cannot build gift detail presentation: neither bot deep link nor cabinet URL available')

        share_url = artifacts.telegram_share_url or build_telegram_gift_share_url(claim_link, share_text)
        claim_link_display, action_buttons = _format_claim_link_and_action_buttons(texts, artifacts, share_url)
        status_text = texts.t('GIFT_STATUS_PENDING', '⏳ Ожидает активации')

        body_template = texts.t(
            'GIFT_MY_DETAIL_CLAIMABLE_TEXT',
            '🎁 <b>Подарок</b>\n\n'
            '📦 Тариф: <b>{tariff_name}</b>\n'
            '📅 Период: <b>{period_days} дн.</b>\n'
            '📊 Трафик: <b>{traffic}</b>\n'
            '📱 Устройства: <b>{devices}</b>\n'
            '📋 Статус: <b>{status_text}</b>\n'
            '📅 Оформлен: <b>{created_at}</b>\n'
            '🔑 Код подарка: <code>{public_code}</code>\n\n'
            '🔗 Ссылка на подарок:\n{claim_link}\n\n'
            'Отправьте эту ссылку или код получателю для активации подписки.',
        )

        text = body_template.format(
            tariff_name=escaped_tariff_name,
            period_days=item.period_days,
            traffic=traffic_str,
            devices=devices_str,
            status_text=status_text,
            created_at=created_str,
            public_code=artifacts.public_code,
            claim_link=claim_link_display,
        )

        # Два способа передать подарок вживую, когда переслать сообщение нельзя:
        # показать QR с камеры и отдать готовый текст одним нажатием. Ссылка и код
        # выше остаются — они для тех, кому удобнее скопировать вручную.
        buttons = [
            *action_buttons,
            [
                InlineKeyboardButton(
                    text=texts.t('GIFT_QR_BUTTON', '📱 QR-код подарка'),
                    callback_data=f'gift_my_qr:{item.purchase_id}',
                ),
                InlineKeyboardButton(
                    text=texts.t('GIFT_COPY_TEXT_BUTTON', '📋 Текст для отправки'),
                    callback_data=f'gift_my_text:{item.purchase_id}',
                ),
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('GIFT_MY_BACK_BUTTON', '◀️ К списку подарков'),
                    callback_data='gift_my_back',
                )
            ],
        ]
        return text, InlineKeyboardMarkup(inline_keyboard=buttons)

    # Delivered / other final state
    status_text = texts.t('GIFT_STATUS_DELIVERED', '✅ Активирован')
    delivered_line = ''
    if item.delivered_at:
        deliv_str = item.delivered_at.strftime('%d.%m.%Y %H:%M')
        delivered_line = texts.t('GIFT_MY_DELIVERED_AT_LINE', '🎉 Активирован: <b>{delivered_at}</b>\n').format(
            delivered_at=deliv_str
        )

    recipient_line = ''
    if item.recipient_display:
        escaped_recipient = html.escape(item.recipient_display)
        recipient_line = texts.t('GIFT_MY_RECIPIENT_LINE', '👤 Получатель: <b>{recipient_display}</b>\n').format(
            recipient_display=escaped_recipient
        )

    body_template = texts.t(
        'GIFT_MY_DETAIL_DELIVERED_TEXT',
        '🎁 <b>Подарок</b>\n\n'
        '📦 Тариф: <b>{tariff_name}</b>\n'
        '📅 Период: <b>{period_days} дн.</b>\n'
        '📊 Трафик: <b>{traffic}</b>\n'
        '📱 Устройства: <b>{devices}</b>\n'
        '📋 Статус: <b>{status_text}</b>\n'
        '📅 Оформлен: <b>{created_at}</b>\n'
        '{delivered_line}{recipient_line}',
    )

    text = body_template.format(
        tariff_name=escaped_tariff_name,
        period_days=item.period_days,
        traffic=traffic_str,
        devices=devices_str,
        status_text=status_text,
        created_at=created_str,
        delivered_line=delivered_line,
        recipient_line=recipient_line,
    ).rstrip()

    buttons = [
        [
            InlineKeyboardButton(
                text=texts.t('GIFT_MY_BACK_BUTTON', '◀️ К списку подарков'),
                callback_data='gift_my_back',
            )
        ]
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


async def send_gift_result_message(
    bot: Bot,
    user: User,
    purchase_result: GiftPurchaseResult,
    bot_username: str | None = None,
    cabinet_url: str | None = None,
) -> types.Message | None:
    """Send localized gift result presentation directly to user's Telegram chat.

    Used after top-up auto-purchase or direct balance confirmation.
    """
    resolved_bot_username, resolved_cabinet_url = await resolve_gift_claim_channel(
        bot=bot,
        bot_username=bot_username,
        cabinet_url=cabinet_url,
    )

    try:
        text, keyboard = build_gift_result_presentation(
            language=user.language,
            purchase_result=purchase_result,
            bot_username=resolved_bot_username,
            cabinet_url=resolved_cabinet_url,
        )
    except Exception as err:
        logger.error('Failed to build gift result presentation for notification', user_id=user.id, error=str(err))
        return None

    target_chat_id = user.telegram_id or user.id
    try:
        return await bot.send_message(
            chat_id=target_chat_id,
            text=text,
            reply_markup=keyboard,
            parse_mode='HTML',
        )
    except Exception as err:
        logger.error('Failed to send gift result message to user chat', user_id=user.id, error=str(err))
        return None
