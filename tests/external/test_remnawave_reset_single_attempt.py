"""Reset acknowledgement must not be obscured by replay or optional Happ I/O."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest

from app.external import remnawave_api as mod
from tests.external.test_remnawave_3_0_0 import _api_with_session, _FakeResponse


@pytest.mark.parametrize('code', [429, 502, 503, 504])
async def test_reset_does_not_replay_unacknowledged_post(monkeypatch, code):
    monkeypatch.setattr(mod.asyncio, 'sleep', AsyncMock())
    monkeypatch.setattr(mod.RemnaWaveAPI, '_throttled_until', 0.0)
    api, session = _api_with_session(_FakeResponse(code, '{"message":"test failure"}'))
    with pytest.raises(mod.RemnaWaveAPIError):
        await api.reset_user_traffic(9001)
    assert len(session.calls) == 1
    monkeypatch.setattr(mod.RemnaWaveAPI, '_throttled_until', 0.0)


async def test_reset_connection_loss_is_one_attempt(monkeypatch):
    monkeypatch.setattr(mod.asyncio, 'sleep', AsyncMock())
    api, _ = _api_with_session(_FakeResponse(200, '{}'))
    api.session.request = Mock(side_effect=aiohttp.ClientConnectionError('response lost'))
    with pytest.raises(mod.RemnaWaveAPIError):
        await api.reset_user_traffic(9001)
    assert api.session.request.call_count == 1


async def test_raw_reset_ack_precedes_optional_enrichment():
    api, _ = _api_with_session(_FakeResponse(200, '{"response":{}}'))
    user = SimpleNamespace(id=9001)
    api._parse_user = Mock(return_value=user)
    api.enrich_user_with_happ_link = AsyncMock(side_effect=TimeoutError('optional enrichment'))
    assert await api.reset_user_traffic(9001, enrich=False) is user
    api.enrich_user_with_happ_link.assert_not_awaited()


@pytest.mark.parametrize('raw,known', [(None, True), ('', False), (0, False), ('2026-01-01T00:00:00Z', True)])
def test_reset_marker_presence_is_not_confused_with_missing_or_invalid(raw, known):
    from tests.external.test_remnawave_3_0_0 import _api, _user_payload

    api = _api()
    assert api._parse_user(_user_payload(lastTrafficResetAt=raw)).last_traffic_reset_at_known is known
    assert api._parse_user(_user_payload()).last_traffic_reset_at_known is False


def test_invalid_reset_marker_is_rejected_before_it_can_be_used_as_baseline():
    from tests.external.test_remnawave_3_0_0 import _api, _user_payload

    with pytest.raises(ValueError):
        _api()._parse_user(_user_payload(lastTrafficResetAt='not-a-date'))
