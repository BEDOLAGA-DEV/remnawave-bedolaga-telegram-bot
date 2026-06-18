import pytest
from app.keyboards.inline import get_balance_keyboard
from app.config import settings

def test_get_balance_keyboard_no_saved_cards(monkeypatch):
    # Enable recurrent settings to test conditional logic
    monkeypatch.setattr(settings, "YOOKASSA_RECURRENT_ENABLED", True)
    monkeypatch.setattr(settings, "ANTILOPAY_RECURRENT_ENABLED", True)

    keyboard = get_balance_keyboard(language="ru", has_saved_cards=False)
    
    # Flatten the buttons list to check elements
    buttons = [btn for row in keyboard.inline_keyboard for btn in row]
    
    # Check that "Пополнить" button has style='success' (green)
    top_up_btn = next((btn for btn in buttons if btn.callback_data == "balance_topup"), None)
    assert top_up_btn is not None
    assert getattr(top_up_btn, "style", None) == "success"

    # Check that "Привязанные карты" (saved_cards_list) is NOT present
    saved_cards_btn = next((btn for btn in buttons if btn.callback_data == "saved_cards_list"), None)
    assert saved_cards_btn is None

def test_get_balance_keyboard_with_saved_cards(monkeypatch):
    # Enable recurrent settings
    monkeypatch.setattr(settings, "YOOKASSA_RECURRENT_ENABLED", True)
    monkeypatch.setattr(settings, "ANTILOPAY_RECURRENT_ENABLED", True)

    keyboard = get_balance_keyboard(language="ru", has_saved_cards=True)
    
    # Flatten the buttons list
    buttons = [btn for row in keyboard.inline_keyboard for btn in row]
    
    # Check that "Пополнить" button has style='success' (green)
    top_up_btn = next((btn for btn in buttons if btn.callback_data == "balance_topup"), None)
    assert top_up_btn is not None
    assert getattr(top_up_btn, "style", None) == "success"

    # Check that "Привязанные карты" (saved_cards_list) IS present
    saved_cards_btn = next((btn for btn in buttons if btn.callback_data == "saved_cards_list"), None)
    assert saved_cards_btn is not None

def test_payment_methods_keyboard_sorting(monkeypatch):
    from app.keyboards.inline import get_payment_methods_keyboard
    
    # Enable multiple payment methods by patching the Settings class methods/fields
    monkeypatch.setattr(settings, "TELEGRAM_STARS_ENABLED", True)
    monkeypatch.setattr(settings.__class__, "is_yookassa_enabled", lambda self: True)
    monkeypatch.setattr(settings, "YOOKASSA_SBP_ENABLED", True)
    monkeypatch.setattr(settings.__class__, "is_heleket_enabled", lambda self: True)
    monkeypatch.setattr(settings.__class__, "is_wata_enabled", lambda self: True)
    monkeypatch.setattr(settings.__class__, "is_support_topup_enabled", lambda self: True)
    monkeypatch.setattr(settings, "TRIBUTE_ENABLED", True)

    keyboard = get_payment_methods_keyboard(amount_kopeks=0, language="ru")
    
    # Extract callback datas in order of appearance
    callbacks = [btn.callback_data for row in keyboard.inline_keyboard for btn in row if btn.callback_data]
    
    # Filter for topup callback datas only
    topup_callbacks = [cb for cb in callbacks if cb.startswith("topup_")]
    
    # Expected order categories priorities:
    # 1. Cards (wata, yookassa): priority 10
    # 2. SBP (yookassa_sbp): priority 20
    # 3. Stars (stars): priority 30
    # 4. Crypto (heleket): priority 40
    # 5. Foreign (tribute): priority 50
    # 6. Support (support): priority 60
    
    expected_order = [
        "topup_yookassa",
        "topup_wata",
        "topup_yookassa_sbp",
        "topup_stars",
        "topup_heleket",
        "topup_tribute"
    ]
    
    # Make sure all enabled topup methods are ordered exactly as expected
    assert topup_callbacks == expected_order
