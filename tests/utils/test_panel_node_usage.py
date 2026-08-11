"""Каноническая форма потребления по ноде.

Обе админские поверхности (Web API и кабинет) отдают наружу результат этой
функции. Раньше они разъезжались по ключам молча — схема кабинета
`list[dict[str, Any]]`, pydantic там ничего не проверяет, и фронт просто
рисовал пустые ячейки. Поэтому форма пиннится тестом.
"""

from app.utils.panel_node_usage import coerce_bytes, normalize_node_usage


def test_normalizes_the_production_shape():
    """ЕДИНСТВЕННАЯ форма, которая реально сюда приходит: {userId, nodeUuid, total}.

    `RemnaWaveService.get_node_user_usage_by_range` уже перекладывает ответ
    `POST /api/bandwidth-stats/nodes/usage` из {id, totalBytes} в эти ключи, так
    что нормализатор видит именно их. Этот тест — единственный страж продового
    пути; удалять его нельзя.
    """
    items = normalize_node_usage([{'userId': 8812, 'nodeUuid': 'n-1', 'total': 4096}], 'fallback')
    assert items == [{'user_id': 8812, 'username': None, 'node_uuid': 'n-1', 'total_bytes': 4096}]


def test_also_tolerates_the_raw_3_0_0_keys():
    """Сырые ключи панели {id, totalBytes} — запас на случай, если сюда однажды
    придёт неперелоденный ответ.

    Сейчас ни один вызывающий их не передаёт (сервис перекладывает ключи сам),
    так что это терпимость, а не продовый путь. Тест держит её честной: без
    ключей в `_first_present` он падает.
    """
    items = normalize_node_usage([{'id': 8812, 'totalBytes': 4096}], 'n-1')
    assert items == [{'user_id': 8812, 'username': None, 'node_uuid': 'n-1', 'total_bytes': 4096}]


def test_accepts_the_already_normalised_shape():
    items = normalize_node_usage([{'user_id': 1, 'username': 'bob', 'node_uuid': 'n-2', 'total_bytes': 7}], 'fallback')
    assert items[0]['username'] == 'bob'
    assert items[0]['total_bytes'] == 7


def test_falls_back_to_the_requested_node_uuid():
    items = normalize_node_usage([{'id': 5, 'totalBytes': 1}], 'n-fallback')
    assert items[0]['node_uuid'] == 'n-fallback'


def test_skips_non_dict_entries_and_tolerates_missing_fields():
    items = normalize_node_usage(['garbage', None, {}], 'n-1')
    assert len(items) == 1
    assert items[0] == {'user_id': None, 'username': None, 'node_uuid': 'n-1', 'total_bytes': 0}


def test_non_numeric_totals_do_not_raise():
    assert coerce_bytes('nope') == 0
    assert coerce_bytes(None) == 0
    assert coerce_bytes('12') == 12


def test_empty_input():
    assert normalize_node_usage(None, 'n-1') == []
    assert normalize_node_usage([], 'n-1') == []
