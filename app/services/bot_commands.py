"""Telegram command menus: private scopes, localized descriptions and durable cleanup."""

import asyncio
import json

import structlog
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat

from app.config import settings
from app.database.crud.system_setting import get_setting_value, upsert_system_setting
from app.database.database import AsyncSessionLocal
from app.localization.texts import get_texts


logger = structlog.get_logger(__name__)

# Only actual message handlers belong here. Menu visibility never grants access.
_COMMANDS = ('start', 'contests', 'admin_help', 'rules_stats', 'clear_rules', 'monitoring')
_FALLBACK_DESCRIPTIONS = (
    'Main menu',
    'Contests',
    'Administrator help',
    'Rules statistics',
    'Clear rules',
    'Monitoring status',
)


def build_menu(language: str, *, admin: bool = False) -> list[BotCommand]:
    language = language.strip().lower()
    language = 'ua' if language == 'uk' else language
    texts = get_texts(language)
    count = len(_COMMANDS) if admin else 2
    commands = []
    for name, fallback in zip(_COMMANDS[:count], _FALLBACK_DESCRIPTIONS[:count], strict=True):
        description = texts.get(f'BOT_COMMAND_{name.upper()}', fallback)
        if not isinstance(description, str) or not 1 <= len(description.strip()) <= 256:
            description = fallback
        commands.append(BotCommand(command=name, description=description.strip()))
    if settings.TELEGRAM_STARS_ENABLED:
        description = texts.get('BOT_COMMAND_PAYSUPPORT', 'Payment support')
        if not isinstance(description, str) or not 1 <= len(description.strip()) <= 256:
            description = 'Payment support'
        commands.insert(1, BotCommand(command='paysupport', description=description.strip()))
    return commands


def desired_menus(languages: list[str], default_language: str, admin_ids: list[int]) -> dict:
    localized = {'': default_language}
    for language in languages:
        code = language.strip().lower().replace('_', '-').split('-')[0]
        code = 'uk' if code == 'ua' else code
        if len(code) == 2 and code.isascii() and code.isalpha():
            localized[code] = language
    scopes = ['private', *(str(admin_id) for admin_id in sorted(set(admin_ids)) if admin_id > 0)]
    return {
        (scope, code): build_menu(language, admin=scope != 'private')
        for scope in scopes
        for code, language in localized.items()
    }


def _scope(key: str):
    return BotCommandScopeAllPrivateChats() if key == 'private' else BotCommandScopeChat(chat_id=int(key))


class CommandMenuStore:
    """Internal journal, not a configurable setting. Written before remote mutations."""

    def __init__(self, bot_id: int):
        self.key = f'bot_command_scopes:{bot_id}'

    async def load(self) -> set[tuple[str, str]]:
        async with AsyncSessionLocal() as db:
            raw = await get_setting_value(db, self.key)
        if raw is None:
            return set()
        entries = json.loads(raw)
        # Fail closed on a damaged journal: never guess which remote scopes to delete.
        if not isinstance(entries, list) or any(
            not isinstance(entry, list)
            or len(entry) != 2
            or not all(isinstance(value, str) for value in entry)
            or not (entry[0] == 'private' or (entry[0].isascii() and entry[0].isdigit() and int(entry[0]) > 0))
            or not (entry[1] == '' or (len(entry[1]) == 2 and entry[1].isascii() and entry[1].isalpha()))
            for entry in entries
        ):
            raise ValueError('Invalid bot command scope journal')
        return {tuple(entry) for entry in entries}

    async def save(self, scopes: set[tuple[str, str]]) -> None:
        async with AsyncSessionLocal() as db:
            await upsert_system_setting(db, self.key, json.dumps(sorted(scopes)))
            await db.commit()


class CommandMenuSync:
    def __init__(self, bot, store):
        self.bot = bot
        self.store = store
        self.known = None
        self.applied = {}

    async def reconcile(self, desired: dict) -> None:
        if self.known is None:
            self.known = await self.store.load()
        errors = []
        for key in sorted(self.known - desired.keys()):
            # DELETE may succeed even if its response or journal commit is lost.
            # A re-added administrator must then receive a fresh publication.
            self.applied.pop(key, None)
            try:
                await self.bot.delete_my_commands(scope=_scope(key[0]), language_code=key[1])
                await asyncio.sleep(0.1)
                remaining = self.known - {key}
                await self.store.save(remaining)
                self.known = remaining
            except TelegramRetryAfter:
                raise
            except Exception as error:
                errors.append(error)
        for key, commands in desired.items():
            fingerprint = tuple((command.command, command.description) for command in commands)
            if self.applied.get(key) == fingerprint:
                continue
            try:
                if key not in self.known:
                    known = self.known | {key}
                    await self.store.save(known)
                    self.known = known
                await self.bot.set_my_commands(commands=commands, scope=_scope(key[0]), language_code=key[1])
                await asyncio.sleep(0.1)
                self.applied[key] = fingerprint
            except TelegramRetryAfter:
                raise
            except Exception as error:
                errors.append(error)
        if errors:
            raise errors[0]

    async def run(self) -> None:
        while True:
            delay = 60
            try:
                await self.reconcile(
                    desired_menus(
                        settings.get_available_languages(), settings.DEFAULT_LANGUAGE, settings.get_admin_ids()
                    )
                )
            except TelegramRetryAfter as error:
                delay = max(delay, error.retry_after + 1)
                logger.warning('Telegram command menu rate limited', retry_after=delay)
            except Exception as error:
                logger.warning('Telegram command menu sync failed; will retry', error_type=type(error).__name__)
            await asyncio.sleep(delay)
