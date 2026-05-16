from __future__ import annotations

import asyncio
from typing import Any

import structlog

from app.config import settings
from app.localization.loader import (
    DEFAULT_LANGUAGE,
    clear_locale_cache,
    load_locale,
)


_logger = structlog.get_logger(__name__)

_cached_rules: dict[str, str] = {}


_LANGUAGE_ALIASES = {
    'uk': 'ua',
}


_DYNAMIC_LANGUAGE_CONFIGS = {
    'ru': {
        'traffic_pattern': '📊 {size} ГБ - {price}',
        'unlimited_pattern': '📊 Безлимит - {price}',
        'support_info': (
            '\n🛟 <b>Поддержка</b>\n\n'
            'Это центр тикетов: создавайте обращения, просматривайте ответы и историю.\n\n'
            '• 🎫 Создать тикет — опишите проблему или вопрос\n'
            '• 📋 Мои тикеты — статус и переписка\n'
            '• 💬 Связаться — написать напрямую (если нужно)\n\n'
            'Старайтесь использовать тикеты — так мы быстрее поможем и ничего не потеряется.\n'
        ),
    },
    'fa': {
        'traffic_pattern': '📊 {size} گیگابایت - {price}',
        'unlimited_pattern': '📊 نامحدود - {price}',
        'support_info': (
            '\n🛟 <b>پشتیبانی</b>\n\n'
            'برای هرگونه سؤال به پشتیبانی پیام دهید:\n\n'
            '👤 {support_username}\n\n'
            '• 🎫 ایجاد تیکت\n'
            '• 📋 تیکت‌های من\n'
            '• 💬 تماس مستقیم\n'
        ),
    },
    'en': {
        'traffic_pattern': '📊 {size} GB - {price}',
        'unlimited_pattern': '📊 Unlimited - {price}',
        'support_info': (
            '\n🛟 <b>RemnaWave Support</b>\n\n'
            'This is the ticket center: create requests, view replies and history.\n\n'
            '• 🎫 Create ticket — describe your issue or question\n'
            '• 📋 My tickets — status and conversation\n'
            '• 💬 Contact — message directly if needed\n\n'
            'Prefer tickets — it helps us respond faster and keep context.\n'
        ),
    },
    'ua': {
        'traffic_pattern': '📊 {size} ГБ - {price}',
        'unlimited_pattern': '📊 Безліміт - {price}',
        'support_info': (
            '\n🛠️ <b>Технічна підтримка</b>\n\n'
            'З усіх питань звертайтеся до нашої підтримки:\n\n'
            '👤 {support_username}\n\n'
            'Ми допоможемо з:\n'
            '• Налаштуванням підключення\n'
            '• Вирішенням технічних проблем\n'
            '• Питаннями щодо оплати\n'
            '• Іншими питаннями\n\n'
            '⏰ Час відповіді: зазвичай протягом 1-2 годин\n'
        ),
    },
    'zh': {
        'traffic_pattern': '📊{size}GB-{price}',
        'unlimited_pattern': '📊无限-{price}',
        'support_info': (
            '\n🛠️ <b>技术支持</b>\n\n'
            '如有任何问题，请联系我们的支持团队：\n\n'
            '👤 {support_username}\n\n'
            '我们将帮助您：\n'
            '• 设置连接\n'
            '• 解决技术问题\n'
            '• 付款问题\n'
            '• 其他问题\n\n'
            '⏰ 响应时间：通常在 1-2 小时内\n'
        ),
    },
}


_TRAFFIC_TIERS = (
    ('TRAFFIC_5GB', '5', 'PRICE_TRAFFIC_5GB'),
    ('TRAFFIC_10GB', '10', 'PRICE_TRAFFIC_10GB'),
    ('TRAFFIC_25GB', '25', 'PRICE_TRAFFIC_25GB'),
    ('TRAFFIC_50GB', '50', 'PRICE_TRAFFIC_50GB'),
    ('TRAFFIC_100GB', '100', 'PRICE_TRAFFIC_100GB'),
    ('TRAFFIC_250GB', '250', 'PRICE_TRAFFIC_250GB'),
)


def _get_cached_rules_value(language: str) -> str:
    if language in _cached_rules:
        return _cached_rules[language]

    default = _get_default_rules(language)
    _cached_rules[language] = default
    return default


def _build_dynamic_values(language: str) -> dict[str, Any]:
    language_code = (language or DEFAULT_LANGUAGE).split('-')[0].lower()

    language_code = _LANGUAGE_ALIASES.get(language_code, language_code)
    config = _DYNAMIC_LANGUAGE_CONFIGS.get(language_code)

    if not config:
        return {}

    values: dict[str, Any] = {}
    traffic_pattern = config['traffic_pattern']
    for key, size, price_attr in _TRAFFIC_TIERS:
        price_value = getattr(settings, price_attr)
        values[key] = traffic_pattern.format(
            size=size,
            price=settings.format_price(price_value),
        )

    values['TRAFFIC_UNLIMITED'] = config['unlimited_pattern'].format(
        price=settings.format_price(settings.PRICE_TRAFFIC_UNLIMITED)
    )

    support_template = config.get('support_info')
    if support_template:
        values['SUPPORT_INFO'] = support_template.format(support_username=settings.SUPPORT_USERNAME)

    return values


class Texts:
    def __init__(self, language: str = DEFAULT_LANGUAGE):
        self.language = language or DEFAULT_LANGUAGE
        raw_data = load_locale(self.language)
        self._values = {key: value for key, value in raw_data.items()}

        if self.language != DEFAULT_LANGUAGE:
            fallback_data = load_locale(DEFAULT_LANGUAGE)
        else:
            fallback_data = self._values

        self._fallback_values = {key: value for key, value in fallback_data.items() if key not in self._values}

        self._values.update(_build_dynamic_values(self.language))

    def __getattr__(self, item: str) -> Any:
        if item == 'language':
            return super().__getattribute__(item)
        try:
            return self._get_value(item)
        except KeyError as error:
            raise AttributeError(item) from error

    def __getitem__(self, item: str) -> Any:
        return self._get_value(item)

    def get(self, item: str, default: Any = None) -> Any:
        try:
            return self._get_value(item)
        except KeyError:
            return default

    def t(self, key: str, default: Any = None) -> Any:
        try:
            return self._get_value(key)
        except KeyError:
            if default is not None:
                return default
            raise

    def _get_value(self, item: str) -> Any:
        if item == 'RULES_TEXT':
            return _get_cached_rules_value(self.language)

        if item in self._values:
            return self._values[item]

        if item in self._fallback_values:
            return self._fallback_values[item]

        _logger.warning("Missing localization key '' for language ''", item=item, language=self.language)
        raise KeyError(item)

    @staticmethod
    def format_price(kopeks: int, round_kopeks: bool | None = None) -> str:
        return settings.format_price(kopeks, round_kopeks=round_kopeks)

    @staticmethod
    def format_traffic(gb: float, is_limit: bool = True) -> str:
        """Format traffic value.

        Args:
            gb: Traffic in gigabytes
            is_limit: If True, 0 means unlimited. If False, 0 means zero used.
        """
        if gb == 0:
            return '∞ (безлимит)' if is_limit else '0 ГБ'
        if gb >= 1024:
            return f'{gb / 1024:.1f} ТБ'
        return f'{gb:.0f} ГБ'


def get_texts(language: str = DEFAULT_LANGUAGE) -> Texts:
    return Texts(language)


async def get_rules_from_db(language: str = DEFAULT_LANGUAGE) -> str:
    try:
        from app.database.crud.rules import get_current_rules_content
        from app.database.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            rules = await get_current_rules_content(db, language)
            if rules:
                _cached_rules[language] = rules
                return rules

    except Exception as error:  # pragma: no cover - defensive logging
        _logger.warning('Failed to load rules from DB for', language=language, error=error)

    default = _get_default_rules(language)
    _cached_rules[language] = default
    return default


def _get_default_rules(language: str = DEFAULT_LANGUAGE) -> str:
    default_key = 'RULES_TEXT_DEFAULT'
    locale = load_locale(language)
    if default_key in locale:
        return locale[default_key]
    fallback = load_locale(DEFAULT_LANGUAGE)
    return fallback.get(default_key, '')


def _get_default_privacy_policy(language: str = DEFAULT_LANGUAGE) -> str:
    default_key = 'PRIVACY_POLICY_TEXT_DEFAULT'
    locale = load_locale(language)
    if default_key in locale:
        return locale[default_key]
    fallback = load_locale(DEFAULT_LANGUAGE)
    return fallback.get(default_key, '')


def get_privacy_policy(language: str = DEFAULT_LANGUAGE) -> str:
    return _get_default_privacy_policy(language)


def get_rules_sync(language: str = DEFAULT_LANGUAGE) -> str:
    if language in _cached_rules:
        return _cached_rules[language]

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(get_rules(language))

    loop.create_task(get_rules(language))
    return _get_cached_rules_value(language)


async def get_rules(language: str = DEFAULT_LANGUAGE) -> str:
    if language in _cached_rules:
        return _cached_rules[language]

    return await get_rules_from_db(language)


async def refresh_rules_cache(language: str = DEFAULT_LANGUAGE) -> None:
    _cached_rules.pop(language, None)
    await get_rules_from_db(language)


def clear_rules_cache() -> None:
    _cached_rules.clear()


def reload_locales() -> None:
    clear_locale_cache()
