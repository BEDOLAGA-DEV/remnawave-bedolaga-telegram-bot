"""Способы входа в аккаунт — модуль без зависимостей от CRUD и сервисов.

Жили в ``crud.user`` и ``account_merge_service``, но условия напоминаний
(``services.user_reminders.conditions``) нужны им, а оба модуля тянут за собой
полпроекта вплоть до мониторинга, который запускает сами напоминания. Получалось
кольцо импортов (CodeQL py/cyclic-import). Старые имена там по-прежнему доступны.
"""

from __future__ import annotations

from typing import Any


OAUTH_PROVIDER_COLUMNS: dict[str, str] = {
    'google': 'google_id',
    'yandex': 'yandex_id',
    'discord': 'discord_id',
    'vk': 'vk_id',
}


def compute_auth_methods(user: Any) -> list[str]:
    """Вычисляет список методов авторизации пользователя."""
    methods: list[str] = []
    if user.telegram_id:
        methods.append('telegram')
    if user.email and user.password_hash:
        methods.append('email')
    for provider, column in OAUTH_PROVIDER_COLUMNS.items():
        if getattr(user, column, None):
            methods.append(provider)
    return methods
