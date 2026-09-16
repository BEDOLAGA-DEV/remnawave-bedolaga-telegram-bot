"""Ошибки клиента Remnawave и разбор номера аккаунта панели — без зависимостей.

Вынесено из ``remnawave_api``: клиент на уровне модуля тянет настройки, а через
них — CRUD, поэтому любой, кому нужен только разбор номера (снимки грейса в базе),
попадал в круговой импорт. Клиент реэкспортирует всё отсюда под прежними именами.
"""

from __future__ import annotations

from typing import Any


class RemnaWaveAPIError(Exception):
    def __init__(self, message: str, status_code: int = None, response_data: dict = None):
        self.message = message
        self.status_code = status_code
        self.response_data = response_data
        super().__init__(self.message)


class RemnaWaveInvalidUserIdError(RemnaWaveAPIError):
    """Локальный идентификатор панельного пользователя непригоден к запросу.

    Все user-эндпоинты 3.0.0 параметризованы ``numberParamSchema =
    z.coerce.number().positive()``. Нечисловое значение (протухший UUID, None,
    пустая строка) коерсится в NaN, и панель отвечает **400 VALIDATION, а не
    404**. Это опасно: ``is_user_not_found_error`` такой ответ не распознаёт,
    зато распознал бы, если бы мы ослабили её до «любой 400» — и тогда каждый
    промах идентификатора уходил бы в ветку «пользователя нет → создать»,
    плодя дубли в панели.

    Поэтому мусорный идентификатор отсекается на границе клиента и никогда не
    доходит до сети. Тип отдельный, чтобы вызывающий код мог отличить «у нас
    битая ссылка в БД» от «панель отвергла запрос».
    """


def coerce_panel_user_id(value: Any) -> int:
    """Привести локально хранимый идентификатор к числовому id панели.

    Принимает int и строку из цифр (БД отдаёт BigInteger, но JSON/FSM могут
    донести строку). Всё остальное — ошибка, а не запрос в панель.
    """
    if isinstance(value, bool):
        raise RemnaWaveInvalidUserIdError(f'Invalid panel user id: {value!r}')
    if isinstance(value, int):
        candidate = value
    elif isinstance(value, str) and (stripped := value.strip()).isascii() and stripped.isdigit():
        # Строго ASCII-цифры. `isdigit()` в одиночку истинен для '²' и '٥',
        # которые int() либо не принимает вовсе, либо молча переводит в число;
        # а голый int() вдобавок принимает '4_2' и '+42' и превращает их в 42,
        # то есть в id ДРУГОГО пользователя. Для граничной проверки расширять
        # приём нельзя — только сужать.
        candidate = int(stripped)
    else:
        raise RemnaWaveInvalidUserIdError(f'Invalid panel user id: {value!r}')
    if candidate <= 0:
        raise RemnaWaveInvalidUserIdError(f'Invalid panel user id: {value!r}')
    return candidate
