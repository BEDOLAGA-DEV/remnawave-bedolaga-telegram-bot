from __future__ import annotations

import time

import structlog

from app.config import settings
from app.services.remnawave_service import RemnaWaveService
from app.services.speedtest_settings_service import SpeedtestSettingsService, _sanitize_host


logger = structlog.get_logger(__name__)

_CACHE_TTL_SECONDS = 60


class SpeedtestService:
    def __init__(self) -> None:
        self._remnawave = RemnaWaveService()
        self._nodes_cache: list[dict] | None = None
        self._nodes_cache_at: float | None = None

    async def _get_nodes_cached(self) -> list[dict]:
        now = time.monotonic()
        if (
            self._nodes_cache is not None
            and self._nodes_cache_at is not None
            and (now - self._nodes_cache_at) < _CACHE_TTL_SECONDS
        ):
            return self._nodes_cache
        nodes = await self._remnawave.get_all_nodes()
        self._nodes_cache = nodes
        self._nodes_cache_at = now
        return nodes

    def _resolve_ping_host(self, node: dict, mapping: dict) -> str | None:
        host = mapping.get(node.get('uuid'))
        if host:
            return host
        template = settings.SPEEDTEST_PING_HOST_TEMPLATE
        if template:
            try:
                raw = template.format(
                    node_name=node.get('name', ''),
                    country_code=node.get('country_code', ''),
                )
            except Exception:
                return None
            # Sanitize: a hostile/typo node name must not inject a bad host that
            # the frontend would fetch() (the mapping path is sanitized at write
            # time; the template path is sanitized here).
            return _sanitize_host(raw)
        return None

    async def get_ping_targets(self) -> list[dict]:
        nodes = await self._get_nodes_cached()
        mapping = SpeedtestSettingsService.get_host_mapping()
        name_mapping = SpeedtestSettingsService.get_name_mapping()
        targets = []
        for node in nodes:
            if node.get('is_disabled'):
                continue  # admin took this node out of service — don't offer it
            ping_host = self._resolve_ping_host(node, mapping)
            if not ping_host:
                continue
            # Custom display name overrides the panel node name; the flag is
            # built by the frontend from country_code, which stays untouched.
            display_name = name_mapping.get(node.get('uuid')) or node.get('name', '')
            targets.append({
                'name': display_name,
                'country_code': node.get('country_code'),
                'ping_host': ping_host,
                'is_online': bool(node.get('is_node_online', node.get('is_connected', False))),
                'users_online': node.get('users_online', 0),
            })
        targets.sort(key=lambda t: ((t['country_code'] or ''), t['name']))
        return targets


speedtest_service = SpeedtestService()
