"""Кто подключён к VPN прямо сейчас — результат опроса панели.

Отдельно от ``panel_online``: этим типом пользуется CRUD списка пользователей, а
``panel_online`` ходит в панель через сервис Remnawave, который сам импортирует
CRUD, — общий тип в любом из них замыкал импорт по кругу.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConnectedAccounts:
    """Панельные аккаунты, подключённые прямо сейчас, и Telegram ID их владельцев."""

    panel_ids: frozenset[int]
    telegram_ids: frozenset[int]

    def has_user(self, user) -> bool:
        """Подключён ли пользователь бота хоть одним своим аккаунтом в панели.

        Ключи те же, что у фильтра списка (``_users_list_conditions``): id панели у
        пользователя (одиночный тариф), у любой подписки (мультитариф) и Telegram ID.
        """
        if user.remnawave_id in self.panel_ids:
            return True
        if any(sub.remnawave_id in self.panel_ids for sub in user.subscriptions or ()):
            return True
        return user.telegram_id is not None and user.telegram_id in self.telegram_ids
