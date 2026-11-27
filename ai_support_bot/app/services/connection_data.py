"""Connection data of the customer's own subscriptions from the Remnawave panel, read-only.

For each working subscription from <user_data> (at most three), by the code of its personal link:

- ``GET /api/users/by-short-uuid/{code}`` — the panel user: the last server connection (``userTraffic.onlineAt``),
  the device limit and the numeric ``id`` that the next two requests take;
- ``GET /api/hwid/devices/{id}`` — the registered devices;
- ``GET /api/users/{id}/subscription-request-history`` — when and by which app the subscription was fetched.

Nothing is ever written to the panel. Parsing keeps only what support may name to the customer — device models,
systems, apps with their versions and dates — so IPs, HWIDs, panel ids and node ids never reach the cache, the logs or
the prompt. The userAgent and the device fields come from the customer's own app, so they are cut down to short plain
labels before the model sees them.
"""
import asyncio
import re
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import aiohttp
import structlog

from ai_support_bot.app.core.config import settings
from ai_support_bot.app.services import settings_store
from ai_support_bot.app.services.answer_buttons import device_action
from ai_support_bot.app.services.response_rules import partnership_offer, starts_technical_issue
from ai_support_bot.app.services.server_status import MSK, _is_local_host
from ai_support_bot.app.services.subscription_address import active_subscriptions, address_problem, link_code


logger = structlog.get_logger(__name__)

TOTAL_TIMEOUT = 5.0  # seconds for all subscriptions together
CACHE_TTL = 60  # seconds; one dialogue does not hit the panel on every message
MAX_SUBSCRIPTIONS = 3
MAX_DEVICES = 8
RECENT_REQUESTS = 20  # «ранее» looks at this many latest app requests
MAX_EARLIER_APPS = 2
STALE_AFTER = timedelta(hours=24)
# 23.11.2026 13:00 MSK the subscription moved to sub.slig.my: a fetch before that came by the old address.
ADDRESS_MOVED_AT = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
BROWSER = 'браузер'
UNKNOWN_APP = 'неизвестное приложение'


@dataclass(frozen=True)
class Device:
    model: str  # «iPhone 17 Pro»; '' when the app did not send it
    system: str  # «iOS 27.2.1»
    app: str  # «INCY 2.7.4»
    created_at: datetime | None


@dataclass(frozen=True)
class Fetch:
    at: datetime
    app: str  # an app label, BROWSER or UNKNOWN_APP


@dataclass(frozen=True)
class Connection:
    number: str
    online_at: datetime | None
    connected: bool | None  # False — never connected to a server; None — the panel does not say
    device_limit: int | None
    devices: tuple[Device, ...] | None  # None — the panel did not answer
    devices_total: int
    fetches: tuple[Fetch, ...] | None  # newest first; None — the panel did not answer


# ---------------------------------------------------------------- settings


def enabled() -> bool:
    """The panel is configured and the owner has not switched the block off (runtime flag CONNECTION_DATA_ENABLED)."""
    return bool(settings.remnawave_enabled) and settings_store.get_bool('CONNECTION_DATA_ENABLED')


def _request_options(base: str) -> tuple[dict[str, str], bool]:
    # The same headers as server_status uses for /api/nodes: direct access to the panel backend needs proxy headers.
    headers = {'Authorization': f'Bearer {settings.REMNAWAVE_API_TOKEN.strip()}', 'Accept': 'application/json'}
    verify_ssl = True
    if _is_local_host(base):
        headers.update({
            'X-Forwarded-Proto': 'https',
            'X-Forwarded-For': '127.0.0.1',
            'X-Real-IP': '127.0.0.1',
            'X-Forwarded-Host': 'localhost',
            'Host': 'localhost',
        })
        if base.startswith('https://'):
            verify_ssl = False
    return headers, verify_ssl


# ---------------------------------------------------------------- parsing


_MISSING_WORDS = frozenset({'', 'none', 'null', 'undefined', 'unknown', 'n/a'})


def _clean(value: Any, limit: int = 40) -> str:
    """A short plain label from a client-controlled field: no tags, quotes or line breaks."""
    if not isinstance(value, str):
        return ''
    text = re.sub(r'[^\w .,+()-]', ' ', value)
    text = re.sub(r'\s+', ' ', text).strip()
    if text.casefold() in _MISSING_WORDS:
        return ''
    return text[:limit].strip()


def _moment(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_SYSTEMS = {
    'ios': 'iOS', 'ipados': 'iPadOS', 'android': 'Android', 'androidtv': 'Android TV', 'android tv': 'Android TV',
    'windows': 'Windows', 'macos': 'macOS', 'mac os': 'macOS', 'darwin': 'macOS', 'linux': 'Linux', 'tvos': 'tvOS',
}


def _system_name(value: Any) -> str:
    text = _clean(value, 20)
    return _SYSTEMS.get(text.casefold(), text)


# The first matching name wins: «v2rayNG» before «v2rayN», «FlClash X» before «FlClash».
_APPS = tuple((re.compile(pattern, re.IGNORECASE), name) for pattern, name in (
    (r'^happ\b', 'Happ'),
    (r'^incy\b', 'INCY'),
    (r'^v2ray\s*tun\b', 'v2RayTun'),
    (r'^flclash\s*x\b', 'FlClash X'),
    (r'^flclash\b', 'FlClash'),
    (r'^koala[\s_-]*clash\b', 'Koala Clash'),
    (r'^streisand\b', 'Streisand'),
    (r'^v2rayng\b', 'v2rayNG'),
    (r'^v2rayn\b', 'v2rayN'),
    (r'^v2box\b', 'V2Box'),
    (r'^hiddify', 'Hiddify'),
    (r'^shadowrocket\b', 'Shadowrocket'),
    (r'^clash[\s_-]*verge', 'Clash Verge'),
    (r'^(?:clash[\s._-]*meta|mihomo)', 'Clash Meta'),
    (r'^nekobox', 'NekoBox'),
    (r'^karing\b', 'Karing'),
    (r'^foxray\b', 'FoXray'),
    (r'^(?:sing-box|sf[aimt])\b', 'sing-box'),
))
# Not an app: the subscription page in a browser or a link preview in a messenger.
_NOT_AN_APP = re.compile(
    r'^(?:mozilla|opera|telegrambot|twitterbot|facebookexternalhit|whatsapp|vkshare|yandexbot|googlebot)\b',
    re.IGNORECASE,
)
_VERSION = re.compile(r'^[A-Za-z]*\s*[/ ]\s*v?(\d+(?:\.\d+){0,3})')
_UA_SYSTEM = re.compile(r'(?:^|[/\s(;])(ios|ipados|android|windows|macos|mac\s?os|linux|tvos)\b', re.IGNORECASE)
_UNKNOWN_NAME = re.compile(r'[A-Za-z][A-Za-z0-9._+ -]{0,29}')


def app_label(user_agent: Any, with_system: bool = True) -> str:
    """«INCY/2.7.4/ios CFNetwork/…» → «INCY 2.7.4 (iOS)»; a browser → BROWSER; nothing readable → ''.

    ``with_system=False`` leaves out the system: a device line names it already.
    """
    text = user_agent.strip() if isinstance(user_agent, str) else ''
    if not text:
        return ''
    if _NOT_AN_APP.match(text):
        return BROWSER
    for pattern, known in _APPS:
        match = pattern.match(text)
        if match:
            name, rest = known, text[match.end():]
            break
    else:
        # An app we do not know: its first word before «/».
        found = _UNKNOWN_NAME.match(text.split('/', 1)[0].strip())
        if not found:
            return ''
        name, rest = _clean(found.group(0), 30), text[found.end():]
        if not name:
            return ''
    version = _VERSION.match(rest)
    label = f'{name} {version.group(1)}' if version else name
    if not with_system:
        return label
    system = _UA_SYSTEM.search(text)
    if system is None and re.search(r'\bdalvik/', text, re.IGNORECASE):
        return f'{label} (Android)'
    if system:
        return f'{label} ({_SYSTEMS.get(re.sub(r"\s+", " ", system.group(1).casefold()), system.group(1))})'
    return label


def _items(payload: Any, key: str) -> tuple[list, int] | None:
    if isinstance(payload, list):
        return payload, len(payload)
    if isinstance(payload, dict) and isinstance(payload.get(key), list):
        items = payload[key]
        return items, max(_number(payload.get('total')) or 0, len(items))
    return None


_EPOCH = datetime.min.replace(tzinfo=UTC)


def parse_devices(payload: Any) -> tuple[tuple[Device, ...], int] | None:
    """Devices newest first and their total; None for an unreadable answer. HWIDs and IPs are not kept."""
    found = _items(payload, 'devices')
    if found is None:
        return None
    items, total = found
    devices = []
    for item in items:
        if not isinstance(item, dict):
            continue
        system = _system_name(item.get('platform'))
        version = _clean(item.get('osVersion'), 20)
        label = app_label(item.get('userAgent'), with_system=not system)
        devices.append(Device(
            model=_clean(item.get('deviceModel')),
            system=f'{system} {version}' if system and version else system,
            app='' if label == BROWSER else label,
            created_at=_moment(item.get('createdAt')),
        ))
    devices.sort(key=lambda device: device.created_at or _EPOCH, reverse=True)
    return tuple(devices), max(total, len(devices))


def parse_fetches(payload: Any) -> tuple[Fetch, ...] | None:
    """Subscription requests newest first; None for an unreadable answer. Request IPs are not kept."""
    found = _items(payload, 'records')
    if found is None:
        return None
    fetches = []
    for record in found[0]:
        if not isinstance(record, dict) or (moment := _moment(record.get('requestAt'))) is None:
            continue
        fetches.append(Fetch(at=moment, app=app_label(record.get('userAgent')) or UNKNOWN_APP))
    fetches.sort(key=lambda fetch: fetch.at, reverse=True)
    return tuple(fetches)


def parse_connection(number: str, user: dict, devices_payload: Any = None, history_payload: Any = None) -> Connection:
    traffic = user.get('userTraffic') if isinstance(user.get('userTraffic'), dict) else None
    online_at = _moment(traffic.get('onlineAt')) if traffic else None
    connected = None
    if online_at is not None:
        connected = True
    elif traffic is not None:
        ever = traffic.get('firstConnectedAt') or (_number(traffic.get('lifetimeUsedTrafficBytes')) or 0) > 0
        connected = None if ever else False
    devices = parse_devices(devices_payload)
    limit = _number(user.get('hwidDeviceLimit'))
    return Connection(
        number=str(number),
        online_at=online_at,
        connected=connected,
        device_limit=limit if limit and limit > 0 else None,
        devices=devices[0] if devices else None,
        devices_total=devices[1] if devices else 0,
        fetches=parse_fetches(history_payload),
    )


# ---------------------------------------------------------------- panel


def _monotonic() -> float:
    return time.monotonic()


class _Cache:
    """Code → (stored at, Connection or None). Failures are kept too: a slow panel must not slow every message."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Connection | None]] = {}

    def get(self, code: str):
        entry = self._store.get(code)
        if entry is None or _monotonic() - entry[0] > CACHE_TTL:
            self._store.pop(code, None)
            return _MISSING
        return entry[1]

    def set(self, code: str, value: Connection | None) -> None:
        if len(self._store) > 2000:
            self._store.clear()
        self._store[code] = (_monotonic(), value)

    def clear(self) -> None:
        self._store.clear()


_MISSING = object()
_cache = _Cache()


def clear_cache() -> None:
    _cache.clear()


async def _get_json(session: aiohttp.ClientSession, kind: str, url: str, verify_ssl: bool) -> Any:
    """The ``response`` payload of a panel GET; None for any answer but 200. The log names only the endpoint."""
    async with session.get(url, ssl=verify_ssl) as response:
        if response.status != 200:
            logger.warning('Connection data request failed', endpoint=kind, status=response.status)
            return None
        body = await response.json(content_type=None)
    return body.get('response', body) if isinstance(body, dict) else None


async def _read_one(session: aiohttp.ClientSession, base: str, verify_ssl: bool, number: str, code: str) -> Connection | None:
    user = await _get_json(session, 'user', f'{base}/api/users/by-short-uuid/{quote(code, safe="")}', verify_ssl)
    if not isinstance(user, dict):
        return None
    panel_id = user.get('id')
    if isinstance(panel_id, bool) or not isinstance(panel_id, (int, str)) or not str(panel_id).strip():
        return None
    user_path = quote(str(panel_id).strip(), safe='')
    devices, history = await asyncio.gather(
        _get_json(session, 'devices', f'{base}/api/hwid/devices/{user_path}', verify_ssl),
        _get_json(session, 'history', f'{base}/api/users/{user_path}/subscription-request-history', verify_ssl),
        return_exceptions=True,
    )
    for kind, part in (('devices', devices), ('history', history)):
        if isinstance(part, BaseException):
            logger.warning('Connection data request failed', endpoint=kind, error=type(part).__name__)
    return parse_connection(
        number, user,
        None if isinstance(devices, BaseException) else devices,
        None if isinstance(history, BaseException) else history,
    )


async def _read_from_panel(subscriptions: list[tuple[str, str]]) -> dict[str, Connection | None]:
    base = settings.REMNAWAVE_API_URL.strip().rstrip('/')
    headers, verify_ssl = _request_options(base)
    found: dict[str, Connection | None] = {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TOTAL_TIMEOUT), headers=headers) as session:
        tasks = {
            asyncio.ensure_future(_read_one(session, base, verify_ssl, number, code)): code
            for number, code in subscriptions
        }
        done, pending = await asyncio.wait(list(tasks), timeout=TOTAL_TIMEOUT)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            logger.warning('Connection data timed out', subscriptions=len(pending))
        for task in done:
            if task.cancelled():
                continue
            if (error := task.exception()) is not None:
                logger.warning('Connection data request failed', endpoint='user', error=type(error).__name__)
                found[tasks[task]] = None
                continue
            found[tasks[task]] = task.result()
    return found


async def read_connections(subscriptions: list[tuple[str, str]]) -> list[Connection]:
    """Panel data for (subscription number, link code) pairs in the given order; a part the panel did not give is skipped.

    Only the customer's own working subscriptions may be passed here (see ``subscription_codes``). Never raises.
    """
    subscriptions = list(subscriptions)[:MAX_SUBSCRIPTIONS]
    if not subscriptions:
        return []
    results: dict[str, Connection | None] = {}
    missing = []
    for number, code in subscriptions:
        cached = _cache.get(code)
        if cached is _MISSING:
            missing.append((number, code))
        else:
            results[code] = cached
    if missing:
        try:
            fetched = await _read_from_panel(missing)
        except Exception as error:
            logger.warning('Connection data failed', error=type(error).__name__)
            fetched = {}
        for _, code in missing:
            results[code] = fetched.get(code)
            _cache.set(code, results[code])
    return [replace(results[code], number=str(number)) for number, code in subscriptions if results.get(code)]


def subscription_codes(user_context: str) -> list[tuple[str, str]]:
    """(number, link code) of the working subscriptions from <user_data>, at most MAX_SUBSCRIPTIONS."""
    pairs: list[tuple[str, str]] = []
    for subscription in active_subscriptions(user_context):
        code = link_code(subscription.link) if subscription.link else None
        if code and all(code != known for _, known in pairs):
            pairs.append((subscription.number, code))
    return pairs[:MAX_SUBSCRIPTIONS]


# ---------------------------------------------------------------- block for the model


def _when(moment: datetime, now: datetime) -> str:
    local = moment.astimezone(MSK)
    return local.strftime('%d.%m %H:%M' if local.year == now.astimezone(MSK).year else '%d.%m.%Y %H:%M')


def _day(moment: datetime, now: datetime) -> str:
    local = moment.astimezone(MSK)
    return local.strftime('%d.%m' if local.year == now.astimezone(MSK).year else '%d.%m.%Y')


def _app_fetches(connection: Connection) -> list[Fetch]:
    return [fetch for fetch in connection.fetches or () if fetch.app != BROWSER]


def _online_line(connection: Connection, now: datetime) -> str:
    if connection.online_at is None:
        if connection.connected is False:
            return '  • подключений к серверам не было'
        return '  • последнее подключение к серверу: нет данных'
    line = f'  • последнее подключение к серверу: {_when(connection.online_at, now)}'
    apps = _app_fetches(connection)
    if now - connection.online_at > STALE_AFTER and apps and now - apps[0].at <= STALE_AFTER:
        line += ' (подписка обновляется, но к серверам давно не подключались)'
    return line


def _fetch_lines(connection: Connection, now: datetime) -> list[str]:
    if connection.fetches is None:
        return ['  • приложение последний раз забирало подписку: нет данных']
    apps = _app_fetches(connection)
    if not apps:
        line = '  • приложение подписку не забирало (записей нет)'
    else:
        last = apps[0]
        line = f'  • приложение последний раз забирало подписку: {_when(last.at, now)} — {last.app}'
        if last.at < ADDRESS_MOVED_AT:
            line += ' (до смены адреса подписки 23.11 — приложение, вероятно, обращается к старому адресу)'
        elif now - last.at > STALE_AFTER:
            line += ' (больше суток назад — приложение, похоже, не может обновить подписку)'
        earlier: dict[str, datetime] = {}
        for fetch in apps[1:RECENT_REQUESTS]:
            if len(earlier) >= MAX_EARLIER_APPS:
                break
            if fetch.app != last.app and fetch.app not in earlier:
                earlier[fetch.app] = fetch.at
        if earlier:
            line += '; ранее: ' + ', '.join(f'{app} — {_when(at, now)}' for app, at in earlier.items())
    lines = [line]
    browser = next((fetch for fetch in connection.fetches if fetch.app == BROWSER), None)
    if browser and (not apps or browser.at > apps[0].at):
        lines.append(f'  • страница подписки открывалась в браузере: {_when(browser.at, now)}')
    return lines


def _device_label(device: Device, now: datetime) -> str:
    if device.model:
        name, details = device.model, [device.system, device.app]
    elif device.app and not device.system:
        name, details = f'устройство с {device.app}', []
    else:
        name, details = 'устройство', [device.system, device.app]
    if device.created_at:
        details.append(f'с {_day(device.created_at, now)}')
    details = [part for part in details if part]
    return f'{name} ({", ".join(details)})' if details else name


def _devices_line(connection: Connection, now: datetime) -> str:
    if connection.devices is None:
        return '  • устройства: нет данных'
    total, limit = connection.devices_total, connection.device_limit
    line = f'  • устройств зарегистрировано: {total}'
    if limit:
        line += f' при лимите {limit}' + (' — все места заняты' if total >= limit else '')
    shown = [_device_label(device, now) for device in connection.devices[:MAX_DEVICES]]
    if shown:
        line += ': ' + '; '.join(shown)
        if total > len(shown):
            line += f' и ещё {total - len(shown)}'
    return line


def format_block(connections: list[Connection], now: datetime | None = None) -> str:
    """The <connection_data> block; '' without data. It carries no IPs, HWIDs or panel ids: parsing never keeps them."""
    if not connections:
        return ''
    now = now or datetime.now(UTC)
    lines = [
        f'<connection_data> Данные панели SligVPN по подпискам этого клиента (время МСК, сейчас {_when(now, now)}). '
        'IP-адреса и идентификаторы устройств не показывай.'
    ]
    for connection in connections:
        lines.append(f'Подписка №{connection.number}:')
        lines.append(_online_line(connection, now))
        lines.extend(_fetch_lines(connection, now))
        lines.append(_devices_line(connection, now))
    lines.append('</connection_data>')
    return '\n'.join(lines)


async def build_block(user_context: str, now: datetime | None = None) -> str:
    """The block for the prompt; '' when switched off, without working subscriptions or panel data. Never raises."""
    try:
        if not enabled():
            return ''
        subscriptions = subscription_codes(user_context)
        if not subscriptions:
            return ''
        return format_block(await read_connections(subscriptions), now=now)
    except Exception as error:
        logger.warning('Connection data block failed', error=type(error).__name__)
        return ''


# ---------------------------------------------------------------- is the block needed


_FAILURE = re.compile(
    r'не\s+(?:работа|робит|подключ|обновля|обновил|обнов(?:ится|ятся)|грузит|загружа|открыва|соедин|коннект|запуска)'
    r'|перестал\w*\s+(?:работ|подключ|грузит|открыва|обновл)|медленн|тормоз|вылета|отвалива|обрыва|пропада'
    r'|(?<![\w/])n\s*/\s*a(?![\w/])'
    r'|не\s+(?:удаётся|удается|удалось|получается|получилось)\s+(?:\w+\s+)?(?:подключ|обнов|импорт|добав)'
    r'|not\s+working|doesn.?t\s+work|can.?t\s+connect',
    re.IGNORECASE,
)
# «Ошибка» alone is a failure too, but «ошибка при оплате» is the payment's, not the VPN's.
_ERROR = re.compile(r'ошибк|\berror\b', re.IGNORECASE)
_DEVICES = re.compile(r'устройств|девайс|лимит|подключен[оаы]?\b|превыш|макс\w*\s+(?:числ|количеств)', re.IGNORECASE)
_GADGET = re.compile(
    r'телефон|смартфон|айфон|iphone|андроид|android|ноутбук|ноуте?\b|компьютер|\bкомп\b|\bпк\b|телевизор|\bтв\b'
    r'|планшет|ipad|айпад|макбук|macbook',
    re.IGNORECASE,
)
_SUBSCRIPTION = re.compile(r'подписк|ссылк|ключ|happ|incy|хапп|инси|vpn|впн', re.IGNORECASE)
# «Какое приложение у меня?», «подписка добавилась?», «когда я последний раз подключался?»
_APP_QUESTION = re.compile(
    r'как(?:ое|ом|им)\s+(?:\w+\s+)?приложени|приложени\w*\s+(?:\w+\s+)?у\s+меня'
    r'|подписк\w*\s+(?:\w+\s+){0,2}(?:добав|импорт)\w*|(?:добав|импорт)\w*\s+(?:\w+\s+){0,2}подписк'
    r'|последн\w*\s+(?:раз\s+)?(?:подключ|обновл|заход)',
    re.IGNORECASE,
)
_PRICE = re.compile(
    r'сколько\s+стоит|стоимост|\bцен[аыуеой]?\b|почём|почем|прайс|скидк|акци[яиюей]|промокод|price|cost|how\s+much',
    re.IGNORECASE,
)
_PAYMENT = re.compile(r'оплат|оплач|заплат|плат[её]ж|списа|деньг|денег|возврат|\bчек\b|пополн|продл|refund|payment', re.IGNORECASE)
_GREETING_WORD = re.compile(
    r'здравствуй\w*|привет\w*|добр\w+|утр\w*|день|вечер\w*|спасибо|благодарю|ок|окей|хорошо|понял\w*|ясно'
    r'|hi|hello|thanks?',
    re.IGNORECASE,
)
FOLLOW_UP_MAX_CHARS = 60


def _greeting(text: str) -> bool:
    # Word by word: one pattern over the whole text would backtrack exponentially on «приветпривет… x».
    words = re.findall(r'[a-zа-яё]+', text, re.IGNORECASE)
    return bool(words) and len(text) <= FOLLOW_UP_MAX_CHARS and all(_GREETING_WORD.fullmatch(word) for word in words)


def _technical(text: str, strict: bool = False) -> bool:
    if _FAILURE.search(text) or address_problem(text):
        return True
    return not strict and bool(_ERROR.search(text) or starts_technical_issue(text))


def _topic(text: str) -> bool:
    return bool(
        _technical(text) or device_action(text) or _DEVICES.search(text)
        or (_GADGET.search(text) and _SUBSCRIPTION.search(text)) or _APP_QUESTION.search(text)
    )


def connection_relevant(question: str, recent: str = '') -> bool:
    """Whether the panel data helps with this message.

    Yes for VPN failures, devices and «which app do I have / is the subscription added»; no for prices, payments
    without a failure, greetings and partnership. A short follow-up (up to FOLLOW_UP_MAX_CHARS, like «Happ» after
    «какое приложение?») takes the topic from ``recent`` — the customer's previous messages.
    """
    text = (question or '').strip()
    if not text or _greeting(text) or partnership_offer(text) or re.search(r'сотруднич', text, re.IGNORECASE):
        return False
    if (_PRICE.search(text) or _PAYMENT.search(text)) and not _technical(text, strict=True):
        return False
    if _topic(text):
        return True
    return bool(recent) and len(text) <= FOLLOW_UP_MAX_CHARS and _topic(recent)
