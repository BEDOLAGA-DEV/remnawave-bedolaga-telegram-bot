"""Regression: admin labels must not contain UTF-16 lone surrogates.

Before fix: emojis in CONDITION_TYPES were stored as surrogate-pair escapes
('\\ud83c\\udf9f'), which Python parses but `urllib.parse.quote(...,
encoding='utf-8')` rejects with `UnicodeEncodeError: surrogates not allowed`.
That crashed `callback.message.edit_text(...)` whenever the dict was rendered
into a Telegram form-urlencoded body.
"""
from app.handlers.admin.achievements import CONDITION_TYPES, REWARD_TYPES


def _scan(d, name):
    for key, value in d.items():
        for ch in value:
            cp = ord(ch)
            if 0xD800 <= cp <= 0xDFFF:
                raise AssertionError(
                    f'{name}[{key!r}] contains surrogate U+{cp:04X}: {value!r}'
                )


def test_condition_types_no_surrogates():
    _scan(CONDITION_TYPES, 'CONDITION_TYPES')


def test_reward_types_no_surrogates():
    _scan(REWARD_TYPES, 'REWARD_TYPES')


def test_condition_types_encode_to_utf8():
    """Defensive: every value must encode cleanly to UTF-8 without errors."""
    for key, value in CONDITION_TYPES.items():
        try:
            value.encode('utf-8')
        except UnicodeEncodeError as exc:
            raise AssertionError(f'CONDITION_TYPES[{key!r}] fails utf-8: {exc}')
