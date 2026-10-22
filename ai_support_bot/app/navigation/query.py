import re


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
