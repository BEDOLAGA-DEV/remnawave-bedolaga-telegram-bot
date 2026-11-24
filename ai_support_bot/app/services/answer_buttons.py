"""Buttons under an answer: the model names an action, the code supplies a checked address."""
import re
from datetime import datetime

from ai_support_bot.app.services.subscription_address import current_link
from ai_support_bot.app.services.telegram_html import is_allowed_link

SITE = 'https://web.slig.app'
BOT_LINK = 'https://t.me/SligVPN_bot?start=ai_support'
# Payment page of one subscription by the code from its link: no bot, no cabinet login; a trial gets the Standard plan.
RENEW_PAGE = f'{SITE}/api/cabinet/public/renew/'
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
_SUBSCRIPTION_CODE = re.compile(r'подписка №\d+ \(id=(\d+)\)[^\n]*?ссылка=https://[^/\s,]+/([A-Za-z0-9_-]{6,64})(?=[\s,]|$)')
_SITE_LINK = re.compile(re.escape(SITE) + r'(/[\w/:-]*)')


def extract(text: str) -> tuple[str, list[str]]:
    """Remove [[BUTTONS: ...]] markers and return the known actions in their order."""
    actions: list[str] = []
    for match in _MARKER.finditer(text or ''):
        for name in re.split(r'[,\s]+', match.group(1).strip().lower()):
            if name in ACTIONS and name not in actions:
                actions.append(name)
    return _MARKER.sub('', text or '').strip(), actions


def renew_page(user_context: str) -> dict[str, str]:
    """Subscription id → its own renew page, only for subscriptions whose link is in the customer's data."""
    return {sub_id: RENEW_PAGE + code for sub_id, code in _SUBSCRIPTION_CODE.findall(user_context or '')}


def _url(action: str, links: list[str], subscription_ids: list[str], pages: dict[str, str] | None = None) -> str:
    single = subscription_ids[0] if len(subscription_ids) == 1 else None
    if action == 'renew' and single and (pages or {}).get(single):
        return pages[single]
    return {
        # A link still on connect.sligvpn.ru opens on sub.slig.my: the old domain is blocked by some providers.
        'subscription': current_link(links[0]) if len(links) == 1 else f'{SITE}/subscriptions',
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
    pages = renew_page(user_context)
    rows = []
    for action in actions[:MAX_BUTTONS]:
        label, style = ACTIONS[action]
        url = _url(action, links, subscription_ids, pages)
        if not is_allowed_link(url):
            continue
        rows.append([{'text': label, 'url': url, **({'style': style} if style else {})}])
    return rows


def subscription_button(links: list[str] | tuple[str, ...]) -> list[list[dict]]:
    """«Моя подписка» for the one working subscription named in a rule answer; several links get no button, not a guess."""
    if len(links) != 1 or not is_allowed_link(links[0]):
        return []
    label, style = ACTIONS['subscription']
    return [[{'text': label, 'url': links[0], 'style': style}]]


_SUBSCRIPTION_LINE = re.compile(r'подписка №(\d+) \(id=(\d+)\), статус=[^,]*, триал=(да|нет), до ([\d.]+)')


def renew_buttons(user_context: str) -> list[list[dict]]:
    """Green buttons straight to the renewal page of each paid subscription; a trial-only customer gets «buy».

    Nothing is offered when no subscription could be read: «buy» must not be shown to someone who has one.
    """
    found = _SUBSCRIPTION_LINE.findall(user_context or '')
    pages = renew_page(user_context)
    subscriptions = [(number, sub_id, end) for number, sub_id, trial, end in found if trial == 'нет']
    if not subscriptions:
        trials = [sub_id for _, sub_id, trial, _ in found if trial == 'да']
        if len(trials) == 1 and pages.get(trials[0]):
            # The renew page moves a trial to the paid Standard plan in one payment.
            return [[{'text': ACTIONS['buy'][0], 'url': pages[trials[0]], 'style': 'success'}]]
        return build(['buy']) if found or 'Подписки: отсутствуют' in (user_context or '') else []

    def url(sub_id):
        return pages.get(sub_id) or f'{SITE}/subscriptions/{sub_id}/renew'

    if len(subscriptions) == 1:
        return [[{'text': 'Продлить подписку', 'url': url(subscriptions[0][1]), 'style': 'success'}]]
    return [[{'text': f'Продлить №{number} · до {end}', 'url': url(sub_id), 'style': 'success'}]
            for number, sub_id, end in subscriptions[:MAX_BUTTONS]]


_SUBSCRIPTION_STATE = re.compile(r'подписка №\d+ \(id=\d+\), статус=([^,]*), триал=(да|нет), до ([\d.]+)( \(истекла)?')


def expired_until(user_context: str) -> tuple[str, bool] | None:
    """When none of the subscriptions works: the latest end date and whether a paid one exists."""
    found = _SUBSCRIPTION_STATE.findall(user_context or '')
    if not found or any(status.strip() == 'active' and not expired for status, _, _, expired in found):
        return None
    try:
        latest = max(found, key=lambda item: datetime.strptime(item[2], '%d.%m.%Y'))
    except ValueError:
        return None
    return latest[2], any(trial == 'нет' for _, trial, _, _ in found)


def renew_answer(rows: list[list[dict]]) -> str:
    """Short text under renew buttons: no menu path to read, no «write 1 or 2»."""
    direct = bool(rows) and all(row[0]['url'].startswith(RENEW_PAGE) for row in rows)
    if rows and rows[0][0]['text'] == ACTIONS['buy'][0]:
        return ('Оформить подписку можно по кнопке ниже — оплата сразу на сайте, без бота.' if direct
                else 'Оформить подписку можно по кнопке ниже.')
    if direct:
        return ('Продлить можно по кнопке ниже — оплата сразу на сайте, без бота, в том числе заранее.'
                + (' Выберите нужную подписку.' if len(rows) > 1 else ''))
    if len(rows) > 1:
        return 'Выберите подписку на кнопке ниже — продлить можно и заранее, пока она активна.'
    return 'Продлить можно по кнопке ниже — в том числе заранее, пока подписка активна.'


_DEVICE = re.compile(r'устройств|девайс|телевизор|\bтв\b|ноутбук|компьютер|планшет|ipad|айпад|macbook|макбук', re.IGNORECASE)


def device_action(question: str) -> str | None:
    """«Добавить/подключить ещё одно устройство» → connect; «удалить, лимит, превышено» → the device list."""
    if not _DEVICE.search(question or ''):
        return None
    if re.search(r'удал|сброс|отвяз|лимит|превыш|максимальн', question, re.IGNORECASE):
        return 'devices'
    if re.search(r'добав|подключ|ещ[её]|втор|нов|друг', question, re.IGNORECASE):
        return 'connect'
    return None


def mentions_connect(answer: str) -> bool:
    """The answer names the «Подключить устройство» section or its page."""
    text = re.sub(r'<[^>]*>', '', answer or '')
    return bool(re.search(r'подключить\s+устройство', text, re.IGNORECASE) or re.search(re.escape(SITE) + r'/connection\b', text))


def page_actions(answer: str) -> list[str]:
    """Rule answers already name a cabinet page; offer it as a button."""
    for match in _SITE_LINK.finditer(answer or ''):
        path = match.group(1)
        if re.match(r'/subscriptions/\d+/renew|/api/cabinet/public/renew/', path):
            return ['renew']
        for prefix, action in _PAGE_ACTIONS:
            if path.startswith(prefix):
                return [action]
    return []
