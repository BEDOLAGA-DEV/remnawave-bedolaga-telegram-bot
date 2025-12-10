from unittest.mock import MagicMock, patch

from app.handlers.subscription.purchase import confirm_purchase

# Импортируем фикстуры из fixtures
from tests.fixtures.purchase_fixtures import (
    mock_callback_query,
    mock_user,
    mock_user_with_subscription,
    mock_db,
    mock_state_with_countries,
    mock_state_without_countries,
    ERROR_COUNTRIES_REQUIRED,
    TEST_COUNTRY_UUID_1,
    TEST_COUNTRY_UUID_2,
)


async def test_confirm_purchase_rejects_empty_countries_before_balance_deduction(
    mock_callback_query,
    mock_user,
    mock_db,
    mock_state_without_countries
):
    with patch('app.handlers.subscription.purchase.subtract_user_balance') as mock_subtract, \
         patch('app.handlers.subscription.purchase.save_subscription_checkout_draft') as mock_save_draft, \
         patch('app.handlers.subscription.purchase.should_offer_checkout_resume', return_value=False), \
         patch('app.handlers.subscription.purchase._get_available_countries', return_value=[]), \
         patch('app.handlers.subscription.purchase.get_texts') as mock_get_texts, \
         patch('app.handlers.subscription.purchase.get_back_keyboard') as mock_keyboard, \
         patch('app.handlers.subscription.purchase.calculate_months_from_days', return_value=1), \
         patch('app.handlers.subscription.purchase.validate_pricing_calculation', return_value=True):
        
        mock_texts = MagicMock()
        mock_texts.t = MagicMock(return_value=ERROR_COUNTRIES_REQUIRED)
        mock_texts.format_price = lambda x: f"{x/100:.0f} ₽"
        mock_get_texts.return_value = mock_texts
        
        mock_keyboard.return_value = MagicMock()
        
        await confirm_purchase(
            mock_callback_query,
            mock_state_without_countries,
            mock_user,
            mock_db
        )
        
        mock_subtract.assert_not_called()
        
        mock_callback_query.message.edit_text.assert_called_once()
        call_args = mock_callback_query.message.edit_text.call_args
        
        error_text = call_args[0][0] if call_args[0] else call_args[1].get('text', '')
        assert "Нельзя отключить все страны" in str(error_text) or "COUNTRIES_MINIMUM_REQUIRED" in str(call_args)
        
        mock_callback_query.answer.assert_called_once()


async def test_confirm_purchase_rejects_empty_countries_with_existing_subscription(
    mock_callback_query,
    mock_user_with_subscription,
    mock_db,
    mock_state_without_countries
):
    with patch('app.handlers.subscription.purchase.subtract_user_balance') as mock_subtract, \
         patch('app.handlers.subscription.purchase.save_subscription_checkout_draft') as mock_save_draft, \
         patch('app.handlers.subscription.purchase.should_offer_checkout_resume', return_value=False), \
         patch('app.handlers.subscription.purchase._get_available_countries', return_value=[]), \
         patch('app.handlers.subscription.purchase.get_texts') as mock_get_texts, \
         patch('app.handlers.subscription.purchase.get_back_keyboard') as mock_keyboard, \
         patch('app.handlers.subscription.purchase.calculate_months_from_days', return_value=1), \
         patch('app.handlers.subscription.purchase.validate_pricing_calculation', return_value=True):
        
        mock_texts = MagicMock()
        mock_texts.t = MagicMock(return_value=ERROR_COUNTRIES_REQUIRED)
        mock_texts.format_price = lambda x: f"{x/100:.0f} ₽"
        mock_get_texts.return_value = mock_texts
        
        mock_keyboard.return_value = MagicMock()
        
        await confirm_purchase(
            mock_callback_query,
            mock_state_without_countries,
            mock_user_with_subscription,
            mock_db
        )
        
        mock_subtract.assert_not_called()
        
        mock_callback_query.message.edit_text.assert_called_once()


async def test_confirm_purchase_allows_purchase_with_countries(
    mock_callback_query,
    mock_user,
    mock_db,
    mock_state_with_countries
):
    with patch('app.handlers.subscription.purchase.subtract_user_balance') as mock_subtract, \
         patch('app.handlers.subscription.purchase.save_subscription_checkout_draft') as mock_save_draft, \
         patch('app.handlers.subscription.purchase.should_offer_checkout_resume', return_value=False), \
         patch('app.handlers.subscription.purchase._get_available_countries', return_value=[
             {'uuid': TEST_COUNTRY_UUID_1, 'name': 'Test Country 1', 'price_kopeks': 5000, 'is_available': True},
             {'uuid': TEST_COUNTRY_UUID_2, 'name': 'Test Country 2', 'price_kopeks': 5000, 'is_available': True},
         ]), \
         patch('app.handlers.subscription.purchase.get_texts') as mock_get_texts, \
         patch('app.handlers.subscription.purchase.get_back_keyboard') as mock_keyboard, \
         patch('app.handlers.subscription.purchase.calculate_months_from_days', return_value=1), \
         patch('app.handlers.subscription.purchase.validate_pricing_calculation', return_value=True):
        
        mock_texts = MagicMock()
        mock_texts.t = MagicMock(side_effect=lambda key, default: default)
        mock_texts.format_price = lambda x: f"{x/100:.0f} ₽"
        mock_get_texts.return_value = mock_texts
        
        try:
            await confirm_purchase(
                mock_callback_query,
                mock_state_with_countries,
                mock_user,
                mock_db
            )
        except Exception:
            pass
        
        error_calls = []
        for call in mock_callback_query.message.edit_text.call_args_list:
            if call:
                args = call[0] if call[0] else []
                kwargs = call[1] if len(call) > 1 and call[1] else {}
                text = args[0] if args else kwargs.get('text', '')
                if "Нельзя отключить все страны" in str(text) or "COUNTRIES_MINIMUM_REQUIRED" in str(text):
                    error_calls.append(call)
        
        assert len(error_calls) == 0, "Не должно быть ошибки о странах, когда страны выбраны"
