"""Каноническая форма пользовательского трафика по ноде.

До Remnawave 3.0.0 оба админских хендлера (Web API и кабинет) отдавали наружу
сырой ответ legacy-эндпоинта панели — `{userUuid, username, nodeUuid, total,
date}` — и были байт-в-байт одинаковыми. Legacy удалён, а у его замен ключи
разные: `POST /api/bandwidth-stats/nodes/usage` даёт `{id, totalBytes}`,
`GET /api/bandwidth-stats/nodes/{uuid}/users` — `{username, total}` вообще без
идентификатора пользователя.

Нормализация живёт здесь, а не в каждом хендлере: иначе поверхности разъезжаются
молча (pydantic ничего не проверяет у `dict[str, Any]`), и фронт одной из них
начинает читать ключи, которых больше нет. `date` не отдаётся — суточной
разбивки по ноде в 3.0.0 не существует.
"""

from __future__ import annotations

from typing import Any


def _first_present(raw: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = raw.get(key)
        if value is not None:
            return value
    return None


def coerce_bytes(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def normalize_node_usage(raw_items: Any, node_uuid: str) -> list[dict[str, Any]]:
    """Привести элементы потребления к форме `{user_id, username, node_uuid, total_bytes}`.

    Принимает любую из известных форм панели; неизвестные элементы пропускает.

    Про ``username``: сохранён в форме, но у единственного источника, где есть
    идентификатор пользователя (`POST /api/bandwidth-stats/nodes/usage` →
    `{id, totalBytes}`), имени нет — панель его на этом маршруте не отдаёт.
    Поэтому на практике поле пустое, и админ видит числовой id. Заполнить его
    можно только соединением с таблицей пользователей бота, а у роутов-
    потребителей сессии БД нет; если это понадобится — добавлять её надо им,
    а не подставлять сюда неиспользуемый параметр.
    """
    items: list[dict[str, Any]] = []
    for raw in raw_items or []:
        if not isinstance(raw, dict):
            continue
        raw_user_id = _first_present(raw, 'user_id', 'id', 'userId')
        try:
            parsed_user_id = int(raw_user_id) if raw_user_id is not None else None
        except (TypeError, ValueError):
            parsed_user_id = None
        items.append(
            {
                'user_id': parsed_user_id,
                'username': raw.get('username'),
                'node_uuid': _first_present(raw, 'node_uuid', 'nodeUuid') or node_uuid,
                'total_bytes': coerce_bytes(_first_present(raw, 'total_bytes', 'totalBytes', 'total')),
            }
        )
    return items
