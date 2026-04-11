from aiogram import types
from aiogram.types import InaccessibleMessage
from sqlalchemy.ext.asyncio import AsyncSession

import structlog

from app.config import settings
from app.database.models import User
from app.keyboards.inline import (
    get_happ_download_link_keyboard,
    get_happ_download_platform_keyboard,
    get_happ_fallback_crypt4_keyboard,
    get_happ_fallback_raw_keyboard,
    get_happ_link_not_working_keyboard,
)
from app.localization.texts import get_texts
from app.utils.subscription_utils import (
    build_redhash_url,
    ensure_single_subscription,
)

logger = structlog.get_logger(__name__)


async def handle_happ_download_request(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    prompt_text = texts.t(
        'HAPP_DOWNLOAD_PROMPT',
        '📥 <b>Скачать Happ</b>\nВыберите ваше устройство:',
    )

    keyboard = get_happ_download_platform_keyboard(db_user.language)

    await callback.message.answer(prompt_text, reply_markup=keyboard, parse_mode='HTML')
    await callback.answer()


async def handle_happ_download_platform_choice(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    # Проверяем, доступно ли сообщение для редактирования
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    platform = callback.data.split('_')[-1]
    if platform == 'pc':
        platform = 'windows'
    texts = get_texts(db_user.language)
    link = settings.get_happ_download_link(platform)

    if not link:
        await callback.answer(
            texts.t('HAPP_DOWNLOAD_LINK_NOT_SET', '❌ Ссылка для этого устройства не настроена'),
            show_alert=True,
        )
        return

    platform_names = {
        'ios': texts.t('HAPP_PLATFORM_IOS', '🍎 iOS'),
        'android': texts.t('HAPP_PLATFORM_ANDROID', '🤖 Android'),
        'macos': texts.t('HAPP_PLATFORM_MACOS', '🖥️ Mac OS'),
        'windows': texts.t('HAPP_PLATFORM_WINDOWS', '💻 Windows'),
    }

    link_text = texts.t(
        'HAPP_DOWNLOAD_LINK_MESSAGE',
        '⬇️ Скачайте Happ для {platform}:',
    ).format(platform=platform_names.get(platform, platform.upper()))

    keyboard = get_happ_download_link_keyboard(db_user.language, link)

    await callback.message.edit_text(link_text, reply_markup=keyboard, parse_mode='HTML')
    await callback.answer()


async def handle_happ_download_close(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    try:
        await callback.message.delete()
    except Exception:
        pass

    await callback.answer()


async def handle_happ_download_back(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    # Проверяем, доступно ли сообщение для редактирования
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    prompt_text = texts.t(
        'HAPP_DOWNLOAD_PROMPT',
        '📥 <b>Скачать Happ</b>\nВыберите ваше устройство:',
    )

    keyboard = get_happ_download_platform_keyboard(db_user.language)

    await callback.message.edit_text(prompt_text, reply_markup=keyboard, parse_mode='HTML')
    await callback.answer()


async def handle_happ_link_not_working(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Step 2: Show 'update Happ' advice with a 'Didn't help' button."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    message_text = texts.t(
        'HAPP_LINK_NOT_WORKING_MESSAGE',
        '⚠️ <b>Ссылка не открывается?</b>\n\nПопробуйте:\n• Обновить Happ до последней версии\n• Переустановить Happ\n• Нажать кнопку <b>«Подключиться»</b>, а не копировать ссылку вручную\n\nЕсли не помогло — нажмите кнопку ниже.',
    )

    keyboard = get_happ_link_not_working_keyboard(db_user.language)
    await callback.message.answer(message_text, parse_mode='HTML', reply_markup=keyboard)
    await callback.answer()


async def handle_happ_link_broken_crypt4(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Step 3: Encrypt subscription URL via RemnaWave API (crypt4), wrap in redhash."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    subscription = await ensure_single_subscription(db, db_user.id)
    subscription_url = getattr(subscription, 'subscription_url', None) if subscription else None

    if not subscription_url:
        await callback.answer(
            texts.t('SUBSCRIPTION_LINK_UNAVAILABLE', '❌ Ссылка подписки недоступна'),
            show_alert=True,
        )
        return

    # Call RemnaWave API to get crypt4 encrypted link
    crypt4_link = None
    try:
        from app.services.remnawave_service import RemnaWaveService

        service = RemnaWaveService()
        async with service.get_api_client() as api:
            data = {'linkToEncrypt': subscription_url}
            response = await api._make_request('POST', '/api/system/tools/happ/encrypt', data)
            crypt4_link = response.get('response', {}).get('encryptedLink')
    except Exception as e:
        logger.warning('Failed to get crypt4 link from API', error=str(e))

    if not crypt4_link:
        await callback.answer(
            texts.t('HAPP_LINK_REDHASH_UNAVAILABLE', '❌ Сервис перенаправления не настроен'),
            show_alert=True,
        )
        return

    happ_link = crypt4_link
    redhash_url = build_redhash_url(happ_link)

    if not redhash_url:
        await callback.answer(
            texts.t('HAPP_LINK_REDHASH_UNAVAILABLE', '❌ Сервис перенаправления не настроен'),
            show_alert=True,
        )
        return

    message_text = texts.t(
        'HAPP_LINK_FALLBACK_CRYPT4_MESSAGE',
        '🔄 <b>Альтернативная ссылка</b>\n\nОткройте ссылку ниже в браузере — она автоматически перенаправит в Happ:\n\n<code>{redhash_url}</code>\n\n💡 Нажмите на ссылку, чтобы скопировать, потом вставьте в браузер.',
    ).format(redhash_url=redhash_url)

    keyboard = get_happ_fallback_crypt4_keyboard(db_user.language, redhash_url=redhash_url)
    await callback.message.answer(
        message_text,
        parse_mode='HTML',
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )
    await callback.answer()


async def handle_happ_link_broken_raw(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Step 4: Provide happ://add/{raw_url} wrapped in redhash."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    subscription = await ensure_single_subscription(db, db_user.id)
    subscription_url = getattr(subscription, 'subscription_url', None) if subscription else None

    if not subscription_url:
        await callback.answer(
            texts.t('SUBSCRIPTION_LINK_UNAVAILABLE', '❌ Ссылка подписки недоступна'),
            show_alert=True,
        )
        return

    happ_link = f'happ://add/{subscription_url}'
    redhash_url = build_redhash_url(happ_link)

    if not redhash_url:
        await callback.answer(
            texts.t('HAPP_LINK_REDHASH_UNAVAILABLE', '❌ Сервис перенаправления не настроен'),
            show_alert=True,
        )
        return

    message_text = texts.t(
        'HAPP_LINK_FALLBACK_RAW_MESSAGE',
        '🔗 <b>Прямая ссылка</b>\n\nЕсли ничего не помогло — откройте ссылку ниже в браузере:\n\n<code>{redhash_url}</code>\n\nЕсли и это не работает — обратитесь в поддержку.',
    ).format(redhash_url=redhash_url)

    keyboard = get_happ_fallback_raw_keyboard(db_user.language, redhash_url=redhash_url)
    await callback.message.answer(
        message_text,
        parse_mode='HTML',
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )
    await callback.answer()
