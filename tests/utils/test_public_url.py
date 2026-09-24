"""Внешняя ссылка на файл за обратным прокси: uvicorn верит X-Forwarded-Proto только от 127.0.0.1, а прокси
в соседнем контейнере — нет, и `request.url_for` отдаёт http:// внутреннего адреса. Telegram downloadFile
качает только по HTTPS, браузер на https-кабинете такую загрузку блокирует."""

from starlette.datastructures import URL, Headers

from app.utils.public_url import public_url


class _Req:
    def __init__(self, url: str, headers: dict[str, str] | None = None):
        self.url = URL(url)
        self.headers = Headers(headers or {})


def test_takes_scheme_and_host_from_proxy_headers():
    req = _Req('http://bot:8080/x', {'x-forwarded-proto': 'https', 'x-forwarded-host': 'api.example.com'})
    assert public_url(req, 'http://bot:8080/cabinet/f/1?token=a') == 'https://api.example.com/cabinet/f/1?token=a'


def test_without_proxy_headers_link_is_unchanged():
    req = _Req('http://localhost:8000/x', {'host': 'localhost:8000'})
    assert public_url(req, 'http://localhost:8000/cabinet/f/1') == 'http://localhost:8000/cabinet/f/1'


def test_first_value_of_chained_headers_and_host_fallback():
    req = _Req('http://bot:8080/x', {'x-forwarded-proto': 'https, http', 'host': 'api.example.com'})
    assert public_url(req, 'http://bot:8080/p') == 'https://api.example.com/p'


def test_unknown_scheme_in_header_is_not_trusted():
    req = _Req('http://bot:8080/x', {'x-forwarded-proto': 'javascript', 'host': 'api.example.com'})
    assert public_url(req, 'http://bot:8080/p').startswith('https://api.example.com/')
