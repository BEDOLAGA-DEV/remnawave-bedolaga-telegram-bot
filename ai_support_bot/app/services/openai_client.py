import asyncio
import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp
import structlog

from ai_support_bot.app.core.config import settings


logger = structlog.get_logger(__name__)

# One chat_completion call (all attempts, back-off and the max_tokens fallback) must finish within this budget,
# otherwise the customer waits for minutes while the bot keeps "typing".
CHAT_DEADLINE_SECONDS = 45
# Errors that never succeed on retry: bad request, wrong key, no access, unknown model, exhausted billing quota.
_NO_RETRY_STATUSES = {400, 401, 403, 404}
_NO_RETRY_CODES = {'insufficient_quota', 'invalid_api_key', 'billing_hard_limit_reached', 'account_deactivated'}
_SAFE_CODE = re.compile(r'[\w.\-]{1,64}')


def _monotonic() -> float:
    return time.monotonic()


class OpenAIError(Exception):
    def __init__(self, message: str = '', status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _api_error(status: int, body: Any) -> OpenAIError:
    """Build an error from any body shape: {"error": {...}}, {"error": "text"}, HTML, empty or a list."""
    message, code = None, None
    if isinstance(body, dict):
        error = body.get('error')
        if isinstance(error, dict):
            message = error.get('message')
            code = error.get('code') or error.get('type')
        elif isinstance(error, str):
            message = error
        else:
            message = body.get('message') or body.get('detail')
    if not isinstance(code, str) or not _SAFE_CODE.fullmatch(code):
        code = None
    return OpenAIError(str(message or f'HTTP {status}')[:500], status=status, code=code)


def _retryable(error: OpenAIError) -> bool:
    if error.code in _NO_RETRY_CODES or error.status in _NO_RETRY_STATUSES:
        return False
    return error.status is not None and (error.status == 429 or error.status >= 500)


def _retry_after(headers: Mapping[str, str] | None) -> float | None:
    if not headers:
        return None
    try:
        value = headers.get('retry-after-ms')
        if value is not None:
            return max(0.0, float(value) / 1000)
        value = headers.get('retry-after')
        if value is None:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            moment = parsedate_to_datetime(value)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            return max(0.0, (moment - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


class OpenAIClient:
    def __init__(self, timeout: int = 60) -> None:
        self._base_url = settings.OPENAI_BASE_URL.rstrip('/')
        self._api_key = settings.OPENAI_API_KEY
        self._timeout_seconds = timeout
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    @property
    def _headers(self) -> dict[str, str]:
        api_key = settings.effective_openai_api_key
        return {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}

    async def _send(self, url: str, payload: dict[str, Any], timeout: float) -> tuple[int, Mapping[str, str], Any]:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.post(url, headers=self._headers, json=payload) as response:
                try:
                    body = await response.json(content_type=None)
                except ValueError:
                    body = None
                return response.status, response.headers, body

    async def _post(
        self, path: str, payload: dict[str, Any], retries: int = 2, deadline: float | None = None,
    ) -> dict[str, Any]:
        url = f'{self._base_url}{path}'
        last_error: OpenAIError | None = None
        for attempt in range(retries + 1):
            timeout = float(self._timeout_seconds)
            if deadline is not None:
                timeout = min(timeout, deadline - _monotonic())
                if timeout <= 0:
                    break
            delay = 1.5 * (attempt + 1)
            try:
                status, headers, body = await self._send(url, payload, timeout)
                if status < 400:
                    if not isinstance(body, dict):
                        raise OpenAIError('Unexpected response body from model', status=status)
                    return body
                last_error = _api_error(status, body)
                if not _retryable(last_error):
                    raise last_error
                retry_after = _retry_after(headers)
                if retry_after is not None:
                    delay = retry_after
            except (TimeoutError, aiohttp.ClientError) as error:
                code = 'timeout' if isinstance(error, TimeoutError) else 'network'
                last_error = OpenAIError(f'{type(error).__name__}: {error}'[:300], code=code)
            if attempt == retries:
                break
            if deadline is not None and _monotonic() + delay >= deadline:
                logger.warning('OpenAI retry skipped: no time left before deadline', path=path, delay=delay)
                break
            await asyncio.sleep(delay)
        if last_error is None:
            last_error = OpenAIError('OpenAI request deadline exceeded', code='timeout')
        raise last_error

    async def create_embedding(self, text: str, model: str) -> list[float]:
        result = await self.create_embeddings([text], model)
        return result[0]

    async def create_embeddings(self, texts: list[str], model: str) -> list[list[float]]:
        if not texts:
            return []
        body = await self._post('/embeddings', {'model': model, 'input': texts})
        data = sorted(body.get('data', []), key=lambda item: item.get('index', 0))
        return [item['embedding'] for item in data]

    async def chat_completion(
        self, messages: list[dict[str, Any]], model: str, max_tokens: int, temperature: float,
        tools: list[dict] | None = None, tool_choice: str | dict = 'auto',
    ) -> dict[str, Any]:
        payload = {
            'model': model,
            'messages': messages,
            'max_completion_tokens': max_tokens,
            'temperature': temperature,
        }
        if tools:
            payload['tools'] = tools
            payload['tool_choice'] = tool_choice
            payload['parallel_tool_calls'] = False
        deadline = _monotonic() + CHAT_DEADLINE_SECONDS
        try:
            body = await self._post('/chat/completions', payload, deadline=deadline)
        except OpenAIError as error:
            error_str = str(error)
            if 'max_completion_tokens' in error_str:
                payload.pop('max_completion_tokens', None)
                payload['max_tokens'] = max_tokens
                body = await self._post('/chat/completions', payload, deadline=deadline)
            elif 'max_tokens' in error_str:
                payload.pop('max_tokens', None)
                payload['max_completion_tokens'] = max_tokens
                body = await self._post('/chat/completions', payload, deadline=deadline)
            else:
                raise

        choices = body.get('choices')
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise OpenAIError('Empty response from model')
        usage = body.get('usage') if isinstance(body.get('usage'), dict) else {}
        prompt_details = usage.get('prompt_tokens_details') or {}
        cached_tokens = prompt_details.get('cached_tokens', 0)

        message = choices[0].get('message') or {}
        return {
            'content': (message.get('content') or '').strip(),
            'tool_calls': message.get('tool_calls') or [],
            'model': body.get('model', model),
            'tokens_prompt': usage.get('prompt_tokens'),
            'tokens_completion': usage.get('completion_tokens'),
            'tokens_cached': cached_tokens,
            'finish_reason': choices[0].get('finish_reason'),
            'reasoning_tokens': (usage.get('completion_tokens_details') or {}).get('reasoning_tokens', 0),
        }


openai_client = OpenAIClient()
