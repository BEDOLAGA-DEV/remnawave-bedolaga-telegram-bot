from app.config import settings
from app.localization.texts import get_texts


def verify_payment_amount(
    received_kopeks: int,
    expected_kopeks: int,
    tolerance_kopeks: int = 1,
) -> bool:
    """Check that the received amount matches the expected amount within tolerance."""
    return abs(received_kopeks - expected_kopeks) <= tolerance_kopeks


def get_available_payment_methods() -> list[dict[str, str]]:
    """
    Возвращает список доступных способов оплаты с их настройками
    """
    methods = []

    if settings.TELEGRAM_STARS_ENABLED:
        methods.append(
            {
                'id': 'stars',
                'name': 'Telegram Stars',
                'icon': '⭐',
                'description': 'быстро и удобно',
                'callback': 'topup_stars',
            }
        )

    if settings.is_yookassa_enabled():
        if getattr(settings, 'YOOKASSA_SBP_ENABLED', False):
            methods.append(
                {
                    'id': 'yookassa_sbp',
                    'name': 'СБП (YooKassa)',
                    'icon': '🏦',
                    'description': 'моментальная оплата по QR',
                    'callback': 'topup_yookassa_sbp',
                }
            )

        methods.append(
            {
                'id': 'yookassa',
                'name': 'Банковская карта',
                'icon': '💳',
                'description': 'через YooKassa',
                'callback': 'topup_yookassa',
            }
        )

    if settings.TRIBUTE_ENABLED:
        methods.append(
            {
                'id': 'tribute',
                'name': 'Банковская карта',
                'icon': '💳',
                'description': 'через Tribute',
                'callback': 'topup_tribute',
            }
        )

    if settings.is_mulenpay_enabled():
        mulenpay_name = settings.get_mulenpay_display_name()
        methods.append(
            {
                'id': 'mulenpay',
                'name': 'Банковская карта',
                'icon': '💳',
                'description': f'через {mulenpay_name}',
                'callback': 'topup_mulenpay',
            }
        )

    if settings.is_wata_enabled():
        methods.append(
            {
                'id': 'wata',
                'name': 'Банковская карта',
                'icon': '💳',
                'description': 'через WATA',
                'callback': 'topup_wata',
            }
        )

    if settings.is_pal24_enabled():
        methods.append(
            {'id': 'pal24', 'name': 'СБП', 'icon': '🏦', 'description': 'через PayPalych', 'callback': 'topup_pal24'}
        )

    if settings.is_cryptobot_enabled():
        methods.append(
            {
                'id': 'cryptobot',
                'name': 'Криптовалюта',
                'icon': '🪙',
                'description': 'через CryptoBot',
                'callback': 'topup_cryptobot',
            }
        )

    if settings.is_heleket_enabled():
        methods.append(
            {
                'id': 'heleket',
                'name': 'Криптовалюта',
                'icon': '🪙',
                'description': 'через Heleket',
                'callback': 'topup_heleket',
            }
        )

    if settings.is_platega_enabled() and settings.get_platega_active_methods():
        platega_name = settings.get_platega_display_name()
        if settings.PLATEGA_INLINE_METHODS:
            for method_code in settings.get_platega_active_methods():
                info = settings.get_platega_method_definitions().get(method_code, {})
                methods.append(
                    {
                        'id': f'platega_m{method_code}',
                        'name': info.get('name', f'Метод {method_code}'),
                        'icon': info.get('title', '💳').split(' ', 1)[0] if info.get('title') else '💳',
                        'description': f'через {platega_name}',
                        'callback': f'topup_platega_m{method_code}',
                    }
                )
        else:
            methods.append(
                {
                    'id': 'platega',
                    'name': 'Банковская карта',
                    'icon': '💳',
                    'description': f'через {platega_name} (карты + СБП)',
                    'callback': 'topup_platega',
                }
            )

    if settings.is_cloudpayments_enabled():
        cloudpayments_name = settings.get_cloudpayments_display_name()
        methods.append(
            {
                'id': 'cloudpayments',
                'name': 'Банковская карта',
                'icon': '💳',
                'description': f'через {cloudpayments_name}',
                'callback': 'topup_cloudpayments',
            }
        )

    if settings.is_freekassa_enabled():
        freekassa_name = settings.get_freekassa_display_name()
        methods.append(
            {
                'id': 'freekassa',
                'name': freekassa_name,
                'icon': '💳',
                'description': f'через {freekassa_name}',
                'callback': 'topup_freekassa',
            }
        )

    if settings.is_kassa_ai_enabled():
        kassa_ai_name = settings.get_kassa_ai_display_name()
        methods.append(
            {
                'id': 'kassa_ai',
                'name': kassa_ai_name,
                'icon': '💳',
                'description': f'через {kassa_ai_name}',
                'callback': 'topup_kassa_ai',
            }
        )

    if settings.is_riopay_enabled():
        riopay_name = settings.get_riopay_display_name()
        methods.append(
            {
                'id': 'riopay',
                'name': f'Банковская карта ({riopay_name})',
                'icon': '💳',
                'description': f'через {riopay_name}',
                'callback': 'topup_riopay',
            }
        )

    if settings.is_severpay_enabled():
        severpay_name = settings.get_severpay_display_name()
        methods.append(
            {
                'id': 'severpay',
                'name': f'Банковская карта ({severpay_name})',
                'icon': '💳',
                'description': f'через {severpay_name}',
                'callback': 'topup_severpay',
            }
        )

    if settings.is_paypear_enabled():
        paypear_name = settings.get_paypear_display_name()
        methods.append(
            {
                'id': 'paypear',
                'name': paypear_name,
                'icon': '💳',
                'description': f'через {paypear_name}',
                'callback': 'topup_paypear',
            }
        )

    if settings.is_rollypay_enabled():
        rollypay_name = settings.get_rollypay_display_name()
        methods.append(
            {
                'id': 'rollypay',
                'name': rollypay_name,
                'icon': '💳',
                'description': f'через {rollypay_name}',
                'callback': 'topup_rollypay',
            }
        )

    if settings.is_overpay_enabled():
        overpay_name = settings.get_overpay_display_name()
        methods.append(
            {
                'id': 'overpay',
                'name': overpay_name,
                'icon': '💳',
                'description': f'через {overpay_name}',
                'callback': 'topup_overpay',
            }
        )

    if settings.is_aurapay_enabled():
        aurapay_name = settings.get_aurapay_display_name()
        methods.append(
            {
                'id': 'aurapay',
                'name': aurapay_name,
                'icon': '💳',
                'description': f'через {aurapay_name}',
                'callback': 'topup_aurapay',
            }
        )

    if settings.is_support_topup_enabled():
        methods.append(
            {
                'id': 'support',
                'name': 'Через поддержку',
                'icon': '🛠️',
                'description': 'другие способы',
                'callback': 'topup_support',
            }
        )

    return methods


def get_payment_methods_text(language: str) -> str:
    """
    Генерирует текст с описанием доступных способов оплаты
    """
    texts = get_texts(language)
    methods = get_available_payment_methods()

    if not methods:
        return texts.t(
            'PAYMENT_METHODS_NONE_AVAILABLE',
            """💳 <b>Способы пополнения баланса</b>

⚠️ В данный момент способы оплаты временно недоступны.
Попробуйте позже.

Выберите способ пополнения:""",
        )

    if len(methods) == 1 and methods[0]['id'] == 'support':
        return texts.t(
            'PAYMENT_METHODS_ONLY_SUPPORT',
            """💳 <b>Способы пополнения баланса</b>

⚠️ В данный момент автоматические способы оплаты временно недоступны.
Обратитесь в техподдержку для пополнения баланса.

Выберите способ пополнения:""",
        )

    text = (
        texts.t(
            'PAYMENT_METHODS_TITLE',
            '💳 <b>Способы пополнения баланса</b>',
        )
        + '\n\n'
    )
    text += (
        texts.t(
            'PAYMENT_METHODS_PROMPT',
            'Выберите удобный для вас способ оплаты:',
        )
        + '\n\n'
    )

    for method in methods:
        method_id = method['id'].upper()
        name = texts.t(
            f'PAYMENT_METHOD_{method_id}_NAME',
            f'{method["icon"]} <b>{method["name"]}</b>',
        )
        description = texts.t(
            f'PAYMENT_METHOD_{method_id}_DESCRIPTION',
            method['description'],
        )
        if method_id == 'MULENPAY':
            mulenpay_name = settings.get_mulenpay_display_name()
            mulenpay_name_html = settings.get_mulenpay_display_name_html()
            name = name.format(mulenpay_name=mulenpay_name_html)
            description = description.format(mulenpay_name=mulenpay_name)
        elif method_id == 'PLATEGA':
            platega_name = settings.get_platega_display_name()
            platega_name_html = settings.get_platega_display_name_html()
            name = name.format(platega_name=platega_name_html)
            description = description.format(platega_name=platega_name)

        text += f'{name} - {description}\n'

    text += '\n' + texts.t(
        'PAYMENT_METHODS_FOOTER',
        'Выберите способ пополнения:',
    )

    return text


def is_payment_method_available(method_id: str) -> bool:
    """
    Проверяет, доступен ли конкретный способ оплаты
    """
    if method_id == 'stars':
        return settings.TELEGRAM_STARS_ENABLED
    if method_id == 'yookassa':
        return settings.is_yookassa_enabled()
    if method_id == 'tribute':
        return settings.TRIBUTE_ENABLED
    if method_id == 'mulenpay':
        return settings.is_mulenpay_enabled()
    if method_id == 'wata':
        return settings.is_wata_enabled()
    if method_id == 'pal24':
        return settings.is_pal24_enabled()
    if method_id == 'cryptobot':
        return settings.is_cryptobot_enabled()
    if method_id == 'heleket':
        return settings.is_heleket_enabled()
    if method_id == 'platega':
        return settings.is_platega_enabled() and bool(settings.get_platega_active_methods())
    if method_id.startswith('platega_m'):
        if not settings.is_platega_enabled():
            return False
        try:
            code = int(method_id[len('platega_m') :])
        except ValueError:
            return False
        return code in settings.get_platega_active_methods()
    if method_id == 'cloudpayments':
        return settings.is_cloudpayments_enabled()
    if method_id == 'freekassa':
        return settings.is_freekassa_enabled()
    if method_id == 'kassa_ai':
        return settings.is_kassa_ai_enabled()
    if method_id == 'riopay':
        return settings.is_riopay_enabled()
    if method_id == 'severpay':
        return settings.is_severpay_enabled()
    if method_id == 'paypear':
        return settings.is_paypear_enabled()
    if method_id == 'rollypay':
        return settings.is_rollypay_enabled()
    if method_id == 'overpay':
        return settings.is_overpay_enabled()
    if method_id == 'aurapay':
        return settings.is_aurapay_enabled()
    if method_id == 'support':
        return settings.is_support_topup_enabled()
    return False


def get_payment_method_status() -> dict[str, bool]:
    """
    Возвращает статус всех способов оплаты
    """
    return {
        'stars': settings.TELEGRAM_STARS_ENABLED,
        'yookassa': settings.is_yookassa_enabled(),
        'tribute': settings.TRIBUTE_ENABLED,
        'mulenpay': settings.is_mulenpay_enabled(),
        'wata': settings.is_wata_enabled(),
        'pal24': settings.is_pal24_enabled(),
        'cryptobot': settings.is_cryptobot_enabled(),
        'heleket': settings.is_heleket_enabled(),
        'platega': settings.is_platega_enabled() and bool(settings.get_platega_active_methods()),
        'cloudpayments': settings.is_cloudpayments_enabled(),
        'freekassa': settings.is_freekassa_enabled(),
        'kassa_ai': settings.is_kassa_ai_enabled(),
        'riopay': settings.is_riopay_enabled(),
        'severpay': settings.is_severpay_enabled(),
        'paypear': settings.is_paypear_enabled(),
        'rollypay': settings.is_rollypay_enabled(),
        'overpay': settings.is_overpay_enabled(),
        'aurapay': settings.is_aurapay_enabled(),
        'support': settings.is_support_topup_enabled(),
    }


def get_enabled_payment_methods_count() -> int:
    """
    Возвращает количество включенных способов оплаты (не считая поддержку)
    """
    count = 0
    if settings.TELEGRAM_STARS_ENABLED:
        count += 1
    if settings.is_yookassa_enabled():
        count += 1
    if settings.TRIBUTE_ENABLED:
        count += 1
    if settings.is_mulenpay_enabled():
        count += 1
    if settings.is_wata_enabled():
        count += 1
    if settings.is_pal24_enabled():
        count += 1
    if settings.is_cryptobot_enabled():
        count += 1
    if settings.is_heleket_enabled():
        count += 1
    if settings.is_platega_enabled() and settings.get_platega_active_methods():
        count += 1
    if settings.is_cloudpayments_enabled():
        count += 1
    if settings.is_freekassa_enabled():
        count += 1
    if settings.is_kassa_ai_enabled():
        count += 1
    if settings.is_riopay_enabled():
        count += 1
    if settings.is_severpay_enabled():
        count += 1
    if settings.is_paypear_enabled():
        count += 1
    if settings.is_rollypay_enabled():
        count += 1
    if settings.is_overpay_enabled():
        count += 1
    if settings.is_aurapay_enabled():
        count += 1
    return count
