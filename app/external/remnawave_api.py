import asyncio
import base64
import json
import logging
import ssl
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any
from urllib.parse import urlparse

import aiohttp


logger = logging.getLogger(__name__)


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
    uuid: str
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
    sub_last_user_agent: str | None = None
    sub_last_opened_at: datetime | None = None
    sub_revoked_at: datetime | None = None
    last_traffic_reset_at: datetime | None = None
    trojan_password: str | None = None
    vless_uuid: str | None = None
    ss_password: str | None = None
    last_triggered_threshold: int = 0
    happ_link: str | None = None
    happ_crypto_link: str | None = None
    external_squad_uuid: str | None = None
    id: int | None = None

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
    users_online: int | None
    traffic_used_bytes: int | None
    traffic_limit_bytes: int | None
    port: int | None = None
    is_connecting: bool = False
    xray_version: str | None = None
    node_version: str | None = None
    view_position: int = 0
    tags: list[str] | None = None
    # Новые поля API
    last_status_change: datetime | None = None
    last_status_message: str | None = None
    xray_uptime: str | None = None
    is_traffic_tracking_active: bool = False
    traffic_reset_day: int | None = None
    notify_percent: int | None = None
    consumption_multiplier: float = 1.0
    cpu_count: int | None = None
    cpu_model: str | None = None
    total_ram: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    provider_uuid: str | None = None

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
    response_headers: dict[str, str] | None = None
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


class RemnaWaveAPI:
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

        # Caddy авторизация — добавляется поверх основной
        if self.caddy_token:
            # Caddy Security: готовый base64 токен используется как есть
            headers['Authorization'] = f'Basic {self.caddy_token}'
            logger.debug('Используем Caddy Basic Auth')

        # Основная авторизация RemnaWave API
        if self.auth_type == 'basic' and self.username and self.password:
            credentials = f'{self.username}:{self.password}'
            encoded_credentials = base64.b64encode(credentials.encode()).decode()
            headers['X-Api-Key'] = f'Basic {encoded_credentials}'
            logger.debug('Используем Basic Auth в X-Api-Key заголовке')
        elif self.auth_type == 'caddy':
            # Для caddy auth_type основная авторизация уже в Authorization header
            # Но API ключ всё равно нужен для RemnaWave
            if self.api_key:
                headers['X-Api-Key'] = self.api_key
                logger.debug('Используем API ключ для RemnaWave + Caddy авторизацию')
        else:
            # api_key или bearer — стандартный режим
            headers['X-Api-Key'] = self.api_key
            if not self.caddy_token:
                headers['Authorization'] = f'Bearer {self.api_key}'
            logger.debug('Используем API ключ в X-Api-Key заголовке')

        return headers

    async def __aenter__(self):
        conn_type = self._detect_connection_type()

        logger.debug(f'Подключение к Remnawave: {self.base_url} (тип: {conn_type})')

        headers = self._prepare_auth_headers()

        cookies = None
        if self.secret_key:
            if ':' in self.secret_key:
                key_name, key_value = self.secret_key.split(':', 1)
                cookies = {key_name: key_value}
                logger.debug(f'Используем куки: {key_name}=***')
            else:
                cookies = {self.secret_key: self.secret_key}
                logger.debug(f'Используем куки: {self.secret_key}=***')

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
            'timeout': aiohttp.ClientTimeout(total=60, connect=10),
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

                if data:
                    kwargs['json'] = data

                async with self.session.request(method, **kwargs) as response:
                    response_text = await response.text()

                    try:
                        response_data = json.loads(response_text) if response_text else {}
                    except json.JSONDecodeError:
                        response_data = {'raw_response': response_text}

                    if response.status == 429 and attempt < max_retries:
                        retry_after = float(response.headers.get('Retry-After', base_delay * (2**attempt)))
                        logger.warning(
                            'Rate limited (429) on %s %s, retry %d/%d after %.1fs',
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
                        logger.error(f'API Error {response.status}: {error_message}')
                        logger.error(f'Response: {response_text[:500]}')
                        raise RemnaWaveAPIError(error_message, response.status, response_data)

                    return response_data

            except aiohttp.ClientError as e:
                if attempt < max_retries:
                    delay = base_delay * (2**attempt)
                    logger.warning(
                        'Request failed on %s %s: %s, retry %d/%d after %.1fs',
                        method,
                        endpoint,
                        e,
                        attempt + 1,
                        max_retries,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.error(f'Request failed: {e}')
                raise RemnaWaveAPIError(f'Request failed: {e!s}')

        raise RemnaWaveAPIError(f'Max retries exceeded for {method} {endpoint}')

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
    ) -> RemnaWaveUser:
        data = {
            'username': username,
            'status': status.value,
            'expireAt': expire_at.isoformat(),
            'trafficLimitBytes': traffic_limit_bytes,
            'trafficLimitStrategy': traffic_limit_strategy.value,
        }

        if telegram_id:
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

        logger.debug('Создание пользователя в панели: %s', data)
        response = await self._make_request('POST', '/api/users', data)
        user = self._parse_user(response['response'])
        return await self.enrich_user_with_happ_link(user)

    async def get_user_by_uuid(self, uuid: str) -> RemnaWaveUser | None:
        try:
            response = await self._make_request('GET', f'/api/users/{uuid}')
            user = self._parse_user(response['response'])
            return await self.enrich_user_with_happ_link(user)
        except RemnaWaveAPIError as e:
            if e.status_code == 404:
                return None
            raise

    async def get_user_by_telegram_id(self, telegram_id: int) -> list[RemnaWaveUser]:
        try:
            response = await self._make_request('GET', f'/api/users/by-telegram-id/{telegram_id}')
            users_data = response.get('response', [])
            if not users_data:
                return []
            users = [self._parse_user(user) for user in users_data]
            return [await self.enrich_user_with_happ_link(u) for u in users]
        except RemnaWaveAPIError as e:
            if e.status_code == 404:
                return []
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

    async def get_user_by_email(self, email: str) -> list[RemnaWaveUser]:
        """Get users by email address."""
        try:
            response = await self._make_request('GET', f'/api/users/by-email/{email}')
            users_data = response.get('response', [])
            if not users_data:
                return []
            # Handle both single object and array responses
            if isinstance(users_data, dict):
                users_data = [users_data]
            users = [self._parse_user(user) for user in users_data]
            return [await self.enrich_user_with_happ_link(u) for u in users]
        except RemnaWaveAPIError as e:
            if e.status_code == 404:
                return []
            raise

    async def update_user(
        self,
        uuid: str,
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
    ) -> RemnaWaveUser:
        data = {'uuid': uuid}

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

        response = await self._make_request('PATCH', '/api/users', data)
        user = self._parse_user(response['response'])
        return await self.enrich_user_with_happ_link(user)

    async def delete_user(self, uuid: str) -> bool:
        response = await self._make_request('DELETE', f'/api/users/{uuid}')
        return response['response']['isDeleted']

    async def enable_user(self, uuid: str) -> RemnaWaveUser:
        response = await self._make_request('POST', f'/api/users/{uuid}/actions/enable')
        user = self._parse_user(response['response'])
        return await self.enrich_user_with_happ_link(user)

    async def disable_user(self, uuid: str) -> RemnaWaveUser:
        response = await self._make_request('POST', f'/api/users/{uuid}/actions/disable')
        user = self._parse_user(response['response'])
        return await self.enrich_user_with_happ_link(user)

    async def reset_user_traffic(self, uuid: str) -> RemnaWaveUser:
        response = await self._make_request('POST', f'/api/users/{uuid}/actions/reset-traffic')
        user = self._parse_user(response['response'])
        return await self.enrich_user_with_happ_link(user)

    async def revoke_user_subscription(
        self, uuid: str, new_short_uuid: str | None = None, revoke_only_passwords: bool = False
    ) -> RemnaWaveUser:
        """
        Отзывает подписку пользователя (меняет ссылку/пароли).

        Args:
            uuid: UUID пользователя
            new_short_uuid: Новый короткий UUID (опционально, рекомендуется генерировать автоматически)
            revoke_only_passwords: Если True, меняются только пароли без изменения URL подписки
        """
        data = {}
        if new_short_uuid:
            data['shortUuid'] = new_short_uuid
        if revoke_only_passwords:
            data['revokeOnlyPasswords'] = True

        response = await self._make_request('POST', f'/api/users/{uuid}/actions/revoke', data)
        user = self._parse_user(response['response'])
        return await self.enrich_user_with_happ_link(user)

    async def get_user_accessible_nodes(self, uuid: str) -> list[RemnaWaveAccessibleNode]:
        """Получает список доступных нод для пользователя"""
        try:
            response = await self._make_request('GET', f'/api/users/{uuid}/accessible-nodes')
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
        response = await self._make_request('DELETE', f'/api/internal-squads/{uuid}')
        return response['response']['isDeleted']

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
        """Добавляет всех пользователей в Internal Squad (bulk action)"""
        response = await self._make_request('POST', f'/api/internal-squads/{uuid}/bulk-actions/add-users')
        return response['response']['eventSent']

    async def remove_users_from_internal_squad(self, uuid: str) -> bool:
        """Удаляет всех пользователей из Internal Squad (bulk action)"""
        response = await self._make_request('POST', f'/api/internal-squads/{uuid}/bulk-actions/remove-users')
        return response['response']['eventSent']

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
        response_headers: dict[str, str] | None = None,
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
        if response_headers is not None:
            data['responseHeaders'] = response_headers
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
        response = await self._make_request('DELETE', f'/api/external-squads/{uuid}')
        return response['response']['isDeleted']

    async def add_users_to_external_squad(self, uuid: str) -> bool:
        """Добавляет всех пользователей в External Squad (bulk action)"""
        response = await self._make_request('POST', f'/api/external-squads/{uuid}/bulk-actions/add-users')
        return response['response']['eventSent']

    async def remove_users_from_external_squad(self, uuid: str) -> bool:
        """Удаляет всех пользователей из External Squad (bulk action)"""
        response = await self._make_request('POST', f'/api/external-squads/{uuid}/bulk-actions/remove-users')
        return response['response']['eventSent']

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
            response_headers=squad_data.get('responseHeaders'),
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

    async def restart_node(self, uuid: str) -> bool:
        response = await self._make_request('POST', f'/api/nodes/{uuid}/actions/restart')
        return response['response']['eventSent']

    async def restart_all_nodes(self) -> bool:
        response = await self._make_request('POST', '/api/nodes/actions/restart-all')
        return response['response']['eventSent']

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

    async def get_system_stats(self) -> dict[str, Any]:
        response = await self._make_request('GET', '/api/system/stats')
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

    async def get_bandwidth_stats(self) -> dict[str, Any]:
        response = await self._make_request('GET', '/api/system/stats/bandwidth')
        return response['response']

    async def get_nodes_statistics(self) -> dict[str, Any]:
        response = await self._make_request('GET', '/api/system/stats/nodes')
        return response['response']

    async def get_nodes_realtime_usage(self) -> list[dict[str, Any]]:
        return await self.get_bandwidth_stats_nodes_realtime()

    async def get_user_stats_usage(self, user_uuid: str, start_date: str, end_date: str) -> dict[str, Any]:
        return await self.get_bandwidth_stats_user_legacy(user_uuid, start_date, end_date)

    # ============== Bandwidth Stats API ==============

    async def get_bandwidth_stats_nodes(self, start_date: str, end_date: str) -> dict[str, Any]:
        params = {'start': start_date, 'end': end_date}
        response = await self._make_request('GET', '/api/bandwidth-stats/nodes', params=params)
        return response['response']

    async def get_bandwidth_stats_nodes_realtime(self) -> list[dict[str, Any]]:
        response = await self._make_request('GET', '/api/bandwidth-stats/nodes/realtime')
        return response['response']

    async def get_bandwidth_stats_node_users(
        self, node_uuid: str, start_date: str, end_date: str, top_users_limit: int = 10
    ) -> dict[str, Any]:
        params = {'start': start_date, 'end': end_date, 'topUsersLimit': top_users_limit}
        response = await self._make_request('GET', f'/api/bandwidth-stats/nodes/{node_uuid}/users', params=params)
        return response['response']

    async def get_bandwidth_stats_node_users_legacy(
        self, node_uuid: str, start_date: str, end_date: str
    ) -> dict[str, Any]:
        params = {'start': start_date, 'end': end_date}
        response = await self._make_request(
            'GET', f'/api/bandwidth-stats/nodes/{node_uuid}/users/legacy', params=params
        )
        return response['response']

    async def get_bandwidth_stats_user(self, user_uuid: str, start_date: str, end_date: str) -> dict[str, Any]:
        params = {'start': start_date, 'end': end_date}
        response = await self._make_request('GET', f'/api/bandwidth-stats/users/{user_uuid}', params=params)
        return response['response']

    async def get_bandwidth_stats_user_legacy(self, user_uuid: str, start_date: str, end_date: str) -> dict[str, Any]:
        params = {'start': start_date, 'end': end_date}
        response = await self._make_request('GET', f'/api/bandwidth-stats/users/{user_uuid}/legacy', params=params)
        return response

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
        response = await self._make_request('DELETE', f'/api/subscription-page-configs/{uuid}')
        return response['response']['isDeleted']

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

    async def get_user_devices(self, user_uuid: str) -> dict[str, Any]:
        try:
            response = await self._make_request('GET', f'/api/hwid/devices/{user_uuid}')
            return response['response']
        except RemnaWaveAPIError as e:
            if e.status_code == 404:
                return {'total': 0, 'devices': []}
            raise

    async def reset_user_devices(self, user_uuid: str) -> bool:
        try:
            devices_info = await self.get_user_devices(user_uuid)
            devices = devices_info.get('devices', [])

            if not devices:
                return True

            failed_count = 0
            for device in devices:
                device_hwid = device.get('hwid')
                if device_hwid:
                    try:
                        delete_data = {'userUuid': user_uuid, 'hwid': device_hwid}
                        await self._make_request('POST', '/api/hwid/devices/delete', data=delete_data)
                    except Exception as device_error:
                        logger.error(f'Ошибка удаления устройства {device_hwid}: {device_error}')
                        failed_count += 1

            return failed_count < len(devices) / 2

        except Exception as e:
            logger.error(f'Ошибка при сбросе устройств: {e}')
            return False

    async def remove_device(self, user_uuid: str, device_hwid: str) -> bool:
        try:
            delete_data = {'userUuid': user_uuid, 'hwid': device_hwid}
            await self._make_request('POST', '/api/hwid/devices/delete', data=delete_data)
            return True
        except Exception as e:
            logger.error(f'Ошибка удаления устройства {device_hwid}: {e}')
            return False

    async def encrypt_happ_crypto_link(self, link_to_encrypt: str) -> str | None:
        try:
            data = {'linkToEncrypt': link_to_encrypt}
            response = await self._make_request('POST', '/api/system/tools/happ/encrypt', data)
            return response.get('response', {}).get('encryptedLink')
        except RemnaWaveAPIError as e:
            logger.warning(f'Не удалось зашифровать happ ссылку: {e.message}')
            return None
        except Exception as e:
            logger.warning(f'Ошибка при шифровании happ ссылки: {e}')
            return None

    async def enrich_user_with_happ_link(self, user: RemnaWaveUser) -> RemnaWaveUser:
        if not user.happ_crypto_link and user.subscription_url:
            encrypted = await self.encrypt_happ_crypto_link(user.subscription_url)
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
            logger.warning(f'Неизвестный статус пользователя: {status_str}, используем ACTIVE')
            status = UserStatus.ACTIVE

        # Получаем trafficLimitStrategy с fallback
        strategy_str = user_data.get('trafficLimitStrategy') or 'NO_RESET'
        try:
            traffic_strategy = TrafficLimitStrategy(strategy_str)
        except ValueError:
            logger.warning(f'Неизвестная стратегия трафика: {strategy_str}, используем NO_RESET')
            traffic_strategy = TrafficLimitStrategy.NO_RESET

        return RemnaWaveUser(
            uuid=user_data['uuid'],
            short_uuid=user_data['shortUuid'],
            username=user_data['username'],
            status=status,
            traffic_limit_bytes=user_data.get('trafficLimitBytes', 0),
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
            sub_last_user_agent=user_data.get('subLastUserAgent'),
            sub_last_opened_at=self._parse_optional_datetime(user_data.get('subLastOpenedAt')),
            sub_revoked_at=self._parse_optional_datetime(user_data.get('subRevokedAt')),
            last_traffic_reset_at=self._parse_optional_datetime(user_data.get('lastTrafficResetAt')),
            trojan_password=user_data.get('trojanPassword'),
            vless_uuid=user_data.get('vlessUuid'),
            ss_password=user_data.get('ssPassword'),
            last_triggered_threshold=user_data.get('lastTriggeredThreshold', 0),
            happ_link=happ_link,
            happ_crypto_link=happ_crypto_link,
            external_squad_uuid=user_data.get('externalSquadUuid'),
            id=user_data.get('id'),
        )

    def _parse_optional_datetime(self, date_str: str | None) -> datetime | None:
        if date_str:
            return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        return None

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
            users_online=node_data.get('usersOnline'),
            traffic_used_bytes=node_data.get('trafficUsedBytes'),
            traffic_limit_bytes=node_data.get('trafficLimitBytes'),
            port=node_data.get('port'),
            is_connecting=node_data.get('isConnecting', False),
            xray_version=node_data.get('xrayVersion'),
            node_version=node_data.get('nodeVersion'),
            view_position=node_data.get('viewPosition', 0),
            tags=node_data.get('tags', []),
            # Новые поля API
            last_status_change=self._parse_optional_datetime(node_data.get('lastStatusChange')),
            last_status_message=node_data.get('lastStatusMessage'),
            xray_uptime=node_data.get('xrayUptime'),
            is_traffic_tracking_active=node_data.get('isTrafficTrackingActive', False),
            traffic_reset_day=node_data.get('trafficResetDay'),
            notify_percent=node_data.get('notifyPercent'),
            consumption_multiplier=node_data.get('consumptionMultiplier', 1.0),
            cpu_count=node_data.get('cpuCount'),
            cpu_model=node_data.get('cpuModel'),
            total_ram=node_data.get('totalRam'),
            created_at=self._parse_optional_datetime(node_data.get('createdAt')),
            updated_at=self._parse_optional_datetime(node_data.get('updatedAt')),
            provider_uuid=node_data.get('providerUuid'),
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
    size_str = size_str.upper().strip()

    units = {'B': 1, 'KB': 1024, 'MB': 1024**2, 'GB': 1024**3, 'TB': 1024**4}

    for unit, multiplier in units.items():
        if size_str.endswith(unit):
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
        logger.error(f'API connection test failed: {e}')
        return False
