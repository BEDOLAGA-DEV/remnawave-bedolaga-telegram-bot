"""
Тест разовой чистки дублей тарифных подписок (subscription_dedup_service).

DB-only: удаляем только лишние ИСТЁКШИЕ/отключённые дубли (строки в БД), живые
(active/limited/trial), одиночные и pending не трогаем. Панель сервис не трогает
(её юзеров не удаляем — слишком рискованно), поэтому выжившая подписка и её
панель-юзер всегда целы.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from app.database.models import SubscriptionStatus
from app.services import subscription_dedup_service as dedup


def _sub(sub_id, user_id, tariff_id, status, days_from_now):
    s = MagicMock()
    s.id = sub_id
    s.user_id = user_id
    s.tariff_id = tariff_id
    s.status = status
    s.end_date = datetime.now(UTC) + timedelta(days=days_from_now)
    s.is_trial = False
    return s


def _patch(monkeypatch, subs):
    db = AsyncMock()
    db.commit = AsyncMock()
    deleted: list = []
    db.delete = AsyncMock(side_effect=lambda obj: deleted.append(obj))

    result = MagicMock()
    result.scalars.return_value.all.return_value = subs
    db.execute = AsyncMock(return_value=result)

    acm = MagicMock()
    acm.__aenter__ = AsyncMock(return_value=db)
    acm.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(dedup, 'AsyncSessionLocal', MagicMock(return_value=acm))

    return deleted, db


async def test_collapses_report_scenario(monkeypatch):
    subs = [
        # user 1, тариф 1: active + 2 истёкших → остаётся active
        _sub(1, 1, 1, SubscriptionStatus.ACTIVE.value, 14),
        _sub(2, 1, 1, SubscriptionStatus.EXPIRED.value, -14),
        _sub(3, 1, 1, SubscriptionStatus.EXPIRED.value, -59),
        # user 1, тариф 2: 2 истёкших → остаётся самый свежий
        _sub(4, 1, 2, SubscriptionStatus.EXPIRED.value, -1),
        _sub(5, 1, 2, SubscriptionStatus.EXPIRED.value, -30),
        # user 2, тариф 1: одна active → не трогаем
        _sub(6, 2, 1, SubscriptionStatus.ACTIVE.value, 20),
    ]
    deleted, db = _patch(monkeypatch, subs)

    stats = await dedup._run_dedupe()

    assert {s.id for s in deleted} == {2, 3, 5}
    assert stats == {'removed_db': 3}
    db.commit.assert_awaited_once()


async def test_never_removes_alive_even_if_outranked_by_date(monkeypatch):
    subs = [
        _sub(1, 1, 1, SubscriptionStatus.EXPIRED.value, 30),  # дата позже, но истёкшая
        _sub(2, 1, 1, SubscriptionStatus.ACTIVE.value, 1),  # активная, дата раньше
    ]
    deleted, _db = _patch(monkeypatch, subs)

    await dedup._run_dedupe()

    assert {s.id for s in deleted} == {1}  # удалён только истёкший дубль


async def test_disabled_duplicate_is_removed_active_survives(monkeypatch):
    subs = [
        _sub(1, 1, 1, SubscriptionStatus.ACTIVE.value, 10),
        _sub(2, 1, 1, SubscriptionStatus.DISABLED.value, -5),
    ]
    deleted, _db = _patch(monkeypatch, subs)

    await dedup._run_dedupe()

    assert {s.id for s in deleted} == {2}


async def test_single_rows_untouched(monkeypatch):
    subs = [
        _sub(1, 1, 1, SubscriptionStatus.EXPIRED.value, -5),
        _sub(2, 1, 2, SubscriptionStatus.ACTIVE.value, 10),
    ]
    deleted, db = _patch(monkeypatch, subs)

    stats = await dedup._run_dedupe()

    assert deleted == []
    assert stats == {'removed_db': 0}
    db.commit.assert_not_awaited()
