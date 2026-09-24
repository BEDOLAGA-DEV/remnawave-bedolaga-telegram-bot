"""Записанные ответы API DPI//CHECKER (см. tests/fixtures/dpichecker/README.md)."""

from __future__ import annotations

import json
from pathlib import Path


FIXTURES_DIR = Path(__file__).parent / 'dpichecker'


def load_dpichecker_fixture(name: str) -> dict:
    """{'status': int, 'body': ...} — ответ живого API после очистки."""
    return json.loads((FIXTURES_DIR / f'{name}.json').read_text(encoding='utf-8'))
