"""Keep seller identity separate from VPN clients and account/payment status."""
import re
from urllib.parse import urlsplit


_SERVICE_HOSTS = {'slig.app', 'web.slig.app', 'sligvpn.ru', 'connect.sligvpn.ru'}
_CLIENT_NAME = re.compile(r'^(?:happ|incy|v2raytun|v2rayng|v2rayn|shadowrocket|streisand|v2box)$', re.IGNORECASE)


def classify_service_source(name: str, url: str, location: str, owned_hosts=()) -> dict:
    if location in {'client', 'advertisement'}:
        return {'status': 'irrelevant', 'location': location}
    name = name.strip()[:120]
    own_name = bool(re.search(r'(?<![\w])slig\s*vpn(?![\w])|(?<![\w])slig\.app(?![\w.])', name, re.IGNORECASE))
    host, bot = '', ''
    try:
        parsed = urlsplit(url if '://' in url else 'https://' + url)
        if parsed.scheme.lower() == 'https' and not parsed.username and not parsed.password:
            host = (parsed.hostname or '').lower().rstrip('.')
            if host == 't.me':
                bot = parsed.path.strip('/').casefold()
    except ValueError:
        pass
    own_url = bool(host) and (
        (host != 't.me' and host in _SERVICE_HOSTS | set(owned_hosts))
        or (host == 't.me' and bot == 'sligvpn_bot')
    )
    other_name = bool(name) and not own_name and not _CLIENT_NAME.fullmatch(name)
    other_vpn = other_name and bool(re.search(r'vpn|впн', name, re.IGNORECASE))
    seller_location = location in {'payment_confirmation', 'merchant', 'seller_chat'}
    status = 'unknown'
    if seller_location:
        if (own_name and host and not own_url) or (own_url and other_name):
            status = 'conflict'
        elif own_url or (own_name and not host):
            status = 'ours'
        elif other_vpn and host:
            status = 'foreign'
    # Persist/display only the host, never a personal subscription token or query.
    return {'status': status, 'seller_name': name, 'host': host, 'location': location}


def renewal_navigation_request(question: str) -> bool:
    return bool(re.search(r'\bкак\s+(?:мне\s+|теперь\s+)?(?:самостоятельно\s+)?продл', question, re.IGNORECASE))


def renewal_payment_request(question: str, history: list, pending: bool = False) -> bool:
    text = question.casefold()
    previous = ' '.join(item.content or '' for item in history[-8:] if item.role == 'user').casefold()
    paid = r'оплатил|проплат|оплачен|оплачена|заплат|плат[её]ж.*(?:прош|произвед)|оплат\w*\s+(?:за|на)'
    renewal = r'продл|добав\w*|прибав\w*|зачисл\w*.*(?:дн|месяц)|к\s+(?:ней|нему|текущей|имеющейся)'
    if renewal_navigation_request(question) and not re.search(paid, text):
        return False
    return bool((re.search(paid, text) and re.search(renewal, text)) or
                ((pending or re.search(paid, previous)) and re.search(renewal, text)))


def renewal_verification_answer(source: dict | None, state: dict) -> tuple[str, bool]:
    status = (source or {}).get('status', 'unknown')
    if status == 'foreign':
        return (
            ('На подтверждении оплаты указан другой VPN-сервис. Оплата другого сервиса не продлевает подписку SligVPN. '
             'По этой операции обратитесь к продавцу, указанному в подтверждении. '
             'Если отдельно оплачивали SligVPN, сообщите об этой операции — проверим её.'), False,
        )
    if status != 'ours':
        return (
            ('Пока не подтверждено, что эта оплата относится к SligVPN. '
             'Текущая дата окончания подписки сама по себе не доказывает добавление оплаченных месяцев. '
             'В каком боте или на каком сайте вы оплачивали? Пришлите его @username или адрес.'), False,
        )
    if state.get('receipt_reported') or all(state.get(k) for k in ('payment_amount', 'payment_time', 'payment_operation_id')):
        return (('По одной текущей дате окончания нельзя подтвердить, что именно этот платёж добавил оплаченный срок. '
                 'Передаю обращение команде для проверки оплаты и продления.'), True)
    return (('Текущая дата окончания подписки не подтверждает, что именно эта оплата продлила её. '
             'Пришлите чек этой операции с суммой, временем и идентификатором платежа для проверки. '
             'Данные карты и остальные операции можно скрыть. Повторно оплачивать не нужно.'), False)
