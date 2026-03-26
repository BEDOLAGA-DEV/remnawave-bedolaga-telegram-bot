"""Game strategies for different contest types."""

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from aiogram import types

from app.services.contests.enums import GameType


@dataclass
class GameRenderResult:
    """Result of rendering a game."""

    text: str
    keyboard: types.InlineKeyboardMarkup
    requires_text_input: bool = False


@dataclass
class AnswerCheckResult:
    """Result of checking user's answer."""

    is_correct: bool
    response_text: str


class BaseGameStrategy(ABC):
    """Base class for game strategies."""

    game_type: GameType

    @abstractmethod
    def build_payload(self, template_payload: dict[str, Any]) -> dict[str, Any]:
        """Build round-specific payload from template config."""

    @abstractmethod
    def render(
        self,
        round_id: int,
        payload: dict[str, Any],
        language: str,
        back_callback: str = 'contests_menu',
    ) -> GameRenderResult:
        """Render game UI for user."""

    @abstractmethod
    def check_answer(
        self,
        user_answer: str,
        payload: dict[str, Any],
        language: str,
    ) -> AnswerCheckResult:
        """Check if user's answer is correct."""

    def _get_back_button(self, language: str, callback: str) -> types.InlineKeyboardButton:
        from app.localization.texts import get_texts

        texts = get_texts(language)
        return types.InlineKeyboardButton(text=texts.BACK, callback_data=callback)

    def _get_texts(self, language: str):
        from app.localization.texts import get_texts

        return get_texts(language)


class QuestButtonsStrategy(BaseGameStrategy):
    """3x3 grid game - find the secret button."""

    game_type = GameType.QUEST_BUTTONS

    def build_payload(self, template_payload: dict[str, Any]) -> dict[str, Any]:
        rows = template_payload.get('rows', 3)
        cols = template_payload.get('cols', 3)
        total = rows * cols
        secret_idx = random.randint(0, total - 1)
        return {'rows': rows, 'cols': cols, 'secret_idx': secret_idx}

    def render(
        self,
        round_id: int,
        payload: dict[str, Any],
        language: str,
        back_callback: str = 'contests_menu',
    ) -> GameRenderResult:
        texts = self._get_texts(language)
        rows = payload.get('rows', 3)
        cols = payload.get('cols', 3)

        keyboard_rows = []
        for r in range(rows):
            row_buttons = []
            for c in range(cols):
                idx = r * cols + c
                row_buttons.append(
                    types.InlineKeyboardButton(
                        text='🎛',
                        callback_data=f'nz!_contest_pick_{round_id}_quest_{idx}',
                    )
                )
            keyboard_rows.append(row_buttons)
        keyboard_rows.append([self._get_back_button(language, back_callback)])

        return GameRenderResult(
            text=texts.t('CONTEST_QUEST_PROMPT', 'Выбери один из узлов 3×3:'),
            keyboard=types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
        )

    def check_answer(
        self,
        user_answer: str,
        payload: dict[str, Any],
        language: str,
    ) -> AnswerCheckResult:
        secret_idx = payload.get('secret_idx')
        try:
            if user_answer.startswith('quest_'):
                idx = int(user_answer.split('_')[1])
                is_correct = secret_idx is not None and idx == secret_idx
            else:
                is_correct = False
        except (ValueError, IndexError):
            is_correct = False

        responses = ['Пусто', 'Ложный сервер', 'Найди другой узел']
        return AnswerCheckResult(
            is_correct=is_correct,
            response_text='' if is_correct else random.choice(responses),
        )


class LockHackStrategy(BaseGameStrategy):
    """20 locks game - find the hacked one."""

    game_type = GameType.LOCK_HACK

    def build_payload(self, template_payload: dict[str, Any]) -> dict[str, Any]:
        total = template_payload.get('buttons', 20)
        secret_idx = random.randint(0, max(0, total - 1))
        return {'total': total, 'secret_idx': secret_idx}

    def render(
        self,
        round_id: int,
        payload: dict[str, Any],
        language: str,
        back_callback: str = 'contests_menu',
    ) -> GameRenderResult:
        texts = self._get_texts(language)
        total = payload.get('total', 20)

        keyboard_rows = []
        row = []
        for i in range(total):
            row.append(
                types.InlineKeyboardButton(
                    text='🔒',
                    callback_data=f'nz!_contest_pick_{round_id}_locks_{i}',
                )
            )
            if len(row) == 5:
                keyboard_rows.append(row)
                row = []
        if row:
            keyboard_rows.append(row)
        keyboard_rows.append([self._get_back_button(language, back_callback)])

        return GameRenderResult(
            text=texts.t('CONTEST_LOCKS_PROMPT', 'Найди взломанную кнопку среди замков:'),
            keyboard=types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
        )

    def check_answer(
        self,
        user_answer: str,
        payload: dict[str, Any],
        language: str,
    ) -> AnswerCheckResult:
        secret_idx = payload.get('secret_idx')
        try:
            if user_answer.startswith('locks_'):
                idx = int(user_answer.split('_')[1])
                is_correct = secret_idx is not None and idx == secret_idx
            else:
                is_correct = False
        except (ValueError, IndexError):
            is_correct = False

        responses = ['Заблокировано', 'Попробуй ещё', 'Нет доступа']
        return AnswerCheckResult(
            is_correct=is_correct,
            response_text='' if is_correct else random.choice(responses),
        )


class ServerLotteryStrategy(BaseGameStrategy):
    """Flag lottery game - pick the correct server flag."""

    game_type = GameType.SERVER_LOTTERY

    DEFAULT_FLAGS = ['🇸🇪', '🇸🇬', '🇺🇸', '🇷🇺', '🇩🇪', '🇯🇵', '🇧🇷', '🇦🇺', '🇨🇦', '🇫🇷']

    def build_payload(self, template_payload: dict[str, Any]) -> dict[str, Any]:
        flags = template_payload.get('flags') or self.DEFAULT_FLAGS
        secret_idx = random.randint(0, len(flags) - 1)
        return {'flags': flags, 'secret_idx': secret_idx}

    def render(
        self,
        round_id: int,
        payload: dict[str, Any],
        language: str,
        back_callback: str = 'contests_menu',
    ) -> GameRenderResult:
        texts = self._get_texts(language)
        flags = payload.get('flags') or []
        shuffled_flags = flags.copy()
        random.shuffle(shuffled_flags)

        keyboard_rows = []
        row = []
        for flag in shuffled_flags:
            row.append(
                types.InlineKeyboardButton(
                    text=flag,
                    callback_data=f'nz!_contest_pick_{round_id}_{flag}',
                )
            )
            if len(row) == 5:
                keyboard_rows.append(row)
                row = []
        if row:
            keyboard_rows.append(row)
        keyboard_rows.append([self._get_back_button(language, back_callback)])

        return GameRenderResult(
            text=texts.t('CONTEST_SERVER_PROMPT', 'Выбери сервер:'),
            keyboard=types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
        )

    def check_answer(
        self,
        user_answer: str,
        payload: dict[str, Any],
        language: str,
    ) -> AnswerCheckResult:
        secret_idx = payload.get('secret_idx')
        flags = payload.get('flags') or []
        correct_flag = flags[secret_idx] if secret_idx is not None and secret_idx < len(flags) else ''
        is_correct = user_answer == correct_flag

        responses = ['Сервер перегружен', 'Нет ответа', 'Попробуй завтра']
        return AnswerCheckResult(
            is_correct=is_correct,
            response_text='' if is_correct else random.choice(responses),
        )


class BlitzReactionStrategy(BaseGameStrategy):
    """Blitz reaction game - press button quickly."""

    game_type = GameType.BLITZ_REACTION

    def build_payload(self, template_payload: dict[str, Any]) -> dict[str, Any]:
        return {'timeout_seconds': template_payload.get('timeout_seconds', 10)}

    def render(
        self,
        round_id: int,
        payload: dict[str, Any],
        language: str,
        back_callback: str = 'contests_menu',
    ) -> GameRenderResult:
        texts = self._get_texts(language)

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('CONTEST_BLITZ_BUTTON', 'Я здесь!'),
                        callback_data=f'nz!_contest_pick_{round_id}_blitz',
                    )
                ],
                [self._get_back_button(language, back_callback)],
            ]
        )

        return GameRenderResult(
            text=texts.t('CONTEST_BLITZ_PROMPT', '⚡️ Блиц! Нажми «Я здесь!»'),
            keyboard=keyboard,
        )

    def check_answer(
        self,
        user_answer: str,
        payload: dict[str, Any],
        language: str,
    ) -> AnswerCheckResult:
        is_correct = user_answer == 'blitz'
        return AnswerCheckResult(
            is_correct=is_correct,
            response_text='' if is_correct else 'Время вышло',
        )


class LetterCipherStrategy(BaseGameStrategy):
    """Letter cipher game - decode word from letter codes."""

    game_type = GameType.LETTER_CIPHER

    DEFAULT_WORDS = ['VPN', 'SERVER', 'PROXY', 'XRAY']

    def build_payload(self, template_payload: dict[str, Any]) -> dict[str, Any]:
        words = template_payload.get('words') or self.DEFAULT_WORDS
        word = random.choice(words)
        codes = [str(ord(ch.upper()) - 64) for ch in word if ch.isalpha()]
        return {'question': '-'.join(codes), 'answer': word.upper()}

    def render(
        self,
        round_id: int,
        payload: dict[str, Any],
        language: str,
        back_callback: str = 'contests_menu',
    ) -> GameRenderResult:
        texts = self._get_texts(language)
        question = payload.get('question', '')
        from app.keyboards.inline import get_back_keyboard

        return GameRenderResult(
            text=texts.t('CONTEST_CIPHER_PROMPT', 'Расшифруй: {q}').format(q=question),
            keyboard=get_back_keyboard(language),
            requires_text_input=True,
        )

    def check_answer(
        self,
        user_answer: str,
        payload: dict[str, Any],
        language: str,
    ) -> AnswerCheckResult:
        correct = (payload.get('answer') or '').upper()
        is_correct = correct and user_answer.strip().upper() == correct

        return AnswerCheckResult(
            is_correct=is_correct,
            response_text='' if is_correct else 'Неверно, попробуй в следующем раунде',
        )


class EmojiGuessStrategy(BaseGameStrategy):
    """Emoji guess game - guess service by emoji."""

    game_type = GameType.EMOJI_GUESS

    def build_payload(self, template_payload: dict[str, Any]) -> dict[str, Any]:
        pairs = template_payload.get('pairs') or [{'question': '🔐📡🌐', 'answer': 'VPN'}]
        pair = random.choice(pairs)
        return pair

    def render(
        self,
        round_id: int,
        payload: dict[str, Any],
        language: str,
        back_callback: str = 'contests_menu',
    ) -> GameRenderResult:
        texts = self._get_texts(language)
        question = payload.get('question', '🤔')
        emoji_list = question.split()
        random.shuffle(emoji_list)
        shuffled_question = ' '.join(emoji_list)
        from app.keyboards.inline import get_back_keyboard

        return GameRenderResult(
            text=texts.t('CONTEST_EMOJI_PROMPT', 'Угадай сервис по эмодзи: {q}').format(q=shuffled_question),
            keyboard=get_back_keyboard(language),
            requires_text_input=True,
        )

    def check_answer(
        self,
        user_answer: str,
        payload: dict[str, Any],
        language: str,
    ) -> AnswerCheckResult:
        correct = (payload.get('answer') or '').upper()
        is_correct = correct and user_answer.strip().upper() == correct

        return AnswerCheckResult(
            is_correct=is_correct,
            response_text='' if is_correct else 'Неверно, попробуй в следующем раунде',
        )


class AnagramStrategy(BaseGameStrategy):
    """Anagram game - unscramble letters to form a word."""

    game_type = GameType.ANAGRAM

    DEFAULT_WORDS = ['SERVER', 'XRAY', 'VPN']

    def build_payload(self, template_payload: dict[str, Any]) -> dict[str, Any]:
        words = template_payload.get('words') or self.DEFAULT_WORDS
        word = random.choice(words).upper()
        shuffled = ''.join(random.sample(word, len(word)))
        return {'letters': shuffled, 'answer': word}

    def render(
        self,
        round_id: int,
        payload: dict[str, Any],
        language: str,
        back_callback: str = 'contests_menu',
    ) -> GameRenderResult:
        texts = self._get_texts(language)
        letters = payload.get('letters', '')
        from app.keyboards.inline import get_back_keyboard

        return GameRenderResult(
            text=texts.t('CONTEST_ANAGRAM_PROMPT', 'Составь слово: {letters}').format(letters=letters),
            keyboard=get_back_keyboard(language),
            requires_text_input=True,
        )

    def check_answer(
        self,
        user_answer: str,
        payload: dict[str, Any],
        language: str,
    ) -> AnswerCheckResult:
        correct = (payload.get('answer') or '').upper()
        is_correct = correct and user_answer.strip().upper() == correct

        return AnswerCheckResult(
            is_correct=is_correct,
            response_text='' if is_correct else 'Неверно, попробуй в следующем раунде',
        )


# Registry of game strategies
_GAME_STRATEGIES: dict[GameType, BaseGameStrategy] = {
    GameType.QUEST_BUTTONS: QuestButtonsStrategy(),
    GameType.LOCK_HACK: LockHackStrategy(),
    GameType.SERVER_LOTTERY: ServerLotteryStrategy(),
    GameType.BLITZ_REACTION: BlitzReactionStrategy(),
    GameType.LETTER_CIPHER: LetterCipherStrategy(),
    GameType.EMOJI_GUESS: EmojiGuessStrategy(),
    GameType.ANAGRAM: AnagramStrategy(),
}


def get_game_strategy(game_type: GameType | str) -> BaseGameStrategy | None:
    """Get game strategy by type."""
    if isinstance(game_type, str):
        try:
            game_type = GameType(game_type)
        except ValueError:
            return None
    return _GAME_STRATEGIES.get(game_type)


def get_all_game_types() -> list[GameType]:
    """Get list of all supported game types."""
    return list(_GAME_STRATEGIES.keys())
