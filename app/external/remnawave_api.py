import asyncio
import base64
import json
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, ClassVar
from urllib.parse import urlparse

import aiohttp
import structlog
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

from app.config import settings


logger = structlog.get_logger(__name__)


class UserStatus(Enum):
    ACTIVE = 'ACTIVE'
    DISABLED = 'DISABLED'
    LIMITED = 'LIMITED'
    EXPIRED = 'EXPIRED'


class TrafficLimitStrategy(Enum):
    NO_RESET = 'NO_RESET'
    DAY = 'DAY'
    WEEK = 'WEEK'
    MONTH = 'MONTH'
    MONTH_ROLLING = 'MONTH_ROLLING'


@dataclass
class UserTraffic:
    """Данные о трафике пользователя (новая структура API)"""

    used_traffic_bytes: int
    lifetime_used_traffic_bytes: int
    online_at: datetime | None = None
    first_connected_at: datetime | None = None
    last_connected_node_uuid: str | None = None


@dataclass
class RemnaWaveUser:
    # Remnawave 3.0.0 удалил поле `uuid` из UsersSchema: запись пользователя
    # идентифицируется только числовым `id`. `short_uuid` и `vless_uuid`
    # сохранились, но идентификаторами записи не являются — `vless_uuid` это
    # VLESS-креденшл, `short_uuid` — публичный ключ ссылки на подписку.
    id: int
    short_uuid: str
    username: str
    status: UserStatus
    traffic_limit_bytes: int
    traffic_limit_strategy: TrafficLimitStrategy
    expire_at: datetime
    telegram_id: int | None
    email: str | None
    hwid_device_limit: int | None
    description: str | None
    tag: str | None
    subscription_url: str
    active_internal_squads: list[dict[str, str]]
    created_at: datetime
    updated_at: datetime
    user_traffic: UserTraffic | None = None
    sub_revoked_at: datetime | None = None
    last_traffic_reset_at: datetime | None = None
    trojan_password: str | None = None
    vless_uuid: str | None = None
    ss_password: str | None = None
    last_triggered_threshold: int = 0
    happ_link: str | None = None
    happ_crypto_link: str | None = None
    external_squad_uuid: str | None = None

    @property
    def used_traffic_bytes(self) -> int:
        """Обратная совместимость: получение used_traffic_bytes из user_traffic"""
        if self.user_traffic:
            return self.user_traffic.used_traffic_bytes
        return 0

    @property
    def lifetime_used_traffic_bytes(self) -> int:
        """Обратная совместимость: получение lifetime_used_traffic_bytes из user_traffic"""
        if self.user_traffic:
            return self.user_traffic.lifetime_used_traffic_bytes
        return 0

    @property
    def online_at(self) -> datetime | None:
        """Обратная совместимость: получение online_at из user_traffic"""
        if self.user_traffic:
            return self.user_traffic.online_at
        return None

    @property
    def first_connected_at(self) -> datetime | None:
        """Обратная совместимость: получение first_connected_at из user_traffic"""
        if self.user_traffic:
            return self.user_traffic.first_connected_at
        return None


@dataclass
class RemnaWaveInbound:
    """Структура inbound для Internal Squad"""

    uuid: str
    profile_uuid: str
    tag: str
    type: str
    network: str | None = None
    security: str | None = None
    port: int | None = None
    raw_inbound: Any | None = None


@dataclass
class RemnaWaveInternalSquad:
    uuid: str
    name: str
    members_count: int
    inbounds_count: int
    inbounds: list[RemnaWaveInbound]
    view_position: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class RemnaWaveAccessibleNode:
    """Доступная нода для Internal Squad"""

    uuid: str
    node_name: str
    country_code: str
    config_profile_uuid: str
    config_profile_name: str
    active_inbounds: list[str]


@dataclass
class RemnaWaveNode:
    uuid: str
    name: str
    address: str
    country_code: str
    is_connected: bool
    is_disabled: bool
    users_online: int
    traffic_used_bytes: int | None
    traffic_limit_bytes: int | None
    port: int | None = None
    is_connecting: bool = False
    view_position: int = 0
    tags: list[str] | None = None
    last_status_change: datetime | None = None
    last_status_message: str | None = None
    xray_uptime: int = 0
    is_traffic_tracking_active: bool = False
    traffic_reset_day: int | None = None
    notify_percent: int | None = None
    consumption_multiplier: float = 1.0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    provider_uuid: str | None = None
    provider_name: str | None = None  # infra-provider name (из вложенного provider)
    provider_favicon: str | None = None  # provider.faviconLink — иконка хостера
    # v2.7.0: replaced cpuCount/cpuModel/totalRam/xrayVersion/nodeVersion
    versions: dict[str, str] | None = None  # {xray, node}
    system: dict[str, Any] | None = None  # {info: {arch, cpus, cpuModel, memoryTotal, ...}, stats: {...}}
    active_plugin_uuid: str | None = None
    # Адреса узла: [{ip, status}], status ∈ INBOUND/OUTBOUND/MANAGEMENT/...
    # Нужны, чтобы предложить выбор исходного адреса в GeoCheck (3.3.0).
    ips: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_node_online(self) -> bool:
        """Обратная совместимость: is_node_online = is_connected"""
        return self.is_connected

    @property
    def is_xray_running(self) -> bool:
        """Обратная совместимость: xray работает если нода подключена"""
        return self.is_connected


@dataclass
class SubscriptionInfo:
    is_found: bool
    user: dict[str, Any] | None
    links: list[str]
    ss_conf_links: dict[str, str]
    subscription_url: str
    happ: dict[str, str] | None
    happ_link: str | None = None
    happ_crypto_link: str | None = None


@dataclass
class SubscriptionPageConfig:
    """Конфигурация страницы подписки"""

    uuid: str
    name: str
    view_position: int
    config: dict[str, Any] | None = None


@dataclass
class RemnaWaveExternalSquad:
    """Структура External Squad"""

    uuid: str
    name: str
    view_position: int
    members_count: int
    templates: list[dict[str, str]]
    subscription_settings: dict[str, Any] | None = None
    host_overrides: dict[str, Any] | None = None
    # 3.0.0: `responseHeaders` разделён на добавляемые (имя → значение) и
    # удаляемые (список имён). Существующие значения панель мигрирует сама.
    response_headers_add: dict[str, str] | None = None
    response_headers_remove: list[str] | None = None
    hwid_settings: dict[str, Any] | None = None
    custom_remarks: dict[str, Any] | None = None
    subpage_config_uuid: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RemnaWaveAPIError(Exception):
    def __init__(self, message: str, status_code: int = None, response_data: dict = None):
        self.message = message
        self.status_code = status_code
        self.response_data = response_data
        super().__init__(self.message)


class RemnaWaveTransientError(RemnaWaveAPIError):
    """Transient panel failure (timeout / connection) after retries — the panel is
    slow or briefly unreachable, not a real API error. A distinct type so the
    admin-error forwarder (app/logging_handler.py) can skip these instead of
    spamming the admin chat on every slow-panel request; a persistent outage is
    surfaced by the monitoring service, not by per-request error logs."""


class RemnaWaveInvalidUserIdError(RemnaWaveAPIError):
    """Локальный идентификатор панельного пользователя непригоден к запросу.

    Все user-эндпоинты 3.0.0 параметризованы ``numberParamSchema =
    z.coerce.number().positive()``. Нечисловое значение (протухший UUID, None,
    пустая строка) коерсится в NaN, и панель отвечает **400 VALIDATION, а не
    404**. Это опасно: ``is_user_not_found_error`` такой ответ не распознаёт,
    зато распознал бы, если бы мы ослабили её до «любой 400» — и тогда каждый
    промах идентификатора уходил бы в ветку «пользователя нет → создать»,
    плодя дубли в панели.

    Поэтому мусорный идентификатор отсекается на границе клиента и никогда не
    доходит до сети. Тип отдельный, чтобы вызывающий код мог отличить «у нас
    битая ссылка в БД» от «панель отвергла запрос».
    """


def coerce_panel_user_id(value: Any) -> int:
    """Привести локально хранимый идентификатор к числовому id панели.

    Принимает int и строку из цифр (БД отдаёт BigInteger, но JSON/FSM могут
    донести строку). Всё остальное — ошибка, а не запрос в панель.
    """
    if isinstance(value, bool):
        raise RemnaWaveInvalidUserIdError(f'Invalid panel user id: {value!r}')
    if isinstance(value, int):
        candidate = value
    elif isinstance(value, str) and (stripped := value.strip()).isascii() and stripped.isdigit():
        # Строго ASCII-цифры. `isdigit()` в одиночку истинен для '²' и '٥',
        # которые int() либо не принимает вовсе, либо молча переводит в число;
        # а голый int() вдобавок принимает '4_2' и '+42' и превращает их в 42,
        # то есть в id ДРУГОГО пользователя. Для граничной проверки расширять
        # приём нельзя — только сужать.
        candidate = int(stripped)
    else:
        raise RemnaWaveInvalidUserIdError(f'Invalid panel user id: {value!r}')
    if candidate <= 0:
        raise RemnaWaveInvalidUserIdError(f'Invalid panel user id: {value!r}')
    return candidate


def is_user_not_found_error(error: RemnaWaveAPIError) -> bool:
    """Панель не нашла пользователя (удалён/протух идентификатор).

    Разные версии RemnaWave сообщают это по-разному: A018 или A063, и не всегда
    со статусом 404 — проверяем оба признака. Вызывающий код по этому признаку
    пересоздаёт пользователя вместо падения в ошибку.

    Коды A018/A063 в 3.0.0 не изменились (сверено с backend-contract@3.0.0).
    Статус 400 сюда намеренно НЕ входит: см. ``RemnaWaveInvalidUserIdError``.
    """
    if isinstance(error, RemnaWaveInvalidUserIdError):
        return False
    error_code = ((error.response_data or {}).get('errorCode') or '').strip()
    return error.status_code == 404 or error_code in ('A018', 'A063')


# Публичный RSA-ключ Happ для crypt4-ссылок — тот же, которым официальная страница
# подписки Remnawave шифрует ссылку в браузере (@kastov/cryptohapp: JSEncrypt =
# RSA PKCS#1 v1.5 + base64). Ключ публичный, расшифровать ссылку может только
# приложение Happ своим приватным ключом.
HAPP_CRYPTO_V4_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEA3UZ0M3L4K+WjM3vkbQnz
ozHg/cRbEXvQ6i4A8RVN4OM3rK9kU01FdjyoIgywve8OEKsFnVwERZAQZ1Trv60B
hmaM76QQEE+EUlIOL9EpwKWGtTL5lYC1sT9XJMNP3/CI0gP5wwQI88cY/xedpOEB
W72EmOOShHUm/b/3m+HPmqwc4ugKj5zWV5SyiT829aFA5DxSjmIIFBAms7DafmSq
LFTYIQL5cShDY2u+/sqyAw9yZIOoqW2TFIgIHhLPWek/ocDU7zyOrlu1E0SmcQQb
LFqHq02fsnH6IcqTv3N5Adb/CkZDDQ6HvQVBmqbKZKf7ZdXkqsc/Zw27xhG7OfXC
tUmWsiL7zA+KoTd3avyOh93Q9ju4UQsHthL3Gs4vECYOCS9dsXXSHEY/1ngU/hjO
WFF8QEE/rYV6nA4PTyUvo5RsctSQL/9DJX7XNh3zngvif8LsCN2MPvx6X+zLouBX
zgBkQ9DFfZAGLWf9TR7KVjZC/3NsuUCDoAOcpmN8pENBbeB0puiKMMWSvll36+2M
YR1Xs0MgT8Y9TwhE2+TnnTJOhzmHi/BxiUlY/w2E0s4ax9GHAmX0wyF4zeV7kDkc
vHuEdc0d7vDmdw0oqCqWj0Xwq86HfORu6tm1A8uRATjb4SzjTKclKuoElVAVa5Jo
oh/uZMozC65SmDw+N5p6Su8CAwEAAQ==
-----END PUBLIC KEY-----"""

HAPP_CRYPTO_V4_DEEP_LINK_PREFIX = 'happ://crypt4/'

# Официальный Happ crypto API: POST {"url": ...} -> {"encrypted_link": "happ://crypt5/..."}.
# Запасной источник crypt-ссылок, если локальное шифрование выключено/не удалось.
HAPP_CRYPTO_API_URL = 'https://crypto.happ.su/api-v2.php'
HAPP_CRYPTO_API_COOLDOWN_SECONDS = 600
HAPP_CRYPTO_API_CACHE_MAX = 512


class RemnaWaveAPI:
    # Remnawave 2.8.0 удалил POST /api/system/tools/happ/encrypt (панель теперь
    # генерирует crypt-ссылки на клиенте своего subpage). Клиент создаётся на каждый
    # запрос, поэтому состояние happ-шифрования держим на классе (сбрасывается рестартом):
    #  - _happ_encrypt_unavailable: после первого 404 не дёргаем удалённый эндпоинт
    #    (иначе 404 + warning на каждый вызов get_subscription_info/enrich_user_with_happ_link);
    #  - _happ_api_disabled_until: monotonic-метка охлаждения официального Happ API после
    #    сбоя сервиса (сеть/5xx), чтобы внешний сервис не тормозил горячие пути
    #    (enrich на каждом get_user_by_*);
    #  - _happ_api_cache: subscription_url -> crypt-ссылка, спасает от повторных внешних
    #    вызовов, пока ссылка не сохранена в БД (клиент пересоздаётся на каждый запрос);
    #  - _happ_api_failed_urls: URL, которые Happ API отверг (4xx/мусор в ответе) —
    #    их не ретраим, но и весь fallback из-за них не выключаем.
    #  - _happ_local_cache: subscription_url -> crypt4-ссылка локального шифрования.
    #    Паддинг PKCS#1 v1.5 случайный, без кэша каждый вызов даёт новую ссылку для
    #    того же URL — и синки (сравнение с сохранённой subscription_crypto_link)
    #    видели бы ложное «изменение» на каждом проходе.
    _happ_encrypt_unavailable: bool = False
    _happ_api_disabled_until: float = 0.0
    _happ_api_cache: ClassVar[dict[str, str]] = {}
    _happ_api_failed_urls: ClassVar[set[str]] = set()
    _happ_local_cache: ClassVar[dict[str, str]] = {}

    def __init__(
        self,
        base_url: str,
        api_key: str,
        secret_key: str | None = None,
        username: str | None = None,
        password: str | None = None,
        caddy_token: str | None = None,
        auth_type: str = 'api_key',
    ):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.secret_key = secret_key
        self.username = username
        self.password = password
        self.caddy_token = caddy_token
        self.auth_type = auth_type.lower() if auth_type else 'api_key'
        self.session: aiohttp.ClientSession | None = None
        self.authenticated = False

    def _detect_connection_type(self) -> str:
        parsed = urlparse(self.base_url)

        local_hosts = ['localhost', '127.0.0.1', 'remnawave', 'remnawave-backend', 'app', 'api']

        if parsed.hostname in local_hosts:
            return 'local'

        if parsed.hostname:
            if (
                parsed.hostname.startswith('192.168.')
                or parsed.hostname.startswith('10.')
                or parsed.hostname.startswith('172.')
                or parsed.hostname.endswith('.local')
            ):
                return 'local'

        return 'external'

    def _prepare_auth_headers(self) -> dict[str, str]:
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'X-Forwarded-Proto': 'https',
            'X-Forwarded-For': '127.0.0.1',
            'X-Real-IP': '127.0.0.1',
        }

        # Основная авторизация RemnaWave API
        if self.auth_type == 'basic' and self.username and self.password:
            credentials = f'{self.username}:{self.password}'
            encoded_credentials = base64.b64encode(credentials.encode()).decode()
            headers['X-Api-Key'] = f'Basic {encoded_credentials}'
            logger.debug('Используем Basic Auth в X-Api-Key заголовке')
        elif self.auth_type == 'caddy':
            # Caddy Security: caddy_token → X-Api-Key, api_key → Authorization: Bearer
            if self.api_key:
                headers['Authorization'] = f'Bearer {self.api_key}'
            if self.caddy_token:
                headers['X-Api-Key'] = self.caddy_token
            logger.debug('Используем Caddy авторизацию')
        else:
            # api_key или bearer — стандартный режим
            headers['X-Api-Key'] = self.api_key
            headers['Authorization'] = f'Bearer {self.api_key}'
            logger.debug('Используем API ключ в X-Api-Key заголовке')

        return headers

    async def __aenter__(self):
        conn_type = self._detect_connection_type()

        logger.debug('Подключение к Remnawave: (тип: )', base_url=self.base_url, conn_type=conn_type)

        headers = self._prepare_auth_headers()

        cookies = None
        if self.secret_key:
            if ':' in self.secret_key:
                key_name, key_value = self.secret_key.split(':', 1)
                cookies = {key_name: key_value}
                logger.debug('Используем куки: =***', key_name=key_name)
            else:
                cookies = {self.secret_key: self.secret_key}
                logger.debug('Используем куки: =***', secret_key=self.secret_key)

        connector_kwargs = {}

        if conn_type == 'local':
            logger.debug('Используют локальные заголовки proxy')
            headers.update({'X-Forwarded-Host': 'localhost', 'Host': 'localhost'})

            if self.base_url.startswith('https://'):
                ssl_context = ssl.create_default_context()
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
                connector_kwargs['ssl'] = ssl_context
                logger.debug('SSL проверка отключена для локального HTTPS')

        elif conn_type == 'external':
            logger.debug('Используют внешнее подключение с полной SSL проверкой')

        connector = aiohttp.TCPConnector(**connector_kwargs)

        session_kwargs = {
            'timeout': aiohttp.ClientTimeout(
                total=settings.REMNAWAVE_API_TOTAL_TIMEOUT,
                connect=settings.REMNAWAVE_API_CONNECT_TIMEOUT,
            ),
            'headers': headers,
            'connector': connector,
        }

        if cookies:
            session_kwargs['cookies'] = cookies

        self.session = aiohttp.ClientSession(**session_kwargs)
        self.authenticated = True

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def _make_request(
        self, method: str, endpoint: str, data: dict | None = None, params: dict | None = None
    ) -> dict:
        if not self.session:
            raise RemnaWaveAPIError('Session not initialized. Use async context manager.')

        url = f'{self.base_url}{endpoint}'
        max_retries = 3
        base_delay = 1.0

        for attempt in range(max_retries + 1):
            try:
                kwargs = {'url': url, 'params': params}

                # `is not None`, а не truthy: у части команд панели (например
                # POST /api/connections/geocheck) requestBody помечен required,
                # и режим «по умолчанию» — это именно пустой объект `{}`.
                if data is not None:
                    kwargs['json'] = data

                async with self.session.request(method, **kwargs) as response:
                    response_text = await response.text()

                    try:
                        response_data = json.loads(response_text) if response_text else {}
                    except json.JSONDecodeError:
                        response_data = {'raw_response': response_text}

                    if response.status in (429, 502, 503, 504) and attempt < max_retries:
                        retry_after = float(response.headers.get('Retry-After', base_delay * (2**attempt)))
                        logger.warning(
                            'Retryable %s on %s %s, retry %s/%s after %ss',
                            response.status,
                            method,
                            endpoint,
                            attempt + 1,
                            max_retries,
                            retry_after,
                        )
                        await asyncio.sleep(retry_after)
                        continue

                    if response.status >= 400:
                        error_message = response_data.get('message', f'HTTP {response.status}')
                        # Downgrade known-harmless 400s to warning (caller handles them as success)
                        error_lower = str(error_message).lower()
                        is_harmless = response.status == 400 and (
                            'already enabled' in error_lower or 'already disabled' in error_lower
                        )
                        # 404 = "not found" — это всегда обрабатывает вызывающий код (get_user_by_id
                        # → None, delete → success, sync → пересоздание). Логировать его как error
                        # нельзя: error-логи буферизуются и сыплют отчётом в админ-чат (например, при
                        # просмотре юзера с протухшим panel uuid — A063). Понижаем до warning.
                        is_not_found = response.status == 404
                        log = (
                            logger.warning
                            if response.status in (502, 503, 504) or is_harmless or is_not_found
                            else logger.error
                        )
                        log('API Error %s: %s', response.status, error_message)
                        log('Response: %s', response_text[:500])
                        raise RemnaWaveAPIError(error_message, response.status, response_data)

                    return response_data

            except aiohttp.ClientError as e:
                if attempt < max_retries:
                    delay = base_delay * (2**attempt)
                    logger.warning(
                        'Request failed on retry / after s',
                        method=method,
                        endpoint=endpoint,
                        e=e,
                        attempt=attempt + 1,
                        max_retries=max_retries,
                        delay=delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                # Транзиент (таймаут/обрыв связи с панелью) после ретраев — WARNING,
                # а не ERROR: иначе медленная панель спамит админ-чат ошибками
                # (forwarder в logging_handler шлёт только error+).
                logger.warning(
                    'RemnaWave request failed after retries (panel slow/unreachable)',
                    method=method,
                    endpoint=endpoint,
                    error=str(e)[:200],
                )
                raise RemnaWaveTransientError(f'Request failed: {e!s}')
            except TimeoutError as e:
                # Total-request timeout — the panel was slow to respond. Transient:
                # log WARNING and wrap in a typed transient error so the admin-error
                # forwarder skips it. No retry (avoids multi-minute user-facing hangs).
                logger.warning(
                    'RemnaWave request timed out (panel slow)',
                    method=method,
                    endpoint=endpoint,
                    error=str(e)[:200],
                )
                raise RemnaWaveTransientError(f'Request timed out: {method} {endpoint}') from e

        raise RemnaWaveTransientError(f'Max retries exceeded for {method} {endpoint}')

    async def create_user(
        self,
        username: str,
        expire_at: datetime,
        status: UserStatus = UserStatus.ACTIVE,
        traffic_limit_bytes: int = 0,
        traffic_limit_strategy: TrafficLimitStrategy = TrafficLimitStrategy.NO_RESET,
        telegram_id: int | None = None,
        email: str | None = None,
        hwid_device_limit: int | None = None,
        description: str | None = None,
        tag: str | None = None,
        active_internal_squads: list[str] | None = None,
        external_squad_uuid: str | None = None,
    ) -> RemnaWaveUser:
        data = {
            'username': username,
            'status': status.value,
            'expireAt': expire_at.isoformat(),
            'trafficLimitBytes': traffic_limit_bytes,
            'trafficLimitStrategy': traffic_limit_strategy.value,
        }

        if telegram_id is not None:
            data['telegramId'] = telegram_id
        if email:
            data['email'] = email
        if hwid_device_limit is not None:
            data['hwidDeviceLimit'] = hwid_device_limit
        if description:
            data['description'] = description
        if tag:
            data['tag'] = tag
        if active_internal_squads:
            data['activeInternalSquads'] = active_internal_squads
        if external_squad_uuid is not None:
            data['externalSquadUuid'] = external_squad_uuid

        logger.info(
            'POST /api/users payload',
            username=data.get('username'),
            hwidDeviceLimit=data.get('hwidDeviceLimit'),
            status=data.get('status'),
        )
        try:
            response = await self._make_request('POST', '/api/users', data)
        except RemnaWaveAPIError as e:
            # A039 = FK violation on externalSquadUuid — retry without it
            error_code = (e.response_data or {}).get('errorCode', '')
            if error_code == 'A039' and 'externalSquadUuid' in data:
                stale_uuid = data.pop('externalSquadUuid')
                logger.warning(
                    'A039 FK violation on externalSquadUuid, retrying without it',
                    stale_uuid=stale_uuid,
                    username=data.get('username'),
                )
                response = await self._make_request('POST', '/api/users', data)
            else:
                logger.error('POST /api/users FAILED — full payload', payload=data)
                raise
        user = self._parse_user(response['response'])
        logger.info(
            'POST /api/users response',
            panel_user_id=user.id,
            response_hwidDeviceLimit=user.hwid_device_limit,
        )
        return await self.enrich_user_with_happ_link(user)

    async def get_user_by_id(self, user_id: int) -> RemnaWaveUser | None:
        panel_user_id = coerce_panel_user_id(user_id)
        try:
            response = await self._make_request('GET', f'/api/users/{panel_user_id}')
            user = self._parse_user(response['response'])
            return await self.enrich_user_with_happ_link(user)
        except RemnaWaveAPIError as e:
            if e.status_code == 404:
                return None
            raise

    async def get_user_by_short_uuid(self, short_uuid: str) -> RemnaWaveUser | None:
        """``GET /api/users/by-short-uuid/{shortUuid}`` — сохранился в 3.0.0.

        Вместе с ``by-username`` это единственные панельные ворота, через
        которые строку без числового id ещё можно опознать; на них построен
        бэкфил.
        """
        try:
            response = await self._make_request('GET', f'/api/users/by-short-uuid/{short_uuid}')
            user = self._parse_user(response['response'])
            return await self.enrich_user_with_happ_link(user)
        except RemnaWaveAPIError as e:
            if e.status_code == 404:
                return None
            raise

    async def get_user_by_username(self, username: str) -> RemnaWaveUser | None:
        try:
            response = await self._make_request('GET', f'/api/users/by-username/{username}')
            user = self._parse_user(response['response'])
            return await self.enrich_user_with_happ_link(user)
        except RemnaWaveAPIError as e:
            if e.status_code == 404:
                return None
            raise

    async def resolve_user(
        self,
        *,
        user_id: int | None = None,
        short_uuid: str | None = None,
        username: str | None = None,
    ) -> dict[str, Any] | None:
        """``POST /api/users/resolve`` → ``{id, username, shortUuid}`` или None.

        Панель требует ровно одно из полей. Дешевле, чем ``get_user_by_*``:
        отдаёт только идентификаторы, без полного профиля и без обогащения
        happ-ссылкой — то, что нужно для сопоставления строк.
        """
        provided = {
            'id': coerce_panel_user_id(user_id) if user_id is not None else None,
            'shortUuid': short_uuid,
            'username': username,
        }
        data = {key: value for key, value in provided.items() if value is not None}
        if len(data) != 1:
            raise RemnaWaveAPIError(f'resolve_user requires exactly one identifier, got {sorted(data)}')

        try:
            response = await self._make_request('POST', '/api/users/resolve', data)
        except RemnaWaveAPIError as e:
            if is_user_not_found_error(e):
                return None
            raise
        return response.get('response') or None

    async def find_users_by_telegram_id(self, telegram_id: int) -> list[RemnaWaveUser]:
        """Замена удалённому ``GET /api/users/by-telegram-id/{telegramId}``.

        В 3.0.0 поиск живёт в ``GET /api/users/stream`` как query-фильтр.
        """
        # Обогащение включено: заменённый `GET /api/users/by-telegram-id` его
        # делал, и вызывающие читают `happ_crypto_link` из результата.
        return await self.find_users(telegram_id=telegram_id, enrich_happ_links=True)

    async def find_users_by_email(self, email: str) -> list[RemnaWaveUser]:
        """Замена удалённому ``GET /api/users/by-email/{email}``."""
        return await self.find_users(email=email, enrich_happ_links=True)

    async def find_users(
        self,
        *,
        telegram_id: int | None = None,
        email: str | None = None,
        tag: str | None = None,
        status: UserStatus | None = None,
        traffic_limit_strategy: TrafficLimitStrategy | None = None,
        external_squad_uuid: str | None = None,
        enrich_happ_links: bool = False,
        max_results: int | None = None,
    ) -> list[RemnaWaveUser]:
        """Полный обход ``GET /api/users/stream`` с фильтрами (3.0.0).

        Фильтры применяет панель, поэтому обход обычно укладывается в одну
        страницу. ``max_results`` ограничивает выборку, когда вызывающему нужен
        лишь факт существования.
        """
        filters: dict[str, Any] = {}
        if telegram_id is not None:
            filters['telegramId'] = telegram_id
        if email is not None:
            filters['email'] = email
        if tag is not None:
            filters['tag'] = tag
        if status is not None:
            filters['status'] = status.value
        if traffic_limit_strategy is not None:
            filters['trafficLimitStrategy'] = traffic_limit_strategy.value
        if external_squad_uuid is not None:
            filters['externalSquadUuid'] = external_squad_uuid

        found: list[RemnaWaveUser] = []
        cursor: str | None = None
        while True:
            page = await self.get_all_users_page_stream(
                cursor=cursor,
                size=1000,
                enrich_happ_links=enrich_happ_links,
                filters=filters,
            )
            found.extend(page['users'])
            if max_results is not None and len(found) >= max_results:
                return found[:max_results]
            if not page['hasMore'] or not page['nextCursor']:
                return found
            cursor = page['nextCursor']

    async def get_subscription_request_history(self, user_id: int) -> dict:
        """Get subscription request history for a panel user.

        Returns dict with 'total' and 'records' list.
        Each record has: id, userId, requestAt, requestIp, userAgent.

        Эндпоинт всегда отдаёт последние записи целиком: ни в 2.8, ни в 3.0
        схема команды не принимает offset/limit — прежние параметры панель
        просто игнорировала.
        """
        panel_user_id = coerce_panel_user_id(user_id)
        try:
            response = await self._make_request(
                'GET',
                f'/api/users/{panel_user_id}/subscription-request-history',
            )
            return response.get('response', {'total': 0, 'records': []})
        except RemnaWaveAPIError:
            return {'total': 0, 'records': []}

    async def update_user(
        self,
        user_id: int,
        status: UserStatus | None = None,
        traffic_limit_bytes: int | None = None,
        traffic_limit_strategy: TrafficLimitStrategy | None = None,
        expire_at: datetime | None = None,
        telegram_id: int | None = None,
        email: str | None = None,
        hwid_device_limit: int | None = None,
        description: str | None = None,
        tag: str | None = None,
        active_internal_squads: list[str] | None = None,
        external_squad_uuid: str | None | type(...) = ...,
    ) -> RemnaWaveUser:
        # 3.0.0: UpdateUserCommand.RequestBodySchema не имеет поля `uuid`, а
        # .refine((d) => d.username ?? d.id) требует хотя бы один из двух —
        # неизвестный ключ zod срезает молча, и запрос падает в 400.
        panel_user_id = coerce_panel_user_id(user_id)
        data = {'id': panel_user_id}

        if status:
            data['status'] = status.value
        if traffic_limit_bytes is not None:
            data['trafficLimitBytes'] = traffic_limit_bytes
        if traffic_limit_strategy:
            data['trafficLimitStrategy'] = traffic_limit_strategy.value
        if expire_at:
            data['expireAt'] = expire_at.isoformat()
        if telegram_id is not None:
            data['telegramId'] = telegram_id
        if email is not None:
            data['email'] = email
        if hwid_device_limit is not None:
            data['hwidDeviceLimit'] = hwid_device_limit
        if description is not None:
            data['description'] = description
        if tag is not None:
            data['tag'] = tag
        if active_internal_squads is not None:
            data['activeInternalSquads'] = active_internal_squads
        if external_squad_uuid is not ...:
            data['externalSquadUuid'] = external_squad_uuid

        try:
            response = await self._make_request('PATCH', '/api/users', data)
        except RemnaWaveAPIError as e:
            # A039 = FK violation on externalSquadUuid — retry without it
            error_code = (e.response_data or {}).get('errorCode', '')
            if error_code == 'A039' and 'externalSquadUuid' in data:
                stale_uuid = data.pop('externalSquadUuid')
                logger.warning(
                    'A039 FK violation on externalSquadUuid, retrying without it',
                    stale_uuid=stale_uuid,
                    panel_user_id=panel_user_id,
                )
                response = await self._make_request('PATCH', '/api/users', data)
            else:
                # «User not found» — не error: как и 404 в _make_request, его
                # обрабатывает вызывающий код (пересоздание пользователя), а
                # error-логи буферизуются и сыплют отчётом в админ-чат.
                log = logger.warning if is_user_not_found_error(e) else logger.error
                log('PATCH /api/users FAILED — full payload', payload=data)
                raise
        user = self._parse_user(response['response'])
        logger.info(
            'PATCH /api/users response',
            panel_user_id=panel_user_id,
            response_hwidDeviceLimit=user.hwid_device_limit,
        )
        return await self.enrich_user_with_happ_link(user)

    async def delete_user(self, user_id: int) -> bool:
        # 3.0.0: DELETE отвечает 204 (синхронно) либо 202 (в очередь) — тела нет,
        # поля `isDeleted` больше не существует. Успех = отсутствие исключения.
        panel_user_id = coerce_panel_user_id(user_id)
        await self._make_request('DELETE', f'/api/users/{panel_user_id}')
        return True

    async def enable_user(self, user_id: int) -> RemnaWaveUser:
        panel_user_id = coerce_panel_user_id(user_id)
        response = await self._make_request('POST', f'/api/users/{panel_user_id}/actions/enable')
        user = self._parse_user(response['response'])
        return await self.enrich_user_with_happ_link(user)

    async def disable_user(self, user_id: int) -> RemnaWaveUser:
        panel_user_id = coerce_panel_user_id(user_id)
        response = await self._make_request('POST', f'/api/users/{panel_user_id}/actions/disable')
        user = self._parse_user(response['response'])
        return await self.enrich_user_with_happ_link(user)

    async def reset_user_traffic(self, user_id: int) -> RemnaWaveUser:
        panel_user_id = coerce_panel_user_id(user_id)
        response = await self._make_request('POST', f'/api/users/{panel_user_id}/actions/reset-traffic')
        user = self._parse_user(response['response'])
        return await self.enrich_user_with_happ_link(user)

    async def extend_user_expiration(self, user_id: int, days: int) -> RemnaWaveUser:
        """``POST /api/users/{userId}/actions/extend`` — новое в 3.0.0.

        Истёкшему пользователю дата считается от текущего момента и статус
        становится ACTIVE; активному дни добавляются к текущей дате. DISABLED и
        LIMITED продлеваются, но статус не меняют.
        """
        panel_user_id = coerce_panel_user_id(user_id)
        if days < 1:
            raise RemnaWaveAPIError(f'extend_user_expiration requires days >= 1, got {days}')
        response = await self._make_request('POST', f'/api/users/{panel_user_id}/actions/extend', {'days': days})
        user = self._parse_user(response['response'])
        return await self.enrich_user_with_happ_link(user)

    async def revoke_user_subscription(
        self, user_id: int, new_short_uuid: str | None = None, revoke_only_passwords: bool = False
    ) -> RemnaWaveUser:
        """
        Отзывает подписку пользователя (меняет ссылку/пароли).

        Args:
            user_id: числовой id пользователя панели
            new_short_uuid: Новый короткий UUID (опционально, рекомендуется генерировать автоматически)
            revoke_only_passwords: Если True, меняются только пароли без изменения URL подписки
        """
        panel_user_id = coerce_panel_user_id(user_id)
        data = {}
        if new_short_uuid:
            data['shortUuid'] = new_short_uuid
        if revoke_only_passwords:
            data['revokeOnlyPasswords'] = True

        response = await self._make_request('POST', f'/api/users/{panel_user_id}/actions/revoke', data)
        user = self._parse_user(response['response'])
        return await self.enrich_user_with_happ_link(user)

    async def get_user_accessible_nodes(self, user_id: int) -> list[RemnaWaveAccessibleNode]:
        """Получает список доступных нод для пользователя"""
        panel_user_id = coerce_panel_user_id(user_id)
        try:
            response = await self._make_request('GET', f'/api/users/{panel_user_id}/accessible-nodes')
            nodes_data = response.get('response', {}).get('activeNodes', [])
            result = []
            for node in nodes_data:
                # Collect inbounds from activeSquads
                inbounds: list[str] = []
                for squad in node.get('activeSquads', []):
                    inbounds.extend(squad.get('activeInbounds', []))
                result.append(
                    RemnaWaveAccessibleNode(
                        uuid=node['uuid'],
                        node_name=node['nodeName'],
                        country_code=node['countryCode'],
                        config_profile_uuid=node.get('configProfileUuid', ''),
                        config_profile_name=node.get('configProfileName', ''),
                        active_inbounds=inbounds,
                    )
                )
            return result
        except RemnaWaveAPIError as e:
            if e.status_code == 404:
                return []
            raise

    async def get_all_users(self, start: int = 0, size: int = 100, enrich_happ_links: bool = False) -> dict[str, Any]:
        params = {'start': start, 'size': size}
        response = await self._make_request('GET', '/api/users', params=params)

        users = [self._parse_user(user) for user in response['response']['users']]

        if enrich_happ_links:
            users = [await self.enrich_user_with_happ_link(u) for u in users]

        return {'users': users, 'total': response['response']['total']}

    async def get_all_users_page_stream(
        self,
        cursor: str | None = None,
        size: int = 500,
        enrich_happ_links: bool = False,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Одна страница keyset-пагинации пользователей.

        ``GET /api/users/stream`` — курсорная пагинация, устойчивая к мутациям
        во время полного обхода (offset-пагинация ``/api/users`` сдвигается, если
        пользователей удаляют/создают между страницами). Передавай ``nextCursor``
        из предыдущего ответа; на первой странице ``cursor=None``.

        ``filters`` — query-фильтры 3.0.0 (``telegramId``, ``email``, ``tag``,
        ``status``, ``trafficLimitStrategy``, ``externalSquadUuid``); на них
        построены замены удалённым ``by-telegram-id`` / ``by-email``.

        Возвращает ``{'users': [...], 'nextCursor': str | None, 'hasMore': bool}``.
        """
        # Контракт панели (zod): size строго 1..1000, иначе 400 «Validation
        # failed». Настраиваемые батчи (TRAFFIC_CHECK_BATCH_SIZE) со времён
        # offset-пагинации могли быть больше — клэмпим, а не роняем весь обход.
        params: dict[str, Any] = {'size': max(1, min(size, 1000))}
        if cursor is not None:
            # Запрос коерсит курсор в число (z.coerce.number), а ответ отдаёт
            # его строкой — прокидываем как есть, панель приведёт сама.
            params['cursor'] = cursor
        if filters:
            params.update(filters)

        response = await self._make_request('GET', '/api/users/stream', params=params)
        data = response['response']

        users = [self._parse_user(user) for user in data['users']]
        if enrich_happ_links:
            users = [await self.enrich_user_with_happ_link(u) for u in users]

        return {
            'users': users,
            'nextCursor': data.get('nextCursor'),
            'hasMore': bool(data.get('hasMore')),
        }

    async def get_all_users_stream(self, size: int = 500, enrich_happ_links: bool = False) -> list[RemnaWaveUser]:
        """Полный обход всех пользователей панели через курсорную пагинацию (2.8.0+)."""
        all_users: list[RemnaWaveUser] = []
        cursor: str | None = None

        while True:
            page = await self.get_all_users_page_stream(cursor=cursor, size=size, enrich_happ_links=enrich_happ_links)
            all_users.extend(page['users'])
            if not page['hasMore'] or not page['nextCursor']:
                break
            cursor = page['nextCursor']

        return all_users

    async def get_internal_squads(self) -> list[RemnaWaveInternalSquad]:
        response = await self._make_request('GET', '/api/internal-squads')
        return [self._parse_internal_squad(squad) for squad in response['response']['internalSquads']]

    async def get_internal_squad_by_uuid(self, uuid: str) -> RemnaWaveInternalSquad | None:
        try:
            response = await self._make_request('GET', f'/api/internal-squads/{uuid}')
            return self._parse_internal_squad(response['response'])
        except RemnaWaveAPIError as e:
            if e.status_code == 404:
                return None
            raise

    async def create_internal_squad(self, name: str, inbounds: list[str]) -> RemnaWaveInternalSquad:
        data = {'name': name, 'inbounds': inbounds}
        response = await self._make_request('POST', '/api/internal-squads', data)
        return self._parse_internal_squad(response['response'])

    async def update_internal_squad(
        self, uuid: str, name: str | None = None, inbounds: list[str] | None = None
    ) -> RemnaWaveInternalSquad:
        data = {'uuid': uuid}
        if name:
            data['name'] = name
        if inbounds is not None:
            data['inbounds'] = inbounds

        response = await self._make_request('PATCH', '/api/internal-squads', data)
        return self._parse_internal_squad(response['response'])

    async def delete_internal_squad(self, uuid: str) -> bool:
        # 3.0.0: DELETE отдаёт 204/202 без тела — `isDeleted` больше нет.
        await self._make_request('DELETE', f'/api/internal-squads/{uuid}')
        return True

    async def get_internal_squad_accessible_nodes(self, uuid: str) -> list[RemnaWaveAccessibleNode]:
        """Получает список доступных нод для Internal Squad"""
        try:
            response = await self._make_request('GET', f'/api/internal-squads/{uuid}/accessible-nodes')
            return [self._parse_accessible_node(node) for node in response['response']['accessibleNodes']]
        except RemnaWaveAPIError as e:
            if e.status_code == 404:
                return []
            raise

    async def add_users_to_internal_squad(self, uuid: str) -> bool:
        """Добавляет всех пользователей в Internal Squad (bulk action).

        3.0.0: bulk-операции отвечают 202 (выполняются в фоне) либо 204 — тела
        нет, поля `eventSent` в контракте не осталось. Успех = нет исключения.
        """
        await self._make_request('POST', f'/api/internal-squads/{uuid}/bulk-actions/add-users')
        return True

    async def remove_users_from_internal_squad(self, uuid: str) -> bool:
        """Удаляет всех пользователей из Internal Squad (bulk action)"""
        await self._make_request('DELETE', f'/api/internal-squads/{uuid}/bulk-actions/remove-users')
        return True

    async def add_many_users_to_internal_squad(self, uuid: str, user_ids: list[int]) -> bool:
        """``POST /api/internal-squads/{uuid}/bulk-actions/add-many-users`` — новое в 3.0.0.

        До 1000 идентификаторов за вызов, выполняется в фоне (202).
        """
        payload = [coerce_panel_user_id(value) for value in user_ids]
        if not payload:
            return True
        if len(payload) > 1000:
            raise RemnaWaveAPIError(f'add_many_users_to_internal_squad accepts at most 1000 ids, got {len(payload)}')
        await self._make_request(
            'POST', f'/api/internal-squads/{uuid}/bulk-actions/add-many-users', {'userIds': payload}
        )
        return True

    async def remove_many_users_from_internal_squad(self, uuid: str, user_ids: list[int]) -> bool:
        """``DELETE /api/internal-squads/{uuid}/bulk-actions/remove-many-users`` — новое в 3.0.0."""
        payload = [coerce_panel_user_id(value) for value in user_ids]
        if not payload:
            return True
        if len(payload) > 1000:
            raise RemnaWaveAPIError(
                f'remove_many_users_from_internal_squad accepts at most 1000 ids, got {len(payload)}'
            )
        await self._make_request(
            'DELETE', f'/api/internal-squads/{uuid}/bulk-actions/remove-many-users', {'userIds': payload}
        )
        return True

    async def reorder_internal_squads(self, items: list[dict[str, Any]]) -> list[RemnaWaveInternalSquad]:
        """
        Изменяет порядок Internal Squads
        items: список словарей с uuid и viewPosition
        Пример: [{'uuid': '...', 'viewPosition': 0}, {'uuid': '...', 'viewPosition': 1}]
        """
        data = {'items': items}
        response = await self._make_request('POST', '/api/internal-squads/actions/reorder', data)
        return [self._parse_internal_squad(squad) for squad in response['response']['internalSquads']]

    # ============== External Squads API ==============

    async def get_external_squads(self) -> list[RemnaWaveExternalSquad]:
        """Получает список всех External Squads"""
        response = await self._make_request('GET', '/api/external-squads')
        return [self._parse_external_squad(squad) for squad in response['response']['externalSquads']]

    async def get_external_squad_by_uuid(self, uuid: str) -> RemnaWaveExternalSquad | None:
        """Получает External Squad по UUID"""
        try:
            response = await self._make_request('GET', f'/api/external-squads/{uuid}')
            return self._parse_external_squad(response['response'])
        except RemnaWaveAPIError as e:
            if e.status_code == 404:
                return None
            raise

    async def create_external_squad(self, name: str) -> RemnaWaveExternalSquad:
        data = {'name': name}
        response = await self._make_request('POST', '/api/external-squads', data)
        return self._parse_external_squad(response['response'])

    async def update_external_squad(
        self,
        uuid: str,
        name: str | None = None,
        templates: list[dict[str, str]] | None = None,
        subscription_settings: dict[str, Any] | None = None,
        host_overrides: dict[str, Any] | None = None,
        response_headers_add: dict[str, str] | None = None,
        response_headers_remove: list[str] | None = None,
        hwid_settings: dict[str, Any] | None = None,
        custom_remarks: dict[str, Any] | None = None,
        subpage_config_uuid: str | None = None,
    ) -> RemnaWaveExternalSquad:
        data = {'uuid': uuid}
        if name is not None:
            data['name'] = name
        if templates is not None:
            data['templates'] = templates
        if subscription_settings is not None:
            data['subscriptionSettings'] = subscription_settings
        if host_overrides is not None:
            data['hostOverrides'] = host_overrides
        # 3.0.0 разделил `responseHeaders` на добавляемые и удаляемые. Старый ключ
        # zod срезает молча: PATCH возвращает 200, а настройка хедеров тихо
        # теряется — поэтому имя поля тут важнее, чем кажется.
        if response_headers_add is not None:
            data['responseHeadersAdd'] = response_headers_add
        if response_headers_remove is not None:
            data['responseHeadersRemove'] = response_headers_remove
        if hwid_settings is not None:
            data['hwidSettings'] = hwid_settings
        if custom_remarks is not None:
            data['customRemarks'] = custom_remarks
        if subpage_config_uuid is not None:
            data['subpageConfigUuid'] = subpage_config_uuid

        response = await self._make_request('PATCH', '/api/external-squads', data)
        return self._parse_external_squad(response['response'])

    async def delete_external_squad(self, uuid: str) -> bool:
        """Удаляет External Squad"""
        await self._make_request('DELETE', f'/api/external-squads/{uuid}')
        return True

    async def add_users_to_external_squad(self, uuid: str) -> bool:
        """Добавляет всех пользователей в External Squad (bulk action)"""
        await self._make_request('POST', f'/api/external-squads/{uuid}/bulk-actions/add-users')
        return True

    async def remove_users_from_external_squad(self, uuid: str) -> bool:
        """Удаляет всех пользователей из External Squad (bulk action)"""
        await self._make_request('DELETE', f'/api/external-squads/{uuid}/bulk-actions/remove-users')
        return True

    async def reorder_external_squads(self, items: list[dict[str, Any]]) -> list[RemnaWaveExternalSquad]:
        data = {'items': items}
        response = await self._make_request('POST', '/api/external-squads/actions/reorder', data)
        return [self._parse_external_squad(squad) for squad in response['response']['externalSquads']]

    def _parse_external_squad(self, squad_data: dict) -> RemnaWaveExternalSquad:
        """Парсит данные External Squad"""
        info = squad_data.get('info', {})
        return RemnaWaveExternalSquad(
            uuid=squad_data['uuid'],
            name=squad_data['name'],
            view_position=squad_data.get('viewPosition', 0),
            members_count=info.get('membersCount', 0),
            templates=squad_data.get('templates', []),
            subscription_settings=squad_data.get('subscriptionSettings'),
            host_overrides=squad_data.get('hostOverrides'),
            response_headers_add=squad_data.get('responseHeadersAdd'),
            response_headers_remove=squad_data.get('responseHeadersRemove'),
            hwid_settings=squad_data.get('hwidSettings'),
            custom_remarks=squad_data.get('customRemarks'),
            subpage_config_uuid=squad_data.get('subpageConfigUuid'),
            created_at=self._parse_optional_datetime(squad_data.get('createdAt')),
            updated_at=self._parse_optional_datetime(squad_data.get('updatedAt')),
        )

    async def get_all_nodes(self) -> list[RemnaWaveNode]:
        response = await self._make_request('GET', '/api/nodes')
        return [self._parse_node(node) for node in response['response']]

    async def get_node_by_uuid(self, uuid: str) -> RemnaWaveNode | None:
        try:
            response = await self._make_request('GET', f'/api/nodes/{uuid}')
            return self._parse_node(response['response'])
        except RemnaWaveAPIError as e:
            if e.status_code == 404:
                return None
            raise

    async def enable_node(self, uuid: str) -> RemnaWaveNode:
        response = await self._make_request('POST', f'/api/nodes/{uuid}/actions/enable')
        return self._parse_node(response['response'])

    async def disable_node(self, uuid: str) -> RemnaWaveNode:
        response = await self._make_request('POST', f'/api/nodes/{uuid}/actions/disable')
        return self._parse_node(response['response'])

    async def restart_node(self, uuid: str, force_restart: bool = False) -> bool:
        # Команда рестарта ноды принимает forceRestart в теле запроса.
        # Поле ответа `eventSent` вычищено из контракта 3.0.0 — рестарт
        # асинхронный, успех = панель приняла запрос.
        data = {'forceRestart': force_restart}
        await self._make_request('POST', f'/api/nodes/{uuid}/actions/restart', data)
        return True

    async def restart_all_nodes(self, force_restart: bool = False) -> bool:
        data = {'forceRestart': force_restart}
        await self._make_request('POST', '/api/nodes/actions/restart-all', data)
        return True

    async def get_subscription_info(self, short_uuid: str) -> SubscriptionInfo:
        response = await self._make_request('GET', f'/api/sub/{short_uuid}/info')
        info = self._parse_subscription_info(response['response'])
        # Обогащаем happ_crypto_link если его нет но есть subscription_url
        if not info.happ_crypto_link and info.subscription_url:
            encrypted = await self.encrypt_happ_crypto_link(info.subscription_url)
            if encrypted:
                info.happ_crypto_link = encrypted
        return info

    async def get_subscription_by_short_uuid(self, short_uuid: str) -> str:
        async with self.session.get(f'{self.base_url}/api/sub/{short_uuid}') as response:
            if response.status >= 400:
                raise RemnaWaveAPIError(f'Failed to get subscription: {response.status}')
            return await response.text()

    async def get_subscription_by_client_type(self, short_uuid: str, client_type: str) -> str:
        valid_types = ['stash', 'singbox', 'singbox-legacy', 'mihomo', 'json', 'v2ray-json', 'clash']
        if client_type not in valid_types:
            raise ValueError(f'Invalid client type. Must be one of: {valid_types}')

        async with self.session.get(f'{self.base_url}/api/sub/{short_uuid}/{client_type}') as response:
            if response.status >= 400:
                raise RemnaWaveAPIError(f'Failed to get subscription: {response.status}')
            return await response.text()

    async def get_subscription_links(self, short_uuid: str) -> dict[str, str]:
        base_url = f'{self.base_url}/api/sub/{short_uuid}'

        links = {
            'base': base_url,
            'stash': f'{base_url}/stash',
            'singbox': f'{base_url}/singbox',
            'singbox_legacy': f'{base_url}/singbox-legacy',
            'mihomo': f'{base_url}/mihomo',
            'json': f'{base_url}/json',
            'v2ray_json': f'{base_url}/v2ray-json',
            'clash': f'{base_url}/clash',
        }

        return links

    async def get_outline_subscription(self, short_uuid: str, encoded_tag: str) -> str:
        async with self.session.get(f'{self.base_url}/api/sub/outline/{short_uuid}/ss/{encoded_tag}') as response:
            if response.status >= 400:
                raise RemnaWaveAPIError(f'Failed to get outline subscription: {response.status}')
            return await response.text()

    async def get_system_stats(self, tz: str | None = None) -> dict[str, Any]:
        params = {'tz': tz} if tz else None
        response = await self._make_request('GET', '/api/system/stats', params=params)
        return response['response']

    async def get_health(self) -> dict[str, Any]:
        """Panel runtime health: {runtimeMetrics: [{rss, heapUsed, heapTotal,
        eventLoopDelayMs, eventLoopP99Ms, uptime, instanceId, ...}]}."""
        response = await self._make_request('GET', '/api/system/health')
        return response['response']

    async def get_hwid_top_users(self, size: int = 10, start: int = 0) -> dict[str, Any]:
        """Top users by HWID device count: {users:[{username,devicesCount,...}], total}."""
        params = {'size': size, 'start': start}
        response = await self._make_request('GET', '/api/hwid/devices/top-users', params=params)
        return response['response']

    async def get_subscription_request_stats(self) -> dict[str, Any]:
        """Subscription request history: {byParsedApp:[{app,count}], hourlyRequestStats:[...]}."""
        response = await self._make_request('GET', '/api/subscription-request-history/stats')
        return response['response']

    async def get_system_metadata(self) -> dict[str, Any]:
        """
        Получает метаданные системы Remnawave.

        Returns:
            Dict с полями:
            - version: версия Remnawave
            - build: {time, number} - информация о сборке
            - git: {backend: {commitSha}, node: {commitSha}} - информация о коммитах
        """
        response = await self._make_request('GET', '/api/system/metadata')
        return response['response']

    async def get_bandwidth_stats(self, tz: str | None = None) -> dict[str, Any]:
        params = {'tz': tz} if tz else None
        response = await self._make_request('GET', '/api/system/stats/bandwidth', params=params)
        return response['response']

    async def get_nodes_statistics(self) -> dict[str, Any]:
        response = await self._make_request('GET', '/api/system/stats/nodes')
        return response['response']

    async def get_stats_recap(self) -> dict[str, Any]:
        """Panel recap: {thisMonth:{users,traffic}, total:{users,nodes,traffic,nodesRam,
        nodesCpuCores,distinctCountries}, version, initDate}."""
        response = await self._make_request('GET', '/api/system/stats/recap')
        return response['response']

    async def get_hwid_devices_stats(self) -> dict[str, Any]:
        """HWID device stats.

        Remnawave 2.8.0 shape: {byPlatform:[{platform,count,byApp:[{app,count}]}],
        stats:{totalUniqueDevices,totalHwidDevices,averageHwidDevicesPerUser}}.
        До 2.8.0 ``byApp`` был top-level массивом (см. backward-compat в
        ``RemnaWaveService.get_devices_statistics``).
        """
        response = await self._make_request('GET', '/api/hwid/devices/stats')
        return response['response']

    async def get_nodes_metrics(self) -> dict[str, Any]:
        response = await self._make_request('GET', '/api/system/nodes/metrics')
        return response.get('response', {})

    async def get_nodes_realtime_usage(self) -> list[dict[str, Any]]:
        """Get per-node metrics with per-inbound traffic breakdown.

        Uses /api/system/nodes/metrics (replacement for removed /api/bandwidth-stats/nodes/realtime).
        Returns list of dicts with node totals + inbounds/outbounds arrays.
        """
        try:
            metrics = await self.get_nodes_metrics()
            nodes = metrics.get('nodes', [])
            if isinstance(metrics, list):
                nodes = metrics
            result = []
            for node in nodes:
                download_bytes = 0
                upload_bytes = 0
                inbounds = []
                for ib in node.get('inboundsStats', []):
                    ib_dl = parse_bytes(ib.get('download', '0'))
                    ib_ul = parse_bytes(ib.get('upload', '0'))
                    download_bytes += ib_dl
                    upload_bytes += ib_ul
                    inbounds.append(
                        {
                            'tag': ib.get('tag', 'unknown'),
                            'downloadBytes': ib_dl,
                            'uploadBytes': ib_ul,
                            'totalBytes': ib_dl + ib_ul,
                        }
                    )

                outbounds = []
                for ob in node.get('outboundsStats', []):
                    ob_dl = parse_bytes(ob.get('download', '0'))
                    ob_ul = parse_bytes(ob.get('upload', '0'))
                    outbounds.append(
                        {
                            'tag': ob.get('tag', 'unknown'),
                            'downloadBytes': ob_dl,
                            'uploadBytes': ob_ul,
                            'totalBytes': ob_dl + ob_ul,
                        }
                    )

                result.append(
                    {
                        'nodeUuid': node.get('nodeUuid', ''),
                        'nodeName': node.get('nodeName', ''),
                        'countryEmoji': node.get('countryEmoji', ''),
                        'providerName': node.get('providerName', ''),
                        'downloadBytes': download_bytes,
                        'uploadBytes': upload_bytes,
                        'totalBytes': download_bytes + upload_bytes,
                        'usersOnline': node.get('usersOnline', 0),
                        'inbounds': inbounds,
                        'outbounds': outbounds,
                    }
                )
            return result
        except Exception as e:
            logger.warning('Failed to get nodes metrics for realtime usage', error=e)
            return []

    async def get_user_stats_usage(self, user_id: int, start_date: str, end_date: str) -> dict[str, Any]:
        """Потребление трафика пользователем за период.

        Раньше уходило в ``.../legacy``; в 3.0.0 legacy-эндпоинты удалены, и
        форма ответа другая: ``{categories, sparklineData, topNodes, series}``
        вместо плоского словаря с ``total``.
        """
        return await self.get_bandwidth_stats_user(user_id, start_date, end_date)

    # ============== Connections API (GeoCheck, 3.3.0) ==============

    async def request_node_geocheck(
        self,
        node_uuid: str,
        ip: str | None = None,
        interface: str | None = None,
    ) -> str:
        """Ставит проверку геоданных ноды в очередь и возвращает ``jobId``.

        Проверка асинхронная: панель отдаёт идентификатор задачи, результат
        забирается через :meth:`get_node_geocheck_result` (нода отвечает до
        минуты). Требует Panel 3.3.0 и Node 3.3.0 — на более старой панели
        эндпоинта нет и запрос вернёт 404.

        Маршрут выбирается ровно один: либо исходный ``ip``, либо сетевой
        ``interface``, либо ничего (маршрут узла по умолчанию).
        """
        if ip and interface:
            raise RemnaWaveAPIError('geocheck accepts either ip or interface, not both')

        data: dict[str, Any] = {}
        if ip and ip.strip():
            data['ip'] = ip.strip()
        elif interface and interface.strip():
            data['interface'] = interface.strip()

        response = await self._make_request('POST', f'/api/connections/geocheck/{node_uuid}', data)
        job_id = (response.get('response') or {}).get('jobId')
        if not job_id:
            raise RemnaWaveAPIError('geocheck response has no jobId', response_data=response)
        return job_id

    async def get_node_geocheck_result(self, job_id: str) -> dict[str, Any]:
        """Статус задачи GeoCheck: ``{isCompleted, isFailed, result}``.

        Пока задача в работе ``result`` равен ``None``. В готовом результате
        лежат ``success``, ``image`` (SVG-отчёт в base64) и ``rawReport``
        (тот же отчёт в JSON, без картинки).
        """
        response = await self._make_request('GET', f'/api/connections/geocheck/{job_id}')
        return response['response']

    # ============== Bandwidth Stats API ==============

    async def get_bandwidth_stats_nodes(self, start_date: str, end_date: str) -> dict[str, Any]:
        params = {'start': start_date, 'end': end_date}
        response = await self._make_request('GET', '/api/bandwidth-stats/nodes', params=params)
        return response['response']

    async def get_bandwidth_stats_node_users(
        self, node_uuid: str, start_date: str, end_date: str, top_users_limit: int = 10
    ) -> dict[str, Any]:
        params = {'start': start_date, 'end': end_date, 'topUsersLimit': top_users_limit}
        response = await self._make_request('GET', f'/api/bandwidth-stats/nodes/{node_uuid}/users', params=params)
        return response['response']

    async def get_bandwidth_stats_nodes_usage(
        self,
        node_uuids: list[str],
        start_date: str,
        end_date: str,
        min_total_bytes: int = 0,
    ) -> dict[str, Any]:
        """``POST /api/bandwidth-stats/nodes/usage`` — новое в 3.0.0.

        Замена удалённому ``.../users/legacy`` там, где нужна привязка трафика к
        конкретным пользователям: отдаёт ``{nodes: [{uuid, users: [{id,
        totalBytes}]}]}``. В отличие от legacy — без разбивки по дням.

        Идентификаторы узлов идут телом, а период и порог — query-параметрами:
        так устроена команда в контракте. Даты строго ``YYYY-MM-DD``, не ISO с
        ``Z`` — иначе панель отвечает 400.
        """
        if not node_uuids:
            raise RemnaWaveAPIError('get_bandwidth_stats_nodes_usage requires at least one node uuid')
        response = await self._make_request(
            'POST',
            '/api/bandwidth-stats/nodes/usage',
            {'nodesUuids': node_uuids},
            params={'start': start_date, 'end': end_date, 'minTotalBytes': min_total_bytes},
        )
        return response['response']

    async def get_bandwidth_stats_user(self, user_id: int, start_date: str, end_date: str) -> dict[str, Any]:
        panel_user_id = coerce_panel_user_id(user_id)
        params = {'start': start_date, 'end': end_date}
        response = await self._make_request('GET', f'/api/bandwidth-stats/users/{panel_user_id}', params=params)
        return response['response']

    # ============== Subscription Page Configs API ==============

    async def get_subscription_page_configs(self) -> list[SubscriptionPageConfig]:
        response = await self._make_request('GET', '/api/subscription-page-configs')
        configs_data = response['response'].get('configs', [])
        return [self._parse_subscription_page_config(c) for c in configs_data]

    async def get_subscription_page_config(self, uuid: str) -> SubscriptionPageConfig | None:
        try:
            response = await self._make_request('GET', f'/api/subscription-page-configs/{uuid}')
            return self._parse_subscription_page_config(response['response'])
        except RemnaWaveAPIError as e:
            if e.status_code == 404:
                return None
            raise

    async def create_subscription_page_config(self, name: str) -> SubscriptionPageConfig:
        data = {'name': name}
        response = await self._make_request('POST', '/api/subscription-page-configs', data)
        return self._parse_subscription_page_config(response['response'])

    async def update_subscription_page_config(
        self, uuid: str, name: str | None = None, config: dict[str, Any] | None = None
    ) -> SubscriptionPageConfig:
        data = {'uuid': uuid}
        if name is not None:
            data['name'] = name
        if config is not None:
            data['config'] = config
        response = await self._make_request('PATCH', '/api/subscription-page-configs', data)
        return self._parse_subscription_page_config(response['response'])

    async def delete_subscription_page_config(self, uuid: str) -> bool:
        await self._make_request('DELETE', f'/api/subscription-page-configs/{uuid}')
        return True

    async def reorder_subscription_page_configs(self, items: list[dict[str, Any]]) -> list[SubscriptionPageConfig]:
        data = {'items': items}
        response = await self._make_request('POST', '/api/subscription-page-configs/actions/reorder', data)
        configs_data = response['response'].get('configs', [])
        return [self._parse_subscription_page_config(c) for c in configs_data]

    async def clone_subscription_page_config(self, clone_from_uuid: str) -> SubscriptionPageConfig:
        data = {'cloneFromUuid': clone_from_uuid}
        response = await self._make_request('POST', '/api/subscription-page-configs/actions/clone', data)
        return self._parse_subscription_page_config(response['response'])

    async def get_subpage_config_by_short_uuid(self, short_uuid: str) -> dict[str, Any] | None:
        try:
            response = await self._make_request('GET', f'/api/subscriptions/subpage-config/{short_uuid}')
            return response.get('response')
        except RemnaWaveAPIError as e:
            if e.status_code == 404:
                return None
            raise

    def _parse_subscription_page_config(self, data: dict) -> SubscriptionPageConfig:
        """Парсит данные конфигурации страницы подписки"""
        return SubscriptionPageConfig(
            uuid=data['uuid'], name=data['name'], view_position=data['viewPosition'], config=data.get('config')
        )

    async def get_all_hwid_devices(self) -> dict[str, Any]:
        """GET /api/hwid/devices — all devices for all users (paginated, max 1000/page)."""
        all_devices: list[dict[str, Any]] = []
        start = 0
        page_size = 1000

        while True:
            response = await self._make_request('GET', '/api/hwid/devices', params={'start': start, 'size': page_size})
            data = response.get('response', {'devices': [], 'total': 0})
            devices = data.get('devices', [])
            total = data.get('total', 0)
            all_devices.extend(devices)

            if len(all_devices) >= total or not devices:
                break
            start += len(devices)

        return {'devices': all_devices, 'total': len(all_devices)}

    async def get_all_panel_subscriptions(self) -> list[dict[str, Any]]:
        """GET /api/subscriptions — all panel subscriptions."""
        response = await self._make_request('GET', '/api/subscriptions')
        return response.get('response') or []

    async def get_user_devices(self, user_id: int) -> dict[str, Any]:
        panel_user_id = coerce_panel_user_id(user_id)
        try:
            response = await self._make_request('GET', f'/api/hwid/devices/{panel_user_id}')
            return response['response']
        except RemnaWaveAPIError as e:
            if e.status_code == 404:
                return {'total': 0, 'devices': []}
            raise

    async def get_user_devices_all(self, user_id: int) -> dict[str, Any]:
        """GET /api/hwid/devices/{userId} — all devices for a user (paginated).

        Элементы устройств несут ``userId`` (число), а не ``userUuid``.
        """
        panel_user_id = coerce_panel_user_id(user_id)
        all_devices: list[dict[str, Any]] = []
        start = 0
        page_size = 1000

        try:
            while True:
                response = await self._make_request(
                    'GET', f'/api/hwid/devices/{panel_user_id}', params={'start': start, 'size': page_size}
                )
                data = response.get('response', {'devices': [], 'total': 0})
                devices = data.get('devices', [])
                total = data.get('total', 0)
                all_devices.extend(devices)

                if len(all_devices) >= total or not devices:
                    break
                start += len(devices)
        except RemnaWaveAPIError as e:
            if e.status_code == 404:
                return {'total': 0, 'devices': []}
            raise

        return {'devices': all_devices, 'total': len(all_devices)}

    async def reset_user_devices(self, user_id: int) -> bool:
        """Снести все HWID-устройства пользователя одним запросом.

        Раньше это был цикл из N удалений с эвристикой «успех, если упало меньше
        половины». ``POST /api/hwid/devices/delete-all`` с телом ``{userId}``
        делает то же атомарно, поэтому и результат теперь однозначный.
        """
        try:
            panel_user_id = coerce_panel_user_id(user_id)
        except RemnaWaveInvalidUserIdError as e:
            logger.error('Сброс устройств: непригодный идентификатор', error=e.message)
            return False

        try:
            result = await self._make_request('POST', '/api/hwid/devices/delete-all', data={'userId': panel_user_id})
        except RemnaWaveAPIError as e:
            if e.status_code == 404:
                return True  # пользователя/устройств уже нет — цель достигнута
            logger.error(
                'Ошибка при сбросе устройств',
                panel_user_id=panel_user_id,
                status_code=e.status_code,
                error=e.message,
            )
            return False
        except Exception as e:
            logger.error('Ошибка при сбросе устройств', panel_user_id=panel_user_id, error=e)
            return False

        # Ответ несёт состояние ПОСЛЕ удаления (`{total, devices}` по контракту).
        # Игнорировать его нельзя: панель может ответить 200, оставив устройства
        # на месте, и вызывающий отрапортует пользователю ложный успех.
        payload = (result or {}).get('response') if isinstance(result, dict) else None
        if isinstance(payload, dict):
            remaining = payload.get('total')
            if remaining is None:
                remaining = len(payload.get('devices') or [])
            try:
                remaining = int(remaining)
            except (TypeError, ValueError):
                remaining = 0
            if remaining > 0:
                logger.error(
                    'Панель приняла сброс устройств, но устройства остались',
                    panel_user_id=panel_user_id,
                    remaining=remaining,
                )
                return False

        return True

    async def remove_device(self, user_id: int, device_hwid: str) -> bool:
        """Удалить одно HWID-устройство пользователя.

        Возвращает True только когда устройство действительно отсутствует.
        Панель на POST /api/hwid/devices/delete отвечает ОСТАВШИМИСЯ устройствами
        ({response: {total, devices}} — та же форма, что и у GET), поэтому мы
        проверяем, что целевого hwid в этом списке больше нет, вместо того чтобы
        считать «нет исключения == удалено». 404 означает, что устройство/пользователь
        уже отсутствует — это и есть нужный результат, поэтому тоже success.
        Панели, отвечающие «голым» ack без списка devices, обрабатываются как раньше
        (успешный запрос == удалено).
        """
        try:
            delete_data = {'userId': coerce_panel_user_id(user_id), 'hwid': device_hwid}
        except RemnaWaveInvalidUserIdError as e:
            logger.error('Удаление устройства: непригодный идентификатор', error=e.message)
            return False
        try:
            response = await self._make_request('POST', '/api/hwid/devices/delete', data=delete_data)
        except RemnaWaveAPIError as e:
            if e.status_code == 404:
                return True  # устройства уже нет — цель достигнута
            logger.error(
                'Ошибка удаления устройства', device_hwid=device_hwid, status_code=e.status_code, error=e.message
            )
            return False
        except Exception as e:
            logger.error('Ошибка удаления устройства', device_hwid=device_hwid, error=e)
            return False

        payload = response.get('response') if isinstance(response, dict) else None
        devices = payload.get('devices') if isinstance(payload, dict) else None
        if isinstance(devices, list):
            still_present = any(isinstance(d, dict) and d.get('hwid') == device_hwid for d in devices)
            if still_present:
                logger.warning(
                    'Панель приняла запрос, но устройство осталось в списке',
                    device_hwid=device_hwid,
                    remaining=payload.get('total'),
                )
                return False

        return True

    async def encrypt_happ_crypto_link(self, link_to_encrypt: str) -> str | None:
        encrypted = self._encrypt_locally(link_to_encrypt)
        if encrypted:
            return encrypted
        encrypted = await self._encrypt_via_panel(link_to_encrypt)
        if encrypted:
            return encrypted
        return await self._encrypt_via_happ_api(link_to_encrypt)

    @staticmethod
    def _encrypt_locally(link_to_encrypt: str) -> str | None:
        """Шифрует ссылку подписки в happ://crypt4/... локально, без сети.

        Повторяет алгоритм официальной страницы подписки Remnawave
        (@kastov/cryptohapp): RSA PKCS#1 v1.5 публичным ключом Happ + base64.
        В отличие от панельного эндпоинта (удалён в 2.8.0) и внешнего Happ API
        (лимиты, cooldown, утечка URL подписки вовне) работает всегда и бесплатно.
        """
        if not settings.HAPP_CRYPTOLINK_LOCAL_ENCRYPTION_ENABLED:
            return None
        cached = RemnaWaveAPI._happ_local_cache.get(link_to_encrypt)
        if cached:
            return cached
        try:
            key = RSA.import_key(HAPP_CRYPTO_V4_PUBLIC_KEY)
            payload = link_to_encrypt.encode('utf-8')
            # Лимит PKCS#1 v1.5 — размер ключа минус 11 байт (501 для RSA-4096)
            if len(payload) > key.size_in_bytes() - 11:
                logger.warning('Ссылка слишком длинная для happ crypt4', length=len(payload))
                return None
            encrypted = PKCS1_v1_5.new(key).encrypt(payload)
            link = HAPP_CRYPTO_V4_DEEP_LINK_PREFIX + base64.b64encode(encrypted).decode('ascii')
            if len(RemnaWaveAPI._happ_local_cache) >= HAPP_CRYPTO_API_CACHE_MAX:
                RemnaWaveAPI._happ_local_cache.clear()
            RemnaWaveAPI._happ_local_cache[link_to_encrypt] = link
            return link
        except Exception as e:
            logger.warning('Не удалось локально зашифровать happ ссылку', error=e)
            return None

    async def _encrypt_via_panel(self, link_to_encrypt: str) -> str | None:
        # Эндпоинт удалён в Remnawave 2.8.0 — после первого 404 больше не дёргаем.
        if RemnaWaveAPI._happ_encrypt_unavailable:
            return None
        try:
            data = {'linkToEncrypt': link_to_encrypt}
            response = await self._make_request('POST', '/api/system/tools/happ/encrypt', data)
            return response.get('response', {}).get('encryptedLink')
        except RemnaWaveAPIError as e:
            if e.status_code == 404:
                RemnaWaveAPI._happ_encrypt_unavailable = True
                logger.info(
                    'happ-encrypt эндпоинт недоступен (удалён в Remnawave 2.8.0) — '
                    'переключаюсь на официальный Happ API до рестарта'
                )
                return None
            logger.warning('Не удалось зашифровать happ ссылку', message=e.message)
            return None
        except Exception as e:
            logger.warning('Ошибка при шифровании happ ссылки', error=e)
            return None

    async def _encrypt_via_happ_api(self, link_to_encrypt: str) -> str | None:
        """Fallback: официальный Happ crypto API v2 -> happ://crypt5/... ."""
        if not settings.HAPP_CRYPTOLINK_API_FALLBACK_ENABLED:
            return None

        cached = RemnaWaveAPI._happ_api_cache.get(link_to_encrypt)
        if cached:
            return cached

        if link_to_encrypt in RemnaWaveAPI._happ_api_failed_urls:
            return None

        if time.monotonic() < RemnaWaveAPI._happ_api_disabled_until:
            return None

        try:
            encrypted = await self._call_happ_crypto_api(link_to_encrypt)
        except RemnaWaveAPIError as e:
            # 429 — троттлинг сервиса (лечится паузой ниже), остальные 4xx — отказ
            # по конкретному URL: не выключаем fallback глобально, просто не ретраим
            # этот URL до рестарта.
            if e.status_code is not None and 400 <= e.status_code < 500 and e.status_code != 429:
                self._remember_failed_happ_url(link_to_encrypt)
                logger.warning('Happ crypto API отклонил ссылку', status=e.status_code)
                return None
            RemnaWaveAPI._happ_api_disabled_until = time.monotonic() + HAPP_CRYPTO_API_COOLDOWN_SECONDS
            logger.warning(
                'Happ crypto API недоступен — пауза перед повторными попытками',
                error=e,
                cooldown_seconds=HAPP_CRYPTO_API_COOLDOWN_SECONDS,
            )
            return None
        except Exception as e:
            RemnaWaveAPI._happ_api_disabled_until = time.monotonic() + HAPP_CRYPTO_API_COOLDOWN_SECONDS
            logger.warning(
                'Happ crypto API недоступен — пауза перед повторными попытками',
                error=e,
                cooldown_seconds=HAPP_CRYPTO_API_COOLDOWN_SECONDS,
            )
            return None

        if not encrypted or not encrypted.startswith('happ://'):
            # 200 с мусором в теле — считаем проблемой конкретного URL, не сервиса.
            self._remember_failed_happ_url(link_to_encrypt)
            logger.warning('Happ crypto API вернул неожиданный ответ', has_link=bool(encrypted))
            return None

        if len(RemnaWaveAPI._happ_api_cache) >= HAPP_CRYPTO_API_CACHE_MAX:
            RemnaWaveAPI._happ_api_cache.clear()
        RemnaWaveAPI._happ_api_cache[link_to_encrypt] = encrypted
        return encrypted

    @staticmethod
    def _remember_failed_happ_url(link_to_encrypt: str) -> None:
        if len(RemnaWaveAPI._happ_api_failed_urls) >= HAPP_CRYPTO_API_CACHE_MAX:
            RemnaWaveAPI._happ_api_failed_urls.clear()
        RemnaWaveAPI._happ_api_failed_urls.add(link_to_encrypt)

    async def _call_happ_crypto_api(self, link_to_encrypt: str) -> str | None:
        # Отдельная сессия: панельные заголовки (X-Api-Key/Authorization) не должны
        # утекать на внешний сервис.
        timeout = aiohttp.ClientTimeout(total=10, connect=5)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.post(HAPP_CRYPTO_API_URL, json={'url': link_to_encrypt}) as response,
        ):
            if response.status != 200:
                raise RemnaWaveAPIError(f'Happ crypto API вернул статус {response.status}', response.status)
            try:
                payload = await response.json(content_type=None)
            except ValueError:
                # 200 с не-JSON телом — мусор по конкретному URL, а не сбой сервиса:
                # None уводит вызывающего в per-URL ветку, а не в глобальный cooldown.
                payload = None
        if not isinstance(payload, dict):
            return None
        encrypted = payload.get('encrypted_link')
        return encrypted if isinstance(encrypted, str) else None

    async def enrich_user_with_happ_link(self, user: RemnaWaveUser) -> RemnaWaveUser:
        if not user.happ_crypto_link and user.subscription_url:
            # Сначала локальное шифрование (мгновенно, без сети), затем панельный
            # эндпоинт (2.7.x). Внешний Happ API на этом горячем пути (enrich зовётся
            # на каждом get_user_by_*) дёргаем только когда crypt-ссылки реально нужны
            # боту (happ_cryptolink-режим); кабинет генерирует недостающие ссылки сам —
            # по факту CRYPT-шаблона в app-config, — так что URL подписок не утекают
            # вовне без надобности.
            encrypted = self._encrypt_locally(user.subscription_url) or await self._encrypt_via_panel(
                user.subscription_url
            )
            if not encrypted and settings.is_happ_cryptolink_mode():
                encrypted = await self._encrypt_via_happ_api(user.subscription_url)
            if encrypted:
                user.happ_crypto_link = encrypted
        return user

    def _parse_user_traffic(self, traffic_data: dict | None) -> UserTraffic | None:
        """Парсит данные трафика из нового формата API"""
        if not traffic_data:
            return None

        return UserTraffic(
            used_traffic_bytes=int(traffic_data.get('usedTrafficBytes', 0)),
            lifetime_used_traffic_bytes=int(traffic_data.get('lifetimeUsedTrafficBytes', 0)),
            online_at=self._parse_optional_datetime(traffic_data.get('onlineAt')),
            first_connected_at=self._parse_optional_datetime(traffic_data.get('firstConnectedAt')),
            last_connected_node_uuid=traffic_data.get('lastConnectedNodeUuid'),
        )

    def _parse_user(self, user_data: dict) -> RemnaWaveUser:
        happ_data = user_data.get('happ') or {}
        happ_link = happ_data.get('link') or happ_data.get('url')
        happ_crypto_link = happ_data.get('cryptoLink') or happ_data.get('crypto_link')

        # Парсим userTraffic из нового формата API
        user_traffic = self._parse_user_traffic(user_data.get('userTraffic'))

        # Получаем status с fallback на ACTIVE
        status_str = user_data.get('status') or 'ACTIVE'
        try:
            status = UserStatus(status_str)
        except ValueError:
            logger.warning('Неизвестный статус пользователя: используем ACTIVE', status_str=status_str)
            status = UserStatus.ACTIVE

        # Получаем trafficLimitStrategy с fallback
        strategy_str = user_data.get('trafficLimitStrategy') or 'NO_RESET'
        try:
            traffic_strategy = TrafficLimitStrategy(strategy_str)
        except ValueError:
            logger.warning('Неизвестная стратегия трафика: используем NO_RESET', strategy_str=strategy_str)
            traffic_strategy = TrafficLimitStrategy.NO_RESET

        return RemnaWaveUser(
            id=int(user_data['id']),
            short_uuid=user_data['shortUuid'],
            username=user_data['username'],
            status=status,
            # Remnawave 2.8.0 ослабил валидацию trafficLimitBytes (integer → number),
            # поэтому панель может вернуть float — приводим к int явно.
            traffic_limit_bytes=int(user_data.get('trafficLimitBytes') or 0),
            traffic_limit_strategy=traffic_strategy,
            expire_at=datetime.fromisoformat(user_data['expireAt'].replace('Z', '+00:00')),
            telegram_id=user_data.get('telegramId'),
            email=user_data.get('email'),
            hwid_device_limit=user_data.get('hwidDeviceLimit'),
            description=user_data.get('description'),
            tag=user_data.get('tag'),
            subscription_url=user_data.get('subscriptionUrl', ''),
            active_internal_squads=user_data.get('activeInternalSquads', []),
            created_at=datetime.fromisoformat(user_data['createdAt'].replace('Z', '+00:00')),
            updated_at=datetime.fromisoformat(user_data['updatedAt'].replace('Z', '+00:00')),
            user_traffic=user_traffic,
            sub_revoked_at=self._parse_optional_datetime(user_data.get('subRevokedAt')),
            last_traffic_reset_at=self._parse_optional_datetime(user_data.get('lastTrafficResetAt')),
            trojan_password=user_data.get('trojanPassword'),
            vless_uuid=user_data.get('vlessUuid'),
            ss_password=user_data.get('ssPassword'),
            last_triggered_threshold=user_data.get('lastTriggeredThreshold', 0),
            happ_link=happ_link,
            happ_crypto_link=happ_crypto_link,
            external_squad_uuid=user_data.get('externalSquadUuid'),
        )

    def _parse_optional_datetime(self, date_str: str | None) -> datetime | None:
        if date_str:
            return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        return None

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        """Safely convert a value to int, returning default on failure."""
        if value is None:
            return default
        try:
            return int(value)
        except (ValueError, TypeError):
            return default

    def _parse_inbound(self, inbound_data: dict) -> RemnaWaveInbound:
        """Парсит данные inbound"""
        return RemnaWaveInbound(
            uuid=inbound_data['uuid'],
            profile_uuid=inbound_data['profileUuid'],
            tag=inbound_data['tag'],
            type=inbound_data['type'],
            network=inbound_data.get('network'),
            security=inbound_data.get('security'),
            port=inbound_data.get('port'),
            raw_inbound=inbound_data.get('rawInbound'),
        )

    def _parse_internal_squad(self, squad_data: dict) -> RemnaWaveInternalSquad:
        info = squad_data.get('info', {})
        inbounds_raw = squad_data.get('inbounds', [])
        inbounds = [self._parse_inbound(ib) for ib in inbounds_raw] if inbounds_raw else []
        return RemnaWaveInternalSquad(
            uuid=squad_data['uuid'],
            name=squad_data['name'],
            members_count=info.get('membersCount', 0),
            inbounds_count=info.get('inboundsCount', 0),
            inbounds=inbounds,
            view_position=squad_data.get('viewPosition', 0),
            created_at=self._parse_optional_datetime(squad_data.get('createdAt')),
            updated_at=self._parse_optional_datetime(squad_data.get('updatedAt')),
        )

    def _parse_accessible_node(self, node_data: dict) -> RemnaWaveAccessibleNode:
        """Парсит данные доступной ноды для Internal Squad"""
        return RemnaWaveAccessibleNode(
            uuid=node_data['uuid'],
            node_name=node_data['nodeName'],
            country_code=node_data['countryCode'],
            config_profile_uuid=node_data['configProfileUuid'],
            config_profile_name=node_data['configProfileName'],
            active_inbounds=node_data.get('activeInbounds', []),
        )

    def _parse_node(self, node_data: dict) -> RemnaWaveNode:
        return RemnaWaveNode(
            uuid=node_data['uuid'],
            name=node_data['name'],
            address=node_data['address'],
            country_code=node_data.get('countryCode', ''),
            is_connected=node_data.get('isConnected', False),
            is_disabled=node_data.get('isDisabled', False),
            users_online=node_data.get('usersOnline', 0),
            # 2.8.0: trafficLimitBytes может прийти float (валидация ослаблена) —
            # приводим к int, но сохраняем None когда панель поле не вернула.
            traffic_used_bytes=(
                None if node_data.get('trafficUsedBytes') is None else self._safe_int(node_data.get('trafficUsedBytes'))
            ),
            traffic_limit_bytes=(
                None
                if node_data.get('trafficLimitBytes') is None
                else self._safe_int(node_data.get('trafficLimitBytes'))
            ),
            port=node_data.get('port'),
            is_connecting=node_data.get('isConnecting', False),
            view_position=node_data.get('viewPosition', 0),
            tags=node_data.get('tags', []),
            last_status_change=self._parse_optional_datetime(node_data.get('lastStatusChange')),
            last_status_message=node_data.get('lastStatusMessage'),
            xray_uptime=self._safe_int(node_data.get('xrayUptime')),
            is_traffic_tracking_active=node_data.get('isTrafficTrackingActive', False),
            traffic_reset_day=node_data.get('trafficResetDay'),
            notify_percent=node_data.get('notifyPercent'),
            consumption_multiplier=node_data.get('consumptionMultiplier', 1.0),
            created_at=self._parse_optional_datetime(node_data.get('createdAt')),
            updated_at=self._parse_optional_datetime(node_data.get('updatedAt')),
            provider_uuid=node_data.get('providerUuid'),
            provider_name=(node_data.get('provider') or {}).get('name'),
            provider_favicon=(node_data.get('provider') or {}).get('faviconLink'),
            versions=node_data.get('versions'),
            system=node_data.get('system'),
            active_plugin_uuid=node_data.get('activePluginUuid'),
            ips=node_data.get('ips') or [],
        )

    def _parse_subscription_info(self, data: dict) -> SubscriptionInfo:
        happ_data = data.get('happ') or {}
        happ_link = happ_data.get('link') or happ_data.get('url')
        happ_crypto_link = happ_data.get('cryptoLink') or happ_data.get('crypto_link')

        return SubscriptionInfo(
            is_found=data['isFound'],
            user=data.get('user'),
            links=data.get('links', []),
            ss_conf_links=data.get('ssConfLinks', {}),
            subscription_url=data.get('subscriptionUrl', ''),
            happ=data.get('happ'),
            happ_link=happ_link,
            happ_crypto_link=happ_crypto_link,
        )


def format_bytes(bytes_value: int) -> str:
    if bytes_value == 0:
        return '0 B'

    units = ['B', 'KB', 'MB', 'GB', 'TB']
    size = bytes_value
    unit_index = 0

    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1

    return f'{size:.1f} {units[unit_index]}'


def parse_bytes(size_str: str) -> int:
    size_str = size_str.strip()

    # Check longest suffixes first; support both IEC (GiB) and SI (GB) units
    units = [
        ('TiB', 1024**4),
        ('GiB', 1024**3),
        ('MiB', 1024**2),
        ('KiB', 1024),
        ('TB', 1024**4),
        ('GB', 1024**3),
        ('MB', 1024**2),
        ('KB', 1024),
        ('B', 1),
    ]

    for unit, multiplier in units:
        if size_str.endswith(unit) or size_str.upper().endswith(unit.upper()):
            try:
                value = float(size_str[: -len(unit)].strip())
                return int(value * multiplier)
            except ValueError:
                break

    return 0


async def test_api_connection(api: RemnaWaveAPI) -> bool:
    try:
        await api.get_system_stats()
        return True
    except Exception as e:
        logger.warning('API connection test failed', e=e)
        return False
