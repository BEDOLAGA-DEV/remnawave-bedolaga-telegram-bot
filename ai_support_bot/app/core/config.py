from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='AISUP_', env_file='.env', extra='ignore')

    BOT_TOKEN: str
    ADMIN_IDS: str = ''

    OPENAI_API_KEY: str
    OPENAI_BASE_URL: str = 'https://api.openai.com/v1'
    MODEL: str = 'gpt-4o-mini'
    EMBEDDING_MODEL: str = 'text-embedding-3-small'
    VISION_ENABLED: bool = True
    MAX_TOKENS: int = 700
    TEMPERATURE: float = 0.3
    TOP_K: int = 5
    MIN_SCORE: float = 0.25
    CHUNK_MAX_CHARS: int = 1200
    CONTEXT_MESSAGES: int = 8
    HISTORY_LIMIT: int = 100
    RESPONSE_CACHE_TTL: int = 900

    DATABASE_URL: str = 'sqlite+aiosqlite:///./data/ai_support.db'
    MAIN_DATABASE_URL: str = ''

    INCLUDE_REMNAWAVE_DATA: bool = True
    REMNAWAVE_API_URL: str = ''
    REMNAWAVE_API_TOKEN: str = ''

    ADMIN_HOST: str = '0.0.0.0'
    ADMIN_PORT: int = 8090
    ADMIN_USERNAME: str = 'admin'
    ADMIN_PASSWORD: str = 'changeme'
    ADMIN_SECRET_KEY: str = 'change-this-secret-key'

    SYSTEM_PROMPT: str = (
        'Ты — вежливый и профессиональный оператор поддержки VPN-сервиса. '
        'Отвечай кратко, по делу и на языке пользователя. Используй приведённые '
        'примеры прошлых ответов поддержки как образец стиля и решений, а данные '
        'пользователя (подписка, баланс, оплаты) — чтобы дать точный ответ. '
        'Если данных недостаточно или вопрос требует ручного вмешательства — '
        'предложи обратиться к живому оператору. Не выдумывай факты.'
    )

    @property
    def admin_ids(self) -> set[int]:
        result: set[int] = set()
        for part in (self.ADMIN_IDS or '').replace(';', ',').split(','):
            part = part.strip()
            if part.isdigit():
                result.add(int(part))
        return result

    @property
    def main_db_enabled(self) -> bool:
        return bool(self.MAIN_DATABASE_URL.strip())

    @property
    def remnawave_enabled(self) -> bool:
        return bool(self.INCLUDE_REMNAWAVE_DATA and self.REMNAWAVE_API_URL and self.REMNAWAVE_API_TOKEN)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
