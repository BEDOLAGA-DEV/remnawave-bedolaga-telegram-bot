"""
Утилита для конвертации AI-ответов (Markdown) в Telegram HTML.

Убирает <think> блоки и конвертирует Markdown-разметку в поддерживаемый
Telegram HTML: <b>, <i>, <code>, <pre>, <blockquote>, <s>, <u>.
"""

import html
import re


def sanitize_ai_response(text: str) -> str:
    """
    Полная обработка AI-ответа для Telegram:
    1. Убирает <think>...</think> блоки (reasoning-модели)
    2. Конвертирует Markdown → Telegram HTML
    """
    # 1. Убираем thinking-блоки
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

    # 2. Конвертируем Markdown → Telegram HTML
    text = _markdown_to_telegram_html(text)

    return text


def _markdown_to_telegram_html(text: str) -> str:
    """Конвертация Markdown-разметки в Telegram HTML."""

    # Сохраняем блоки кода (```...```) чтобы не обрабатывать их содержимое
    code_blocks: list[str] = []

    def _save_code_block(match: re.Match) -> str:
        lang = match.group(1) or ''
        code = html.escape(match.group(2))
        placeholder = f'\x00CODEBLOCK{len(code_blocks)}\x00'
        if lang:
            code_blocks.append(f'<pre><code class="language-{html.escape(lang)}">{code}</code></pre>')
        else:
            code_blocks.append(f'<pre>{code}</pre>')
        return placeholder

    text = re.sub(r'```(\w*)\n?(.*?)```', _save_code_block, text, flags=re.DOTALL)

    # Инлайн-код `...`
    inline_codes: list[str] = []

    def _save_inline_code(match: re.Match) -> str:
        code = html.escape(match.group(1))
        placeholder = f'\x00INLINE{len(inline_codes)}\x00'
        inline_codes.append(f'<code>{code}</code>')
        return placeholder

    text = re.sub(r'`([^`]+)`', _save_inline_code, text)

    # Эскейпим оставшийся HTML (чтобы пользовательский текст не ломал разметку)
    text = html.escape(text)

    # Жирный: **text** или __text__
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)

    # Курсив: *text* или _text_ (но не внутри слов с _)
    text = re.sub(r'(?<!\w)\*([^*]+?)\*(?!\w)', r'<i>\1</i>', text)
    text = re.sub(r'(?<!\w)_([^_]+?)_(?!\w)', r'<i>\1</i>', text)

    # Зачёркнутый: ~~text~~
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)

    # Цитата: строки начинающиеся с > (Telegram blockquote)
    lines = text.split('\n')
    result_lines: list[str] = []
    in_quote = False
    quote_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith('&gt;'):
            # html.escape превратил > в &gt;
            quote_content = stripped[4:].strip()
            quote_lines.append(quote_content)
            in_quote = True
        else:
            if in_quote:
                result_lines.append(f'<blockquote>{"<br>".join(quote_lines)}</blockquote>')
                quote_lines = []
                in_quote = False
            result_lines.append(line)

    if in_quote:
        result_lines.append(f'<blockquote>{"<br>".join(quote_lines)}</blockquote>')

    text = '\n'.join(result_lines)

    # Восстанавливаем блоки кода
    for i, block in enumerate(code_blocks):
        text = text.replace(f'\x00CODEBLOCK{i}\x00', block)

    # Восстанавливаем инлайн-код
    for i, code in enumerate(inline_codes):
        text = text.replace(f'\x00INLINE{i}\x00', code)

    return text
