from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import structlog


logger = structlog.get_logger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_LOCALES_DIR = _PROJECT_ROOT / 'app' / 'localization' / 'locales'
_FALLBACK_LOCALES_DIR = _PROJECT_ROOT / 'locales'


def _normalize_key(key: Any) -> str:
    return str(key).strip().upper()


def _flatten(payload: dict[str, Any], prefix: str = '') -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_key, value in payload.items():
        key = _normalize_key(raw_key)
        full_key = f'{prefix}_{key}' if prefix else key
        if isinstance(value, dict):
            result.update(_flatten(value, full_key))
        elif isinstance(value, str):
            result[full_key] = value
        elif value is not None:
            result[full_key] = str(value)
    return result


def _read_json(path: Path) -> dict[str, str]:
    try:
        with path.open('r', encoding='utf-8') as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        logger.warning('Locale file unreadable', path=str(path), error=str(error))
        return {}
    if not isinstance(payload, dict):
        return {}
    return _flatten(payload)


def _read_yaml(path: Path) -> dict[str, str]:
    try:
        import yaml
    except ImportError:
        return {}
    try:
        with path.open('r', encoding='utf-8') as handle:
            payload = yaml.safe_load(handle)
    except (OSError, ValueError) as error:
        logger.warning('Locale file unreadable', path=str(path), error=str(error))
        return {}
    if not isinstance(payload, dict):
        return {}
    return _flatten(payload)


def _override_dirs() -> list[Path]:
    dirs: list[Path] = []
    raw = os.getenv('LOCALES_PATH', './locales').strip() or './locales'
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        for base in (Path.cwd(), _PROJECT_ROOT):
            dirs.append((base / candidate).resolve())
    else:
        dirs.append(candidate)
    dirs.append(_FALLBACK_LOCALES_DIR)

    unique: list[Path] = []
    seen: set[str] = set()
    for item in dirs:
        key = str(item)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def available_languages() -> list[str]:
    languages: set[str] = set()
    for directory in [_DEFAULT_LOCALES_DIR, *_override_dirs()]:
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            if entry.suffix.lower() in {'.json', '.yml', '.yaml'} and entry.stem:
                languages.add(entry.stem.strip().lower())
    return sorted(languages)


def load_locale(language: str) -> dict[str, str]:
    code = (language or 'ru').strip().lower() or 'ru'
    merged: dict[str, str] = {}

    default_file = _DEFAULT_LOCALES_DIR / f'{code}.json'
    if default_file.is_file():
        merged.update(_read_json(default_file))

    for directory in _override_dirs():
        if not directory.is_dir():
            continue
        json_file = directory / f'{code}.json'
        if json_file.is_file() and json_file != default_file:
            merged.update(_read_json(json_file))
        for suffix in ('.yml', '.yaml'):
            yaml_file = directory / f'{code}{suffix}'
            if yaml_file.is_file():
                merged.update(_read_yaml(yaml_file))

    return merged


def locales_available() -> bool:
    if _DEFAULT_LOCALES_DIR.is_dir():
        return True
    return any(directory.is_dir() for directory in _override_dirs())
