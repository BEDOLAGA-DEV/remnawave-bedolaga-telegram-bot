"""README обязан перечислять все платёжные шлюзы, которые умеет бот.

Подключение нового шлюза задевает около двадцати мест, и таблица в README —
единственное из них, которое ничего не ломает, если про него забыть. Ровно так
и вышло с CisPay: код, миграция, локали и кабинет на месте, а в списке
провайдеров строки нет. Заметить это глазами нельзя — в таблице почти три
десятка строк.

Здесь же проверяется заявленное в тексте количество: «24+ провайдера» пережило
три интеграции подряд.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.database.models import PaymentMethod


README_PATH = Path(__file__).resolve().parents[1] / 'README.md'

# Не шлюзы: внутренний перевод с баланса и ручное начисление админом.
NOT_A_GATEWAY = frozenset({'balance', 'manual'})

# Как провайдер называется в таблице, если это не сам код метода.
DISPLAY_NAMES = {
    'telegram_stars': 'Telegram Stars',
    'apple_iap': 'Apple In-App Purchase',
    'kassa_ai': 'Kassa AI',
    'pal24': 'Pal24',
}

TABLE_HEADING = '## 💳 Платёжные провайдеры'


def _gateways() -> list[str]:
    return sorted({method.value for method in PaymentMethod} - NOT_A_GATEWAY)


def _readme() -> str:
    return README_PATH.read_text(encoding='utf-8')


def _provider_table() -> str:
    text = _readme()
    assert TABLE_HEADING in text, 'в README нет раздела с таблицей провайдеров'
    return text.split(TABLE_HEADING, 1)[1].split('</div>', 1)[0]


def _squash(value: str) -> str:
    """Убирает всё, кроме букв и цифр: разметка и пробелы тут не значимы."""
    return re.sub(r'[^a-z0-9]', '', value.lower())


@pytest.mark.parametrize('gateway', _gateways())
def test_every_gateway_is_listed_in_readme(gateway: str) -> None:
    expected = _squash(DISPLAY_NAMES.get(gateway, gateway))
    assert expected in _squash(_provider_table()), (
        f'шлюз {gateway} есть в PaymentMethod, но его нет в таблице провайдеров README'
    )


def test_table_has_no_rows_for_unknown_providers() -> None:
    """Каждая строка таблицы указывает на существующий шлюз.

    Обратная сторона предыдущей проверки: та ловит пропажу, эта — лишнее.
    Строка про выпиленного провайдера вводит в заблуждение так же, как и
    отсутствие строки про настоящего.

    Считать строки нельзя: таблица перечисляет способы оплаты, как их видит
    покупатель, а один шлюз может давать несколько. У YooKassa карты и СБП
    показаны отдельными строками, хотя ``PaymentMethod`` у них один.
    """
    known = {_squash(DISPLAY_NAMES.get(gateway, gateway)) for gateway in _gateways()}

    unknown = []
    for line in _provider_table().splitlines():
        if not line.startswith('|') or '**' not in line:
            continue
        title = _squash(re.search(r'\*\*(.+?)\*\*', line).group(1))
        # Название в строке содержит имя шлюза: «YooKassa СБП» — подметод,
        # «PayPalych (Pal24)» — торговое имя рядом с кодовым.
        if not any(name in title for name in known):
            unknown.append(title)

    assert not unknown, f'в таблице есть строки без соответствующего шлюза в PaymentMethod: {unknown}'


def test_claimed_provider_count_matches_reality() -> None:
    """Число провайдеров в тексте не должно отставать от кода."""
    claims = re.findall(r'(\d+)\+?\s+(?:платёжных\s+)?провайдер\w*', _readme())
    assert claims, 'в README не нашлось ни одного упоминания количества провайдеров'

    expected = str(len(_gateways()))
    wrong = sorted({claim for claim in claims if claim != expected})
    assert not wrong, f'в README заявлено провайдеров: {wrong}, а на деле {expected}'
