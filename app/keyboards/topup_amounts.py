import structlog
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.localization.loader import DEFAULT_LANGUAGE
from app.localization.texts import get_texts
from app.services.payment_method_config_service import (
    _get_method_defaults,
    get_config_by_method_id,
    get_effective_quick_amounts,
)


logger = structlog.get_logger(__name__)

METHOD_CONFIG_IDS = {
    'stars': 'telegram_stars',
    'yookassa_sbp': 'yookassa',
    'kassa_ai_sbp': 'kassa_ai',
    'kassa_ai_card': 'kassa_ai',
    'kassa_ai_sberpay': 'kassa_ai',
    'aurapay_sbp': 'aurapay',
    'aurapay_card': 'aurapay',
    'etoplatezhi_sbp': 'etoplatezhi',
    'etoplatezhi_card': 'etoplatezhi',
    'antilopay_sbp': 'antilopay',
    'antilopay_card': 'antilopay',
    'antilopay_sberpay': 'antilopay',
    'jupiter_sbp': 'jupiter',
    'donut_card': 'donut',
    'donut_sbp': 'donut',
    'donut_sbp_qr': 'donut',
    'lava_card': 'lava',
    'lava_sbp': 'lava',
    'overpay_fps': 'overpay',
    'overpay_card': 'overpay',
    'overpay_int': 'overpay',
}


def resolve_config_method_id(method: str) -> str:
    if method.startswith('platega_m'):
        return 'platega'
    return METHOD_CONFIG_IDS.get(method, method)


def format_quick_amount(amount_kopeks: int) -> str:
    if amount_kopeks % 100 == 0:
        return f'{amount_kopeks // 100} ₽'
    return f'{amount_kopeks / 100:.2f} ₽'


async def _load_quick_amounts(db: AsyncSession, method: str, min_amount_kopeks: int | None = None) -> list[int]:
    method_id = resolve_config_method_id(method)
    config = await get_config_by_method_id(db, method_id)
    if not config:
        return []
    method_def = _get_method_defaults().get(method_id, {})
    min_amount = (
        config.min_amount_kopeks if config.min_amount_kopeks is not None else method_def.get('default_min', 1000)
    )
    if min_amount_kopeks is not None:
        min_amount = max(min_amount, min_amount_kopeks)
    max_amount = (
        config.max_amount_kopeks if config.max_amount_kopeks is not None else method_def.get('default_max', 10000000)
    )
    return get_effective_quick_amounts(config.quick_amounts, min_amount, max_amount)


async def get_topup_amount_keyboard(
    method: str,
    language: str = DEFAULT_LANGUAGE,
    db: AsyncSession | None = None,
    back_callback: str = 'menu_balance',
    *,
    min_amount_kopeks: int | None = None,
) -> InlineKeyboardMarkup:
    amounts: list[int] = []
    try:
        if db is not None:
            amounts = await _load_quick_amounts(db, method, min_amount_kopeks)
        else:
            from app.database.database import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                amounts = await _load_quick_amounts(session, method, min_amount_kopeks)
    except Exception as error:
        logger.warning('Не удалось загрузить быстрые суммы пополнения', method=method, error=error, exc_info=True)

    texts = get_texts(language)
    keyboard: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for amount in amounts:
        row.append(
            InlineKeyboardButton(
                text=format_quick_amount(amount),
                callback_data=f'topup_amount|{method}|{amount}',
            )
        )
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton(text=texts.BACK, callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)
