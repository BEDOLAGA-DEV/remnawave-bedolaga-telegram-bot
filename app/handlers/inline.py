import logging
from aiogram import Dispatcher, types
from aiogram.types import InlineQueryResultArticle, InputTextMessageContent
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.localization.texts import get_texts
from app.config import settings

logger = logging.getLogger(__name__)


async def handle_inline_query(
    inline_query: types.InlineQuery,
    db_user: User | None = None,
    db: AsyncSession | None = None,
) -> None:
    """Обработчик inline-запросов для появления бота в подсказках @"""
    
    query = inline_query.query.strip().lower() if inline_query.query else ""
    texts = get_texts(db_user.language if db_user else "ru")
    
    # Получаем правильный username бота
    try:
        bot_info = await inline_query.bot.get_me()
        bot_username = bot_info.username or settings.BOT_USERNAME or "bot_username"
    except Exception:
        bot_username = settings.BOT_USERNAME or "bot_username"
    
    # Получаем реферальный код пользователя, если он есть
    referral_code = None
    if db_user:
        if hasattr(db_user, 'referral_code') and db_user.referral_code:
            referral_code = db_user.referral_code
        else:
            # Если кода нет, пробуем сгенерировать его на лету
            try:
                from app.database.crud.user import create_unique_referral_code
                referral_code = await create_unique_referral_code(db)
                db_user.referral_code = referral_code
                logger.info(f"🆕 Сгенерирован отсутствующий реферальный код для {db_user.telegram_id}: {referral_code}")
                # Middleware сделает commit в конце
            except Exception as e:
                logger.warning(f"Не удалось сгенерировать реферальный код для inline: {e}")
    
    # Формируем start параметры: если есть реферальный код, используем его для всех ссылок
    # (в start.py реферальный код обрабатывается автоматически)
    trial_start_param = referral_code if referral_code else "trial"
    vpn_start_param = referral_code if referral_code else settings.INLINE_VPN_NAME.lower()
    
    results = []
    
    # Получаем данные о самом дешёвом пакете из реальных тарифов
    min_price_kopeks = settings.PRICE_14_DAYS
    min_period_days = 14
    min_traffic_gb = 0  # 0 = безлимит
    min_devices = settings.DEFAULT_DEVICE_LIMIT
    
    if db and settings.is_tariffs_mode():
        try:
            from app.database.crud.tariff import get_tariffs_for_user
            promo_group_id = getattr(db_user, 'promo_group_id', None) if db_user else None
            tariffs = await get_tariffs_for_user(db, promo_group_id)
            
            if tariffs:
                # Находим самый дешёвый тариф
                cheapest_tariff = None
                cheapest_price = None
                cheapest_period = None
                
                for tariff in tariffs:
                    if not tariff.is_active:
                        continue
                    prices = getattr(tariff, 'period_prices', None) or {}
                    if prices:
                        for period_str, price_kopeks in prices.items():
                            period_days = int(period_str)
                            if cheapest_price is None or price_kopeks < cheapest_price:
                                cheapest_price = price_kopeks
                                cheapest_period = period_days
                                cheapest_tariff = tariff
                
                if cheapest_tariff and cheapest_price:
                    min_price_kopeks = cheapest_price
                    min_period_days = cheapest_period
                    min_traffic_gb = cheapest_tariff.traffic_limit_gb
                    min_devices = cheapest_tariff.device_limit
        except Exception as e:
            logger.warning(f"Не удалось получить данные о тарифах для inline: {e}")
    
    # URL для миниатюр (можно настроить в settings или использовать публичные URL)
    referral_thumbnail_url = getattr(settings, 'INLINE_REFERRAL_THUMBNAIL_URL', None)
    trial_thumbnail_url = getattr(settings, 'INLINE_TRIAL_THUMBNAIL_URL', None)
    genvpn_thumbnail_url = getattr(settings, 'INLINE_GENVPN_THUMBNAIL_URL', None)
    
    if any([referral_thumbnail_url, trial_thumbnail_url, genvpn_thumbnail_url]):
        logger.info(f"🖼️ Inline thumbnails URLs: ref={referral_thumbnail_url}, trial={trial_thumbnail_url}, vpn={genvpn_thumbnail_url}")
    
    # 1. Реферальная ссылка (только если у пользователя есть реферальный код и программа включена)
    if referral_code and settings.is_referral_program_enabled():
        referral_link = f"https://t.me/{bot_username}?start={referral_code}"
        min_topup = settings.REFERRAL_MINIMUM_TOPUP_KOPEKS / 100
        referral_article = InlineQueryResultArticle(
            id="referral",
            title="👥 Пригласить друга",
            description=f"Получи {texts.format_price(settings.REFERRAL_INVITER_BONUS_KOPEKS)} + {settings.REFERRAL_COMMISSION_PERCENT}% с пополнений",
            input_message_content=InputTextMessageContent(
                message_text=(
                    "👥 <b>Пригласи друга и получи бонус!</b>\n\n"
                    "🎁 <b>Что вы получите за приведённого друга:</b>\n\n"
                    f"💰 <b>При первом пополнении реферала от {min_topup:.0f}₽:</b>\n"
                    f"• Вы получаете: <b>{texts.format_price(settings.REFERRAL_INVITER_BONUS_KOPEKS)}</b>\n"
                    f"• Реферал получает: <b>{texts.format_price(settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS)}</b>\n\n"
                    f"💵 <b>Комиссия с каждого пополнения:</b>\n"
                    f"• Вы получаете: <b>{settings.REFERRAL_COMMISSION_PERCENT}%</b> от суммы пополнения\n\n"
                    "🔗 <b>Ваша реферальная ссылка:</b>\n"
                    f"<code>{referral_link}</code>\n\n"
                    "Приглашайте друзей и зарабатывайте!"
                ),
                parse_mode="HTML"
            ),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text="🚀 Запустить VPN",
                            url=referral_link
                        )
                    ]
                ]
            )
        )
        if referral_thumbnail_url:
            referral_article.thumbnail_url = referral_thumbnail_url
        results.append(referral_article)
    
    # 2. Тестовая подписка (пробный период)
    trial_days_text = "день" if settings.TRIAL_DURATION_DAYS == 1 else "дня" if settings.TRIAL_DURATION_DAYS < 5 else "дней"
    trial_traffic_text = "Безлимит" if settings.TRIAL_TRAFFIC_LIMIT_GB == 0 else f"{settings.TRIAL_TRAFFIC_LIMIT_GB} ГБ"
    trial_article = InlineQueryResultArticle(
        id="trial",
        title="🧪 Тестовая подписка",
        description=f"Пробный период: {settings.TRIAL_DURATION_DAYS} {trial_days_text} • {trial_traffic_text} • {settings.TRIAL_DEVICE_LIMIT} устройства",
        input_message_content=InputTextMessageContent(
            message_text=(
                "🧪 <b>Тестовая подписка</b>\n\n"
                "<b>Условия пробного периода:</b>\n"
                f"📅 <b>Срок:</b> {settings.TRIAL_DURATION_DAYS} {trial_days_text}\n"
                f"📊 <b>Трафик:</b> {trial_traffic_text}\n"
                f"📱 <b>Устройства:</b> {settings.TRIAL_DEVICE_LIMIT}\n"
                f"🌍 <b>Серверы:</b> Доступны все серверы\n\n"
                "🎁 <i>Попробуй наш VPN бесплатно!</i>\n\n"
                "Нажмите кнопку, чтобы активировать:"
            ),
            parse_mode="HTML"
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text="🧪 Активировать тестовую подписку",
                        url=f"https://t.me/{bot_username}?start={trial_start_param}"
                    )
                ]
            ]
        )
    )
    # Если нет URL для картинки, Telegram автоматически использует первый символ из title (эмодзи)
    if trial_thumbnail_url:
        trial_article.thumbnail_url = trial_thumbnail_url
    results.append(trial_article)
    
    # 3. VPN - Мобильный интернет
    min_price_rub = min_price_kopeks / 100
    period_text = f"{min_period_days} дней" if min_period_days < 30 else f"{min_period_days // 30} мес"
    traffic_text = "Безлимитный трафик" if min_traffic_gb == 0 else f"{min_traffic_gb} ГБ"
    device_text = f"{min_devices} устройство" if min_devices == 1 else f"{min_devices} устройства"
    
    vpn_article = InlineQueryResultArticle(
        id="vpn_info",
        title=f"🚀 {settings.INLINE_VPN_NAME} - Мобильный интернет",
        description=f"От {min_price_rub:.0f}₽ за {period_text} • {traffic_text} • {device_text}",
        input_message_content=InputTextMessageContent(
            message_text=(
                f"<b>{settings.INLINE_VPN_NAME} - Мобильный интернет 🌐</b>\n\n"
                "🚀 <b>Быстрый и безопасный VPN</b>\n"
                "Защита данных и доступ к любым ресурсам\n\n"
                "📱 <b>Для всех устройств</b>\n"
                "Одна подписка — телефоны, компьютеры, ТВ\n\n"
                "💰 <b>Доступные пакеты:</b>\n"
                f"• От {min_price_rub:.0f}₽ за {period_text}\n"
                f"• {traffic_text}\n"
                f"• {device_text} включено\n"
                "• Все серверы доступны\n\n"
                "⚡ <b>Стабильная скорость</b>\n"
                "Серверы по всему миру\n\n"
                "🔒 <b>Полная анонимность</b>\n"
                "Твои данные под надёжной защитой"
            ),
            parse_mode="HTML"
        ),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text="Открыть бота",
                        url=f"https://t.me/{bot_username}?start={vpn_start_param}"
                    )
                ]
            ]
        )
    )
    # Если нет URL для картинки, Telegram автоматически использует первый символ из title (эмодзи)
    if genvpn_thumbnail_url:
        genvpn_article.thumbnail_url = genvpn_thumbnail_url
    results.append(genvpn_article)
    
    try:
        await inline_query.answer(
            results=results,
            cache_time=300,  # Кэш на 5 минут
            is_personal=True  # Результаты персональные (для реферальной ссылки)
        )
        logger.info(f"Inline query обработан для пользователя {inline_query.from_user.id}, запрос: '{query}'")
    except Exception as e:
        logger.error(f"Ошибка обработки inline query: {e}")


def register_handlers(dp: Dispatcher):
    """Регистрация обработчиков inline-запросов"""
    dp.inline_query.register(handle_inline_query)
    logger.info("✅ Inline query handlers зарегистрированы")
