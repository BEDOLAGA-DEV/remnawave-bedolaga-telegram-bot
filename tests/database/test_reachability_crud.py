"""CRUD раздела «Доступность из РФ» на настоящем SQLite.

Главное — запрос сводки: последний лег на пару (хост, симка), а не «все леги»,
и учёт активных задач по виду (один VLESS и один скан на аккаунт).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.database.crud import reachability as crud
from app.database.models import ReachabilityJob, ReachabilityLeg, ReachabilityTargetPref, User
from tests.fixtures.sqlite_memory import memory_session


_TABLES = (User.__table__, ReachabilityJob.__table__, ReachabilityLeg.__table__, ReachabilityTargetPref.__table__)


async def _admin(db) -> User:
    user = User(telegram_id=1, username='admin', first_name='A', language='ru')
    db.add(user)
    await db.flush()
    return user


def _job_fields(user_id: int, **overrides) -> dict:
    fields = {
        'kind': 'probe',
        'status': 'pending',
        'trigger': 'manual',
        'started_by_user_id': user_id,
        'idempotency_key': overrides.pop('idempotency_key', 'key-1'),
        'request': {'target': 'bs-host.example:9443'},
        'targets': [{'kind': 'host', 'target_key': 'bs-host.example:9443'}],
        'units_requested': ['mts|цфо|on'],
        'units_resolved': ['mts|цфо|on'],
        'dpi': 'on',
        'estimated_kopeks': 18,
        'estimate_is_exact': True,
    }
    fields.update(overrides)
    return fields


async def test_create_get_and_update_job(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, _TABLES) as db:
        admin = await _admin(db)
        job = await crud.create_job(db, **_job_fields(admin.id))
        await db.commit()

        loaded = await crud.get_job(db, job.id)
        assert loaded is not None
        assert (loaded.kind, loaded.status, loaded.idempotency_key) == ('probe', 'pending', 'key-1')
        assert loaded.request == {'target': 'bs-host.example:9443'}

        await crud.update_job(db, loaded, status='done', cost_kopeks=18, phase=None)
        await db.commit()
        assert (await crud.get_job(db, job.id)).cost_kopeks == 18


async def test_idempotency_key_is_unique(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, _TABLES) as db:
        admin = await _admin(db)
        await crud.create_job(db, **_job_fields(admin.id))
        with pytest.raises(IntegrityError):
            await crud.create_job(db, **_job_fields(admin.id))


async def test_active_job_is_found_per_kind_only_while_running(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, _TABLES) as db:
        admin = await _admin(db)
        vless = await crud.create_job(db, **_job_fields(admin.id, kind='vless', status='running', idempotency_key='v'))
        await crud.create_job(db, **_job_fields(admin.id, kind='scan', status='done', idempotency_key='s'))
        await db.commit()

        assert (await crud.get_active_job(db, 'vless')).id == vless.id
        assert await crud.get_active_job(db, 'scan') is None
        assert [j.id for j in await crud.list_unfinished_jobs(db)] == [vless.id]


async def test_list_jobs_filters_and_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, _TABLES) as db:
        admin = await _admin(db)
        for i, kind in enumerate(('probe', 'probe', 'scan')):
            await crud.create_job(db, **_job_fields(admin.id, kind=kind, idempotency_key=f'k{i}'))
        await db.commit()

        items, total = await crud.list_jobs(db, kind='probe', limit=1)
        assert (len(items), total) == (1, 2)
        items, total = await crud.list_jobs(db, target_key='bs-host.example:9443')
        assert total == 3
        items, total = await crud.list_jobs(db, user_id=admin.id + 1)
        assert total == 0


async def test_latest_legs_returns_newest_per_target_and_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, _TABLES) as db:
        admin = await _admin(db)
        old = await crud.create_job(db, **_job_fields(admin.id, status='done', idempotency_key='old'))
        new = await crud.create_job(db, **_job_fields(admin.id, status='done', idempotency_key='new'))
        t0 = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
        leg = {
            'kind': 'probe',
            'target_key': 'bs-host.example:9443',
            'target_kind': 'host',
            'target_ref': 'h-1',
            'op_key': 'mts|цфо|on',
            'operator': 'mts',
            'region': 'ЦФО',
            'dpi': 'on',
            'raw': {},
        }
        await crud.replace_legs(
            db, old.id, [{**leg, 'verdict': 'down', 'matches_expectation': False, 'checked_at': t0}]
        )
        await crud.replace_legs(
            db,
            new.id,
            [
                {**leg, 'verdict': 'reachable', 'matches_expectation': True, 'checked_at': t0 + timedelta(hours=1)},
                {
                    **leg,
                    'op_key': 'tele2|цфо|on',
                    'operator': 'tele2',
                    'verdict': 'blocked',
                    'matches_expectation': False,
                    'checked_at': t0,
                },
            ],
        )
        await db.commit()

        latest = await crud.latest_legs(db, target_kind='host', dpi='on')
        by_unit = {leg.op_key: leg.verdict for leg in latest}
        assert by_unit == {'mts|цфо|on': 'reachable', 'tele2|цфо|on': 'blocked'}
        assert all(leg.job_id == new.id for leg in latest)


async def test_replace_legs_drops_previous_legs_of_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, _TABLES) as db:
        admin = await _admin(db)
        job = await crud.create_job(db, **_job_fields(admin.id))
        base = {
            'kind': 'probe',
            'target_key': 'a:1',
            'op_key': 'mts|цфо|on',
            'verdict': 'down',
            'raw': {},
            'checked_at': datetime.now(UTC),
        }
        await crud.replace_legs(db, job.id, [base])
        await crud.replace_legs(db, job.id, [{**base, 'verdict': 'reachable'}])
        await db.commit()
        loaded = await crud.get_job(db, job.id)
        assert [leg.verdict for leg in loaded.legs] == ['reachable']


async def test_prefs_upsert_and_list(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, _TABLES) as db:
        admin = await _admin(db)
        await crud.upsert_pref(db, target_kind='host', target_ref='h-1', purpose='bs', user_id=admin.id)
        await crud.upsert_pref(db, target_kind='host', target_ref='h-1', excluded=True, user_id=admin.id)
        await db.commit()

        pref = await crud.get_pref(db, 'host', 'h-1')
        assert (pref.purpose, pref.excluded) == ('bs', True)
        assert len(await crud.list_prefs(db)) == 1


async def test_last_vless_leg_price_uses_latest_done_vless_job(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, _TABLES) as db:
        admin = await _admin(db)
        assert await crud.last_vless_leg_price_kopeks(db) is None
        await crud.create_job(
            db,
            **_job_fields(
                admin.id,
                kind='vless',
                status='done',
                idempotency_key='v1',
                cost_kopeks=206,
                result={'submit': {'n_servers': 1, 'n_modems': 2}},
            ),
        )
        await db.commit()
        assert await crud.last_vless_leg_price_kopeks(db) == 103
