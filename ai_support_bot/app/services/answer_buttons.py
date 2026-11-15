"""Buttons under an answer: the model names an action, the code supplies a checked address."""
import re

from ai_support_bot.app.services.telegram_html import is_allowed_link

SITE = 'https://web.slig.app'
BOT_LINK = 'https://t.me/SligVPN_bot?start=ai_support'
MAX_BUTTONS = 2

# Blue opens the customer's own things, green leads to a payment; red is not used on purpose.
ACTIONS = {
    'subscription': ('Моя подписка', 'primary'),
    'connect': ('Подключить устройство', 'primary'),
    'devices': ('Мои устройства', 'primary'),
    'renew': ('Продлить подписку', 'success'),
    'buy': ('Купить подписку', 'success'),
    'topup': ('Пополнить баланс', 'success'),
    'wheel': ('Колесо', 'primary'),
    'accounts': ('Привязать Telegram', 'primary'),
    'bot': ('Открыть бота', None),
}
# Cabinet pages named in rule answers, longest prefix first.
_PAGE_ACTIONS = (
    ('/balance/top-up', 'topup'), ('/subscription/purchase', 'buy'), ('/connection', 'connect'),
    ('/wheel', 'wheel'), ('/profile/accounts', 'accounts'),
)

_MARKER = re.compile(r'[ \t]*\[\[BUTTONS?:([^\]]*)\]\][ \t]*\n?', re.IGNORECASE)
_SUBSCRIPTION_ID = re.compile(r'подписка №\d+ \(id=(\d+)\)')
_SUBSCRIPTION_LINK = re.compile(r'ссылка=(https://[^\s,]+)')
_SITE_LINK = re.compile(re.escape(SITE) + r'(/[\w/:-]*)')


def extract(text: str) -> tuple[str, list[str]]:
    """Remove [[BUTTONS: ...]] markers and return the known actions in their order."""
    actions: list[str] = []
    for match in _MARKER.finditer(text or ''):
        for name in re.split(r'[,\s]+', match.group(1).strip().lower()):
            if name in ACTIONS and name not in actions:
                actions.append(name)
    return _MARKER.sub('', text or '').strip(), actions


def _url(action: str, links: list[str], subscription_ids: list[str]) -> str:
    single = subscription_ids[0] if len(subscription_ids) == 1 else None
    return {
        'subscription': links[0] if len(links) == 1 else f'{SITE}/subscriptions',
        'connect': f'{SITE}/connection',
        'devices': f'{SITE}/subscriptions/{single}' if single else f'{SITE}/subscriptions',
        'renew': f'{SITE}/subscriptions/{single}/renew' if single else f'{SITE}/subscriptions',
        'buy': f'{SITE}/subscription/purchase',
        'topup': f'{SITE}/balance/top-up',
        'wheel': f'{SITE}/wheel',
        'accounts': f'{SITE}/profile/accounts',
        'bot': BOT_LINK,
    }[action]


def build(actions: list[str], user_context: str = '') -> list[list[dict]]:
    """One button per row; the personal subscription link is taken only from the customer's own data."""
    links = _SUBSCRIPTION_LINK.findall(user_context or '')
    subscription_ids = _SUBSCRIPTION_ID.findall(user_context or '')
    rows = []
    for action in actions[:MAX_BUTTONS]:
        label, style = ACTIONS[action]
        url = _url(action, links, subscription_ids)
        if not is_allowed_link(url):
            continue
        rows.append([{'text': label, 'url': url, **({'style': style} if style else {})}])
    return rows


_SUBSCRIPTION_LINE = re.compile(r'подписка №(\d+) \(id=(\d+)\), статус=[^,]*, триал=(да|нет), до ([\d.]+)')


def renew_buttons(user_context: str) -> list[list[dict]]:
    """Green buttons straight to the renewal page of each paid subscription; a trial-only customer gets «buy».

    Nothing is offered when no subscription could be read: «buy» must not be shown to someone who has one.
    """
    found = _SUBSCRIPTION_LINE.findall(user_context or '')
    subscriptions = [(number, sub_id, end) for number, sub_id, trial, end in found if trial == 'нет']
    if not subscriptions:
        return build(['buy']) if found or 'Подписки: отсутствуют' in (user_context or '') else []
    if len(subscriptions) == 1:
        return [[{'text': 'Продлить подписку', 'url': f'{SITE}/subscriptions/{subscriptions[0][1]}/renew', 'style': 'success'}]]
    return [[{'text': f'Продлить №{number} · до {end}', 'url': f'{SITE}/subscriptions/{sub_id}/renew', 'style': 'success'}]
            for number, sub_id, end in subscriptions[:MAX_BUTTONS]]


def renew_answer(rows: list[list[dict]]) -> str:
    """Short text under renew buttons: no menu path to read, no «write 1 or 2»."""
    if rows and rows[0][0]['text'] == ACTIONS['buy'][0]:
        return 'Оформить подписку можно по кнопке ниже.'
    if len(rows) > 1:
        return 'Выберите подписку на кнопке ниже — продлить можно и заранее, пока она активна.'
    return 'Продлить можно по кнопке ниже — в том числе заранее, пока подписка активна.'


def page_actions(answer: str) -> list[str]:
    """Rule answers already name a cabinet page; offer it as a button."""
    for match in _SITE_LINK.finditer(answer or ''):
        path = match.group(1)
        if re.match(r'/subscriptions/\d+/renew', path):
            return ['renew']
        for prefix, action in _PAGE_ACTIONS:
            if path.startswith(prefix):
                return [action]
    return []
