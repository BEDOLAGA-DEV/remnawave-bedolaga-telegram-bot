from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix='AISUP_',
        env_file=('.env', 'ai_support_bot/.env'),
        extra='ignore',
    )

    BOT_TOKEN: str = ''
    ADMIN_IDS: str = ''

    OPENAI_API_KEY: str = ''
    OPENAI_BASE_URL: str = 'https://api.openai.com/v1'
    MODEL: str = 'gpt-4o-mini'
    EMBEDDING_MODEL: str = 'text-embedding-3-small'
    VISION_ENABLED: bool = True
    MAX_TOKENS: int = 700
    TEMPERATURE: float = 0.3
    TOP_K: int = 5
    MIN_SCORE: float = 0.25
    CHUNK_MAX_CHARS: int = 1200
    CONTEXT_MESSAGES: int = 6
    HISTORY_LIMIT: int = 100
    DAILY_MESSAGE_LIMIT: int = 0  # 0 = без лимита; лимит сообщений пользователя в сутки
    RESPONSE_CACHE_TTL: int = 900

    SUMMARY_ENABLED: bool = True
    SUMMARY_EVERY_N_TURNS: int = 3  # каждые N сообщений пользователя — пересобрать сводку диалога
    SUMMARY_MAX_TOKENS: int = 220
    SUMMARY_MODEL: str = ''  # пусто = использовать тот же MODEL
    MAX_QUESTION_CHARS: int = 1500  # обрезка слишком длинных сообщений пользователя (экономия токенов)
    KB_MIN_QUESTION_CHARS: int = 6  # не искать в базе знаний по коротким репликам («ок», «спасибо»)
    KB_DROP_LOW_VALUE: bool = True  # отсеивать при загрузке базы знаний мусорные пары (болталка, реклама, партнёрка)

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
        'Ты — живой оператор поддержки VPN-сервиса. Твоя цель — самостоятельно решить проблему '
        'пользователя, чтобы ему не пришлось ждать живого оператора.\n'
        'Стиль: дружелюбный, уверенный, человеческий, на языке пользователя. Отвечай коротко и по делу — '
        'без воды и канцелярита. Обычный ответ — 1–4 предложения; пошаговые инструкции давай списком.\n'
        'Прежде чем отвечать, пойми НАМЕРЕНИЕ: техническая проблема (не подключается / не работает / медленно), '
        'вопрос по оплате и подписке, инструкция (как установить / добавить устройство / продлить), '
        'или просто общение («привет», «как дела», «спасибо»). На короткие реплики и болталку отвечай тепло, '
        'кратко и по-человечески, без инструкций и без базы знаний.\n'
        'Всегда опирайся на «Данные текущего пользователя» (подписка, баланс, оплаты) — это твой главный источник '
        'фактов о клиенте. Примеры прошлых обращений используй как образец тона и типовых решений, а не как факты о '
        'конкретном пользователе.\n'
        'Форматирование: ТОЛЬКО HTML-теги Telegram (<b>жирный</b>, <i>курсив</i>, <code>код</code>). '
        'Строго запрещён Markdown (никаких **, __, ```, #, спискок через *).\n'
        'Не выдумывай факты, цены, сроки и ссылки. Если данных не хватает — задай ОДИН уточняющий вопрос '
        'или предложи позвать оператора. Не обещай возвраты и не меняй подписку сам — это делает оператор.'
    )

    @property
    def effective_database_url(self) -> str:
        if self.DATABASE_URL and 'sqlite' in self.DATABASE_URL:
            import os
            if os.path.exists('/app/data'):
                return 'sqlite+aiosqlite:////app/data/ai_support.db'
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
            data_dir = os.path.join(project_root, 'data')
            os.makedirs(data_dir, exist_ok=True)
            db_file = os.path.join(data_dir, 'ai_support.db')
            return f'sqlite+aiosqlite:///{db_file}'
        return self.DATABASE_URL

    @property
    def effective_main_database_url(self) -> str:
        if self.MAIN_DATABASE_URL and self.MAIN_DATABASE_URL.strip():
            return self.MAIN_DATABASE_URL.strip()

        import os
        user = os.getenv('POSTGRES_USER')
        password = os.getenv('POSTGRES_PASSWORD')
        host = os.getenv('POSTGRES_HOST', 'postgres')
        port = os.getenv('POSTGRES_PORT', '5432')
        db = os.getenv('POSTGRES_DB', 'remnawave_bot')

        if user and password:
            return f'postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}'

        main_db_url = os.getenv('DATABASE_URL', '')
        if main_db_url and 'postgresql' in main_db_url:
            return main_db_url

        return ''

    @property
    def admin_ids(self) -> set[int]:
        import os
        raw_ids = self.ADMIN_IDS or os.getenv('ADMIN_IDS', '')
        result: set[int] = set()
        for part in raw_ids.replace(';', ',').split(','):
            part = part.strip()
            if part.isdigit():
                result.add(int(part))
        return result

    @property
    def effective_openai_api_key(self) -> str:
        if self.OPENAI_API_KEY and self.OPENAI_API_KEY.strip():
            return self.OPENAI_API_KEY.strip()

        import os
        key = os.getenv('AISUP_OPENAI_API_KEY') or os.getenv('OPENAI_API_KEY')
        if key and key.strip():
            return key.strip()

        for env_path in ('ai_support_bot/.env', '.env'):
            if os.path.exists(env_path):
                try:
                    with open(env_path, 'r', encoding='utf-8') as f:
                        for line in f:
                            line = line.strip()
                            if line.startswith(('AISUP_OPENAI_API_KEY=', 'OPENAI_API_KEY=')):
                                val = line.split('=', 1)[1].strip().strip('"\'')
                                if val:
                                    return val
                except Exception:
                    pass
        return ''

    @property
    def effective_bot_token(self) -> str:
        if self.BOT_TOKEN and self.BOT_TOKEN.strip():
            return self.BOT_TOKEN.strip()

        import os
        tok = os.getenv('AISUP_BOT_TOKEN') or os.getenv('BOT_TOKEN')
        if tok and tok.strip():
            return tok.strip()

        for env_path in ('ai_support_bot/.env', '.env'):
            if os.path.exists(env_path):
                try:
                    with open(env_path, 'r', encoding='utf-8') as f:
                        for line in f:
                            line = line.strip()
                            if line.startswith(('AISUP_BOT_TOKEN=', 'BOT_TOKEN=')):
                                val = line.split('=', 1)[1].strip().strip('"\'')
                                if val:
                                    return val
                except Exception:
                    pass
        return ''

    @property
    def main_db_enabled(self) -> bool:
        return bool(self.effective_main_database_url)

    @property
    def remnawave_enabled(self) -> bool:
        return bool(self.INCLUDE_REMNAWAVE_DATA and self.REMNAWAVE_API_URL and self.REMNAWAVE_API_TOKEN)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
