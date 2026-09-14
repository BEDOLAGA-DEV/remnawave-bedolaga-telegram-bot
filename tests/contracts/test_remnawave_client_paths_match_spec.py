"""Сторож: каждая ручка панели, которую зовёт клиент, существует в спецификации Remnawave.

Список ручек снят с OpenAPI «Remnawave API v3.4.3» (fixtures/remnawave_api_3_4_3_endpoints.json).
Удалённая или переименованная ручка иначе живёт в клиенте годами как мёртвый код и всплывает
только 404 у пользователя — как `/api/sub/outline/...` и тип клиента `singbox-legacy`.

Обновление панели: пересобрать фикстуру из нового api.json (метод + путь на строку) и
прогнать тест — он покажет, что из вызываемого исчезло.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / 'fixtures' / 'remnawave_api_3_4_3_endpoints.json'

# Файлы, которые сами собирают путь до панели. Всё остальное ходит через методы клиента.
CLIENT_FILES = (
    ROOT / 'app' / 'external' / 'remnawave_api.py',
    ROOT / 'app' / 'services' / 'remnawave_service.py',
)

# Ручки, удалённые из панели, но оставленные в клиенте осознанно — с причиной.
LEGACY_ALLOWED = {
    'POST /api/system/tools/happ/encrypt': 'удалён в 2.8.0; клиент запоминает 404 и больше не зовёт (fallback для 2.7.x)',
}

_MAKE_REQUEST = re.compile(
    r"""_make_request\(\s*['"](?P<method>GET|POST|PUT|PATCH|DELETE)['"]\s*,\s*f?['"](?P<path>/api/[^'"]+)['"]"""
)
_DIRECT_SESSION = re.compile(
    r"""session\.(?P<method>get|post|put|patch|delete)\(\s*f['"]\{self\.base_url\}(?P<path>/api/[^'"]+)['"]"""
)


def _normalize(method: str, path: str) -> str:
    """`GET /api/users/{panel_user_id}` и `GET /api/users/{userId}` — одна и та же ручка."""
    return f'{method.upper()} {re.sub(r"\{[^}]*\}", "{}", path)}'


def _spec_endpoints() -> set[str]:
    data = json.loads(FIXTURE.read_text(encoding='utf-8'))
    return {_normalize(*entry.split(' ', 1)) for entry in data['endpoints']}


def _called_endpoints() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for file in CLIENT_FILES:
        text = file.read_text(encoding='utf-8')
        for regex in (_MAKE_REQUEST, _DIRECT_SESSION):
            for match in regex.finditer(text):
                key = _normalize(match.group('method'), match.group('path'))
                found.setdefault(key, []).append(file.relative_to(ROOT).as_posix())
    return found


def test_client_calls_only_endpoints_that_exist_in_panel_spec() -> None:
    spec = _spec_endpoints()
    called = _called_endpoints()
    assert len(called) >= 60, f'регулярка перестала находить вызовы клиента: {len(called)}'

    unknown = {
        endpoint: files
        for endpoint, files in called.items()
        if endpoint not in spec and endpoint not in {_normalize(*k.split(' ', 1)) for k in LEGACY_ALLOWED}
    }
    assert not unknown, 'клиент зовёт ручки, которых нет в спецификации панели:\n' + '\n'.join(
        f'  {endpoint}  ({", ".join(sorted(set(files)))})' for endpoint, files in sorted(unknown.items())
    )


def test_legacy_allowlist_entries_are_really_absent_from_spec() -> None:
    """Если ручка из allowlist вернулась в спецификацию, запись устарела — убрать."""
    spec = _spec_endpoints()
    stale = [k for k in LEGACY_ALLOWED if _normalize(*k.split(' ', 1)) in spec]
    assert not stale, f'allowlist устарел: {stale}'
