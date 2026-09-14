"""Записанные ответы bschekbot API (см. tests/fixtures/bschek/README.md)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path


FIXTURES_DIR = Path(__file__).parent / 'bschek'


def load_bschek_fixture(name: str) -> dict:
    """Возвращает фикстуру целиком: status, headers, request, idempotency_key, body."""
    return json.loads((FIXTURES_DIR / f'{name}.json').read_text(encoding='utf-8'))


def iter_bschek_fixtures() -> Iterator[tuple[str, dict]]:
    for path in sorted(FIXTURES_DIR.glob('*.json')):
        yield path.stem, json.loads(path.read_text(encoding='utf-8'))
