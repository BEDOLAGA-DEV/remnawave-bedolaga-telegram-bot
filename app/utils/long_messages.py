"""Отправка текстов длиннее лимита Telegram (4096) несколькими сообщениями.

Разбивка — через split_telegram_text: по границам абзацев, с переносом
незакрытых HTML-тегов между кусками. Клавиатура прикрепляется к последнему
сообщению, чтобы кнопки действия оказались под концом документа.
"""

from aiogram import types

from app.utils.telegram_html import split_telegram_text


def _split(text: str) -> list[str]:
    chunks = split_telegram_text(text)
    return chunks if chunks else [text]


async def answer_long_text(
    message: types.Message,
    text: str,
    *,
    reply_markup: types.InlineKeyboardMarkup | None = None,
    **kwargs,
) -> types.Message:
    """message.answer с разбивкой; клавиатура — на последнем куске."""
    chunks = _split(text)
    for chunk in chunks[:-1]:
        await message.answer(chunk, **kwargs)
    return await message.answer(chunks[-1], reply_markup=reply_markup, **kwargs)


async def edit_long_text(
    message: types.Message,
    text: str,
    *,
    reply_markup: types.InlineKeyboardMarkup | None = None,
    **kwargs,
) -> types.Message:
    """message.edit_text с разбивкой.

    Первый кусок редактирует исходное сообщение (без клавиатуры, если кусков
    несколько — старые кнопки при этом убираются), остальные отправляются
    новыми сообщениями, клавиатура — на последнем.
    """
    chunks = _split(text)
    if len(chunks) == 1:
        return await message.edit_text(chunks[0], reply_markup=reply_markup, **kwargs)
    await message.edit_text(chunks[0], **kwargs)
    for chunk in chunks[1:-1]:
        await message.answer(chunk, **kwargs)
    return await message.answer(chunks[-1], reply_markup=reply_markup, **kwargs)


async def send_long_text(
    bot,
    chat_id: int,
    text: str,
    *,
    reply_markup: types.InlineKeyboardMarkup | None = None,
    **kwargs,
) -> types.Message:
    """bot.send_message с разбивкой; клавиатура — на последнем куске."""
    chunks = _split(text)
    for chunk in chunks[:-1]:
        await bot.send_message(chat_id=chat_id, text=chunk, **kwargs)
    return await bot.send_message(chat_id=chat_id, text=chunks[-1], reply_markup=reply_markup, **kwargs)
