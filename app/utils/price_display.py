"""
Unified price display system for all subscription and balance pricing.

This module provides a centralized way to:
- Calculate prices with all applicable discounts (promo groups, promo offers)
- Format price buttons consistently across all flows
- Ensure uniform discount display throughout the application
"""

from dataclasses import dataclass

import structlog

from app.config import settings
from app.database.models import User


logger = structlog.get_logger(__name__)


@dataclass
class PriceInfo:
    """Container for pricing information with discounts."""

    base_price: int  # Original price without any discounts (kopeks)
    final_price: int  # Final price after all discounts (kopeks)
    discount_percent: int  # Total discount percentage

    @property
    def has_discount(self) -> bool:
        """Check if there's any discount applied."""
        return self.base_price > self.final_price and self.discount_percent > 0

    @property
    def discount_value(self) -> int:
        """Get the absolute discount value in kopeks."""
        return self.base_price - self.final_price


def calculate_user_price(user: User | None, base_price: int, period_days: int, category: str = 'period') -> PriceInfo:
    """
    Calculate final price for a user with all applicable discounts.

    Args:
        user: User object (None for base/default pricing from settings)
        base_price: Base price without discounts (kopeks)
        period_days: Subscription period in days
        category: Discount category ("period", "servers", "devices", "traffic")

    Returns:
        PriceInfo with base_price, final_price, and discount_percent

    Example:
        >>> user = get_user_from_db(123)
        >>> price_info = calculate_user_price(user, 100000, 30, 'period')
        >>> print(f'{price_info.base_price} -> {price_info.final_price} ({price_info.discount_percent}%)')
        100000 -> 80000 (20%)

        >>> # For base pricing (no user)
        >>> price_info = calculate_user_price(None, 100000, 30, 'period')
        >>> # Uses BASE_PROMO_GROUP_PERIOD_DISCOUNTS from settings
    """
    if not base_price or base_price <= 0:
        return PriceInfo(base_price=base_price or 0, final_price=base_price or 0, discount_percent=0)

    # Step 1: Get promo group discount
    if user:
        group_discount = user.get_promo_discount(category, period_days)
    else:
        group_discount = settings.get_base_promo_group_period_discount(period_days)

    # Step 2: Get promo offer discount (stacking)
    promo_offer_discount = 0
    if user:
        from app.utils.promo_offer import get_user_active_promo_discount_percent

        promo_offer_discount = get_user_active_promo_discount_percent(user)

    # Apply both discounts sequentially (same as cabinet)
    final_price = base_price
    if group_discount > 0:
        final_price = final_price - (final_price * group_discount) // 100
    if promo_offer_discount > 0:
        final_price = final_price - (final_price * promo_offer_discount) // 100

    # Effective combined discount percent
    if final_price < base_price:
        discount_percent = round((base_price - final_price) * 100 / base_price)
    else:
        discount_percent = 0

    logger.debug(
        'calculate_user_price',
        telegram_id=user.telegram_id if user else 'None',
        base_price=base_price,
        final_price=final_price,
        group_discount=group_discount,
        promo_offer_discount=promo_offer_discount,
        discount_percent=discount_percent,
        category=category,
        period_days=period_days,
    )

    return PriceInfo(base_price=base_price, final_price=final_price, discount_percent=discount_percent)


def format_price_button(
    period_label: str, price_info: PriceInfo, format_price_func, emphasize: bool = False, add_exclamation: bool = True
) -> str:
    """
    Format a price button text with unified discount display.

    Args:
        period_label: Label for the period (e.g., "30 дней", "1 месяц")
        price_info: PriceInfo object with pricing details
        format_price_func: Function to format price (usually texts.format_price)
        emphasize: Add fire emojis for emphasis (for best deals)
        add_exclamation: Add exclamation mark after discount percent

    Returns:
        Formatted button text

    Examples:
        With discount and price > 0:
            "📅 30 дней - 990₽ ➜ 693₽ (-30%)!"

        With final price = 0:
            "📅 30 дней"

        With emphasis:
            "🔥 📅 30 дней - 8990₽ ➜ 6293₽ (-30%)! 🔥"

        Without discount:
            "📅 30 дней - 990₽"
    """
    # Format button text differently if final price is 0
    if price_info.final_price == 0:
        button_text = f'📅 {period_label}'
    elif price_info.has_discount:
        exclamation = '!' if add_exclamation else ''
        button_text = (
            f'📅 {period_label} - '
            f'{format_price_func(price_info.base_price)} ➜ '
            f'{format_price_func(price_info.final_price)} '
            f'(-{price_info.discount_percent}%){exclamation}'
        )
    else:
        button_text = f'📅 {period_label} - {format_price_func(price_info.final_price)}'

    # Add emphasis for best deals
    if emphasize:
        button_text = f'🔥 {button_text} 🔥'

    logger.debug('Formatted button', button_text=button_text)
    return button_text


def format_price_text(period_label: str, price_info: PriceInfo, format_price_func) -> str:
    """
    Format a price for message text (not button) with unified discount display.

    Args:
        period_label: Label for the period (e.g., "30 дней")
        price_info: PriceInfo object with pricing details
        format_price_func: Function to format price (usually texts.format_price)

    Returns:
        Formatted price text for messages

    Examples:
        With discount:
            "📅 30 дней - 990₽ ➜ 693₽"

        Without discount:
            "📅 30 дней - 990₽"

        With zero price:
            "📅 30 дней"
    """
    if price_info.final_price == 0:
        return f'📅 {period_label}'
    if price_info.has_discount:
        return (
            f'📅 {period_label} - '
            f'{format_price_func(price_info.base_price)} ➜ '
            f'{format_price_func(price_info.final_price)}'
        )
    return f'📅 {period_label} - {format_price_func(price_info.final_price)}'
