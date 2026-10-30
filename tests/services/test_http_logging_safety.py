"""HTTP exception objects must never serialize request credentials."""

import io
import logging

import aiohttp
import pytest
import structlog
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from app import logging_config


@pytest.fixture
def http_error():
    headers = CIMultiDictProxy(CIMultiDict({'X-Secret': 'sentinel-secret', 'X-MerchantId': 'sentinel-merchant'}))
    url = URL('https://payments.example/checkout')
    return aiohttp.TooManyRedirects(aiohttp.RequestInfo(url, 'POST', headers, url), (), status=302)


@pytest.mark.parametrize('structured', [False, True])
@pytest.mark.parametrize('formatter_index', [0, 1])
def test_http_errors_do_not_leak_headers(monkeypatch, http_error, structured, formatter_index):
    saved_config = structlog.get_config()
    monkeypatch.setattr(logging_config, '_configure_noisy_loggers', lambda: None)
    monkeypatch.setattr('app.logging_handler._record_error_event', lambda *_: None)
    formatter = logging_config.setup_logging()[formatter_index]
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(formatter)
    raw = logging.getLogger('test.http.safety')
    saved = raw.handlers[:], raw.level, raw.propagate
    raw.handlers, raw.propagate = [handler], False
    raw.setLevel(logging.DEBUG)
    logger = structlog.get_logger(raw.name) if structured else raw
    try:
        try:
            raise http_error
        except aiohttp.ClientError:
            fields = {'error': http_error, 'nested': [http_error]}
            if structured:
                logger.error('request failed', **fields)
            else:
                logger.exception('request failed', extra=fields)
        rendered = output.getvalue()
        assert 'sentinel-secret' not in rendered
        assert 'sentinel-merchant' not in rendered
        assert 'TooManyRedirects' in rendered
        assert '302' in rendered
        assert 'test_http_errors_do_not_leak_headers' in rendered
    finally:
        raw.handlers, level, raw.propagate = saved
        raw.setLevel(level)
        structlog.configure(**saved_config)


def test_chained_group_traceback_and_original_exception(http_error):
    from app.logging_safety import safe_traceback

    http_error.message = repr(http_error.request_info)
    try:
        try:
            raise http_error
        except aiohttp.ClientError as cause:
            raise ExceptionGroup('batch failed', [cause, ValueError('bad configuration')]) from cause
    except ExceptionGroup as error:
        rendered = safe_traceback((type(error), error, error.__traceback__))
    assert 'sentinel-secret' not in rendered
    assert 'sentinel-merchant' not in rendered
    assert 'TooManyRedirects' in rendered
    assert 'ValueError: bad configuration' in rendered
    assert 'direct cause' in rendered
    assert 'sentinel-secret' in repr(http_error)  # original is intact for classifiers/retries


def test_persisted_context_and_traceback_are_safe(http_error):
    from app.services.system_error_log_service import SystemErrorLogService

    try:
        raise http_error
    except aiohttp.ClientError as error:
        payload = SystemErrorLogService._build_payload(
            {
                'event': 'failed',
                'error': error,
                'nested': {'errors': [error]},
                'exc_info': (type(error), error, error.__traceback__),
                'headers': {'Authorization': 'sentinel-bearer'},
            },
            'test-uid',
            None,
        )
    assert 'sentinel-' not in str(payload)
    assert payload['error_type'] == 'TooManyRedirects'
    assert 'TooManyRedirects' in payload['traceback']


async def test_telegram_traceback_is_safe(monkeypatch, http_error):
    from unittest.mock import AsyncMock

    from app.logging_handler import TelegramNotifierProcessor

    send = AsyncMock(return_value='sent')
    monkeypatch.setattr('app.middlewares.global_error.send_error_to_admin_chat', send)
    try:
        raise http_error
    except aiohttp.ClientError as error:
        await TelegramNotifierProcessor._send(
            None,
            {
                'event': 'request failed',
                'error': error,
                'exc_info': (type(error), error, error.__traceback__),
            },
        )
    send.assert_awaited_once()
    assert 'sentinel-' not in send.await_args.kwargs['tb_override']
    assert 'TooManyRedirects' in send.await_args.kwargs['tb_override']


def test_container_cycles_and_header_strings():
    from app.logging_safety import safe_value

    container = []
    container.append(container)
    assert 'omitted' in str(safe_value(container))
    assert 'sentinel-' not in safe_value("headers={'X-Secret': 'sentinel-secret', 'Authorization': 'sentinel-bearer'}")


@pytest.mark.parametrize('provider', ['platega', 'mulenpay'])
async def test_provider_retries_and_final_failure_use_safe_logs(monkeypatch, provider):
    import importlib

    module = importlib.import_module(f'app.services.{provider}_service')
    service_class = module.PlategaService if provider == 'platega' else module.MulenPayService
    monkeypatch.setattr(service_class, 'is_configured', property(lambda self: True))
    saved_config = structlog.get_config()
    monkeypatch.setattr(logging_config, '_configure_noisy_loggers', lambda: None)
    formatters = logging_config.setup_logging()[:2]
    monkeypatch.setattr(module, 'logger', structlog.get_logger(module.__name__))
    raw = logging.getLogger(module.__name__)
    saved = raw.handlers[:], raw.level, raw.propagate
    outputs = [io.StringIO(), io.StringIO()]
    raw.handlers, raw.propagate = [], False
    raw.setLevel(logging.DEBUG)
    for formatter, output in zip(formatters, outputs, strict=True):
        handler = logging.StreamHandler(output)
        handler.setFormatter(formatter)
        raw.addHandler(handler)
    requests = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def request(self, method, url, **kwargs):
            requests.append(kwargs)
            info = aiohttp.RequestInfo(URL(url), method, CIMultiDictProxy(CIMultiDict(kwargs['headers'])), URL(url))
            raise aiohttp.TooManyRedirects(info, (), status=302)

    monkeypatch.setattr(aiohttp, 'ClientSession', lambda **kwargs: Session())
    service = service_class()
    service.base_url = 'https://payments.example'
    service.secret = service.api_key = 'sentinel-secret'
    service.merchant_id = 'sentinel-merchant'
    service._max_retries, service._retry_delay = 2, 0
    try:
        assert await service._request('POST', '/checkout', json_data={}) is None
        assert len(requests) == 2
        for output in outputs:
            rendered = output.getvalue()
            assert 'sentinel-' not in rendered
            assert rendered.count('client error, retrying') == 2
            assert 'failed after all retries' in rendered
            assert 'TooManyRedirects' in rendered
    finally:
        raw.handlers, level, raw.propagate = saved
        raw.setLevel(level)
        structlog.configure(**saved_config)


def test_reused_exception_in_cause_and_group_cannot_leak_url():
    from app.logging_safety import safe_traceback

    url = URL('https://payments.example/?api_key=sentinel-query')
    info = aiohttp.RequestInfo(url, 'POST', CIMultiDictProxy(CIMultiDict()), url)
    cause = aiohttp.TooManyRedirects(info, (), message='sentinel-message')
    try:
        try:
            raise cause
        except aiohttp.ClientError:
            raise ExceptionGroup('failed', [cause, cause]) from cause
    except ExceptionGroup as error:
        rendered = safe_traceback((type(error), error, error.__traceback__))
    assert 'sentinel-' not in rendered
    assert rendered.count('TooManyRedirects:') == 3


async def test_telegram_interpolated_headers_are_redacted(monkeypatch):
    from unittest.mock import AsyncMock

    from app.logging_handler import TelegramNotifierProcessor

    send = AsyncMock(return_value='sent')
    monkeypatch.setattr('app.middlewares.global_error.send_error_to_admin_chat', send)
    await TelegramNotifierProcessor._send(
        None,
        {
            'event': "request failed headers={'X-Secret': 'sentinel-secret'}",
            'error': ValueError('failed'),
        },
    )
    send.assert_awaited_once()
    assert 'sentinel-' not in str(send.await_args.args[1])
    assert type(send.await_args.args[1]).__name__ == 'ValueError'


def test_stdlib_record_is_not_persisted_in_context():
    from app.services.system_error_log_service import SystemErrorLogService

    message = "headers={'X-Secret': 'sentinel-secret'}"
    record = logging.LogRecord('test', logging.ERROR, __file__, 1, message, (), None)
    payload = SystemErrorLogService._build_payload(
        {'event': message, '_record': record, '_from_structlog': False},
        'uid',
        None,
    )
    assert 'sentinel-' not in str(payload)
    assert not payload['context']


def test_exception_object_as_exc_info_keeps_traceback(http_error):
    from app.logging_safety import render_safe_exception

    try:
        raise http_error
    except aiohttp.ClientError as error:
        event = render_safe_exception(None, 'error', {'event': 'failed', 'exc_info': error})
    assert 'exception' in event
    assert 'TooManyRedirects' in event['exception']
    assert 'test_exception_object_as_exc_info_keeps_traceback' in event['exception']
    assert 'sentinel-' not in str(event)
