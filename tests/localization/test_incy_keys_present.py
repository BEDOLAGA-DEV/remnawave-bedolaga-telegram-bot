import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

REQUIRED_KEYS = [
    'APP_CHOICE_PROMPT',
    'APP_CHOICE_HAPP',
    'APP_CHOICE_INCY',
    'INCY_CONNECT_TITLE',
    'INCY_CONNECT_HINT',
    'INCY_DOWNLOAD_BUTTON',
    'INCY_DOWNLOAD_PROMPT',
    'INCY_DOWNLOAD_OPEN_LINK',
    'INCY_DOWNLOAD_LINK_NOT_SET',
    'INCY_PLATFORM_ANDROID',
    'INCY_PLATFORM_IOS',
    'INCY_PLATFORM_WINDOWS',
    'INCY_PLATFORM_MACOS',
    'INCY_PLATFORM_LINUX',
    'INCY_ARCH_ARM',
    'INCY_ARCH_X64',
    'INCY_ARCH_APPLE_SILICON',
    'INCY_ARCH_INTEL',
    'INCY_PKG_DEB',
    'INCY_PKG_RPM',
    'INCY_PKG_PORTABLE',
]

LOCALE_FILES = [
    ROOT / 'app' / 'localization' / 'locales' / 'ru.json',
    ROOT / 'locales' / 'ru.json',
]


def test_required_incy_keys_present_in_ru_locales():
    for path in LOCALE_FILES:
        data = json.loads(path.read_text(encoding='utf-8'))
        missing = [k for k in REQUIRED_KEYS if k not in data]
        assert not missing, f'{path} missing keys: {missing}'
