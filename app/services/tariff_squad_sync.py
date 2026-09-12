"""Фоновая синхронизация сквадов тарифа в панель после правки тарифа.

Одна точка для кабинета и телеграм-редактора: изменился список серверов или
внешний сквад тарифа — все живые подписки тарифа получают новые сквады в панели.
"""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import joinedload

from app.config import settings
from app.database.crud.tariff import get_tariff_by_id
from app.database.models import Subscription, SubscriptionStatus, User
from app.services.panel_sync import patch_panel_squads


logger = structlog.get_logger(__name__)

_SYNC_CONCURRENCY = 5
_background_tasks: set[asyncio.Task[None]] = set()


def schedule_tariff_squad_sync(tariff_id: int, admin_id: int) -> asyncio.Task[None]:
    """Запустить синхронизацию в фоне; ссылка на задачу держится до её конца."""
    task = asyncio.create_task(
        sync_tariff_squads_in_background(tariff_id, admin_id), name=f'sync-squads-tariff-{tariff_id}'
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def sync_tariff_squads_in_background(tariff_id: int, admin_id: int) -> None:
    """Прогнать сквады тарифа по всем его живым подпискам со своей сессией БД."""
    from app.database.database import AsyncSessionLocal
    from app.services.remnawave_service import RemnaWaveService

    try:
        async with AsyncSessionLocal() as db:
            tariff = await get_tariff_by_id(db, tariff_id)
            if not tariff:
                return

            result = await db.execute(
                select(Subscription)
                .join(User, Subscription.user_id == User.id)
                .options(joinedload(Subscription.user))
                .where(
                    and_(
                        Subscription.tariff_id == tariff_id,
                        Subscription.status.in_([SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value]),
                        # Тарифы существуют только в multi-tariff, а там панельная
                        # идентичность живёт на подписке: `users.remnawave_id`
                        # намеренно пуст, и фильтр по нему не выбирал бы никого.
                        or_(Subscription.remnawave_id.isnot(None), User.remnawave_id.isnot(None)),
                    )
                )
            )
            subscriptions = list(result.unique().scalars().all())
            if not subscriptions:
                return

            new_squads = tariff.allowed_squads or []
            ext_squad_uuid = tariff.external_squad_uuid

            from app.services.grace_access_runtime import update_panel_user_grace_safe

            service = RemnaWaveService()
            updated = 0
            failed = 0

            async with service.get_api_client() as api:
                semaphore = asyncio.Semaphore(_SYNC_CONCURRENCY)

                async def _sync_one(sub: Subscription) -> None:
                    nonlocal updated, failed
                    remnawave_id = (
                        getattr(sub, 'remnawave_id', None)
                        if settings.is_multi_tariff_enabled()
                        else (sub.user.remnawave_id if sub.user else None)
                    )
                    if not remnawave_id:
                        return
                    async with semaphore:
                        try:
                            await patch_panel_squads(
                                api,
                                user_id=remnawave_id,
                                squads=new_squads,
                                external_squad_uuid=ext_squad_uuid,
                                subscription_id=sub.id,
                                update_call=lambda **kwargs: update_panel_user_grace_safe(api, sub.id, **kwargs),
                            )
                            sub.connected_squads = new_squads
                            updated += 1
                        except Exception as e:
                            failed += 1
                            logger.warning(
                                'Background sync: failed to sync squads for user',
                                user_id=sub.user_id,
                                error=str(e),
                            )

                await asyncio.gather(*[_sync_one(sub) for sub in subscriptions])

            await db.commit()
            logger.info(
                'Background squad sync completed after tariff update',
                admin_id=admin_id,
                tariff_id=tariff_id,
                tariff_name=tariff.name,
                total=len(subscriptions),
                updated=updated,
                failed=failed,
            )
    except Exception:
        logger.exception('Background squad sync failed', tariff_id=tariff_id)
