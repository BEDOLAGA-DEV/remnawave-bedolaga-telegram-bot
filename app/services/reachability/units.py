"""Симки bschekbot: каталог из GET /operators, селекторы и раскрытие с пропусками."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field


DPI_MODES = ('on', 'off', 'any')


class SelectorError(ValueError):
    """Ключ симки не разобрался — сообщение для админа."""


@dataclass(frozen=True)
class Unit:
    op_key: str
    operator: str
    name: str
    region: str
    region_code: str
    dpi: str
    channel_state: str
    probeable: bool

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Selector:
    operator: str | None
    region: str | None
    dpi: str | None


@dataclass
class Expansion:
    resolved: list[str] = field(default_factory=list)
    skipped_dpi_off: list[Unit] = field(default_factory=list)
    skipped_unavailable: list[Unit] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


def parse_selector(raw: str) -> Selector:
    text = (raw or '').strip()
    if not text:
        raise SelectorError('Пустой ключ симки')
    if ':' in text:
        raise SelectorError(f'«{text}» — старый формат ключа; нужен вид «оператор|округ|бс»')
    parts = [part.strip().lower() for part in text.split('|')]
    if len(parts) > 3:
        raise SelectorError(f'«{text}» — ключ симки состоит максимум из трёх частей')
    parts += [''] * (3 - len(parts))
    operator, region, dpi = (None if part in ('', '*') else part for part in parts)
    if dpi is not None and dpi not in ('on', 'off'):
        raise SelectorError(f'Третья часть ключа «{text}» — on, off или *')
    if operator is None and region is None and dpi is None:
        raise SelectorError('Ключ «все|все|все» не имеет смысла: оставьте список симок пустым')
    return Selector(operator, region, dpi)


def _matches(selector: Selector, unit: Unit) -> bool:
    if selector.operator and selector.operator != unit.operator.lower():
        return False
    if selector.region and selector.region not in (unit.region.lower(), unit.region_code.lower()):
        return False
    return not selector.dpi or selector.dpi == unit.dpi


class UnitsCatalog:
    def __init__(self, units: list[Unit], fetched_at: float) -> None:
        self.units = units
        self.fetched_at = fetched_at
        self.by_key = {unit.op_key: unit for unit in units}
        self._order = {unit.op_key: index for index, unit in enumerate(units)}

    @classmethod
    def from_response(cls, payload: dict, fetched_at: float) -> UnitsCatalog:
        units = [
            Unit(
                op_key=str(item['op_key']),
                operator=str(item.get('operator') or ''),
                name=str(item.get('name') or item.get('operator') or ''),
                region=str(item.get('region') or ''),
                region_code=str(item.get('region_code') or ''),
                dpi=str(item.get('dpi') or ''),
                channel_state=str(item.get('channel_state') or ''),
                probeable=bool(item.get('probeable', False)),
            )
            for item in payload.get('units') or []
        ]
        return cls(units, fetched_at)

    def expand(self, selectors: list[str], dpi: str) -> Expansion:
        """Раскрыть селекторы по каталогу и отделить пропуски. Порядок — как в каталоге."""
        if dpi not in DPI_MODES:
            raise SelectorError(f'Режим Белого списка «{dpi}» — on, off или any')
        result = Expansion()
        matched: list[Unit] = []
        if selectors:
            for raw in selectors:
                selector = parse_selector(raw)
                hits = [unit for unit in self.units if _matches(selector, unit)]
                if not hits:
                    result.unknown.append(raw)
                matched.extend(hits)
        else:
            matched = list(self.units)

        seen: set[str] = set()
        for unit in sorted(matched, key=lambda u: self._order[u.op_key]):
            if unit.op_key in seen:
                continue
            seen.add(unit.op_key)
            if dpi != 'any' and unit.dpi != dpi:
                result.skipped_dpi_off.append(unit)
            elif not unit.probeable:
                result.skipped_unavailable.append(unit)
            else:
                result.resolved.append(unit.op_key)
        return result


class UnitsCache:
    """Кэш каталога: флот меняется в течение часа, поэтому TTL короткий (60 с)."""

    def __init__(
        self,
        fetch: Callable[[], Awaitable[dict]],
        ttl: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._ttl = ttl
        self._clock = clock
        self._catalog: UnitsCatalog | None = None

    async def get(self, force: bool = False) -> UnitsCatalog:
        now = self._clock()
        if force or self._catalog is None or now - self._catalog.fetched_at >= self._ttl:
            payload = await self._fetch()
            self._catalog = UnitsCatalog.from_response(payload, fetched_at=now)
        return self._catalog
