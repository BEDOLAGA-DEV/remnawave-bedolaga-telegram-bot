from app.config import settings
from app.handlers.subscription.tariff_purchase import (
    get_tariff_extend_insufficient_balance_keyboard,
    get_tariff_insufficient_balance_keyboard,
)


def _callbacks(keyboard):
    return [button.callback_data for row in keyboard.inline_keyboard for button in row]


def test_classic_topup_when_autopurchase_disabled(monkeypatch):
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', False)

    keyboard = get_tariff_insufficient_balance_keyboard(7, 30, 'ru', missing_kopeks=50000)
    callbacks = _callbacks(keyboard)

    assert 'balance_topup' in callbacks
    assert not any((c or '').startswith('topup_amount|') for c in callbacks)
    assert 'tariff_select:7' in callbacks


def test_inlines_prefilled_payment_when_autopurchase_enabled(monkeypatch):
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', True)

    keyboard = get_tariff_insufficient_balance_keyboard(7, 30, 'ru', missing_kopeks=50000)
    callbacks = _callbacks(keyboard)

    # прямая оплата ровно недостающей суммой, без промежуточного «Пополнить баланс»
    assert 'topup_amount|stars|50000' in callbacks
    assert 'balance_topup' not in callbacks
    # возврат ведёт к выбору тарифа
    assert 'tariff_select:7' in callbacks


def test_classic_topup_when_missing_zero(monkeypatch):
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', True)

    keyboard = get_tariff_insufficient_balance_keyboard(7, 30, 'ru', missing_kopeks=0)

    assert 'balance_topup' in _callbacks(keyboard)


def test_sbp_purchase_offered_on_insufficient_balance(monkeypatch):
    """СБП-оформлению баланс не нужен — кнопка обязана быть и на экране нехватки средств.

    Регрессия: кнопка «Оформить с автооплатой СБП» жила только на confirm-экранах,
    до которых без денег не дойти, — вход в фичу перекрывала проверка баланса.
    """
    monkeypatch.setattr(type(settings), 'is_platega_recurrent_enabled', lambda self: True)

    # Классическая ветка (автопокупка выключена)
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', False)
    assert 'tariff_sbp:7' in _callbacks(get_tariff_insufficient_balance_keyboard(7, 30, 'ru', missing_kopeks=50000))

    # Ветка с предзаполненными кнопками прямой оплаты
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', True)
    callbacks = _callbacks(get_tariff_insufficient_balance_keyboard(7, 30, 'ru', missing_kopeks=50000))
    assert 'tariff_sbp:7' in callbacks
    assert 'topup_amount|stars|50000' in callbacks


def test_no_sbp_purchase_when_recurrent_disabled(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_platega_recurrent_enabled', lambda self: False)
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', False)

    callbacks = _callbacks(get_tariff_insufficient_balance_keyboard(7, 30, 'ru', missing_kopeks=50000))
    assert not any((c or '').startswith('tariff_sbp:') for c in callbacks)


def test_daily_insufficient_balance_offers_sbp_purchase(monkeypatch):
    from app.handlers.subscription.tariff_purchase import get_daily_tariff_insufficient_balance_keyboard

    monkeypatch.setattr(type(settings), 'is_platega_recurrent_enabled', lambda self: True)
    assert 'tariff_sbp:7' in _callbacks(get_daily_tariff_insufficient_balance_keyboard(7, 'ru'))

    monkeypatch.setattr(type(settings), 'is_platega_recurrent_enabled', lambda self: False)
    assert 'tariff_sbp:7' not in _callbacks(get_daily_tariff_insufficient_balance_keyboard(7, 'ru'))


def test_falls_back_to_topup_without_direct_payment_methods(monkeypatch):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    # Клавиатура пополнения без кнопок прямой оплаты (только навигация) → классический фолбэк
    monkeypatch.setattr(
        'app.keyboards.inline.get_payment_methods_keyboard',
        lambda amount, language=None: InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text='x', callback_data='menu_balance')]]
        ),
    )

    keyboard = get_tariff_insufficient_balance_keyboard(7, 30, 'ru', missing_kopeks=50000)

    assert 'balance_topup' in _callbacks(keyboard)


def test_extend_inlines_prefilled_payment_when_autopurchase_enabled(monkeypatch):
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', True)

    keyboard = get_tariff_extend_insufficient_balance_keyboard(7, 42, 30, 'ru', missing_kopeks=50000)
    callbacks = _callbacks(keyboard)

    assert 'topup_amount|stars|50000' in callbacks
    assert 'balance_topup' not in callbacks
    assert 'subscription_extend' in callbacks
    assert not any((c or '').startswith('tariff_select:') for c in callbacks)
    assert not any((c or '').startswith('tariff_sbp:') for c in callbacks)


def test_extend_classic_topup_when_autopurchase_disabled(monkeypatch):
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', False)
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', True)

    keyboard = get_tariff_extend_insufficient_balance_keyboard(7, 42, 30, 'ru', missing_kopeks=50000)
    callbacks = _callbacks(keyboard)

    assert 'balance_topup' in callbacks
    assert 'subscription_extend' in callbacks
    assert not any((c or '').startswith('topup_amount|') for c in callbacks)


def test_extend_prefilled_amount_is_missing_not_full_price(monkeypatch):
    """Частичная нехватка: доплата = missing (260), не цена (630) и не баланс (370)."""
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', True)

    price = 63000
    balance = 37000
    missing = price - balance  # 26000

    keyboard = get_tariff_extend_insufficient_balance_keyboard(7, 42, 30, 'ru', missing_kopeks=missing)
    callbacks = _callbacks(keyboard)

    assert f'topup_amount|stars|{missing}' in callbacks
    assert f'topup_amount|stars|{price}' not in callbacks
    assert f'topup_amount|stars|{balance}' not in callbacks
    assert 'subscription_extend' in callbacks


def test_extend_back_points_at_the_subscription_in_multi_tariff(monkeypatch):
    """«Назад» обязан адресовать конкретную подписку, иначе он никуда не ведёт.

    На экране возврата после пополнения активная подписка в FSM не закрепляется,
    а само состояние к тому моменту могло быть очищено. Голый
    `subscription_extend` при двух и более подписках упирается в «Выберите
    подписку» и НИЧЕГО не перерисовывает — кнопка выглядит сломанной. Идиома
    `se:{id}` уже используется в monitoring_service и recurrent_payment_service.
    """
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', False)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)

    keyboard = get_tariff_extend_insufficient_balance_keyboard(7, 42, 30, 'ru', missing_kopeks=50000)

    assert 'se:42' in _callbacks(keyboard)


def test_extend_back_stays_generic_without_multi_tariff(monkeypatch):
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', False)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)

    keyboard = get_tariff_extend_insufficient_balance_keyboard(7, 42, 30, 'ru', missing_kopeks=50000)

    assert 'subscription_extend' in _callbacks(keyboard)
    assert not any((c or '').startswith('se:') for c in _callbacks(keyboard))


def test_extend_falls_back_to_topup_without_direct_payment_methods(monkeypatch):
    """Фильтр строк оплаты — единственное, что делает зеркалирование безопасным.

    `get_payment_methods_keyboard` кроме способов оплаты отдаёт и свою навигацию:
    строку поддержки и собственное «Назад» на `menu_balance`. Без фильтра экран
    получил бы ДВЕ кнопки «Назад», ведущие в разные места, а запасной переход
    «Пополнить баланс» перестал бы появляться там, где прямых способов нет.
    """
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from app.keyboards import inline

    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', True)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    # Прямых способов нет — только служебные строки.
    monkeypatch.setattr(
        inline,
        'get_payment_methods_keyboard',
        lambda *a, **kw: InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text='Поддержка', callback_data='topup_support')],
                [InlineKeyboardButton(text='Назад', callback_data='menu_balance')],
            ]
        ),
    )

    callbacks = _callbacks(get_tariff_extend_insufficient_balance_keyboard(7, 42, 30, 'ru', missing_kopeks=50000))

    assert 'balance_topup' in callbacks, 'без прямых способов должен остаться классический переход'
    assert 'menu_balance' not in callbacks, 'чужая навигация не должна попадать на экран продления'
    assert 'topup_support' not in callbacks
