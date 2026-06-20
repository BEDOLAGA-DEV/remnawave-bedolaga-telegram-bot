"""The web_api /health liveness probe must be reachable WITHOUT an API token.

It previously required Security(require_api_token), so Docker/LB/monitoring/
cabinet healthchecks got 401. Liveness must be public; the detailed
database/pool endpoints stay token-gated.
"""

from __future__ import annotations

from app.webapi.dependencies import require_api_token
from app.webapi.routes.health import router


def _uses_token(path: str) -> bool:
    route = next(r for r in router.routes if getattr(r, 'path', None) == path)
    stack = [route.dependant]
    while stack:
        dep = stack.pop()
        if getattr(dep, 'call', None) is require_api_token:
            return True
        stack.extend(dep.dependencies)
    return False


def test_health_is_public() -> None:
    assert _uses_token('/health') is False, '/health must require no API token'


def test_detailed_health_stays_gated() -> None:
    assert _uses_token('/health/database') is True
    assert _uses_token('/metrics/pool') is True
