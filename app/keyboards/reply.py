from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from typing import List

from app.localization.texts import get_texts
from app.config import settings


def get_main_reply_keyboard(language: str = "ru") -> ReplyKeyboardMarkup:
    texts = get_texts(language)
    
    keyboard = [
        [
            KeyboardButton(text=texts.MENU_SUBSCRIPTION)
        ]
    ]
    
    # Добавляем кнопки промокода и рефералов, учитывая настройки
    second_row = [KeyboardButton(text=texts.MENU_PROMOCODE)]
    
    # Добавляем кнопку рефералов только если программа включена
    if settings.is_referral_program_enabled():
        second_row.append(KeyboardButton(text=texts.MENU_REFERRALS))
    
    keyboard.append(second_row)
    
    keyboard.append([
        KeyboardButton(text=texts.MENU_SUPPORT),
        KeyboardButton(text=texts.MENU_RULES)
    ])
    
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        one_time_keyboard=False
    )


def get_admin_reply_keyboard(language: str = "ru") -> ReplyKeyboardMarkup:
    texts = get_texts(language)
    
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=texts.ADMIN_USERS),
                KeyboardButton(text=texts.ADMIN_SUBSCRIPTIONS)
            ],
            [
                KeyboardButton(text=texts.ADMIN_PROMOCODES),
                KeyboardButton(text=texts.ADMIN_MESSAGES)
            ],
            [
                KeyboardButton(text=texts.ADMIN_STATISTICS),
                KeyboardButton(text=texts.ADMIN_MONITORING)
            ],
            [
                KeyboardButton(text=texts.t("ADMIN_MAIN_MENU", "🏠 Главное меню"))
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )


def get_cancel_keyboard(language: str = "ru") -> ReplyKeyboardMarkup:
    texts = get_texts(language)
    
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=texts.CANCEL)]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )


def get_confirmation_reply_keyboard(language: str = "ru") -> ReplyKeyboardMarkup:
    texts = get_texts(language)
    
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=texts.YES),
                KeyboardButton(text=texts.NO)
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )


def get_skip_keyboard(language: str = "ru") -> ReplyKeyboardMarkup:
    texts = get_texts(language)
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=texts.REFERRAL_CODE_SKIP)]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )


def remove_keyboard() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def get_contact_keyboard(language: str = "ru") -> ReplyKeyboardMarkup:
    texts = get_texts(language)
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=texts.t("SEND_CONTACT_BUTTON", "📱 Отправить контакт"), request_contact=True)],
            [KeyboardButton(text=texts.CANCEL)]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )


def get_location_keyboard(language: str = "ru") -> ReplyKeyboardMarkup:
    texts = get_texts(language)
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=texts.t("SEND_LOCATION_BUTTON", "📍 Отправить геолокацию"), request_location=True)],
            [KeyboardButton(text=texts.CANCEL)]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
