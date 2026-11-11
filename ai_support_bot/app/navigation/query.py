import re


def device_occupancy(query: str) -> tuple[int, int] | None:
    if not re.search(r'занят|подключен|подключён|использу', query, re.IGNORECASE):
        return None
    match = re.search(r'\b(\d{1,4})\s*(?:из|/)\s*(\d{1,4})\b', query, re.IGNORECASE)
    return (int(match[1]), int(match[2])) if match else None


def device_action(query: str) -> str | None:
    text = query.casefold()
    if not re.search(r'устройств|девайс|телефон|планшет|ноутбук|компьютер', text):
        return None
    if re.search(r'(?:добав|подключ)\w*\s+(?:трафик|автоплат|автопродл)', text):
        return None
    occupancy = device_occupancy(text)
    if occupancy and occupancy[0] < occupancy[1] and re.search(r'нужн\w* ли.*(?:докуп|плат)|зачем.*докуп', text):
        return 'connect'
    if re.search(r'докуп|купить.*(?:устройств|мест)|увелич\w*.*(?:лимит|количеств|число)|больше.*(?:устройств|мест)', text):
        return 'expand'
    if re.search(r'(?:лимит|мест)\w*.*(?:увелич|добав)|добав\w*.*(?:лимит|мест)', text):
        return 'expand'
    if not re.search(r'добав|подключ|настро|перенес|перенос|установ', text):
        return None
    if occupancy and occupancy[1] > 0 and occupancy[0] >= occupancy[1]:
        return 'full'
    if occupancy or re.search(r'подключ|настро|перенес|перенос|установ', text):
        return 'connect'
    return None


def device_connection_intro(query: str) -> str:
    occupancy = device_occupancy(query)
    prefix = ''
    if occupancy and 0 <= occupancy[0] < occupancy[1]:
        prefix = f'При {occupancy[0]} из {occupancy[1]} занятых мест в лимите ещё есть свободные места. Докупать их для следующего устройства не нужно.\n\n'
    return prefix + (
        'На новом устройстве установите подходящий VPN-клиент, добавьте в него ссылку той же подписки '
        'и включите VPN. Ссылку и инструкцию для нужной платформы откройте в своей подписке:\n\n'
    )


def is_connection_problem(query: str) -> bool:
    return bool(
        re.search(r'vpn|впн|подключ|сервер|интернет|telegram|телеграм|\bтг\b', query, re.IGNORECASE)
        and re.search(r'проблем|не могу|не откры|не за[йх]|не груз|не работ|ничего|сбо[йи]|ошиб|тайм.?аут|отвал|через раз', query, re.IGNORECASE)
    )


def is_subscription_refresh(query: str) -> bool:
    text = query.casefold()
    text = re.sub(r'\bне\s+(?:хочу\s+)?продл\w*', '', text)
    return bool(
        re.search(r'обнов(?:ить|лять|ление|и(?:те)?)\b|синхрониз\w*', text)
        and re.search(r'подписк|спис\w* сервер', text)
        and not re.search(r'продл', text)
    )


def is_context_followup(question: str) -> bool:
    return question.strip().casefold().rstrip('?.!') in {
        'как', 'а как', 'как это сделать', 'как именно', 'что делать', 'не понял', 'не поняла', 'непонятно',
    }


def navigation_target(query: str) -> str | None:
    text = query.casefold()
    if is_subscription_refresh(query):
        return None
    action = device_action(query)
    if action:
        return {'connect': 'subscription_connect', 'expand': 'subscription_add_devices', 'full': 'subscription_manage_devices'}[action]
    if re.search(r'колес|вращен|выигранн', text):
        return 'web_wheel_history'
    if re.search(r'промокод|промик', text):
        return 'promocode'
    if re.search(r'привяз\w*.*(?:telegram|телеграм|аккаунт)|привязанн\w* аккаунт', text):
        return 'web_accounts'
    if re.search(r'смен\w*.*(?:email|почт)|измен\w*.*email', text):
        return 'web_change_email'
    if re.search(r'автоплат|автопродл', text):
        return 'subscription_autopay'
    if re.search(r'продл', text):
        return 'subscription_extend'
    if re.search(r'куп\w*.*подпис|оформ\w*.*подпис', text):
        return 'subs_buy_subscription'
    if re.search(r'пополн\w*.*баланс', text):
        return 'balance_topup'
    if re.search(r'истори\w*.*(?:операц|платеж|платёж)', text):
        return 'balance_history'
    if re.search(r'подключ\w*.*(?:vpn|впн|подписк)|ссылк\w*.*подключ|настро\w*.*vpn', text):
        return 'subscription_connect'
    return None


def is_channel_followup(question: str) -> bool:
    text = question.strip().casefold().rstrip('?.!')
    return bool(re.fullmatch(
        r'(?:(?:а\s+)?(?:теперь\s+)?(?:покажи\s+)?(?:как\s+)?|(?:нужна\s+)?инструкция\s+|(?:а\s+)?(?:как|где)\s+(?:это\s+|там\s+)?)'
        r'(?:(?:именно|только)\s+)?(?:для\s+|в\s+|на\s+)?(?:сайт\w*|кабинет\w*|(?:telegram|телеграм\w*)[- ]бот\w*|telegram|телеграм\w*|бот\w*|tg|тг)', text,
    ))


def contextual_navigation_query(question: str, history: list, summary: str | None = None) -> str:
    if navigation_target(question):
        return question.strip()
    if not is_channel_followup(question):
        return question.strip()
    for item in reversed(history):
        if getattr(item, 'role', '') != 'user':
            continue
        content = getattr(item, 'content', '') or ''
        if navigation_target(content):
            return content[:300] + '\n' + question.strip()
        if not is_channel_followup(content):
            break
    return question.strip()
