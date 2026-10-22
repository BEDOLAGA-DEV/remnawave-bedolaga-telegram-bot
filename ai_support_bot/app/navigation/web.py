from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ai_support_bot.app.navigation.schema import NavNode


WEB_LOCALES_DIR = Path(__file__).resolve().parents[3] / 'bedolaga-cabinet' / 'src' / 'locales'


@dataclass(frozen=True)
class WebEntry:
    id: str
    parent: str
    route: str
    labels: tuple[str, ...]
    keywords: tuple[str, ...] = ()
    hint: str = ''
    link_route: str = ''


_SUB = ('nav.subscription', '[Выберите подписку]')
_FULL = (*_SUB, 'lite.rows.allSettings')
_OPTIONAL = 'Раздел отображается только при включённой функции; наличие маршрута не подтверждает доступность.'
_FULL_HINT = '«Все настройки подписки» переключает простой вид на полный; автопродление, устройства, перевыпуск и пауза доступны также прямо в простом виде при подходящем состоянии подписки.'

PUBLIC_WEB_HELP = {
    'subs_buy_subscription': 'При наличии подписок кнопка называется «Купить ещё»; также есть «Посмотреть тарифы и купить подписку».',
    'subscription_connect': 'Выберите платформу и приложение, затем следуйте инструкции подключения.',
    'subscription_extend': 'Для старой подписки без тарифа сначала потребуется выбрать тариф.',
    'subscription_autopay': 'Переключатель «Автопродление» находится на карточке подписки. Он недоступен для пробных, посуточных и старых подписок без тарифа.',
    'balance_topup': 'Выберите доступный способ оплаты, затем сумму и перейдите к оплате.',
    'promocode': 'Введите код в поле «Промокод», затем нажмите «Активировать».',
    'web_accounts': 'Здесь можно привязать или отвязать способ входа. Последний способ входа отвязать нельзя.',
    'web_change_email': 'Введите новый email и подтвердите его кодом на сайте. Кнопка доступна при включённом входе по почте и наличии email у аккаунта.',
    'web_wheel_history': 'Ниже колеса нажмите «Последние вращения», чтобы раскрыть историю. Код отображается в результате выигрыша. Если результат закрыт и код потерян, обратитесь в поддержку для ручной проверки.',
}

WEB_ENTRIES = (
    WebEntry('main_menu', '', '/', ('nav.dashboard',), ('сайт', 'кабинет', 'главная'),
             'На компьютере разделы в меню; на телефоне основные — в нижней панели, остальные — в «Меню». '
             'В простом виде главная показывает подписку; отдельной кнопки «Подписка» в нижней панели может не быть.'),
    WebEntry('subscription', 'main_menu', '/subscriptions', ('nav.subscription',), (),
             'Заголовок списка — «Мои подписки». При одной подписке сразу открывается её карточка.'),
    WebEntry('subs_buy_subscription', 'subscription', '/subscription/purchase',
             ('nav.subscription', 'subscriptions.buy'), ('купить', 'тариф', 'покупка'),
             'При наличии подписок кнопка называется «Купить ещё»; также есть «Посмотреть тарифы и купить подписку».'),
    WebEntry('subscription_detail', 'subscription', '/subscriptions/:subscriptionId', _SUB),
    WebEntry('subscription_connect', 'subscription_detail', '/connection',
             (*_SUB, 'dashboard.connectDevice'), ('подключение', 'настройка', 'приложение'),
             'Открывает выбор платформы и инструкции клиента для выбранной подписки.'),
    WebEntry('web_copy_link', 'subscription_detail', '/subscriptions/:subscriptionId',
             (*_SUB, 'lite.rows.copyLink'), ('скопировать ссылку', 'ключ'),
             'В полном виде кнопка называется «Копировать ссылку».'),
    WebEntry('subscription_extend', 'subscription_detail', '/subscriptions/:subscriptionId/renew',
             (*_SUB, 'lite.action.renew'), ('продлить', 'продление'),
             'У старой подписки без тарифа вместо продления требуется выбор тарифа; не обещай наличие кнопки продления для неё.'),
    WebEntry('web_full_settings', 'subscription_detail', '/subscriptions/:subscriptionId?full=1',
             _FULL, ('полный вид', 'все настройки', 'управление'), _FULL_HINT),
    WebEntry('subscription_autopay', 'subscription_detail', '/subscriptions/:subscriptionId',
             (*_SUB, 'subscription.autoRenewal'), ('автоплатеж', 'автопродление', 'отключить'),
             'Автопродление находится прямо на карточке в обоих видах и переключается тумблером. Недоступно для пробных, посуточных и старых подписок без тарифа.'),
    WebEntry('subscription_manage_devices', 'subscription_detail', '/subscriptions/:subscriptionId',
             (*_SUB, 'subscription.myDevices'), ('устройства', 'список устройств', 'отключить устройство'), 'В простом виде нажмите «Мои устройства», в полном — найдите одноимённый блок на странице.'),
    WebEntry('subscription_reset_device', 'subscription_manage_devices', '/subscriptions/:subscriptionId',
             (*_SUB, 'subscription.myDevices', '[Выберите устройство]', 'subscription.deleteDevice'),
             ('удалить устройство', 'сбросить устройство'), 'Удаление требует подтверждения.'),
    WebEntry('reset_all_devices', 'subscription_manage_devices', '/subscriptions/:subscriptionId',
             (*_SUB, 'subscription.myDevices', 'subscription.deleteAllDevices'),
             ('удалить все устройства', 'сбросить все'), 'Удаление требует подтверждения.'),
    WebEntry('subscription_add_devices', 'subscription_detail', '/subscriptions/:subscriptionId',
             (*_SUB, 'lite.rows.buyDevices'), ('докупить устройства', 'увеличить лимит'),
             'Кнопка доступна, если тариф разрешает покупку дополнительных устройств.'),
    WebEntry('web_reduce_devices', 'subscription_detail', '/subscriptions/:subscriptionId',
             (*_SUB, 'lite.rows.reduceDevices'), ('уменьшить лимит', 'уменьшить число устройств'),
             'Отображается для подходящих тарифов; условия и стоимость нужно проверять по данным подписки.'),
    WebEntry('subscription_traffic', 'subscription_detail', '/subscriptions/:subscriptionId',
             (*_SUB, 'lite.rows.buyTraffic'), ('трафик', 'докупить гб', 'гигабайты'),
             'Покупка доступна, если тариф допускает дополнительные пакеты трафика.'),
    WebEntry('web_servers', 'subscription_detail', '/subscriptions/:subscriptionId',
             (*_SUB, 'lite.rows.servers'), ('серверы', 'страны', 'локации'),
             'Управление серверами отображается для подходящих тарифов.'),
    WebEntry('subscription_revoke', 'subscription_detail', '/subscriptions/:subscriptionId',
             (*_SUB, 'subscription.revoke.button'), ('перевыпустить ссылку', 'обновить ключ'),
             'Кнопка находится прямо на карточке в обоих видах при подходящем состоянии подписки. Требует подтверждения; старая ссылка перестанет работать.'),
    WebEntry('web_pause', 'subscription_detail', '/subscriptions/:subscriptionId',
             (*_SUB, 'subscription.pause.title'), ('пауза', 'приостановить', 'возобновить'),
             'Блок находится прямо на карточке в обоих видах, только у посуточных непробных подписок. Кнопки «Приостановить» / «Возобновить» зависят от состояния.'),
    WebEntry('balance', 'main_menu', '/balance', ('nav.balance',)),
    WebEntry('balance_topup', 'balance', '/balance/top-up', ('nav.balance', 'balance.topUpBalance'),
             ('пополнить', 'деньги', 'оплата'),
             'Выберите доступный способ, затем сумму и перейдите к оплате. На странице баланса способы также показаны напрямую; список приходит с сервера.'),
    WebEntry('balance_history', 'balance', '/balance', ('nav.balance', 'balance.transactionHistory'),
             ('история', 'платежи', 'операции', 'чек')),
    WebEntry('promocode', 'balance', '/balance', ('nav.balance', 'balance.promocode.title', 'balance.promocode.activate'),
             ('промокод', 'код', 'активация'), 'Введите свой промокод в поле на странице баланса.'),
    WebEntry('saved_cards', 'balance', '/balance/saved-cards', ('nav.balance', 'balance.savedCards.title'),
             ('привязанные карты', 'отвязать карту')),
    WebEntry('profile', 'main_menu', '/profile', ('nav.profile',), ('почта', 'профиль', 'аккаунт')),
    WebEntry('web_accounts', 'profile', '/profile/accounts', ('nav.profile', 'profile.accounts.goToAccounts'),
             ('привязанные аккаунты', 'способ входа', 'привязать telegram', 'привязать почту'),
             'Здесь доступны привязка и отвязка способов входа; последний способ входа отвязать нельзя.'),
    WebEntry('web_change_email', 'profile', '/profile', ('nav.profile', 'profile.changeEmail.button'),
             ('сменить email', 'сменить почту', 'изменить адрес'),
             'Кнопка отображается при включённом входе по почте и наличии email у аккаунта. Введите новый email, получите и подтвердите код на сайте. Не просите пользователя сообщать код в чате.'),
    WebEntry('web_notifications', 'profile', '/profile', ('nav.profile', 'profile.notifications.title'),
             ('уведомления', 'напоминания', 'рассылки'), 'Отображается при доступных настройках уведомлений.'),
    WebEntry('info_language', 'main_menu', '/', ('nav.menu', '[Флаг и код текущего языка]'),
             ('сменить язык', 'язык интерфейса', 'language'),
             'Переключатель с флагом и кодом языка находится в нижней части меню; появляется при нескольких доступных языках.'),
    WebEntry('support', 'main_menu', '/support', ('nav.support',), (),
             'Если встроенные обращения отключены, страница показывает настроенную ссылку связи с поддержкой.'),
    WebEntry('web_new_ticket', 'support', '/support', ('nav.support', 'support.newTicket'),
             ('создать тикет', 'новое обращение', 'написать поддержке'),
             'При включённых тикетах заполните тему и сообщение, при необходимости прикрепите изображение и нажмите «Отправить».'),
    WebEntry('web_tickets', 'support', '/support', ('nav.support', 'support.yourTickets'),
             ('мои тикеты', 'история обращений', 'ответ поддержки'), 'Выберите обращение, чтобы открыть переписку.'),
    WebEntry('referral', 'main_menu', '/referral', ('nav.referral',), ('рефералы', 'партнерка'), _OPTIONAL),
    WebEntry('referral_create_invite', 'referral', '/referral', ('nav.referral', 'referral.yourLink', 'referral.copyLink'),
             ('реферальная ссылка', 'пригласить друга'), 'На странице отдельно показаны ссылки для бота и кабинета.'),
    WebEntry('referral_list', 'referral', '/referral', ('nav.referral', 'referral.yourReferrals'), ('список рефералов',)),
    WebEntry('referral_withdrawal', 'referral', '/referral/withdrawal/request',
             ('nav.referral', 'referral.withdrawal.requestButton'), ('вывод', 'выплата'),
             'Вывод доступен при выполнении условий программы; не обещай одобрение или срок выплаты.'),
    WebEntry('web_partner_apply', 'referral', '/referral/partner/apply',
             ('nav.referral', 'referral.partner.applyButton'), ('заявка партнера', 'стать партнером'), _OPTIONAL),
    WebEntry('info', 'main_menu', '/info', ('nav.info',)),
    WebEntry('info_faq', 'info', '/info', ('nav.info', 'info.faq'), ('faq', 'частые вопросы'), _OPTIONAL),
    WebEntry('web_rules', 'info', '/info', ('nav.info', 'info.rules'), ('правила',), _OPTIONAL),
    WebEntry('info_privacy_policy', 'info', '/info', ('nav.info', 'info.privacy'), ('конфиденциальность',), _OPTIONAL),
    WebEntry('info_public_offer', 'info', '/info', ('nav.info', 'info.offer'), ('оферта', 'договор'), _OPTIONAL),
    WebEntry('info_promo_groups', 'info', '/info', ('nav.info', 'info.loyalty'), ('лояльность', 'скидки'), _OPTIONAL),
    WebEntry('contests', 'main_menu', '/contests', ('nav.contests',), ('конкурс', 'розыгрыш'), _OPTIONAL),
    WebEntry('web_polls', 'main_menu', '/polls', ('nav.polls',), ('опрос', 'голосование'), _OPTIONAL),
    WebEntry('web_wheel', 'main_menu', '/wheel', ('nav.wheel',), ('колесо', 'вращение', 'приз'), _OPTIONAL),
    WebEntry('web_wheel_history', 'web_wheel', '/wheel', ('nav.wheel', 'wheel.recentSpins'),
             ('история вращений', 'выигрыш', 'выигранный код'),
             'Ниже колеса нажмите «Последние вращения», чтобы раскрыть историю. Код отображается в результате выигрыша, а не в списке истории. Если результат закрыт и код потерян, нужна ручная проверка; не предлагай вращать колесо повторно ради кода.'),
    WebEntry('web_gift', 'main_menu', '/gift', ('nav.gift',), ('подарок', 'подарить подписку'), _OPTIONAL),
    WebEntry('web_activate_gift', 'web_gift', '/gift', ('nav.gift', 'gift.tabActivate', 'gift.activateButton'),
             ('активировать подарок', 'подарочный код'), 'Введите полученный код подарка на сайте.'),
    WebEntry('web_my_gifts', 'web_gift', '/gift', ('nav.gift', 'gift.tabMyGifts'), ('мои подарки', 'подарки')),
    WebEntry('web_connection_qr', 'subscription_connect', '/connection/qr',
             (*_SUB, 'dashboard.connectDevice', 'subscription.connection.openQr'), ('qr', 'qr-код', 'телевизор'),
             'Нажмите значок QR на странице подключения; сам экран QR требует данных выбранной подписки.', '/connection'),
)


def load_web_locale(language: str) -> dict[str, str]:
    values = json.loads(Path(__file__).with_name('web_labels_ru.json').read_text(encoding='utf-8'))
    for code in dict.fromkeys(('ru', language)):
        if not _is_language_code(code):
            continue
        try:
            payload = json.loads((WEB_LOCALES_DIR / f'{code}.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            _flatten(payload, '', values)
    return values


def _is_language_code(code: str) -> bool:
    return bool(code) and all(char.isascii() and (char.isalnum() or char in '-_') for char in code)


def _flatten(payload: dict, prefix: str, values: dict[str, str]) -> None:
    for key, value in payload.items():
        name = f'{prefix}.{key}' if prefix else key
        if isinstance(value, dict):
            _flatten(value, name, values)
        elif isinstance(value, str):
            values[name] = value


def apply_web_navigation(index: dict[str, NavNode], language: str) -> list[str]:
    locale = load_web_locale(language)
    for node in index.values():
        node.web_label = None
        node.web_path = None
        node.web_link = None
    for entry in WEB_ENTRIES:
        steps = tuple(locale.get(key, key) for key in entry.labels)
        node = index.get(entry.id)
        if node is None:
            parent = index[entry.parent]
            node = NavNode(id=entry.id, title=steps[-1], parent_id=parent.id, source='web_blueprint')
            parent.children.append(node)
            index[node.id] = node
        node.web_path = entry.route
        node.web_link = entry.link_route or None
        node.web_label = steps[-1]
        node.web_steps = steps
        node.web_hint = entry.hint
        node.keywords = tuple(dict.fromkeys((*node.keywords, *entry.keywords)))
    return ['web_blueprint', 'web_locales'] if locale else ['web_blueprint']
