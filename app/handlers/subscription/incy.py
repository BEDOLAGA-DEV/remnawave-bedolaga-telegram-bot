from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.types import InaccessibleMessage, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

import structlog

from app.config import settings
from app.database.models import User
from app.keyboards.inline import (
    get_incy_download_linux_arch_keyboard,
    get_incy_download_linux_pkg_keyboard,
    get_incy_download_link_keyboard,
    get_incy_download_macos_keyboard,
    get_incy_download_platform_keyboard,
)
from app.localization.texts import get_texts
from app.services.incy_release_service import get_incy_desktop_assets
from app.utils.incy_link import encrypt_incy_link
from app.utils.subscription_utils import (
    apply_subscription_domain_override,
    build_scheme_redirect_link,
)

from .common import resolve_subscription_from_context

logger = structlog.get_logger(__name__)


async def handle_connect_incy(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None
):
    """Render the INCY connect screen: tappable deep link + copy block + redirect."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    subscription, sub_id = await resolve_subscription_from_context(callback, db_user, db, state)
    if subscription is None:
        await callback.answer(
            texts.t('SUBSCRIPTION_LINK_UNAVAILABLE', '❌ Ссылка подписки недоступна'),
            show_alert=True,
        )
        return

    plain_url = apply_subscription_domain_override(getattr(subscription, 'subscription_url', None))
    if not plain_url:
        await callback.answer(
            texts.t('SUBSCRIPTION_NO_ACTIVE_LINK', '⚠ У вас нет активной подписки или ссылка еще генерируется'),
            show_alert=True,
        )
        return

    deep_link = encrypt_incy_link(plain_url, name=settings.get_incy_subscription_name())
    redirect = build_scheme_redirect_link(deep_link, settings.get_incy_connect_redirect_template())

    message_text = (
        texts.t('INCY_CONNECT_TITLE', '🔗 <b>Подключение через INCY</b>')
        + '\n\n'
        + f'<a href="{deep_link}">INCY</a>'
        + '\n\n'
        + texts.t('INCY_CONNECT_HINT', '💡 Нажмите ссылку, чтобы открыть INCY, или скопируйте её вручную:')
        + '\n\n'
        + f'<blockquote expandable><code>{deep_link}</code></blockquote>'
    )

    rows: list[list[InlineKeyboardButton]] = []
    if redirect:
        rows.append([InlineKeyboardButton(text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'), url=redirect, style='success')])
    rows.append([InlineKeyboardButton(text=texts.t('INCY_DOWNLOAD_BUTTON', '⬇️ Скачать INCY'), callback_data='nz!_incy_dl', style='primary')])
    back_cb = f'nz!_sm:{sub_id}' if (sub_id is not None and settings.is_multi_tariff_enabled()) else 'nz!_menu_subscription'
    rows.append([InlineKeyboardButton(text=texts.BACK, callback_data=back_cb, style='danger')])

    await callback.message.answer(
        message_text,
        parse_mode='HTML',
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


async def handle_incy_download(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext = None
):
    """Drive the INCY per-platform download tree.

    callback.data shape: ``nz!_incy_dl[:<platform>[:<arch>[:<pkg>]]]``.
    """
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    data = callback.data or 'nz!_incy_dl'

    if data == 'nz!_incy_dl_close':
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.answer()
        return

    parts = data.split(':')  # ['nz!_incy_dl', platform?, arch?, pkg?]
    segments = parts[1:]

    # Entry — show platform menu.
    if not segments:
        await callback.message.edit_text(
            texts.t('INCY_DOWNLOAD_PROMPT', '📥 <b>Скачать INCY</b>\nВыберите вашу платформу:'),
            reply_markup=get_incy_download_platform_keyboard(db_user.language),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    platform = segments[0]

    # macOS / Linux intermediate menus
    if platform == 'macos' and len(segments) == 1:
        await callback.message.edit_text(
            texts.t('INCY_DOWNLOAD_PROMPT', '📥 <b>Скачать INCY</b>\nВыберите вашу платформу:'),
            reply_markup=get_incy_download_macos_keyboard(db_user.language),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    if platform == 'linux' and len(segments) == 1:
        await callback.message.edit_text(
            texts.t('INCY_DOWNLOAD_PROMPT', '📥 <b>Скачать INCY</b>\nВыберите вашу платформу:'),
            reply_markup=get_incy_download_linux_arch_keyboard(db_user.language),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    if platform == 'linux' and len(segments) == 2:
        arch = segments[1]
        await callback.message.edit_text(
            texts.t('INCY_DOWNLOAD_PROMPT', '📥 <b>Скачать INCY</b>\nВыберите вашу платформу:'),
            reply_markup=get_incy_download_linux_pkg_keyboard(db_user.language, arch),
            parse_mode='HTML',
        )
        await callback.answer()
        return

    # Leaf nodes -> resolve a URL.
    link = await _resolve_incy_download_url(segments)
    if not link:
        await callback.answer(
            texts.t('INCY_DOWNLOAD_LINK_NOT_SET', '❌ Ссылка для этой платформы временно недоступна'),
            show_alert=True,
        )
        return

    await callback.message.edit_text(
        texts.t('INCY_DOWNLOAD_PROMPT', '📥 <b>Скачать INCY</b>\nВыберите вашу платформу:'),
        reply_markup=get_incy_download_link_keyboard(db_user.language, link),
        parse_mode='HTML',
    )
    await callback.answer()


async def _resolve_incy_download_url(segments: list[str]) -> str | None:
    """Map a leaf callback path to a download URL (store links or release asset)."""
    platform = segments[0]
    if platform == 'android':
        return settings.get_incy_android_url()
    if platform == 'ios':
        return settings.get_incy_ios_url()

    assets = await get_incy_desktop_assets()
    key = ':'.join(segments)  # e.g. 'windows', 'macos:arm', 'linux:x64:rpm'
    return assets.get(key)
