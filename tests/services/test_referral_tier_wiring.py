"""Экраны обязаны ПЕРЕДАВАТЬ смотрящего и пригласившего в описания.

Сервисные функции ``describe_active_levels`` / ``describe_referee_bonus`` /
``resolve_tier_progress`` покрыты подробно, но покрытие ничего не стоит, если
вызывающий код перестанет передавать ``viewer=``/``referrer=``: описания молча
вернутся к безличной лестнице и стартовому рангу, то есть к тому самому
расхождению «обещано ≠ начислено», ради которого всё это и делалось.

Мутационная проверка показала, что снятие любого из шести аргументов на местах
вызова не роняло ни одного из 4733 тестов. Здесь проверяется именно проводка.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings


@pytest.fixture
def spies(monkeypatch):
    """Подменяет описания на шпионов, записывающих переданные аргументы."""
    calls = {'levels': [], 'referee': [], 'progress': []}

    async def fake_levels(_db, **kwargs):
        calls['levels'].append(kwargs)
        return ['Ранг 1: 5%']

    async def fake_referee(_db, **kwargs):
        calls['referee'].append(kwargs)
        return '100 ₽'

    async def fake_progress(_db, user):
        calls['progress'].append(user)

    monkeypatch.setattr('app.services.referral_reward_service.describe_active_levels', fake_levels)
    monkeypatch.setattr('app.services.referral_reward_service.describe_referee_bonus', fake_referee)
    monkeypatch.setattr('app.services.referral_reward_service.resolve_tier_progress', fake_progress)
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
    monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', 'tiers')
    return calls


class TestCabinetTerms:
    @pytest.mark.asyncio
    async def test_terms_pass_the_caller_as_viewer_and_referrer(self, spies, monkeypatch):
        from app.cabinet.routes import referral as route

        async def fake_all(_db):
            return {}

        monkeypatch.setattr(
            'app.services.referral_reward_service.ReferralRewardLevelService.get_all',
            classmethod(lambda cls, db: fake_all(db)),
        )
        user = SimpleNamespace(id=7, language='ru', referral_reward_preference=None, referral_days_subscription_id=None)

        await route.get_referral_terms(db=AsyncMock(), user=user)

        assert spies['levels'][0]['viewer'] is user, 'лестница обязана знать смотрящего'
        assert spies['referee'][0]['referrer'] is user, 'бонус приглашённого задаётся рангом смотрящего'
        assert spies['progress'] == [user]

    @pytest.mark.asyncio
    async def test_anonymous_caller_gets_no_personal_rank(self, spies, monkeypatch):
        """Эндпоинт публичный: «ваш ранг» без пользователя был бы чужим."""
        from app.cabinet.routes import referral as route

        async def fake_all(_db):
            return {}

        monkeypatch.setattr(
            'app.services.referral_reward_service.ReferralRewardLevelService.get_all',
            classmethod(lambda cls, db: fake_all(db)),
        )

        response = await route.get_referral_terms(db=AsyncMock(), user=None)

        assert spies['progress'] == []
        assert response.tier_current_level is None

    @pytest.mark.asyncio
    async def test_depth_is_reported_as_one_under_tiers(self, spies, monkeypatch):
        """В рангах цепочки нет — обещать клиенту глубину значит обещать выплаты «дедушкам»."""
        from app.cabinet.routes import referral as route

        async def fake_all(_db):
            return {}

        monkeypatch.setattr(
            'app.services.referral_reward_service.ReferralRewardLevelService.get_all',
            classmethod(lambda cls, db: fake_all(db)),
        )
        monkeypatch.setattr(settings, 'REFERRAL_MAX_LEVEL_DEPTH', 5)

        response = await route.get_referral_terms(db=AsyncMock(), user=None)

        assert response.max_level_depth == 1
        assert response.levels_mode == 'tiers'

    @pytest.mark.asyncio
    async def test_chain_mode_still_reports_the_depth(self, spies, monkeypatch):
        from app.cabinet.routes import referral as route

        async def fake_all(_db):
            return {}

        monkeypatch.setattr(
            'app.services.referral_reward_service.ReferralRewardLevelService.get_all',
            classmethod(lambda cls, db: fake_all(db)),
        )
        monkeypatch.setattr(settings, 'REFERRAL_LEVELS_MODE', 'chain')
        monkeypatch.setattr(settings, 'REFERRAL_MAX_LEVEL_DEPTH', 5)

        response = await route.get_referral_terms(db=AsyncMock(), user=None)

        assert response.max_level_depth == 5
        assert response.levels_mode == 'chain'


class TestBotInviteScreen:
    @pytest.mark.asyncio
    async def test_invite_text_promises_the_inviter_rank(self, spies, monkeypatch):
        """Текст пересылают другу: обещание в нём — про ранг ЭТОГО приглашающего."""
        from app.handlers import referral as screen

        async def fake_edit(_callback, _text, _keyboard):
            return None

        monkeypatch.setattr(screen, 'edit_or_answer_photo', fake_edit)
        monkeypatch.setattr(screen, '_reward_tariff_names', AsyncMock(return_value={}))
        monkeypatch.setattr(
            type(screen.settings), 'get_bot_referral_link', lambda self, code, bot: 'https://t.me/b?start=x'
        )
        monkeypatch.setattr(type(screen.settings), 'get_cabinet_referral_link', lambda self, code: '')
        callback = SimpleNamespace(
            bot=SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username='b'))),
            answer=AsyncMock(),
        )
        db_user = SimpleNamespace(id=3, referral_code='X', language='ru')

        await screen.create_invite_message(callback, db_user, None)

        assert spies['referee'][0]['referrer'] is db_user
