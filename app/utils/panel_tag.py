"""Формат тега панельного пользователя Remnawave (лист-модуль: только конфиг)."""

from __future__ import annotations

from app.config import USER_TAG_PATTERN


PANEL_TAG_MAX_LENGTH = 16
PANEL_TAG_RULES = 'до 16 символов: латинские буквы, цифры и подчёркивание'


def normalize_panel_tag(value: str | None) -> str | None:
    """Привести тег к виду панели; пустое значение — «тега нет».

    Регистр поднимается сам (панель принимает только заглавные), недопустимые
    символы или длина — ``ValueError`` с понятным текстом для формы.
    """
    if value is None:
        return None
    cleaned = str(value).strip().upper()
    if not cleaned:
        return None
    if not USER_TAG_PATTERN.fullmatch(cleaned):
        raise ValueError(f'Тег панели: {PANEL_TAG_RULES}')
    return cleaned
