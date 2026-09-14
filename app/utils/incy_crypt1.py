"""Шифрованные deep links INCY — ``incy://crypt1/...``.

Аналог happ-cryptolink, только для INCY: без шифрования ссылка подписки уезжает
в открытом виде прямо в адресе кнопки (``incy://import/https://sub.example/<token>``),
и её видит всё, что читает ссылки по пути к пользователю.

Формат описан в документации INCY (https://docs.incy.cc/deep-links/) и реализован
в их официальном пакете ``@incy/link-encoder`` (MIT, (c) INCY LLC):

* AES-256-GCM;
* ссылка = ``incy://crypt1/`` + base64url(``iv[12] || ciphertext || tag[16]``);
* открытый текст — компактный JSON с отсортированными ключами:
  ``{"n":"<имя провайдера>","url":"<ссылка подписки>","v":1}``.

Ключ K1 одинаковый у всех провайдеров и вшит в клиенты INCY — это обфускация от
автоматических сканеров, а не криптография (так написано и в их документации).
Секрета он не содержит, поэтому лежит здесь константой; ``_KEY_FINGERPRINT`` —
опубликованный в пакете ``KEY_FINGERPRINT``, по нему сверяется, что константа не
разошлась с клиентами.

Любая осечка (сменился ключ, нет ``cryptography``, битые данные) означает возврат
исходной открытой ссылки: пользователь без подписки не остаётся.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import os

import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

# K1 из @incy/link-encoder 1.3.0: sha256("incy"+"deep"+"crypt1"+"v2026.06" + keymat).
_KEY_B64 = '9tQOoMioiZ18aC0Jug1BZd/is91F5rs+JcsjPPAMJGI='
# KEY_FINGERPRINT оттуда же — sha256(K1); расходится => пакет обновил ключ.
_KEY_FINGERPRINT = 'b6bf708471cc90043232967660aade86a50b4e57929db2e53c5fa34db624c08c'

INCY_CRYPT1_DEEP_LINK_PREFIX = 'incy://crypt1/'
_INCY_SCHEME = 'incy://'
_IV_LEN = 12
_PAYLOAD_VERSION = 1
_PROVIDER_NAME_MAX_LEN = 128
# Действия INCY, у которых хвост — ссылка подписки.
_IMPORT_ACTIONS = frozenset({'import', 'add'})


@functools.cache
def _get_key() -> bytes | None:
    """Возвращает K1, один раз сверив его отпечаток с опубликованным.

    Кешируется и отрицательный ответ: при разошедшемся отпечатке предупреждение
    пишется один раз за процесс, а не на каждую ссылку.
    """
    key = base64.b64decode(_KEY_B64)
    fingerprint = hashlib.sha256(key).hexdigest()
    if fingerprint != _KEY_FINGERPRINT:
        logger.warning(
            'Отпечаток ключа INCY не совпал — шифрование отключено',
            expected=_KEY_FINGERPRINT,
            got=fingerprint,
        )
        return None

    return key


def _build_plaintext(subscription_url: str, provider_name: str | None) -> bytes:
    """Собирает открытый текст так же, как ``sortedCompactJson`` у INCY."""
    payload: dict[str, object] = {'url': subscription_url, 'v': _PAYLOAD_VERSION}
    if provider_name:
        payload['n'] = provider_name[:_PROVIDER_NAME_MAX_LEN]
    parts = [
        f'{json.dumps(key)}:{json.dumps(payload[key], ensure_ascii=False, separators=(",", ":"))}'
        for key in sorted(payload)
    ]
    return ('{' + ','.join(parts) + '}').encode('utf-8')


def encrypt_incy_link(subscription_url: str, provider_name: str | None = None) -> str | None:
    """Шифрует ссылку подписки в ``incy://crypt1/...``.

    Возвращает ``None``, если зашифровать не удалось — вызывающий код в этом
    случае оставляет обычную ссылку.
    """
    if not subscription_url:
        return None

    key = _get_key()
    if key is None:
        return None

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        iv = os.urandom(_IV_LEN)
        sealed = AESGCM(key).encrypt(iv, _build_plaintext(subscription_url, provider_name), None)
        return INCY_CRYPT1_DEEP_LINK_PREFIX + base64.urlsafe_b64encode(iv + sealed).decode('ascii').rstrip('=')
    except Exception as e:
        logger.warning('Не удалось зашифровать ссылку INCY', error=e)
        return None


def wrap_incy_deep_link(link: str | None, subscription_url: str | None = None) -> str | None:
    """Подменяет ``incy://import|add/<url>`` на ``incy://crypt1/<зашифрованное>``.

    Ссылки других приложений, уже зашифрованные ссылки и незаполненные шаблоны
    возвращаются без изменений.
    """
    if not link or not settings.INCY_CRYPTOLINK_ENABLED:
        return link

    if not link.lower().startswith(_INCY_SCHEME) or '{{' in link:
        return link

    # incy://<действие>/<хвост>. Шифруется только импорт подписки: у служебных
    # ссылок (incy://connect, incy://routing/..., уже готовая incy://crypt1/...)
    # хвост не ссылка подписки, и подменять их на crypt1 нельзя — кнопка перестанет
    # делать то, ради чего её добавили в конфиг.
    action, _, tail = link[len(_INCY_SCHEME) :].partition('/')
    if action.lower() not in _IMPORT_ACTIONS:
        return link

    # Хвост может быть закодирован в base64 (isNeedBase64Encoding) — crypt1 ждёт
    # ссылку как есть, поэтому берём переданную ссылку подписки, а хвост нужен
    # только когда вызывающий её не передал.
    url = subscription_url or tail
    if not url.lower().startswith(('http://', 'https://')):
        return link

    return encrypt_incy_link(url, settings.get_incy_provider_name()) or link
