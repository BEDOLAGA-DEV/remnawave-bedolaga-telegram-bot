DISPLAY_MODE_BOT = 'bot'
DISPLAY_MODE_WEB = 'web'
DISPLAY_MODE_BOTH = 'both'

VALID_DISPLAY_MODES = (DISPLAY_MODE_BOT, DISPLAY_MODE_WEB, DISPLAY_MODE_BOTH)

_CYCLE_ORDER = (DISPLAY_MODE_BOTH, DISPLAY_MODE_BOT, DISPLAY_MODE_WEB)

_DISPLAY_MODE_LABELS = {
    DISPLAY_MODE_BOT: '🤖 Только бот',
    DISPLAY_MODE_WEB: '🌐 Только веб',
    DISPLAY_MODE_BOTH: '🔁 Бот и веб',
}


def normalize_display_mode(value: str | None) -> str:
    normalized = (value or '').strip().lower()
    if normalized in VALID_DISPLAY_MODES:
        return normalized
    return DISPLAY_MODE_BOTH


def is_visible_in_bot(value: str | None) -> bool:
    return normalize_display_mode(value) in (DISPLAY_MODE_BOT, DISPLAY_MODE_BOTH)


def is_visible_in_web(value: str | None) -> bool:
    return normalize_display_mode(value) in (DISPLAY_MODE_WEB, DISPLAY_MODE_BOTH)


def next_display_mode(value: str | None) -> str:
    current = normalize_display_mode(value)
    index = _CYCLE_ORDER.index(current)
    return _CYCLE_ORDER[(index + 1) % len(_CYCLE_ORDER)]


def display_mode_label(value: str | None) -> str:
    return _DISPLAY_MODE_LABELS[normalize_display_mode(value)]
