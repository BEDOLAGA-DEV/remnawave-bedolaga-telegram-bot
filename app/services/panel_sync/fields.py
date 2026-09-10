"""Набор полей узкого PATCH в панель — один для кабинета и бота.

Поштучный «в панель» с карточки пользователя шлёт то, что разрешил админ, плюс
поля, описывающие сам аккаунт (а не подписку): описание, лимит устройств, внешний
сквад и тег панели. Тег здесь обязателен — иначе тег тарифа не доезжает до панели
через поштучный синк.
"""

from __future__ import annotations


PANEL_ACCOUNT_METADATA_FIELDS = frozenset({'description', 'hwid_device_limit', 'external_squad_uuid', 'tag'})


def narrow_push_fields(
    *,
    status: bool = False,
    expire_date: bool = False,
    traffic_limit: bool = False,
    squads: bool = False,
    extra: set[str] | frozenset[str] | None = None,
) -> set[str]:
    """Поля для ``push_subscription(only_fields=...)`` по флагам админа."""
    fields = set(PANEL_ACCOUNT_METADATA_FIELDS)
    if status:
        fields.add('status')
    if expire_date:
        fields.add('expire_at')
    if traffic_limit:
        fields.update({'traffic_limit_bytes', 'traffic_limit_strategy'})
    if squads:
        fields.add('active_internal_squads')
    if extra:
        fields.update(extra)
    return fields
