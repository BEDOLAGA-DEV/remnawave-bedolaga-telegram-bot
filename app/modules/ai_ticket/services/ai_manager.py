"""
AI Provider Manager — Multi-provider with key failover.

Ported from Reshala-AI-ticket-bot and adapted for SQLAlchemy (async).
Supports: Groq, OpenAI, Anthropic, Google (Gemini), OpenRouter.
Features:
  - Multiple API keys per provider with automatic rotation on 429/402/403
  - Provider priority levels — failover from primary to secondary etc.
  - Circular failover across all enabled providers
  - Dynamic model list fetching from provider APIs
"""

import structlog
import httpx
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models_ai_ticket import AIProviderConfig

logger = structlog.get_logger(__name__)

# Default provider definitions for first-run seeding
DEFAULT_PROVIDERS = [
    {'name': 'groq', 'priority': 0, 'base_url': 'https://api.groq.com/openai/v1'},
    {'name': 'openai', 'priority': 1, 'base_url': 'https://api.openai.com/v1'},
    {'name': 'anthropic', 'priority': 2, 'base_url': 'https://api.anthropic.com/v1'},
    {'name': 'google', 'priority': 3, 'base_url': 'https://generativelanguage.googleapis.com/v1beta'},
    {'name': 'openrouter', 'priority': 4, 'base_url': 'https://openrouter.ai/api/v1'},
]


async def ensure_providers_exist(db: AsyncSession) -> None:
    """Seed default provider rows if they don't exist yet."""
    result = await db.execute(select(AIProviderConfig.name))
    existing = {row[0] for row in result.all()}
    for p in DEFAULT_PROVIDERS:
        if p['name'] not in existing:
            db.add(AIProviderConfig(
                name=p['name'],
                priority=p['priority'],
                base_url=p['base_url'],
                api_keys=[],
                available_models=[],
            ))
    await db.commit()


async def get_providers(db: AsyncSession) -> list[AIProviderConfig]:
    """Get all providers sorted by priority."""
    result = await db.execute(
        select(AIProviderConfig).order_by(AIProviderConfig.priority)
    )
    return list(result.scalars().all())


async def get_provider(db: AsyncSession, name: str) -> Optional[AIProviderConfig]:
    result = await db.execute(
        select(AIProviderConfig).where(AIProviderConfig.name == name)
    )
    return result.scalars().first()


async def add_key(db: AsyncSession, provider_name: str, key: str) -> bool:
    """Add an API key to a provider (no duplicates)."""
    provider = await get_provider(db, provider_name)
    if not provider:
        return False
    keys = list(provider.api_keys or [])
    if key not in keys:
        keys.append(key)
        provider.api_keys = keys
        await db.commit()
    return True


async def remove_key(db: AsyncSession, provider_name: str, key_index: int) -> bool:
    """Remove an API key by index."""
    provider = await get_provider(db, provider_name)
    if not provider:
        return False
    keys = list(provider.api_keys or [])
    if 0 <= key_index < len(keys):
        keys.pop(key_index)
        active_idx = provider.active_key_index or 0
        if active_idx >= len(keys):
            active_idx = max(0, len(keys) - 1)
        provider.api_keys = keys
        provider.active_key_index = active_idx
        await db.commit()
        return True
    return False


async def set_model(db: AsyncSession, provider_name: str, model: str) -> bool:
    provider = await get_provider(db, provider_name)
    if not provider:
        return False
    provider.selected_model = model
    await db.commit()
    return True


async def set_enabled(db: AsyncSession, provider_name: str, enabled: bool) -> bool:
    provider = await get_provider(db, provider_name)
    if not provider:
        return False
    provider.enabled = enabled
    await db.commit()
    return True


async def set_priority(db: AsyncSession, provider_name: str, priority: int) -> bool:
    provider = await get_provider(db, provider_name)
    if not provider:
        return False
    provider.priority = priority
    await db.commit()
    return True


async def _rotate_key(db: AsyncSession, provider: AIProviderConfig) -> None:
    """Rotate to the next API key in the list."""
    keys = provider.api_keys or []
    if len(keys) <= 1:
        return
    current_idx = provider.active_key_index or 0
    next_idx = (current_idx + 1) % len(keys)
    provider.active_key_index = next_idx
    await db.commit()
    logger.info('ai_manager.key_rotated', provider=provider.name, from_idx=current_idx, to_idx=next_idx)


def _get_working_key(provider: AIProviderConfig) -> Optional[str]:
    """Get the currently active API key."""
    keys = provider.api_keys or []
    if not keys:
        return None
    idx = provider.active_key_index or 0
    if idx < len(keys):
        return keys[idx]
    return keys[0]


# ───────────────── Test Connection / Model Fetching ─────────────────

async def test_connection(db: AsyncSession, provider_name: str, key: Optional[str] = None) -> dict:
    """
    Test a provider's API key and fetch available models.
    Returns {'ok': bool, 'models': list, 'error': str | None}
    """
    provider = await get_provider(db, provider_name)
    if not provider:
        return {'ok': False, 'error': 'Провайдер не найден', 'models': []}

    keys_to_test = [key] if key else (provider.api_keys or [])
    if not keys_to_test:
        return {'ok': False, 'error': 'Нет API ключей', 'models': []}

    fetchers = {
        'groq': _fetch_models_groq,
        'openai': _fetch_models_openai,
        'anthropic': _fetch_models_anthropic,
        'google': _fetch_models_google,
        'openrouter': _fetch_models_openrouter,
    }
    fetch_fn = fetchers.get(provider_name)
    if not fetch_fn:
        return {'ok': False, 'error': 'Неизвестный провайдер', 'models': []}

    active_idx = provider.active_key_index or 0
    last_result = {'ok': False, 'error': 'Неизвестная ошибка', 'models': []}

    # Test keys starting from the active index
    for attempt in range(len(keys_to_test)):
        # If manually testing a specific key, just use idx 0
        idx = 0 if key else (active_idx + attempt) % len(keys_to_test)
        test_key = keys_to_test[idx]

        try:
            result = await fetch_fn(provider, test_key)
            if result['ok'] and result['models']:
                # Working key found! Cache models and update active index
                provider.available_models = result['models']
                if not provider.selected_model:
                    provider.selected_model = result['models'][0]
                if not key and idx != active_idx:
                    provider.active_key_index = idx
                await db.commit()
                return result
            
            last_result = result
        except Exception as e:
            logger.warning('ai_manager.test_connection_failed', provider=provider_name, error=str(e))
            last_result = {'ok': False, 'error': str(e), 'models': []}

    # If we get here, all keys failed
    return last_result


async def _fetch_models_groq(provider: AIProviderConfig, key: str) -> dict:
    base = provider.base_url or 'https://api.groq.com/openai/v1'
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f'{base}/models', headers={'Authorization': f'Bearer {key}'})
    if r.status_code == 200:
        models = [m['id'] for m in r.json().get('data', [])]
        return {'ok': True, 'models': sorted(models), 'count': len(models)}
    return {'ok': False, 'error': f'HTTP {r.status_code}: {r.text[:200]}', 'models': []}


async def _fetch_models_openai(provider: AIProviderConfig, key: str) -> dict:
    base = provider.base_url or 'https://api.openai.com/v1'
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f'{base}/models', headers={'Authorization': f'Bearer {key}'})
    if r.status_code == 200:
        models = [
            m['id'] for m in r.json().get('data', [])
            if any(prefix in m['id'].lower() for prefix in ('gpt', 'o1', 'o3', 'o4'))
        ]
        return {'ok': True, 'models': sorted(models), 'count': len(models)}
    return {'ok': False, 'error': f'HTTP {r.status_code}: {r.text[:200]}', 'models': []}


async def _fetch_models_anthropic(provider: AIProviderConfig, key: str) -> dict:
    headers = {'x-api-key': key, 'anthropic-version': '2023-06-01', 'Content-Type': 'application/json'}
    payload = {'model': 'claude-3-5-haiku-20241022', 'max_tokens': 10, 'messages': [{'role': 'user', 'content': 'hi'}]}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post('https://api.anthropic.com/v1/messages', json=payload, headers=headers)
    if r.status_code == 200:
        models = ['claude-sonnet-4-20250514', 'claude-3-5-haiku-20241022', 'claude-3-opus-20240229']
        return {'ok': True, 'models': models, 'count': len(models)}
    return {'ok': False, 'error': f'HTTP {r.status_code}: {r.text[:200]}', 'models': []}


async def _fetch_models_google(provider: AIProviderConfig, key: str) -> dict:
    base = provider.base_url or 'https://generativelanguage.googleapis.com/v1beta'
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f'{base}/models', params={'key': key})
    if r.status_code == 200:
        data = r.json()
        models = [
            m['name'].replace('models/', '')
            for m in data.get('models', [])
            if 'generateContent' in str(m.get('supportedGenerationMethods', []))
        ]
        return {'ok': True, 'models': models, 'count': len(models)}
    return {'ok': False, 'error': f'HTTP {r.status_code}: {r.text[:200]}', 'models': []}


async def _fetch_models_openrouter(provider: AIProviderConfig, key: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get('https://openrouter.ai/api/v1/models', headers={'Authorization': f'Bearer {key}'})
    if r.status_code == 200:
        models = [m['id'] for m in r.json().get('data', [])[:100]]
        return {'ok': True, 'models': models, 'count': len(models)}
    return {'ok': False, 'error': f'HTTP {r.status_code}: {r.text[:200]}', 'models': []}


# ───────────────── Chat — Main Entry Point ─────────────────

async def generate_ai_response(
    db: AsyncSession,
    messages: list[dict],
) -> Optional[str]:
    """
    Try to get an AI response using the priority-based provider chain.
    1. Try active provider → rotate keys on failure
    2. If all keys exhausted → try next provider by priority
    3. Circular through all enabled providers
    """
    providers = await get_providers(db)
    enabled = [p for p in providers if p.enabled and (p.api_keys or [])]

    if not enabled:
        logger.warning('ai_manager.no_enabled_providers')
        return None

    for provider in enabled:
        result = await _try_provider(db, provider, messages)
        if result:
            return result

    logger.warning('ai_manager.all_providers_exhausted')
    return None


async def _try_provider(
    db: AsyncSession,
    provider: AIProviderConfig,
    messages: list[dict],
) -> Optional[str]:
    """Try all keys of a single provider."""
    keys = provider.api_keys or []
    if not keys:
        return None

    active_idx = provider.active_key_index or 0
    tried: set[int] = set()

    for attempt in range(len(keys)):
        idx = (active_idx + attempt) % len(keys)
        if idx in tried:
            continue
        tried.add(idx)
        key = keys[idx]

        try:
            result = await _call_provider(provider, key, messages)
            if result:
                # Update active index if we rotated
                if idx != active_idx:
                    provider.active_key_index = idx
                    await db.commit()
                return result
        except _RateLimitError:
            logger.warning('ai_manager.key_rate_limited', provider=provider.name, key_idx=idx)
            continue
        except Exception as e:
            logger.warning('ai_manager.key_failed', provider=provider.name, key_idx=idx, error=str(e))
            continue

    logger.warning('ai_manager.all_keys_exhausted', provider=provider.name)
    return None


class _RateLimitError(Exception):
    pass


async def _call_provider(
    provider: AIProviderConfig,
    key: str,
    messages: list[dict],
) -> Optional[str]:
    """Call a specific provider with a specific key."""
    model = provider.selected_model or ''
    if not model:
        return None

    name = provider.name
    if name in ('groq', 'openai', 'openrouter'):
        return await _call_openai_compat(provider, key, model, messages)
    elif name == 'anthropic':
        return await _call_anthropic(key, model, messages)
    elif name == 'google':
        return await _call_google(provider, key, model, messages)
    return None


async def _call_openai_compat(
    provider: AIProviderConfig,
    key: str,
    model: str,
    messages: list[dict],
) -> Optional[str]:
    defaults = {
        'groq': 'https://api.groq.com/openai/v1',
        'openai': 'https://api.openai.com/v1',
        'openrouter': 'https://openrouter.ai/api/v1',
    }
    base = provider.base_url or defaults.get(provider.name, '')
    url = f'{base}/chat/completions'
    headers = {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}
    payload = {'model': model, 'messages': messages, 'temperature': 0.7, 'max_tokens': 2048}

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, json=payload, headers=headers)

    if r.status_code == 200:
        choices = r.json().get('choices', [])
        if choices:
            content = choices[0].get('message', {}).get('content', '')
            return content.strip() if content else None

    if r.status_code in (429, 402, 403):
        raise _RateLimitError(f'HTTP {r.status_code}')

    logger.warning('ai_manager.openai_compat_error', model=model, status=r.status_code, response=r.text[:200])
    return None


async def _call_anthropic(key: str, model: str, messages: list[dict]) -> Optional[str]:
    system_parts: list[str] = []
    chat_messages: list[dict] = []
    for m in messages:
        if m.get('role') == 'system':
            system_parts.append(m.get('content', ''))
        else:
            chat_messages.append({'role': m['role'], 'content': m.get('content', '')})

    if not chat_messages:
        return None

    headers = {'x-api-key': key, 'anthropic-version': '2023-06-01', 'Content-Type': 'application/json'}
    payload: dict = {'model': model, 'max_tokens': 2048, 'messages': chat_messages}
    if system_parts:
        payload['system'] = '\n\n'.join(system_parts)

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post('https://api.anthropic.com/v1/messages', json=payload, headers=headers)

    if r.status_code == 200:
        content = r.json().get('content', [])
        if content and content[0].get('type') == 'text':
            return content[0].get('text', '').strip()

    if r.status_code in (429, 402, 403):
        raise _RateLimitError(f'HTTP {r.status_code}')

    logger.warning('ai_manager.anthropic_error', model=model, status=r.status_code, response=r.text[:200])
    return None


async def _call_google(
    provider: AIProviderConfig,
    key: str,
    model: str,
    messages: list[dict],
) -> Optional[str]:
    base = provider.base_url or 'https://generativelanguage.googleapis.com/v1beta'
    system_parts: list[str] = []
    contents: list[dict] = []

    for m in messages:
        role = m.get('role', 'user')
        content = m.get('content', '')
        if role == 'system':
            system_parts.append(content)
            continue
        gemini_role = 'model' if role == 'assistant' else 'user'
        contents.append({'role': gemini_role, 'parts': [{'text': content}]})

    if not contents:
        return None

    payload: dict = {
        'contents': contents,
        'generationConfig': {'temperature': 0.7, 'maxOutputTokens': 2048},
    }
    if system_parts:
        payload['systemInstruction'] = {'parts': [{'text': '\n\n'.join(system_parts)}]}

    url = f'{base}/models/{model}:generateContent'
    headers = {'Content-Type': 'application/json'}

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, params={'key': key}, headers=headers, json=payload)

    if r.status_code == 200:
        candidates = r.json().get('candidates', [])
        if candidates:
            parts = candidates[0].get('content', {}).get('parts', [])
            if parts:
                return parts[0].get('text', '').strip()

    if r.status_code in (429, 403):
        raise _RateLimitError(f'HTTP {r.status_code}')

    logger.warning('ai_manager.google_error', model=model, status=r.status_code, response=r.text[:200])
    return None
