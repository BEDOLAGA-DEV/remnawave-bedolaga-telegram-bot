"""Разбор посквадных лимитов трафика («премиум-сквады») из тарифа.

Лимиты живут в `Tariff.server_traffic_limits` — JSON вида
`{squad_uuid: {traffic_limit_gb, topup_enabled, topup_packages, max_topup_gb}}`.
Поле появилось раньше этой функциональности и успело обрасти тремя формами
записи, поэтому разбор терпимый:

* `{"uuid": {"traffic_limit_gb": 5}}` — актуальная;
* `{"uuid": 5}` — ранняя, лимит числом;
* `{"uuid": {...}}` без `traffic_limit_gb` — считаем, что лимита нет.

Премиумным сквад делает **положительный** лимит: ноль по договорённости самого
поля означает «брать общий лимит тарифа», то есть никакого отдельного
ограничения. Такие сквады из разбора выпадают, и воркер их не трогает.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)

BYTES_IN_GB = 1024**3


@dataclass(frozen=True)
class PremiumSquadConfig:
    """Настройки премиум-лимита одного сквада внутри тарифа."""

    squad_uuid: str
    limit_gb: int
    # Своё название вместо имени сервера. Нужно, когда премиум-серверов
    # несколько: без него в интерфейсе все строки подписаны одинаково, и
    # непонятно, к какому серверу относится лимит.
    name: str | None = None
    # Порядок показа. Задаётся в админке стрелками; ноль у всех означает
    # «порядок не настраивали» и разрешается — тогда строки идут по UUID,
    # лишь бы стабильно, а не как ляжет ключ в JSON.
    sort_order: int = 0
    topup_enabled: bool = False
    # {ГБ: цена в копейках} — посквадный аналог `Tariff.traffic_topup_packages`.
    topup_packages: dict[int, int] = field(default_factory=dict)
    # Потолок докупки сверх лимита, 0 = без ограничения.
    max_topup_gb: int = 0

    @property
    def limit_bytes(self) -> int:
        return self.limit_gb * BYTES_IN_GB

    def price_kopeks_for(self, gb: int) -> int | None:
        """Цена пакета в копейках или None, если такого пакета нет."""
        if not self.topup_enabled:
            return None
        return self.topup_packages.get(gb)

    def available_packages(self) -> list[tuple[int, int]]:
        """Пакеты докупки, отсортированные по объёму: [(ГБ, копейки), ...]."""
        if not self.topup_enabled:
            return []
        return sorted(self.topup_packages.items())


def _coerce_int(value: Any, default: int = 0) -> int:
    """Привести к неотрицательному int, не роняя разбор на мусоре в JSON."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _parse_packages(raw: Any) -> dict[int, int]:
    """Разобрать `{ГБ: копейки}`. Ключи в JSON — строки, наружу отдаём int."""
    if not isinstance(raw, dict):
        return {}
    packages: dict[int, int] = {}
    for raw_gb, raw_price in raw.items():
        gb = _coerce_int(raw_gb)
        price = _coerce_int(raw_price, default=-1)
        # Нулевой объём бессмыслен; отрицательная цена — мусор. Бесплатный
        # пакет (price == 0) оставляем: это осознанная настройка админа.
        if gb > 0 and price >= 0:
            packages[gb] = price
    return packages


def parse_premium_squad(squad_uuid: str, raw: Any) -> PremiumSquadConfig | None:
    """Разобрать одну запись `server_traffic_limits`.

    Возвращает None, если у сквада нет положительного лимита — тогда он не
    премиумный и отдельного учёта не требует.
    """
    if isinstance(raw, dict):
        limit_gb = _coerce_int(raw.get('traffic_limit_gb'))
        raw_name = raw.get('name')
        name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else None
        sort_order = _coerce_int(raw.get('sort_order'))
        topup_enabled = bool(raw.get('topup_enabled', False))
        packages = _parse_packages(raw.get('topup_packages'))
        max_topup_gb = _coerce_int(raw.get('max_topup_gb'))
    else:
        # Ранняя форма записи: лимит числом, докупки не предусматривалось.
        limit_gb = _coerce_int(raw)
        name = None
        sort_order = 0
        topup_enabled = False
        packages = {}
        max_topup_gb = 0

    if limit_gb <= 0:
        return None

    return PremiumSquadConfig(
        squad_uuid=squad_uuid,
        limit_gb=limit_gb,
        name=name,
        sort_order=sort_order,
        # Докупка без пакетов ничего не продаёт — считаем её выключенной, чтобы
        # интерфейс не показывал пустой список «купить».
        topup_enabled=topup_enabled and bool(packages),
        topup_packages=packages,
        max_topup_gb=max_topup_gb,
    )


def parse_premium_squads(server_traffic_limits: Any) -> dict[str, PremiumSquadConfig]:
    """Разобрать весь `server_traffic_limits` тарифа.

    Ключ результата — UUID сквада. Сквады без положительного лимита опущены,
    поэтому пустой результат означает «в тарифе премиум-сквадов нет».

    Порядок результата — заданный админом (`sort_order`), а не порядок ключей в
    JSON: словарь сохраняет порядок вставки, и он зависел бы от того, в какой
    последовательности сервера когда-то добавляли в тариф. Пользователь видел бы
    строки не в том порядке, что настроен в админке.
    """
    if not isinstance(server_traffic_limits, dict):
        return {}
    parsed: list[PremiumSquadConfig] = []
    for squad_uuid, raw in server_traffic_limits.items():
        if not isinstance(squad_uuid, str) or not squad_uuid:
            continue
        config = parse_premium_squad(squad_uuid, raw)
        if config is not None:
            parsed.append(config)
    # UUID вторым ключом — чтобы порядок был устойчивым, пока его не настроили.
    parsed.sort(key=lambda item: (item.sort_order, item.squad_uuid))
    return {config.squad_uuid: config for config in parsed}


def get_premium_squads_for_tariff(tariff: Any) -> dict[str, PremiumSquadConfig]:
    """Премиум-сквады тарифа. Безопасно принимает None вместо тарифа."""
    if tariff is None:
        return {}
    return parse_premium_squads(getattr(tariff, 'server_traffic_limits', None))


def exclude_limited_squads(squads: Iterable[str] | None, limited_uuids: Iterable[str]) -> list[str]:
    """Убрать из набора сквады, снятые за исчерпание премиум-лимита.

    Порядок сохраняем: панель принимает список, и произвольная перестановка
    сделала бы диффы в логах и снимках grace нечитаемыми.
    """
    limited = set(limited_uuids or ())
    if not limited:
        return list(squads or [])
    return [uuid for uuid in (squads or []) if uuid not in limited]


async def effective_panel_squads(
    subscription_id: int | None,
    squads: Sequence[str] | None,
    *,
    db: AsyncSession | None = None,
) -> list[str] | None:
    """Набор сквадов для отправки в панель с учётом снятых премиум-сквадов.

    Вызывается на каждой границе, где бот пишет ``activeInternalSquads``: без
    этого любая синхронизация вернула бы пользователю сквад, который воркер снял
    за перерасход, и ограничение бы развалилось.

    Возвращает ``None``, если и на входе был ``None`` — вызывающие отличают
    «сквады не трогаем» от «список пуст», и терять это различие нельзя.

    Пустой список на выходе при непустом входе — осмысленный результат: все
    сквады подписки премиумные и все исчерпаны. ``update_user`` шлёт ``[]`` как
    «снять все», что здесь и требуется.

    Сессию можно не передавать: часть точек отправки работает вне запроса.
    Запрос узкий и по индексу, поэтому своя сессия дешева.
    """
    if squads is None:
        return None
    if not squads or not subscription_id:
        return list(squads)

    from app.database.crud.premium_traffic import get_limited_squad_uuids

    try:
        if db is not None:
            limited = await get_limited_squad_uuids(db, subscription_id)
        else:
            from app.database.database import AsyncSessionLocal

            async with AsyncSessionLocal() as own_db:
                limited = await get_limited_squad_uuids(own_db, subscription_id)
    except Exception:
        # Отказ БД не должен рвать синхронизацию с панелью. Отдаём набор как
        # есть: пользователь на несколько минут получит снятый сквад обратно,
        # но следующий проход воркера его снова уберёт. Обратный выбор — снять
        # сквады «на всякий случай» — отобрал бы доступ у тех, кто ни при чём.
        logger.warning(
            'Не удалось прочитать снятые премиум-сквады, отправляем набор без фильтра',
            subscription_id=subscription_id,
            exc_info=True,
        )
        return list(squads)

    return exclude_limited_squads(squads, limited)
