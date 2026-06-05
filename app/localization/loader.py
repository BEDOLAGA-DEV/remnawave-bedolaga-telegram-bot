from __future__ import annotations

import json
import os
import shutil
import tempfile
from functools import cache
from pathlib import Path
from typing import Any

import structlog

from app.config import settings


_logger = structlog.get_logger(__name__)

_FALLBACK_LANGUAGE = 'ru'

_BASE_DIR = Path(__file__).resolve().parent
_DEFAULT_LOCALES_DIR = _BASE_DIR / 'locales'


def _normalize_language_code(value: Any) -> str:
    if isinstance(value, str):
        return value.strip().lower()
    if value is None:
        return ''
    return str(value).strip().lower()


def _resolve_user_locales_dir() -> Path:
    path = Path(settings.LOCALES_PATH).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _locale_file_exists(language: str) -> bool:
    code = _normalize_language_code(language)
    if not code:
        return False

    default_candidate = _DEFAULT_LOCALES_DIR / f'{code}.json'
    if default_candidate.exists():
        return True

    user_dir = _resolve_user_locales_dir()
    return any((user_dir / f'{code}{extension}').exists() for extension in ('.json', '.yml', '.yaml'))


def _select_fallback_language(available_map: dict[str, str]) -> str:
    candidates = []
    if _FALLBACK_LANGUAGE:
        candidates.append(_FALLBACK_LANGUAGE)
    candidates.extend(available_map.values())

    seen = set()
    for candidate in candidates:
        normalized = _normalize_language_code(candidate)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)

        if normalized in available_map:
            return available_map[normalized]

        if _locale_file_exists(normalized):
            return normalized

    if _FALLBACK_LANGUAGE and _locale_file_exists(_FALLBACK_LANGUAGE):
        return _FALLBACK_LANGUAGE

    return _FALLBACK_LANGUAGE or 'ru'


def _determine_default_language() -> str:
    try:
        raw_default = settings.DEFAULT_LANGUAGE
    except AttributeError:
        raw_default = None

    configured = raw_default.strip() if isinstance(raw_default, str) else ''

    try:
        available_languages = settings.get_available_languages()
    except Exception as error:  # pragma: no cover - defensive logging
        _logger.warning('Failed to load available languages from settings', error=error)
        available_languages = []

    available_map = {
        _normalize_language_code(lang): lang.strip()
        for lang in available_languages
        if isinstance(lang, str) and lang.strip()
    }

    if configured:
        normalized_configured = _normalize_language_code(configured)

        if normalized_configured in available_map:
            return available_map[normalized_configured]

        if not available_map and _locale_file_exists(normalized_configured):
            return normalized_configured

        if _locale_file_exists(normalized_configured):
            _logger.warning(
                'Configured default language is not listed in AVAILABLE_LANGUAGES — falling back',
                configured=configured,
                FALLBACK_LANGUAGE=_FALLBACK_LANGUAGE,
            )
        else:
            _logger.warning(
                'Configured default language is not available — falling back',
                configured=configured,
                FALLBACK_LANGUAGE=_FALLBACK_LANGUAGE,
            )
    else:
        _logger.debug('DEFAULT_LANGUAGE is not set — falling back', FALLBACK_LANGUAGE=_FALLBACK_LANGUAGE)

    fallback_language = _select_fallback_language(available_map)

    if _normalize_language_code(fallback_language) != _normalize_language_code(_FALLBACK_LANGUAGE):
        _logger.warning(
            'Fallback language is not available — using a different fallback instead',
            FALLBACK_LANGUAGE=_FALLBACK_LANGUAGE,
            fallback_language=fallback_language,
        )

    return fallback_language or _FALLBACK_LANGUAGE


DEFAULT_LANGUAGE = _determine_default_language()


def _normalize_key(raw_key: Any) -> str:
    key = str(raw_key).strip().replace(' ', '_')
    return key.upper()


def _flatten_locale_dict(data: dict[str, Any], parent_key: str = '') -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, value in (data or {}).items():
        composite_key = _normalize_key(key)
        if parent_key:
            composite_key = f'{parent_key}_{composite_key}'

        if isinstance(value, dict):
            flattened.update(_flatten_locale_dict(value, composite_key))
        else:
            flattened[composite_key] = value
    return flattened


def _normalize_locale_dict(data: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in (data or {}).items():
        if isinstance(value, dict):
            normalized.update(_flatten_locale_dict(value, _normalize_key(key)))
        else:
            normalized[_normalize_key(key)] = value
    return normalized


def _directory_is_writable(directory: Path) -> bool:
    try:
        current_user = f'{os.geteuid()}:{os.getegid()}'
        user_hint = f' (running as UID:GID {current_user})'
    except Exception:  # pragma: no cover - best effort only
        user_hint = ''

    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix='.locale_write_test_', delete=True):
            pass
        return True
    except PermissionError as error:
        _logger.warning(
            'Locale directory is not writable. Ensure the mounted directory allows writes for the container user or configure LOCALES_PATH to a writable path.',
            directory=directory,
            user_hint=user_hint,
            error=error,
        )
    except OSError as error:
        _logger.warning(
            'Unable to prepare locale directory for writing. Configure LOCALES_PATH to a writable path.',
            directory=directory,
            user_hint=user_hint,
            error=error,
        )
    except Exception as error:  # pragma: no cover - defensive logging
        _logger.warning(
            'Unexpected error while checking locale directory', directory=directory, user_hint=user_hint, error=error
        )
    return False


def ensure_locale_templates() -> None:
    destination = _resolve_user_locales_dir()
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except Exception as error:
        _logger.warning('Unable to create locales directory', destination=destination, error=error)
        return

    if not _DEFAULT_LOCALES_DIR.exists():
        _logger.debug('Default locales directory is missing', DEFAULT_LOCALES_DIR=_DEFAULT_LOCALES_DIR)
        return

    if not _directory_is_writable(destination):
        return

    destination_has_files = any(destination.glob('*'))

    def _copy_locale(source: Path, target: Path) -> None:
        try:
            shutil.copyfile(source, target)
        except Exception as error:
            _logger.warning('Failed to copy default locale', source=source, target=target, error=error)

    if not destination_has_files:
        for template in _DEFAULT_LOCALES_DIR.iterdir():
            if not template.is_file():
                continue
            _copy_locale(template, destination / template.name)
        return

    for locale_code in ('ru', 'en', 'fa'):
        source_path = _DEFAULT_LOCALES_DIR / f'{locale_code}.json'
        target_path = destination / f'{locale_code}.json'

        if target_path.exists():
            continue

        if not source_path.exists():
            _logger.debug('Default locale template is missing', locale_code=locale_code, source_path=source_path)
            continue

        _copy_locale(source_path, target_path)


def _load_default_locale(language: str) -> dict[str, Any]:
    default_path = _DEFAULT_LOCALES_DIR / f'{language}.json'
    if not default_path.exists():
        return {}
    return _normalize_locale_dict(_load_locale_file(default_path))


def _load_user_locale(language: str) -> dict[str, Any]:
    user_dir = _resolve_user_locales_dir()
    for extension in ('.json', '.yml', '.yaml'):
        candidate = user_dir / f'{language}{extension}'
        if candidate.exists():
            return _normalize_locale_dict(_load_locale_file(candidate))
    return {}


def _load_locale_file(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    try:
        if suffix == '.json':
            return json.loads(path.read_text(encoding='utf-8'))
        if suffix in {'.yml', '.yaml'}:
            try:
                import yaml  # type: ignore
            except ModuleNotFoundError as import_error:
                raise RuntimeError(
                    'PyYAML is required to load YAML locale files. Install PyYAML or provide JSON files.'
                ) from import_error
            return yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    except Exception as error:
        _logger.warning('Failed to parse locale file', path=path, error=error)
        return {}

    _logger.warning('Unsupported locale format', path=path)
    return {}


def _merge_dicts(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in overrides.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


@cache
def load_locale(language: str) -> dict[str, Any]:
    language = language or DEFAULT_LANGUAGE
    defaults = _load_default_locale(language)
    overrides = _load_user_locale(language)
    merged = _merge_dicts(defaults, overrides)

    if not merged and language != DEFAULT_LANGUAGE:
        _logger.warning(
            'Locale not found — falling back to default language', language=language, DEFAULT_LANGUAGE=DEFAULT_LANGUAGE
        )
        return load_locale(DEFAULT_LANGUAGE)
    return merged


def clear_locale_cache() -> None:
    load_locale.cache_clear()
