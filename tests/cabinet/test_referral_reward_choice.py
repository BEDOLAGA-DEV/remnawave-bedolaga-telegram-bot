"""Эндпоинт, которым кабинет сохраняет выбор пользователя.

Три вещи здесь важнее остального: запрещённую админом настройку нельзя записать
даже прямым запросом; чужую подписку нельзя выбрать; и «не прислали поле» надо
отличать от «прислали null» — null здесь значимое значение, а не пустота.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import referral as route
from app.cabinet.schemas.referral import ReferralRewardChoiceRequest
from app.config import settings


def _user(**over):
    base = {
        'id': 1,
        'language': 'ru',
        'referral_reward_preference': None,
        'referral_days_subscription_id': None,
    }
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def allowed(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_REWARD_SCHEME', 'levels')
    monkeypatch.setattr(settings, 'REFERRAL_ALLOW_REWARD_KIND_CHOICE', True)
    monkeypatch.setattr(settings, 'REFERRAL_ALLOW_DAYS_TARGET_CHOICE', True)


@pytest.fixture
def stub_terms(monkeypatch):
    """Ответ эндпоинта собирается тем же get_referral_terms — здесь он не предмет."""

    async def fake_terms(db=None, user=None):
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(route, 'get_referral_terms', fake_terms)


@pytest.fixture
def options(monkeypatch):
    store = {'ids': [10, 11]}

    async def fake_options(_db, _user):
        return [SimpleNamespace(id=i) for i in store['ids']]

    monkeypatch.setattr(route, '_days_target_options', fake_options)
    return store


class TestPermissionIsEnforcedServerSide:
    @pytest.mark.asyncio
    async def test_reward_preference_refused_when_disabled(self, allowed, stub_terms, options, monkeypatch):
        monkeypatch.setattr(settings, 'REFERRAL_ALLOW_REWARD_KIND_CHOICE', False)
        user, db = _user(), SimpleNamespace(commit=AsyncMock())

        with pytest.raises(HTTPException) as excinfo:
            await route.update_reward_choice(
                ReferralRewardChoiceRequest(reward_preference='money', set_reward_preference=True), db=db, user=user
            )

        assert excinfo.value.status_code == 403
        assert user.referral_reward_preference is None
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_days_target_refused_when_disabled(self, allowed, stub_terms, options, monkeypatch):
        monkeypatch.setattr(settings, 'REFERRAL_ALLOW_DAYS_TARGET_CHOICE', False)
        user, db = _user(), SimpleNamespace(commit=AsyncMock())

        with pytest.raises(HTTPException) as excinfo:
            await route.update_reward_choice(
                ReferralRewardChoiceRequest(days_target_subscription_id=10, set_days_target=True), db=db, user=user
            )

        assert excinfo.value.status_code == 403
        db.commit.assert_not_awaited()


class TestOwnership:
    @pytest.mark.asyncio
    async def test_a_foreign_subscription_is_refused(self, allowed, stub_terms, options):
        user, db = _user(), SimpleNamespace(commit=AsyncMock())

        with pytest.raises(HTTPException) as excinfo:
            await route.update_reward_choice(
                ReferralRewardChoiceRequest(days_target_subscription_id=999, set_days_target=True), db=db, user=user
            )

        assert excinfo.value.status_code == 400
        assert user.referral_days_subscription_id is None
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_own_subscription_is_saved(self, allowed, stub_terms, options):
        user, db = _user(), SimpleNamespace(commit=AsyncMock())

        await route.update_reward_choice(
            ReferralRewardChoiceRequest(days_target_subscription_id=11, set_days_target=True), db=db, user=user
        )

        assert user.referral_days_subscription_id == 11
        db.commit.assert_awaited()


class TestNullIsAValue:
    """null здесь значит «как настроено» и «подбирать сам», а не «не трогали»."""

    @pytest.mark.asyncio
    async def test_explicit_null_clears_the_preference(self, allowed, stub_terms, options):
        user = _user(referral_reward_preference='money')
        db = SimpleNamespace(commit=AsyncMock())

        await route.update_reward_choice(
            ReferralRewardChoiceRequest(reward_preference=None, set_reward_preference=True), db=db, user=user
        )

        assert user.referral_reward_preference is None

    @pytest.mark.asyncio
    async def test_explicit_null_clears_the_target(self, allowed, stub_terms, options):
        user = _user(referral_days_subscription_id=11)
        db = SimpleNamespace(commit=AsyncMock())

        await route.update_reward_choice(
            ReferralRewardChoiceRequest(days_target_subscription_id=None, set_days_target=True), db=db, user=user
        )

        assert user.referral_days_subscription_id is None

    @pytest.mark.asyncio
    async def test_untouched_fields_are_left_alone(self, allowed, stub_terms, options):
        """Правка одного поля не должна затирать выбор, сделанный из бота."""
        user = _user(referral_reward_preference='days', referral_days_subscription_id=11)
        db = SimpleNamespace(commit=AsyncMock())

        await route.update_reward_choice(
            ReferralRewardChoiceRequest(reward_preference='money', set_reward_preference=True), db=db, user=user
        )

        assert user.referral_reward_preference == 'money'
        assert user.referral_days_subscription_id == 11, 'подписка не присылалась — трогать её нельзя'

    @pytest.mark.asyncio
    async def test_unknown_preference_becomes_none(self, allowed, stub_terms, options):
        """Опечатка не должна лишать половины награды — падаем в «как настроено»."""
        user, db = _user(), SimpleNamespace(commit=AsyncMock())

        await route.update_reward_choice(
            ReferralRewardChoiceRequest(reward_preference='МОНЕТЫ', set_reward_preference=True), db=db, user=user
        )

        assert user.referral_reward_preference is None
