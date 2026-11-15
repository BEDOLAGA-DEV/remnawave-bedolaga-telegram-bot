"""Живое состояние серверов SligVPN для промпта ИИ-поддержки.

Источники:
- xray-checker: только ``GET /metrics`` (Prometheus), метрики ``xray_proxy_status``
  и ``xray_proxy_latency_ms``. ``/api/v1/proxies`` не используется: там полные
  конфиги прокси с ключами.
- панель Remnawave: ``GET /api/nodes`` с токеном только на чтение. Без токена
  источник молча пропускается.

В блок для модели попадают только страны и отображаемые имена серверов (как
в приложении, без эмодзи). Адреса, stable_id, sub_name, внутренние имена нод
и IP в блок не попадают и в памяти модуля не хранятся.

Проверка вживую из корня репозитория::

    python -m ai_support_bot.app.services.server_status
"""

import asyncio
import math
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import aiohttp
import structlog

from ai_support_bot.app.core.config import settings


logger = structlog.get_logger(__name__)

SOURCE_TIMEOUT = 5.0  # секунд на один источник
SLOW_LATENCY_MS = 5000  # медленнее этого отметим «отвечает медленно»

MSK = timezone(timedelta(hours=3), 'MSK')

_STATUS_METRIC = 'xray_proxy_status'
_LATENCY_METRIC = 'xray_proxy_latency_ms'

WHITELIST_GROUP = 'Белые списки'
OTHER_GROUP = 'Прочие'
_WHITELIST_KEY = '__whitelist__'
_OTHER_KEY = '__other__'


COUNTRY_NAMES: dict[str, str] = {
    'AD': 'Андорра', 'AE': 'ОАЭ', 'AF': 'Афганистан', 'AL': 'Албания', 'AM': 'Армения',
    'AO': 'Ангола', 'AR': 'Аргентина', 'AT': 'Австрия', 'AU': 'Австралия', 'AZ': 'Азербайджан',
    'BA': 'Босния и Герцеговина', 'BD': 'Бангладеш', 'BE': 'Бельгия', 'BG': 'Болгария',
    'BH': 'Бахрейн', 'BO': 'Боливия', 'BR': 'Бразилия', 'BY': 'Беларусь', 'CA': 'Канада',
    'CH': 'Швейцария', 'CL': 'Чили', 'CN': 'Китай', 'CO': 'Колумбия', 'CR': 'Коста-Рика',
    'CU': 'Куба', 'CY': 'Кипр', 'CZ': 'Чехия', 'DE': 'Германия', 'DK': 'Дания',
    'DO': 'Доминикана', 'DZ': 'Алжир', 'EC': 'Эквадор', 'EE': 'Эстония', 'EG': 'Египет',
    'ES': 'Испания', 'ET': 'Эфиопия', 'EU': 'Европа', 'FI': 'Финляндия', 'FR': 'Франция',
    'GB': 'Великобритания', 'GE': 'Грузия', 'GR': 'Греция', 'HK': 'Гонконг', 'HR': 'Хорватия',
    'HU': 'Венгрия', 'ID': 'Индонезия', 'IE': 'Ирландия', 'IL': 'Израиль', 'IN': 'Индия',
    'IQ': 'Ирак', 'IR': 'Иран', 'IS': 'Исландия', 'IT': 'Италия', 'JO': 'Иордания',
    'JP': 'Япония', 'KE': 'Кения', 'KG': 'Киргизия', 'KH': 'Камбоджа', 'KP': 'Северная Корея',
    'KR': 'Южная Корея', 'KW': 'Кувейт', 'KZ': 'Казахстан', 'LA': 'Лаос', 'LB': 'Ливан',
    'LI': 'Лихтенштейн', 'LK': 'Шри-Ланка', 'LT': 'Литва', 'LU': 'Люксембург', 'LV': 'Латвия',
    'MA': 'Марокко', 'MC': 'Монако', 'MD': 'Молдова', 'ME': 'Черногория',
    'MK': 'Северная Македония', 'MN': 'Монголия', 'MO': 'Макао', 'MT': 'Мальта',
    'MX': 'Мексика', 'MY': 'Малайзия', 'NG': 'Нигерия', 'NL': 'Нидерланды', 'NO': 'Норвегия',
    'NP': 'Непал', 'NZ': 'Новая Зеландия', 'OM': 'Оман', 'PA': 'Панама', 'PE': 'Перу',
    'PH': 'Филиппины', 'PK': 'Пакистан', 'PL': 'Польша', 'PT': 'Португалия', 'PY': 'Парагвай',
    'QA': 'Катар', 'RO': 'Румыния', 'RS': 'Сербия', 'RU': 'Россия', 'SA': 'Саудовская Аравия',
    'SE': 'Швеция', 'SG': 'Сингапур', 'SI': 'Словения', 'SK': 'Словакия', 'TH': 'Таиланд',
    'TJ': 'Таджикистан', 'TM': 'Туркменистан', 'TN': 'Тунис', 'TR': 'Турция', 'TW': 'Тайвань',
    'UA': 'Украина', 'US': 'США', 'UY': 'Уругвай', 'UZ': 'Узбекистан', 'VE': 'Венесуэла',
    'VN': 'Вьетнам', 'ZA': 'ЮАР',
}

# Страна по тексту имени, если флага нет. Проверяются подстроки в нижнем регистре.
_EXTRA_NAME_ALIASES: dict[str, str] = {
    'germany': 'DE', 'франкфурт': 'DE', 'frankfurt': 'DE', 'ffm': 'DE', 'берлин': 'DE',
    'finland': 'FI', 'хельсинк': 'FI', 'helsinki': 'FI',
    'france': 'FR', 'париж': 'FR', 'paris': 'FR',
    'netherlands': 'NL', 'голланд': 'NL', 'амстердам': 'NL', 'amsterdam': 'NL',
    'sweden': 'SE', 'стокгольм': 'SE',
    'latvia': 'LV', 'рига': 'LV',
    'estonia': 'EE', 'таллин': 'EE',
    'usa': 'US', 'америк': 'US', 'нью-йорк': 'US', 'new york': 'US',
    'russia': 'RU', 'москв': 'RU', 'moscow': 'RU', 'петербург': 'RU', 'спб': 'RU',
    'japan': 'JP', 'токио': 'JP', 'turkey': 'TR', 'стамбул': 'TR', 'poland': 'PL', 'варшав': 'PL',
    'britain': 'GB', 'англи': 'GB', 'лондон': 'GB', 'london': 'GB',
    'kazakhstan': 'KZ', 'алмат': 'KZ', 'singapore': 'SG', 'hong kong': 'HK',
}


def _stem(name: str) -> str:
    low = name.lower()
    return low[:-1] if len(low) >= 5 else low


_NAME_ALIASES: list[tuple[str, str]] = sorted(
    [(_stem(name), code) for code, name in COUNTRY_NAMES.items() if code != 'EU']
    + list(_EXTRA_NAME_ALIASES.items()),
    key=lambda item: -len(item[0]),
)

_BALANCER_RE = re.compile(r'авто\s*-?\s*выбор|auto\s*-?\s*select', re.IGNORECASE)
_WHITELIST_RE = re.compile(r'бел\w*\s+спис', re.IGNORECASE)

# Эмодзи и пиктограммы, которые убираются из отображаемого имени.
_EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    (0x1F000, 0x1FAFF),  # эмодзи, флаги (regional indicators), 🏳 👈 и т. п.
    (0x2300, 0x23FF),  # ⌚ ⏩ …
    (0x2600, 0x27BF),  # ⚡ ✅ ❤ …
    (0x2B00, 0x2BFF),  # ⭐ ⬆ …
    (0xFE00, 0xFE0F),  # variation selectors
    (0xE0000, 0xE007F),  # теги флагов-регионов
)
_EMOJI_SINGLE = {0x200D, 0x20E3}
_EDGE_PUNCT = ' \t|-–—·•:,;/'


@dataclass(frozen=True)
class ProxyRecord:
    """Одна запись мониторинга: имя как в приложении (сырое) и итог проверки."""

    name: str
    ok: bool
    latency_ms: float | None = None


@dataclass(frozen=True)
class NodeRecord:
    """Нода панели без имени и адреса: только страна и связь."""

    country_code: str
    connected: bool


@dataclass(frozen=True)
class SourceSnapshot:
    items: tuple
    fetched_at: float
    stale: bool = False


@dataclass(frozen=True)
class ServerStatus:
    checked_at: float
    proxies: SourceSnapshot | None = None
    nodes: SourceSnapshot | None = None

    @property
    def empty(self) -> bool:
        return self.proxies is None and self.nodes is None


class SourceError(Exception):
    pass


def _now() -> float:
    return time.time()


# ---------------------------------------------------------------- кеш


class _StatusCache:
    def __init__(self) -> None:
        self.status: ServerStatus | None = None
        self.created: float = 0.0
        self.generation: int = 0  # растёт при каждой новой выборке
        self.last_good: dict[str, SourceSnapshot] = {}
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    def lock(self) -> asyncio.Lock:
        # Замок привязывается к циклу событий: при новом цикле (тесты, перезапуск) создаём новый.
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    def fresh(self, ttl: int, now: float) -> ServerStatus | None:
        if self.status is None or ttl <= 0:
            return None
        if now - self.created > ttl:
            return None
        return self.status

    def store(self, status: ServerStatus, now: float) -> None:
        self.status = status
        self.created = now
        self.generation += 1

    def clear(self) -> None:
        self.status = None
        self.created = 0.0
        self.generation = 0
        self.last_good.clear()


_cache = _StatusCache()


def clear_cache() -> None:
    _cache.clear()


# ---------------------------------------------------------------- настройки


def _xray_configured() -> bool:
    return bool((settings.XRAY_CHECKER_URL or '').strip())


def _remnawave_configured() -> bool:
    return bool((settings.REMNAWAVE_API_URL or '').strip() and (settings.REMNAWAVE_API_TOKEN or '').strip())


def _enabled() -> bool:
    return bool(settings.SERVER_STATUS_ENABLED) and (_xray_configured() or _remnawave_configured())


def _secrets() -> list[str]:
    values = [settings.XRAY_CHECKER_PASSWORD, settings.XRAY_CHECKER_USERNAME, settings.REMNAWAVE_API_TOKEN]
    return [value for value in (v.strip() for v in values if v) if len(value) >= 3]


def _describe_error(error: BaseException) -> str:
    """Короткое описание ошибки для лога без логинов, паролей и токенов."""
    text = f'{type(error).__name__}: {error}'[:300]
    text = re.sub(r'://[^/@\s]+@', '://***@', text)
    for secret in _secrets():
        text = text.replace(secret, '***')
    return text


# ---------------------------------------------------------------- Prometheus


def _parse_prom_line(line: str) -> tuple[str, dict[str, str], float] | None:
    """Строка текстового формата Prometheus → (метрика, метки, значение)."""
    line = line.strip()
    if not line or line.startswith('#'):
        return None
    n = len(line)
    i = 0
    while i < n and (line[i].isalnum() or line[i] in '_:'):
        i += 1
    metric = line[:i]
    if not metric:
        return None

    def skip_ws(pos: int) -> int:
        while pos < n and line[pos] in ' \t':
            pos += 1
        return pos

    labels: dict[str, str] = {}
    j = skip_ws(i)
    if j < n and line[j] == '{':
        j += 1
        while True:
            j = skip_ws(j)
            if j >= n:
                return None
            if line[j] == '}':
                j += 1
                break
            start = j
            while j < n and (line[j].isalnum() or line[j] == '_'):
                j += 1
            label = line[start:j]
            j = skip_ws(j)
            if not label or j >= n or line[j] != '=':
                return None
            j = skip_ws(j + 1)
            if j >= n or line[j] != '"':
                return None
            j += 1
            buf: list[str] = []
            closed = False
            while j < n:
                ch = line[j]
                if ch == '\\' and j + 1 < n:
                    nxt = line[j + 1]
                    buf.append({'n': '\n', '"': '"', '\\': '\\'}.get(nxt, '\\' + nxt))
                    j += 2
                    continue
                if ch == '"':
                    closed = True
                    j += 1
                    break
                buf.append(ch)
                j += 1
            if not closed:
                return None
            labels[label] = ''.join(buf)
            j = skip_ws(j)
            if j < n and line[j] == ',':
                j += 1
    rest = line[j:].split()
    if not rest:
        return None
    try:
        value = float(rest[0])
    except ValueError:
        return None
    return metric, labels, value


def _is_balancer(name: str) -> bool:
    return bool(_BALANCER_RE.search(name))


def parse_xray_metrics(text: str) -> list[ProxyRecord]:
    """Разбор /metrics xray-checker. Балансировщики «Авто выбор» пропускаются."""
    entries: dict[tuple, dict[str, Any]] = {}
    for line in (text or '').splitlines():
        parsed = _parse_prom_line(line)
        if parsed is None:
            continue
        metric, labels, value = parsed
        if metric not in (_STATUS_METRIC, _LATENCY_METRIC):
            continue
        name = (labels.get('name') or '').strip()
        if not name or _is_balancer(name):
            continue
        key = tuple(sorted(labels.items()))
        entry = entries.setdefault(key, {'name': name})
        entry['status' if metric == _STATUS_METRIC else 'latency'] = value

    records: list[ProxyRecord] = []
    for entry in entries.values():
        status = entry.get('status')
        latency = entry.get('latency')
        if status is None and latency is None:
            continue
        if status is None or math.isnan(status):
            ok = bool(latency and math.isfinite(latency) and latency > 0)
        else:
            ok = status >= 0.5
        clean_latency = latency if latency is not None and math.isfinite(latency) and latency > 0 else None
        records.append(ProxyRecord(name=entry['name'], ok=ok, latency_ms=clean_latency))
    return records


# ---------------------------------------------------------------- Remnawave


def parse_remnawave_nodes(payload: Any) -> list[NodeRecord]:
    """Ответ GET /api/nodes → ноды без имён и адресов. Выключенные (isDisabled) не считаются."""
    data = payload.get('response', payload) if isinstance(payload, dict) else payload
    if isinstance(data, dict) and isinstance(data.get('nodes'), list):
        data = data['nodes']
    if not isinstance(data, list):
        raise SourceError('unexpected /api/nodes payload')
    nodes: list[NodeRecord] = []
    for node in data:
        if not isinstance(node, dict) or node.get('isDisabled'):
            continue
        code = str(node.get('countryCode') or '').strip().upper()
        nodes.append(NodeRecord(country_code=code, connected=bool(node.get('isConnected'))))
    return nodes


def _is_local_host(url: str) -> bool:
    # Та же логика, что у основного бота (app/external/remnawave_api.py, _detect_connection_type).
    host = urlparse(url).hostname or ''
    if host in {'localhost', '127.0.0.1', 'remnawave', 'remnawave-backend', 'app', 'api'}:
        return True
    return host.startswith(('192.168.', '10.', '172.')) or host.endswith('.local')


# ---------------------------------------------------------------- сеть


async def _fetch_xray_metrics() -> str:
    base = settings.XRAY_CHECKER_URL.strip().rstrip('/')
    url = base if urlparse(base).path.endswith('/metrics') else f'{base}/metrics'
    username = settings.XRAY_CHECKER_USERNAME or ''
    password = settings.XRAY_CHECKER_PASSWORD or ''
    auth = aiohttp.BasicAuth(username, password) if (username or password) else None
    timeout = aiohttp.ClientTimeout(total=SOURCE_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, auth=auth, headers={'Accept': 'text/plain'}) as response:
            if response.status != 200:
                raise SourceError(f'xray-checker HTTP {response.status}')
            return await response.text()


async def _fetch_remnawave_nodes() -> Any:
    base = settings.REMNAWAVE_API_URL.strip().rstrip('/')
    headers = {
        'Authorization': f'Bearer {settings.REMNAWAVE_API_TOKEN.strip()}',
        'Accept': 'application/json',
    }
    verify_ssl = True
    if _is_local_host(base):
        # Как основной бот: прямой доступ к бэкенду панели внутри сети требует заголовков прокси.
        headers.update({
            'X-Forwarded-Proto': 'https',
            'X-Forwarded-For': '127.0.0.1',
            'X-Real-IP': '127.0.0.1',
            'X-Forwarded-Host': 'localhost',
            'Host': 'localhost',
        })
        if base.startswith('https://'):
            verify_ssl = False
    timeout = aiohttp.ClientTimeout(total=SOURCE_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(f'{base}/api/nodes', headers=headers, ssl=verify_ssl) as response:
            if response.status != 200:
                raise SourceError(f'Remnawave HTTP {response.status}')
            return await response.json(content_type=None)


async def _load_proxies() -> list[ProxyRecord]:
    text = await asyncio.wait_for(_fetch_xray_metrics(), timeout=SOURCE_TIMEOUT + 1)
    records = parse_xray_metrics(text)
    if not records:
        raise SourceError('no xray_proxy_status metrics')
    return records


async def _load_nodes() -> list[NodeRecord]:
    payload = await asyncio.wait_for(_fetch_remnawave_nodes(), timeout=SOURCE_TIMEOUT + 1)
    return parse_remnawave_nodes(payload)


def _fallback(key: str, now: float) -> SourceSnapshot | None:
    last = _cache.last_good.get(key)
    max_stale = max(0, int(settings.SERVER_STATUS_MAX_STALE or 0))
    if last is None or now - last.fetched_at > max_stale:
        return None
    return replace(last, stale=True)


async def _collect() -> ServerStatus:
    now = _now()
    loaders: dict[str, Any] = {}
    if _xray_configured():
        loaders['proxies'] = _load_proxies()
    if _remnawave_configured():
        loaders['nodes'] = _load_nodes()
    results = await asyncio.gather(*loaders.values(), return_exceptions=True)
    snapshots: dict[str, SourceSnapshot | None] = {}
    for key, result in zip(loaders, results):
        if isinstance(result, BaseException):
            fallback = _fallback(key, now)
            logger.warning(
                'Server status source failed',
                source=key,
                error=_describe_error(result),
                fallback='stale' if fallback else 'none',
            )
            snapshots[key] = fallback
        else:
            snapshot = SourceSnapshot(items=tuple(result), fetched_at=now)
            _cache.last_good[key] = snapshot
            snapshots[key] = snapshot
    return ServerStatus(checked_at=now, proxies=snapshots.get('proxies'), nodes=snapshots.get('nodes'))


async def get_server_status(force: bool = False) -> ServerStatus | None:
    """Состояние серверов с кешем на SERVER_STATUS_TTL. Никогда не бросает исключений.

    None — модуль выключен или ни один источник не настроен. При сбое источника
    отдаются последние удачные данные не старше SERVER_STATUS_MAX_STALE (stale=True).
    """
    try:
        if not _enabled():
            return None
        ttl = int(settings.SERVER_STATUS_TTL or 0)
        if not force:
            cached = _cache.fresh(ttl, _now())
            if cached is not None:
                return cached
        generation = _cache.generation
        async with _cache.lock():
            # Пока ждали замок, выборку мог сделать параллельный запрос — берём её.
            if _cache.status is not None and _cache.generation != generation:
                return _cache.status
            if not force:
                cached = _cache.fresh(ttl, _now())
                if cached is not None:
                    return cached
            status = await _collect()
            _cache.store(status, _now())
            return status
    except Exception as error:
        logger.warning('Server status failed', error=_describe_error(error))
        return None


# ---------------------------------------------------------------- группировка


def _is_emoji(ch: str) -> bool:
    code = ord(ch)
    if code in _EMOJI_SINGLE:
        return True
    return any(lo <= code <= hi for lo, hi in _EMOJI_RANGES)


def display_name(raw: str) -> str:
    """Имя как в приложении, но без эмодзи, пиктограмм и лишних пробелов."""
    text = ''.join(' ' if _is_emoji(ch) else ch for ch in raw or '')
    text = ' '.join(text.split())
    return text.strip(_EDGE_PUNCT)


def flag_country_code(raw: str) -> str | None:
    """Первый флаг-эмодзи в имени → код ISO (две буквы)."""
    chars = list(raw or '')
    for index, ch in enumerate(chars):
        code = ord(ch)
        if 0x1F1E6 <= code <= 0x1F1FF and index + 1 < len(chars):
            nxt = ord(chars[index + 1])
            if 0x1F1E6 <= nxt <= 0x1F1FF:
                return chr(code - 0x1F1E6 + 65) + chr(nxt - 0x1F1E6 + 65)
        if code == 0x1F3F4:  # 🏴 + теги: флаги Англии, Шотландии, Уэльса
            tags = ''
            for tag in chars[index + 1:]:
                tag_code = ord(tag)
                if 0xE0061 <= tag_code <= 0xE007A:
                    tags += chr(tag_code - 0xE0000)
                else:
                    break
            if tags.startswith('gb'):
                return 'GB'
    return None


def _country_from_text(name: str) -> str | None:
    low = name.lower().replace('ё', 'е')
    for alias, code in _NAME_ALIASES:
        if alias in low:
            return code
    return None


# ---------------------------------------------------------------- страны в вопросе клиента

# Окончания после основы названия: «Япони|и», «Казахстан|е», «Нидерланд|ах».
_CASE_ENDINGS = frozenset({
    'а', 'у', 'е', 'ы', 'и', 'я', 'ю', 'й', 'ь', 'ей', 'ии', 'ию', 'ией', 'ью',
    'ом', 'ой', 'ем', 'ам', 'ах', 'ами', 'ов',
})
_STRIPPED_TAIL = 'аяиыйеоуь'

# Разговорные названия: проверяются раньше справочника («корея» — Южная, а не Северная).
_QUESTION_NAME_ALIASES: dict[str, str] = {
    'корея': 'KR', 'англия': 'GB', 'британия': 'GB', 'америка': 'US', 'голландия': 'NL',
    'эмираты': 'AE', 'белоруссия': 'BY', 'молдавия': 'MD', 'кыргызстан': 'KG', 'тайланд': 'TH',
}

# Города и английские названия: слово в вопросе начинается с этой основы.
_QUESTION_PREFIX_ALIASES: dict[str, str] = {
    'japan': 'JP', 'токио': 'JP', 'turkey': 'TR', 'стамбул': 'TR', 'poland': 'PL', 'варшав': 'PL',
    'britain': 'GB', 'england': 'GB', 'лондон': 'GB', 'london': 'GB', 'kazakhstan': 'KZ',
    'алмат': 'KZ', 'астан': 'KZ', 'singapore': 'SG', 'дубай': 'AE', 'dubai': 'AE', 'uae': 'AE',
    'germany': 'DE', 'франкфурт': 'DE', 'берлин': 'DE', 'finland': 'FI', 'хельсинк': 'FI',
    'france': 'FR', 'париж': 'FR', 'netherlands': 'NL', 'амстердам': 'NL', 'sweden': 'SE',
    'стокгольм': 'SE', 'latvia': 'LV', 'estonia': 'EE', 'таллин': 'EE', 'russia': 'RU',
    'москв': 'RU', 'usa': 'US', 'america': 'US', 'нью-йорк': 'US', 'china': 'CN', 'korea': 'KR',
    'india': 'IN', 'armenia': 'AM', 'georgia': 'GE', 'ukraine': 'UA', 'israel': 'IL',
    'spain': 'ES', 'italy': 'IT', 'египт': 'EG',
}

_QUESTION_WORD_RE = re.compile(r'[a-zа-я]+(?:-[a-zа-я]+)*')


def _question_form(name: str) -> tuple[str, str, frozenset[str]]:
    """Как искать название в вопросе: (слово целиком, основа, окончания после основы)."""
    word = name.lower().replace('ё', 'е')
    parts = word.split()
    if len(parts) > 1:
        # «Южная Корея» → «корея», «Босния и Герцеговина» → «босния».
        word = parts[0] if parts[1] == 'и' else parts[-1]
    if name.isupper() or len(word) <= 3:
        return word, word, frozenset({''})
    if word[-1] in _STRIPPED_TAIL:
        # Без окончания основа не считается: «грузи» и «грузит» — не Грузия.
        return word, word[:-1], _CASE_ENDINGS
    return word, word, _CASE_ENDINGS | {''}


_QUESTION_FORMS: list[tuple[str, str, frozenset[str], str]] = [
    (*_question_form(name), code)
    for name, code in list(_QUESTION_NAME_ALIASES.items())
    + [(name, code) for code, name in COUNTRY_NAMES.items() if code != 'EU']
]


def _token_country(token: str) -> str | None:
    for word, stem, endings, code in _QUESTION_FORMS:
        if token == word or (token.startswith(stem) and token[len(stem):] in endings):
            return code
    for prefix, code in _QUESTION_PREFIX_ALIASES.items():
        if token.startswith(prefix):
            return code
    return None


def asked_countries(question: str) -> list[str]:
    """Коды стран из вопроса в порядке упоминания: «Есть сервер в Японии?» → ['JP']."""
    found: list[str] = []
    for token in _QUESTION_WORD_RE.findall((question or '').lower().replace('ё', 'е')):
        code = _token_country(token)
        if code and code not in found:
            found.append(code)
    return found


def _asked_lines(question: str, labels: list[str]) -> list[str]:
    """Подсказка модели по странам из вопроса: есть ли они в списке."""
    asked = [country_name(code) for code in asked_countries(question)]
    missing = [name for name in asked if name not in labels]
    present = [name for name in asked if name in labels]
    lines: list[str] = []
    if missing:
        lines.append(
            f'Клиент спросил про {"страны" if len(missing) > 1 else "страну"}: {", ".join(missing)}. '
            'Таких серверов у нас сейчас нет — так и скажи и предложи 2–3 зарубежные страны из списка; '
            'Россию взамен не предлагай.'
        )
    if present:
        lines.append(f'{", ".join(present)} — есть в списке выше.')
    return lines


def _group_key(raw: str) -> str:
    if _WHITELIST_RE.search(raw.replace('ё', 'е')):
        return _WHITELIST_KEY
    code = flag_country_code(raw) or _country_from_text(display_name(raw))
    return code or _OTHER_KEY


def _group_label(key: str) -> str:
    if key == _WHITELIST_KEY:
        return WHITELIST_GROUP
    if key == _OTHER_KEY:
        return OTHER_GROUP
    return country_name(key)


def country_name(code: str) -> str:
    code = (code or '').strip().upper()
    if not code or code == 'XX':
        return 'страна не указана'
    return COUNTRY_NAMES.get(code, code)


def _text_sort_key(text: str) -> tuple:
    # Кириллица раньше латиницы: «Россия · Банки» перед «Россия · YouTube».
    return tuple((0 if 'а' <= ch <= 'я' or ch == 'ё' else 1, ch) for ch in text.casefold())


def _group_sort_key(key: str) -> tuple:
    if key == _WHITELIST_KEY:
        return (3,)
    if key == 'RU':
        return (2,)
    if key == _OTHER_KEY:
        return (1,)
    return (0, _text_sort_key(_group_label(key)))


@dataclass
class _NameStat:
    ok: int = 0
    total: int = 0
    best_latency: float | None = None


def group_proxies(records: list[ProxyRecord] | tuple) -> list[tuple[str, dict[str, _NameStat]]]:
    """Записи → [(название группы, {отображаемое имя: счётчики})] в порядке вывода."""
    groups: dict[str, dict[str, _NameStat]] = {}
    for record in records:
        key = _group_key(record.name)
        name = display_name(record.name) or _group_label(key)
        stat = groups.setdefault(key, {}).setdefault(name, _NameStat())
        stat.total += 1
        if record.ok:
            stat.ok += 1
            if record.latency_ms is not None:
                if stat.best_latency is None or record.latency_ms < stat.best_latency:
                    stat.best_latency = record.latency_ms

    result: list[tuple[str, dict[str, _NameStat]]] = []
    for key in sorted(groups, key=_group_sort_key):
        names = sorted(groups[key].items(), key=lambda item: _text_sort_key(item[0]))
        result.append((_group_label(key), dict(names)))
    return result


def _variants(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return 'варианта'
    return 'вариантов'


def _nodes_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return 'узла'
    return 'узлов'


def _group_state(names: dict[str, _NameStat]) -> str:
    down = [name for name, stat in names.items() if stat.ok == 0]
    partial = [(name, stat) for name, stat in names.items() if 0 < stat.ok < stat.total]
    slow = [
        name for name, stat in names.items()
        if stat.ok and stat.best_latency is not None and stat.best_latency >= SLOW_LATENCY_MS
    ]
    single = len(names) == 1

    if len(down) == len(names):
        return 'сейчас не отвечает' if single else 'сейчас не отвечают'
    if single and partial:
        _, stat = partial[0]
        missing = stat.total - stat.ok
        state = f'работает частично: не отвечает {missing} из {stat.total} {_variants(stat.total)}'
    elif down or partial:
        exceptions = list(down) + [
            f'{name} (не отвечает {stat.total - stat.ok} из {stat.total} {_variants(stat.total)})'
            for name, stat in partial
        ]
        state = 'работают, кроме ' + ', '.join(exceptions)
    else:
        state = 'работает' if single else 'работают'
    if slow:
        state += '; медленно отвечает: ' + ', '.join(slow)
    return state


def _nodes_line(nodes: tuple, stale_time: str | None, only_problems: bool) -> str:
    """Строка о нодах панели: только страна и «N из M узлов», без имён и адресов."""
    per_country: dict[str, list[int]] = {}
    for node in nodes:
        counts = per_country.setdefault(node.country_code, [0, 0])
        counts[1] += 1
        if not node.connected:
            counts[0] += 1
    problems = [
        (country_name(code), down, total) for code, (down, total) in per_country.items() if down
    ]
    suffix = f' (данные на {stale_time} МСК)' if stale_time else ''
    if not problems:
        if only_problems or not per_country:
            return ''
        return f'По панели все узлы на связи{suffix}.'
    problems.sort(key=lambda item: _text_sort_key(item[0]))
    parts = [f'{name} — {down} из {total} {_nodes_word(total)}' for name, down, total in problems]
    return f'По панели сейчас не на связи{suffix}: ' + '; '.join(parts) + '.'


def _usable(snapshot: SourceSnapshot | None, now: float) -> SourceSnapshot | None:
    if snapshot is None:
        return None
    max_stale = max(0, int(settings.SERVER_STATUS_MAX_STALE or 0))
    if now - snapshot.fetched_at > max_stale:
        return None
    ttl = max(int(settings.SERVER_STATUS_TTL or 0), 60)
    if not snapshot.stale and now - snapshot.fetched_at > ttl * 2:
        return replace(snapshot, stale=True)
    return snapshot


def _msk(ts: float, fmt: str) -> str:
    return datetime.fromtimestamp(ts, MSK).strftime(fmt)


def format_status_block(status: ServerStatus | None, now: float | None = None, question: str = '') -> str:
    """Текст для промпта. '' — если данных нет или они старше SERVER_STATUS_MAX_STALE.

    ``question`` — вопрос клиента: страны из него сверяются со списком прямо в блоке.
    """
    if status is None:
        return ''
    now = _now() if now is None else now
    proxies = _usable(status.proxies, now)
    nodes = _usable(status.nodes, now)
    if proxies is not None and not proxies.items:
        proxies = None
    node_line = ''
    if nodes is not None:
        stale_time = _msk(nodes.fetched_at, '%H:%M') if nodes.stale else None
        node_line = _nodes_line(nodes.items, stale_time, only_problems=proxies is not None)
    if proxies is None and not node_line:
        return ''

    lines: list[str] = ['<server_status>']
    if proxies is not None:
        if proxies.stale:
            when = f'данные на {_msk(proxies.fetched_at, "%H:%M")} МСК, свежие пока недоступны'
            lines.append(f'Серверы SligVPN по нашему мониторингу: {when}. Это полный список: других стран сейчас нет.')
        else:
            when = _msk(proxies.fetched_at, '%d.%m %H:%M')
            lines.append(f'Серверы SligVPN по нашему мониторингу на {when} МСК. Это полный список: других стран сейчас нет.')
        groups = group_proxies(proxies.items)
        for label, names in groups:
            lines.append(f'• {label}: {", ".join(names)} — {_group_state(names)}')
        lines.extend(_asked_lines(question, [label for label, _ in groups]))
        if any(label == WHITELIST_GROUP for label, _ in groups):
            lines.append(
                f'«{WHITELIST_GROUP}» — только для заглушек мобильного интернета, когда оператор открывает '
                'лишь сайты из белого списка. При обычных сбоях, медленной работе и на Wi‑Fi их не советуй. '
                'Пропускает ли их оператор, мониторинг не видит: не пиши, что белые списки «работают», '
                'пиши, что серверы включены.'
            )
        if all(not record.ok for record in proxies.items):
            lines.append(
                'Ни один сервер не ответил мониторингу: возможен общий сбой или сбой самого мониторинга.'
            )
    else:
        when = _msk(nodes.fetched_at, '%d.%m %H:%M')
        lines.append(f'Состояние узлов SligVPN по панели на {when} МСК (список стран недоступен).')

    if node_line:
        lines.append(node_line)

    if proxies is not None:
        lines.append(
            'Мониторинг проверяет серверы из дата-центра: «работает» значит, что сервер жив; '
            'ограничения у мобильного оператора клиента он не видит.'
        )
    lines.append('</server_status>')
    return '\n'.join(lines)


async def build_status_block(question: str = '') -> str:
    """Готовый блок для промпта; '' если выключено, ничего не настроено или данных нет."""
    try:
        if not _enabled():
            return ''
        status = await get_server_status()
        return format_status_block(status, question=question)
    except Exception as error:
        logger.warning('Server status block failed', error=_describe_error(error))
        return ''


# ---------------------------------------------------------------- нужен ли блок


_TOPIC_PATTERNS = [
    r'\bсервер\w*', r'\bсервак\w*', r'\bserver\w*',
    r'\bстран(?:а|ы|у|е|ой|ам|ами|ах)?\b', r'\bлокаци\w*', r'\blocation\w*', r'\bрегион\w*',
    r'\bip\b', r'\bайпи\w*', r'\bip-?адрес\w*',
    r'\bне\s+(?:работ\w*|робит\w*|подключ\w*|грузит\w*|загружа\w*|'
    r'открыва\w*|коннект\w*|соедин\w*|заход\w*|пуска\w*)',
    r'\bдолго\s+(?:груз|загруж|откры|подключ)\w*',
    r'\bне\s+(?:могу|можем|может|получается|выходит)\s+(?:\w+\s+)?(?:подключ\w*|зайти|открыть|загрузить|соедин\w*)',
    r'\bперестал\w*\s+(?:работ|подключ|грузит|открыва)\w*',
    r'\bплохо\s+работ\w*', r'\bработа\w*\s+плохо\b',
    r'\bнет\s+(?:соединени|подключени|связи|интернет)\w*',
    r'\b(?:вылета|отвалива|обрыва|разрыва)\w*',
    r'\bупал\w*', r'\bлеж(?:ит|ат)\b', r'\bсбо[йия]\w*', r'\bавари\w*',
    r'\bмедлен\w*', r'\bскорост\w*', r'\bпинг\w*', r'\bping\b', r'\bтормоз\w*',
    r'\bлаг(?:и|ает|ают|ать|ов)?\b',
    r'\bбел\w*\s+спис\w*', r'\bwhitelist\w*', r'\bобход\w*', r'\bзаглушк\w*', r'\bглушилк\w*',
    r'\bглуш(?:ат|ит|ение|ения)\b',
    r'\blte\b', r'\b[345]g\b', r'\bмобильн\w*\s+(?:сет|интернет|связ|оператор|данн)\w*',
    r'\b(?:мтс|мегафон\w*|билайн\w*|теле2|tele2|йот[аеуы]|yota|ростелеком\w*)\b',
    r'youtube', r'\bютуб\w*', r'\bютюб\w*',
    r'\bбанк(?:и|ов|ам|ами|ах|а|у|е|ом)?\b', r'госуслуг\w*',
    r'\bдобав\w*\s+(?:\w+\s+){0,2}(?:стран|локаци|сервер)\w*',
]

_COUNTRY_PATTERNS = [
    r'\b(?:япони|казахстан|турци|польш|великобритани|британи|америк|канад|эмират|дубай|армени|'
    r'сингапур|гонконг|китай|кита[ея]\b|герман|финлянд|франци|нидерланд|голланд|швеци|латви|'
    r'эстони|литв|москв|испани|итали|швейцари|австри|израил|бразили|аргентин|мексик|австрали|'
    r'вьетнам|таиланд|тайланд|индонези|малайзи|филиппин|египт|египет|украин|беларус|белорус|'
    r'молдав|молдов|узбекистан|киргиз|кыргызстан|таджикистан|туркменистан|азербайджан|норвеги|'
    r'исланди|ирланди|португали|румыни|болгари|венгри|серби|хорвати|греци|кипр|черногори|'
    r'словаки|словени|бельги|люксембург|монако|европ|африк|пакистан|саудовск|катар|кувейт|'
    r'тайван|монголи|непал|камбодж|токио|стамбул|варшав|лондон|нью-йорк|франкфурт|амстердам|'
    r'хельсинки|париж|стокгольм|таллин|алмат|ереван|тбилиси|минск|киев|ташкент|бишкек|душанбе)\w*',
    r'\b(?:англи|инди|грузи|коре|росси|чехи|дани|ази|мальт|астан)(?:я|и|ю|ей|а|у|е|ы)\b',
    r'\b(?:сша|оаэ|юар|рф|иран|ирак|баку|usa|uae|uk)\b',
    r'\b(?:japan|kazakhstan|turkey|poland|britain|england|america|canada|india|georgia|armenia|'
    r'singapore|hong\s*kong|korea|china|germany|finland|france|netherlands|sweden|latvia|estonia|'
    r'russia|spain|italy|switzerland|austria|israel|brazil|ukraine)\w*',
]

_RELEVANT_RE = re.compile('|'.join(_TOPIC_PATTERNS + _COUNTRY_PATTERNS), re.IGNORECASE)

FOLLOW_UP_MAX_CHARS = 60


def _matches(text: str) -> bool:
    if not text:
        return False
    return bool(_RELEVANT_RE.search(text.lower().replace('ё', 'е')))


def status_relevant(question: str, recent: str = '') -> bool:
    """Нужен ли блок серверов для этого вопроса.

    Вопрос сам говорит о серверах, странах, сбоях, скорости, обходе блокировок и т. п. —
    да. Короткая реплика-продолжение (до FOLLOW_UP_MAX_CHARS символов, вроде «а сейчас?»)
    наследует тему из ``recent`` — недавних сообщений диалога.
    """
    question = question or ''
    if _matches(question):
        return True
    if recent and len(question.strip()) <= FOLLOW_UP_MAX_CHARS:
        return _matches(recent)
    return False


if __name__ == '__main__':
    async def _main() -> None:
        status = await get_server_status(force=True)
        block = format_status_block(status)
        print(block or '(блок пуст: источники не настроены или данные недоступны)')

    asyncio.run(_main())
