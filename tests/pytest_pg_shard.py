"""Opt-in deterministic CI partitions; each job owns its PostgreSQL instance."""

import hashlib

import pytest


def partition_items(items, index: int, count: int):
    if count <= 0 or not 0 <= index < count:
        raise ValueError('Shard index must be in [0, count), with count > 0')
    selected, deselected = [], []
    for item in items:
        shard = int.from_bytes(hashlib.sha256(item.nodeid.encode()).digest(), 'big') % count
        (selected if shard == index else deselected).append(item)
    return selected, deselected


def pytest_addoption(parser):
    parser.addoption('--pg-shard-index', type=int, default=None)
    parser.addoption('--pg-shard-count', type=int, default=None)


def pytest_configure(config):
    index = config.getoption('--pg-shard-index')
    count = config.getoption('--pg-shard-count')
    if index is None and count is None:
        return
    if index is None or count is None or count <= 0 or not 0 <= index < count:
        raise pytest.UsageError('Specify valid --pg-shard-index and --pg-shard-count together')


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    index = config.getoption('--pg-shard-index')
    if index is None:
        return
    selected, deselected = partition_items(items, index, config.getoption('--pg-shard-count'))
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)
