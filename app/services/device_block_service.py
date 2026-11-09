"""Service for hard HWID device blocking (hwid-blocker middleware).

Owns the `device_blocks` table as the single source of truth and keeps the
middleware in sync by pushing the full state (monotonic version + device
list) to `PUT {MIDDLEWARE_URL}/v1/state`. Pushes are best-effort with
retries — a failed push never breaks the user-facing ban/unban flow, and
the periodic sync + middleware startup pull repair drift.

The middleware pulls `GET /device-blocks` from this bot's webapi on its own
startup (bootstrap), so a bot restart must not wipe the middleware state.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import aiohttp
import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud import device_block as crud
from app.database.crud.system_setting import get_setting_value, upsert_system_setting
from app.database.models import DeviceBlock, User
from app.localization.texts import get_texts


logger = structlog.get_logger(__name__)

VERSION_KEY = 'device_blocks_version'

# Push retries: 3 attempts total with 0.5s / 5s backoffs (README §5.3)
PUSH_RETRIES = 3
PUSH_RETRY_DELAYS = (0.5, 5.0)


async def _get_version(db: AsyncSession) -> int:
    raw = await get_setting_value(db, VERSION_KEY)
    try:
        return int(raw) if raw else 0
    except (TypeError, ValueError):
        return 0


async def _bump_version(db: AsyncSession) -> int:
    version = await _get_version(db) + 1
    await upsert_system_setting(
        db,
        VERSION_KEY,
        str(version),
        description='Monotonic counter for hwid-blocker middleware state pushes',
    )
    await db.commit()
    return version


class DeviceBlockService:
    def __init__(self) -> None:
        self._sync_task: asyncio.Task | None = None
        self._push_lock = asyncio.Lock()

    # ------------------------------------------------------------------ ban

    async def ban_device(
        self,
        db: AsyncSession,
        user: User,
        hwid: str,
        *,
        remnawave_uuid: str | None = None,
        device_label: str | None = None,
        platform: str | None = None,
        banned_by: str = 'user',
    ) -> DeviceBlock:
        """Block a device: DB row + version bump + best-effort panel remove + push."""
        hwid = crud.normalize_hwid(hwid)
        if not hwid:
            raise ValueError('hwid is required')

        texts = get_texts(user.language)
        message = texts.t(
            'DEVICE_BLOCK_ANNOUNCE',
            'This device was disconnected by the account owner. Contact support if this is a mistake.',
        )

        # Scope the ban to the subscription (Remnawave short UUID). Best-effort:
        # if it can't be resolved we fall back to a global ban (short_uuid=None).
        short_uuid = await self._resolve_short_uuid(remnawave_uuid)

        block = await crud.upsert_block(
            db,
            user.id,
            hwid,
            remnawave_uuid=remnawave_uuid,
            short_uuid=short_uuid,
            device_label=device_label,
            platform=platform,
            message=message,
            banned_by=banned_by,
            commit=False,
        )
        await _bump_version(db)

        # Best-effort: also remove the device from the RemnaWave panel so it
        # drops from the user's device list. Never raises — the middleware
        # block is authoritative.
        if remnawave_uuid:
            await self._remove_device_from_panel(remnawave_uuid, hwid)

        self.schedule_push()
        return block

    async def unban_device(self, db: AsyncSession, user: User | None, block_id: int) -> bool:
        """Unblock a device: delete row + version bump + push.

        `user` restricts the delete to that user's rows; pass None for an
        admin unban that may remove any row (including user_id=NULL bans).
        """
        result = await db.execute(select(DeviceBlock).where(DeviceBlock.id == block_id))
        block = result.scalars().first()
        if block is None:
            return False
        if user is not None and block.user_id not in (None, user.id):
            return False

        await db.delete(block)
        await _bump_version(db)
        await db.commit()
        self.schedule_push()
        return True

    # --------------------------------------------------------------- payload

    @staticmethod
    def build_state_payload(version: int, blocks: list[DeviceBlock]) -> dict:
        """Full state snapshot in the shape the middleware expects."""
        return {
            'version': version,
            'updated_at': datetime.now(timezone.utc).isoformat(),
            'devices': [
                {
                    'hwid': block.hwid,
                    'user_id': block.user_id,
                    'short_uuid': block.short_uuid,
                    'message': block.message or '',
                    'banned_at': block.banned_at.isoformat() if block.banned_at else None,
                }
                for block in blocks
            ],
        }

    async def get_state_payload(self) -> dict | None:
        """Read version + all blocks in one session. None when feature disabled."""
        if not settings.is_device_block_enabled():
            return None
        from app.database.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            version = await _get_version(db)
            blocks = await crud.get_all_blocks(db)
            return self.build_state_payload(version, blocks)

    # ----------------------------------------------------------------- push

    def _push_enabled(self) -> bool:
        if not settings.is_device_block_enabled():
            return False
        if not settings.MIDDLEWARE_ADMIN_TOKEN:
            return False
        return True

    async def push_state(self) -> bool:
        """Push the full state to the middleware. Retries, never raises."""
        if not self._push_enabled():
            logger.debug('Device block push skipped (feature disabled or no admin token)')
            return False

        payload = await self.get_state_payload()
        if payload is None:
            return False

        url = f'{settings.MIDDLEWARE_URL.rstrip("/")}/v1/state'
        headers = {'X-Admin-Token': settings.MIDDLEWARE_ADMIN_TOKEN, 'Content-Type': 'application/json'}
        body = json.dumps(payload).encode('utf-8')

        for attempt in range(1, PUSH_RETRIES + 1):
            try:
                async with self._push_lock:
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                        async with session.put(url, headers=headers, data=body) as resp:
                            if resp.status == 200:
                                logger.info(
                                    'Device block state pushed to middleware',
                                    version=payload['version'],
                                    devices=len(payload['devices']),
                                    attempt=attempt,
                                )
                                return True
                            text = await resp.text()
                            logger.warning(
                                'Middleware rejected device block state',
                                status=resp.status,
                                body=text[:300],
                                attempt=attempt,
                            )
                            # 409 = middleware holds a newer version; retrying
                            # the same older payload will never succeed.
                            if resp.status == 409:
                                return False
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    'Failed to push device block state to middleware',
                    error=str(exc)[:300],
                    attempt=attempt,
                )
            if attempt < PUSH_RETRIES:
                await asyncio.sleep(PUSH_RETRY_DELAYS[min(attempt - 1, len(PUSH_RETRY_DELAYS) - 1)])

        logger.error('Device block state push failed after all retries — periodic sync will retry')
        return False

    def schedule_push(self) -> None:
        """Fire-and-forget push from a handler context (does not block the user)."""
        if not self._push_enabled():
            return
        task = asyncio.create_task(self.push_state())
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    async def _align_version_with_middleware(self) -> None:
        """Raise the local version counter to the middleware's current version.

        The push protocol only accepts versions >= the stored one. A fresh or
        rebuilt bot database starts at 0 and would 409 forever against a
        middleware that has already advanced (seeded manually or by an earlier
        installation). Since this service owns the device set, adopting the
        higher counter is safe: the very next push replaces the middleware's
        device list with ours.
        """
        if not self._push_enabled():
            return
        url = f'{settings.MIDDLEWARE_URL.rstrip("/")}/v1/health'
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning('Device block version alignment skipped', status=resp.status)
                        return
                    data = await resp.json()
                    mw_version = int(data.get('version', 0))
        except Exception as exc:
            logger.warning('Device block version alignment skipped', error=str(exc)[:200])
            return

        from app.database.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            local = await _get_version(db)
            if mw_version > local:
                await upsert_system_setting(
                    db,
                    VERSION_KEY,
                    str(mw_version),
                    description='Monotonic counter for hwid-blocker middleware state pushes',
                )
                await db.commit()
                logger.info('Device block version aligned with middleware', local=local, version=mw_version)

    async def startup_sync(self) -> None:
        """Push current state on bot startup (recovery after downtime)."""
        logger.info('Device block startup sync')
        await self._align_version_with_middleware()
        await self.backfill_short_uuids()
        await self.push_state()

    # ------------------------------------------------------------- lifecycle

    async def start_periodic_sync(self) -> None:
        if self._sync_task and not self._sync_task.done():
            return
        self._sync_task = asyncio.create_task(self._periodic_loop())

    async def stop_periodic_sync(self) -> None:
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
        self._sync_task = None

    async def _periodic_loop(self) -> None:
        interval = max(1, settings.MIDDLEWARE_PUSH_INTERVAL_MIN) * 60
        try:
            while True:
                await asyncio.sleep(interval)
                await self.push_state()
        except asyncio.CancelledError:
            raise

    async def _remove_device_from_panel(self, remnawave_uuid: str, hwid: str) -> None:
        try:
            from app.services.remnawave_service import RemnaWaveService

            service = RemnaWaveService()
            async with service.get_api_client() as api:
                await api.remove_device(remnawave_uuid, hwid)
        except Exception as exc:
            logger.warning('Failed to remove blocked device from panel (best-effort)', error=str(exc)[:300])

    async def _resolve_short_uuid(self, remnawave_uuid: str | None) -> str | None:
        """Resolve the Remnawave public subscription key (short UUID) for a panel
        UUID. Best-effort: returns None on any failure (→ global ban)."""
        if not remnawave_uuid:
            return None
        try:
            from app.services.remnawave_service import RemnaWaveService

            service = RemnaWaveService()
            if not service.is_configured:
                return None
            async with service.get_api_client() as api:
                rw_user = await api.get_user_by_uuid(remnawave_uuid)
                if rw_user and rw_user.short_uuid:
                    return rw_user.short_uuid
        except Exception as exc:
            logger.warning('Failed to resolve short_uuid for device block', error=str(exc)[:300])
        return None

    async def backfill_short_uuids(self) -> int:
        """Resolve `short_uuid` for legacy rows that have a `remnawave_uuid` but
        no `short_uuid` yet, so existing bans become subscription-scoped. Returns
        the number of rows updated."""
        from app.database.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(DeviceBlock).where(
                    DeviceBlock.short_uuid.is_(None),
                    DeviceBlock.remnawave_uuid.is_not(None),
                )
            )
            blocks = list(result.scalars().all())

        changed = 0
        for block in blocks:
            short_uuid = await self._resolve_short_uuid(block.remnawave_uuid)
            if not short_uuid:
                continue
            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(DeviceBlock).where(DeviceBlock.id == block.id).values(short_uuid=short_uuid)
                )
                await db.commit()
            changed += 1
        if changed:
            logger.info('Backfilled short_uuid for legacy device blocks', count=changed)
        return changed


device_block_service = DeviceBlockService()
