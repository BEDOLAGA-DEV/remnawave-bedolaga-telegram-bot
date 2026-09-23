"""Фильтр сквадов на границе отправки в панель."""

import pytest

from app.utils.premium_traffic import effective_panel_squads, exclude_limited_squads


SQUAD = 'e4f819ca-2cfd-4425-9354-16a262b180c1'
OTHER = '82a12389-14d6-40c6-b320-4674f6bbb344'
THIRD = '3ca79b63-1b0d-49ec-b2d7-6eb264a560c5'


def _patch_limited(monkeypatch, limited):
    async def _fake(_db, _subscription_id):
        return set(limited)

    monkeypatch.setattr('app.database.crud.premium_traffic.get_limited_squad_uuids', _fake)


def test_pure_filter_keeps_order():
    """Панель принимает список; перестановка сделала бы диффы нечитаемыми."""
    assert exclude_limited_squads([THIRD, SQUAD, OTHER], {SQUAD}) == [THIRD, OTHER]


def test_pure_filter_without_limits_is_a_copy():
    source = [SQUAD, OTHER]

    result = exclude_limited_squads(source, set())

    assert result == source
    assert result is not source


def test_pure_filter_tolerates_none():
    assert exclude_limited_squads(None, {SQUAD}) == []


async def test_limited_squad_is_removed(monkeypatch):
    _patch_limited(monkeypatch, {SQUAD})

    assert await effective_panel_squads(1, [SQUAD, OTHER]) == [OTHER]


async def test_nothing_limited_keeps_the_whole_set(monkeypatch):
    _patch_limited(monkeypatch, set())

    assert await effective_panel_squads(1, [SQUAD, OTHER]) == [SQUAD, OTHER]


async def test_none_stays_none(monkeypatch):
    """«Сквады не трогаем» и «список пуст» — разные намерения."""
    _patch_limited(monkeypatch, {SQUAD})

    assert await effective_panel_squads(1, None) is None


async def test_all_squads_limited_gives_empty_list(monkeypatch):
    """Осмысленный результат: update_user трактует [] как «снять все»."""
    _patch_limited(monkeypatch, {SQUAD, OTHER})

    assert await effective_panel_squads(1, [SQUAD, OTHER]) == []


async def test_empty_input_does_not_touch_the_database(monkeypatch):
    called = False

    async def _fake(_db, _subscription_id):
        nonlocal called
        called = True
        return set()

    monkeypatch.setattr('app.database.crud.premium_traffic.get_limited_squad_uuids', _fake)

    assert await effective_panel_squads(1, []) == []
    assert called is False


async def test_missing_subscription_id_skips_the_lookup(monkeypatch):
    called = False

    async def _fake(_db, _subscription_id):
        nonlocal called
        called = True
        return {SQUAD}

    monkeypatch.setattr('app.database.crud.premium_traffic.get_limited_squad_uuids', _fake)

    assert await effective_panel_squads(None, [SQUAD]) == [SQUAD]
    assert called is False


async def test_database_failure_sends_the_set_unfiltered(monkeypatch):
    """Отказ БД не должен рвать синхронизацию и не должен отбирать доступ.

    Пользователь на несколько минут получит снятый сквад обратно — следующий
    проход воркера его уберёт. Обратный выбор, снять сквады «на всякий случай»,
    отобрал бы доступ у тех, кто ни при чём.
    """

    async def _boom(_db, _subscription_id):
        raise RuntimeError('база недоступна')

    monkeypatch.setattr('app.database.crud.premium_traffic.get_limited_squad_uuids', _boom)

    assert await effective_panel_squads(1, [SQUAD, OTHER]) == [SQUAD, OTHER]


async def test_given_session_is_used_as_is(monkeypatch):
    seen = {}

    async def _fake(db, subscription_id):
        seen['db'] = db
        seen['subscription_id'] = subscription_id
        return {SQUAD}

    monkeypatch.setattr('app.database.crud.premium_traffic.get_limited_squad_uuids', _fake)
    sentinel = object()

    result = await effective_panel_squads(42, [SQUAD, OTHER], db=sentinel)

    assert result == [OTHER]
    assert seen == {'db': sentinel, 'subscription_id': 42}


async def test_own_session_is_opened_when_none_given(monkeypatch):
    opened = False

    class _Session:
        async def __aenter__(self):
            nonlocal opened
            opened = True
            return self

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr('app.database.database.AsyncSessionLocal', _Session)

    async def _fake(_db, _subscription_id):
        return {SQUAD}

    monkeypatch.setattr('app.database.crud.premium_traffic.get_limited_squad_uuids', _fake)

    assert await effective_panel_squads(1, [SQUAD, OTHER]) == [OTHER]
    assert opened is True


@pytest.mark.parametrize('squads', [[SQUAD], [SQUAD, OTHER, THIRD]])
async def test_filtering_never_invents_squads(monkeypatch, squads):
    _patch_limited(monkeypatch, {OTHER})

    result = await effective_panel_squads(1, squads)

    assert set(result) <= set(squads)
