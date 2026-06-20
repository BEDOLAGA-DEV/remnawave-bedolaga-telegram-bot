import re

from app.utils.telegram_html import (
    html_to_telegram,
    info_page_faq_to_telegram,
    split_telegram_text,
)


def test_keeps_allowed_inline_tags():
    assert html_to_telegram('<p>Hello <b>world</b></p>') == 'Hello <b>world</b>'


def test_maps_tag_aliases_to_telegram_tags():
    assert html_to_telegram('<strong>x</strong> <em>y</em> <del>z</del>') == '<b>x</b> <i>y</i> <s>z</s>'


def test_strips_unsupported_tags_but_keeps_text():
    assert html_to_telegram('<span class="x">text</span>') == 'text'


def test_drops_script_and_iframe_content():
    assert html_to_telegram('<script>alert(1)</script><iframe>bad</iframe>ok') == 'ok'


def test_paragraphs_become_blank_lines():
    assert html_to_telegram('<p>one</p><p>two</p>') == 'one\n\ntwo'


def test_br_becomes_newline():
    assert html_to_telegram('<p>a<br>b</p>') == 'a\nb'


def test_unordered_list_items_get_bullets():
    assert html_to_telegram('<ul><li>a</li><li>b</li></ul>') == '• a\n• b'


def test_ordered_list_items_get_numbers():
    assert html_to_telegram('<ol><li>a</li><li>b</li></ol>') == '1. a\n2. b'


def test_heading_becomes_bold_block():
    assert html_to_telegram('<h2>Title</h2><p>body</p>') == '<b>Title</b>\n\nbody'


def test_link_kept_only_with_http_href():
    assert html_to_telegram('<a href="https://example.com">x</a>') == '<a href="https://example.com">x</a>'
    assert html_to_telegram('<a href="javascript:alert(1)">x</a>') == 'x'


def test_oversized_href_drops_anchor_but_keeps_text():
    href = 'https://example.com/?token=' + 'x' * 5000
    body = 'word ' * 1500
    rendered = html_to_telegram(f'<p>{body}</p><p>see <a href="{href}">{body}</a> end</p>')
    assert '<a' not in rendered
    assert 'see' in rendered and 'end' in rendered
    chunks = split_telegram_text(rendered)
    assert chunks
    for chunk in chunks:
        assert len(chunk) <= 4096
        assert not re.search(r'<[^>]*$', chunk)


def test_misnested_skip_closers_recover():
    assert html_to_telegram('<svg><script></svg></script><p>after</p>') == 'after'


def test_text_entities_are_escaped():
    assert html_to_telegram('<p>a & b < c</p>') == 'a &amp; b &lt; c'


def test_unclosed_tags_are_closed():
    assert html_to_telegram('<b>bold') == '<b>bold</b>'


def test_blockquote_and_code_preserved():
    assert html_to_telegram('<blockquote>q</blockquote>') == '<blockquote>q</blockquote>'
    assert html_to_telegram('<pre>x</pre>') == '<pre>x</pre>'


def test_split_short_text_single_chunk():
    assert split_telegram_text('hello') == ['hello']


def test_split_empty_returns_empty_list():
    assert split_telegram_text('') == []
    assert split_telegram_text('   ') == []


def test_split_respects_paragraph_boundaries():
    text = 'aaa\n\nbbb\n\nccc'
    chunks = split_telegram_text(text, max_length=8)
    assert all(len(chunk) <= 8 for chunk in chunks)
    assert [c for c in chunks if 'aaa' in c]
    assert [c for c in chunks if 'ccc' in c]


def test_split_hard_splits_oversized_paragraph():
    text = 'x' * 9000
    chunks = split_telegram_text(text, max_length=3500)
    assert len(chunks) == 3
    assert all(len(chunk) <= 3500 for chunk in chunks)


def test_split_closes_open_tags_in_each_chunk():
    text = '<b>' + 'a' * 4000 + '</b>'
    chunks = split_telegram_text(text, max_length=3500)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert chunk.count('<b>') == chunk.count('</b>')


def test_split_never_exceeds_telegram_hard_limit():
    text = '<b>' + 'a' * 12000 + '</b>'
    chunks = split_telegram_text(text, max_length=3500)
    assert all(len(chunk) <= 4096 for chunk in chunks)


def test_split_link_text_spanning_chunks_stays_within_hard_limit():
    href = 'https://example.com/' + 'y' * 620
    text = f'<a href="{href}">' + 'b' * 8000 + '</a>'
    chunks = split_telegram_text(text, max_length=3500)
    assert all(len(chunk) <= 4096 for chunk in chunks)
    assert all(chunk.count('<a ') == chunk.count('</a>') for chunk in chunks)


def test_hard_split_backs_off_incomplete_entity():
    text = 'x' * 97 + '&amp;' + 'y' * 200
    chunks = split_telegram_text(text, max_length=100)
    assert chunks[0] == 'x' * 97
    assert chunks[1].startswith('&amp;')


def test_faq_content_rendered_as_question_blocks():
    raw = '[{"q": "Вопрос?", "a": "<p>Ответ <b>жирный</b></p>"}]'
    rendered = info_page_faq_to_telegram(raw)
    assert rendered == '<b>Вопрос?</b>\nОтвет <b>жирный</b>'


def test_faq_content_invalid_json_returns_empty():
    assert info_page_faq_to_telegram('not json') == ''
    assert info_page_faq_to_telegram('') == ''
