import structlog
from aiogram import Dispatcher, F, types
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.localization.texts import get_texts
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)

PROBLEMS = {
    'connect': '🔌 Не подключается',
    'slow': '🐌 Медленная скорость',
    'disconnect': '⚡ Обрывы соединения',
    'setup': '📱 Настройка на устройстве',
}

PLATFORMS = {
    'ios': '🍏 iOS',
    'android': '🤖 Android',
    'windows': '💻 Windows',
    'mac': '🍎 Mac',
    'linux': '🐧 Linux',
}

# Solutions: problem -> platform -> list of steps (HTML)
SOLUTIONS: dict[str, dict[str, str]] = {
    'connect': {
        'ios': (
            '<b>Не удается подключиться (iOS):</b>\n\n'
            '1. Откройте <b>Настройки → VPN</b> и удалите старый профиль\n'
            '2. Переустановите приложение (Happ / Streisand)\n'
            '3. Импортируйте ссылку подписки заново\n'
            '4. Убедитесь, что Wi-Fi или мобильные данные работают\n'
            '5. Попробуйте переключиться на другой сервер'
        ),
        'android': (
            '<b>Не удается подключиться (Android):</b>\n\n'
            '1. Проверьте, что приложению разрешен VPN-доступ\n'
            '2. Откройте Happ → Удалите текущий профиль\n'
            '3. Импортируйте ссылку подписки заново\n'
            '4. Отключите энергосбережение для приложения VPN\n'
            '5. Попробуйте переключить сервер в настройках'
        ),
        'windows': (
            '<b>Не удается подключиться (Windows):</b>\n\n'
            '1. Запустите клиент от имени администратора\n'
            '2. Проверьте, не блокирует ли антивирус VPN-соединение\n'
            '3. Обновите подписку: удалите профиль и добавьте заново\n'
            '4. Откройте Брандмауэр Windows и разрешите приложение\n'
            '5. Перезагрузите компьютер и попробуйте снова'
        ),
        'mac': (
            '<b>Не удается подключиться (Mac):</b>\n\n'
            '1. Откройте <b>Системные настройки → VPN</b> и удалите профиль\n'
            '2. Переустановите VPN-клиент\n'
            '3. Импортируйте ссылку подписки заново\n'
            '4. Проверьте разрешения в <b>Конфиденциальность и безопасность</b>\n'
            '5. Попробуйте другой сервер'
        ),
        'linux': (
            '<b>Не удается подключиться (Linux):</b>\n\n'
            '1. Проверьте, что v2ray/xray установлен и обновлен\n'
            '2. Обновите конфиг подписки командой обновления\n'
            '3. Проверьте логи: <code>journalctl -u v2ray -f</code>\n'
            '4. Убедитесь, что порты не заблокированы firewall\n'
            '5. Попробуйте сменить сервер в конфигурации'
        ),
    },
    'slow': {
        'ios': (
            '<b>Медленная скорость (iOS):</b>\n\n'
            '1. Переключитесь на ближайший сервер\n'
            '2. Попробуйте переключить протокол (VLESS → VMess)\n'
            '3. Отключите и заново подключите VPN\n'
            '4. Проверьте скорость без VPN — проблема может быть у провайдера\n'
            '5. Перезагрузите устройство'
        ),
        'android': (
            '<b>Медленная скорость (Android):</b>\n\n'
            '1. Выберите сервер, ближайший к вашему расположению\n'
            '2. В Happ попробуйте сменить протокол\n'
            '3. Отключите энергосбережение для VPN-приложения\n'
            '4. Проверьте скорость без VPN для сравнения\n'
            '5. Очистите кеш приложения и переподключитесь'
        ),
        'windows': (
            '<b>Медленная скорость (Windows):</b>\n\n'
            '1. Выберите сервер с наименьшим пингом\n'
            '2. Попробуйте другой протокол в настройках клиента\n'
            '3. Отключите антивирусное сканирование трафика\n'
            '4. Проверьте, нет ли других программ, занимающих канал\n'
            '5. Попробуйте подключиться через Ethernet вместо Wi-Fi'
        ),
        'mac': (
            '<b>Медленная скорость (Mac):</b>\n\n'
            '1. Переключитесь на ближайший сервер\n'
            '2. Попробуйте другой протокол подключения\n'
            '3. Закройте приложения, потребляющие много трафика\n'
            '4. Проверьте базовую скорость без VPN\n'
            '5. Перезагрузите роутер и Mac'
        ),
        'linux': (
            '<b>Медленная скорость (Linux):</b>\n\n'
            '1. Проверьте пинг до сервера: <code>ping server_ip</code>\n'
            '2. Смените сервер на ближайший\n'
            '3. Попробуйте другой протокол в конфигурации\n'
            '4. Проверьте нагрузку сети: <code>iftop</code>\n'
            '5. Обновите ядро v2ray/xray до последней версии'
        ),
    },
    'disconnect': {
        'ios': (
            '<b>Обрывы соединения (iOS):</b>\n\n'
            '1. Включите <b>Always On VPN</b> в настройках iOS\n'
            '2. Отключите оптимизацию батареи для VPN-приложения\n'
            '3. Переключитесь на другой сервер\n'
            '4. Обновите приложение до последней версии\n'
            '5. Проверьте стабильность интернет-соединения'
        ),
        'android': (
            '<b>Обрывы соединения (Android):</b>\n\n'
            '1. Отключите оптимизацию батареи для VPN-приложения\n'
            '2. Включите "Постоянная VPN" в настройках Android\n'
            '3. Попробуйте другой сервер\n'
            '4. Обновите Happ до последней версии\n'
            '5. Проверьте, не переключается ли Wi-Fi/мобильные данные'
        ),
        'windows': (
            '<b>Обрывы соединения (Windows):</b>\n\n'
            '1. Отключите спящий режим сетевого адаптера\n'
            '2. Обновите драйверы сетевой карты\n'
            '3. Попробуйте другой сервер\n'
            '4. Добавьте VPN-клиент в исключения антивируса\n'
            '5. Проверьте стабильность подключения к роутеру'
        ),
        'mac': (
            '<b>Обрывы соединения (Mac):</b>\n\n'
            '1. Отключите автопереключение Wi-Fi сетей\n'
            '2. Обновите VPN-клиент до последней версии\n'
            '3. Попробуйте другой сервер\n'
            '4. Проверьте настройки энергосбережения\n'
            '5. Сбросьте сетевые настройки Mac'
        ),
        'linux': (
            '<b>Обрывы соединения (Linux):</b>\n\n'
            '1. Проверьте логи: <code>journalctl -u v2ray --since "1h ago"</code>\n'
            '2. Настройте auto-restart: <code>Restart=always</code> в systemd\n'
            '3. Смените сервер на более стабильный\n'
            '4. Проверьте MTU: <code>ping -M do -s 1400 server_ip</code>\n'
            '5. Обновите v2ray/xray до последней версии'
        ),
    },
    'setup': {
        'ios': (
            '<b>Настройка на iOS:</b>\n\n'
            '1. Установите <b>Streisand</b> или <b>V2Box</b> из App Store\n'
            '2. Скопируйте ссылку подписки из бота\n'
            '3. Откройте приложение и нажмите "+" → "Импорт из буфера"\n'
            '4. Выберите сервер и нажмите "Подключить"\n'
            '5. Разрешите установку VPN-профиля в системном запросе'
        ),
        'android': (
            '<b>Настройка на Android:</b>\n\n'
            '1. Установите <b>Happ</b> из Google Play или GitHub\n'
            '2. Скопируйте ссылку подписки из бота\n'
            '3. В приложении нажмите "+" → "Импорт из буфера"\n'
            '4. Выберите сервер из списка\n'
            '5. Нажмите кнопку подключения (V) внизу экрана'
        ),
        'windows': (
            '<b>Настройка на Windows:</b>\n\n'
            '1. Скачайте <b>v2rayN</b> с GitHub\n'
            '2. Распакуйте архив в удобную папку\n'
            '3. Запустите v2rayN.exe от имени администратора\n'
            '4. Скопируйте ссылку подписки → "Подписки" → "Добавить"\n'
            '5. Обновите подписку и выберите сервер для подключения'
        ),
        'mac': (
            '<b>Настройка на Mac:</b>\n\n'
            '1. Установите <b>V2Box</b> из App Store или <b>V2rayU</b> с GitHub\n'
            '2. Скопируйте ссылку подписки из бота\n'
            '3. Импортируйте ссылку в приложение\n'
            '4. Выберите сервер и нажмите "Подключить"\n'
            '5. Разрешите установку VPN-профиля в системном запросе'
        ),
        'linux': (
            '<b>Настройка на Linux:</b>\n\n'
            '1. Установите <b>v2ray</b> или <b>xray</b>: <code>bash <(curl -L https://raw.githubusercontent.com/v2fly/fhs-install-v2ray/master/install-release.sh)</code>\n'
            '2. Скопируйте ссылку подписки из бота\n'
            '3. Сконвертируйте ссылку в конфиг (или используйте v2rayA GUI)\n'
            '4. Запустите сервис: <code>sudo systemctl start v2ray</code>\n'
            '5. Настройте системный прокси или используйте proxychains'
        ),
    },
}


def _get_problem_keyboard(language: str) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    buttons = []
    for key, label in PROBLEMS.items():
        buttons.append([
            types.InlineKeyboardButton(
                text=label,
                callback_data=f'nz!_ts_{key}',
            )
        ])
    buttons.append([
        types.InlineKeyboardButton(text=texts.BACK, callback_data='nz!_back_to_menu')
    ])
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


def _get_platform_keyboard(problem: str, language: str) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    buttons = []
    row = []
    for key, label in PLATFORMS.items():
        row.append(
            types.InlineKeyboardButton(
                text=label,
                callback_data=f'nz!_ts_{problem}_{key}',
            )
        )
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([
        types.InlineKeyboardButton(text=texts.BACK, callback_data='nz!_troubleshoot')
    ])
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


def _get_solution_keyboard(language: str) -> types.InlineKeyboardMarkup:
    texts = get_texts(language)
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(
                text='🔙 К выбору проблемы',
                callback_data='nz!_troubleshoot',
            ),
        ],
        [
            types.InlineKeyboardButton(
                text='🎫 Не помогло? Создать тикет',
                callback_data='nz!_support_create',
            ),
        ],
        [
            types.InlineKeyboardButton(text=texts.BACK, callback_data='nz!_back_to_menu'),
        ],
    ])


@error_handler
async def show_troubleshoot(
    callback: types.CallbackQuery,
    db_user: User,
):
    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t(
            'TROUBLESHOOT_CHOOSE_PROBLEM',
            'Выберите, с какой проблемой вы столкнулись:',
        ),
        reply_markup=_get_problem_keyboard(db_user.language),
    )
    await callback.answer()


@error_handler
async def on_problem_selected(
    callback: types.CallbackQuery,
    db_user: User,
):
    # Extract problem key from callback: nz!_ts_{problem}
    problem = callback.data.replace('nz!_ts_', '')
    if problem not in PROBLEMS:
        await callback.answer('Неизвестная проблема', show_alert=True)
        return

    texts = get_texts(db_user.language)
    await callback.message.edit_text(
        texts.t(
            'TROUBLESHOOT_CHOOSE_PLATFORM',
            'Выберите вашу платформу:',
        ),
        reply_markup=_get_platform_keyboard(problem, db_user.language),
    )
    await callback.answer()


@error_handler
async def on_platform_selected(
    callback: types.CallbackQuery,
    db_user: User,
):
    # Extract problem and platform: nz!_ts_{problem}_{platform}
    parts = callback.data.replace('nz!_ts_', '').rsplit('_', 1)
    if len(parts) != 2:
        await callback.answer('Ошибка', show_alert=True)
        return

    problem, platform = parts
    if problem not in SOLUTIONS or platform not in SOLUTIONS.get(problem, {}):
        await callback.answer('Решение не найдено', show_alert=True)
        return

    solution_text = SOLUTIONS[problem][platform]
    await callback.message.edit_text(
        solution_text,
        reply_markup=_get_solution_keyboard(db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


@error_handler
async def on_support_create_from_troubleshoot(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession = None,
):
    """Redirect to ticket creation from troubleshoot wizard."""
    # Rewrite callback data to trigger the existing ticket creation handler
    callback.data = 'nz!_create_ticket'
    # Let aiogram re-dispatch or call existing handler
    from app.handlers.tickets import show_ticket_priority_selection
    from aiogram.fsm.context import FSMContext

    # We need state from the middleware; get it from callback
    state: FSMContext = callback.bot.get('fsm_context')  # fallback
    # Actually, the ticket handler is registered separately; just forward
    # by changing callback data and answering. The user will click again.
    await callback.answer()
    # Notify user to use the ticket button
    texts = get_texts(db_user.language)
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(
                text='🎫 Создать тикет',
                callback_data='nz!_create_ticket',
            ),
        ],
        [
            types.InlineKeyboardButton(text=texts.BACK, callback_data='nz!_troubleshoot'),
        ],
    ])
    await callback.message.edit_text(
        texts.t(
            'TROUBLESHOOT_CREATE_TICKET',
            'Если проблема не решена, создайте тикет и наша поддержка поможет вам:',
        ),
        reply_markup=keyboard,
    )


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_troubleshoot, F.data == 'nz!_troubleshoot')

    # Platform selection: nz!_ts_{problem}_{platform} - must be registered before problem
    dp.callback_query.register(
        on_platform_selected,
        F.data.regexp(r'^nz!_ts_(connect|slow|disconnect|setup)_(ios|android|windows|mac|linux)$'),
    )

    # Problem selection: nz!_ts_{problem}
    dp.callback_query.register(
        on_problem_selected,
        F.data.regexp(r'^nz!_ts_(connect|slow|disconnect|setup)$'),
    )

    # Support create from troubleshoot
    dp.callback_query.register(
        on_support_create_from_troubleshoot, F.data == 'nz!_support_create'
    )
