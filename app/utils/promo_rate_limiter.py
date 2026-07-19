import re
import time

import structlog


logger = structlog.get_logger(__name__)

# Только буквы, цифры, дефис, подчёркивание
PROMO_CODE_PATTERN = re.compile(r'^[A-Za-z0-9_-]+$')

# Лимиты
MAX_FAILED_ATTEMPTS = 5
FAILED_WINDOW_SECONDS = 300  # 5 минут
MAX_ACTIVATIONS_PER_DAY = 5
ACTIVATION_WINDOW_SECONDS = 86400  # 24 часа


class PromoRateLimiter:
    """
    In-memory rate limiter для промокодов:
    1. Лимит на неудачные попытки (перебор)
    2. Лимит на количество активаций за день (стакинг)
    """

    def __init__(self):
        # user_id → list[timestamp] неудачных попыток
        self._failed_attempts: dict[int, list[float]] = {}
        # user_id → list[timestamp] успешных активаций
        self._activations: dict[int, list[float]] = {}

    def record_failed_attempt(self, user_id: int) -> None:
        now = time.time()
        attempts = self._failed_attempts.get(user_id, [])
        attempts = [ts for ts in attempts if now - ts < FAILED_WINDOW_SECONDS]
        attempts.append(now)
        self._failed_attempts[user_id] = attempts

        if len(attempts) >= MAX_FAILED_ATTEMPTS:
            logger.warning(
                'Promo brute-force: too many failed attempts within window',
                user_id=user_id,
                attempts_count=len(attempts),
                FAILED_WINDOW_SECONDS=FAILED_WINDOW_SECONDS,
            )

    def is_blocked(self, user_id: int) -> bool:
        now = time.time()
        attempts = self._failed_attempts.get(user_id, [])
        attempts = [ts for ts in attempts if now - ts < FAILED_WINDOW_SECONDS]
        self._failed_attempts[user_id] = attempts
        return len(attempts) >= MAX_FAILED_ATTEMPTS

    def get_block_cooldown(self, user_id: int) -> int:
        attempts = self._failed_attempts.get(user_id, [])
        if not attempts:
            return 0
        oldest = attempts[0]
        remaining = int(FAILED_WINDOW_SECONDS - (time.time() - oldest)) + 1
        return max(remaining, 0)

    def record_activation(self, user_id: int) -> None:
        now = time.time()
        activations = self._activations.get(user_id, [])
        activations = [ts for ts in activations if now - ts < ACTIVATION_WINDOW_SECONDS]
        activations.append(now)
        self._activations[user_id] = activations

    def can_activate(self, user_id: int) -> bool:
        now = time.time()
        activations = self._activations.get(user_id, [])
        activations = [ts for ts in activations if now - ts < ACTIVATION_WINDOW_SECONDS]
        self._activations[user_id] = activations
        return len(activations) < MAX_ACTIVATIONS_PER_DAY

    def get_activations_left(self, user_id: int) -> int:
        now = time.time()
        activations = self._activations.get(user_id, [])
        activations = [ts for ts in activations if now - ts < ACTIVATION_WINDOW_SECONDS]
        return max(0, MAX_ACTIVATIONS_PER_DAY - len(activations))

    def cleanup(self) -> None:
        now = time.time()
        if len(self._failed_attempts) > 500:
            self._failed_attempts = {
                uid: [ts for ts in tss if now - ts < FAILED_WINDOW_SECONDS]
                for uid, tss in self._failed_attempts.items()
                if any(now - ts < FAILED_WINDOW_SECONDS for ts in tss)
            }
        if len(self._activations) > 500:
            self._activations = {
                uid: [ts for ts in tss if now - ts < ACTIVATION_WINDOW_SECONDS]
                for uid, tss in self._activations.items()
                if any(now - ts < ACTIVATION_WINDOW_SECONDS for ts in tss)
            }


def validate_promo_format(code: str) -> bool:
    """Проверяет формат промокода: 3-50 символов, только буквы/цифры/дефис/подчёркивание."""
    if not code or len(code) < 3 or len(code) > 50:
        return False
    return bool(PROMO_CODE_PATTERN.match(code))


# Глобальный синглтон
promo_limiter = PromoRateLimiter()
