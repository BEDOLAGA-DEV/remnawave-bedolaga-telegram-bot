import re
from decimal import Decimal
from difflib import SequenceMatcher
from html import unescape

from ai_support_bot.app.navigation.query import (
    is_channel_followup,
    is_connection_problem,
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


APP_STORE_REGION_HELP = (
    'Если Incy или Happ недоступны в вашем App Store, попробуйте сменить регион Apple Account:\n\n'
    '1️⃣ Откройте App Store и нажмите на иконку профиля.\n'
    '2️⃣ Нажмите на своё имя или учётную запись.\n'
    '3️⃣ Выберите «Страна/регион».\n'
    '4️⃣ Нажмите «Изменить страну или регион».\n'
    '5️⃣ Выберите нужную страну, например Казахстан или США.\n'
    '6️⃣ Если потребуется способ оплаты, выберите «Нет», если этот вариант доступен.\n'
    '7️⃣ Если потребуется адрес, заполните данные выбранной страны.\n\n'
    'Пример формата полей (значения не проверены как действительный платёжный адрес):\n'
    '➖ Улица / Адрес: Нур-Султан / 294J+W6\n'
    '➖ Город: Нур-Султан / Alaska\n'
    '➖ Регион / Штат: Pavlodar / Аляска\n'
    '➖ Почтовый индекс: 101000 / 99756\n'
    '➖ Телефон: 9999999999\n'
    'Для своего аккаунта используйте действительные адрес и телефон; город, регион и индекс должны соответствовать выбранной стране.\n\n'
    'Затем снова откройте ссылку App Store в инструкции вашей подписки и попробуйте установить Incy или Happ. '
    'Меняется регион магазина Apple Account, а не язык или регион iPhone.\n\n'
    'Смене региона могут мешать остаток средств, подписки Apple или семейный доступ. '
    'Условия и инструкция Apple: https://support.apple.com/ru-ru/118283'
)

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
    if cancellation_request(question):
        return 'payment_review'
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
    if current is None and state.get('issue') in {'payment_missing', 'payment_activation', 'wheel_reward'}:
        return False
    if state.get('issue') == 'wheel_reward' and current in {None, 'promocode', 'web_wheel_history'}:
        return False
    previous = 'payment' if state.get('issue') == 'payment_missing' else next(
        (topic for item in reversed(history) if item.role == 'user' and (topic := conversation_topic(item.content or ''))), None,
    )
    if current and previous:
        return current != previous
    return bool(history) and starts_unrelated_question(question)


def keeps_account_scope(question: str) -> bool:
    return bool(re.search(r'баланс|подписк|аккаунт|email|почт|ссылк|плат[её]ж|операци|данные|тот же|этот же|\bтам\b|^\s*(?:да|нет|покажи|проверь)\b', question, re.IGNORECASE))


def starts_technical_issue(question: str) -> bool:
    return (is_connection_problem(question) or (bool(re.search(r'vpn|впн|incy|happ|сервер|интернет', question, re.IGNORECASE)) and bool(
        re.search(r'не работает|не подключ|ошибк|нет интернет|обрыв', question, re.IGNORECASE)
    ))) and not re.search(r'плат[её]ж|оплат|попол|деньги|списал', question, re.IGNORECASE)


def payment_next_step(state: dict) -> tuple[str, bool]:
    if all(state.get(key) for key in ('payment_amount', 'payment_time', 'payment_operation_id')):
        return NOTICE, True
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
    recent_user = ' '.join(getattr(item, 'content', '') or '' for item in history[-6:] if item.role == 'user')
    payment_context = text + ' ' + recent_user.casefold()
    charge_pattern = r'спис\w*|снима\w*|снял\w*|снят\w*'
    denial_pattern = r'не\s+(?:подписывал\w*|подключал\w*|оформлял\w*|заказывал\w*|давал\w* согласия)|без\s+(?:моего\s+)?согласия|незнаком\w* списан|не\s+мо[яё]\s+подписк'
    if (
        re.search(charge_pattern, payment_context)
        and re.search(denial_pattern, payment_context)
        and (re.search(charge_pattern, text) or re.search(denial_pattern, text) or manual_review_followup(question))
    ):
        return 'unrecognized_charge'
    if cancellation_request(question) or (manual_review_followup(question) and cancellation_request(recent_user)):
        return 'subscription_cancellation'
    if subscription_restricted(question):
        return 'subscription_restricted'
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


def cancellation_request(question: str) -> bool:
    text = question.casefold()
    if re.search(r'\bне\s+(?:хочу\s+)?(?:отмен|отключ|отпис)', text) or re.match(r'^\s*как\b', text):
        return False
    action = re.search(r'\b(?:отмените|отключите|уберите|отпишите|прекратите)\b|(?:хочу|прошу|можете|пожалуйста)\s+(?:мне\s+)?(?:отменить|отключить|отписать|прекратить)', text)
    return bool(action and re.search(r'подписк|авто\w*плат|авто\w*продл|списан|списыва|снимают|отпишите\s+меня', text))


def manual_review_followup(question: str) -> bool:
    return bool(re.fullmatch(r'[?\s]+|\s*(?:уже\s+)?(?:отменили|отключили|отписали|ну что|не пользуюсь|я (?:им )?не пользуюсь|я не подписывался)[?!.\s]*', question, re.IGNORECASE))


HAPP_RECOVERY = (
    'Для Happ попробуйте ещё так:\n'
    '• Настройки → Пинг → выберите TCP.\n'
    '• Сохраните свою ссылку подписки. В самом конце настроек: Сброс → Сброс пользовательских настроек.\n'
    '• Затем обновите подписку в приложении и перезапустите устройство.\n\n'
    'Если после этих шагов не заработает, напишите — передам обращение команде.'
)


def happ_recovery_step(question: str, history: list, state: dict) -> tuple[str | None, str | None]:
    progress = dict(state.get('diagnostics') or {})
    failed = bool(re.search(r'не помог\w*|не работает|не заработал|та же ошибк|вс[её] равно|не уда[её]тся', question, re.IGNORECASE))
    previous = next((item.content or '' for item in reversed(history) if item.role == 'assistant'), '')
    context = question + ' ' + previous + ' ' + ' '.join(item.content or '' for item in history if item.role == 'user')
    if not progress.get('awaiting_app') and not re.search(r'\b(?:vpn|впн|happ|incy|хапп|хэпп)\b|сервер', context, re.IGNORECASE):
        return None, None
    basic_advice = bool(re.search(r'обнов\w*|перезапуст\w*|перезагруз\w*|друг\w* сервер|смен\w* сервер', previous, re.IGNORECASE))
    if not failed and not progress.get('awaiting_app'):
        return None, None
    client = None
    for item in [question] + [item.content or '' for item in reversed(history) if item.role == 'user'] + [previous]:
        pattern = r'\b(?:happ|incy|хапп|хэпп|инси|v2rayng|streisand|shadowrocket)\b'
        stripped = re.sub(r'\bне\s+(?:в\s+)?' + pattern, '', item, flags=re.IGNORECASE)
        names = re.findall(pattern, stripped, re.IGNORECASE)
        if names:
            if len({name.casefold() for name in names}) == 1:
                client = 'happ' if names[0].casefold() in {'happ', 'хапп', 'хэпп'} else 'other'
            break
        if re.search(pattern, item, re.IGNORECASE):
            break
    attempted = progress.get('happ_recovery_offered') or bool(re.search(r'пинг.*tcp|tcp.*пинг|сброс.*(?:пользовательск|настро)', previous + '\n' + question, re.IGNORECASE | re.DOTALL))
    if attempted and failed:
        return None, 'diagnostics_failed'
    if not (basic_advice or progress.get('awaiting_app') or re.search(r'вс[её].*(?:пробовал|сделал)', question, re.IGNORECASE)):
        return None, None
    if client == 'happ':
        progress.update(happ_recovery_offered=True, awaiting_app=False)
        state['diagnostics'] = progress
        return HAPP_RECOVERY, None
    if client is None and not progress.get('awaiting_app'):
        progress['awaiting_app'] = True
        state['diagnostics'] = progress
        return 'Какое VPN-приложение вы используете? Это нужно, чтобы предложить следующий шаг именно для него.', None
    if client is None and progress.get('awaiting_app') and (failed or re.search(r'не знаю|не помню', question, re.IGNORECASE)):
        return None, 'diagnostics_failed'
    return None, None


def subscription_restricted(text: str) -> bool:
    return bool(re.search(r'подписк\w*\s+(?:(?:временно|сейчас)\s+)?(?:ограничена|заблокирована)\b', text, re.IGNORECASE))


def repeated_diagnostics(answer: str, question: str, history: list, has_image: bool = False) -> bool:
    if re.search(r'\bповтори(?:те)?\b|ещ[её] раз.*(?:инструкц|шаг)', question, re.IGNORECASE):
        return False
    if not has_image and not re.search(r'не работает|не помог\w*|не заработал|та же ошибк|вс[её] равно', question, re.IGNORECASE):
        return False
    previous = next((item.content or '' for item in reversed(history) if item.role == 'assistant'), '')
    if not previous or not re.search(r'vpn|впн|сервер|incy|happ|приложени|подключ', previous + ' ' + question, re.IGNORECASE):
        return False

    def normalized(text):
        return ' '.join(re.findall(r'[а-яёa-z0-9]+', unescape(re.sub(r'<[^>]*>', '', text)).casefold()))

    current, prior = normalized(answer), normalized(previous)
    if len(current) >= 60 and SequenceMatcher(None, current, prior).ratio() >= 0.82:
        return True
    actions = (
        r'перезапуст|перезагруз', r'обнов\w*.*(?:сервер|подписк)|(?:сервер|подписк).*обнов',
        r'(?:друг\w*|смен\w*|выбер\w*).*сервер', r'wi.?fi|мобильн\w* сет|сотов\w*',
        r'импорт|заново добав|повторно добав', r'переустанов',
        r'пинг.*tcp|tcp.*пинг', r'сброс.*настро',
    )
    current_actions = {i for i, pattern in enumerate(actions) if re.search(pattern, current)}
    prior_actions = {i for i, pattern in enumerate(actions) if re.search(pattern, prior)}
    return len(current_actions) >= 2 and current_actions <= prior_actions


def handoff_answer(reason: str, question: str) -> str:
    if reason == 'partnership':
        return 'Спасибо за предложение о сотрудничестве! Передаю его команде, пожалуйста, подождите.'
    if reason == 'subscription_cancellation':
        return 'Передаю запрос на отмену подписки команде для ручной проверки. Отмена пока не подтверждена.'
    if reason == 'unrecognized_charge':
        return 'Передаю обращение о незнакомых списаниях команде для ручной проверки и запроса на отключение. Отмена и возврат пока не подтверждены.'
    if not re.search(r'[а-яё]', question, re.IGNORECASE):
        return 'I am passing your request to the support team for review. Please wait.'
    return NOTICE


def apps_answer(question: str, history: list) -> str | None:
    text = question.casefold()
    previous = ' '.join(getattr(item, 'content', '') or '' for item in history[-4:]).casefold()
    store_context = bool(re.search(r'app\s*store|апп\s*стор|апстор|эпп\s*стор|iphone|ipad|ios|айфон|айпад', text + ' ' + previous))
    missing = (
        bool(re.search(r'\b(?:happ|incy)\b', text + ' ' + previous))
        and bool(re.search(r'нет|недоступ|не (?:найти|вижу|отображ|доступ|могу (?:скачать|установить|найти))|не (?:скачива|устанавлива|наход)|не получается (?:скачать|установить|найти)', text))
        and store_context
    )
    app_request = bool(re.search(r'каки\w* приложени|како\w* (?:приложени|клиент)|что скачать|приложени\w* скачать', text))
    followup = bool(re.match(r'^а (?:для|на)\b', text)) and bool(re.search(r'каки\w* приложени|incy|shadowrocket|v2rayng', previous))
    if not (app_request or followup or missing):
        return None
    if missing:
        prefix = (
            'Для подключения этой VPN-подписки на iPhone нужен VPN-клиент. Веб-кабинет не заменяет приложение.\n\n'
            if re.search(r'без приложени|веб.?верси|web.?верси', text) else ''
        )
        return prefix + APP_STORE_REGION_HELP
    patterns = {'Android': r'android|андроид', 'iPhone/iPad': r'iphone|ipad|ios|айфон', 'Windows': r'windows|виндов', 'macOS': r'macos|макбук|mac\b'}
    platforms = [platform for platform, pattern in patterns.items() if re.search(pattern, text)] or list(CLIENTS)
    return '\n'.join(f'{platform}: {", ".join(CLIENTS[platform])}.' for platform in platforms)


def wheel_reward_request(question: str, history: list, state: dict) -> bool:
    text = question.casefold()
    context = ' '.join(item.content or '' for item in history[-8:]).casefold()
    wheel = bool(re.search(r'колес|wheel|вращен|выигр|выпал\w*.*(?:код|приз)', text + ' ' + context))
    reward = bool(re.search(r'промо|промик|\bкод\w*|приз', text + ' ' + context))
    recovery = bool(re.search(r'не успел|потер|пропал|исчез|скопир|восстанов|скин|пришл|получ|забрать|где.*(?:посмотр|найти|код)|покаж|верн', text))
    return (wheel and reward and recovery) or (state.get('issue') == 'wheel_reward' and bool(re.search(r'нет|скрин|код|где|пришл|повтор|найти', text)))


def paid_access_request(question: str) -> bool:
    text = question.casefold()
    paid = bool(re.search(r'оплатил|оплач|заплатил|(?:оплат\w*|плат[её]ж).*(?:произвед|прош[её]л|прошла|соверш)|деньги.*спис', text))
    access = bool(re.search(r'включ|активир|доступ|не работ|не подключ|не продл|ист[её]к|не обнов|0\s*дн|нет.*подпис', text))
    installation = bool(re.search(r'установ|скач|app\s*store|апп.?стор', text))
    return paid and access and not installation and not is_topup_without_renewal(question)


def initial_greeting(answer: str, first_reply: bool) -> str:
    visible = re.sub(r'<[^>]*>', '', answer).lstrip()
    if first_reply and not re.match(r'здравств|привет|добр(?:ый|ое|ой)\b|hello\b|hi\b', visible, re.IGNORECASE):
        return 'Здравствуйте!\n\n' + answer
    return answer


def is_payment_missing(question: str) -> bool:
    return bool(re.search(
        r'(?:деньги|плат[её]ж|пополнени\w*|оплата)\s+не\s+(?:приш|поступ|зачис|появ|отраз)|не\s+(?:приш|поступ|зачис|появ|отраз)\w*\s+(?:деньги|плат[её]ж|оплата)|баланс\s+не\s+попол|не\s+пополни\w*\s+баланс|(?:денег|средств)\s+на\s+балансе\s+нет',
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
