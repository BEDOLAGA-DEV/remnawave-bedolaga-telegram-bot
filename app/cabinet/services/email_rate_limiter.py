import json
import logging
import time
from typing import Optional, Tuple

import redis.asyncio as redis

from app.config import settings

logger = logging.getLogger(__name__)


class EmailRateLimitService:
    """
    Service to limit email sending rate per user/email.
    
    Cooldown strategy:
    - After 1st message: 30 seconds
    - After 2nd message: 3 minutes
    - After 3rd message: 5 minutes
    - After 4th+ message: 15 minutes
    
    State resets after 1.5 hours (90 minutes) of inactivity.
    """

    def __init__(self):
        self._redis: Optional[redis.Redis] = None

    @property
    def redis(self) -> redis.Redis:
        if self._redis is None:
            self._redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
        return self._redis

    async def check_rate_limit(self, identifier: str) -> Tuple[bool, float]:
        """
        Check if the user allowed to send an email.
        
        Args:
            identifier: Unique identifier (email or IP)
            
        Returns:
            Tuple[bool, float]: (is_allowed, wait_seconds_required)
        """
        key = f"email_limiter:{identifier}"
        
        try:
            data = await self.redis.get(key)
            if not data:
                return True, 0.0

            state = json.loads(data)
            count = state.get("count", 0)
            last_attempt = state.get("last_attempt", 0.0)
            now = time.time()

            # Determine cooldown based on how many messages ALREADY sent
            cooldown = self._get_cooldown(count)
            
            elapsed = now - last_attempt
            if elapsed < cooldown:
                return False, cooldown - elapsed

            return True, 0.0

        except Exception as e:
            logger.error(f"Rate limit check failed for {identifier}: {e}")
            # Fail open (allow) if redis down, or fail closed? 
            # Usually fail open for user experience, but fail closed for spam prev.
            # Allowing for now to avoid locking users out on redis flux
            return True, 0.0

    async def register_attempt(self, identifier: str) -> None:
        """
        Register a successful email sending attempt.
        Increment counter and update timestamp.
        """
        key = f"email_limiter:{identifier}"
        try:
            now = time.time()
            data = await self.redis.get(key)
            
            if data:
                state = json.loads(data)
                count = state.get("count", 0) + 1
            else:
                count = 1

            new_state = {
                "count": count,
                "last_attempt": now
            }
            
            # Save with 90 minutes TTL
            await self.redis.set(key, json.dumps(new_state), ex=5400)
            
        except Exception as e:
            logger.error(f"Failed to register attempt for {identifier}: {e}")

    def _get_cooldown(self, count: int) -> int:
        """
        Get required cooldown after 'count' messages have been sent.
        """
        # If count is 0 (haven't sent any yet), cooldown is 0.
        # If count is 1 (sent 1 message), wait 30s.
        # If count is 2 (sent 2 messages), wait 3m.
        # If count is 3 (sent 3 messages), wait 5m.
        # If count >= 4 (sent 4+ messages), wait 15m.
        
        if count == 0:
            return 0
        if count == 1:
            return 30
        if count == 2:
            return 180  # 3 min
        if count == 3:
            return 300  # 5 min
        
        return 900  # 15 min


email_rate_limiter = EmailRateLimitService()
