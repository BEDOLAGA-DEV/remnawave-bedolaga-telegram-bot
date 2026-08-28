"""Кабинетные эндпоинты уровней реферальных наград.

Проверяется то, что дорого стоит: приём мусорного режима, частичная правка,
затирающая чужие поля, и молчаливое согласие переключить схему, залоченную в
``.env`` (запись легла бы в БД, но после перезапуска победил бы файл — админ
считал бы схему переключённой).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.config import settings


def test_referral_level_routes_registered(registered_paths):
    assert '/cabinet/admin/partners/referral-levels' in registered_paths
    assert '/cabinet/admin/partners/referral-levels/{level}' in registered_paths
    assert '/cabinet/admin/partners/referral-scheme' in registered_paths
    assert '/cabinet/admin/partners/referral-depth' in registered_paths


@pytest.mark.parametrize(
    ('method', 'path', 'expected'),
    [
        ('GET', '/admin/partners/referral-levels', 'list_referral_levels'),
        ('PUT', '/admin/partners/referral-levels/2', 'upsert_referral_level'),
        ('DELETE', '/admin/partners/referral-levels/2', 'remove_referral_level'),
        ('POST', '/admin/partners/referral-levels/import-legacy', 'import_legacy_referral_settings'),
        ('PATCH', '/admin/partners/referral-scheme', 'update_referral_scheme'),
        ('PATCH', '/admin/partners/referral-depth', 'update_referral_depth'),
        # Контроль: параметризованный путь обязан продолжать работать.
        ('GET', '/admin/partners/42', 'get_partner_detail'),
    ],
)
def test_each_url_reaches_its_own_handler(method, path, expected):
    """Наличия пути в списке МАЛО — важно, какой обработчик его получит.

    ``/{user_id}`` объявлен в том же роутере и перехватывает любой литеральный
    сегмент: FastAPI берёт первый совпавший маршрут и пытается разобрать
    ``referral-levels`` как int, отдавая 422 на GET без единого параметра. Ровно
    это и уехало в прод — прежняя проверка видела путь зарегистрированным и
    молчала.
    """
    from app.cabinet.routes.admin_partners import router

    for route in router.routes:
        if method not in route.methods:
            continue
        match, _scope = route.matches({'type': 'http', 'method': method, 'path': path, 'headers': []})
        if match.name == 'FULL':
            assert route.endpoint.__name__ == expected, (
                f'{method} {path} попадает в {route.endpoint.__name__}, а не в {expected}'
            )
            return

    raise AssertionError(f'{method} {path} не совпал ни с одним маршрутом')


@pytest.fixture
def wired(monkeypatch):
    from app.cabinet.routes import admin_partners

    state = {'saved': [], 'deleted': []}

    async def fake_get_all(_db, only_active=False):
        return []

    async def fake_upsert(_db, level, **values):
        state['saved'].append({'level': level, **values})
        return SimpleNamespace(level=level)

    async def fake_delete(_db, level):
        state['deleted'].append(level)
        return True

    monkeypatch.setattr(admin_partners, 'get_all_reward_levels', fake_get_all)
    monkeypatch.setattr(admin_partners, 'upsert_reward_level', fake_upsert)
    monkeypatch.setattr(admin_partners, 'delete_reward_level', fake_delete)
    return state


def _db_returning(value):
    """Сессия, у которой любой SELECT возвращает заданный scalar."""

    class _Result:
        def scalar_one_or_none(self):
            return value

        def all(self):
            return []

    db = AsyncMock()
    db.execute = AsyncMock(return_value=_Result())
    return db


class TestValidation:
    @pytest.mark.asyncio
    async def test_unknown_reward_mode_rejected(self, wired):
        from app.cabinet.routes import admin_partners
        from app.cabinet.schemas.referral import ReferralRewardLevelUpdateRequest

        with pytest.raises(HTTPException) as excinfo:
            await admin_partners.upsert_referral_level(
                1,
                ReferralRewardLevelUpdateRequest(reward_mode='everything'),
                admin=SimpleNamespace(id=1),
                db=_db_returning(None),
            )
        assert excinfo.value.status_code == 400
        assert not wired['saved']

    @pytest.mark.asyncio
    async def test_unknown_trigger_rejected(self, wired):
        from app.cabinet.routes import admin_partners
        from app.cabinet.schemas.referral import ReferralRewardLevelUpdateRequest

        with pytest.raises(HTTPException) as excinfo:
            await admin_partners.upsert_referral_level(
                1,
                ReferralRewardLevelUpdateRequest(trigger='whenever'),
                admin=SimpleNamespace(id=1),
                db=_db_returning(None),
            )
        assert excinfo.value.status_code == 400

    @pytest.mark.asyncio
    async def test_unknown_tariff_rejected(self, wired):
        """Тариф-призрак означал бы дни, которым некуда лечь."""
        from app.cabinet.routes import admin_partners
        from app.cabinet.schemas.referral import ReferralRewardLevelUpdateRequest

        with pytest.raises(HTTPException) as excinfo:
            await admin_partners.upsert_referral_level(
                1,
                ReferralRewardLevelUpdateRequest(referrer_tariff_id=999),
                admin=SimpleNamespace(id=1),
                db=_db_returning(None),
            )
        assert excinfo.value.status_code == 400
        assert not wired['saved']


class TestPartialUpdate:
    @pytest.mark.asyncio
    async def test_only_sent_fields_are_written(self, wired):
        """Экран правит поля по одному; отправка всего объекта затирала бы правки из бота."""
        from app.cabinet.routes import admin_partners
        from app.cabinet.schemas.referral import ReferralRewardLevelUpdateRequest

        await admin_partners.upsert_referral_level(
            2,
            ReferralRewardLevelUpdateRequest(referrer_days=7),
            admin=SimpleNamespace(id=1),
            db=_db_returning(None),
        )

        assert wired['saved'] == [{'level': 2, 'referrer_days': 7}]

    @pytest.mark.asyncio
    async def test_explicit_null_tariff_is_written(self, wired):
        """«Без тарифа» — осмысленное значение, а не отсутствие поля."""
        from app.cabinet.routes import admin_partners
        from app.cabinet.schemas.referral import ReferralRewardLevelUpdateRequest

        await admin_partners.upsert_referral_level(
            1,
            ReferralRewardLevelUpdateRequest(referrer_tariff_id=None),
            admin=SimpleNamespace(id=1),
            db=_db_returning(None),
        )

        assert wired['saved'] == [{'level': 1, 'referrer_tariff_id': None}]


class TestSchemeSwitch:
    @pytest.mark.asyncio
    async def test_env_pinned_scheme_conflicts(self, wired, monkeypatch):
        from app.cabinet.routes import admin_partners
        from app.cabinet.schemas.referral import ReferralSchemeUpdateRequest

        service = SimpleNamespace(set_value=AsyncMock(), is_env_locked=lambda key: True)
        monkeypatch.setattr(admin_partners, 'bot_configuration_service', service)

        with pytest.raises(HTTPException) as excinfo:
            await admin_partners.update_referral_scheme(
                ReferralSchemeUpdateRequest(scheme='levels'),
                admin=SimpleNamespace(id=1),
                db=_db_returning(None),
            )

        assert excinfo.value.status_code == 409
        service.set_value.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_scheme_rejected(self, wired, monkeypatch):
        from app.cabinet.routes import admin_partners
        from app.cabinet.schemas.referral import ReferralSchemeUpdateRequest

        service = SimpleNamespace(set_value=AsyncMock(), is_env_locked=lambda key: False)
        monkeypatch.setattr(admin_partners, 'bot_configuration_service', service)

        with pytest.raises(HTTPException) as excinfo:
            await admin_partners.update_referral_scheme(
                ReferralSchemeUpdateRequest(scheme='pyramid'),
                admin=SimpleNamespace(id=1),
                db=_db_returning(None),
            )

        assert excinfo.value.status_code == 400
        service.set_value.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_valid_switch_persists(self, wired, monkeypatch):
        from app.cabinet.routes import admin_partners
        from app.cabinet.schemas.referral import ReferralSchemeUpdateRequest

        service = SimpleNamespace(set_value=AsyncMock(), is_env_locked=lambda key: False)
        monkeypatch.setattr(admin_partners, 'bot_configuration_service', service)
        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'legacy')

        db = _db_returning(None)
        await admin_partners.update_referral_scheme(
            ReferralSchemeUpdateRequest(scheme='levels'), admin=SimpleNamespace(id=1), db=db
        )

        service.set_value.assert_awaited_once_with(db, 'REFERRAL_REWARD_SCHEME', 'levels')


class TestDeletion:
    @pytest.mark.asyncio
    async def test_missing_level_returns_404(self, wired, monkeypatch):
        from app.cabinet.routes import admin_partners

        async def fake_delete(_db, _level):
            return False

        monkeypatch.setattr(admin_partners, 'delete_reward_level', fake_delete)

        with pytest.raises(HTTPException) as excinfo:
            await admin_partners.remove_referral_level(7, admin=SimpleNamespace(id=1), db=_db_returning(None))
        assert excinfo.value.status_code == 404


class TestTermsEndpointUnderLevels:
    """Публичные условия программы обязаны отвечать при включённой схеме.

    Ветка levels выполняется только когда схема переключена, поэтому опечатка в
    ней невидима на обычной установке и проявляется ровно в момент включения
    фичи — то есть у того, кто её включил, страница условий отдаёт 500.
    Линтер здесь не помощник: F821 в проекте отключён глобально.
    """

    @pytest.mark.asyncio
    async def test_levels_scheme_returns_terms(self, monkeypatch):
        from app.cabinet.routes import referral as route

        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')

        async def fake_all(_db):
            return {}

        monkeypatch.setattr(
            'app.services.referral_reward_service.ReferralRewardLevelService.get_all',
            classmethod(lambda cls, db: fake_all(db)),
        )

        response = await route.get_referral_terms(db=AsyncMock(), user=None)
        assert response.scheme == 'levels'
        assert response.level_descriptions == []

    @pytest.mark.asyncio
    async def test_language_follows_the_caller_when_known(self, monkeypatch):
        from app.cabinet.routes import referral as route

        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
        captured = {}

        async def fake_all(_db):
            return {}

        async def fake_describe(_db, tariff_names=None, language=None, viewer=None, referrer=None):
            captured['language'] = language
            return []

        monkeypatch.setattr(
            'app.services.referral_reward_service.ReferralRewardLevelService.get_all',
            classmethod(lambda cls, db: fake_all(db)),
        )
        monkeypatch.setattr('app.services.referral_reward_service.describe_active_levels', fake_describe)

        await route.get_referral_terms(
            db=AsyncMock(),
            user=SimpleNamespace(
                language='en', id=1, referral_reward_preference=None, referral_days_subscription_id=None
            ),
        )
        assert captured['language'] == 'en'

    @pytest.mark.asyncio
    async def test_legacy_scheme_still_public(self, monkeypatch):
        from app.cabinet.routes import referral as route

        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'legacy')
        response = await route.get_referral_terms(db=AsyncMock(), user=None)
        assert response.scheme == 'legacy'


class TestLegacyImportEndpoint:
    """Перенос старых настроек в уровень 1 — паритет с админкой бота."""

    @pytest.mark.asyncio
    async def test_creates_level_one_disabled_on_first_topup(self, wired, monkeypatch):
        from app.cabinet.routes import admin_partners

        monkeypatch.setattr(settings, 'REFERRAL_COMMISSION_PERCENT', 25)
        monkeypatch.setattr(settings, 'REFERRAL_INVITER_BONUS_KOPEKS', 100_00)
        monkeypatch.setattr(settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 50_00)
        monkeypatch.setattr(settings, 'REFERRAL_MAX_COMMISSION_PAYMENTS', 3)

        async def no_level(_db, _level):
            return None

        monkeypatch.setattr(admin_partners, 'get_reward_level', no_level)

        await admin_partners.import_legacy_referral_settings(admin=SimpleNamespace(id=1), db=_db_returning(None))

        imported = wired['saved'][-1]
        assert imported['is_active'] is False, 'перенос не должен молча начать платить'
        # Фиксированные бонусы классической схемы разовые: повод «каждое
        # пополнение» превратил бы их в регулярную выплату.
        assert imported['trigger'] == 'first_topup'
        assert imported['referrer_percent'] == 25
        assert imported['referrer_fixed_kopeks'] == 100_00
        assert imported['referee_fixed_kopeks'] == 50_00
        assert imported['max_payments'] == 3

    @pytest.mark.asyncio
    async def test_refuses_when_level_one_exists(self, wired, monkeypatch):
        from app.cabinet.routes import admin_partners

        async def existing(_db, _level):
            return SimpleNamespace(level=1)

        monkeypatch.setattr(admin_partners, 'get_reward_level', existing)

        with pytest.raises(HTTPException) as excinfo:
            await admin_partners.import_legacy_referral_settings(admin=SimpleNamespace(id=1), db=_db_returning(None))
        assert excinfo.value.status_code == 409
        assert not wired['saved'], 'существующее правило не должно затираться переносом'


class TestDepthEndpoint:
    @pytest.mark.asyncio
    async def test_depth_within_bounds_persists(self, wired, monkeypatch):
        from app.cabinet.routes import admin_partners
        from app.cabinet.schemas.referral import ReferralDepthUpdateRequest
        from app.database.crud.referral_reward_level import MAX_SUPPORTED_LEVEL

        service = SimpleNamespace(set_value=AsyncMock(), is_env_locked=lambda key: False)
        monkeypatch.setattr(admin_partners, 'bot_configuration_service', service)

        db = _db_returning(None)
        await admin_partners.update_referral_depth(
            ReferralDepthUpdateRequest(max_level_depth=MAX_SUPPORTED_LEVEL),
            admin=SimpleNamespace(id=1),
            db=db,
        )
        service.set_value.assert_awaited_once_with(db, 'REFERRAL_MAX_LEVEL_DEPTH', MAX_SUPPORTED_LEVEL)

    @pytest.mark.asyncio
    async def test_depth_beyond_supported_levels_is_rejected(self, wired, monkeypatch):
        """Глубже, чем можно завести уровней, — лишний обход на каждом пополнении."""
        from app.cabinet.routes import admin_partners
        from app.cabinet.schemas.referral import ReferralDepthUpdateRequest
        from app.database.crud.referral_reward_level import MAX_SUPPORTED_LEVEL

        service = SimpleNamespace(set_value=AsyncMock(), is_env_locked=lambda key: False)
        monkeypatch.setattr(admin_partners, 'bot_configuration_service', service)

        with pytest.raises(HTTPException) as excinfo:
            await admin_partners.update_referral_depth(
                ReferralDepthUpdateRequest(max_level_depth=MAX_SUPPORTED_LEVEL + 1),
                admin=SimpleNamespace(id=1),
                db=_db_returning(None),
            )
        assert excinfo.value.status_code == 400
        service.set_value.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_env_pinned_depth_conflicts(self, wired, monkeypatch):
        from app.cabinet.routes import admin_partners
        from app.cabinet.schemas.referral import ReferralDepthUpdateRequest

        service = SimpleNamespace(set_value=AsyncMock(), is_env_locked=lambda key: True)
        monkeypatch.setattr(admin_partners, 'bot_configuration_service', service)

        with pytest.raises(HTTPException) as excinfo:
            await admin_partners.update_referral_depth(
                ReferralDepthUpdateRequest(max_level_depth=5),
                admin=SimpleNamespace(id=1),
                db=_db_returning(None),
            )
        assert excinfo.value.status_code == 409
        service.set_value.assert_not_awaited()


class TestLevelsModeSetting:
    """Режим уровней меняет получателей выплат — значение обязано быть проверено.

    Общий эндпоинт ``PUT /admin/settings/{key}`` сверяет присланное со списком
    вариантов ТОЛЬКО если ключ в нём есть. Без записи в CHOICES он принял бы
    'ranks' или 'TIERS!', вернул 200 и показал сохранённое значение, а чтение
    молча трактовало бы его как 'chain': кабинет говорит «сохранено» о настройке,
    которая не применилась.
    """

    def test_mode_has_choice_options(self):
        from app.services.system_settings_service import bot_configuration_service

        values = [option.value for option in bot_configuration_service.get_choice_options('REFERRAL_LEVELS_MODE')]
        assert values == ['chain', 'tiers']

    def test_mode_route_is_registered_and_literal(self, registered_paths):
        assert '/cabinet/admin/partners/referral-levels-mode' in registered_paths

    @pytest.mark.parametrize(
        ('path', 'expected'),
        [('/admin/partners/referral-levels-mode', 'update_referral_levels_mode')],
    )
    def test_mode_route_is_not_shadowed(self, path, expected):
        from app.cabinet.routes.admin_partners import router

        for route in router.routes:
            if 'PATCH' not in route.methods:
                continue
            match, _scope = route.matches({'type': 'http', 'method': 'PATCH', 'path': path, 'headers': []})
            if match.name == 'FULL':
                assert route.endpoint.__name__ == expected
                return
        raise AssertionError(f'PATCH {path} не совпал ни с одним маршрутом')


class TestLevelsModeEndpoint:
    """Эндпоинт переключения режима вызывается по-настоящему.

    Мутационная проверка показала, что его тело можно было целиком превратить в
    no-op — убрать белый список, убрать проверку .env, убрать саму запись — и весь
    набор оставался зелёным. Кабинет при этом возвращал бы 200 и «сохранено», а
    бот продолжал платить по прежнему режиму: молчаливое расхождение UI и выплат.
    """

    @pytest.mark.asyncio
    async def test_valid_mode_is_persisted(self, wired, monkeypatch):
        from app.cabinet.routes import admin_partners
        from app.cabinet.schemas.referral import ReferralLevelsModeUpdateRequest

        service = SimpleNamespace(set_value=AsyncMock(), is_env_locked=lambda key: False)
        monkeypatch.setattr(admin_partners, 'bot_configuration_service', service)

        db = _db_returning(None)
        await admin_partners.update_referral_levels_mode(
            ReferralLevelsModeUpdateRequest(levels_mode='tiers'), admin=SimpleNamespace(id=1), db=db
        )
        service.set_value.assert_awaited_once_with(db, 'REFERRAL_LEVELS_MODE', 'tiers')

    @pytest.mark.asyncio
    async def test_switching_back_to_chain_is_persisted(self, wired, monkeypatch):
        """Обратный путь так же важен: без него включивший ранги не вернётся."""
        from app.cabinet.routes import admin_partners
        from app.cabinet.schemas.referral import ReferralLevelsModeUpdateRequest

        service = SimpleNamespace(set_value=AsyncMock(), is_env_locked=lambda key: False)
        monkeypatch.setattr(admin_partners, 'bot_configuration_service', service)

        db = _db_returning(None)
        await admin_partners.update_referral_levels_mode(
            ReferralLevelsModeUpdateRequest(levels_mode='chain'), admin=SimpleNamespace(id=1), db=db
        )
        service.set_value.assert_awaited_once_with(db, 'REFERRAL_LEVELS_MODE', 'chain')

    @pytest.mark.asyncio
    @pytest.mark.parametrize('bad', ['ranks', 'tier', 'TIERS!', '', 'levels'])
    async def test_unknown_mode_is_rejected(self, wired, monkeypatch, bad):
        """Неизвестная строка при чтении молча стала бы 'chain'.

        То есть кабинет показал бы «сохранено» на настройке, которая не применилась.
        """
        from app.cabinet.routes import admin_partners
        from app.cabinet.schemas.referral import ReferralLevelsModeUpdateRequest

        service = SimpleNamespace(set_value=AsyncMock(), is_env_locked=lambda key: False)
        monkeypatch.setattr(admin_partners, 'bot_configuration_service', service)

        with pytest.raises(HTTPException) as excinfo:
            await admin_partners.update_referral_levels_mode(
                ReferralLevelsModeUpdateRequest(levels_mode=bad), admin=SimpleNamespace(id=1), db=_db_returning(None)
            )
        assert excinfo.value.status_code == 400
        service.set_value.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_case_and_spaces_are_normalised(self, wired, monkeypatch):
        from app.cabinet.routes import admin_partners
        from app.cabinet.schemas.referral import ReferralLevelsModeUpdateRequest

        service = SimpleNamespace(set_value=AsyncMock(), is_env_locked=lambda key: False)
        monkeypatch.setattr(admin_partners, 'bot_configuration_service', service)

        db = _db_returning(None)
        await admin_partners.update_referral_levels_mode(
            ReferralLevelsModeUpdateRequest(levels_mode='  TIERS '), admin=SimpleNamespace(id=1), db=db
        )
        service.set_value.assert_awaited_once_with(db, 'REFERRAL_LEVELS_MODE', 'tiers')

    @pytest.mark.asyncio
    async def test_env_pinned_mode_is_refused(self, wired, monkeypatch):
        """Ключ из .env перетрёт запись при рестарте — писать его в БД нельзя."""
        from app.cabinet.routes import admin_partners
        from app.cabinet.schemas.referral import ReferralLevelsModeUpdateRequest

        service = SimpleNamespace(set_value=AsyncMock(), is_env_locked=lambda key: key == 'REFERRAL_LEVELS_MODE')
        monkeypatch.setattr(admin_partners, 'bot_configuration_service', service)

        with pytest.raises(HTTPException) as excinfo:
            await admin_partners.update_referral_levels_mode(
                ReferralLevelsModeUpdateRequest(levels_mode='tiers'),
                admin=SimpleNamespace(id=1),
                db=_db_returning(None),
            )
        assert excinfo.value.status_code == 409
        service.set_value.assert_not_awaited()


class TestLevelsPayloadReportsTheMode:
    """Кабинет рисует редактор по levels_mode из ответа — значение обязано быть настоящим."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(('stored', 'expected'), [('chain', 'chain'), ('tiers', 'tiers')])
    async def test_payload_reports_the_stored_mode(self, wired, monkeypatch, stored, expected):
        from app.cabinet.routes import admin_partners

        monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
        monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', stored)
        service = SimpleNamespace(set_value=AsyncMock(), is_env_locked=lambda key: False)
        monkeypatch.setattr(admin_partners, 'bot_configuration_service', service)

        payload = await admin_partners._levels_payload(_db_returning(None))
        assert payload.levels_mode == expected

    @pytest.mark.asyncio
    async def test_payload_reports_the_env_lock(self, wired, monkeypatch):
        """Без честного флага кнопка перестанет быть заблокированной, и правка уйдёт в БД."""
        from app.cabinet.routes import admin_partners

        service = SimpleNamespace(set_value=AsyncMock(), is_env_locked=lambda key: key == 'REFERRAL_LEVELS_MODE')
        monkeypatch.setattr(admin_partners, 'bot_configuration_service', service)

        payload = await admin_partners._levels_payload(_db_returning(None))
        assert payload.levels_mode_locked_by_env is True


class TestBotAndCabinetAgree:
    """Одинаковый ввод обязан давать одинаковый результат из бота и из кабинета.

    Границы значений знал только редактор бота: через API проходило что угодно.
    «100000 дней» кабинет сохранял, а бот на том же вводе отвечал «Максимум: 3650».
    """

    @pytest.mark.asyncio
    async def test_days_are_capped_the_same_way(self, wired, monkeypatch):
        from app.database.crud.referral_reward_level import MAX_REWARD_DAYS, upsert_reward_level
        from app.database.models import ReferralRewardLevel
        from tests.fixtures.sqlite_memory import ensure_real_aiosqlite, memory_session

        ensure_real_aiosqlite(monkeypatch)
        async with memory_session(monkeypatch, [ReferralRewardLevel.__table__]) as db:
            level = await upsert_reward_level(db, 1, referrer_days=100000, referee_days=100000)
            assert level.referrer_days == MAX_REWARD_DAYS
            assert level.referee_days == MAX_REWARD_DAYS

    @pytest.mark.asyncio
    async def test_reviving_a_deleted_level_does_not_switch_it_on(self, wired, monkeypatch):
        """Устаревший экран не должен включать пустое правило.

        Админ A удалил уровень; у админа B открыт старый снимок, он жмёт
        «Включить». Уровень создавался заново СРАЗУ активным и начинал платить по
        одному заполненному полю.
        """
        from app.database.crud.referral_reward_level import delete_reward_level, upsert_reward_level
        from app.database.models import ReferralRewardLevel
        from tests.fixtures.sqlite_memory import ensure_real_aiosqlite, memory_session

        ensure_real_aiosqlite(monkeypatch)
        async with memory_session(monkeypatch, [ReferralRewardLevel.__table__]) as db:
            await upsert_reward_level(db, 1, is_active=True, referrer_percent=25)
            assert await delete_reward_level(db, 1) is True

            revived = await upsert_reward_level(db, 1, is_active=True)
            assert revived.is_active is False, 'воскрешённый уровень обязан остаться выключенным'

    @pytest.mark.asyncio
    async def test_switching_on_an_existing_level_still_works(self, wired, monkeypatch):
        """Контроль: защита не должна мешать включать настоящий уровень."""
        from app.database.crud.referral_reward_level import upsert_reward_level
        from app.database.models import ReferralRewardLevel
        from tests.fixtures.sqlite_memory import ensure_real_aiosqlite, memory_session

        ensure_real_aiosqlite(monkeypatch)
        async with memory_session(monkeypatch, [ReferralRewardLevel.__table__]) as db:
            await upsert_reward_level(db, 1, is_active=False, referrer_percent=25)
            level = await upsert_reward_level(db, 1, is_active=True)
            assert level.is_active is True
