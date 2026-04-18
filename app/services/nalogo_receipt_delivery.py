from __future__ import annotations

import asyncio
import io
from html import escape

import structlog
from aiogram import Bot
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from app.bot_factory import create_bot
from app.utils.cache import cache


logger = structlog.get_logger(__name__)


def _use_russian(language: str | None) -> bool:
    return not language or language.lower().startswith(('ru', 'uk', 'ua'))


def _build_receipt_subject(language: str | None) -> str:
    if _use_russian(language):
        return 'Ваш чек'
    return 'Your receipt'


def _build_receipt_email_body(receipt_url: str, receipt_uuid: str, language: str | None) -> str:
    safe_url = escape(receipt_url, quote=True)
    safe_uuid = escape(receipt_uuid)
    if _use_russian(language):
        return (
            '<p>Здравствуйте!</p>'
            '<p>Ваш чек сформирован в системе «Мой налог».</p>'
            f'<p><b>UUID чека:</b> {safe_uuid}</p>'
            f'<p><a href="{safe_url}">Открыть и скачать чек</a></p>'
        )

    return (
        '<p>Hello!</p>'
        '<p>Your receipt has been created in the Moy Nalog system.</p>'
        f'<p><b>Receipt UUID:</b> {safe_uuid}</p>'
        f'<p><a href="{safe_url}">Open and download receipt</a></p>'
    )


def _build_receipt_telegram_caption(receipt_url: str, receipt_uuid: str, language: str | None) -> str:
    safe_url = escape(receipt_url)
    safe_uuid = escape(receipt_uuid)
    if _use_russian(language):
        return (
            '🧾 <b>Ваш чек сформирован</b>\n\n'
            f'UUID: <code>{safe_uuid}</code>\n'
            f'Ссылка на чек:\n{safe_url}'
        )

    return (
        '🧾 <b>Your receipt is ready</b>\n\n'
        f'UUID: <code>{safe_uuid}</code>\n'
        f'Receipt link:\n{safe_url}'
    )


def _build_receipt_keyboard(language: str | None, receipt_url: str) -> InlineKeyboardMarkup:
    if _use_russian(language):
        button_text = 'Открыть чек'
    else:
        button_text = 'Open receipt'

    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=button_text, url=receipt_url)]]
    )


def _build_receipt_qr(receipt_url: str) -> BufferedInputFile | None:
    try:
        import qrcode
    except ImportError:
        logger.warning('qrcode library is not available; receipt QR will be skipped')
        return None

    try:
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(receipt_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color='black', back_color='white')

        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        return BufferedInputFile(buffer.getvalue(), filename='receipt_qr.png')
    except Exception as error:
        logger.warning('Failed to generate receipt QR', error=error)
        return None


def _delivery_cache_key(receipt_uuid: str, channel: str) -> str:
    return f'nalogo:delivery:v2:{receipt_uuid}:{channel}'


async def _send_receipt_via_email(
    *,
    email: str,
    receipt_url: str,
    receipt_uuid: str,
    language: str | None,
) -> bool:
    from app.cabinet.services.email_service import email_service

    cache_key = _delivery_cache_key(receipt_uuid, 'email')
    if await cache.get(cache_key):
        logger.info('Receipt email already delivered, skipping duplicate send', receipt_uuid=receipt_uuid, to_email=email)
        return True

    if not email_service.is_configured():
        logger.info('Email service is not configured; receipt email skipped', to_email=email)
        return False

    subject = _build_receipt_subject(language)
    body_html = _build_receipt_email_body(receipt_url, receipt_uuid, language)

    sent = await asyncio.to_thread(
        email_service.send_email,
        to_email=email,
        subject=subject,
        body_html=body_html,
    )

    if sent:
        await cache.set(cache_key, True, expire=365 * 24 * 3600)
        logger.info('Receipt email sent', to_email=email, receipt_uuid=receipt_uuid)
    else:
        logger.warning('Failed to send receipt email', to_email=email, receipt_uuid=receipt_uuid)

    return bool(sent)


async def _send_receipt_via_telegram(
    *,
    telegram_id: int,
    receipt_url: str,
    receipt_uuid: str,
    language: str | None,
    bot: Bot | None = None,
) -> bool:
    cache_key = _delivery_cache_key(receipt_uuid, 'telegram')
    if await cache.get(cache_key):
        logger.info(
            'Receipt Telegram notification already delivered, skipping duplicate send',
            telegram_id=telegram_id,
            receipt_uuid=receipt_uuid,
        )
        return True

    text = _build_receipt_telegram_caption(receipt_url, receipt_uuid, language)
    keyboard = _build_receipt_keyboard(language, receipt_url)
    qr_photo = _build_receipt_qr(receipt_url)

    async def _dispatch(active_bot: Bot) -> None:
        if qr_photo:
            await active_bot.send_photo(
                chat_id=telegram_id,
                photo=qr_photo,
                caption=text,
                reply_markup=keyboard,
            )
        else:
            await active_bot.send_message(
                chat_id=telegram_id,
                text=text,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )

    try:
        if bot is not None:
            await _dispatch(bot)
        else:
            async with create_bot() as temp_bot:
                await _dispatch(temp_bot)

        await cache.set(cache_key, True, expire=365 * 24 * 3600)
        logger.info('Receipt Telegram notification sent', telegram_id=telegram_id, receipt_uuid=receipt_uuid)
        return True
    except Exception as error:
        logger.warning(
            'Failed to send receipt Telegram notification',
            telegram_id=telegram_id,
            receipt_uuid=receipt_uuid,
            error=error,
        )
        return False


async def deliver_nalogo_receipt(
    *,
    receipt_url: str,
    receipt_uuid: str,
    telegram_id: int | None = None,
    email: str | None = None,
    language: str | None = 'ru',
    bot: Bot | None = None,
) -> dict[str, bool]:
    result = {'email': False, 'telegram': False}

    if email:
        result['email'] = await _send_receipt_via_email(
            email=email,
            receipt_url=receipt_url,
            receipt_uuid=receipt_uuid,
            language=language,
        )

    if telegram_id:
        result['telegram'] = await _send_receipt_via_telegram(
            telegram_id=telegram_id,
            receipt_url=receipt_url,
            receipt_uuid=receipt_uuid,
            language=language,
            bot=bot,
        )

    if not email and not telegram_id:
        logger.info('Receipt delivery skipped: no buyer contacts available', receipt_uuid=receipt_uuid)

    return result
