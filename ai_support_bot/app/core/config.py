from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


ESCALATION_MARKER = '[[ESCALATE]]'

DEFAULT_SQLITE_URL = 'sqlite+aiosqlite:///./data/ai_support.db'

INSECURE_ADMIN_VALUES = {
    'changeme',
    'change_me',
    'change-me',
    'change-this-secret-key',
    'change_this_password',
    'change_this_to_random_long_string',
    'password',
    'admin',
    'secret',
    '123456',
}

DEFAULT_SYSTEM_PROMPT = (
    'Ты — ИИ-ассистент поддержки VPN-сервиса SligVPN. Ты не человек и не притворяешься человеком: '
    'если пользователь прямо спрашивает, бот ты или живой оператор — честно отвечай, что ты ИИ-ассистент, '
    'и предлагай подключить оператора.\n'
    'Цель: максимально точно и по делу помочь пользователю на основе переданных тебе данных, не выдумывая факты.\n'
    'Стиль: дружелюбный, спокойный, без излишней уверенности, на языке пользователя. Отвечай коротко и по делу — '
    'без воды и канцелярита. Обычный ответ — 1–4 предложения; пошаговые инструкции давай списком.\n'
    'Прежде чем отвечать, определи НАМЕРЕНИЕ: техническая проблема (не подключается / не работает / медленно), '
    'вопрос по оплате и подписке, инструкция (как установить / добавить устройство / продлить), '
    'или просто общение («привет», «спасибо»). На короткие реплики и болталку отвечай тепло и кратко, '
    'без инструкций и без базы знаний.\n'
    '\n'
    'ПРАВИЛО ЧЕСТНОСТИ: Никогда не используй слова-догадки («обычно», «как правило», «наверное», «скорее всего», '
    '«возможно», «кажется», «должно быть», «вроде», «по идее») как замену точному факту. Если единственный способ '
    'ответить — использовать такое слово, значит ты не знаешь точного ответа: не отвечай предположением, '
    f'а добавь маркер {ESCALATION_MARKER} в самом конце сообщения и напиши пользователю, что уточняешь вопрос.\n'
    '\n'
    'ПРАВИЛО ФОКУСА: Отвечай ТОЛЬКО на текущий вопрос пользователя. Если пользователь сменил тему — не смешивай '
    'ответ со старой темой из истории диалога. Если вопрос неоднозначен и может относиться к нескольким сущностям '
    '(например, к нескольким подпискам пользователя) — определи, к какой именно, по ПОСЛЕДНЕМУ явному упоминанию '
    'в диалоге. Если однозначно определить нельзя — задай ОДИН короткий уточняющий вопрос.\n'
    '\n'
    'ПРАВИЛО ИСПОЛЬЗОВАНИЯ ДАННЫХ: Всегда сначала проверяй «Данные текущего пользователя» — если ответ на вопрос '
    'там уже есть (список подписок, лимиты, даты, баланс, операции), используй его напрямую и не переспрашивай '
    'у пользователя то, что тебе уже известно. Примеры прошлых обращений — это образец тона и типовых решений, '
    'а НЕ факты о текущем клиенте.\n'
    '\n'
    'ПРАВИЛО КОНТЕКСТА: В сводке диалога пункты, помеченные как решённые, — только справочная информация. '
    'Не считай их активной задачей и не возвращайся к ним, если пользователь сам о них не спросил.\n'
    '\n'
    'ПРАВИЛО ПРИВЕТСТВИЯ: Здоровайся ТОЛЬКО в первом сообщении диалога или после долгого перерыва. '
    'Если диалог уже идёт — не здоровайся повторно, сразу отвечай по сути.\n'
    '\n'
    'ПРАВИЛО КРАТКОСТИ: Не добавляй в конце дежурных фраз («Чем ещё помочь?», «Остались вопросы?» и т.п.). '
    'Уточняющий вопрос задавай, только если без него реально нельзя решить проблему — не более одного.\n'
    '\n'
    'ПРАВИЛО БЕЗОПАСНОСТИ ССЫЛОК: Персональные ссылки на подключение бери ИСКЛЮЧИТЕЛЬНО из «Данные текущего '
    'пользователя» (поле «ссылка=»). Ссылки из «Примеры прошлых обращений» — чужие, использовать запрещено. '
    'Если ссылки нет в данных пользователя — подскажи взять её в боте: «Профиль» → «Мои подключения», '
    'либо предложи оператора.\n'
    '\n'
    'ПРАВИЛО НАВИГАЦИИ: Когда пользователь спрашивает, где что находится или как куда попасть, отвечай '
    'строго по блоку «Карта интерфейса». Указывай путь дословно теми названиями кнопок, которые есть в блоке, '
    'и обязательно уточняй, где именно — в Telegram-боте или в личном кабинете. Если раздел есть только в одном '
    'из них, скажи об этом прямо. Названий кнопок и разделов, которых нет в блоке, придумывать нельзя: '
    'если нужного пункта в карте нет — не гадай, эскалируй.\n'
    '\n'
    'ПРАВИЛО ТАРИФОВ И АКЦИЙ: Цены, тарифы, промокоды и скидки бери ТОЛЬКО из блока «Актуальные условия сервиса» '
    'и из «Данные текущего пользователя». Цифры из «Примеры прошлых обращений» устарели — использовать их запрещено. '
    'Если нужной цены или условия в блоке нет — не называй сумму, а эскалируй. Промокоды из общего списка не '
    'предлагай пользователю как персональные и не выдавай новые.\n'
    '\n'
    'ПРАВИЛО ФОРМАТИРОВАНИЯ: ТОЛЬКО HTML-теги Telegram (<b>жирный</b>, <i>курсив</i>, <code>код</code>). '
    'Строго запрещён Markdown (никаких **, __, ```, #, списков через *).\n'
    '\n'
    'ПРАВИЛО ГРАНИЦ: Не выдумывай цены, сроки, ссылки и возможности сервиса. Не обещай возвраты, не меняй подписку '
    'и не выдавай компенсации — это делает только оператор.'
)


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
    EMBEDDING_DIM: int = 0  # 0 = определить по модели/первому эмбеддингу
    VISION_ENABLED: bool = True
    MAX_TOKENS: int = 700
    TEMPERATURE: float = 0.3
    TOP_K: int = 5
    MIN_SCORE: float = 0.25
    CHUNK_MAX_CHARS: int = 1200
    CONTEXT_MESSAGES: int = 12
    HISTORY_LIMIT: int = 100
    DAILY_MESSAGE_LIMIT: int = 0  # 0 = без лимита; лимит сообщений пользователя в сутки
    THROTTLE_SECONDS: int = 4  # 0 = без троттлинга; минимальный интервал между сообщениями пользователя
    RESPONSE_CACHE_TTL: int = 900
    EMBEDDING_CACHE_TTL: int = 3600

    SUMMARY_ENABLED: bool = True
    SUMMARY_EVERY_N_TURNS: int = 3  # каждые N сообщений пользователя — пересобрать сводку диалога
    SUMMARY_MAX_TOKENS: int = 220
    SUMMARY_MODEL: str = ''  # пусто = использовать тот же MODEL
    MAX_QUESTION_CHARS: int = 1500  # обрезка слишком длинных сообщений пользователя (экономия токенов)
    KB_MIN_QUESTION_CHARS: int = 6  # не искать в базе знаний по коротким репликам («ок», «спасибо»)
    KB_DROP_LOW_VALUE: bool = True  # отсеивать при загрузке базы знаний мусорные пары (болталка, реклама, партнёрка)
    RETRIEVAL_CONTEXT_MESSAGES: int = 2  # сколько последних сообщений добавлять в поисковый запрос к базе знаний
    HEDGE_ESCALATION: bool = True  # эскалировать, если модель отвечает догадками без опоры на базу знаний
    ESCALATION_USER_NOTICE: str = 'Уточняю этот вопрос у оператора, подождите, пожалуйста.'
    PGVECTOR_ENABLED: bool = True  # использовать pgvector для поиска, если БД — PostgreSQL
    ALERT_ADMINS_ON_FAILURE: bool = True  # уведомлять админов о сбоях чтения основной БД
    ALERT_THROTTLE_SECONDS: int = 900

    NAVIGATION_ENABLED: bool = True  # подсказывать пути к разделам бота и веб-кабинета
    NAVIGATION_LANGUAGES: str = 'ru'  # языки, для которых дерево навигации строится при запуске
    NAVIGATION_TTL: int = 900  # 0 = не пересобирать дерево автоматически, только при запуске
    NAVIGATION_TOP_K: int = 3  # сколько разделов возвращает инструмент навигации
    NAVIGATION_DEPTH: int = 2  # глубина выводимого поддерева
    NAVIGATION_MAX_CHILDREN: int = 8  # максимум вложенных пунктов на уровень
    NAVIGATION_MAX_CHARS: int = 1400  # ограничение блока навигации в промпте
    NAVIGATION_MIN_QUESTION_CHARS: int = 6  # не искать раздел по коротким репликам

    SERVICE_CATALOG_ENABLED: bool = True  # добавлять в промпт тарифы, промокоды и скидки из основной БД
    SERVICE_CATALOG_TTL: int = 600  # кеш каталога услуг в секундах
    SERVICE_CATALOG_MAX_CHARS: int = 1600  # ограничение блока каталога в промпте

    DATABASE_URL: str = DEFAULT_SQLITE_URL
    MAIN_DATABASE_URL: str = ''

    INCLUDE_REMNAWAVE_DATA: bool = True
    REMNAWAVE_API_URL: str = ''
    REMNAWAVE_API_TOKEN: str = ''

    ADMIN_HOST: str = '0.0.0.0'
    ADMIN_PORT: int = 8090
    ADMIN_USERNAME: str = ''
    ADMIN_PASSWORD: str = ''
    ADMIN_SECRET_KEY: str = ''

    SYSTEM_PROMPT: str = DEFAULT_SYSTEM_PROMPT

    @property
    def effective_database_url(self) -> str:
        if self.DATABASE_URL and 'sqlite' in self.DATABASE_URL:
            if self.DATABASE_URL.strip() != DEFAULT_SQLITE_URL:
                return self.DATABASE_URL.strip()

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

    @property
    def admin_panel_configured(self) -> bool:
        return bool(self.ADMIN_PASSWORD.strip() or self.ADMIN_SECRET_KEY.strip() or self.ADMIN_USERNAME.strip())

    @property
    def embedding_dim(self) -> int:
        if self.EMBEDDING_DIM > 0:
            return self.EMBEDDING_DIM
        known = {
            'text-embedding-3-small': 1536,
            'text-embedding-3-large': 3072,
            'text-embedding-ada-002': 1536,
        }
        return known.get((self.EMBEDDING_MODEL or '').strip(), 1536)

    def security_problems(self) -> list[str]:
        problems: list[str] = []
        if not self.admin_panel_configured:
            return problems

        username = self.ADMIN_USERNAME.strip()
        password = self.ADMIN_PASSWORD.strip()
        secret = self.ADMIN_SECRET_KEY.strip()

        if not username:
            problems.append('AISUP_ADMIN_USERNAME не задан')
        if not password:
            problems.append('AISUP_ADMIN_PASSWORD не задан')
        elif password.lower() in INSECURE_ADMIN_VALUES:
            problems.append('AISUP_ADMIN_PASSWORD использует небезопасное значение по умолчанию')
        elif len(password) < 10:
            problems.append('AISUP_ADMIN_PASSWORD короче 10 символов')

        if not secret:
            problems.append('AISUP_ADMIN_SECRET_KEY не задан')
        elif secret.lower() in INSECURE_ADMIN_VALUES:
            problems.append('AISUP_ADMIN_SECRET_KEY использует небезопасное значение по умолчанию')
        elif len(secret) < 32:
            problems.append('AISUP_ADMIN_SECRET_KEY короче 32 символов')

        return problems

    def assert_secure(self) -> None:
        problems = self.security_problems()
        if problems:
            raise RuntimeError(
                'Небезопасная конфигурация админки ИИ-поддержки: '
                + '; '.join(problems)
                + '. Задайте значения в .env или уберите переменные AISUP_ADMIN_* полностью.'
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
