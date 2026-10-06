import asyncio
from typing import Any

import aiohttp
import structlog

from ai_support_bot.app.core.config import settings


logger = structlog.get_logger(__name__)


class OpenAIError(Exception):
    pass


class OpenAIClient:
    def __init__(self, timeout: int = 60) -> None:
        self._base_url = settings.OPENAI_BASE_URL.rstrip('/')
        self._api_key = settings.OPENAI_API_KEY
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    @property
    def _headers(self) -> dict[str, str]:
        api_key = settings.effective_openai_api_key
        return {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}

    async def _post(self, path: str, payload: dict[str, Any], retries: int = 2) -> dict[str, Any]:
        url = f'{self._base_url}{path}'
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                async with aiohttp.ClientSession(timeout=self._timeout) as session:
                    async with session.post(url, headers=self._headers, json=payload) as response:
                        body = await response.json()
                        if response.status >= 400:
                            message = body.get('error', {}).get('message', str(body))
                            if response.status == 429 or response.status >= 500:
                                last_error = OpenAIError(message)
                                await asyncio.sleep(1.5 * (attempt + 1))
                                continue
                            raise OpenAIError(message)
                        return body
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                last_error = error
                await asyncio.sleep(1.5 * (attempt + 1))
        raise OpenAIError(str(last_error) if last_error else 'Unknown OpenAI error')

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
        self, messages: list[dict[str, Any]], model: str, max_tokens: int, temperature: float
    ) -> dict[str, Any]:
        payload = {
            'model': model,
            'messages': messages,
            'max_completion_tokens': max_tokens,
            'temperature': temperature,
        }
        try:
            body = await self._post('/chat/completions', payload)
        except OpenAIError as error:
            error_str = str(error)
            if 'max_completion_tokens' in error_str:
                payload.pop('max_completion_tokens', None)
                payload['max_tokens'] = max_tokens
                body = await self._post('/chat/completions', payload)
            elif 'max_tokens' in error_str:
                payload.pop('max_tokens', None)
                payload['max_completion_tokens'] = max_tokens
                body = await self._post('/chat/completions', payload)
            else:
                raise

        choices = body.get('choices', [])
        if not choices:
            raise OpenAIError('Empty response from model')
        usage = body.get('usage', {})
        prompt_details = usage.get('prompt_tokens_details') or {}
        cached_tokens = prompt_details.get('cached_tokens', 0)

        return {
            'content': choices[0].get('message', {}).get('content', '').strip(),
            'model': body.get('model', model),
            'tokens_prompt': usage.get('prompt_tokens'),
            'tokens_completion': usage.get('completion_tokens'),
            'tokens_cached': cached_tokens,
        }


openai_client = OpenAIClient()
