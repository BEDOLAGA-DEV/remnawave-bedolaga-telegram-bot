"""Админский редактор уровней реферальных наград.

Проверяется то, что ломается тихо: ответ на callback дважды, ввод в неверных
единицах и создание уровня, который сразу начинает платить недонастроенным
правилом.
"""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.handlers.admin import referral_levels as editor


class _Message:
    def __init__(self):
        self.edit_text = AsyncMock()
        self.answer = AsyncMock()


def _raw(handler):
    """Хендлер без декораторов админки.

    ``admin_required`` проверяет ``isinstance(event, types.CallbackQuery)``, а
    здесь события подставные. Разворачивать декораторы честнее, чем подделывать
    типы aiogram: проверяется логика редактора, а не работа самой обёртки.
    """
    return inspect.unwrap(handler)


def _callback(data: str = 'admin_ref_levels'):
    return SimpleNamespace(data=data, message=_Message(), answer=AsyncMock(), from_user=SimpleNamespace(id=1))


def _level(level=1, **kwargs):
    base = {
        'level': level,
        'is_active': True,
        'reward_mode': 'money',
        'trigger': 'every_topup',
        'referrer_percent': 10,
        'referrer_fixed_kopeks': None,
        'referrer_days': 0,
        'referrer_tariff_id': None,
        'referee_fixed_kopeks': None,
        'referee_days': 0,
        'referee_tariff_id': None,
        'max_payments': 0,
        'required_referrals': 0,
        'required_referrals_active_only': True,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


@pytest.fixture
def wired(monkeypatch):
    """Подменяет CRUD уровней и тарифов, собирая записи."""
    state = {'levels': [_level(1)], 'saved': []}

    async def fake_get_all(_db, only_active=False):
        return [lvl for lvl in state['levels'] if lvl.is_active or not only_active]

    async def fake_get(_db, level):
        return next((lvl for lvl in state['levels'] if lvl.level == level), None)

    async def fake_upsert(_db, level, **values):
        state['saved'].append({'level': level, **values})
        existing = next((lvl for lvl in state['levels'] if lvl.level == level), None)
        if existing is None:
            existing = _level(level)
            state['levels'].append(existing)
        for key, value in values.items():
            setattr(existing, key, value)
        return existing

    async def fake_delete(_db, level):
        state['levels'] = [lvl for lvl in state['levels'] if lvl.level != level]
        return True

    async def fake_tariffs(_db, include_inactive=False):
        return [SimpleNamespace(id=42, name='Про')]

    monkeypatch.setattr(editor, 'get_all_reward_levels', fake_get_all)
    monkeypatch.setattr(editor, 'get_reward_level', fake_get)
    monkeypatch.setattr(editor, 'upsert_reward_level', fake_upsert)
    monkeypatch.setattr(editor, 'delete_reward_level', fake_delete)
    monkeypatch.setattr(editor, 'get_all_tariffs', fake_tariffs)
    monkeypatch.setattr(settings, 'ADMIN_IDS', [1])
    return state


class TestSingleAnswerPerCallback:
    """Telegram принимает ровно один ответ на callback.

    Хендлер, который подтверждает действие своим текстом и потом перерисовывает
    экран, отвечал бы дважды — второй вызов падает с «query is invalid», и
    пользователь видит зависшую кнопку.
    """

    def test_render_helpers_never_answer(self):
        import ast

        for name in ('_render_levels', '_render_level'):
            tree = ast.parse(inspect.getsource(getattr(editor, name)).lstrip())
            calls = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Attribute)
                and node.attr == 'answer'
                and isinstance(node.value, ast.Name)
                and node.value.id == 'callback'
            ]
            assert not calls, f'{name} не должен отвечать на callback'

    @pytest.mark.asyncio
    async def test_toggle_active_answers_once(self, wired):
        callback = _callback('admin_ref_lvl_active:1')
        await _raw(editor.toggle_level_active)(callback, db_user=SimpleNamespace(id=1), db=None)
        assert callback.answer.await_count == 1

    @pytest.mark.asyncio
    async def test_toggle_toast_matches_what_happened(self, wired):
        """upsert правит тот же ORM-объект, поэтому чтение флага после записи врёт."""
        callback = _callback('admin_ref_lvl_active:1')  # уровень 1 активен в фикстуре
        await _raw(editor.toggle_level_active)(callback, db_user=SimpleNamespace(id=1), db=None)
        assert wired['saved'][-1]['is_active'] is False
        assert 'выключен' in callback.answer.await_args.args[0]

        callback = _callback('admin_ref_lvl_active:1')
        await _raw(editor.toggle_level_active)(callback, db_user=SimpleNamespace(id=1), db=None)
        assert wired['saved'][-1]['is_active'] is True
        assert 'включён' in callback.answer.await_args.args[0]

    @pytest.mark.asyncio
    async def test_mode_cycle_answers_once(self, wired):
        callback = _callback('admin_ref_lvl_mode:1')
        await _raw(editor.cycle_level_mode)(callback, db_user=SimpleNamespace(id=1), db=None)
        assert callback.answer.await_count == 1


class TestActiveBonusSelection:
    """«Выбрать активный бонус/бонусы» — это и есть reward_mode."""

    @pytest.mark.asyncio
    async def test_mode_cycles_money_days_both(self, wired):
        expected = ['days', 'both', 'money']
        for want in expected:
            callback = _callback('admin_ref_lvl_mode:1')
            await _raw(editor.cycle_level_mode)(callback, db_user=SimpleNamespace(id=1), db=None)
            assert wired['saved'][-1]['reward_mode'] == want

    @pytest.mark.asyncio
    async def test_trigger_cycles_all_three(self, wired):
        seen = []
        for _ in range(3):
            callback = _callback('admin_ref_lvl_trigger:1')
            await _raw(editor.cycle_level_trigger)(callback, db_user=SimpleNamespace(id=1), db=None)
            seen.append(wired['saved'][-1]['trigger'])
        assert set(seen) == {'registration', 'first_topup', 'every_topup'}


class TestNewLevelSafety:
    @pytest.mark.asyncio
    async def test_new_level_starts_disabled(self, wired):
        """Включённый при создании уровень начал бы платить пустым правилом."""
        callback = _callback('admin_ref_lvl_add')
        await _raw(editor.add_reward_level)(callback, db_user=SimpleNamespace(id=1), db=None)
        created = wired['saved'][-1]
        assert created['level'] == 2
        assert created['is_active'] is False

    @pytest.mark.asyncio
    async def test_legacy_import_starts_disabled(self, wired, monkeypatch):
        wired['levels'] = []
        monkeypatch.setattr(settings, 'REFERRAL_COMMISSION_PERCENT', 25)
        monkeypatch.setattr(settings, 'REFERRAL_INVITER_BONUS_KOPEKS', 100_00)
        monkeypatch.setattr(settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 50_00)

        callback = _callback('admin_ref_lvl_import')
        await _raw(editor.import_legacy_settings)(callback, db_user=SimpleNamespace(id=1), db=None)

        imported = wired['saved'][-1]
        assert imported['is_active'] is False, 'перенос не должен молча начать платить'
        assert imported['referrer_percent'] == 25
        assert imported['referrer_fixed_kopeks'] == 100_00
        assert imported['referee_fixed_kopeks'] == 50_00

    @pytest.mark.asyncio
    async def test_legacy_import_does_not_make_one_off_bonuses_recurring(self, wired, monkeypatch):
        """Фиксированные бонусы классической схемы разовые — за первое пополнение.

        Повод у уровня один на всё правило. Перенос с «каждым пополнением»
        превратил бы оба разовых бонуса в регулярную выплату: на живой базе это
        деньги, которых никто не обещал.
        """
        wired['levels'] = []
        monkeypatch.setattr(settings, 'REFERRAL_COMMISSION_PERCENT', 25)
        monkeypatch.setattr(settings, 'REFERRAL_INVITER_BONUS_KOPEKS', 100_00)
        monkeypatch.setattr(settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 50_00)

        callback = _callback('admin_ref_lvl_import')
        await _raw(editor.import_legacy_settings)(callback, db_user=SimpleNamespace(id=1), db=None)

        assert wired['saved'][-1]['trigger'] == 'first_topup'


class TestValueInput:
    @pytest.fixture
    def fsm(self):
        store = {'data': {'referral_level': 1, 'referral_field': 'referrer_fixed_kopeks'}, 'cleared': False}

        async def get_data():
            return store['data']

        async def clear():
            store['cleared'] = True

        return SimpleNamespace(get_data=get_data, clear=clear, store=store)

    @pytest.mark.asyncio
    async def test_money_is_entered_in_rubles_stored_in_kopeks(self, wired, fsm):
        message = SimpleNamespace(text='150,50', answer=AsyncMock(), from_user=SimpleNamespace(id=1))
        await _raw(editor.process_level_value)(message, db_user=SimpleNamespace(id=1), db=None, state=fsm)
        assert wired['saved'][-1]['referrer_fixed_kopeks'] == 15050

    @pytest.mark.asyncio
    async def test_days_are_plain_integers(self, wired, fsm):
        fsm.store['data']['referral_field'] = 'referrer_days'
        message = SimpleNamespace(text='7', answer=AsyncMock(), from_user=SimpleNamespace(id=1))
        await _raw(editor.process_level_value)(message, db_user=SimpleNamespace(id=1), db=None, state=fsm)
        assert wired['saved'][-1]['referrer_days'] == 7

    @pytest.mark.asyncio
    async def test_zero_percent_is_stored_as_null(self, wired, fsm):
        """NULL и 0 значат «не начисляется» — два представления одного состояния спутались бы."""
        fsm.store['data']['referral_field'] = 'referrer_percent'
        message = SimpleNamespace(text='0', answer=AsyncMock(), from_user=SimpleNamespace(id=1))
        await _raw(editor.process_level_value)(message, db_user=SimpleNamespace(id=1), db=None, state=fsm)
        assert wired['saved'][-1]['referrer_percent'] is None

    @pytest.mark.asyncio
    async def test_zero_days_stays_zero_not_null(self, wired, fsm):
        """Колонка дней NOT NULL: запись None уронила бы сохранение."""
        fsm.store['data']['referral_field'] = 'referrer_days'
        message = SimpleNamespace(text='0', answer=AsyncMock(), from_user=SimpleNamespace(id=1))
        await _raw(editor.process_level_value)(message, db_user=SimpleNamespace(id=1), db=None, state=fsm)
        assert wired['saved'][-1]['referrer_days'] == 0

    @pytest.mark.asyncio
    async def test_percent_above_hundred_is_rejected(self, wired, fsm):
        fsm.store['data']['referral_field'] = 'referrer_percent'
        message = SimpleNamespace(text='150', answer=AsyncMock(), from_user=SimpleNamespace(id=1))
        await _raw(editor.process_level_value)(message, db_user=SimpleNamespace(id=1), db=None, state=fsm)
        assert not wired['saved'], 'значение вне диапазона не должно сохраняться'
        assert fsm.store['cleared'] is False, 'ввод остаётся открытым для исправления'

    @pytest.mark.asyncio
    async def test_non_numeric_input_is_rejected(self, wired, fsm):
        message = SimpleNamespace(text='много', answer=AsyncMock(), from_user=SimpleNamespace(id=1))
        await _raw(editor.process_level_value)(message, db_user=SimpleNamespace(id=1), db=None, state=fsm)
        assert not wired['saved']

    @pytest.mark.asyncio
    async def test_negative_input_is_rejected(self, wired, fsm):
        message = SimpleNamespace(text='-5', answer=AsyncMock(), from_user=SimpleNamespace(id=1))
        await _raw(editor.process_level_value)(message, db_user=SimpleNamespace(id=1), db=None, state=fsm)
        assert not wired['saved']


class TestTariffSelection:
    @pytest.mark.asyncio
    async def test_no_tariff_option_stores_null(self, wired):
        callback = _callback('admin_ref_lvl_settariff:1:referrer:0')
        await _raw(editor.set_level_tariff)(callback, db_user=SimpleNamespace(id=1), db=None)
        assert wired['saved'][-1]['referrer_tariff_id'] is None

    @pytest.mark.asyncio
    async def test_referee_side_writes_its_own_column(self, wired):
        callback = _callback('admin_ref_lvl_settariff:1:referee:42')
        await _raw(editor.set_level_tariff)(callback, db_user=SimpleNamespace(id=1), db=None)
        assert wired['saved'][-1] == {'level': 1, 'referee_tariff_id': 42}


class TestCallbackRouting:
    """Каждый callback обязан попасть в свой хендлер.

    Сегодня префиксы не пересекаются: `admin_ref_lvl:` и `admin_ref_lvl_active:`
    расходятся на четырнадцатом символе, поэтому порядок регистрации не важен.
    Опасен не он, а новый префикс без разделителя — `admin_ref_lvl` поглощает
    сразу все соседние строки, и кнопка «Дни» молча открывает карточку уровня.
    Проверка гоняет реальные фильтры в реальном порядке и ловит именно это.
    """

    EXPECTED = {
        'admin_ref_levels': 'show_reward_levels',
        'admin_ref_lvl_scheme': 'toggle_reward_scheme',
        'admin_ref_lvl_add': 'add_reward_level',
        'admin_ref_lvl_import': 'import_legacy_settings',
        'admin_ref_lvl_depth': 'start_depth_edit',
        'admin_ref_lvl:2': 'show_reward_level',
        'admin_ref_lvl_active:2': 'toggle_level_active',
        'admin_ref_lvl_mode:2': 'cycle_level_mode',
        'admin_ref_lvl_trigger:2': 'cycle_level_trigger',
        'admin_ref_lvl_del:2': 'delete_level',
        'admin_ref_lvl_countmode:2': 'toggle_threshold_population',
        'admin_ref_lvl_delask:2': 'confirm_delete_level',
        'admin_ref_lvl_tariff:2:referrer': 'choose_level_tariff',
        'admin_ref_lvl_settariff:2:referrer:9': 'set_level_tariff',
        'admin_ref_lvl_edit:2:referrer_days': 'start_level_value_edit',
    }

    @staticmethod
    def _registrations():
        """Пары (фильтр, имя хендлера) в порядке регистрации."""
        registered = []

        class _Registry:
            def register(self, handler, condition):
                registered.append((condition, handler.__name__))

        class _Dispatcher:
            callback_query = _Registry()
            message = _Registry()

        editor.register_handlers(_Dispatcher())
        return registered

    def test_every_callback_reaches_its_handler(self):
        registrations = self._registrations()

        for callback_data, expected in self.EXPECTED.items():
            winner = None
            for condition, name in registrations:
                probe = SimpleNamespace(data=callback_data)
                try:
                    # MagicFilter вычисляется через resolve; .callback у него —
                    # обращение к несуществующему атрибуту, оно правдиво всегда.
                    matched = bool(condition.resolve(probe))
                except Exception:
                    matched = False
                if matched:
                    winner = name
                    break
            assert winner == expected, f'{callback_data} ушёл в {winner}, а не в {expected}'


class TestPendingInputIsCancelled:
    """Возврат на экран уровней обязан снимать ожидание ввода.

    «Отмена» ведёт на карточку уровня. Пока состояние оставалось взведённым,
    следующее произвольное сообщение админа попадало в редактор поля: набранное
    позже «100» превращалось в «процент пригласившему = 100%» без вопросов.
    Глобальный фоллбек неизвестных сообщений здесь не срабатывает — он навешен
    с StateFilter(None).
    """

    @staticmethod
    def _state(current):
        store = {'state': current, 'cleared': False}

        async def get_state():
            return store['state']

        async def clear():
            store['cleared'] = True
            store['state'] = None

        return SimpleNamespace(get_state=get_state, clear=clear, store=store)

    @pytest.mark.asyncio
    async def test_level_card_clears_pending_input(self, wired):
        from app.states import AdminStates

        state = self._state(AdminStates.referral_level_value_input.state)
        callback = _callback('admin_ref_lvl:1')
        await _raw(editor.show_reward_level)(callback, db_user=SimpleNamespace(id=1), db=None, state=state)
        assert state.store['cleared'] is True

    @pytest.mark.asyncio
    async def test_levels_list_clears_pending_input(self, wired):
        from app.states import AdminStates

        state = self._state(AdminStates.referral_level_value_input.state)
        callback = _callback('admin_ref_levels')
        await _raw(editor.show_reward_levels)(callback, db_user=SimpleNamespace(id=1), db=None, state=state)
        assert state.store['cleared'] is True

    @pytest.mark.asyncio
    async def test_other_states_are_left_alone(self, wired):
        """Чужое состояние сбрасывать нельзя — оно принадлежит другому сценарию."""
        state = self._state('AdminStates:editing_user_balance')
        callback = _callback('admin_ref_lvl:1')
        await _raw(editor.show_reward_level)(callback, db_user=SimpleNamespace(id=1), db=None, state=state)
        assert state.store['cleared'] is False


class TestDeletedLevelDoesNotResurrectActive:
    """Правка поля у удалённого уровня не должна включать его обратно.

    upsert вставляет новую строку, когда прежней нет, а колоночный default для
    is_active — True. Устаревшая кнопка «тариф» или ввод суммы после удаления
    воскрешали бы уровень СРАЗУ АКТИВНЫМ, с одним заполненным полем, и он начинал
    бы платить с ближайшего пополнения.
    """

    @pytest.mark.asyncio
    async def test_upsert_of_a_missing_level_creates_it_disabled(self, monkeypatch):
        from app.database.crud import referral_reward_level as crud

        created = {}

        class _FakeLevel:
            def __init__(self, level, is_active=True):
                created['is_active'] = is_active
                self.level = level
                self.is_active = is_active

        async def no_level(_db, _level):
            return None

        monkeypatch.setattr(crud, 'get_reward_level', no_level)
        monkeypatch.setattr(crud, 'ReferralRewardLevel', _FakeLevel)

        async def noop():
            return None

        async def noop_refresh(_obj):
            return None

        db = SimpleNamespace(commit=noop, refresh=noop_refresh, add=lambda _o: None)
        await crud.upsert_reward_level(db, 2, referrer_tariff_id=7)

        assert created['is_active'] is False


class TestEditorTraps:
    """Ситуации, из которых админ не мог выбраться или видел неправду."""

    def test_gap_in_levels_is_offered_again(self):
        """«Последний плюс один» делал удалённый средний уровень невосстановимым."""
        levels = [SimpleNamespace(level=1), SimpleNamespace(level=3)]
        assert editor._next_free_level(levels) == 2

        assert editor._next_free_level([]) == 1
        assert editor._next_free_level([SimpleNamespace(level=1), SimpleNamespace(level=2)]) == 3

    @pytest.mark.asyncio
    async def test_add_fills_the_gap(self, wired):
        wired['levels'] = [_level(1), _level(3)]
        callback = _callback('admin_ref_lvl_add')
        await _raw(editor.add_reward_level)(callback, db_user=SimpleNamespace(id=1), db=None)
        assert wired['saved'][-1]['level'] == 2

    @pytest.mark.asyncio
    async def test_delete_asks_before_removing(self, wired):
        """Удаление правила с одного касания слишком легко нажать мимо."""
        callback = _callback('admin_ref_lvl_delask:1')
        await _raw(editor.confirm_delete_level)(callback, db_user=SimpleNamespace(id=1), db=None)

        assert wired['levels'], 'подтверждение не должно ничего удалять'
        markup = callback.message.edit_text.await_args.kwargs['reply_markup']
        actions = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert 'admin_ref_lvl_del:1' in actions
        assert 'admin_ref_lvl:1' in actions, 'должен быть путь отмены'

    @pytest.mark.asyncio
    async def test_levels_list_shows_the_referee_side(self, wired):
        """Правило «только приглашённому» читалось как «не платит ничего»."""
        wired['levels'] = [_level(1, referrer_percent=None, referee_fixed_kopeks=50_000, reward_mode='money')]
        callback = _callback('admin_ref_levels')
        await _raw(editor.show_reward_levels)(callback, db_user=SimpleNamespace(id=1), db=None)

        text = callback.message.edit_text.await_args.args[0]
        assert 'Приглашённому' in text
        assert 'Пригласившему: ничего' in text

    @pytest.mark.asyncio
    async def test_level_card_warns_when_the_scheme_is_off(self, wired, monkeypatch):
        """Настроенное правило под классической схемой не применяется вовсе."""
        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'legacy')
        callback = _callback('admin_ref_lvl:1')
        await _raw(editor.show_reward_level)(callback, db_user=SimpleNamespace(id=1), db=None)

        text = callback.message.edit_text.await_args.args[0]
        assert 'НЕ применяется' in text

    @pytest.mark.asyncio
    async def test_level_card_is_quiet_when_the_scheme_is_on(self, wired, monkeypatch):
        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
        callback = _callback('admin_ref_lvl:1')
        await _raw(editor.show_reward_level)(callback, db_user=SimpleNamespace(id=1), db=None)

        assert 'НЕ применяется' not in callback.message.edit_text.await_args.args[0]

    @pytest.mark.asyncio
    async def test_tariff_picker_keeps_the_assigned_inactive_tariff(self, wired, monkeypatch):
        """Назначенный тариф мог стать неактивным — из списка он просто исчезал."""
        wired['levels'] = [_level(1, reward_mode='days', referrer_days=7, referrer_tariff_id=99)]

        async def only_active(_db, include_inactive=False):
            return [SimpleNamespace(id=42, name='Активный', is_active=True)]

        async def by_id(_db, tariff_id):
            return SimpleNamespace(id=99, name='Снятый', is_active=False)

        monkeypatch.setattr(editor, 'get_all_tariffs', only_active)
        monkeypatch.setattr('app.database.crud.tariff.get_tariff_by_id', by_id)

        callback = _callback('admin_ref_lvl_tariff:1:referrer')
        await _raw(editor.choose_level_tariff)(callback, db_user=SimpleNamespace(id=1), db=None)

        markup = callback.message.edit_text.await_args.kwargs['reply_markup']
        labels = [b.text for row in markup.inline_keyboard for b in row]
        assert any('Снятый' in label for label in labels), 'назначенный тариф обязан остаться в списке'
        assert any('✅' in label and 'Снятый' in label for label in labels), 'и быть помечен как выбранный'
        assert any('неактивен' in label for label in labels)


class TestChainDepthEditing:
    """Глубина обхода задаётся там же, где уровни.

    Настройка лежала в общем списке конфигурации, и уровни глубже неё помечались
    как неплатящие без всякого способа поднять предел с этого экрана: со стороны
    это выглядело как «уровни выше третьего просто не работают».
    """

    @pytest.mark.asyncio
    async def test_depth_button_is_on_the_levels_screen(self, wired, monkeypatch):
        monkeypatch.setattr(settings, 'REFERRAL_MAX_LEVEL_DEPTH', 3)
        callback = _callback('admin_ref_levels')
        await _raw(editor.show_reward_levels)(callback, db_user=SimpleNamespace(id=1), db=None)

        markup = callback.message.edit_text.await_args.kwargs['reply_markup']
        actions = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert 'admin_ref_lvl_depth' in actions

    @pytest.mark.asyncio
    async def test_depth_accepts_up_to_the_supported_maximum(self, wired, monkeypatch):
        from app.database.crud.referral_reward_level import MAX_SUPPORTED_LEVEL

        saved = {}

        async def fake_set(_db, key, value):
            saved[key] = value

        monkeypatch.setattr(editor.bot_configuration_service, 'set_value', fake_set)
        state = SimpleNamespace(clear=lambda: _resolved(None))
        message = SimpleNamespace(text=str(MAX_SUPPORTED_LEVEL), answer=AsyncMock(), from_user=SimpleNamespace(id=1))

        await _raw(editor.process_depth_value)(message, db_user=SimpleNamespace(id=1), db=None, state=state)

        assert saved['REFERRAL_MAX_LEVEL_DEPTH'] == MAX_SUPPORTED_LEVEL

    @pytest.mark.asyncio
    async def test_depth_beyond_the_maximum_is_rejected(self, wired, monkeypatch):
        from app.database.crud.referral_reward_level import MAX_SUPPORTED_LEVEL

        saved = {}

        async def fake_set(_db, key, value):
            saved[key] = value

        monkeypatch.setattr(editor.bot_configuration_service, 'set_value', fake_set)
        state = SimpleNamespace(clear=lambda: _resolved(None))
        message = SimpleNamespace(
            text=str(MAX_SUPPORTED_LEVEL + 1), answer=AsyncMock(), from_user=SimpleNamespace(id=1)
        )

        await _raw(editor.process_depth_value)(message, db_user=SimpleNamespace(id=1), db=None, state=state)

        assert not saved, 'глубже, чем можно завести уровней, — бессмысленно'

    def test_depth_is_clamped_to_the_supported_maximum(self, monkeypatch):
        """Значение из .env тоже нельзя задрать выше числа уровней."""
        from app.database.crud.referral_reward_level import MAX_SUPPORTED_LEVEL

        monkeypatch.setattr(settings, 'REFERRAL_MAX_LEVEL_DEPTH', 999)
        assert settings.get_referral_max_level_depth() == MAX_SUPPORTED_LEVEL

        monkeypatch.setattr(settings, 'REFERRAL_MAX_LEVEL_DEPTH', 0)
        assert settings.get_referral_max_level_depth() == 1


async def _resolved(value):
    return value


class TestLevelUnlockThresholdEditing:
    """«За что уровень» должно быть видно на карточке и настраиваться.

    Номер уровня отвечает, чьё пополнение приносит награду; порог — с какого
    момента партнёр начинает получать доход с этого звена.
    """

    @pytest.mark.asyncio
    async def test_card_states_what_unlocks_the_level(self, wired):
        wired['levels'] = [_level(1, required_referrals=10, required_referrals_active_only=True)]
        callback = _callback('admin_ref_lvl:1')
        await _raw(editor.show_reward_level)(callback, db_user=SimpleNamespace(id=1), db=None)

        text = callback.message.edit_text.await_args.args[0]
        assert 'Открывается за' in text
        assert '10 рефералов с пополнением' in text

    @pytest.mark.asyncio
    async def test_zero_threshold_reads_as_immediately_available(self, wired):
        callback = _callback('admin_ref_lvl:1')
        await _raw(editor.show_reward_level)(callback, db_user=SimpleNamespace(id=1), db=None)
        assert 'доступен сразу' in callback.message.edit_text.await_args.args[0]

    @pytest.mark.asyncio
    async def test_threshold_is_entered_as_a_plain_count(self, wired):
        state = SimpleNamespace(
            get_data=lambda: _resolved({'referral_level': 1, 'referral_field': 'required_referrals'}),
            clear=lambda: _resolved(None),
        )
        message = SimpleNamespace(text='25', answer=AsyncMock(), from_user=SimpleNamespace(id=1))
        await _raw(editor.process_level_value)(message, db_user=SimpleNamespace(id=1), db=None, state=state)

        assert wired['saved'][-1]['required_referrals'] == 25

    @pytest.mark.asyncio
    async def test_counted_population_can_be_switched(self, wired):
        """Порог по всем регистрациям берётся накруткой пустых аккаунтов."""
        callback = _callback('admin_ref_lvl_countmode:1')
        await _raw(editor.toggle_threshold_population)(callback, db_user=SimpleNamespace(id=1), db=None)
        assert wired['saved'][-1]['required_referrals_active_only'] is False

        callback = _callback('admin_ref_lvl_countmode:1')
        await _raw(editor.toggle_threshold_population)(callback, db_user=SimpleNamespace(id=1), db=None)
        assert wired['saved'][-1]['required_referrals_active_only'] is True


class TestLevelsModeToggle:
    """Переключатель «цепочка / ранги» в редакторе уровней.

    Переключение меняет и получателей выплат, и число сработавших правил на одном
    пополнении, поэтому оно отдельное действие и обязано быть видимым с экрана
    уровней: искать его в общем списке конфигурации админ не пойдёт.
    """

    @pytest.mark.asyncio
    async def test_toggle_is_on_the_levels_screen(self, wired, monkeypatch):
        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
        callback = _callback('admin_ref_levels')
        await _raw(editor.show_reward_levels)(callback, db_user=SimpleNamespace(id=1), db=None)

        markup = callback.message.edit_text.await_args.kwargs['reply_markup']
        actions = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert 'admin_ref_lvl_tiers' in actions

    @pytest.mark.asyncio
    async def test_switches_chain_to_tiers_and_back(self, wired, monkeypatch):
        saved = {}

        async def fake_set(_db, key, value):
            saved[key] = value
            monkeypatch.setattr(settings, key, value)

        monkeypatch.setattr(editor.bot_configuration_service, 'set_value', fake_set)
        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
        monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', 'chain')

        await _raw(editor.toggle_levels_mode)(_callback('admin_ref_lvl_tiers'), db_user=SimpleNamespace(id=1), db=None)
        assert saved['REFERRAL_LEVELS_MODE'] == 'tiers'

        await _raw(editor.toggle_levels_mode)(_callback('admin_ref_lvl_tiers'), db_user=SimpleNamespace(id=1), db=None)
        assert saved['REFERRAL_LEVELS_MODE'] == 'chain'

    @pytest.mark.asyncio
    async def test_env_locked_mode_is_not_written(self, wired, monkeypatch):
        saved = {}

        async def fake_set(_db, key, value):
            saved[key] = value

        monkeypatch.setattr(editor.bot_configuration_service, 'set_value', fake_set)
        monkeypatch.setattr(editor.bot_configuration_service, 'is_env_locked', lambda key: True)

        callback = _callback('admin_ref_lvl_tiers')
        await _raw(editor.toggle_levels_mode)(callback, db_user=SimpleNamespace(id=1), db=None)

        assert not saved, 'ключ из .env перезапишется при рестарте — писать его в БД нельзя'
        assert callback.answer.await_args.kwargs.get('show_alert') is True

    @pytest.mark.asyncio
    async def test_warns_when_no_tier_starts_at_zero(self, wired, monkeypatch):
        """Лестница без стартовой ступени молча прекращает выплаты всем новичкам."""

        async def fake_set(_db, key, value):
            monkeypatch.setattr(settings, key, value)

        monkeypatch.setattr(editor.bot_configuration_service, 'set_value', fake_set)
        monkeypatch.setattr(editor.bot_configuration_service, 'is_env_locked', lambda key: False)
        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
        monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', 'chain')

        async def only_gated(_db, only_active=False):
            return [_level(1, required_referrals=10), _level(2, required_referrals=25)]

        monkeypatch.setattr(editor, 'get_all_reward_levels', only_gated)

        callback = _callback('admin_ref_lvl_tiers')
        await _raw(editor.toggle_levels_mode)(callback, db_user=SimpleNamespace(id=1), db=None)

        # Предупреждения печатаются на ЭКРАНЕ: во всплывающем окне Telegram
        # обрезает всё длиннее 200 символов, и текст терялся бы целиком.
        screen = callback.message.edit_text.await_args.args[0]
        assert 'нулевого порога' in screen and '10' in screen, screen

    @pytest.mark.asyncio
    async def test_depth_editor_refuses_to_open_under_tiers(self, wired, monkeypatch):
        """Форма, которая примет значение и ничего не изменит, хуже отсутствующей кнопки."""
        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
        monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', 'tiers')

        state = SimpleNamespace(set_state=AsyncMock())
        callback = _callback('admin_ref_lvl_depth')
        await _raw(editor.start_depth_edit)(callback, db_user=SimpleNamespace(id=1), db=None, state=state)

        state.set_state.assert_not_awaited()
        assert callback.answer.await_args.kwargs.get('show_alert') is True

    @pytest.mark.asyncio
    async def test_tier_levels_beyond_depth_are_not_marked_as_dead(self, wired, monkeypatch):
        """Глубина ограничивает только цепочку — в рангах работают все уровни."""
        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
        monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', 'tiers')
        monkeypatch.setattr(settings, 'REFERRAL_MAX_LEVEL_DEPTH', 3)

        async def deep(_db, only_active=False):
            return [_level(5, required_referrals=10)]

        monkeypatch.setattr(editor, 'get_all_reward_levels', deep)

        callback = _callback('admin_ref_levels')
        await _raw(editor.show_reward_levels)(callback, db_user=SimpleNamespace(id=1), db=None)

        markup = callback.message.edit_text.await_args.kwargs['reply_markup']
        labels = [b.text for row in markup.inline_keyboard for b in row]
        assert not any('не платит' in label for label in labels), labels
        assert any('Уровень 5' in label for label in labels), labels

    @pytest.mark.asyncio
    async def test_button_label_follows_the_stored_value(self, wired, monkeypatch):
        """Ярлык обязан совпадать с тем, что сделает нажатие.

        Он рисовался по ``is_referral_tier_levels()``, которая требует включённой
        схемы уровней, а переключатель решает по ``get_referral_levels_mode()``,
        от схемы не зависящей. При классической схеме кнопка показывала «цепочка»
        поверх сохранённых «рангов», и первое нажатие не меняло ничего видимого.
        """
        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'legacy')
        monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', 'tiers')

        callback = _callback('admin_ref_levels')
        await _raw(editor.show_reward_levels)(callback, db_user=SimpleNamespace(id=1), db=None)

        markup = callback.message.edit_text.await_args.kwargs['reply_markup']
        labels = [b.text for row in markup.inline_keyboard for b in row]
        assert any('Режим: за приглашённых' in label for label in labels), labels

    @pytest.mark.asyncio
    async def test_warns_about_a_ladder_carried_over_from_chain(self, wired, monkeypatch):
        """У цепочки пороги нулевые: после переключения платит один уровень, старший.

        Без предупреждения это выглядит как «остальные уровни перестали работать».
        """

        async def fake_set(_db, key, value):
            monkeypatch.setattr(settings, key, value)

        monkeypatch.setattr(editor.bot_configuration_service, 'set_value', fake_set)
        monkeypatch.setattr(editor.bot_configuration_service, 'is_env_locked', lambda key: False)
        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
        monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', 'chain')

        async def chain_ladder(_db, only_active=False):
            return [_level(1), _level(2), _level(3)]

        monkeypatch.setattr(editor, 'get_all_reward_levels', chain_ladder)

        callback = _callback('admin_ref_lvl_tiers')
        await _raw(editor.toggle_levels_mode)(callback, db_user=SimpleNamespace(id=1), db=None)

        screen = callback.message.edit_text.await_args.args[0]
        assert 'одинаковое условие' in screen, screen


class TestCallbackAnswerLength:
    """Ответ на callback не должен превышать лимит Telegram.

    answerCallbackQuery отклоняет текст длиннее 200 символов, и aiogram его не
    подрезает: вызов падает уже ПОСЛЕ выполненного действия. Админ видит
    generic-ошибку поверх неперерисованного экрана и считает, что ничего не
    произошло, — хотя режим уже переключён и выплаты идут по-новому.
    """

    @pytest.mark.asyncio
    async def test_mode_toggle_answer_fits_the_limit(self, wired, monkeypatch):
        async def fake_set(_db, key, value):
            monkeypatch.setattr(settings, key, value)

        monkeypatch.setattr(editor.bot_configuration_service, 'set_value', fake_set)
        monkeypatch.setattr(editor.bot_configuration_service, 'is_env_locked', lambda key: False)
        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
        monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', 'chain')

        async def worst_case(_db, only_active=False):
            # Лестница, которая срабатывает по ВСЕМ четырём предупреждениям сразу.
            return [
                _level(1, required_referrals=5, trigger='registration', referrer_percent=None),
                _level(2, required_referrals=5, trigger='every_topup'),
            ]

        monkeypatch.setattr(editor, 'get_all_reward_levels', worst_case)

        callback = _callback('admin_ref_lvl_tiers')
        await _raw(editor.toggle_levels_mode)(callback, db_user=SimpleNamespace(id=1), db=None)

        answer = callback.answer.await_args.args[0]
        assert len(answer) <= editor._CALLBACK_ANSWER_LIMIT, f'{len(answer)} символов: {answer}'
        # И при этом ничего не потеряно — подробности на экране.
        assert 'одинаковое условие' in callback.message.edit_text.await_args.args[0]

    @pytest.mark.asyncio
    async def test_legacy_import_answer_fits_the_limit(self, wired, monkeypatch):
        """Перенос с заметками: уровень создан, значит ответ обязан дойти."""
        saved = {}

        async def fake_upsert(_db, level, **values):
            saved.update({'level': level, **values})
            return _level(level, **{k: v for k, v in values.items() if k != 'level'})

        monkeypatch.setattr(editor, 'upsert_reward_level', fake_upsert)
        # Импорт локальный внутри обработчика, поэтому подменяется в исходном модуле.
        monkeypatch.setattr(
            'app.services.referral_reward_service.legacy_percent_for_import',
            lambda: (30, ['Ступени комиссии не перенесены', 'Взят иной процент вместо общего']),
        )

        callback = _callback('admin_ref_lvl_import')
        await _raw(editor.import_legacy_settings)(callback, db_user=SimpleNamespace(id=1), db=None)

        answer = callback.answer.await_args.args[0]
        assert len(answer) <= editor._CALLBACK_ANSWER_LIMIT, f'{len(answer)} символов: {answer}'

    @pytest.mark.asyncio
    async def test_long_text_is_capped_not_dropped(self):
        callback = _callback('x')
        await editor._answer_capped(callback, 'я' * 500, show_alert=True)

        answer = callback.answer.await_args.args[0]
        assert len(answer) <= editor._CALLBACK_ANSWER_LIMIT
        assert answer.endswith('…'), 'обрезка обязана быть видимой'


class TestRegistrationPercentTrap:
    """Процент с поводом «за регистрацию» не может начислиться никогда.

    На этом событии пополнения нет, topup_amount_kopeks = 0, и деньги считаются
    только от суммы. Карточка при этом печатала «Процент: 50%» без единой
    оговорки, то есть выглядела рабочей настройкой.
    """

    @pytest.mark.asyncio
    async def test_card_warns_about_percent_at_registration(self, wired, monkeypatch):
        async def only_level(_db, level):
            return _level(1, trigger='registration', referrer_percent=50, referrer_fixed_kopeks=None)

        monkeypatch.setattr(editor, 'get_reward_level', only_level)

        callback = _callback('admin_ref_lvl:1')
        await _raw(editor.show_reward_level)(callback, db_user=SimpleNamespace(id=1), db=None)

        text = callback.message.edit_text.await_args.args[0]
        assert 'не начислит пригласившему ничего' in text, text

    @pytest.mark.asyncio
    async def test_no_warning_when_a_fixed_amount_is_set(self, wired, monkeypatch):
        """С фиксированной суммой правило работает — предупреждать не о чем."""

        async def only_level(_db, level):
            return _level(1, trigger='registration', referrer_percent=50, referrer_fixed_kopeks=10000)

        monkeypatch.setattr(editor, 'get_reward_level', only_level)

        callback = _callback('admin_ref_lvl:1')
        await _raw(editor.show_reward_level)(callback, db_user=SimpleNamespace(id=1), db=None)

        assert 'не начислит пригласившему ничего' not in callback.message.edit_text.await_args.args[0]

    @pytest.mark.asyncio
    async def test_no_warning_on_a_topup_trigger(self, wired, monkeypatch):
        async def only_level(_db, level):
            return _level(1, trigger='every_topup', referrer_percent=50, referrer_fixed_kopeks=None)

        monkeypatch.setattr(editor, 'get_reward_level', only_level)

        callback = _callback('admin_ref_lvl:1')
        await _raw(editor.show_reward_level)(callback, db_user=SimpleNamespace(id=1), db=None)

        assert 'не начислит пригласившему ничего' not in callback.message.edit_text.await_args.args[0]


class TestNonFiniteInput:
    """'inf' и 'nan' роняли обработчик и оставляли состояние взведённым.

    float() их принимает, проверка на отрицательность пропускает, а int() падает.
    Обработчик уходил с ошибкой, НЕ сняв состояние ввода, — и следующее
    произвольное сообщение админа попадало сюда же и переписывало денежное поле.
    Ровно так однажды «100» превратилось в «процент пригласившему = 100%».
    """

    @pytest.mark.parametrize('raw', ['inf', '-inf', 'nan', 'Infinity', 'INF', 'NaN'])
    @pytest.mark.parametrize('field', ['referrer_percent', 'referrer_days', 'max_payments', 'referrer_fixed_kopeks'])
    @pytest.mark.asyncio
    async def test_non_finite_is_refused_without_crashing(self, wired, monkeypatch, raw, field):
        saved = {}

        async def fake_upsert(_db, level, **values):
            saved.update(values)
            return _level(level)

        monkeypatch.setattr(editor, 'upsert_reward_level', fake_upsert)
        cleared = {'called': False}

        async def fake_clear():
            cleared['called'] = True

        state = SimpleNamespace(
            get_data=lambda: _resolved({'referral_level': 1, 'referral_field': field}),
            clear=fake_clear,
        )
        message = SimpleNamespace(text=raw, answer=AsyncMock(), from_user=SimpleNamespace(id=1))

        await _raw(editor.process_level_value)(message, db_user=SimpleNamespace(id=1), db=None, state=state)

        assert not saved, f'{raw} не должно сохраняться'
        message.answer.assert_awaited()
        assert '❌' in message.answer.await_args.args[0]

    @pytest.mark.asyncio
    async def test_ordinary_number_still_saves(self, wired, monkeypatch):
        """Контроль: отсечка не должна ломать обычный ввод."""
        saved = {}

        async def fake_upsert(_db, level, **values):
            saved.update(values)
            return _level(level)

        monkeypatch.setattr(editor, 'upsert_reward_level', fake_upsert)
        state = SimpleNamespace(
            get_data=lambda: _resolved({'referral_level': 1, 'referral_field': 'referrer_percent'}),
            clear=lambda: _resolved(None),
        )
        message = SimpleNamespace(text='25', answer=AsyncMock(), from_user=SimpleNamespace(id=1))

        await _raw(editor.process_level_value)(message, db_user=SimpleNamespace(id=1), db=None, state=state)

        assert saved == {'referrer_percent': 25}


class TestThresholdWarningPrecision:
    """Одинаковое ЧИСЛО ещё не значит одинаковое условие.

    «5 приглашённых» и «5 из них с пополнением» достигаются в разное время и
    конфликта не создают. Предупреждение по одним числам объявляло такую
    лестницу сломанной, хотя она работает как задумано.
    """

    @pytest.mark.asyncio
    async def test_same_number_different_population_is_not_a_conflict(self, wired, monkeypatch):
        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
        monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', 'tiers')

        levels = [
            _level(1, required_referrals=0),
            _level(2, required_referrals=5, required_referrals_active_only=True),
            _level(3, required_referrals=5, required_referrals_active_only=False),
        ]
        warnings = editor._tier_ladder_warnings(levels)

        assert not any('одинаковое условие' in w for w in warnings), warnings

    @pytest.mark.asyncio
    async def test_same_number_same_population_is_a_conflict(self, wired, monkeypatch):
        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
        monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', 'tiers')

        levels = [
            _level(1, required_referrals=0),
            _level(2, required_referrals=5, required_referrals_active_only=True),
            _level(3, required_referrals=5, required_referrals_active_only=True),
        ]
        warnings = editor._tier_ladder_warnings(levels)

        assert any('одинаковое условие' in w and '5' in w for w in warnings), warnings


class TestDepthInputIsCancelledToo:
    """«Отмена» в редакторе глубины обязана снимать состояние.

    Кнопка ведёт на экран уровней, а состояние оставалось взведённым: следующее
    произвольное число в чате переписывало REFERRAL_MAX_LEVEL_DEPTH и обрубало
    цепочку — уровни глубже переставали платить, и связать это с набранным в чат
    числом было нельзя. Для правки полей уровня эту ловушку закрыли раньше, а у
    глубины она осталась.
    """

    @pytest.mark.asyncio
    async def test_returning_to_levels_clears_the_depth_state(self, wired, monkeypatch):
        cleared = {'called': False}

        async def fake_clear():
            cleared['called'] = True

        state = SimpleNamespace(
            get_state=lambda: _resolved(editor.AdminStates.referral_depth_input.state),
            clear=fake_clear,
        )

        await _raw(editor.show_reward_levels)(
            _callback('admin_ref_levels'), db_user=SimpleNamespace(id=1), db=None, state=state
        )

        assert cleared['called'], 'состояние ввода глубины осталось взведённым'

    @pytest.mark.asyncio
    async def test_returning_to_a_level_card_clears_it_as_well(self, wired, monkeypatch):
        cleared = {'called': False}

        async def fake_clear():
            cleared['called'] = True

        state = SimpleNamespace(
            get_state=lambda: _resolved(editor.AdminStates.referral_depth_input.state),
            clear=fake_clear,
        )

        await _raw(editor.show_reward_level)(
            _callback('admin_ref_lvl:1'), db_user=SimpleNamespace(id=1), db=None, state=state
        )

        assert cleared['called']

    @pytest.mark.asyncio
    async def test_unrelated_state_is_left_alone(self, wired, monkeypatch):
        """Чужое состояние снимать нельзя — оно принадлежит другому экрану."""
        cleared = {'called': False}

        async def fake_clear():
            cleared['called'] = True

        state = SimpleNamespace(get_state=lambda: _resolved('SomeOther:state'), clear=fake_clear)

        await _raw(editor.show_reward_levels)(
            _callback('admin_ref_levels'), db_user=SimpleNamespace(id=1), db=None, state=state
        )

        assert not cleared['called']
