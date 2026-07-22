"""Доставка промопредложений на email — для аккаунтов без telegram_id.

Промопредложения (шаблоны и broadcast из кабинета, рассылка из бота) уходили
только в Telegram: email-only юзеры получали оффер в БД, но не узнавали о нём.
Этот модуль — общий email-канал для обоих путей: кабинетного
``/admin/promo-offers/broadcast`` и бот-хендлера ``_send_offer_to_users``.

Текст письма — тот же, что и в Telegram (Telegram-HTML b/i/code — валидный
HTML-фрагмент; переносы строк конвертируются в <br>). Кнопке «Получить»
соответствует ссылка на кабинет: оффер уже создан в БД и активируется там.
Поддерживается DB-override шаблона (email_templates, тип ``promo_offer``).
"""

import asyncio

import structlog


logger = structlog.get_logger(__name__)


def _to_email_html(text: str | None) -> str:
    """Telegram-текст → HTML-фрагмент письма (переносы строк → <br>)."""
    return (text or '').replace('\n', '<br>')


async def send_promo_offer_email(
    *,
    email: str,
    language: str | None,
    username: str = '',
    message_text: str | None,
    valid_hours: int,
    discount_percent: int = 0,
    bonus_amount_kopeks: int = 0,
) -> bool:
    """Шлёт одно промопредложение на почту. True — письмо реально отправлено.

    Принимает только скалярные значения (не ORM-объекты) — безопасно вызывать
    из detached background-задачи после закрытия сессии запроса.
    """
    from app.cabinet.services.email_service import email_service
    from app.cabinet.services.email_templates import EmailNotificationTemplates
    from app.config import settings
    from app.services.notification_delivery_service import NotificationType

    if not email:
        return False
    if not email_service.is_configured():
        logger.debug('SMTP не настроен — промо-письмо пропущено', email=email)
        return False

    language = language or 'ru'
    context = {
        'cabinet_url': getattr(settings, 'CABINET_URL', '') or '',
        'username': username or '',
        'email': email,
        'message_html': _to_email_html(message_text),
        'valid_hours': valid_hours,
        'discount_percent': discount_percent,
        'bonus_amount_kopeks': bonus_amount_kopeks,
    }
    if bonus_amount_kopeks:
        context['amount'] = settings.format_price(bonus_amount_kopeks)

    # DB-override шаблона (раздел email_templates в кабинете), затем дефолтный
    template = None
    try:
        from app.cabinet.services.email_template_overrides import get_rendered_override

        rendered = await get_rendered_override(NotificationType.PROMO_OFFER.value, language, context)
        if rendered:
            template = {'subject': rendered[0], 'body_html': rendered[1]}
    except Exception as e:
        logger.debug('Не удалось проверить override промо-шаблона', e=e)

    if not template:
        template = EmailNotificationTemplates().get_template(NotificationType.PROMO_OFFER, language, context)
    if not template:
        logger.warning('Не найден email-шаблон промопредложения', language=language)
        return False

    try:
        # send_email — sync smtplib, уводим в thread чтобы не блокировать event loop
        success = await asyncio.to_thread(
            email_service.send_email,
            to_email=email,
            subject=template['subject'],
            body_html=template['body_html'],
            body_text=template.get('body_text'),
        )
    except Exception as e:
        logger.error('Ошибка отправки промо-письма', email=email, e=e)
        return False

    if success:
        logger.info('Промопредложение отправлено на email', email=email)
    return bool(success)
