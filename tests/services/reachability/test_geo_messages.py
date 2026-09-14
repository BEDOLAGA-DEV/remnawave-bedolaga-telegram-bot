"""Коды ошибок /v1/geo → слова для админа; неизвестный код — сообщение сервиса с кодом в скобках."""

from __future__ import annotations

from app.external.bschek_api import BschekAPIError
from app.services.reachability.geo_messages import geo_error_message


def err(code: str, message: str = 'msg', **details) -> BschekAPIError:
    return BschekAPIError(code=code, message=message, status=400, details=details)


def test_too_many_nodes_names_the_suggested_limit() -> None:
    text = geo_error_message(err('too_many_nodes', suggested_city_limit=120))
    assert 'Слишком много городов' in text and '120' in text
    assert 'потолок' not in geo_error_message(err('too_many_nodes'))


def test_known_codes_are_worded() -> None:
    assert 'кредитов' in geo_error_message(err('insufficient_credits'))
    assert 'Bronze' in geo_error_message(err('tier_too_low'))
    assert 'платном' in geo_error_message(err('paid_only'))
    assert 'ключа' in geo_error_message(err('api_not_available'))
    assert 'Порт' in geo_error_message(err('port_not_allowed'))
    assert 'доменн' in geo_error_message(err('heavy_requires_domain'))
    assert '20' in geo_error_message(err('too_many_targets', ''))
    assert 'три прогона' in geo_error_message(err('too_many_active'))
    assert 'обслуживании' in geo_error_message(err('maintenance'))
    assert 'Справочник' in geo_error_message(err('catalog_unavailable'))
    assert 'ни одного города' in geo_error_message(err('no_nodes'))
    assert 'подписку' in geo_error_message(err('subscription_error', 'bad sub'))
    assert 'останавливать нечего' in geo_error_message(err('not_running'))
    assert 'закроет' in geo_error_message(err('cannot_cancel'))


def test_too_many_targets_keeps_the_service_message_when_it_has_one() -> None:
    # У probe тот же код, но другой лимит: сообщение сервиса точнее нашего.
    assert geo_error_message(err('too_many_targets', 'Лимит 10 целей')) == 'Лимит 10 целей'


def test_unknown_city_lists_what_was_not_found() -> None:
    text = geo_error_message(err('unknown_city', unknown=['tmutarakan', 'kitezh']))
    assert 'не найден' in text and 'tmutarakan, kitezh' in text
    assert geo_error_message(err('unknown_isp')).startswith('Провайдер не найден')


def test_rate_limited_names_the_wait() -> None:
    exc = BschekAPIError(code='rate_limited', message='m', status=429, retry_after=7.0)
    assert '7' in geo_error_message(exc)
    assert 'через' not in geo_error_message(err('rate_limited'))


def test_unknown_code_falls_back_to_service_message_with_code() -> None:
    assert geo_error_message(err('something_new', 'Что-то новое')) == 'Что-то новое [something_new]'
