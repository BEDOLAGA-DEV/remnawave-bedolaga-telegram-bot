"""Приветственный экран после регистрации: что предлагать, если подписка уже есть.

Бонус рекламной кампании создаёт подписку САМ, без шага «активировать
пробную». Экран при этом продолжал предлагать «🚀 Подключиться бесплатно»
(callback ``trial_activate``) — активацию триала, которого у человека уже
нет. Во второй ветке было не лучше: при существующей подписке кнопку
триала убирали, но взамен оставляли только «Назад», и пользователь уходил,
так и не узнав, как подключить устройство.
"""

from __future__ import annotations

from app.keyboards.inline import get_post_registration_keyboard


def _callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def test_offers_trial_activation_when_no_subscription():
    """Обычная регистрация: подписки нет — предлагаем активировать пробную."""
    markup = get_post_registration_keyboard('ru')
    assert _callbacks(markup) == ['trial_activate', 'back_to_menu']


def test_offers_connect_when_subscription_already_granted():
    """Подписку выдала кампания — ведём подключать устройство, а не в триал."""
    markup = get_post_registration_keyboard('ru', has_active_subscription=True)
    callbacks = _callbacks(markup)

    assert callbacks == ['subscription_connect', 'back_to_menu']
    assert 'trial_activate' not in callbacks


def test_skip_button_survives_in_both_modes():
    """Уйти с экрана можно в любом случае — «Пропустить» на месте."""
    for granted in (False, True):
        markup = get_post_registration_keyboard('ru', has_active_subscription=granted)
        assert 'back_to_menu' in _callbacks(markup)
