"""text/plain версия письма: конвертация HTML не должна тащить CSS/JS (#2974).

Старый конвертер в ``EmailService.send_email`` срезал регуляркой только сами
теги (``<style>``, ``</style>``), но не их содержимое — CSS-правила базового
шаблона (``body { font-family: ... }``, ``.container { ... }``) оказывались в
text/plain части письма ПЕРЕД основным текстом. Почтовики, показывающие
plain-версию, отдавали пользователю простыню CSS вместо письма.

Фикс: ``EmailService._html_to_plain_text`` — блоки ``<style>``/``<script>``
удаляются целиком до вырезания тегов; ``&amp;`` расшифровывается последним
(иначе ``&amp;lt;`` двойной расшифровкой превращается в ``<``); пустые строки
после удаления блоков схлопываются.
"""

from app.cabinet.routes.admin_email_templates import SAMPLE_CONTEXTS, _get_default_template
from app.cabinet.services.email_service import EmailService


def test_style_block_content_is_stripped() -> None:
    html = (
        '<html><head><style>\n'
        'body { font-family: Arial, sans-serif; line-height: 1.6; }\n'
        '.container { max-width: 600px; }\n'
        '</style></head>'
        '<body><p>Здравствуйте, user!</p></body></html>'
    )

    text = EmailService._html_to_plain_text(html)

    assert 'font-family' not in text
    assert '.container' not in text
    assert 'Здравствуйте, user!' in text


def test_script_block_content_is_stripped() -> None:
    html = '<body><script type="text/javascript">alert("x");</script><p>Текст письма</p></body>'

    text = EmailService._html_to_plain_text(html)

    assert 'alert' not in text
    assert 'Текст письма' in text


def test_style_block_stripped_case_insensitive_and_multiline() -> None:
    html = '<STYLE media="all">\n.button {\n  color: red;\n}\n</STYLE >\n<p>Привет</p>'

    text = EmailService._html_to_plain_text(html)

    assert 'color' not in text
    assert 'Привет' in text


def test_entities_unescaped_amp_last() -> None:
    # &amp;lt; — это экранированная строка "&lt;": расшифровка &amp; первым
    # давала бы двойную расшифровку в "<".
    html = '<p>A &amp; B, 5 &lt; 6, x&nbsp;y, &amp;lt;</p>'

    text = EmailService._html_to_plain_text(html)

    assert 'A & B' in text
    assert '5 < 6' in text
    assert 'x y' in text
    assert '&lt;' in text


def test_blank_line_runs_are_collapsed() -> None:
    html = '<style>\nbody { color: #333; }\n</style>\n\n\n\n<p>Первая строка</p>\n\n\n\n\n<p>Вторая строка</p>'

    text = EmailService._html_to_plain_text(html)

    assert '\n\n\n' not in text
    assert not text.startswith('\n')
    assert 'Первая строка' in text
    assert 'Вторая строка' in text


def test_real_default_template_produces_clean_plain_text() -> None:
    """Регрессия на живом шаблоне: дефолтное письмо верификации собирается на
    базовом шаблоне с большим <style>-блоком — раньше весь этот CSS уезжал в
    text/plain перед текстом письма."""
    template = _get_default_template('email_verification', 'ru', SAMPLE_CONTEXTS['email_verification'])
    assert template is not None
    body_html = template['body_html']
    assert '<style' in body_html.lower(), 'тест потерял смысл: в дефолтном шаблоне больше нет <style>'

    text = EmailService._html_to_plain_text(body_html)

    assert 'font-family' not in text
    assert '{' not in text, 'CSS-правила утекли в text/plain'
    assert text.strip(), 'plain-версия не должна быть пустой'


def test_unclosed_style_block_does_not_leak_css() -> None:
    """Битый шаблон (частый случай для кастомных писем из админки): открытый
    <style> без закрывающего тега не должен утечь телом CSS в text/plain."""
    html = '<body><p>Здравствуйте</p><style>\nbody { color: #333; }\n.container { max-width: 600px; }'

    text = EmailService._html_to_plain_text(html)

    assert 'color' not in text
    assert 'max-width' not in text
    assert '{' not in text
    assert 'Здравствуйте' in text


def test_send_email_plain_part_is_clean(monkeypatch) -> None:
    """End-to-end: send_email должен положить в text/plain часть очищенный текст,
    а не сырой CSS — фикс живёт именно в этой сборке multipart/alternative."""
    from email import message_from_string

    service = EmailService()
    # from_email/from_name are read-only properties over settings — patch the
    # descriptors on the class; is_configured is a method, patch on the instance.
    monkeypatch.setattr(service, 'is_configured', lambda: True)
    monkeypatch.setattr(type(service), 'from_email', property(lambda self: 'noreply@example.com'))
    monkeypatch.setattr(type(service), 'from_name', property(lambda self: 'Service'))

    sent: dict[str, str] = {}

    class _FakeSMTP:
        def sendmail(self, from_addr, to_addrs, msg):
            sent['raw'] = msg

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(service, '_get_smtp_connection', lambda: _FakeSMTP())

    html = '<html><head><style>body { font-family: Arial; }</style></head><body><p>Код: 123456</p></body></html>'
    ok = service.send_email('user@example.com', 'Тема', html)

    assert ok is True
    message = message_from_string(sent['raw'])
    plain_parts = [
        part.get_payload(decode=True).decode('utf-8')
        for part in message.walk()
        if part.get_content_type() == 'text/plain'
    ]
    assert plain_parts, 'письмо должно содержать text/plain часть'
    plain = plain_parts[0]
    assert 'font-family' not in plain, 'CSS утёк в text/plain'
    assert 'Код: 123456' in plain
