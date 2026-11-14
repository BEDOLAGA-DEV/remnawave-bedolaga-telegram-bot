"""Premium emoji from the sligvpn_bot set: at most two per answer and only at the start of a line."""
import re

# https://t.me/addemoji/sligvpn_bot; 👑, 🔥 and ⚡ are left out on purpose: they read like an ad.
CUSTOM_EMOJI = {
    '🔑': '5444909935844436725',
    '🛡': '5444861376944188969',
    '🌐': '5445336653730197408',
    '🏷': '5445180299740755955',
    '🎁': '5445024113255035480',
    '⏳': '5445250101549248873',
    '✅': '5447512308068692394',
    '🎧': '5444920102032031423',
    '🚀': '5445337512723656831',
}
CUSTOM_EMOJI = {emoji.replace('️', ''): emoji_id for emoji, emoji_id in CUSTOM_EMOJI.items()}
CUSTOM_EMOJI_IDS = frozenset(CUSTOM_EMOJI.values())
MAX_PER_ANSWER = 2

_EMOJI = '|'.join(re.escape(emoji) for emoji in CUSTOM_EMOJI)
_WRAPPED = re.compile(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', re.DOTALL)
_LINE_START = re.compile(r'(?m)^([ \t]*(?:(?:[-•*]|\d+[.)])[ \t]+)?(?:<[bi]>)?)(' + _EMOJI + r')️?[ \t]*')


def unwrap(text: str) -> str:
    """Premium emoji back to their plain fallback characters."""
    return _WRAPPED.sub(r'\1', text or '')


def decorate(text: str, lead: str | None = None, plain: bool = False) -> str:
    """Turn line-leading emoji from the set into premium ones; extra and, for plain answers, all of them are dropped."""
    text = unwrap(text)
    if lead in CUSTOM_EMOJI and not plain and text.strip() and not _LINE_START.match(text.lstrip('\n')):
        text = f'{lead} {text.lstrip()}'
    used: list[str] = []

    def replace(match):
        prefix, emoji = match.group(1), match.group(2)
        if plain or emoji in used or len(used) >= MAX_PER_ANSWER:
            return prefix
        used.append(emoji)
        return f'{prefix}<tg-emoji emoji-id="{CUSTOM_EMOJI[emoji]}">{emoji}</tg-emoji> '

    return _LINE_START.sub(replace, text)
