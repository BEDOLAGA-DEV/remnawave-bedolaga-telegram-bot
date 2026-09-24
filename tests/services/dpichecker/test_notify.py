"""Текст админ-уведомления о прогоне монитора DPI//CHECKER: имя, страна, «N из M» по ресурсам,
неудачи подряд, ссылка в кабинет; без ключей и ссылок прокси, HTML экранирован."""

from types import SimpleNamespace

from app.services.dpichecker.notify import monitor_run_text
from app.services.dpichecker.presenter import present_check
from tests.fixtures.dpichecker_fixtures import load_dpichecker_fixture


def _fx(name):
    return load_dpichecker_fixture(name)['body']


ACTION = SimpleNamespace(id=12, label='Finland <main>', targets=[{'value': 'google.com', 'name': 'Finland'}])


def test_text_has_name_country_and_counts():
    view = present_check(_fx('check_watcher_run'), {'google.com': 'Finland'})
    text = monitor_run_text(ACTION, _fx('monitor_after_run'), view, cabinet_url='https://cab.example')
    assert 'DPI//CHECKER' in text
    assert 'Finland &lt;main&gt;' in text
    assert 'Россия' in text
    assert '10 из 10' in text
    assert 'https://cab.example/admin/dpichecker?tab=monitors' in text


def test_alert_mentions_fails_in_a_row():
    view = present_check(_fx('check_watcher_run'), {})
    monitor = {**_fx('monitor_after_run'), 'consecutive_fails': 3, 'last_status': 'down'}
    text = monitor_run_text(ACTION, monitor, view, cabinet_url=None)
    assert 'неудач подряд: 3' in text
    assert 'href' not in text


def test_no_secrets_in_text():
    view = present_check(_fx('check_vpn'), {})
    text = monitor_run_text(ACTION, _fx('monitor_after_run'), view, cabinet_url=None)
    assert 'vless://' not in text and 'tg://' not in text
