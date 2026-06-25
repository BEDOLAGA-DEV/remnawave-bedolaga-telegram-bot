from app.utils.subscription_utils import build_scheme_redirect_link


def test_returns_none_when_template_empty():
    assert build_scheme_redirect_link('incy://crypt1/abc', None) is None
    assert build_scheme_redirect_link('incy://crypt1/abc', '') is None


def test_appends_url_encoded_link_when_template_ends_with_eq():
    out = build_scheme_redirect_link('incy://crypt1/a b', 'https://r.example/?redirect_to=')
    assert out == 'https://r.example/?redirect_to=incy%3A%2F%2Fcrypt1%2Fa%20b'


def test_substitutes_link_placeholder():
    out = build_scheme_redirect_link('incy://crypt1/abc', 'https://r.example/?to={link}')
    assert out == 'https://r.example/?to=incy%3A%2F%2Fcrypt1%2Fabc'


def test_returns_none_for_empty_deep_link():
    assert build_scheme_redirect_link('', 'https://r.example/?redirect_to=') is None


def test_substitutes_raw_placeholder_unencoded():
    out = build_scheme_redirect_link('incy://crypt1/abc', 'https://r.example/?to={link}&raw={link_raw}')
    assert out == 'https://r.example/?to=incy%3A%2F%2Fcrypt1%2Fabc&raw=incy://crypt1/abc'


def test_appends_when_template_has_no_placeholder_and_no_trailing_sep():
    out = build_scheme_redirect_link('incy://crypt1/abc', 'https://r.example/redir/')
    assert out == 'https://r.example/redir/incy%3A%2F%2Fcrypt1%2Fabc'
