"""Текст админ-уведомления о прогоне монитора DPI//CHECKER (HTML для Telegram)."""

from __future__ import annotations

from html import escape
from typing import Any


COUNTRIES = {'russia': 'Россия', 'china': 'Китай', 'iran': 'Иран', 'turkmenistan': 'Туркменистан'}
MAX_RESOURCES_IN_TEXT = 10
PLACEHOLDER_CABINET = 'example.com'


def _cabinet_link(cabinet_url: str | None) -> str | None:
    if not cabinet_url or PLACEHOLDER_CABINET in cabinet_url:
        return None
    return f'{cabinet_url.rstrip("/")}/admin/dpichecker?tab=monitors'


def monitor_run_text(action: Any, monitor: dict[str, Any], view: dict[str, Any], *, cabinet_url: str | None) -> str:
    fails = int(monitor.get('consecutive_fails') or 0)
    healthy = fails == 0
    head = '🟢' if healthy else '🔴'
    country = COUNTRIES.get(
        str(monitor.get('location') or view.get('location') or ''), str(monitor.get('location') or '')
    )
    lines = [f'{head} <b>DPI//CHECKER</b> · монитор «{escape(action.label or "")}»', f'Откуда: {escape(country)}']
    for res in view.get('resources', [])[:MAX_RESOURCES_IN_TEXT]:
        mark = '✅' if res['total'] and res['ok_count'] == res['total'] else ('❌' if res['ok_count'] == 0 else '⚠️')
        lines.append(f'{mark} {escape(res["name"])}: {res["ok_count"]} из {res["total"]} точек доступно')
    hidden = len(view.get('resources', [])) - MAX_RESOURCES_IN_TEXT
    if hidden > 0:
        lines.append(f'…и ещё {hidden}')
    if not healthy:
        lines.append(f'Тревога: неудач подряд: {fails}')
    link = _cabinet_link(cabinet_url)
    if link:
        lines.append(f'<a href="{escape(link)}">Открыть мониторы в кабинете</a>')
    return '\n'.join(lines)
