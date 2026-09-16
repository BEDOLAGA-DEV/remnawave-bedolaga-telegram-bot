"""Признак открытого грейса на самой подписке.

Баг 2026-09-15 (жалобы из «Багов», 5+ аккаунтов): мониторинг, гася истёкшую
подписку, спросил панель «может, продлили в обход бота?», увидел ACTIVE до конца
грейса и перенёс в бота дату, статус, сквад грейса и лимит «расход + 1 ГБ».
Воркер грейса принял это за продление: «человек продлил — вернули обычный тариф»,
человек остался в скваде грейса с лимитом в гигабайт.

Импорт «панель — истина» защищался флагом ``grace_open``, который каждый вызывающий
должен был не забыть передать; трое забыли. Теперь признак лежит на подписке, его
ведёт хранилище сессий в той же транзакции, что и состояние сессии, — импорт видит
его сам.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.database.models import GraceAccessSessionModel, Subscription, User
from app.services.grace_access_service import (
    GraceAccessSession,
    GraceBillingState,
    GraceCompletionReason,
    GracePanelOverlay,
    GracePanelSnapshot,
    GraceReason,
    GraceSessionState,
)


NOW = datetime(2026, 9, 15, 6, 16, tzinfo=UTC)
GIB = 1024**3
PANEL_ID = 7
TARIFF_SQUAD = '11111111-1111-1111-1111-111111111111'
GRACE_SQUAD = '22222222-2222-2222-2222-222222222222'


def _session(state: GraceSessionState = GraceSessionState.PENDING) -> GraceAccessSession:
    end_at = NOW - timedelta(minutes=1)
    billing = GraceBillingState(
        subscription_id=42,
        remnawave_id=PANEL_ID,
        status='expired',
        end_at=end_at,
        traffic_limit_bytes=0,
        used_traffic_bytes=102 * GIB,
        device_limit=5,
        squad_uuids=(TARIFF_SQUAD,),
    )
    panel = GracePanelSnapshot(
        remnawave_id=PANEL_ID,
        status='EXPIRED',
        expire_at=end_at,
        traffic_limit_bytes=0,
        used_traffic_bytes=102 * GIB,
        squad_uuids=(TARIFF_SQUAD,),
    )
    overlay = GracePanelOverlay(
        status='ACTIVE',
        expire_at=NOW + timedelta(hours=72),
        traffic_limit_bytes=103 * GIB,
        squad_uuids=(GRACE_SQUAD,),
    )
    return GraceAccessSession(
        id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
        subscription_id=42,
        remnawave_id=PANEL_ID,
        reason=GraceReason.EXPIRED,
        incident_key=f'expired:{end_at.isoformat()}',
        state=state,
        billing_before=billing,
        panel_before=panel,
        overlay=overlay,
        started_at=NOW,
        grace_until=NOW + timedelta(hours=72),
        updated_at=NOW,
    )


async def _marker(db) -> bool:
    return (await db.execute(select(Subscription.grace_session_open).where(Subscription.id == 42))).scalar_one()


@pytest.mark.asyncio
async def test_the_marker_follows_the_session_from_create_to_completion(monkeypatch):
    from app.services.grace_access_runtime import SQLAlchemyGraceSessionStore
    from tests.fixtures.sqlite_memory import memory_session

    tables = [User.__table__, Subscription.__table__, GraceAccessSessionModel.__table__]
    async with memory_session(monkeypatch, tables) as db:
        db.add(User(id=1, telegram_id=100, remnawave_id=PANEL_ID))
        db.add(
            Subscription(
                id=42,
                user_id=1,
                status='active',
                end_date=NOW - timedelta(minutes=1),
                remnawave_short_id='sid42',
            )
        )
        await db.commit()
        assert await _marker(db) is False

        store = SQLAlchemyGraceSessionStore(db)
        created = await store.create(_session())
        assert await _marker(db) is True, 'PENDING уже держит аккаунт: оверлей вот-вот уйдёт в панель'

        active = await store.save(replace(created, state=GraceSessionState.ACTIVE))
        await db.commit()
        assert await _marker(db) is True

        await store.save(
            replace(
                active,
                state=GraceSessionState.COMPLETED,
                completion_reason=GraceCompletionReason.TIMEOUT,
                completed_at=NOW + timedelta(hours=72),
            )
        )
        await db.commit()
        assert await _marker(db) is False, 'после конца грейса импорт снова берёт дату и статус из панели'


@pytest.mark.asyncio
async def test_a_save_that_lost_the_race_does_not_touch_the_marker(monkeypatch):
    """Проигравший оптимистичную гонку ничего не записал — и признак не меняет."""
    from app.services.grace_access_runtime import SQLAlchemyGraceSessionStore
    from tests.fixtures.sqlite_memory import memory_session

    tables = [User.__table__, Subscription.__table__, GraceAccessSessionModel.__table__]
    async with memory_session(monkeypatch, tables) as db:
        db.add(User(id=1, telegram_id=100, remnawave_id=PANEL_ID))
        db.add(Subscription(id=42, user_id=1, status='active', end_date=NOW, remnawave_short_id='sid42'))
        await db.commit()

        store = SQLAlchemyGraceSessionStore(db)
        created = await store.create(_session())
        await store.save(replace(created, state=GraceSessionState.ACTIVE))
        await db.commit()

        # Устаревшая версия: другой воркер уже сохранил ACTIVE.
        stale = replace(
            created,
            state=GraceSessionState.COMPLETED,
            completion_reason=GraceCompletionReason.TIMEOUT,
            completed_at=NOW,
        )
        winner = await store.save(stale)
        await db.commit()

        assert winner.state is GraceSessionState.ACTIVE
        assert await _marker(db) is True


@pytest.mark.asyncio
async def test_the_overlay_date_is_written_with_the_session_and_outlives_it(monkeypatch):
    """Дата оверлея ложится на подписку вместе с сессией (до PATCH) и не стирается при закрытии.

    Снимок панели, снятый при открытой сессии, может обрабатываться уже после
    досрочного закрытия грейса: признак снят, хвост — другая дата. Дата оверлея
    остаётся — по ней снимок узнаётся как оверлей.
    """
    from app.services.grace_access_runtime import SQLAlchemyGraceSessionStore
    from tests.fixtures.sqlite_memory import memory_session

    tables = [User.__table__, Subscription.__table__, GraceAccessSessionModel.__table__]
    async with memory_session(monkeypatch, tables) as db:
        db.add(User(id=1, telegram_id=100, remnawave_id=PANEL_ID))
        db.add(
            Subscription(
                id=42, user_id=1, status='active', end_date=NOW - timedelta(minutes=1), remnawave_short_id='sid42'
            )
        )
        await db.commit()

        store = SQLAlchemyGraceSessionStore(db)
        created = await store.create(_session())
        overlay_date = (
            await db.execute(select(Subscription.grace_overlay_expire_at).where(Subscription.id == 42))
        ).scalar_one()
        assert abs((overlay_date.replace(tzinfo=UTC) - created.overlay.expire_at).total_seconds()) < 1

        await store.save(
            replace(
                created,
                state=GraceSessionState.COMPLETED,
                completion_reason=GraceCompletionReason.CONFLICT,
                completed_at=NOW + timedelta(minutes=5),
            )
        )
        await db.commit()
        kept = (
            await db.execute(select(Subscription.grace_overlay_expire_at).where(Subscription.id == 42))
        ).scalar_one()
        assert kept is not None, 'после досрочного закрытия дата оверлея нужна, чтобы узнать запоздалый снимок'
