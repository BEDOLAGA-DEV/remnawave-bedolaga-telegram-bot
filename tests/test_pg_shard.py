"""CI partitions must preserve every collected test exactly once."""

import pytest

from tests.pytest_pg_shard import partition_items


class Item:
    def __init__(self, nodeid):
        self.nodeid = nodeid


def test_partitions_cover_all_items_once_and_are_order_independent():
    items = [Item(f'test_case.py::test_case[{i}]') for i in range(2773)]
    shards = [partition_items(items, index, 4)[0] for index in range(4)]
    assert {id(item) for shard in shards for item in shard} == {id(item) for item in items}
    assert sum(map(len, shards)) == len(items)
    assert all(shards)
    for index, shard in enumerate(shards):
        selected, deselected = partition_items(items, index, 4)
        assert selected == shard
        assert len(selected) + len(deselected) == len(items)
        assert partition_items(list(reversed(items)), index, 4)[0] == list(reversed(shard))


@pytest.mark.parametrize(('index', 'count'), [(-1, 4), (4, 4), (0, 0), (0, -1)])
def test_invalid_partition_is_an_error(index, count):
    with pytest.raises(ValueError):
        partition_items([Item('test_example')], index, count)
