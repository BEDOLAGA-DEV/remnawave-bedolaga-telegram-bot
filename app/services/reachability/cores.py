"""Ядра Xray у bschekbot: ключ параметра ``core`` → номер версии.

Оригинал bsbord.com показывает ядро цифрами («26.3.27», «26.7.11»), а API оперирует словами
``stable`` / ``prerelease``. Версии названы только в описании параметра ``core`` в OpenAPI
bschekbot — живой страж ``tests/live/test_bschek_live.py`` сверяет константу с ним.
"""

from __future__ import annotations


XRAY_CORES: dict[str, str] = {'stable': '26.3.27', 'prerelease': '26.7.11'}
