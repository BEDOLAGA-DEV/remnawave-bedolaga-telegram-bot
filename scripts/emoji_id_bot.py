# -*- coding: utf-8 -*-
"""Одноразовый помощник: собирает custom_emoji_id премиум-эмодзи в data/premium_emoji.json.

Запуск (нужен ОТДЕЛЬНЫЙ токен бота от @BotFather — основной бот держит polling):
    .venv\\Scripts\\python.exe scripts\\emoji_id_bot.py --token 123456:ABC...
    (или переменная окружения EMOJI_BOT_TOKEN)

Использование: отправь боту любое сообщение с премиум-эмодзи (можно пачкой,
например "⌛🕐⏰✏️" премиум-версиями). Бот достанет custom_emoji_id из entities
и запишет их в data/premium_emoji.json (сохраняя бэкап .bak). Обычные эмодзи
в сообщении игнорируются. После заполнения — docker restart remnawave_bot.

Опционально: --user <telegram_id> — принимать сообщения только от этого ID.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.types import Message

JSON_PATH = Path(__file__).resolve().parents[1] / 'data' / 'premium_emoji.json'
_VS16 = '️'


def _resolve_key(emojis: dict[str, str], char: str) -> str:
    """Ключ в маппинге для эмодзи: точное совпадение, VS16-близнец, иначе новый ключ."""
    if char in emojis:
        return char
    bare = char.replace(_VS16, '')
    for key in emojis:
        if key.replace(_VS16, '') == bare:
            return key
    return char


def update_mapping(text: str, entities: list) -> list[tuple[str, str, str]]:
    """Записывает custom_emoji_id из entities в JSON. Возвращает [(emoji, new_id, old_id)]."""
    data = json.loads(JSON_PATH.read_text(encoding='utf-8'))
    emojis = data['emojis']
    updated: list[tuple[str, str, str]] = []

    for ent in entities or []:
        if ent.type != 'custom_emoji' or not ent.custom_emoji_id:
            continue
        char = ent.extract_from(text)  # UTF-16-safe срез
        key = _resolve_key(emojis, char)
        old = emojis.get(key, '')
        if old == ent.custom_emoji_id:
            continue
        emojis[key] = ent.custom_emoji_id
        updated.append((key, ent.custom_emoji_id, old))

    if updated:
        shutil.copyfile(JSON_PATH, JSON_PATH.with_suffix('.json.bak'))
        JSON_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=4) + '\n',
            encoding='utf-8',
        )
    return updated


def _empty_left() -> int:
    emojis = json.loads(JSON_PATH.read_text(encoding='utf-8'))['emojis']
    return sum(1 for v in emojis.values() if not v.strip())


async def main() -> None:
    parser = argparse.ArgumentParser(description='Сбор custom_emoji_id в premium_emoji.json')
    parser.add_argument('--token', default=os.environ.get('EMOJI_BOT_TOKEN'))
    parser.add_argument('--user', type=int, default=None, help='принимать только от этого telegram id')
    args = parser.parse_args()
    if not args.token:
        raise SystemExit('Нужен токен: --token или EMOJI_BOT_TOKEN')

    bot = Bot(token=args.token)
    dp = Dispatcher()

    @dp.message()
    async def handle(message: Message) -> None:
        if args.user and (not message.from_user or message.from_user.id != args.user):
            return

        text = message.text or message.caption or ''
        entities = message.entities or message.caption_entities or []
        try:
            updated = update_mapping(text, entities)
        except Exception as exc:
            await message.answer(f'Ошибка записи: {exc}')
            return

        if not updated:
            await message.answer(
                'Премиум-эмодзи в сообщении не найдено (нужны custom emoji, не обычные), '
                'либо все id уже записаны.\n'
                f'Осталось незаполненных: {_empty_left()}'
            )
            return

        lines = [
            f'{e} -> {new_id}' + (' (заменил старый)' if old else '')
            for e, new_id, old in updated
        ]
        await message.answer(
            'Записано:\n' + '\n'.join(lines) + f'\n\nОсталось незаполненных: {_empty_left()}'
        )

    print(f'JSON: {JSON_PATH}')
    print('Бот запущен. Отправь ему премиум-эмодзи. Ctrl+C для остановки.')
    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())
