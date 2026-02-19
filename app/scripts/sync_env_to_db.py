"""
Однократная синхронизация цен и периодов из .env в БД.

Источник правды после этого — БД (тариф «Стандартный» и system_settings).
Менять цены/периоды: через админку бота или правки в БД.

Запуск (из корня проекта, с поднятой БД):
  docker compose run --rm bot python -m app.scripts.sync_env_to_db

или без Docker (если БД доступна):
  python -m app.scripts.sync_env_to_db
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.config import PERIOD_PRICES, settings
from app.database.crud.system_setting import upsert_system_setting
from app.database.crud.tariff import update_tariff
from app.database.database import AsyncSessionLocal
from app.database.models import Tariff

PRICE_KEYS = (
    'PRICE_14_DAYS', 'PRICE_30_DAYS', 'PRICE_60_DAYS',
    'PRICE_90_DAYS', 'PRICE_180_DAYS', 'PRICE_360_DAYS',
)
PERIOD_KEYS = ('AVAILABLE_SUBSCRIPTION_PERIODS', 'AVAILABLE_RENEWAL_PERIODS')


def _to_db_value(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


async def main() -> int:
    async with AsyncSessionLocal() as db:
        # 1) Тариф «Стандартный» — period_prices из текущего PERIOD_PRICES (из .env)
        period_prices = {int(d): int(p) for d, p in PERIOD_PRICES.items() if p and int(p) > 0}
        if period_prices:
            norm = {str(k): v for k, v in period_prices.items()}
            result = await db.execute(select(Tariff).where(Tariff.name == 'Стандартный').limit(1))
            tariff = result.scalar_one_or_none()
            if tariff:
                await update_tariff(db, tariff, period_prices=period_prices)
                await db.commit()
                await db.refresh(tariff)
                print('OK: Тариф «Стандартный» обновлён: period_prices =', norm)
            else:
                print('Тариф «Стандартный» не найден. Создайте его через админку или при первом запуске бота.')
        else:
            print('PERIOD_PRICES пуст — нечего записывать в тариф.')

        # 2) system_settings — цены и периоды из текущего settings (из .env)
        for key in list(PRICE_KEYS) + list(PERIOD_KEYS):
            value = getattr(settings, key, None)
            raw = _to_db_value(value)
            if raw is not None:
                await upsert_system_setting(db, key, raw)
        await db.commit()
        print('OK: system_settings обновлены для PRICE_* и AVAILABLE_* из .env')

    print('Готово. Дальше бот использует значения из БД. Меняйте их в админке или в БД.')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
