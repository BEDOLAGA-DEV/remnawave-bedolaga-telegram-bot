import asyncio
import base64
import html as html_mod
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import structlog
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import get_traffic_prices, settings
from app.database.models import Subscription, User
from app.localization.texts import get_texts
from app.utils.pricing_utils import (
    apply_percentage_discount,
)
from app.utils.promo_offer import (
    get_user_active_promo_discount_percent,
)


logger = structlog.get_logger(__name__)

TRAFFIC_PRICES = get_traffic_prices()

# ── App config cache ──
_app_config_cache: dict[str, Any] = {}
_app_config_cache_ts: float = 0.0
_app_config_lock = asyncio.Lock()


_PLACEHOLDER_RE = re.compile(r'\{(\w+)\}')


def _format_text_with_placeholders(template: str, values: dict[str, Any]) -> str:
    """Safe placeholder substitution — only replaces simple {key} patterns.

    Unlike str.format_map, this does NOT allow attribute access ({key.attr})
    or indexing ({key[0]}), preventing format string injection attacks.
    """
    if not isinstance(template, str):
        return template

    def _replace(match: re.Match) -> str:
        key = match.group(1)
        if key in values:
            return str(values[key])
        return match.group(0)

    try:
        return _PLACEHOLDER_RE.sub(_replace, template)
    except Exception:  # pragma: no cover - defensive logging
        logger.warning('Failed to format template with values', template=template, values=values)
        return template


def _get_promo_offer_discount_percent(user: User | None) -> int:
    return get_user_active_promo_discount_percent(user)


def _apply_promo_offer_discount(user: User | None, amount: int) -> dict[str, int]:
    percent = _get_promo_offer_discount_percent(user)

    if amount <= 0 or percent <= 0:
        return {'discounted': amount, 'discount': 0, 'percent': 0}

    discounted, discount_value = apply_percentage_discount(amount, percent)
    return {'discounted': discounted, 'discount': discount_value, 'percent': percent}


def _get_period_hint_from_subscription(subscription: Subscription | None) -> int | None:
    if not subscription or not subscription.end_date:
        return None

    now = datetime.now(UTC)
    days_remaining = (subscription.end_date - now).days
    if days_remaining <= 0:
        return None

    return days_remaining


def _apply_discount_to_monthly_component(
    amount_per_month: int,
    percent: int,
    months: int,
) -> dict[str, int]:
    discounted_per_month, discount_per_month = apply_percentage_discount(amount_per_month, percent)

    return {
        'original_per_month': amount_per_month,
        'discounted_per_month': discounted_per_month,
        'discount_percent': max(0, min(100, percent)),
        'discount_per_month': discount_per_month,
        'total': discounted_per_month * months,
        'discount_total': discount_per_month * months,
    }


def update_traffic_prices():
    from app.config import refresh_traffic_prices

    refresh_traffic_prices()
    logger.info('🔄 TRAFFIC_PRICES обновлены из конфигурации')


def format_traffic_display(traffic_gb: int, is_fixed_mode: bool = None) -> str:
    if is_fixed_mode is None:
        is_fixed_mode = settings.is_traffic_fixed()

    if traffic_gb == 0:
        if is_fixed_mode:
            return 'Безлимитный'
        return 'Безлимитный'
    if is_fixed_mode:
        return f'{traffic_gb} ГБ'
    return f'{traffic_gb} ГБ'


def validate_traffic_price(gb: int) -> bool:
    from app.config import settings

    price = settings.get_traffic_price(gb)
    if gb == 0:
        return True

    return price > 0


def get_localized_value(values: Any, language: str, default_language: str = 'en') -> str:
    if not isinstance(values, dict):
        return ''

    candidates: list[str] = []
    normalized_language = (language or '').strip().lower()

    if normalized_language:
        candidates.append(normalized_language)
        if '-' in normalized_language:
            candidates.append(normalized_language.split('-')[0])

    default_language = (default_language or '').strip().lower()
    if default_language and default_language not in candidates:
        candidates.append(default_language)

    for candidate in candidates:
        if not candidate:
            continue
        value = values.get(candidate)
        if isinstance(value, str) and value.strip():
            return value

    for value in values.values():
        if isinstance(value, str) and value.strip():
            return value

    return ''


def render_guide_blocks(blocks: list[dict], language: str) -> str:
    """Render block-format guide steps to HTML text."""
    parts: list[str] = []
    step_num = 1
    for block in blocks:
        if not isinstance(block, dict):
            continue
        title = block.get('title', {})
        desc = block.get('description', {})
        title_text = html_mod.escape(
            get_localized_value(title, language) if isinstance(title, dict) else str(title or '')
        )
        desc_text = html_mod.escape(get_localized_value(desc, language) if isinstance(desc, dict) else str(desc or ''))
        if title_text or desc_text:
            step = f'<b>Шаг {step_num}'
            if title_text:
                step += f' - {title_text}'
            step += ':</b>'
            if desc_text:
                step += f'\n{desc_text}'
            parts.append(step)
            step_num += 1
    return '\n\n'.join(parts)


def build_redirect_link(target_link: str | None, template: str | None) -> str | None:
    if not target_link or not template:
        return None

    normalized_target = str(target_link).strip()
    normalized_template = str(template).strip()

    if not normalized_target or not normalized_template:
        return None

    encoded_target = quote(normalized_target, safe='')
    result = normalized_template
    replaced = False

    replacements = [
        ('{subscription_link}', encoded_target),
        ('{link}', encoded_target),
        ('{subscription_link_raw}', normalized_target),
        ('{link_raw}', normalized_target),
    ]

    for placeholder, replacement in replacements:
        if placeholder in result:
            result = result.replace(placeholder, replacement)
            replaced = True

    if not replaced:
        result = f'{result}{encoded_target}'

    return result


def get_device_name(device_type: str, language: str = 'ru') -> str:
    names = {
        'ios': 'iPhone/iPad',
        'android': 'Android',
        'windows': 'Windows',
        'mac': 'macOS',
        'linux': 'Linux',
        'tv': 'Android TV',
        'appletv': 'Apple TV',
        'apple_tv': 'Apple TV',
    }

    return names.get(device_type, device_type)


# ── Remnawave async config loader ──

_PLATFORM_DISPLAY = {
    'ios': {'name': 'iPhone/iPad', 'emoji': '📱'},
    'android': {'name': 'Android', 'emoji': '🤖'},
    'windows': {'name': 'Windows', 'emoji': '💻'},
    'macos': {'name': 'macOS', 'emoji': '🎯'},
    'linux': {'name': 'Linux', 'emoji': '🐧'},
    'androidTV': {'name': 'Android TV', 'emoji': '📺'},
    'appleTV': {'name': 'Apple TV', 'emoji': '📺'},
}

# Map callback device_type keys to Remnawave platform keys
_DEVICE_TO_PLATFORM = {
    'ios': 'ios',
    'android': 'android',
    'windows': 'windows',
    'mac': 'macos',
    'linux': 'linux',
    'tv': 'androidTV',
    'appletv': 'appleTV',
    'apple_tv': 'appleTV',
}

# Reverse: Remnawave platform key → callback device_type
_PLATFORM_TO_DEVICE = {
    'ios': 'ios',
    'android': 'android',
    'windows': 'windows',
    'macos': 'mac',
    'linux': 'linux',
    'androidTV': 'tv',
    'appleTV': 'appletv',
}

_LEGACY_TO_REMNAWAVE_PLATFORM = {
    'ios': 'ios',
    'android': 'android',
    'windows': 'windows',
    'mac': 'macos',
    'macos': 'macos',
    'linux': 'linux',
    'tv': 'androidTV',
    'androidtv': 'androidTV',
    'appletv': 'appleTV',
    'apple_tv': 'appleTV',
}


def _normalize_localized_text(value: Any, fallback_en: str = '', fallback_ru: str | None = None) -> dict[str, str]:
    if isinstance(value, dict):
        normalized: dict[str, str] = {}
        for key, item in value.items():
            key_str = str(key).strip()
            if not key_str or not isinstance(item, str):
                continue
            cleaned = item.strip()
            if cleaned:
                normalized[key_str] = cleaned
        if normalized:
            return normalized

    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned:
            return {'en': cleaned, 'ru': cleaned}

    result: dict[str, str] = {}
    if fallback_en:
        result['en'] = fallback_en
    if fallback_ru:
        result['ru'] = fallback_ru
    elif fallback_en:
        result['ru'] = fallback_en
    return result


def _legacy_step_to_block(step_data: Any, default_title_en: str, default_title_ru: str) -> dict[str, Any] | None:
    if not isinstance(step_data, dict):
        return None

    block: dict[str, Any] = {
        'title': _normalize_localized_text(step_data.get('title'), default_title_en, default_title_ru),
        'description': _normalize_localized_text(step_data.get('description')),
        'buttons': [],
    }

    for button in step_data.get('buttons', []):
        if not isinstance(button, dict):
            continue
        button_url = str(button.get('buttonLink', '')).strip()
        if not button_url:
            continue

        text_value = button.get('buttonText')
        if isinstance(text_value, dict):
            text = _normalize_localized_text(text_value, 'Open', 'Открыть')
        elif isinstance(text_value, str) and text_value.strip():
            text = {'en': text_value.strip(), 'ru': text_value.strip()}
        else:
            text = {'en': 'Open', 'ru': 'Открыть'}

        block['buttons'].append(
            {
                'type': 'externalLink',
                'text': text,
                'url': button_url,
            }
        )

    if not block['description'] and not block['buttons']:
        return None

    return block


def _normalize_legacy_app(legacy_app: dict[str, Any]) -> dict[str, Any]:
    app_name = str(legacy_app.get('name', '')).strip() or 'Unknown'
    app_id = str(legacy_app.get('id', '')).strip() or app_name
    url_scheme = str(legacy_app.get('urlScheme', '')).strip()

    blocks: list[dict[str, Any]] = []
    installation_block = _legacy_step_to_block(
        legacy_app.get('installationStep'),
        default_title_en='Install',
        default_title_ru='Установка',
    )
    if installation_block:
        blocks.append(installation_block)

    add_block = _legacy_step_to_block(
        legacy_app.get('addSubscriptionStep'),
        default_title_en='Add subscription',
        default_title_ru='Добавление подписки',
    )
    if add_block:
        blocks.append(add_block)

    connect_block = _legacy_step_to_block(
        legacy_app.get('connectAndUseStep'),
        default_title_en='Connect and use',
        default_title_ru='Подключение',
    )
    if connect_block:
        blocks.append(connect_block)

    has_subscription_button = any(
        isinstance(button, dict) and button.get('type') == 'subscriptionLink'
        for block in blocks
        for button in block.get('buttons', [])
        if isinstance(block, dict)
    )

    if not has_subscription_button:
        if blocks:
            target_block = blocks[-1]
        else:
            target_block = {
                'title': {'en': 'Connect', 'ru': 'Подключение'},
                'description': {},
                'buttons': [],
            }
            blocks.append(target_block)

        target_block.setdefault('buttons', [])
        target_block['buttons'].append(
            {
                'type': 'subscriptionLink',
                'text': {'en': 'Connect', 'ru': 'Подключиться'},
                # If scheme is absent, create_deep_link() will safely fallback to plain subscription URL.
                'url': '{{SUBSCRIPTION_LINK}}',
            }
        )

    return {
        'id': app_id,
        'name': app_name,
        'featured': bool(legacy_app.get('isFeatured', False)),
        'urlScheme': url_scheme,
        'isNeedBase64Encoding': bool(legacy_app.get('isNeedBase64Encoding', False)),
        'blocks': blocks,
    }


def normalize_local_app_config(config: dict[str, Any]) -> dict[str, Any]:
    """Normalize local app-config.json to RemnaWave-like shape.

    Supports both formats:
    - RemnaWave format: platforms.<key>.apps = [...]
    - Legacy format:    platforms.<key> = [...]
    """
    if not isinstance(config, dict):
        return {}

    platforms = config.get('platforms', {})
    if not isinstance(platforms, dict):
        return config

    # Already fully in RemnaWave shape.
    if platforms and all(isinstance(value, dict) and isinstance(value.get('apps'), list) for value in platforms.values()):
        return config

    normalized_platforms: dict[str, dict[str, Any]] = {}
    for raw_platform, raw_value in platforms.items():
        normalized_key = _LEGACY_TO_REMNAWAVE_PLATFORM.get(str(raw_platform).strip().lower(), str(raw_platform))

        platform_payload: dict[str, Any] | None = None

        if isinstance(raw_value, dict) and isinstance(raw_value.get('apps'), list):
            apps = []
            for app in raw_value.get('apps', []):
                if not isinstance(app, dict):
                    continue
                # Preserve RemnaWave-ready apps, normalize legacy ones.
                if isinstance(app.get('blocks'), list):
                    apps.append(app)
                else:
                    apps.append(_normalize_legacy_app(app))

            if not apps:
                continue

            platform_payload = {k: v for k, v in raw_value.items() if k != 'apps'}
            platform_payload['apps'] = apps
        elif isinstance(raw_value, list):
            apps = [_normalize_legacy_app(app) for app in raw_value if isinstance(app, dict)]
            if not apps:
                continue
            platform_payload = {'apps': apps}
        else:
            continue

        existing = normalized_platforms.get(normalized_key)
        if existing and isinstance(existing.get('apps'), list):
            existing['apps'].extend(platform_payload.get('apps', []))
            for key, value in platform_payload.items():
                if key == 'apps':
                    continue
                if key not in existing:
                    existing[key] = value
        else:
            normalized_platforms[normalized_key] = platform_payload

    normalized: dict[str, Any] = {k: v for k, v in config.items() if k != 'platforms'}
    normalized['platforms'] = normalized_platforms
    return normalized


def load_app_config() -> dict[str, Any]:
    """Load local app-config.json as fallback for guide mode."""
    try:
        if hasattr(settings, 'get_app_config_path'):
            config_path = settings.get_app_config_path()
        else:
            raw_path = str(getattr(settings, 'APP_CONFIG_PATH', 'app-config.json')).strip() or 'app-config.json'
            if Path(raw_path).is_absolute():
                config_path = raw_path
            else:
                config_path = str(Path(__file__).resolve().parents[3] / raw_path)

        with open(config_path, encoding='utf-8') as file_obj:
            data = json.load(file_obj)
            if isinstance(data, dict):
                return normalize_local_app_config(data)
    except Exception as e:
        logger.warning('Failed to load local app-config fallback', error=e)

    return {}


def _get_bot_guide_config_source() -> str:
    source = str(getattr(settings, 'BOT_GUIDE_CONFIG_SOURCE', 'auto') or 'auto').strip().lower()
    if source in {'auto', 'local', 'remnawave'}:
        return source
    return 'auto'


def _get_remnawave_config_uuid() -> str | None:
    # Allow bot guide flow to be decoupled from cabinet app-config source.
    # In "local" mode, bot always uses local app-config file.
    if _get_bot_guide_config_source() == 'local':
        return None

    try:
        from app.services.system_settings_service import bot_configuration_service

        return bot_configuration_service.get_current_value('CABINET_REMNA_SUB_CONFIG')
    except Exception as e:
        logger.debug('Could not read CABINET_REMNA_SUB_CONFIG from service, using settings fallback', error=e)
        return getattr(settings, 'CABINET_REMNA_SUB_CONFIG', None)


async def load_app_config_async() -> dict[str, Any] | None:
    """Load app config from Remnawave API (if configured), with TTL cache.

    Returns None when no Remnawave config is set or API fails.
    """
    global _app_config_cache, _app_config_cache_ts

    ttl = settings.APP_CONFIG_CACHE_TTL
    source = _get_bot_guide_config_source()

    def _is_stale_for_mode(config: dict[str, Any]) -> bool:
        # If mode switched to local, ignore cached RemnaWave config immediately.
        return source == 'local' and bool(config.get('_isRemnawave'))

    if _app_config_cache and (time.monotonic() - _app_config_cache_ts) < ttl:
        if not _is_stale_for_mode(_app_config_cache):
            return _app_config_cache

    async with _app_config_lock:
        # Double-check after acquiring lock
        if _app_config_cache and (time.monotonic() - _app_config_cache_ts) < ttl:
            if not _is_stale_for_mode(_app_config_cache):
                return _app_config_cache

        remnawave_uuid = _get_remnawave_config_uuid()

        if remnawave_uuid:
            try:
                from app.services.remnawave_service import RemnaWaveService

                service = RemnaWaveService()
                async with service.get_api_client() as api:
                    config = await api.get_subscription_page_config(remnawave_uuid)
                    if config and config.config:
                        raw = dict(config.config)
                        raw['_isRemnawave'] = True
                        _app_config_cache = raw
                        _app_config_cache_ts = time.monotonic()
                        logger.debug('Loaded app config from Remnawave', remnawave_uuid=remnawave_uuid)
                        return raw
            except Exception as e:
                logger.warning('Failed to load Remnawave config', error=e)

        local_config = load_app_config()
        if local_config:
            local_with_meta = dict(local_config)
            local_with_meta['_isRemnawave'] = False
            _app_config_cache = local_with_meta
            _app_config_cache_ts = time.monotonic()
            logger.debug('Loaded app config from local fallback file')
            return local_with_meta

        return None


def invalidate_app_config_cache() -> None:
    """Clear the cached app config so next call re-fetches from Remnawave.

    Note: This is intentionally sync (called from sync contexts in cabinet API).
    Setting timestamp to 0 first ensures the fast-path check in load_app_config_async
    fails immediately, even without acquiring _app_config_lock.
    """
    global _app_config_cache, _app_config_cache_ts
    _app_config_cache_ts = 0.0
    _app_config_cache = {}


async def get_apps_for_platform_async(device_type: str, language: str = 'ru') -> list[dict[str, Any]]:
    """Get apps for a device type from Remnawave config."""
    config = await load_app_config_async()
    if not config:
        return []

    platforms = config.get('platforms', {})
    if not isinstance(platforms, dict):
        return []

    platform_key = _DEVICE_TO_PLATFORM.get(device_type, device_type)
    platform_data = platforms.get(platform_key)
    if isinstance(platform_data, dict):
        apps = platform_data.get('apps', [])
        return [normalize_app(app) for app in apps if isinstance(app, dict)]
    return []


def get_apps_for_device(device_type: str, language: str = 'ru') -> list[dict[str, Any]]:
    """Sync helper for compatibility with legacy guide handlers."""
    config = load_app_config()
    platforms = config.get('platforms', {}) if isinstance(config, dict) else {}
    if not isinstance(platforms, dict):
        return []

    platform_key = _DEVICE_TO_PLATFORM.get(device_type, device_type)
    platform_data = platforms.get(platform_key)

    if isinstance(platform_data, dict):
        apps = platform_data.get('apps', [])
        return [normalize_app(app) for app in apps if isinstance(app, dict)]

    if isinstance(platform_data, list):  # defensive backward compatibility
        return [normalize_app(app) for app in platform_data if isinstance(app, dict)]

    return []


def normalize_app(app: dict[str, Any]) -> dict[str, Any]:
    """Normalize Remnawave app dict to a unified format with blocks."""
    return {
        'id': app.get('id', app.get('name', 'unknown')),
        'name': app.get('name', ''),
        'isFeatured': app.get('featured', app.get('isFeatured', False)),
        'urlScheme': app.get('urlScheme', ''),
        'isNeedBase64Encoding': app.get('isNeedBase64Encoding', False),
        'blocks': app.get('blocks', []),
        '_raw': app,
    }


def get_platforms_list(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract available platforms from config for keyboard generation.

    Returns list of {key, displayName, icon_emoji, device_type} sorted by typical order.
    """
    platforms = config.get('platforms', {})
    if not isinstance(platforms, dict):
        return []

    # Desired order
    order = ['ios', 'android', 'windows', 'macos', 'linux', 'androidTV', 'appleTV']

    result = []
    for pk in order:
        if pk not in platforms:
            continue
        pd = platforms[pk]

        if not isinstance(pd, dict) or not pd.get('apps'):
            continue

        display = _PLATFORM_DISPLAY.get(pk, {'name': pk, 'emoji': '📱'})

        # Get displayName from Remnawave or fallback
        display_name_data = pd.get('displayName', display['name'])
        icon_emoji = display['emoji']
        if isinstance(pd.get('icon_emoji'), str) and pd.get('icon_emoji').strip():
            icon_emoji = pd.get('icon_emoji').strip()
        elif isinstance(pd.get('iconEmoji'), str) and pd.get('iconEmoji').strip():
            icon_emoji = pd.get('iconEmoji').strip()

        icon_custom_emoji_id = ''
        for field_name in ('icon_custom_emoji_id', 'iconCustomEmojiId'):
            value = pd.get(field_name)
            if isinstance(value, str) and value.strip():
                icon_custom_emoji_id = value.strip()
                break

        result.append(
            {
                'key': pk,
                'displayName': display_name_data,
                'icon_emoji': icon_emoji,
                'icon_custom_emoji_id': icon_custom_emoji_id,
                'device_type': _PLATFORM_TO_DEVICE.get(pk, pk),
            }
        )

    # Also include any platforms in config not in our order list
    for pk, pd in platforms.items():
        if pk in order:
            continue
        if not isinstance(pd, dict) or not pd.get('apps'):
            continue

        display = _PLATFORM_DISPLAY.get(pk, {'name': pk, 'emoji': '📱'})
        icon_emoji = display.get('emoji', '📱')
        if isinstance(pd.get('icon_emoji'), str) and pd.get('icon_emoji').strip():
            icon_emoji = pd.get('icon_emoji').strip()
        elif isinstance(pd.get('iconEmoji'), str) and pd.get('iconEmoji').strip():
            icon_emoji = pd.get('iconEmoji').strip()

        icon_custom_emoji_id = ''
        for field_name in ('icon_custom_emoji_id', 'iconCustomEmojiId'):
            value = pd.get(field_name)
            if isinstance(value, str) and value.strip():
                icon_custom_emoji_id = value.strip()
                break

        result.append(
            {
                'key': pk,
                'displayName': display.get('name', pk),
                'icon_emoji': icon_emoji,
                'icon_custom_emoji_id': icon_custom_emoji_id,
                'device_type': _PLATFORM_TO_DEVICE.get(pk, pk),
            }
        )

    return result


def resolve_button_url(
    url: str,
    subscription_url: str | None,
    crypto_link: str | None = None,
) -> str:
    """Resolve template variables in button URLs (port of cabinet's _resolve_button_url)."""
    if not url:
        return url
    result = url
    if subscription_url:
        result = result.replace('{{SUBSCRIPTION_LINK}}', subscription_url)
    if crypto_link:
        result = result.replace('{{HAPP_CRYPT3_LINK}}', crypto_link)
        result = result.replace('{{HAPP_CRYPT4_LINK}}', crypto_link)
    return result


def create_deep_link(app: dict[str, Any], subscription_url: str) -> str | None:
    if not subscription_url:
        return None

    if not isinstance(app, dict):
        return subscription_url

    scheme = str(app.get('urlScheme', '')).strip()
    payload = subscription_url

    if app.get('isNeedBase64Encoding'):
        try:
            payload = base64.b64encode(subscription_url.encode('utf-8')).decode('utf-8')
        except Exception as exc:
            logger.warning(
                'Не удалось закодировать ссылку подписки в base64 для приложения', app=app.get('id'), exc=exc
            )
            payload = subscription_url

    scheme_link = f'{scheme}{payload}' if scheme else None

    template = settings.get_happ_cryptolink_redirect_template()
    redirect_link = build_redirect_link(scheme_link, template) if scheme_link and template else None

    return redirect_link or scheme_link or subscription_url


def get_reset_devices_confirm_keyboard(language: str = 'ru') -> InlineKeyboardMarkup:
    get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='✅ Да, сбросить все устройства', callback_data='confirm_reset_devices')],
            [InlineKeyboardButton(text='❌ Отмена', callback_data='menu_subscription')],
        ]
    )


def get_traffic_switch_keyboard(
    current_traffic_gb: int,
    language: str = 'ru',
    subscription_end_date: datetime = None,
    discount_percent: int = 0,
    base_traffic_gb: int = None,
) -> InlineKeyboardMarkup:
    from app.config import settings

    # Если базовый трафик не передан, используем текущий
    # (для обратной совместимости и случаев без докупленного трафика)
    if base_traffic_gb is None:
        base_traffic_gb = current_traffic_gb

    # Считаем по дням (как в кабинете и подтверждении)
    if subscription_end_date:
        now = datetime.now(UTC)
        days_left = max(1, (subscription_end_date - now).days)
        price_multiplier = days_left / 30
        period_text = f' (за {days_left} дн.)' if days_left > 1 else ' (за 1 день)'
    else:
        price_multiplier = 1
        period_text = ''

    packages = settings.get_traffic_packages()
    enabled_packages = [pkg for pkg in packages if pkg['enabled']]

    # Используем базовый трафик для определения цены текущего пакета
    current_price_per_month = settings.get_traffic_price(base_traffic_gb)
    discounted_current_per_month, _ = apply_percentage_discount(
        current_price_per_month,
        discount_percent,
    )

    buttons = []

    for package in enabled_packages:
        gb = package['gb']
        price_per_month = package['price']
        discounted_price_per_month, _ = apply_percentage_discount(
            price_per_month,
            discount_percent,
        )

        price_diff_per_month = discounted_price_per_month - discounted_current_per_month
        total_price_diff = int(price_diff_per_month * price_multiplier)

        # Сравниваем с базовым трафиком (без докупленного)
        if gb == base_traffic_gb:
            emoji = '✅'
            action_text = ' (текущий)'
            price_text = ''
        elif total_price_diff > 0:
            emoji = '⬆️'
            action_text = ''
            price_text = f' (+{total_price_diff // 100}₽{period_text})'
            if discount_percent > 0:
                discount_total = int((price_per_month - current_price_per_month) * price_multiplier) - total_price_diff
                if discount_total > 0:
                    price_text += f' (скидка {discount_percent}%: -{discount_total // 100}₽)'
        elif total_price_diff < 0:
            emoji = '⬇️'
            action_text = ''
            price_text = ' (без возврата)'
        else:
            emoji = '🔄'
            action_text = ''
            price_text = ' (бесплатно)'

        if gb == 0:
            traffic_text = 'Безлимит'
        else:
            traffic_text = f'{gb} ГБ'

        button_text = f'{emoji} {traffic_text}{action_text}{price_text}'

        buttons.append([InlineKeyboardButton(text=button_text, callback_data=f'switch_traffic_{gb}')])

    language_code = (language or 'ru').split('-')[0].lower()
    buttons.append(
        [
            InlineKeyboardButton(
                text='⬅️ Назад' if language_code in {'ru', 'fa'} else '⬅️ Back',
                callback_data='subscription_settings',
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_confirm_switch_traffic_keyboard(
    new_traffic_gb: int, price_difference: int, language: str = 'ru'
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text='✅ Подтвердить переключение',
                    callback_data=f'confirm_switch_traffic_{new_traffic_gb}_{price_difference}',
                )
            ],
            [InlineKeyboardButton(text='❌ Отмена', callback_data='subscription_settings')],
        ]
    )
