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
from ai_support_bot.app.services.service_identity import renewal_payment_request


NOTICE = 'Передаю обращение команде для проверки, подождите, пожалуйста.'
SUPPORT_SCOPE_ANSWER = 'Я помогаю с SligVPN: подключением, подписками, оплатой и настройкой VPN-клиентов. Напишите, какой вопрос по сервису нужно решить.'
# Shadowrocket is left out on purpose: it does not send the HWID the device limit relies on (not told to customers).
CLIENTS = {
    'Android': ('Incy', 'Happ', 'V2RayNG'),
    'iPhone/iPad': ('Incy', 'Happ', 'Streisand'),
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
    creation = r'(?:напиш\w*|напис\w*|сдела\w*|созда\w*|разработа\w*|реализ\w*|сгенерир\w*|write|build|implement|create|generate)'
    modifiers = r'(?:(?:мне|нам|пожалуйста|простой|простую|небольшой|полный|новый|свой|на|in|a|an|the|simple|basic|python|javascript)\s+){0,5}'
    product = r'(?:калькулятор|код\b|скрипт|программ\w*|бота\b|сайт\b|приложени\w*|calculator|script|code|app\b|website|bot\b)'
    request = re.search(creation + r'\s+' + modifiers + product, question, re.IGNORECASE)
    if request:
        target = question[request.start():request.end() + 25]
        url_request = re.search(r'напиш\w*\s+(?:мне\s+)?сайт\b', question, re.IGNORECASE) and not re.search(r'html|javascript|калькулятор|исходн\w*\s+код', question, re.IGNORECASE)
        if not url_request and not re.search(r'код (?:ошибки|подключения|подтверждения)|промокод', target, re.IGNORECASE):
            return SUPPORT_SCOPE_ANSWER
    if re.search(r'сочини|напиши|придумай|compose|write', question, re.IGNORECASE) and re.search(r'стих|сказк|анекдот|рецепт|эссе|poem|story|recipe|essay|joke', question, re.IGNORECASE):
        if not re.search(r'slig|vpn|впн|подписк|подключ|оплат', question, re.IGNORECASE):
            return SUPPORT_SCOPE_ANSWER
    return None


def generated_program(answer: str) -> bool:
    """Reject software generation while allowing VPN configuration, logs and commands."""
    answer = re.sub(r'</?(?:code|pre|b|i|strong|em)(?:\s[^>]*)?>', '\n', answer, flags=re.IGNORECASE)
    return bool(re.search(
        r'(?m)^\s*(?:def\s+\w+\s*\(|class\s+\w+\s*[:(]|(?:async\s+)?function\s+\w+\s*\(|'
        r'(?:const|let|var)\s+\w+\s*=|public\s+(?:static\s+)?(?:class|void)\b|(?:print|input|console\.log)\s*\()|'
        r'<!doctype\s+html|<html\b|#include\s*[<"]|```\s*(?:python|javascript|typescript|java|cpp|csharp)\b',
        answer, re.IGNORECASE,
    ))


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
    if state.get('renewal_verification') and renewal_payment_request(question, history, pending=True):
        return False
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


_CHARGE = r'спис\w*|снима\w*|снял\w*|снят\w*'
# «Вернуть», «верните», «вернутся», but not «верно».
_RETURN = r'верн(?:и|ите|уть|ут|утся|[её]т|[её]те|[её]тся|ул\w*)\b'
_REFUND = re.compile(
    r'(?:хочу|прошу|можно|нужно|могу|можете)\s+(?:ли\s+)?(?:как[\s-]?(?:то|нибудь)\s+)?(?:сделать\s+|оформить\s+|получить\s+)?(?:возврат|вернуть деньги)'
    r'|верните\s+(?:мне\s+)?деньги'
    r'|' + _RETURN + r'\s+(?:[\w-]+\s+){0,4}?(?:деньг|денег|денежн|средств|оплат[уы]\b|плат[её]ж)'
    r'|\bвоз[а-яё]{0,3}в?рат[а-яё]*\s+(?:\w+\s+)?(?:денег|средств|оплат|част|неиспольз|за\s+подписк)'
    r'|(?:есть|возможен|возможно|будет|предусмотрен\w*)\s+ли\s+(?:\w+\s+)?возврат|возврат\w*\s+(?:возмож|предусмотр)'
)
_PAID = r'оплатил\w*|заплатил\w*|списал\w*|списан\w*|списыва\w*|снял\w*|сняли|оплат[аыу]\b|плат[её]ж\w*'
_DOUBLE_PAYMENT = re.compile(
    r'(?:' + _PAID + r')\s+(?:[\w-]+\s+){0,3}?(?:дважды|два\s+раза|2\s+раза)\b'
    r'|(?:дважды|два\s+раза|2\s+раза)\s+(?:[\w-]+\s+){0,2}?(?:' + _PAID + r')|двойн\w*\s+(?:оплат|списан|плат[её]ж)'
)
_WRONG_TARIFF = re.compile(
    r'(?:случайно|по\s+ошибке|ошибочно|нечаянно)\s+(?:[\w-]+\s+){0,2}?(?:купил|оплатил|оформил|выбрал|взял|продлил|подключил|заплатил)'
    r'|(?:купил|оплатил|оформил|взял|продлил)\w*\s+не\s+т(?:от|у|ой|е)\b|не\s+т(?:от|у)\s+(?:тариф|подписк|период)'
)
_ACCOUNT = r'аккаунт|\bакк\b|\bтг\b|telegram|телеграм|профил'


def account_transfer_request(question: str) -> bool:
    """A hacked or lost account, or a wish to move the subscription to another account: only the team can do it.

    Moving the subscription to a new phone is a device question and stays self-service.
    """
    text = question.casefold()
    hacked = re.search(r'взломал\w*|взломан\w*|(?:после|из-за)\s+взлом\w*|угнал\w*', text) and re.search(_ACCOUNT, text)
    transfer = re.search(r'перен(?:ести|есите|ес[её]те|ос\w*|[её]с\w*)\s+(?:[\w-]+\s+){0,3}?(?:подписк|впн|vpn|тариф|баланс|срок|дн[иея]\b)', text) and re.search(
        _ACCOUNT + r'|@\w+|айди|\bid\b|почт|email', text)
    lost = re.search(r'(?:потерял\w*|утерял\w*|нет\s+доступа\s+к)\s+(?:доступ\w*\s+к\s+)?(?:сво\w+\s+|стар\w+\s+)?(?:тг|telegram|телеграм|аккаунт)'
                     r'|(?:тг|telegram|телеграм|аккаунт\w*)\s+(?:[\w-]+\s+)?(?:потерял|утерял)', text)
    return bool(hacked or transfer or lost)


_AUTO_RENEWAL = r'автопродл\w*|автоплат\w*|автосписан\w*|авто\s*-?\s*(?:продл|плат|списан)\w*|продлил\w*|продлен\w*|продлён\w*'
_UNWANTED = (
    r'не\s+(?:нужн\w*|нужен|просил\w*|хотел\w*|включал\w*|заказывал\w*|планировал\w*|собирал\w*)'
    r'|без\s+(?:моего\s+)?(?:спроса|согласия|запроса|предупреждени\w*)'
)
# The charge already happened: «пришло», «списали», «10₽»; «чтобы не списали» is only a wish.
_HAPPENED = r'(?<!не\s)(?<!бы\s)\b(?:спис\w*|снял\w*|сняли|снят\w*|пришл\w*|продлил\w*|продлен\w*|продлён\w*)|\d\s*(?:₽|р\b|руб)'


def unwanted_renewal(question: str) -> bool:
    """«10₽ пришло автопродление, хотя мне не нужно»: a renewal the customer did not want is a money dispute.

    «Мне не нужно автопродление, как отключить?» has no charge yet and stays self-service.
    """
    text = question.casefold()
    return bool(re.search(_AUTO_RENEWAL + '|' + _CHARGE, text) and re.search(_UNWANTED, text) and re.search(_HAPPENED, text))


_DEVICES_EXCEEDED = re.compile(
    r'(?:превыш\w*|макс\w*\.?|достигнут\w*)\s+(?:[\w.-]+\s+){0,3}?устройств|устройств\w*\s+(?:[\w-]+\s+){0,2}?(?:превыш|достигнут)\w*'
)
DEVICES_EXCEEDED_HELP = (
    'Ошибка «превышено число устройств» значит, что все места подписки заняты. Удалите старое устройство: '
    'в боте — ГЛАВНОЕ МЕНЮ → Мои подписки → выберите подписку → Устройства → Управление устройствами; '
    'на сайте — https://web.slig.app/subscriptions → подписка → Мои устройства. Затем снова нажмите подключение в приложении.'
)
AUTOPAY_OFF_HELP = (
    'Чтобы следующего списания не было, выключите автоплатёж: в боте https://t.me/SligVPN_bot — Мои подписки → '
    'выберите подписку → Автоплатеж → Выключить; на сайте https://web.slig.app/subscriptions — переключатель '
    '«Автопродление» на карточке подписки.'
)


def devices_exceeded(question: str) -> bool:
    """«Макс. число устройств превышено», «пишет максимальное количество устройств»: every place is taken."""
    return bool(_DEVICES_EXCEEDED.search(question.casefold()))


def money_handoff(question: str) -> str | None:
    """Double payment, a wrong tariff or an account transfer always go to the team with their own reason."""
    text = question.casefold()
    if _DOUBLE_PAYMENT.search(text):
        return 'payment_dispute'
    if _WRONG_TARIFF.search(text):
        return 'wrong_tariff'
    return None


def money_dispute_topic(question: str) -> bool:
    """Refunds, cancellations, disputed charges and account transfers: no emoji, no buttons and never «Продлить».

    Wider than the forced handoff: «как отключить автопродление?» is answered by the model, but still gets no renew button.
    """
    text = question.casefold()
    return bool(
        _REFUND.search(text) or money_handoff(question) or cancellation_request(question) or account_transfer_request(question)
        or unwanted_renewal(question)
        or re.search(r'возврат|отмен\w*|отпис\w*|(?:отключ|выключ|убр)\w*\s+(?:[\w-]+\s+)?(?:авто\w*|подписк|списан)|спорн\w*|чарджб'
                     r'|спис(?:ал|ан|ыв)\w*|(?:деньг|денег|средств|руб|₽).{0,40}(?:снял|сняли|снят|снима)'
                     r'|(?:снял\w*|сняли|снят\w*|снима\w*).{0,40}(?:деньг|денег|средств|руб|₽|карт)', text)
    )


_TRIED = re.compile(
    r'уже\s+(?:вс[её]\s+)?(?:пробовал\w*|попробовал\w*|перепробовал\w*|делал\w*|сделал\w*)|перепробовал\w*'
    r'|пробовал\w*\s+(?:через\s+|в\s+|на\s+)?(?:разн\w*|все|вс[её]|други\w*|несколько)'
    r'|не\s+помог\w*|не\s+помогает|ничего\s+не\s+(?:помог\w*|измен\w*|поменял\w*|меня\w*)|вс[её]\s+равно\s+не\b'
)
_LONG_OUTAGE = re.compile(
    r'(?<!на\s)(?<!в\s)(?:уже\s+|больше\s+|более\s+|около\s+|почти\s+|целую\s+|всю\s+)?недел[юиья]\b'
    r'|(?<!на\s)(?:\d+|два|три|четыре|пять|несколько|пару|пара)(?:-?(?:х|й|ий|ой|ый|ти|е))?\s+(?:дн(?:я|ей)|день|сут(?:ки|ок))\b(?!\s+назад)'
    r'|(?:второй|третий|четв[её]ртый|пятый)\s+день|(?:целый|весь)\s+день|(?:уже|больше|более|почти|целый)\s+месяц\w*'
    r'|давно\s+(?:уже\s+)?не\s+(?:работа|подключ|грузит|открыва)'
)
_TECH_FAILURE = re.compile(
    r'не\s+работа\w*|не\s+подключ\w*|не\s+грузит|не\s+загружа\w*|не\s+открыва\w*|не\s+заработал\w*|недоступн\w*'
    r'|(?:плохо|ужасно)\s+работа\w*|нет\s+интернет\w*|\bn[\\/]a\b|н/д'
)
_BASIC_STEPS = (
    r'(?:смен|друг|выбер|выбра|переключ)\w*[^.\n]{0,30}сервер|сервер\w*[^.\n]{0,20}(?:смен|друг)',
    r'обнов\w*[^.\n]{0,30}(?:подписк|список|сервер)',
    r'перезапуст|перезагруз',
    r'wi.?fi|мобильн\w*\s+(?:сет|интернет)|сотов',
)


def basic_steps_given(answer: str) -> bool:
    """The usual «смените сервер / обновите подписку / перезапустите» template."""
    text = (answer or '').casefold()
    return sum(bool(re.search(pattern, text)) for pattern in _BASIC_STEPS) >= 2


def diagnostics_exhausted(question: str, history: list) -> bool:
    """«Уже пробовал», «неделю не работает» or a new «не работает» after the template: a new round of the same steps won't help."""
    text = question.casefold()
    # Payments have their own checks; a client missing from the App Store gets the region instructions.
    if re.search(r'плат[её]ж|оплат|попол|деньг|списал|чек\b|app\s*store|апп?\s*стор|аппстор|эпп\s*стор|google\s*play|плей\s*маркет', text):
        return False
    if _TECH_FAILURE.search(text) and (_TRIED.search(text) or _LONG_OUTAGE.search(text)):
        return True
    if _TRIED.search(text) and _LONG_OUTAGE.search(text):
        return True
    previous = next((getattr(item, 'content', '') or '' for item in reversed(history) if item.role == 'assistant'), '')
    return basic_steps_given(previous) and bool(_TECH_FAILURE.search(text) or _TRIED.search(text) or re.search(r'та\s+же\s+ошибк|то\s+же\s+самое', text))


def mandatory_handoff(question: str, history: list) -> str | None:
    text = question.casefold()
    recent_user = ' '.join(getattr(item, 'content', '') or '' for item in history[-6:] if item.role == 'user')
    payment_context = text + ' ' + recent_user.casefold()
    charge_pattern = _CHARGE
    denial_pattern = (
        r'не\s+(?:подписывал\w*|подключал\w*|оформлял\w*|заказывал\w*|покупал\w*|давал\w* согласия)|без\s+(?:моего\s+)?согласия'
        r'|незнаком\w* списан|не\s+мо[яё]\s+подписк|(?:подписк\w*|услуг\w*)\s+(?:у\s+меня\s+)?(?:никогда\s+)?не\s+было'
    )
    if (
        re.search(charge_pattern, payment_context)
        and re.search(denial_pattern, payment_context)
        and (re.search(charge_pattern, text) or re.search(denial_pattern, text) or manual_review_followup(question))
    ):
        return 'unrecognized_charge'
    if unwanted_renewal(question):
        return 'unwanted_renewal'
    if reason := money_handoff(question):
        return reason
    if cancellation_request(question) or (manual_review_followup(question) and cancellation_request(recent_user)):
        return 'subscription_cancellation'
    if subscription_restricted(question):
        return 'subscription_restricted'
    if re.fullmatch(r'\s*(?:человек|оператор|админ)[!.?\s]*', text):
        return 'human'
    human = re.search(r'(?:позов\w*|зови|свяж\w*|соедин\w*|подключ\w*|хочу поговорить|нужен|нужна)\s+(?:меня\s+)?(?:с\s+)?(?:жив\w*\s+)?(?:человек\w*|оператор\w*|админ\w*|поддержк\w*)', text)
    if human and not re.search(r'не\s*$', text[:human.start()]):
        return 'human'
    if re.search(r'(?:можно|может|хочу|пусть).{0,35}(?:ответит|ответить|поговорить).{0,20}(?:не\s+бот|человек|сотрудник)|(?:можно|хочу)\s+не\s+бота?\b', text):
        return 'human'
    if re.search(r'передайте\s+(?:это\s+)?обращение|передайте\s+.*команде', text):
        return 'human'
    if _REFUND.search(text) or re.fullmatch(r'\s*воз[а-яё]{0,3}в?рат[а-яё]*[.!?\s]*', text):
        return 'refund'
    if account_transfer_request(question):
        return 'account_transfer'
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
    if diagnostics_exhausted(question, history):
        return 'diagnostics_failed'
    return None


def cancellation_blocked(text: str) -> bool:
    """«Не отменяется подписка», «не могу отменить, а деньги списывают»; a stuck autopay toggle alone stays self-service."""
    text = text.casefold()
    if not re.search(r'не\s+отмен\w*ся|не\s+отключа\w*ся|не\s+(?:получается|получилось|удаётся|удается|удалось|могу|смог\w*|выходит)\s+(?:[\w-]+\s+)?(?:отменить|отключить|отписаться|отказаться)', text):
        return False
    subscription = re.search(r'подписк|услуг', text) and not re.search(r'автопл|автопрод|авто\s*списан', text)
    return bool(subscription or re.search(_CHARGE + r'|деньг', text))


def cancellation_request(question: str) -> bool:
    text = question.casefold()
    if cancellation_blocked(text):
        return True
    if re.search(r'\bне\s+(?:хочу\s+)?(?:отмен|отключ|отпис)', text) or re.match(r'^\s*как\b', text):
        return False
    action = re.search(r'\b(?:отмените|отключите|уберите|отпишите|прекратите)\b|(?:хочу|прошу|можете|пожалуйста)\s+(?:мне\s+)?(?:отменить|отключить|отписать|прекратить)', text)
    # «Есть ли возможность отменить подписку?» and «Отмена вашей услуги» ask for a cancellation, not for the autopay toggle.
    possible = re.search(r'(?:можно|возможно|есть\s+(?:ли\s+)?возможность|могу|получится)\s+(?:ли\s+)?(?:как[\s-]?(?:то|нибудь)\s+)?(?:мне\s+)?'
                         r'(?:отменить|отказаться\s+от)\s+(?:[\w-]+\s+)?(?:подписк|услуг|тариф)', text)
    noun = re.match(r'^\W*отмен[аеуы]\s+(?:[\w-]+\s+)?(?:подписк|услуг)', text)
    unused = re.search(_CHARGE, text) and re.search(r'не\s+пользу(?:юсь|емся)', text)
    return bool((action and re.search(r'подписк|авто\w*плат|авто\w*продл|списан|списыва|снимают|отпишите\s+меня', text)) or possible or noun or unused)


def manual_review_followup(question: str) -> bool:
    return bool(re.fullmatch(r'[?\s]+|\s*(?:уже\s+)?(?:отменили|отключили|отписали|ну что|не пользуюсь|я (?:им )?не пользуюсь|я не подписывался)[?!.\s]*', question, re.IGNORECASE))


HAPP_RECOVERY = (
    'Для Happ попробуйте ещё так:\n'
    '• Сохраните свою ссылку подписки.\n'
    '• В самом конце настроек: Сброс → Сброс пользовательских настроек.\n'
    '• После сброса: Настройки → Пинг → выберите TCP.\n'
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


def repeated_clarification(answer: str, question: str, history: list) -> bool:
    if re.search(r'повтор|ещ[её] раз|продублир', question, re.IGNORECASE):
        return False
    if not re.search(r'\?|(?:^|[.!]\s+|\n)\s*(?:пришлите|уточните|сообщите|напишите)', answer, re.IGNORECASE):
        return False
    def normalized(text):
        return ' '.join(re.findall(r'[а-яёa-z0-9]+', unescape(re.sub(r'<[^>]*>', '', text)).casefold()))
    current = normalized(answer)
    previous = [normalized(item.content or '') for item in history if item.role == 'assistant'][-2:]
    return len(current) >= 60 and len(previous) == 2 and all(SequenceMatcher(None, current, old).ratio() >= 0.9 for old in previous)


def repeated_answer(answer: str, question: str, history: list) -> bool:
    """One earlier identical reply in the dialogue is enough: the user did not move the case forward."""
    if re.search(r'повтор|ещ[её] раз|продублир', question, re.IGNORECASE):
        return False

    def normalized(text):
        words = re.findall(r'[а-яёa-z0-9]+', unescape(re.sub(r'<[^>]*>', '', text)).casefold())
        return ' '.join(words[1:] if words[:1] == ['здравствуйте'] else words)

    current = normalized(answer)
    return bool(current) and any(normalized(item.content or '') == current for item in history if item.role == 'assistant')


def handoff_answer(reason: str, question: str) -> str:
    if reason == 'partnership':
        return 'Спасибо за предложение о сотрудничестве! Передаю его команде, пожалуйста, подождите.'
    if reason == 'subscription_cancellation':
        return 'Передаю запрос на отмену подписки команде для ручной проверки. Отмена пока не подтверждена.'
    if reason == 'unrecognized_charge':
        return 'Передаю обращение о незнакомых списаниях команде для ручной проверки и запроса на отключение. Отмена и возврат пока не подтверждены.'
    if not re.search(r'[а-яё]', question, re.IGNORECASE):
        return 'I am passing your request to the support team for review. Please wait.'
    if reason == 'unwanted_renewal':
        # The devices question from the same message is self-service: it should not wait for the team.
        devices = '\n\n' + DEVICES_EXCEEDED_HELP if devices_exceeded(question) else ''
        return 'Передаю обращение о списании за автопродление команде для проверки. Возврат пока не подтверждён.\n\n' + AUTOPAY_OFF_HELP + devices
    return NOTICE


_STORE = r'app\s*store|апп?\s*стор|аппстор|эпп\s*стор'
_APP_NAME = r'happ|incy|хапп|хэпп|инси'
_PLATFORMS = {'Android': r'android|андроид', 'iPhone/iPad': r'iphone|ipad|ios|айфон', 'Windows': r'windows|виндов', 'macOS': r'macos|макбук|mac\b'}
# The approved instructions contain this phrase: its copy in the history means they were already sent.
_REGION_HELP_MARK = 'сменить регион Apple Account'
_SHADOWROCKET = re.compile(r'shadow\s*rocket|ш[аэ]д[оа]у?\s*рокет', re.IGNORECASE)
SHADOWROCKET_ANSWER = (
    'Для подключения используйте Happ или Incy. Если на iPhone их не получается установить без смены региона Apple ID, '
    'подойдёт Streisand. Затем добавьте в приложение ссылку своей подписки.'
)


def region_help_given(history: list) -> bool:
    return any(item.role == 'assistant' and _REGION_HELP_MARK in (getattr(item, 'content', '') or '') for item in history)


def region_change_refused(question: str, history: list) -> bool:
    """«Не хочу менять регион», «деньги на балансе Apple ID», «семейный доступ»: the region change is not an option."""
    text = question.casefold()
    change = re.search(
        r'не\s+(?:хочу|могу|получается|получилось|выходит|вышло|удаётся|удается|удалось|буду|стану)\s+(?:[\w-]+\s+){0,2}?(?:меня(?:ть|ю|ем)|смен|поменя|измен)\w*'
        r'|регион\w*\s+(?:[\w-]+\s+)?не\s+(?:меня|смен|поменя|измен)\w*', text)
    obstacle = re.search(r'(?:деньг|денег|средств|остат|баланс)\w*\s+(?:[\w-]+\s+){0,2}?(?:apple|эпл)|семейн\w*\s+доступ|подписк\w*\s+(?:в\s+)?(?:apple|эпл)', text)
    region = re.search(r'регион|apple|эпл|' + _STORE, text) or region_help_given(history)
    return bool((change or obstacle) and region)


def store_alternatives_answer(question: str = '') -> str:
    """Instead of a second copy of the region instructions: the other clients of the platform, iPhone by default."""
    platform = next((name for name, pattern in _PLATFORMS.items() if re.search(pattern, question.casefold())), 'iPhone/iPad')
    apps = ', '.join(name for name in CLIENTS[platform] if name not in {'Incy', 'Happ'})
    if platform == 'iPhone/iPad':
        return (f'Если регион Apple ID сменить не получается или не хочется, установите {apps}: его можно скачать '
                'без смены региона. Затем добавьте в него ссылку своей подписки.')
    return f'{platform}: можно установить {apps} и добавить в него ссылку своей подписки.'


def apps_answer(question: str, history: list) -> str | None:
    text = question.casefold()
    previous = ' '.join(getattr(item, 'content', '') or '' for item in history[-4:]).casefold()
    if _SHADOWROCKET.search(text) and re.search(r'не\s|ошибк|поддерж|можно|подойд|подход|работа|подключ|\?', text):
        return SHADOWROCKET_ANSWER
    store_context = bool(re.search(_STORE + r'|iphone|ipad|ios|айфон|айпад', text + ' ' + previous))
    app_word = r'\b(?:' + _APP_NAME + r')\b|приложени\w*|(?:' + _STORE + r')\w*'
    # «Нет» counts only next to the app or the store: «телеграм не работает, потому что нет впн» is not about the store.
    absent = bool(re.search(r'нету?\s+(?:ни\s+)?(?:[\w-]+\s+)?(?:в\s+)?(?:' + app_word + r')|(?:' + app_word + r')\s+(?:[\w-]+\s+){0,2}?нету?\b', text)) and not re.search(
        r'не\s+(?:работа|подключ|открыва|грузит|загружа)', text)
    missing = (
        bool(re.search(r'\b(?:happ|incy)\b', text + ' ' + previous))
        and bool(re.search(app_word + r'|скач|установ', text))
        and (absent or bool(re.search(r'недоступ|не (?:найти|вижу|отображ|доступ|могу (?:скачать|установить|найти))|не (?:скачива|устанавлива|наход)|не получается (?:скачать|установить|найти)', text)))
        and store_context
    )
    refused = region_change_refused(question, history)
    app_request = bool(re.search(r'каки\w* приложени|како\w* (?:приложени|клиент)|что скачать|приложени\w* скачать', text))
    followup = bool(re.match(r'^а (?:для|на)\b', text)) and bool(re.search(r'каки\w* приложени|incy|shadowrocket|v2rayng', previous))
    if not (app_request or followup or missing or refused):
        return None
    # The region instructions go once: a refusal or a second «нет в App Store» gets the other clients.
    if refused or (missing and region_help_given(history)):
        return store_alternatives_answer(question)
    if missing:
        prefix = (
            'Для подключения этой VPN-подписки на iPhone нужен VPN-клиент. Веб-кабинет не заменяет приложение.\n\n'
            if re.search(r'без приложени|веб.?верси|web.?верси', text) else ''
        )
        return prefix + APP_STORE_REGION_HELP
    platforms = [platform for platform, pattern in _PLATFORMS.items() if re.search(pattern, text)] or list(CLIENTS)
    return '\n'.join(f'{platform}: {", ".join(CLIENTS[platform])}.' for platform in platforms)


_BLANK_PAGE = re.compile(r'бел\w*\s+(?:экран|страниц|лист)|пуст\w*\s+(?:экран|страниц)')
_PAGE_FAILED = re.compile(
    r'не\s+(?:откр|загружа|грузит|отвеча|нажима|переход)\w*|ничего\s+не\s+(?:происход|открыва|грузит)\w*'
    r'|(?:кнопк|ссылк|страниц)\w*\s+(?:[\w-]+\s+)?не\s+работа\w*|не\s+работа\w*\s+(?:кнопк|ссылк|страниц)'
)
# «Не открывается сайт / телеграм / приложение» stays a VPN case even right after a button.
_VPN_TARGET = re.compile(r'vpn|впн|сервер|telegram|телеграм|\bтг\b|ютуб|youtube|инстаграм|instagram|сайт|интернет|прилож')
_CLIENT_APP = re.compile(r'\b(?:happ|incy|хапп|хэпп|инси|v2ray\w*|streisand|v2box)\b')


def blank_page_answer(question: str, history: list) -> str | None:
    """«Белый экран» or «не открывается» after a button or a cabinet link: the page did not load, not a VPN failure."""
    text = question.casefold()
    previous = next((getattr(item, 'content', '') or '' for item in reversed(history) if item.role == 'assistant'), '')
    after_link = bool(re.search(r'кнопк|web\.slig\.app', previous, re.IGNORECASE))
    blank = bool(_BLANK_PAGE.search(text)) and not _CLIENT_APP.search(text) and (after_link or not _VPN_TARGET.search(text))
    failed = after_link and len(text) <= 80 and bool(_PAGE_FAILED.search(text)) and not _VPN_TARGET.search(text) and not _CLIENT_APP.search(text)
    if not (blank or failed):
        return None
    renew = ' → Продлить' if re.search(r'продл', previous, re.IGNORECASE) or re.search(r'продл', text) else ''
    return (
        'Похоже, страница не загрузилась. Откройте https://web.slig.app в обычном браузере (Safari или Chrome) '
        'и войдите через Telegram или по email.\n\n'
        f'Или сделайте то же в боте https://t.me/SligVPN_bot: Мои подписки → выберите подписку{renew}.'
    )


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


_ON_TOPIC = re.compile(
    r'vpn|впн|подписк|сервер|стран[аеуы]?\b|страны|локаци|оплат|тариф|устройств|приложени|подключ|прокси|proxy|сайт|сервис'
    r'|не\s+откр|не\s+работа|не\s+грузит|ютуб|youtube|telegram|телеграм|\bтг\b|инстаграм|instagram'
    r'|япони|казахстан|сша|америк|герман|нидерланд|голланд|финлянд|турци|польш|франц|англи|британ|эстони|латви|швеци|итали|испани|канад|кита|коре[яи]|грузи|армени|серби|оаэ|дубай',
    re.IGNORECASE,
)
_SCOPE_DISCLAIMER = re.compile(
    r'[ \t]*Я\s+помогаю\s+(?:только\s+)?(?:с|по)\s+(?:вопросам\s+)?SligVPN\b[^.!?\n]*[.!?]*'
    r'(?:[ \t]*\n?[ \t]*(?:Напишите|Задайте)[^.!?\n]{0,60}вопрос[^.!?\n]{0,60}[.!?]*)?',
    re.IGNORECASE,
)
_GREETING_ONLY = re.compile(r'^\W*(?:здравствуйте|привет|добр(?:ый|ое|ой)\s+\w+)?\W*$', re.IGNORECASE)


def drop_scope_disclaimer(answer: str, question: str) -> str:
    """The model writes «Я помогаю только с SligVPN» to on-topic questions too («добавьте Японию»): drop it there."""
    if not _ON_TOPIC.search(question or '') or not _SCOPE_DISCLAIMER.search(answer or ''):
        return answer
    stripped = _SCOPE_DISCLAIMER.sub('', answer)
    stripped = re.sub(r'[ \t]+\n', '\n', stripped)
    stripped = re.sub(r'\n{3,}', '\n\n', stripped).strip()
    if _GREETING_ONLY.match(re.sub(r'<[^>]*>', '', stripped)):
        return answer
    # «Здравствуйте.» that lost the rest of its line keeps its own paragraph.
    return re.sub(r'^((?:здравствуйте|привет)[.!])[ \t]+(?=\S)', r'\1\n\n', stripped, flags=re.IGNORECASE)


# Sentence start: text or line start, or after .!? — with an optional list marker, <b>/<i> and a leading emoji.
_START = r'((?:^|(?<=\n)|(?<=[.!?…])[ \t]+)[ \t]*(?:[-•*][ \t]+|\d+[.)][ \t]+)?(?:<[bi]>)?(?:[^\w\s<>.,!?]{1,3}[ \t]*)?)'
_GENITIVE = {
    'доступ': 'доступа', 'подписку': 'подписки', 'подписки': 'подписок', 'аккаунт': 'аккаунта', 'оплату': 'оплаты',
    'платёж': 'платежа', 'платеж': 'платежа', 'данные': 'данных', 'баланс': 'баланса', 'статус': 'статуса',
    'ссылку': 'ссылки', 'операции': 'операций', 'устройства': 'устройств', 'срок': 'срока',
}
_POSSESSIVE = {'ваш': 'вашего', 'вашу': 'вашей', 'ваши': 'ваших', 'вашей': 'вашей'}
# «Передал(а) X» → «X передан(о/а/ы)»: the object's own gender, never the speaker's.
_HANDED = {
    'обращение': ('обращение', 'передано'), 'запрос': ('запрос', 'передан'), 'вопрос': ('вопрос', 'передан'),
    'заявку': ('заявка', 'передана'), 'информацию': ('информация', 'передана'), 'данные': ('данные', 'переданы'),
}
_INFINITIVE = {
    'помочь': 'помогу', 'подсказать': 'подскажу', 'проверить': 'проверю', 'передать': 'передам', 'ответить': 'отвечу',
    'уточнить': 'уточню', 'объяснить': 'объясню', 'показать': 'покажу', 'разобраться': 'разберусь',
}


def _cap(word: str, upper: bool) -> str:
    return word[:1].upper() + word[1:] if upper else word


def neutral_self_reference(text: str) -> str:
    """«Понял», «Рада помочь», «Проверила:» → forms without grammatical gender. Only safe, fixed phrases are rewritten."""
    if not text:
        return text

    def start(pattern, pronoun):
        # The pronoun «я» is part of the phrase; the replacement is capitalised at a sentence start.
        return re.compile(_START + (r'я[ \t]+' if pronoun else r'(?:я[ \t]+)?') + pattern, re.IGNORECASE)

    def after_pronoun(pattern):
        return re.compile(r'(?<=[,;:—–(])([ \t]+)я[ \t]+' + pattern, re.IGNORECASE)

    def apply(pattern, make, pronoun=False):
        nonlocal text
        text = start(pattern, pronoun).sub(lambda m: m.group(1) + make(m, True), text)
        text = after_pronoun(pattern).sub(lambda m: m.group(1) + make(m, False), text)

    stop = r'(?=[ \t]*(?:[.,!:;—–)]|$))'
    apply(r'понял(?:а)?(?:[ \t]+вас)?' + stop, lambda m, up: _cap('понятно', up))
    apply(r'рад(?:а)?,[ \t]+что\b', lambda m, up: _cap('хорошо, что', up))
    # «Отлично, рад, что заработало»: after a comma the pronoun is usually dropped.
    text = re.sub(r'(?<=[,;:—–])([ \t]+)(?:очень[ \t]+)?рад(?:а)?(?=,[ \t]+что\b)', r'\1хорошо', text, flags=re.IGNORECASE)
    apply(r'рад(?:а)?[ \t]+(?:это[ \t]+)?слышать', lambda m, up: _cap('отлично', up))
    apply(r'(?:проверил|посмотрел)(?:а)?' + r'(?=[ \t]*[:—–])', lambda m, up: _cap('проверка показала', up))
    apply(r'(?:проверил|посмотрел)(?:а)?[ \t]+(по[ \t]+(?:вашему[ \t]+)?(?:аккаунту|данным))(?=[ \t]*[:—–])',
          lambda m, up: _cap('проверка ', up) + m.group(2) + ' показала')

    def checked(m, up):
        owner, noun = (m.group(2) or '').casefold(), m.group(3).casefold()
        return _cap('проверка ', up) + (_POSSESSIVE[owner] + ' ' if owner else '') + _GENITIVE[noun] + ' показала'

    nouns = '|'.join(sorted(_GENITIVE, key=len, reverse=True))
    apply(r'проверил(?:а)?[ \t]+(?:(ваш|вашу|ваши|вашей)[ \t]+)?(' + nouns + r')(?=[ \t]*[:—–])', checked)
    apply(r'проверил(?:а)?' + r'(?=[ \t]*(?:[.,!]|$))', lambda m, up: _cap('проверено', up))
    def handed(m, up):
        owner, noun = (m.group(2) or '').casefold(), m.group(3).casefold()
        subject, participle = _HANDED[noun]
        owner = {'вашу': 'ваша'}.get(owner, owner)
        return _cap((owner + ' ' if owner else '') + subject + ' ' + participle, up)

    apply(r'передал(?:а)?[ \t]+(?:(ваш|ваше|вашу|ваши)[ \t]+)?(' + '|'.join(_HANDED) + r')\b', handed)
    apply(r'передал(?:а)?(?=[ \t]+команде\b|[ \t]*(?:[.,!]|$))', lambda m, up: _cap('передано', up))
    apply(r'не[ \t]+смог(?:ла)?(?=[ \t]+\w+(?:ть|ти|чь)(?:ся)?\b)', lambda m, up: _cap('не удалось', up))
    # «Должна быть ссылка» is about the link: only «я должен(а) …» is rewritten.
    apply(r'(?:должен|должна)(?=[ \t]+\w+(?:ть|ти|чь)(?:ся)?\b)', lambda m, up: _cap('нужно', up), pronoun=True)
    # «Рад(а) помочь!» → «Обращайтесь!»; «Буду рада помочь с настройкой» → «Помогу с настройкой».
    infinitives = '|'.join(_INFINITIVE)

    def offer(m):
        verb = m.group('verb').casefold()
        word = 'обращайтесь' if verb == 'помочь' and m.group('end') is not None else _INFINITIVE[verb]
        first = m.group('me') or m.group('extra') or m.group('adj')
        return _cap(word, first[:1].isupper()) + (m.group('end') or '')

    text = re.sub(
        r'(?:^|(?<=[\s,(>]))(?:(?P<me>я)[ \t]+)?(?P<extra>(?:буду|всегда)[ \t]+(?:(?:буду|всегда)[ \t]+)?)?(?P<adj>рад|рада|готов|готова)[ \t]+'
        r'(?:был[аи]?[ \t]+)?(?P<verb>' + infinitives + r')\b(?P<end>[ \t]*[.!,;)]|[ \t]*$)?',
        offer, text, flags=re.IGNORECASE | re.MULTILINE,
    )
    return text


def initial_greeting(answer: str, first_reply: bool) -> str:
    # A leading premium emoji is decoration: «🎧 Здравствуйте!» already greets.
    visible = re.sub(r'^[^\w]+', '', re.sub(r'<[^>]*>', '', answer))
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
