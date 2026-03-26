from __future__ import annotations

import re
from dataclasses import dataclass

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


@dataclass
class ServerStatusEntry:
    address: str
    instance: str | None
    protocol: str | None
    name: str
    flag: str
    display_name: str
    latency_ms: int | None
    is_online: bool


class ServerStatusError(Exception):
    """Raised when server status information cannot be fetched or parsed."""


class ServerStatusService:
    _LATENCY_PATTERN = re.compile(r'xray_proxy_latency_ms\{(?P<labels>[^}]*)\}\s+(?P<value>[-+]?\d+(?:\.\d+)?)')
    _STATUS_PATTERN = re.compile(r'xray_proxy_status\{(?P<labels>[^}]*)\}\s+(?P<value>[-+]?\d+(?:\.\d+)?)')
    _LABEL_PATTERN = re.compile(r'(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)=\"(?P<value>(?:\\.|[^\"])*)\"')
    _FLAG_PATTERN = None  # Flag/country extraction disabled

    def __init__(self) -> None:
        pass

    async def get_servers(self) -> list[ServerStatusEntry]:
        mode = settings.get_server_status_mode()
        if mode != 'xray':
            raise ServerStatusError('Server status integration is not enabled')

        url = settings.get_server_status_metrics_url()
        if not url:
            raise ServerStatusError('Metrics URL is not configured')

        timeout = aiohttp.ClientTimeout(total=settings.get_server_status_request_timeout())
        auth = None
        auth_credentials = settings.get_server_status_metrics_auth()
        if auth_credentials:
            username, password = auth_credentials
            auth = aiohttp.BasicAuth(username, password)

        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.get(
                    url,
                    auth=auth,
                    ssl=settings.SERVER_STATUS_METRICS_VERIFY_SSL,
                ) as response,
            ):
                if response.status != 200:
                    text = await response.text()
                    raise ServerStatusError(f'Unexpected response status: {response.status} - {text[:200]}')
                metrics_body = await response.text()
        except TimeoutError as error:
            raise ServerStatusError('Request to metrics endpoint timed out') from error
        except aiohttp.ClientError as error:
            raise ServerStatusError('Failed to fetch metrics') from error

        return self._parse_metrics(metrics_body)

    def _parse_metrics(self, body: str) -> list[ServerStatusEntry]:
        servers: dict[tuple[str, str, str, str], ServerStatusEntry] = {}

        for match in self._LATENCY_PATTERN.finditer(body):
            labels = self._parse_labels(match.group('labels'))
            key = self._build_key(labels)
            entry = servers.get(key)
            if not entry:
                entry = self._create_entry(labels)
                servers[key] = entry

            try:
                value = float(match.group('value'))
                entry.latency_ms = int(round(value))
            except (TypeError, ValueError):
                entry.latency_ms = None

        for match in self._STATUS_PATTERN.finditer(body):
            labels = self._parse_labels(match.group('labels'))
            key = self._build_key(labels)
            entry = servers.get(key)
            if not entry:
                entry = self._create_entry(labels)
                servers[key] = entry

            try:
                value = float(match.group('value'))
                entry.is_online = value >= 1
            except (TypeError, ValueError):
                entry.is_online = False

        return sorted(
            servers.values(),
            key=lambda item: (
                0 if item.is_online else 1,
                (item.display_name or item.name).lower(),
            ),
        )

    def _build_key(self, labels: dict[str, str]) -> tuple[str, str, str, str]:
        return (
            labels.get('address', ''),
            labels.get('instance', ''),
            labels.get('protocol', ''),
            labels.get('name', labels.get('address', '')),
        )

    def _create_entry(self, labels: dict[str, str]) -> ServerStatusEntry:
        name = labels.get('name') or labels.get('address') or 'Unknown'
        flag, display_name = self._extract_flag(name)
        return ServerStatusEntry(
            address=labels.get('address', ''),
            instance=labels.get('instance'),
            protocol=labels.get('protocol'),
            name=name,
            flag=flag,
            display_name=display_name or name,
            latency_ms=None,
            is_online=False,
        )

    def _extract_flag(self, name: str) -> tuple[str, str]:
        # Country/flag display is disabled — return full name as-is
        return '', name

    def _parse_labels(self, labels_str: str) -> dict[str, str]:
        labels: dict[str, str] = {}
        for match in self._LABEL_PATTERN.finditer(labels_str):
            key = match.group('key')
            value = match.group('value').replace('\\"', '"')
            labels[key] = value
        return labels
