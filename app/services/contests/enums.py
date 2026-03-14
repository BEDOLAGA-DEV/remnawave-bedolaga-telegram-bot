"""Enum classes for contest system."""

from enum import StrEnum


class GameType(StrEnum):
    """Types of daily contest games."""

    QUEST_BUTTONS = 'quest_buttons'
    LOCK_HACK = 'lock_hack'
    LETTER_CIPHER = 'letter_cipher'
    SERVER_LOTTERY = 'server_lottery'
    BLITZ_REACTION = 'blitz_reaction'
    EMOJI_GUESS = 'emoji_guess'
    ANAGRAM = 'anagram'

    @classmethod
    def is_text_input(cls, game_type: 'GameType') -> bool:
        """Check if game requires text input from user."""
        return game_type in {cls.LETTER_CIPHER, cls.EMOJI_GUESS, cls.ANAGRAM}

    @classmethod
    def is_button_pick(cls, game_type: 'GameType') -> bool:
        """Check if game uses button selection."""
        return game_type in {
            cls.QUEST_BUTTONS,
            cls.LOCK_HACK,
            cls.SERVER_LOTTERY,
            cls.BLITZ_REACTION,
        }


class RoundStatus(StrEnum):
    """Contest round status."""

    ACTIVE = 'active'
    FINISHED = 'finished'


class PrizeType(StrEnum):
    """Types of prizes for contests."""

    DAYS = 'days'
    BALANCE = 'balance'
    CUSTOM = 'custom'
