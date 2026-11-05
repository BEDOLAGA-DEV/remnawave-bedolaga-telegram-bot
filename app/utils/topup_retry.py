"""Actionable, exact RUB input limits for rejected Telegram top-ups."""

from app.localization.texts import get_texts


MIN_MANUAL_TOPUP_RUBLES = 1
MAX_MANUAL_TOPUP_RUBLES = 50000


def _rubles(kopeks: int) -> str:
    whole, cents = divmod(kopeks, 100)
    return f'{whole}.{cents:02d}'.rstrip('0').rstrip('.') if cents else str(whole)


def format_topup_amount_retry(language: str, minimum: int, maximum: int, *, too_low: bool) -> str:
    texts = get_texts(language)
    lower = max(minimum, MIN_MANUAL_TOPUP_RUBLES * 100)
    upper = min(maximum, MAX_MANUAL_TOPUP_RUBLES * 100)
    values = {'minimum': _rubles(lower), 'maximum': _rubles(upper)}
    if lower > upper:
        return texts.t(
            'TOPUP_RETRY_NO_RANGE',
            '❌ У этого способа нет допустимой суммы для ручного ввода '
            '(минимум {minimum} ₽, максимум {maximum} ₽). Выберите другой способ оплаты.',
        ).format(**values)
    key = 'TOPUP_RETRY_TOO_LOW' if too_low else 'TOPUP_RETRY_TOO_HIGH'
    fallback = (
        '❌ Минимальная сумма пополнения — {minimum} ₽.\n\n'
        if too_low
        else '❌ Максимальная сумма пополнения — {maximum} ₽.\n\n'
    ) + (
        'Чтобы продолжить, отправьте боту сообщение с суммой пополнения не меньше {minimum} ₽ и не больше {maximum} ₽.'
    )
    message = texts.t(key, fallback).format(**values)
    whole_ruble_example = (lower + 99) // 100
    if whole_ruble_example * 100 <= upper:
        message += '\n\n' + texts.t('TOPUP_RETRY_EXAMPLE', 'Например, отправьте: {amount}').format(
            amount=whole_ruble_example,
        )
    return message
