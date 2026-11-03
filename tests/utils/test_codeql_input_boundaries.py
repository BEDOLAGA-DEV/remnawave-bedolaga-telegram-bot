"""Adversarial inputs must not stall parsing or survive article sanitization."""

import signal
from contextlib import contextmanager

import pytest

from app.cabinet.schemas.news import _sanitize_html_content
from app.services.server_status_service import ServerStatusService
from app.utils import rich_admin, validators


@contextmanager
def parsing_deadline():
    def expired(*_):
        raise TimeoutError('parsing exceeded two seconds')

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 2)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_unterminated_escaped_metric_label_is_bounded():
    with parsing_deadline():
        assert ServerStatusService()._parse_labels('A="' + '\\!' * 100) == {}


@pytest.mark.parametrize('validate', [validators.validate_html_tags, validators.validate_html_structure])
def test_unterminated_long_tag_is_bounded(validate):
    with parsing_deadline():
        validate('<A' + '-' * 100_000)


@pytest.mark.parametrize('tag', ['pre', 'blockquote'])
def test_unterminated_rich_blocks_are_bounded(tag):
    with parsing_deadline():
        assert rich_admin._inline_newlines_to_rich(f'<{tag}>' * 20_000)


@pytest.mark.parametrize(
    'markup',
    [
        '<img src=javascript:alert(1)>',
        '<a href="java&#x73;cript:alert(1)">link</a>',
        '<img src=x oNerror=alert(1)>',
        '<scr<script>ipt>alert(1)</scr</script>ipt>',
    ],
)
def test_article_sanitization_removes_executable_markup(markup):
    result = _sanitize_html_content(markup).lower()
    assert 'javascript:' not in result
    assert '&#x73;' not in result
    assert 'onerror' not in result
    assert '<script' not in result


def test_metric_label_escapes_and_adjacent_labels_still_parse():
    labels = ServerStatusService()._parse_labels(r'name="edge\"one",address="example.com",protocol="vless"')
    assert labels == {'name': 'edge"one', 'address': 'example.com', 'protocol': 'vless'}


def test_rich_segmentation_preserves_existing_short_input_semantics():
    import itertools
    import re

    tokens = ['text\n', '<pre>', '</pre>', '<blockquote>', '</blockquote>']
    for size in range(5):
        for pieces in itertools.product(tokens, repeat=size):
            text = ''.join(pieces)
            expected = re.split(r'(<blockquote>.*?</blockquote>|<pre>.*?</pre>)', text, flags=re.IGNORECASE | re.DOTALL)
            assert rich_admin._split_blocks(text, frozenset({'blockquote', 'pre'})) == expected


@pytest.mark.parametrize(
    ('source', 'expected'),
    [
        (
            'https://github.com/owner/repo/blob/main/list.txt',
            'https://raw.githubusercontent.com/owner/repo/main/list.txt',
        ),
        (
            'https://github.com/owner/repo/blob/main/a/blob/list.txt?q=github.com',
            'https://raw.githubusercontent.com/owner/repo/main/a/blob/list.txt?q=github.com',
        ),
        ('https://github.com.example/list.txt', 'https://github.com.example/list.txt'),
        ('https://example.com/github.com/blob/list.txt', 'https://example.com/github.com/blob/list.txt'),
        ('https://example.com/?source=github.com', 'https://example.com/?source=github.com'),
        (
            'https://raw.githubusercontent.com/owner/repo/main/list.txt',
            'https://raw.githubusercontent.com/owner/repo/main/list.txt',
        ),
    ],
)
def test_blacklist_conversion_uses_parsed_host_and_blob_path(source, expected):
    from app.services.blacklist_service import _blacklist_raw_url

    assert _blacklist_raw_url(source) == expected


def test_article_keeps_editor_formatting_and_uploaded_media():
    result = _sanitize_html_content(
        '<h2>Title</h2><p style="text-align:center;color:red">Text <strong>bold</strong></p>'
        '<img src="https://cabinet.example/api/cabinet/uploads/images/a.jpg" alt="image">'
        '<video src="http://cabinet.example/api/cabinet/uploads/videos/a.mp4"></video>'
        '<a href="https://example.com">link</a>'
        '<table><tbody><tr><td colspan="2">cell</td></tr></tbody></table>'
    )
    assert '<h2>Title</h2>' in result
    assert '<strong>bold</strong>' in result
    assert 'text-align:center' in result.replace(' ', '')
    assert 'color' not in result
    assert '/uploads/images/a.jpg' in result
    assert '/uploads/videos/a.mp4' in result
    assert 'controls=' in result
    assert 'noopener noreferrer' in result
    assert 'colspan="2"' in result


@pytest.mark.parametrize('host', ['www.youtube.com', 'youtube.com', 'www.youtube-nocookie.com', 'player.vimeo.com'])
def test_article_preserves_trusted_embeds_with_forced_sandbox(host):
    result = _sanitize_html_content(
        f'<iframe src="https://{host}/embed/example" sandbox="allow-top-navigation"></iframe>'
    )
    assert f'src="https://{host}/embed/example"' in result
    assert 'allow-top-navigation' not in result
    assert 'sandbox="allow-scripts allow-same-origin allow-presentation"' in result


@pytest.mark.parametrize(
    'url',
    ['https://www.youtube.com.example/embed/a', 'javascript:alert(1)', '//youtube.com/a', 'https://user@youtube.com/a'],
)
def test_article_removes_untrusted_embed_source(url):
    assert 'src=' not in _sanitize_html_content(f'<iframe src="{url}"></iframe>')


def test_legacy_article_response_is_sanitized_without_rewriting_storage():
    from datetime import UTC, datetime

    from app.cabinet.schemas.news import NewsArticleResponse

    stored = {
        'id': 1,
        'title': 'Legacy',
        'slug': 'legacy',
        'content': '<p>Saved text</p><a href="java&#x73;cript:alert(1)">link</a>',
        'excerpt': None,
        'category': 'News',
        'category_color': '#ffffff',
        'tag': None,
        'featured_image_url': None,
        'is_published': True,
        'is_featured': False,
        'published_at': None,
        'read_time_minutes': 1,
        'views_count': 0,
        'created_at': datetime.now(UTC),
        'updated_at': None,
    }
    response = NewsArticleResponse.model_validate(stored)
    assert '&#x73;' not in response.content
    assert 'javascript:' not in response.content
    assert '<p>Saved text</p>' in response.content
    assert '&#x73;' in stored['content']
