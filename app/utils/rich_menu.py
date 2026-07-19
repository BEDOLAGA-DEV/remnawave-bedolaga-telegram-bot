"""Rich-рендер главного меню через rich-сообщения Bot API 10.1 (aiogram 3.29+).

Главное меню собирается в rich-HTML (заголовки, таблица подписок, details-блоки,
tg-time с датами в таймзоне клиента, footer) и отправляется через sendRichMessage /
editMessageText(rich_message=...). Все try_*-хелперы возвращают bool: False означает
«rich не отрисован» — вызывающий код обязан показать классическое меню.

Fallback-модель повторяет happ-crypt паттерн из app/external/remnawave_api.py:
после первого ответа сервера «метод неизвестен» (устаревший self-hosted
telegram-bot-api) модуль запоминает недоступность до рестарта и больше не
пытается. Ошибки конкретного рендера (например, неотредактированное сообщение)
на флаг не влияют — просто отдаём False и меню рисуется классикой.

Ограничение: у rich-сообщения нет фото, поэтому при ENABLE_LOGO_MODE главное
меню в rich-режиме показывается без логотипа, а переходы меню <-> разделы с
логотипом идут через delete+send (существующие fallback-и photo_message).
"""

import html
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import structlog
from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
)
from aiogram.methods import EditMessageText
from aiogram.types import (
    CallbackQuery,
    InaccessibleMessage,
    InlineKeyboardMarkup,
    InputRichMessage,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import get_all_subscriptions_by_user_id
from app.database.crud.tariff import get_tariff_by_id
from app.database.crud.user_message import get_random_active_message
from app.database.models import User
from app.utils.miniapp_buttons import build_miniapp_startapp_url
from app.utils.promo_offer import build_promo_offer_hint, build_test_access_hint
from app.utils.subscription_utils import get_happ_cryptolink_redirect_link
from app.utils.timezone import format_local_datetime


logger = structlog.get_logger(__name__)

_RTL_LANGUAGES = frozenset({'ar', 'fa', 'he'})
_PROGRESS_BAR_LENGTH = 10

# Сервер не поддерживает rich-сообщения (устаревший self-hosted bot-api).
# Взводится один раз до рестарта — по образцу _happ_encrypt_unavailable.
_rich_unavailable = False

# Сервер отклонил message_effect_id (например, эффект отключили или id невалиден) —
# дальше шлём меню без эффекта, не роняя rich-рендер в классику.
_effect_unavailable = False

# Telegram не смог скачать логотип по URL (нет публичного доступа, битый файл) —
# дальше собираем меню без логотипа, не роняя rich-рендер в классику.
_logo_unavailable = False

# Маркеры ошибок загрузки медиа по URL со стороны Telegram.
_MEDIA_FETCH_ERROR_MARKERS = (
    'http url',
    'webpage_',
    'media_empty',
    'photo_invalid',
    'image_process',
    'wrong type of the web page',
)

# Теги, которые допускает sanitize_html, но не понимает rich-HTML: спойлерный
# span конвертируем в родной <tg-spoiler>, прочие span разворачиваем (содержимое
# остаётся), img выкидываем целиком.
_SPOILER_SPAN_RE = re.compile(
    r'<span\s+class=(["\'])tg-spoiler\1[^>]*>(.*?)</span>',
    re.IGNORECASE | re.DOTALL,
)
_SPAN_TAG_RE = re.compile(r'</?span[^>]*>', re.IGNORECASE)
_IMG_TAG_RE = re.compile(r'<img[^>]*/?>', re.IGNORECASE)


def is_rich_menu_enabled() -> bool:
    return bool(settings.MAIN_MENU_RICH_ENABLED) and not _rich_unavailable


def _reset_rich_menu_availability() -> None:
    """Сбрасывает флаги недоступности (используется в тестах)."""
    global _rich_unavailable, _effect_unavailable, _logo_unavailable
    _rich_unavailable = False
    _effect_unavailable = False
    _logo_unavailable = False


def _resolve_rich_logo_url() -> str:
    """Публичный URL логотипа для шапки rich-меню ('' — без логотипа).

    Явный MAIN_MENU_RICH_LOGO_URL приоритетнее. Иначе, если задан WEBHOOK_URL
    (публичный origin нашего FastAPI) и файл LOGO_FILE существует, логотип
    отдаётся собственным эндпоинтом /cabinet/branding/bot-logo.
    """
    if _logo_unavailable:
        return ''

    explicit = (settings.MAIN_MENU_RICH_LOGO_URL or '').strip()
    if explicit:
        return explicit

    webhook_url = (settings.WEBHOOK_URL or '').strip()
    if not webhook_url or not settings.LOGO_FILE or not Path(settings.LOGO_FILE).is_file():
        return ''
    parsed = urlparse(webhook_url)
    if not parsed.scheme or not parsed.netloc:
        return ''
    return f'{parsed.scheme}://{parsed.netloc}/cabinet/branding/bot-logo'


def _is_media_fetch_error(error: Exception) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in _MEDIA_FETCH_ERROR_MARKERS)


def _mark_logo_unavailable_once(error: Exception) -> bool:
    """Взводит флаг «логотип не загружается». True — если флаг только что взвёлся
    (можно один раз пересобрать меню без логотипа и повторить отправку)."""
    global _logo_unavailable
    if _logo_unavailable:
        return False
    _logo_unavailable = True
    logger.warning(
        'Telegram не смог загрузить логотип rich-меню по URL — меню отправляется без логотипа',
        error=str(error),
    )
    return True


def _mark_rich_unavailable(error: Exception) -> None:
    global _rich_unavailable
    if not _rich_unavailable:
        logger.warning(
            'Bot API сервер не поддерживает rich-сообщения — главное меню переключено на классический рендер',
            error=str(error),
        )
    _rich_unavailable = True


def _looks_like_unsupported(error: Exception) -> bool:
    """Отличает «сервер не знает про rich» от ошибок конкретного рендера.

    Устаревший telegram-bot-api отвечает 404 Not Found на неизвестный метод
    (sendRichMessage) и 'message text is empty' на editMessageText без text.
    """
    if isinstance(error, TelegramNotFound):
        return True
    text = str(error).lower()
    return 'unknown method' in text or 'method not found' in text or 'text is empty' in text


# Telegram хранит даты 32-битным unix time: tg-time со значением вне диапазона
# сервер отклоняет ошибкой RICH_MESSAGE_DATE_INVALID и меню не отправляется.
# Реальный кейс — «вечные» подписки, импортированные из панели с датой окончания
# после 19.01.2038; такие даты показываем fallback-текстом без tg-time.
_TG_TIME_MAX_UNIX = 2**31 - 1


def _tg_time(moment: datetime, time_format: str, fallback: str) -> str:
    unix_time = int(moment.timestamp())
    if not 0 < unix_time <= _TG_TIME_MAX_UNIX:
        return html.escape(fallback)
    return f'<tg-time unix="{unix_time}" format="{time_format}">{html.escape(fallback)}</tg-time>'


def _progress_bar(seconds_left: float, total_seconds: float) -> str:
    # Тот же вид [████░░░░░░], что у таймеров промо-предложений (app/utils/promo_offer.py).
    if total_seconds <= 0:
        total_seconds = seconds_left or 1
    ratio = max(0.0, min(1.0, seconds_left / total_seconds))
    filled = int(round(ratio * _PROGRESS_BAR_LENGTH))
    filled = max(0, min(_PROGRESS_BAR_LENGTH, filled))
    if filled == 0 and seconds_left > 0:
        filled = 1
    return f'[{"█" * filled}{"░" * (_PROGRESS_BAR_LENGTH - filled)}]'


def _rich_status_label(texts, actual_status: str, is_trial: bool) -> str:
    if actual_status == 'limited':
        return texts.t('MAIN_MENU_RICH_STATUS_LIMITED', '🟡 Лимит трафика')
    if actual_status == 'expired':
        return texts.t('MAIN_MENU_RICH_STATUS_EXPIRED', '🔴 Истекла')
    if actual_status == 'disabled':
        return texts.t('MAIN_MENU_RICH_STATUS_DISABLED', '⚫ Отключена')
    if actual_status == 'pending':
        return texts.t('MAIN_MENU_RICH_STATUS_PENDING', '⏳ Ожидает')
    if is_trial or actual_status == 'trial':
        return texts.t('MAIN_MENU_RICH_STATUS_TRIAL', '🎁 Тестовая')
    if actual_status == 'active':
        return texts.t('MAIN_MENU_RICH_STATUS_ACTIVE', '🟢 Активна')
    return texts.t('SUB_STATUS_UNKNOWN', '❓ Неизвестно')


def _sanitize_rich_inline(value: str) -> str:
    """Приводит sanitize_html-вывод (случайные сообщения админа) к rich-HTML."""
    value = _SPOILER_SPAN_RE.sub(r'<tg-spoiler>\2</tg-spoiler>', value)
    value = _SPAN_TAG_RE.sub('', value)
    return _IMG_TAG_RE.sub('', value)


def _renew_link(subscription_id: int | None, texts) -> str:
    """Ссылка «Продлить» для истёкшей подписки — открывает раздел подписок кабинета.

    Текстовая ссылка не умеет web_app, поэтому единственный путь в Mini App из
    текста — t.me/<bot>/<app>?startapp=… (нужен MINIAPP_APP_SHORT_NAME). Параметр
    разбирает StartParamNavigator кабинета: renew_<id> → /subscriptions/<id>/renew,
    subscriptions → /subscriptions. Только в cabinet-режиме; иначе ''.
    """
    if not settings.is_cabinet_mode():
        return ''
    start_param = f'renew_{subscription_id}' if subscription_id else 'subscriptions'
    url = build_miniapp_startapp_url(start_param)
    if not url:
        return ''
    label = html.escape(texts.t('MAIN_MENU_RICH_RENEW', '🔄 Продлить'))
    return f'<a href="{url}">{label}</a>'


def _traffic_usage_text(subscription, texts) -> str:
    used = texts.format_traffic(float(getattr(subscription, 'traffic_used_gb', 0) or 0), is_limit=False)
    limit = texts.format_traffic(float(getattr(subscription, 'traffic_limit_gb', 0) or 0), is_limit=True)
    return f'{used} / {limit}'


def _connect_url(subscription) -> str:
    """URL мгновенного подключения подписки для текстовой ссылки.

    В happ-режиме — https-обёртка редиректа над crypto-ссылкой (сырой happ://
    в <a href> rich-HTML не поддерживается); иначе — страница подписки
    subscription_url, если оператор не скрыл прямые ссылки.
    """
    if settings.is_happ_cryptolink_mode():
        crypto_link = getattr(subscription, 'subscription_crypto_link', None)
        redirect_link = get_happ_cryptolink_redirect_link(crypto_link) if crypto_link else None
        if redirect_link:
            return redirect_link
    if settings.should_hide_subscription_link():
        return ''
    return getattr(subscription, 'subscription_url', None) or ''


def _connect_link(subscription, texts) -> str:
    url = _connect_url(subscription)
    if not url:
        return ''
    label = html.escape(texts.t('MAIN_MENU_RICH_CONNECT', '⚡ Подключить'))
    return f'<a href="{html.escape(url, quote=True)}"><b>{label}</b></a>'


def _trial_offer_link(user: User, texts) -> str:
    """Ссылка «Активировать триал» для нового юзера без использованного триала.

    Бесплатный триал — диплинк t.me/<bot>?start=trial (обрабатывается в
    start.py: активация + перерисовка меню с новой подпиской). Платный триал
    (TRIAL_PAYMENT_ENABLED + цена) активируется только через оплату — ссылка
    ведёт в миниапп-кабинет (дашборд с TrialOfferCard, startapp=trial).
    """
    if settings.TRIAL_DURATION_DAYS <= 0 or settings.TRIAL_DISABLED_FOR == 'all':
        return ''
    if settings.is_trial_disabled_for_user(getattr(user, 'auth_type', None)):
        return ''
    try:
        if user.is_trial_already_used():
            return ''
    except Exception as error:
        logger.debug('Не удалось проверить доступность триала для rich-меню', error=str(error))
        return ''

    if settings.is_trial_paid_activation_enabled():
        url = build_miniapp_startapp_url('trial')
    else:
        bot_username = settings.get_bot_username()
        url = f'https://t.me/{bot_username}?start=trial' if bot_username else ''
    if not url:
        return ''

    label = html.escape(texts.t('MAIN_MENU_RICH_TRIAL_BUTTON', '🚀 Активировать триал'))
    return f'<a href="{html.escape(url, quote=True)}"><b>{label}</b></a>'


def _build_subscriptions_table(subscriptions, texts) -> str:
    if not subscriptions:
        return f'<p>{html.escape(texts.t("SUB_STATUS_NONE", "❌ Отсутствует"))}</p>'

    current_time = datetime.now(UTC)
    header = (
        '<tr>'
        f'<th>{html.escape(texts.t("MAIN_MENU_RICH_TABLE_TARIFF", "Тариф"))}</th>'
        f'<th>{html.escape(texts.t("MAIN_MENU_RICH_TABLE_STATUS", "Статус"))}</th>'
        f'<th>{html.escape(texts.t("MAIN_MENU_RICH_TABLE_UNTIL", "Действует до"))}</th>'
        '</tr>'
    )
    tariff_fallback = texts.t('MAIN_MENU_RICH_TARIFF_FALLBACK', 'Подписка')
    rows = [header]
    for subscription in subscriptions:
        tariff_name = html.escape(subscription.tariff.name if subscription.tariff else tariff_fallback)
        actual_status = (subscription.actual_status or '').lower()
        status_label = _rich_status_label(texts, actual_status, bool(getattr(subscription, 'is_trial', False)))

        end_date = getattr(subscription, 'end_date', None)
        end_date_text = format_local_datetime(end_date, '%d.%m.%Y') if end_date else ''
        if end_date and end_date > current_time and actual_status in {'active', 'trial', 'limited'}:
            days_left = (end_date - current_time).days
            days_text = texts.t('MAIN_MENU_RICH_DAYS_LEFT', 'осталось {days} дн.').replace('{days}', str(days_left))
            until_cell = f'{_tg_time(end_date, "d", end_date_text)} ({html.escape(days_text)})'
        elif end_date:
            until_cell = _tg_time(end_date, 'd', end_date_text)
        else:
            until_cell = '—'

        rows.append(
            f'<tr><td>{tariff_name}</td><td>{html.escape(status_label)}</td><td align="right">{until_cell}</td></tr>'
        )

        # Нижняя строка ряда: расход + «кнопки» действий. Отдельная узкая колонка
        # действий не влезает на мобильных (таблица уезжает за край экрана) —
        # colspan-строка видна всегда.
        if actual_status in {'active', 'trial', 'limited'}:
            usage_parts = [f'📊 {html.escape(_traffic_usage_text(subscription, texts))}']
            device_limit = getattr(subscription, 'device_limit', None)
            if device_limit:
                usage_parts.append(f'📱 {device_limit}')
            connect_link = _connect_link(subscription, texts)
            if connect_link:
                usage_parts.append(connect_link)
            rows.append(f'<tr><td colspan="3">{" · ".join(usage_parts)}</td></tr>')
        elif actual_status == 'expired':
            renew_link = _renew_link(getattr(subscription, 'id', None), texts)
            if renew_link:
                rows.append(f'<tr><td colspan="3">{renew_link}</td></tr>')

    return f'<table bordered striped>{"".join(rows)}</table>'


async def _build_single_subscription_block(user: User, texts, db: AsyncSession) -> str:
    # Статусные строки берём из того же builder-а, что и классическое меню, —
    # единый источник правды для формулировок (см. tests/test_start_menu_text_consistency.py).
    from app.handlers.menu import _get_subscription_status

    subscription = getattr(user, 'subscription', None)
    if not subscription:
        return f'<p>{html.escape(texts.t("SUB_STATUS_NONE", "❌ Отсутствует"))}</p>'

    is_daily_tariff = False
    tariff_line = ''
    if settings.is_tariffs_mode() and subscription.tariff_id:
        try:
            tariff = await get_tariff_by_id(db, subscription.tariff_id)
        except Exception as error:
            tariff = None
            logger.debug('Не удалось загрузить тариф для rich-меню', error=str(error))
        if tariff:
            is_daily_tariff = bool(getattr(tariff, 'is_daily', False))
            tariff_template = texts.t('MAIN_MENU_RICH_TARIFF', '📦 Тариф: {tariff}')
            tariff_line = html.escape(tariff_template).replace('{tariff}', f'<b>{html.escape(tariff.name)}</b>')

    status_text = _get_subscription_status(user, texts, is_daily_tariff)
    lines = [html.escape(line) for line in status_text.split('\n') if line.strip()]
    if tariff_line:
        lines.append(tariff_line)

    current_time = datetime.now(UTC)
    end_date = getattr(subscription, 'end_date', None)
    start_date = getattr(subscription, 'start_date', None)
    actual_status = (subscription.actual_status or '').lower()
    if not is_daily_tariff and end_date and end_date > current_time and actual_status in {'active', 'trial'}:
        seconds_left = (end_date - current_time).total_seconds()
        total_seconds = (end_date - start_date).total_seconds() if start_date else 0
        relative_template = texts.t('MAIN_MENU_RICH_EXPIRES_RELATIVE', '⏳ истекает {when}')
        days_left_text = texts.t('MAIN_MENU_RICH_DAYS_LEFT', 'осталось {days} дн.').replace(
            '{days}', str(max((end_date - current_time).days, 0))
        )
        relative_line = html.escape(relative_template).replace('{when}', _tg_time(end_date, 'r', days_left_text))
        lines.append(f'<code>{_progress_bar(seconds_left, total_seconds)}</code> {relative_line}')

    if actual_status in {'active', 'trial', 'limited'}:
        traffic_template = texts.t('MAIN_MENU_RICH_TRAFFIC', '📊 Трафик: {traffic}')
        lines.append(
            html.escape(traffic_template).replace('{traffic}', html.escape(_traffic_usage_text(subscription, texts)))
        )
        device_limit = getattr(subscription, 'device_limit', None)
        if device_limit:
            devices_template = texts.t('MAIN_MENU_RICH_DEVICES', '📱 Устройства: {devices}')
            lines.append(html.escape(devices_template).replace('{devices}', str(device_limit)))
        connect_link = _connect_link(subscription, texts)
        if connect_link:
            lines.append(connect_link)

    if actual_status == 'expired':
        renew_link = _renew_link(getattr(subscription, 'id', None), texts)
        if renew_link:
            lines.append(renew_link)

    return '<blockquote>' + '<br>'.join(lines) + '</blockquote>'


async def build_main_menu_rich_html(user: User, texts, db: AsyncSession) -> str:
    """Собирает rich-HTML главного меню (контент, без клавиатуры)."""
    blocks: list[str] = []

    logo_url = _resolve_rich_logo_url()
    if logo_url:
        blocks.append(f'<img src="{html.escape(logo_url, quote=True)}"/>')

    user_name = html.escape(user.full_name or '')
    blocks.append(f'<h4>👤 {user_name}</h4>')
    blocks.append('<hr/>')

    if settings.is_multi_tariff_enabled():
        heading = texts.t('MAIN_MENU_RICH_SUBSCRIPTIONS_HEADING', '📱 Подписки')
        subscriptions = await get_all_subscriptions_by_user_id(db, user.id)
        subscription_block = _build_subscriptions_table(subscriptions, texts)
        if len(subscriptions) > 1 and settings.MAIN_MENU_RICH_SUBSCRIPTIONS_COLLAPSIBLE:
            # Несколько подписок раздувают меню — сворачиваем таблицу в details;
            # summary служит заголовком (h6 не дублируем), счётчик — вместо содержимого.
            summary = f'<b>{html.escape(heading)} ({len(subscriptions)})</b>'
            blocks.append(f'<details><summary>{summary}</summary>{subscription_block}</details>')
        else:
            blocks.append(f'<h6>{html.escape(heading)}</h6>')
            blocks.append(subscription_block)
    else:
        heading = texts.t('MAIN_MENU_RICH_SUBSCRIPTION_HEADING', '📱 Подписка')
        blocks.append(f'<h6>{html.escape(heading)}</h6>')
        blocks.append(await _build_single_subscription_block(user, texts, db))

    trial_link = _trial_offer_link(user, texts)
    if trial_link:
        blocks.append(f'<p>{trial_link}</p>')

    balance_template = texts.t('MAIN_MENU_RICH_BALANCE', '💰 Баланс: {balance}')
    balance_value = f'<b>{html.escape(settings.format_price(user.balance_kopeks))}</b>'
    blocks.append(f'<p>{html.escape(balance_template).replace("{balance}", balance_value)}</p>')

    hint_sections: list[str] = []
    try:
        promo_hint = await build_promo_offer_hint(db, user, texts)
        if promo_hint:
            hint_sections.append(promo_hint.strip())
    except Exception as hint_error:
        logger.debug('Не удалось построить подсказку промо-предложения для rich-меню', hint_error=hint_error)
    try:
        test_access_hint = await build_test_access_hint(db, user, texts)
        if test_access_hint:
            hint_sections.append(test_access_hint.strip())
    except Exception as test_error:
        logger.debug('Не удалось построить подсказку тестового доступа для rich-меню', test_error=test_error)

    if hint_sections:
        summary = texts.t('MAIN_MENU_RICH_HINTS_SUMMARY', '💡 Акции и подсказки')
        # Строки подсказок содержат только inline-теги (<code>{bar}</code>) — переносы
        # превращаем в отдельные параграфы внутри details-блока.
        inner = ''.join(f'<p>{line}</p>' for section in hint_sections for line in section.split('\n') if line.strip())
        blocks.append(f'<details open><summary>{html.escape(summary)}</summary>{inner}</details>')

    try:
        random_message = await get_random_active_message(db)
    except Exception as error:
        random_message = None
        logger.error('Ошибка получения случайного сообщения для rich-меню', error=error)
    if random_message:
        # Rich-HTML живёт по правилам HTML: перенос строки — только через <br>.
        random_message_html = _sanitize_rich_inline(random_message).replace('\n', '<br>')
        blocks.append(f'<blockquote>{random_message_html}</blockquote>')

    blocks.append('<hr/>')
    action_prompt = texts.t('MAIN_MENU_ACTION_PROMPT', 'Выберите действие:')
    blocks.append(f'<footer>{html.escape(action_prompt)}</footer>')

    return ''.join(blocks)


def _input_rich_message(rich_html: str, language: str | None) -> InputRichMessage:
    return InputRichMessage(
        html=rich_html,
        is_rtl=True if (language or '').lower() in _RTL_LANGUAGES else None,
        skip_entity_detection=True,
    )


async def _send_rich_menu(
    bot: Bot,
    chat_id: int,
    rich_html: str,
    keyboard: InlineKeyboardMarkup,
    language: str | None,
) -> None:
    global _effect_unavailable

    effect_id = (settings.MAIN_MENU_RICH_EFFECT_ID or '').strip() or None
    if _effect_unavailable:
        effect_id = None

    try:
        await bot.send_rich_message(
            chat_id=chat_id,
            rich_message=_input_rich_message(rich_html, language),
            reply_markup=keyboard,
            message_effect_id=effect_id,
        )
    except TelegramBadRequest as error:
        # Невалидный/отключённый эффект не должен ронять rich-меню в классику —
        # повторяем без эффекта и больше его не шлём до рестарта.
        if effect_id and 'effect' in str(error).lower():
            _effect_unavailable = True
            logger.warning(
                'Сервер отклонил message_effect_id — меню отправляется без эффекта',
                effect_id=effect_id,
                error=str(error),
            )
            await bot.send_rich_message(
                chat_id=chat_id,
                rich_message=_input_rich_message(rich_html, language),
                reply_markup=keyboard,
            )
        else:
            raise


async def try_send_rich_main_menu(
    bot: Bot,
    chat_id: int,
    db_user: User,
    texts,
    db: AsyncSession,
    keyboard: InlineKeyboardMarkup,
) -> bool:
    """Отправляет главное меню rich-сообщением. False — показать классическое меню."""
    if not is_rich_menu_enabled():
        return False

    try:
        rich_html = await build_main_menu_rich_html(db_user, texts, db)
    except Exception as error:
        logger.error('Ошибка сборки rich-меню', error=error, user_id=getattr(db_user, 'id', None))
        return False

    try:
        await _send_rich_menu(bot, chat_id, rich_html, keyboard, db_user.language)
        return True
    except TelegramForbiddenError:
        # Пользователь заблокировал бота — классический рендер упадёт так же, не ретраим.
        logger.warning('Не удалось отправить rich-меню: бот заблокирован пользователем', chat_id=chat_id)
        return True
    except (TelegramNotFound, TelegramBadRequest) as error:
        if _looks_like_unsupported(error):
            _mark_rich_unavailable(error)
        elif _is_media_fetch_error(error) and _mark_logo_unavailable_once(error):
            # Логотип не скачался — единственный повтор уже без него (флаг взведён).
            return await try_send_rich_main_menu(bot, chat_id, db_user, texts, db, keyboard)
        else:
            logger.error('Не удалось отправить rich-меню', error=error, chat_id=chat_id)
        return False
    except TelegramNetworkError as error:
        logger.warning('Сетевая ошибка при отправке rich-меню', error=str(error), chat_id=chat_id)
        return False


async def try_answer_rich_main_menu(
    message: Message,
    db_user: User,
    texts,
    db: AsyncSession,
    keyboard: InlineKeyboardMarkup,
) -> bool:
    """Rich-аналог message.answer(menu_text) для /start и завершения регистрации."""
    bot = message.bot
    if bot is None:
        return False
    return await try_send_rich_main_menu(bot, message.chat.id, db_user, texts, db, keyboard)


async def try_edit_rich_main_menu(
    callback: CallbackQuery,
    db_user: User,
    texts,
    db: AsyncSession,
    keyboard: InlineKeyboardMarkup,
) -> bool:
    """Rich-аналог edit_or_answer_photo для callback-навигации. False — рисовать классику."""
    if not is_rich_menu_enabled():
        return False

    message = callback.message
    bot = callback.bot
    if message is None or bot is None:
        return False

    try:
        rich_html = await build_main_menu_rich_html(db_user, texts, db)
    except Exception as error:
        logger.error('Ошибка сборки rich-меню', error=error, user_id=getattr(db_user, 'id', None))
        return False

    chat_id = message.chat.id
    language = db_user.language

    is_editable_as_rich = (
        not isinstance(message, InaccessibleMessage)
        and not getattr(message, 'photo', None)
        and (message.text is not None or getattr(message, 'rich_message', None) is not None)
    )

    try:
        if is_editable_as_rich:
            # parse_mode=None явно: иначе дефолтный parse_mode бота (HTML) сериализуется
            # в запрос рядом с rich_message.
            await bot(
                EditMessageText(
                    chat_id=chat_id,
                    message_id=message.message_id,
                    rich_message=_input_rich_message(rich_html, language),
                    reply_markup=keyboard,
                    parse_mode=None,
                )
            )
        else:
            # Фото/медиа-сообщение (логотип) или недоступное (>48ч) нельзя превратить
            # в rich редактированием — пересоздаём, как это делает edit_or_answer_photo
            # при смене типа сообщения.
            if not isinstance(message, InaccessibleMessage):
                try:
                    await message.delete()
                except (TelegramBadRequest, TelegramForbiddenError) as delete_error:
                    # Например, сообщению больше 48 часов — deleteMessage запрещён, хотя
                    # редактирование ещё работает. Отдаём классическому рендеру: он
                    # отредактирует уцелевшее сообщение на месте и не наплодит дублей.
                    logger.debug('Не удалось удалить сообщение перед rich-меню', error=str(delete_error))
                    return False
            await _send_rich_menu(bot, chat_id, rich_html, keyboard, language)
        return True
    except TelegramForbiddenError:
        logger.warning('Не удалось показать rich-меню: бот заблокирован пользователем', chat_id=chat_id)
        return True
    except (TelegramNotFound, TelegramBadRequest) as error:
        if 'message is not modified' in str(error).lower():
            return True
        if _looks_like_unsupported(error):
            _mark_rich_unavailable(error)
        elif _is_media_fetch_error(error) and _mark_logo_unavailable_once(error):
            # Логотип не скачался — единственный повтор уже без него (флаг взведён).
            return await try_edit_rich_main_menu(callback, db_user, texts, db, keyboard)
        else:
            # Правка не удалась (сообщение удалено/устарело и т.п.) — классический
            # рендер разрулит своей цепочкой фоллбеков (edit_or_answer_photo).
            logger.warning(
                'Не удалось отредактировать rich-меню, фоллбек на классику', error=str(error), chat_id=chat_id
            )
        return False
    except TelegramNetworkError as error:
        logger.warning('Сетевая ошибка при показе rich-меню', error=str(error), chat_id=chat_id)
        return False
