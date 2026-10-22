import re
from decimal import Decimal

from ai_support_bot.app.navigation.query import (
    is_channel_followup,
    is_context_followup,
    is_subscription_refresh,
    navigation_target,
)


NOTICE = 'Передаю обращение команде для проверки, подождите, пожалуйста.'
CLIENTS = {
    'Android': ('Incy', 'Happ', 'V2RayNG'),
    'iPhone/iPad': ('Incy', 'Happ', 'Shadowrocket', 'Streisand'),
    'Windows': ('Incy', 'Happ', 'v2rayN'),
    'macOS': ('Incy', 'Happ', 'V2Box'),
}


def boundary_answer(question: str) -> str | None:
    if re.search(r'(?:покажи|раскрой|пришли|выведи|напиши|повтори).*?(?:системн\w* (?:промпт|инструкц)|внутренн\w* инструкц)', question, re.IGNORECASE):
        return 'Внутренние инструкции не раскрываю. Могу помочь с вопросами по SligVPN.'
    return None


def topic_switch_question(question: str) -> str | None:
    text = question.strip()
    marker = re.search(r'\b(?:другой|новый)\s+вопрос\b|\b(?:другая|новая)\s+тема\b|\b(?:сменим|сменить|поменяем)\s+тему\b|\bзабудь\s+(?:прошлый|предыдущий)\s+вопрос\b|^другое[.!?]*$', text, re.IGNORECASE)
    if not marker:
        return None
    remainder = text[marker.end():].strip(' :—,-.!?')
    return remainder if re.match(r'^(?:как|где|почему|что|зачем|како\w*|сколько|vpn|впн)\b', remainder, re.IGNORECASE) else ''


def starts_unrelated_question(question: str) -> bool:
    return bool(re.match(r'^\s*(?:как|где|почему|зачем|како\w*|сколько)\b', question, re.IGNORECASE)) and not re.search(
        r'плат[её]ж|оплат|попол|деньг|списал|чек|выписк|email|почт|регистр|прислать|отправить|передать|это|там', question, re.IGNORECASE,
    )


def conversation_topic(question: str) -> str | None:
    text = question.casefold()
    if is_subscription_refresh(question):
        return 'vpn'
    if is_payment_missing(question) or re.search(r'воз\w*рат.*(?:денег|средств)|верн\w*.*деньг|оплат|плат[её]ж|списал', text):
        return 'payment'
    if re.search(r'сотрудничеств|предложени\w*\s+(?:о\s+)?реклам', text):
        return 'partnership'
    target = navigation_target(question)
    if target:
        return {
            'subscription_connect': 'vpn', 'subscription_autopay': 'renewal',
            'subscription_extend': 'renewal', 'balance_topup': 'payment',
            'balance_history': 'payment', 'web_accounts': 'account', 'web_change_email': 'account',
        }.get(target, target)
    if re.search(r'каки\w* приложени|како\w* (?:приложени|клиент)|что скачать|приложени\w* скачать', text) or starts_technical_issue(question):
        return 'vpn'
    if re.search(r'скорост|качеств|тормоз|медленно|бел\w*\s+списк', text):
        return 'vpn'
    if re.search(r'лимит.*устройств|сколько.*устройств|удал\w*.*устройств', text) and re.search(r'\?|как|сколько|нужно|удал|ошибк', text):
        return 'devices'
    if re.search(r'цен[ауы]|стоимост|тариф|стоит|поч[её]м', text):
        return 'subs_buy_subscription'
    if re.search(r'баланс|аккаунт|email|почт|подписк', text) and re.search(r'покажи|проверь|какой|какая|какие|сколько|мой|моя|моего|моей', text):
        return 'account'
    return None


def starts_new_topic(question: str, history: list, state: dict) -> bool:
    if is_context_followup(question) or is_channel_followup(question) or re.search(r'\b(?:это|этим|этот|эта|этой|его|её|ее|там|тогда)\b|вс[её] равно', question, re.IGNORECASE):
        return False
    current = conversation_topic(question)
    previous = 'payment' if state.get('issue') == 'payment_missing' else next(
        (topic for item in reversed(history) if item.role == 'user' and (topic := conversation_topic(item.content or ''))), None,
    )
    if current and previous:
        return current != previous
    return bool(history) and starts_unrelated_question(question)


def keeps_account_scope(question: str) -> bool:
    return bool(re.search(r'баланс|подписк|аккаунт|email|почт|ссылк|плат[её]ж|операци|данные|тот же|этот же|\bтам\b|^\s*(?:да|нет|покажи|проверь)\b', question, re.IGNORECASE))


def starts_technical_issue(question: str) -> bool:
    return bool(re.search(r'vpn|впн|incy|happ|сервер|интернет', question, re.IGNORECASE)) and bool(
        re.search(r'не работает|не подключ|ошибк|нет интернет|обрыв', question, re.IGNORECASE)
    ) and not re.search(r'плат[её]ж|оплат|попол|деньги|списал', question, re.IGNORECASE)


def payment_next_step(state: dict) -> tuple[str, bool]:
    channel = state.get('purchase_channel')
    if channel not in {'site', 'telegram'}:
        return 'Где вы оплачивали: на сайте или в Telegram-боте?', False
    if channel == 'site':
        if state.get('registration_method') not in {'email', 'telegram', 'other'}:
            return 'На сайте вы регистрировались через электронную почту или другим способом?', False
        if state.get('registration_method') == 'email' and not state.get('email'):
            return 'Напишите адрес электронной почты, с которым регистрировались на сайте.', False
    if not state.get('receipt_reported'):
        return 'Пришлите чек или выписку по этой операции, где видны сумма, время и идентификатор платежа. Остальные операции и данные карты можно скрыть.', False
    return NOTICE, True


def cannot_provide_payment_details(question: str, history: list) -> bool:
    text = question.strip().casefold().rstrip('.!?')
    if re.search(r'не могу[^.!?]{0,55}(?:чек|выписк|email|почт)|нет\s+(?:чека|выписки|доступа к почте)|не помню[^.!?]{0,30}(?:email|почт)', text):
        return True
    if text not in {'не могу', 'не знаю', 'нет возможности'}:
        return False
    previous = next((item.content or '' for item in reversed(history) if item.role == 'assistant'), '')
    return bool(re.search(r'пришлите.*(?:чек|выписк)|напишите.*(?:почт|email)', previous, re.IGNORECASE))


def smalltalk_answer(question: str) -> str | None:
    text = question.strip().casefold().rstrip('!?. 🙂😊👍')
    if text in {'что', 'не понял', 'не поняла', 'непонятно'}:
        return 'Что именно нужно пояснить?'
    if text in {'привет', 'здравствуйте', 'доброе утро', 'добрый день', 'добрый вечер'}:
        return 'Здравствуйте!'
    if text in {'спасибо', 'спасибо большое', 'благодарю', 'спс'}:
        return 'Пожалуйста!'
    if text in {'ок', 'окей', 'понял', 'ясно', 'хорошо'}:
        return 'Хорошо.'
    return None


def mandatory_handoff(question: str, history: list) -> str | None:
    text = question.casefold()
    if re.fullmatch(r'\s*(?:человек|оператор|админ)[!.?\s]*', text):
        return 'human'
    human = re.search(r'(?:позов\w*|зови|свяж\w*|соедин\w*|подключ\w*|хочу поговорить|нужен|нужна)\s+(?:меня\s+)?(?:с\s+)?(?:жив\w*\s+)?(?:человек\w*|оператор\w*|админ\w*|поддержк\w*)', text)
    if human and not re.search(r'не\s*$', text[:human.start()]):
        return 'human'
    if re.search(r'передайте\s+(?:это\s+)?обращение|передайте\s+.*команде', text):
        return 'human'
    if re.search(r'(?:хочу|прошу|можно|нужно)\s+(?:сделать\s+)?(?:возврат|вернуть деньги)|верните\s+(?:мне\s+)?деньги|\bвоз[а-яё]{0,3}в?рат[а-яё]*\s+(?:денег|средств|оплат)', text) or re.fullmatch(r'\s*воз[а-яё]{0,3}в?рат[а-яё]*[.!?\s]*', text):
        return 'refund'
    if re.search(r'сотрудничеств|предложени\w*\s+(?:о\s+)?реклам', text) and not re.search(r'условия реферальн|как.*реферальн', text):
        return 'partnership'
    if re.search(r'списал\w*.*(?:продлен|продлён|продление)', text) and re.search(r'(?:срок|подписк).*не (?:измен|продл)', text):
        return 'payment_dispute'
    previous = ' '.join(getattr(item, 'content', '') or '' for item in history[-4:]).casefold()
    technical = bool(re.search(r'vpn|впн|сервер|приложени|incy|happ|подключени', text + ' ' + previous))
    failed = bool(re.search(r'не помог\w*|вс[её] равно не работает|не заработал', text))
    actions = sum(bool(re.search(pattern, text)) for pattern in (
        r'перезапуст|перезагруз', r'сервер', r'wi.?fi|мобильн', r'обновил', r'заново|повторн\w* импорт',
    ))
    if technical and failed and (actions >= 2 or re.search(r'вс[её]\s+(?:это\s+)?(?:уже\s+)?(?:пробовал|сделал)', text)):
        return 'diagnostics_failed'
    return None


def handoff_answer(reason: str, question: str) -> str:
    if reason == 'partnership':
        return 'Спасибо за предложение о сотрудничестве! Передаю его команде, пожалуйста, подождите.'
    if not re.search(r'[а-яё]', question, re.IGNORECASE):
        return 'I am passing your request to the support team for review. Please wait.'
    return NOTICE


def apps_answer(question: str, history: list) -> str | None:
    text = question.casefold()
    previous = ' '.join(getattr(item, 'content', '') or '' for item in history[-4:]).casefold()
    missing = 'happ' in text and bool(re.search(r'нет|не (?:найти|вижу|отображ|доступ)', text)) and 'app store' in text
    app_request = bool(re.search(r'каки\w* приложени|како\w* (?:приложени|клиент)|что скачать|приложени\w* скачать', text))
    followup = bool(re.match(r'^а (?:для|на)\b', text)) and bool(re.search(r'каки\w* приложени|incy|shadowrocket|v2rayng', previous))
    if not (app_request or followup or missing):
        return None
    if missing:
        return 'Если Happ не отображается в App Store, используйте Incy, Shadowrocket или Streisand. Доступность приложения зависит от региона магазина.'
    patterns = {'Android': r'android|андроид', 'iPhone/iPad': r'iphone|ipad|ios|айфон', 'Windows': r'windows|виндов', 'macOS': r'macos|макбук|mac\b'}
    platforms = [platform for platform, pattern in patterns.items() if re.search(pattern, text)] or list(CLIENTS)
    return '\n'.join(f'{platform}: {", ".join(CLIENTS[platform])}.' for platform in platforms)


def is_payment_missing(question: str) -> bool:
    return bool(re.search(
        r'(?:деньги|плат[её]ж|пополнени\w*|оплата)\s+не\s+(?:приш|поступ|зачис|появ)|не\s+(?:приш|поступ|зачис|появ)\w*\s+(?:деньги|плат[её]ж|оплата)|баланс\s+не\s+попол|не\s+пополни\w*\s+баланс',
        question, re.IGNORECASE,
    ))


def is_topup_without_renewal(question: str) -> bool:
    return bool(re.search(r'(?<!не )пополнил\w*\s+баланс', question, re.IGNORECASE)) and bool(
        re.search(r'подписк\w*\s+не\s+продл|не\s+продл\w*\s+подписк', question, re.IGNORECASE)
    ) and not re.search(r'списал|списан|авто|статус|когда|достат|не приш|не зачис', question, re.IGNORECASE)


def compact_knowledge(knowledge: list[dict], max_chars: int = 2400) -> list[dict]:
    result = []
    remaining = max_chars
    for item in knowledge:
        content = (item.get('content') or '')[:min(1200, remaining)]
        if not content or remaining < 100:
            break
        content = re.sub(r'(?:https?|vless|vmess|trojan|ss|happ|incy)://[^\s<>"\x27]+', '[ссылка из прошлого обращения скрыта]', content)
        content = re.sub(r'\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', '[email из прошлого обращения скрыт]', content, flags=re.IGNORECASE)
        content = content[:remaining]
        result.append({**item, 'content': content})
        remaining -= len(content)
    return result


def deduplicate_answer(answer: str) -> str:
    answer = answer.strip()
    for _ in range(3):
        match = re.fullmatch(r'(.{80,}?)\s*\1', answer, re.DOTALL)
        if not match:
            break
        answer = match[1].strip()
    return answer


def unsupported_price(answer: str, question: str, catalog: str) -> bool:
    if not re.search(r'стоит|стоимост|цен[ауы]|поч[её]м', question, re.IGNORECASE):
        return False

    def amounts(text):
        values = re.findall(r'((?:\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:[.,]\d{1,2})?)\s*(?:₽|руб\w*)', text, re.IGNORECASE)
        return {Decimal(''.join(value.split()).replace(',', '.')) for value in values}

    return bool(amounts(answer) - amounts(catalog))
