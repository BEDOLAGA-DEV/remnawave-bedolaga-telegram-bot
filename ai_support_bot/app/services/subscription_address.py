"""The subscription address moved on 23.11.2026: connect.sligvpn.ru → sub.slig.my, the code in the link stays the same.

The old domain is blocked by some Russian providers, so the apps (Happ, INCY) cannot refresh a subscription added by the
old link: «ошибка обновления», «превышен таймаут», «нет серверов», N/A on every server, an old end date after a payment.
The fix is to add the subscription again by the new link; a reissue is not needed and would cut off every device.
"""
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

from ai_support_bot.app.services.service_identity import renewal_payment_request

SUBSCRIPTION_HOST = 'sub.slig.my'
OLD_SUBSCRIPTION_HOSTS = frozenset({'connect.sligvpn.ru'})
SUBSCRIPTION_HOSTS = frozenset({SUBSCRIPTION_HOST}) | OLD_SUBSCRIPTION_HOSTS
# Both rule answers contain this phrase: its copy in the history means the steps were already given in this dialogue.
ADDRESS_MARK = 'сменили адрес подписки'
MAX_LINKS = 3

_CODE = re.compile(r'[A-Za-z0-9_-]{4,128}')


def _split(url: str):
    try:
        parsed = urlsplit(url if '://' in url else f'https://{url}')
        host = (parsed.hostname or '').lower().rstrip('.')
        parsed.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:
        return None, ''
    return parsed, host


def link_code(url: str) -> str | None:
    """The personal code of a subscription link on our subscription hosts, old or new."""
    parsed, host = _split(url)
    if parsed is None or host not in SUBSCRIPTION_HOSTS or parsed.username or parsed.password:
        return None
    segment = parsed.path.strip('/').split('/', 1)[0]
    return segment if _CODE.fullmatch(segment) else None


def is_subscription_link(url: str) -> bool:
    return link_code(url) is not None


def current_link(url: str) -> str:
    """The same link on the current host: connect.sligvpn.ru/<code> → sub.slig.my/<code>; other links stay as they are."""
    parsed, host = _split(url)
    if parsed is None or host not in OLD_SUBSCRIPTION_HOSTS or parsed.username or parsed.password:
        return url
    if '://' not in url:
        return SUBSCRIPTION_HOST + url[len(parsed.netloc):]
    return urlunsplit(('https', SUBSCRIPTION_HOST, parsed.path, parsed.query, parsed.fragment))


_CONTEXT_LINK = re.compile(r'ссылка=(https?://[^\s,]+)')


def own_codes(*contexts: str) -> set[str]:
    """Codes of the customer's own subscription links from <user_data> and refreshed account data."""
    codes = set()
    for context in contexts:
        for url in _CONTEXT_LINK.findall(context or ''):
            parsed, _ = _split(url)
            segment = parsed.path.strip('/').split('/', 1)[0] if parsed else ''
            if _CODE.fullmatch(segment):
                codes.add(segment)
    return codes


@dataclass(frozen=True)
class Subscription:
    number: str
    sub_id: str
    trial: bool
    until: str
    link: str | None


_SUBSCRIPTION = re.compile(
    r'подписка №(\d+) \(id=(\d+)\), статус=([^,\n]*)(?:, триал=(да|нет))?(?:, до ([\d.]+))?([^\n]*)'
)


def active_subscriptions(user_context: str) -> list[Subscription]:
    """Working subscriptions from <user_data>, with their links already on the current host."""
    found = []
    for number, sub_id, status, trial, until, rest in _SUBSCRIPTION.findall(user_context or ''):
        if status.strip() != 'active' or rest.lstrip().startswith('(истекла'):
            continue
        link = _CONTEXT_LINK.search(rest)
        url = current_link(link.group(1)) if link and is_subscription_link(link.group(1)) else None
        found.append(Subscription(number, sub_id, trial == 'да', until, url))
    return found


_PAYMENT_LINE = re.compile(
    r'•\s*(\d{2}\.\d{2}\.\d{4})(?:\s+(\d{2}:\d{2}))?(?:\s+UTC)?\s+subscription_payment\b([^\n]*)'
)


def recent_subscription_payment(user_context: str, now: datetime | None = None, days: int = 3) -> bool:
    """A completed subscription payment in the last `days` days, by the UTC time of the operation line."""
    now = now or datetime.now(UTC)
    for day, clock, rest in _PAYMENT_LINE.findall(user_context or ''):
        if 'статус=завершена' not in rest:
            continue
        try:
            moment = datetime.strptime(f'{day} {clock or "00:00"}', '%d.%m.%Y %H:%M').replace(tzinfo=UTC)
        except ValueError:
            continue
        if clock:
            if now - timedelta(days=days) <= moment <= now + timedelta(hours=1):
                return True
        elif 0 <= (now.date() - moment.date()).days <= days:
            return True
    return False


def address_help_given(history: list) -> bool:
    """Rule answers go once per dialogue: a second complaint takes the usual path (model, diagnostics, team)."""
    return any(
        getattr(item, 'role', '') == 'assistant' and ADDRESS_MARK in (getattr(item, 'content', '') or '')
        for item in history
    )


# --- What the customer writes -------------------------------------------------------------------------------------
# All patterns run on casefolded text.

# The mailing, the domains and «you changed the address»: always about the move.
_ADDRESS_NEWS = re.compile(
    r'(?:смени|поменя|измени)(?:ли|лся|лась|лось)\s+(?:\w+\s+){0,2}?(?:адрес|домен|ссылк)'
    r'|(?:адрес|домен)\w*\s+(?:\w+\s+){0,2}?(?:смени|поменя|измени)(?:ли|лся|лась|лось)'
    r'|нов\w*\s+(?:адрес|домен)|прежн\w*\s+(?:адрес|домен)|стар\w*\s+домен'
    r'|connect\.sligvpn|(?<![\w.-])(?:sub\.)?slig\.my\b'
    r'|обновите\s+подписку\s*[—–-]+\s*1\s+минут'
    r'|перевыпуст\w*\s+или\s+обнов|обнов\w*\s+или\s+перевыпуст'
    r'|(?:прислал\w*|присла\w*|пришл\w*|рассылк\w*|сообщени\w*|уведомлени\w*|написал\w*)[^.!?\n]{0,80}?'
    r'(?:перевыпуст|обнов\w*\s+(?:\w+\s+)?подписк)'
)
# «Нужно поменять адрес», «пришло уведомление … адрес», «пришла инструкция по обновлению Happ»: the mailing in other words.
# An e-mail address is another question: these two only count without e-mail words in the message.
_ADDRESS_CHANGE = re.compile(r'(?:поменять|сменить|изменить|обновить)\s+(?:\w+\s+){0,2}?(?:адрес|домен)')
_MAILING = re.compile(
    r'(?:пришл|прислал|присла|рассылк|сообщени|уведомлени|смс|sms|инструкци)\w*[^.!?\n]{0,80}?'
    r'(?:адрес|домен|обновлени\w*\s+(?:happ|incy|хапп\w*|инси|подписк|ссылк)|обнов\w*\s+(?:\w+\s+)?(?:happ|incy|хапп\w*|инси))'
)
_EMAIL_WORDS = re.compile(r'почт|email|e-mail|электрон|ящик')
# «И то, что по последним смс, я не могу сделать»: the steps from the mailing did not work out.
_BY_MESSAGES = re.compile(
    r'по\s+(?:последн|вчерашн|недавн|ваш|прошл|нов)\w*\s+(?:смс|sms|сообщени|уведомлени|рассылк|инструкци)\w*'
)
_UNABLE = re.compile(
    r'не\s+(?:могу|можем|получа\w*|выход\w*|вышл\w*|удаётся|удается|удал\w*|смог\w*|понимаю|понял\w*|работа\w*|помог\w*)'
)
# «Я сделал всё по ней, но не получается» right after «пришла инструкция по обновлению»: the same mailing.
_REFERS_BACK = re.compile(
    r'по\s+(?:ней|нему|инструкци\w*|шагам|этой\s+инструкци\w*)|как\s+(?:там\s+|в\s+ней\s+)?(?:написан|сказан)\w*'
    r'|сделал\w*\s+(?:вс[её]|как)'
)


def _address_news(text: str) -> bool:
    if _ADDRESS_NEWS.search(text):
        return True
    if not _EMAIL_WORDS.search(text) and (_ADDRESS_CHANGE.search(text) or _MAILING.search(text)):
        return True
    # A login code by SMS is not the mailing.
    return bool(_BY_MESSAGES.search(text) and _UNABLE.search(text)) and not re.search(r'\bкод\w*', text)


# «Хочу новую ссылку, кто-то пользуется моей подпиской»: a real reissue, not the move.
_REISSUE_WISH = re.compile(
    r'чуж\w*\s+(?:устройств|подключ|человек|люд|телефон)|кто[\s-]?то\s+(?:ещ[её]\s+|другой\s+)?(?:подключ|пользу|сидит|зашел|зашёл)'
    r'|посторонн\w*|украл\w*|слил\w*|утек\w*|ут[её]кл\w*'
    r'|отключить\s+(?:все\s+)?(?:чужие\s+|други\w+\s+|остальн\w+\s+)?устройств|сбросить\s+(?:все\s+)?устройств'
    r'|(?:хочу|нужно|надо|можно|как)\s+(?:\w+\s+)?(?:перевыпустить|сменить\s+ссылк|поменять\s+ссылк)'
)
_ADDRESS_HINT = re.compile(r'нов\w*\s+ссылк|ссылк\w*\s+(?:\w+\s+)?(?:смени|поменя|измени)(?:лась|лись)')
# The app's own refresh errors: «Ошибка обновления подписки», «превышен таймаут», INCY «Network error», «Нет серверов», N/A.
_REFRESH_ERROR = re.compile(
    r'ошибк\w*\s+(?:при\s+)?обновлени'
    r'|подписк\w*\s+(?:\w+\s+)?не\s+(?:загруж|подгруж|скачива)\w*|не\s+(?:загруж|подгруж)\w*\s+подписк'
    r'|данн\w*\s+(?:\w+\s+)?не\s+(?:были\s+)?получ'
    r'|превыш\w*\s+(?:\w+\s+){0,2}?(?:таймаут|тайм-аут|ожидани)|тайм-?аут|timed?\s*-?\s*out|network\s+error|сетев\w*\s+ошибк'
    r'|нет\s+(?:ни\s+одного\s+|доступных\s+|никаких\s+)?(?:серверов|ни\s+одного\s+сервера)|серверов\s+(?:\w+\s+){0,2}?нет\b'
    # All servers gone, not one: «сервер Нидерланды пропал» is a server question for the live status.
    r'|сервер(?:ы|а)\s+(?:\w+\s+)?(?:пропал|исчез)\w*|(?:пропал|исчез)\w*\s+(?:все\s+)?сервер(?:ы|а)\b'
)
# «В приложении пропали страны для выбора», «нет стран», «страны исчезли»: the whole list is gone, like the servers.
# One country gone («пропала локация Германия») or «каких стран нет?» is a server question for the live status.
_COUNTRIES_GONE = re.compile(
    r'(?:пропал|исчез)\w*\s+(?:все\s+)?(?:страны|локации)\b|(?:страны|локации)\s+(?:\w+\s+){0,2}?(?:пропал|исчез)\w*'
    r'|^\W*(?:(?:а|у\s+меня|теперь|сейчас|почему|в\s+приложении)\s+)*нет\s+(?:ни\s+одной\s+страны|никаких\s+стран|стран|локаций)\b\W*$'
    r'|(?:приложени\w*|happ|incy|хапп\w*|инси|списк\w*)\s+(?:\w+\s+)?нет\s+(?:ни\s+одной\s+страны|никаких\s+стран|стран|локаций)\b'
    r'|нет\s+(?:ни\s+одной\s+страны|никаких\s+стран|стран|локаций)\s+(?:для\s+выбора|в\s+(?:приложени|списк|happ|incy))'
)
_TIMEOUT = re.compile(r'тайм-?аут|timed?\s*-?\s*out')
_CONNECTING = re.compile(r'подключ|соедин|\bconnect(?:ing|ion)?\b')
_SUBSCRIPTION_WORDS = re.compile(r'подписк|обнов|subscription|update')


def _refresh_error(text: str) -> bool:
    if _REFRESH_ERROR.search(_TIMEOUT.sub(' ', text)):
        return True
    # «Ошибка таймаут при подключении к серверу» is a server question; «превышен таймаут» of a refresh is the move.
    return bool(_TIMEOUT.search(text)) and not (_CONNECTING.search(text) and not _SUBSCRIPTION_WORDS.search(text))


# N/A on every server; «пинг 80, потом 800 или N/A» on one server is a speed complaint.
_NOT_AVAILABLE = re.compile(r'(?<![\w/])n\s*/\s*a(?![\w/])')
_EVERY_SERVER = re.compile(r'кажд\w*|\bвсе[хм]?\b|\bвс[её]\b|везде|сервер(?:ы|а|ов|ам|ах)\b')
# «Не могу обновить», «не обновляется» — unless it is the app itself that does not update.
_REFRESH_FAILED = re.compile(
    r'не\s+обновля\w*|не\s+обновил(?:ась|ось|ся|ись)|не\s+обнов(?:ится|ятся)'
    r'|не\s+(?:могу|можем|может|получается|получилось|удаётся|удается|удалось|выходит|вышло|смог\w*)\s+(?:\w+\s+){0,2}?обнов\w*'
)
_APP_UPDATE = re.compile(
    r'не\s+(?:\w+\s+){0,2}?обнов\w*\s+(?:само\s+)?приложени\w*(?!\s+подписк)'
    r'|обнов\w*\s+(?:\w+\s+){0,3}?(?:в|через|из)\s+(?:app\s*store|апп?\s*стор|аппстор|эпп\s*стор|google\s*play|play\s*market'
    r'|плей\s*маркет|маркет\w*|магазин\w*)'
)
_DEVICE = (
    r'(?:\bпк\b|компьютер\w*|\bкомп\b|ноутбук\w*|ноуте|телефон\w*|смартфон\w*|айфон\w*|iphone|ipad|айпад\w*|андроид\w*'
    r'|android|приложени\w*|happ|incy|хапп\w*|хэпп\w*|инси|телевизор\w*|\bтв\b|планшет\w*|макбук\w*|macbook|\bmac\b|устройств\w*)'
)
_OWN_PLACE = r'(?:бот\w*|сайт\w*|кабинет\w*|личн\w+\s+кабинет\w*)'
_STALE_STRONG = (
    r'(?:не\s+(?:продлил|продлен|продлён|обнов|измени|поменял|добавил|прибавил|увеличил|зачисл)\w*'
    r'|(?:стар|прежн)\w*\s+(?:дат|срок)\w*|(?:дат|срок)\w*\s+(?:стар|прежн|не\s+(?:измен|поменял|обнов))\w*)'
)
_STALE_WEAK = r'(?:ист[её]к\w*|истека\w*|заканчива\w*|закончил\w*)'
_STALE_ANY = (
    r'(?:' + _STALE_STRONG + '|' + _STALE_WEAK
    # «Подписка не пришла», but not «письмо на почту не пришло».
    + r'|(?:подписк|дн|срок|продлени|оплат)\w*\s+(?:\w+\s+)?не\s+(?:пришл|появ|отобража|засчит|прош[её]л|прошла)\w*'
    + r'|не\s+(?:пришл|появил)\w*\s+(?:подписк|дн|продлени)'
    + r'|\b0\s*дн|(?:дн\w*|срок)\s+(?:те\s+же|прежн|стар))'
)
_STALE_ANY_RE = re.compile(_STALE_ANY)
_AT = r'(?<!\w)(?:на|в)\s+'


def _gap(stop: str) -> str:
    """Up to 40 characters inside one clause that do not reach another place («в боте, а в приложении …»)."""
    return r'(?:(?!' + stop + r'|\bа\b|\bно\b)[^.!?\n,;]){0,40}?'


# «В приложении старая дата», «на ПК не продлился», «ссылку на продлённую подписку».
_STALE_ON_DEVICE = re.compile(
    _AT + _DEVICE + _gap(_OWN_PLACE) + _STALE_STRONG
    + r'|' + _STALE_STRONG + _gap(_OWN_PLACE) + _AT + _DEVICE
    + r'|(?:приложени\w*|happ|incy|хапп\w*|инси)\s+(?:\w+\s+){0,3}?(?:стар|прежн)\w*\s+(?:дат|срок)'
    + r'|ссылк\w*[^.!?\n]{0,40}продл[её]нн\w*|продл[её]нн\w*\s+подписк\w*[^.!?\n]{0,30}(?:ссылк|подключ)'
)
_EXPIRED_ON_DEVICE = re.compile(
    _AT + _DEVICE + _gap(_OWN_PLACE) + _STALE_WEAK + r'|' + _STALE_WEAK + _gap(_OWN_PLACE) + _AT + _DEVICE
)
# The bot, the site and the cabinet show our database: an old date there is a payment question, not the move.
_OWN_DATA_STALE = re.compile(
    _AT + _OWN_PLACE + _gap(_DEVICE) + r'(?:' + _STALE_ANY + r'|висит)'
    + r'|' + _STALE_ANY + _gap(_DEVICE) + _AT + _OWN_PLACE
    + r'|баланс\w*\s+(?:\w+\s+)?не\s+(?:обнов|пополн|измен)|не\s+(?:обнов|пополн|измен)\w*\s+баланс'
)
# «Оплатила в боте» names where the money went, not where the date is shown.
_PAYMENT_PLACE = re.compile(
    r'(?:оплат|оплач|заплат|проплат|купил|продлил|продлен|продлён)\w*\s+(?:\w+\s+){0,2}?(?:в|на|через)\s+' + _OWN_PLACE
)


def _own_data_stale(text: str) -> bool:
    """«В личном кабинете … написано, что подписка истекла»: the old term is in our own data, no app is named."""
    if _OWN_DATA_STALE.search(text):
        return True
    rest = _PAYMENT_PLACE.sub(' ', text)
    return bool(re.search(_AT + _OWN_PLACE, rest) and _STALE_ANY_RE.search(rest) and not re.search(_DEVICE, rest))
_TOPUP = re.compile(
    r'пополн\w*|(?:на|в)\s+(?:сч[её]т|баланс|кошел[её]к)\w*|полож\w*\s+(?:\w+\s+)?(?:деньг|денег|средств)'
    r'|закин\w*|внес(?:ла|ли|л)?\s+(?:\w+\s+)?(?:деньг|денег|средств)'
)
_PAYMENT_WORDS = re.compile(r'оплат|оплач|заплат|проплат|купил|продл|списа|автоплат|автопродл')
_PAID = re.compile(
    r'оплатил\w*|оплачен\w*|заплатил\w*|проплатил\w*|купил\w*|(?<!не\s)(?:продлил\w*|продлен\w*|продлён\w*)'
)
# Money disputes are the team's: never «оплата дошла» there.
_DISPUTE = re.compile(
    r'возврат|верн\w*\s+(?:мне\s+)?(?:деньг|денег|средств)|отмен\w*|отпис\w*|дважды|два\s+раза|\b2\s+раза|двойн\w*'
    r'|чарджб\w*|спорн\w*|без\s+(?:моего\s+)?(?:спроса|согласия|предупрежд)'
    r'|не\s+(?:просил|хотел|подписывал|оформлял|заказывал|включал)\w*|случайно|по\s+ошибке|ошибочно|не\s+т(?:от|у)\s+(?:тариф|подписк)'
)


def address_problem(question: str, context: str = '') -> str | None:
    """Why the question is about the moved address: 'news', 'refresh' or 'stale'; None when it is not.

    'news' — the mailing, the domains, «вы сменили адрес»; 'refresh' — the app cannot refresh the subscription;
    'stale' — after a renewal the device still shows the old subscription.
    ``context`` — the customer's previous messages: «сделал всё по ней, не получается» after the mailing is 'news'.
    """
    text = (question or '').casefold()
    if not text.strip():
        return None
    if _address_news(text):
        return 'news'
    if _REISSUE_WISH.search(text):
        return None
    if _ADDRESS_HINT.search(text):
        return 'news'
    if context and _REFERS_BACK.search(text) and _UNABLE.search(text) and _address_news((context or '').casefold()):
        return 'news'
    if _own_data_stale(text):
        return None
    if _refresh_error(text) or (_REFRESH_FAILED.search(text) and not _APP_UPDATE.search(text)) or _COUNTRIES_GONE.search(text):
        return 'refresh'
    if _NOT_AVAILABLE.search(text) and (len(text) <= 30 or _EVERY_SERVER.search(text)):
        return 'refresh'
    if _TOPUP.search(text):
        return None
    if _STALE_ON_DEVICE.search(text) or (_EXPIRED_ON_DEVICE.search(text) and _PAYMENT_WORDS.search(text)):
        return 'stale'
    return None


def paid_claim(question: str, pending: bool = False) -> bool:
    """«Оплатил, а дни не обновились»: a renewal the customer paid for and does not see.

    A balance top-up, an old date in the bot or on the site, and money disputes are not this case.
    Only the current message counts: an old «оплатил» must not turn «как добавить устройство» into a payment.
    """
    text = (question or '').casefold()
    if _TOPUP.search(text) or _own_data_stale(text) or _DISPUTE.search(text):
        return False
    if not (_PAID.search(text) or pending):
        return False
    return bool(_STALE_ANY_RE.search(text)) or renewal_payment_request(question, [])


# --- Answers ------------------------------------------------------------------------------------------------------

_INTRO = ('🔄 Мы сменили адрес подписки: старый перестал открываться у части провайдеров, поэтому приложение не может '
          'обновиться. Оплата и срок не меняются.')
_STEPS_TAIL = ('2. Откройте Happ или INCY, нажмите «+» и добавьте подписку из буфера обмена.\n'
               '3. Старую подписку в приложении можно удалить.')
_BOT_PATH = ('Откройте свою подписку в боте https://t.me/SligVPN_bot: Мои подписки → выберите подписку → '
             'Подключить устройство → «Добавить в Happ» или «Добавить в INCY».')
_CHECK_OWN_DATA = 'Если и в боте или на сайте срок старый — напишите, проверим.'


def _steps(subscriptions: list[Subscription]) -> str:
    if len(subscriptions) == 1:
        first = f'1. Нажмите на ссылку — она скопируется:\n<code>{subscriptions[0].link}</code>'
    else:
        first = '1. Нажмите на ссылку нужной подписки — она скопируется:\n' + '\n'.join(
            f'Подписка №{sub.number}: <code>{sub.link}</code>' for sub in subscriptions)
    return first + '\n' + _STEPS_TAIL


@dataclass(frozen=True)
class RuleAnswer:
    text: str
    links: tuple[str, ...]


def address_answer(user_context: str, question: str = '') -> RuleAnswer | None:
    """Rule «the address moved»: the personal links of the working subscriptions, without a guess.

    None when no subscription works: the expired-subscription answer is the right one then.
    """
    active = active_subscriptions(user_context)
    if not active:
        return None
    linked = [sub for sub in active if sub.link][:MAX_LINKS]
    check = '\n' + _CHECK_OWN_DATA if _PAYMENT_WORDS.search((question or '').casefold()) else ''
    if not linked:
        text = (f'{_INTRO}\n\nДобавьте подписку заново — это 1 минута. {_BOT_PATH} '
                'Старую подписку в приложении можно удалить.\nПеревыпускать подписку не нужно.' + check)
        return RuleAnswer(text, ())
    other = ('Или откройте подписку по кнопке ниже и нажмите «Добавить в Happ» или «Добавить в INCY».' if len(linked) == 1
             else 'Или откройте ссылку в браузере и нажмите «Добавить в Happ» или «Добавить в INCY».')
    text = (f'{_INTRO}\n\nДобавьте подписку заново — это 1 минута:\n{_steps(linked)}\n\n{other}\n'
            'Перевыпускать подписку не нужно.' + check)
    return RuleAnswer(text, tuple(sub.link for sub in linked))


def paid_answer(user_context: str, now: datetime | None = None) -> RuleAnswer | None:
    """Rule «оплатил, а дни не обновились» when the payment already arrived: the term from our database and the steps.

    Needs a completed subscription_payment in the last three days and a working paid subscription.
    """
    paid = [sub for sub in active_subscriptions(user_context) if not sub.trial]
    if not paid or not recent_subscription_payment(user_context, now):
        return None
    if len(paid) == 1:
        head = f'✅ Оплата дошла: подписка №{paid[0].number} активна до {paid[0].until}.'
    else:
        head = '✅ Оплата дошла, подписки активны: ' + ', '.join(f'№{sub.number} до {sub.until}' for sub in paid) + '.'
    linked = [sub for sub in paid if sub.link][:MAX_LINKS]
    reason = 'Если в приложении старая дата — оно не может обновиться: мы сменили адрес подписки.'
    if linked:
        body = f'{reason} Добавьте подписку заново:\n{_steps(linked)}'
    else:
        body = f'{reason} Добавьте подписку заново. {_BOT_PATH}'
    return RuleAnswer(f'{head}\n{body}\n{_CHECK_OWN_DATA}', tuple(sub.link for sub in linked))
