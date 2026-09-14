"""Причины по подписке словами: срок, трафик, статус пользователя панели, заглушки.

Владелец: «внятные ошибки, чтобы человек понимал: лимит устройств, истекла, отключена».
Откуда берутся факты (контракт Remnawave 3.4.3, ``api-1.json``):

* заголовок ``subscription-userinfo`` публичной подписки — ``upload``, ``download``,
  ``total``, ``expire`` (unix); панель шлёт его и вместе с заглушкой;
* пользователь панели (``GET /api/users/by-short-uuid``) — статус EXPIRED / DISABLED /
  LIMITED, дата и трафик — для подписок своей панели;
* заглушки вместо серверов — ссылки на ``0.0.0.0:1`` с текстом оператора в ремарке
  (``customRemarks``: expiredUsers, limitedUsers, disabledUsers, emptyHosts,
  HWIDMaxDevicesExceeded, HWIDNotSupported) — текст показываем как есть.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote, urlsplit


_GB = 1024**3


@dataclass(frozen=True)
class SubscriptionUserinfo:
    """Заголовок ``subscription-userinfo`` в числах."""

    upload: int = 0
    download: int = 0
    total: int = 0
    expire: datetime | None = None

    @property
    def used(self) -> int:
        return self.upload + self.download

    @property
    def exhausted(self) -> bool:
        return self.total > 0 and self.used >= self.total


def parse_userinfo(header: str | None) -> SubscriptionUserinfo | None:
    """``upload=0; download=1; total=2; expire=1700000000`` → числа; мусор — ``None``."""
    if not header:
        return None
    values: dict[str, int] = {}
    for part in header.split(';'):
        key, _, value = part.strip().partition('=')
        value = value.strip()
        if key and value.lstrip('-').isdigit():
            values[key.strip().lower()] = int(value)
    if not values:
        return None
    expire = values.get('expire') or 0
    return SubscriptionUserinfo(
        upload=values.get('upload', 0),
        download=values.get('download', 0),
        total=values.get('total', 0),
        expire=datetime.fromtimestamp(expire, tz=UTC) if expire > 0 else None,
    )


def format_gb(value: int) -> str:
    gb = value / _GB
    text = f'{gb:.0f}' if gb >= 10 or gb == int(gb) else f'{gb:.1f}'
    return text


def _as_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def expired_text(expire: datetime) -> str:
    return f'Подписка истекла {_as_utc(expire).astimezone(UTC):%d.%m.%Y}'


def exhausted_text(used: int, total: int) -> str:
    return f'Трафик подписки исчерпан: {format_gb(used)} из {format_gb(total)} ГБ'


def note_for_userinfo(info: SubscriptionUserinfo | None, *, now: datetime | None = None) -> str | None:
    """Что не так с подпиской по её заголовку; ``None`` — всё в порядке."""
    if info is None:
        return None
    moment = now or datetime.now(UTC)
    if info.expire is not None and info.expire <= moment:
        return expired_text(info.expire)
    if info.exhausted:
        return exhausted_text(info.used, info.total)
    return None


def note_for_panel_user(user: Any, *, now: datetime | None = None) -> str | None:
    """Что не так с пользователем своей панели по его статусу; ``None`` — активен или неизвестен."""
    if user is None:
        return None
    raw_status = getattr(user, 'status', None)
    status = str(getattr(raw_status, 'value', raw_status) or '').upper()
    if status == 'EXPIRED':
        expire_at = getattr(user, 'expire_at', None)
        return expired_text(expire_at) if isinstance(expire_at, datetime) else 'Подписка истекла'
    if status == 'DISABLED':
        return 'Подписка отключена в панели'
    if status == 'LIMITED':
        used = int(getattr(user, 'used_traffic_bytes', 0) or 0)
        total = int(getattr(user, 'traffic_limit_bytes', 0) or 0)
        return exhausted_text(used, total) if total else 'Трафик подписки исчерпан'
    return None


def stub_remarks(links: list[str]) -> list[str]:
    """Тексты заглушек из ремарок ссылок (после ``#``), без повторов, в порядке появления."""
    remarks: list[str] = []
    for link in links:
        text = unquote(urlsplit(link).fragment).strip()
        if text and text not in remarks:
            remarks.append(text)
    return remarks


def explain_missing_configs(
    *,
    info: SubscriptionUserinfo | None,
    stubs: list[str],
    body: str,
    device_retry: bool,
    now: datetime | None = None,
) -> str:
    """Почему в ответе нет ни одного сервера — словами для админа."""
    note = note_for_userinfo(info, now=now)
    if note:
        return note
    if stubs:
        quoted = ', '.join(f'«{remark}»' for remark in stubs[:3])
        text = f'Панель отдала вместо серверов заглушку: {quoted}'
        if device_retry:
            text += (
                ' — даже на запрос с привязкой устройства: похоже, у подписки исчерпан лимит устройств'
                ' или панель не пускает наше приложение'
            )
        return text
    if body.lstrip().lower().startswith(('<!doctype', '<html', '<')):
        return 'По этому адресу страница, а не подписка'
    return 'В подписке нет ни одного сервера'
