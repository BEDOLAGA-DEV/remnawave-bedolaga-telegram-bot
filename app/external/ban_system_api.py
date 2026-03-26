"""
Ban System API Client.

Client for interacting with the NoZapretBan monitoring system.
"""

from typing import Any

import aiohttp
import structlog


logger = structlog.get_logger(__name__)


class BanSystemAPIError(Exception):
    """Ban System API error."""

    def __init__(self, message: str, status_code: int | None = None, response_data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.response_data = response_data
        super().__init__(self.message)


class BanSystemAPI:
    """HTTP client for Ban System API."""

    def __init__(self, base_url: str, api_token: str, timeout: int = 30):
        self.base_url = base_url.rstrip('/')
        self.api_token = api_token
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.session: aiohttp.ClientSession | None = None

    def _get_headers(self) -> dict[str, str]:
        """Get request headers with authorization."""
        return {
            'Authorization': f'Bearer {self.api_token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }

    async def __aenter__(self):
        """Async context manager entry."""
        self.session = aiohttp.ClientSession(timeout=self.timeout, headers=self._get_headers())
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self.session:
            await self.session.close()
            self.session = None

    async def _ensure_session(self):
        """Ensure session is created."""
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=self.timeout, headers=self._get_headers())

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        json_data: dict | None = None,
    ) -> Any:
        """Execute HTTP request."""
        await self._ensure_session()

        url = f'{self.base_url}{endpoint}'

        try:
            async with self.session.request(
                method=method,
                url=url,
                params=params,
                json=json_data,
            ) as response:
                response_text = await response.text()

                if response.status >= 400:
                    logger.error('Ban System API error', status=response.status, response_text=response_text)
                    raise BanSystemAPIError(
                        message=f'API error {response.status}: {response_text}',
                        status_code=response.status,
                        response_data={'error': response_text},
                    )

                if response_text:
                    try:
                        return await response.json()
                    except Exception:
                        return {'raw': response_text}
                return {}

        except aiohttp.ClientError as e:
            logger.error('Ban System API connection error', error=e)
            raise BanSystemAPIError(message=f'Connection error: {e!s}', status_code=None, response_data=None)
        except TimeoutError:
            logger.error('Ban System API request timeout')
            raise BanSystemAPIError(message='Request timeout', status_code=None, response_data=None)

    async def close(self):
        """Close the session."""
        if self.session:
            await self.session.close()
            self.session = None

    # === Stats ===

    async def get_stats(self) -> dict[str, Any]:
        """
        Get overall system statistics.

        GET /api/stats
        """
        return await self._request('GET', '/api/stats')

    async def get_stats_period(self, hours: int = 24) -> dict[str, Any]:
        """
        Get statistics for a specific period.

        GET /api/stats/period?hours={hours}
        """
        return await self._request('GET', '/api/stats/period', params={'hours': hours})

    # === Users ===

    async def get_users(
        self,
        offset: int = 0,
        limit: int = 50,
        status: str | None = None,
    ) -> dict[str, Any]:
        """
        Get list of users with pagination.

        GET /api/users

        Args:
            offset: Pagination offset
            limit: Number of users per page (max 100)
            status: Filter by status (over_limit, with_limit, unlimited)
        """
        params = {'offset': offset, 'limit': min(limit, 100)}
        if status:
            params['status'] = status
        return await self._request('GET', '/api/users', params=params)

    async def get_users_over_limit(self, limit: int = 50, window: bool = True) -> dict[str, Any]:
        """
        Get users who exceeded their device limit.

        GET /api/users/over-limit
        """
        return await self._request(
            'GET', '/api/users/over-limit', params={'limit': limit, 'window': str(window).lower()}
        )

    async def search_users(self, query: str) -> dict[str, Any]:
        """
        Search for a user.

        GET /api/users/search/{query}
        """
        return await self._request('GET', f'/api/users/search/{query}')

    async def get_user(self, email: str) -> dict[str, Any]:
        """
        Get detailed user information.

        GET /api/users/{email}
        """
        return await self._request('GET', f'/api/users/{email}')

    async def get_user_network(self, email: str) -> dict[str, Any]:
        """
        Get user network information (WiFi/Mobile detection).

        GET /api/users/{email}/network
        """
        return await self._request('GET', f'/api/users/{email}/network')

    # === Punishments (Bans) ===

    async def get_punishments(self) -> list[dict[str, Any]]:
        """
        Get list of active punishments (bans).

        GET /api/punishments
        """
        return await self._request('GET', '/api/punishments')

    async def enable_user(self, user_id: str) -> dict[str, Any]:
        """
        Enable (unban) a user.

        POST /api/punishments/{user_id}/enable
        """
        return await self._request('POST', f'/api/punishments/{user_id}/enable')

    async def ban_user(
        self,
        username: str,
        minutes: int = 30,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """
        Manually ban a user.

        POST /api/ban
        """
        params = {'username': username, 'minutes': minutes}
        if reason:
            params['reason'] = reason
        return await self._request('POST', '/api/ban', params=params)

    async def get_punishment_history(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """
        Get punishment history for a user.

        GET /api/history/{query}
        """
        return await self._request('GET', f'/api/history/{query}', params={'limit': limit})

    # === Nodes ===

    async def get_nodes(self, include_agent_stats: bool = True) -> list[dict[str, Any]]:
        """
        Get list of connected nodes.

        GET /api/nodes
        """
        return await self._request(
            'GET', '/api/nodes', params={'include_agent_stats': str(include_agent_stats).lower()}
        )

    # === Agents ===

    async def get_agents(
        self,
        search: str | None = None,
        health: str | None = None,
        status: str | None = None,
        sort_by: str = 'name',
        sort_order: str = 'asc',
    ) -> dict[str, Any]:
        """
        Get list of monitoring agents.

        GET /api/agents

        Args:
            search: Search query
            health: Filter by health (healthy, warning, critical)
            status: Filter by status (online, offline)
            sort_by: Sort by field (name, sent, dropped, health)
            sort_order: Sort order (asc, desc)
        """
        params = {'sort_by': sort_by, 'sort_order': sort_order}
        if search:
            params['search'] = search
        if health:
            params['health'] = health
        if status:
            params['status'] = status
        return await self._request('GET', '/api/agents', params=params)

    async def get_agents_summary(self) -> dict[str, Any]:
        """
        Get summary statistics for all agents.

        GET /api/agents/summary
        """
        return await self._request('GET', '/api/agents/summary')

    async def get_agent_history(
        self,
        node_name: str,
        hours: int = 24,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """
        Get agent statistics history.

        GET /api/agents/{node_name}/history
        """
        return await self._request('GET', f'/api/agents/{node_name}/history', params={'hours': hours, 'limit': limit})

    # === Traffic ===

    async def get_traffic(self) -> dict[str, Any]:
        """
        Get overall traffic statistics.

        GET /api/traffic
        """
        return await self._request('GET', '/api/traffic')

    async def get_traffic_top(self, limit: int = 20) -> list[dict[str, Any]]:
        """
        Get top users by traffic.

        GET /api/traffic/top
        """
        return await self._request('GET', '/api/traffic/top', params={'limit': limit})

    async def get_user_traffic(self, username: str) -> dict[str, Any]:
        """
        Get traffic information for a specific user.

        GET /api/traffic/user/{username}
        """
        return await self._request('GET', f'/api/traffic/user/{username}')

    async def get_traffic_violations(self, limit: int = 50) -> list[dict[str, Any]]:
        """
        Get list of traffic limit violations.

        GET /api/traffic/violations
        """
        return await self._request('GET', '/api/traffic/violations', params={'limit': limit})

    # === Health ===

    async def health_check(self) -> dict[str, Any]:
        """
        Check API health.

        GET /health
        """
        return await self._request('GET', '/health')

    async def health_detailed(self) -> dict[str, Any]:
        """
        Get detailed health information.

        GET /health/detailed
        """
        return await self._request('GET', '/health/detailed')

    # === Settings ===

    async def get_settings(self) -> dict[str, Any]:
        """
        Get all settings with their definitions.

        GET /api/settings
        """
        return await self._request('GET', '/api/settings')

    async def get_setting(self, key: str) -> dict[str, Any]:
        """
        Get a specific setting value.

        GET /api/settings/{key}
        """
        return await self._request('GET', f'/api/settings/{key}')

    async def set_setting(self, key: str, value: Any) -> dict[str, Any]:
        """
        Set a setting value.

        POST /api/settings/{key}?value={value}
        """
        return await self._request('POST', f'/api/settings/{key}', params={'value': value})

    async def toggle_setting(self, key: str) -> dict[str, Any]:
        """
        Toggle a boolean setting.

        POST /api/settings/{key}/toggle
        """
        return await self._request('POST', f'/api/settings/{key}/toggle')

    async def whitelist_add(self, username: str) -> dict[str, Any]:
        """
        Add user to whitelist.

        POST /api/settings/whitelist/add?username={username}
        """
        return await self._request('POST', '/api/settings/whitelist/add', params={'username': username})

    async def whitelist_remove(self, username: str) -> dict[str, Any]:
        """
        Remove user from whitelist.

        POST /api/settings/whitelist/remove?username={username}
        """
        return await self._request('POST', '/api/settings/whitelist/remove', params={'username': username})
