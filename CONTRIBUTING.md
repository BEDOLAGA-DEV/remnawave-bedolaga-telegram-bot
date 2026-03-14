# 🤝 Contributing to Remnawave Bedolaga Bot

Спасибо за интерес к развитию проекта! Этот документ содержит правила и рекомендации для контрибьюторов.

## 📋 Содержание

- [Кодекс поведения](#-кодекс-поведения)
- [Как помочь проекту](#-как-помочь-проекту)
- [Настройка среды разработки](#-настройка-среды-разработки)
- [Стандарты кода](#-стандарты-кода)
- [Работа с Git](#-работа-с-git)
- [Тестирование](#-тестирование)
- [Процесс ревью](#-процесс-ревью)
- [Документация](#-документация)

## 📜 Кодекс поведения

### Основные принципы

- **Уважение** - относитесь к участникам проекта с уважением
- **Конструктивность** - предлагайте решения, а не только критику
- **Открытость** - будьте открыты к обратной связи и новым идеям
- **Профессионализм** - поддерживайте высокий уровень обсуждений

### Недопустимое поведение

- Оскорбления и личные атаки
- Спам и офф-топик сообщения
- Публикация приватной информации
- Любые формы дискриминации

## 🚀 Как помочь проекту

### 🐛 Сообщения о багах

Перед созданием issue проверьте:
- [ ] Аналогичная проблема не была описана ранее
- [ ] Вы используете актуальную версию бота
- [ ] Проблема воспроизводится стабильно

**Шаблон для багрепорта:**

```markdown
## 🐛 Описание бага
Краткое описание проблемы

## 🔄 Шаги воспроизведения
1. Перейти к '...'
2. Нажать на '...'
3. Увидеть ошибку

## ✅ Ожидаемое поведение
Что должно было произойти

## ❌ Фактическое поведение
Что произошло на самом деле

## 🌍 Окружение
- Версия бота: [например, 1.0.0]
- Python версия: [например, 3.11.7]
- ОС: [например, Ubuntu 22.04]
- Версия Docker: [если используется]

## 📋 Логи
```
Вставьте соответствующие логи
```

## 📷 Скриншоты
Если применимо, добавьте скриншоты
```

### 💡 Предложения функций

Используйте лейбл `enhancement` и опишите:
- **Проблему**, которую решает функция
- **Предлагаемое решение**
- **Альтернативы**, которые вы рассматривали
- **Дополнительную информацию** и контекст

### 📝 Улучшение документации

- Исправление опечаток
- Дополнение примеров
- Перевод на другие языки
- Улучшение структуры

## 🛠 Настройка среды разработки

### Требования

- Python 3.11+
- Docker и Docker Compose
- Git
- PostgreSQL 15+ (опционально для локальной разработки)
- Redis (опционально для локальной разработки)

### Установка

1. **Форкните и клонируйте репозиторий:**
```bash
git clone https://github.com/Fr1ngg/remnawave-bedolaga-telegram-bot.git
cd remnawave-bedolaga-telegram-bot
```

2. **Создайте виртуальное окружение:**
```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
# или
venv\Scripts\activate  # Windows
```

3. **Установите зависимости:**
```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt  # если есть dev зависимости
```

4. **Настройте окружение:**
```bash
cp .env.example .env
# Отредактируйте .env файл с вашими настройками
```

5. **Запустите через Docker (рекомендуется):**
```bash
docker compose up -d postgres redis
python main.py
```

### Структура проекта

```
bedolaga_bot/
├── app/                     # Основной код приложения
│   ├── handlers/           # Обработчики сообщений
│   ├── services/           # Бизнес-логика
│   ├── database/           # Модели и CRUD операции
│   ├── utils/              # Утилиты
│   ├── middlewares/        # Middleware
│   └── external/           # Внешние API
├── migrations/             # Миграции БД (если нужны)
├── tests/                  # Тесты (создать при необходимости)
├── docs/                   # Документация
└── requirements.txt        # Зависимости
```

## 🎨 Стандарты кода

### Python стиль

Мы следуем **PEP 8** с некоторыми исключениями:

```python
# ✅ Хорошо
async def get_user_subscription(user_id: int) -> Optional[Subscription]:
    """Получает активную подписку пользователя."""
    async with get_session() as session:
        result = await session.execute(
            select(Subscription)
            .where(Subscription.user_id == user_id)
            .where(Subscription.is_active == True)
        )
        return result.scalar_one_or_none()

# ❌ Плохо
async def getUserSub(uid):
    session = get_session()
    sub = session.query(Subscription).filter(Subscription.user_id==uid,Subscription.is_active==True).first()
    return sub
```

### Правила именования

- **Функции и переменные**: `snake_case`
- **Классы**: `PascalCase`
- **Константы**: `UPPER_CASE`
- **Приватные методы**: `_leading_underscore`

### Типизация

Обязательно используйте type hints:

```python
from typing import Optional, List, Dict, Any

async def create_subscription(
    user_id: int, 
    duration_days: int,
    traffic_limit_gb: Optional[int] = None
) -> Subscription:
    """Создает новую подписку."""
    # implementation
```

### Документация кода

```python
from app.services.pricing_engine import PricingEngine

pricing = PricingEngine.calculate_renewal_price(
    subscription=subscription,
    period_days=30,
    user=user,
)
# pricing.final_total — стоимость в копейках
# pricing.original_total — цена до скидок
# pricing.promo_group_discount — скидка промогруппы
# pricing.promo_offer_discount — скидка промо-оффера
```

### Обработка ошибок

```python
# ✅ Хорошо
try:
    subscription = await subscription_service.create_subscription(user_id, data)
    await message.answer("✅ Подписка создана успешно!")
except RemnaWaveAPIError as e:
    logger.error(f"RemnaWave API error: {e}")
    await message.answer("❌ Ошибка при создании подписки. Попробуйте позже.")
except ValidationError as e:
    logger.warning(f"Validation error: {e}")
    await message.answer("❌ Некорректные данные для создания подписки.")

# ❌ Плохо
try:
    subscription = await subscription_service.create_subscription(user_id, data)
    await message.answer("✅ Подписка создана успешно!")
except:
    await message.answer("Ошибка")
```

### Логирование

```python
import logging

logger = logging.getLogger(__name__)

# Уровни логирования
logger.debug("Детальная информация для отладки")
logger.info("Общая информация о работе")
logger.warning("Предупреждение о потенциальной проблеме") 
logger.error("Ошибка, которая не прерывает работу")
logger.critical("Критическая ошибка")
```

## 🔄 Работа с Git

### Ветки

- `main` - стабильная версия
- `dev` - разработка новых функций

### Коммиты

Используйте [Conventional Commits](https://www.conventionalcommits.org/):

```bash
# Типы коммитов
feat: добавить поддержку CryptoBot платежей
fix: исправить ошибку с расчетом цены подписки
docs: обновить документацию по настройке
style: исправить форматирование кода
refactor: рефакторинг сервиса платежей
test: добавить тесты для subscription_service
chore: обновить зависимости

# Примеры
git commit -m "feat(payments): добавить поддержку YooKassa webhook"
git commit -m "fix(subscription): исправить расчет стоимости устройств"
git commit -m "docs(readme): обновить инструкцию по установке"
```

### Pull Request процесс

1. **Создайте ветку** от `dev`:
```bash
git checkout develop
git pull origin develop
git checkout -b feature/new-payment-method
```

2. **Разработайте функцию** с соблюдением стандартов

3. **Протестируйте** изменения локально

4. **Создайте Pull Request** с описанием:

```markdown
## 📝 Описание
Краткое описание изменений

## 🎯 Мотивация
Почему эти изменения нужны?

## 🔧 Тип изменений
- [ ] Bug fix (исправление)
- [ ] New feature (новая функция)
- [ ] Breaking change (ломающие изменения)
- [ ] Documentation update (обновление документации)

## ✅ Чеклист
- [ ] Код соответствует стандартам проекта
- [ ] Добавлены/обновлены тесты
- [ ] Документация обновлена
- [ ] Проверена работа в Docker
- [ ] Проверена совместимость с существующим API

## 🧪 Тестирование
Как тестировались изменения:
- [ ] Локальное тестирование
- [ ] Тестирование с реальным Remnawave API
- [ ] Тестирование платежных систем
```

## 🧪 Тестирование

### Локальное тестирование

```bash
# Запуск с тестовой базой
export DATABASE_URL="sqlite:///test.db"
export BOT_TOKEN="test_token"
python main.py
```

### Тестирование компонентов

```python
# tests/services/test_pricing_engine.py
import pytest
from app.services.pricing_engine import PricingEngine

def test_calculate_renewal_price():
    pricing = PricingEngine.calculate_renewal_price(
        subscription=mock_subscription,
        period_days=30,
        user=mock_user,
    )
    assert pricing.final_total > 0
    assert isinstance(pricing.final_total, int)
```

### Integration тесты

Тестируйте интеграцию с:
- Remnawave API (с тестовыми данными)
- Базой данных
- Платежными системами (sandbox режим)

## 👀 Процесс ревью

### Требования для ревьюера

- Проверить соответствие стандартам кода
- Убедиться в работоспособности
- Проверить безопасность (особенно для платежных функций)
- Оценить производительность
- Проверить совместимость с существующим API

### Требования для автора

- Ответить на все комментарии
- Исправить замечания
- Обновить документацию при необходимости
- Убедиться в прохождении всех проверок

## 📚 Документация

### Обновление документации

При добавлении новых функций обновляйте:

- `README.md` - если изменился API или конфигурация
- Комментарии в коде
- Примеры использования
- Changelog (если есть)

### Стиль документации

- Используйте ясный и понятный язык
- Добавляйте примеры кода
- Указывайте возможные ошибки и их решения
- Используйте эмодзи для улучшения читаемости

## 🏷 Лейблы Issues и PR

### Приоритет
- `priority:high` - высокий приоритет
- `priority:medium` - средний приоритет  
- `priority:low` - низкий приоритет

### Тип
- `bug` - ошибка
- `enhancement` - улучшение
- `feature` - новая функция
- `documentation` - документация
- `question` - вопрос

### Область
- `payments` - платежные системы
- `api` - Remnawave API
- `database` - база данных
- `ui/ux` - интерфейс пользователя
- `admin` - админ панель

## 🔐 Безопасность

### Сообщения о уязвимостях

Для сообщений о критических уязвимостях безопасности:
- Свяжитесь с [@fringg](https://t.me/fringg) напрямую
- Не создавайте публичные issues для уязвимостей
- Дайте время на исправление перед публичным раскрытием

### Рекомендации по безопасности

- Никогда не коммитьте API ключи и пароли
- Используйте переменные окружения для чувствительных данных
- Валидируйте все пользовательские данные
- Используйте HTTPS для всех внешних запросов

## 📞 Получение помощи

### Каналы связи

- **💬 Telegram Group:** [Bedolaga Chat](https://t.me/+wTdMtSWq8YdmZmVi) - общие вопросы
- **🐛 GitHub Issues:** Технические вопросы и баги
- **📧 Прямой контакт:** [@fringg](https://t.me/fringg) - только критические вопросы

### Часто задаваемые вопросы

**Q: Как настроить локальную разработку без Docker?**
A: Установите PostgreSQL и Redis локально, обновите DATABASE_URL и REDIS_URL в .env

**Q: Можно ли использовать SQLite для разработки?**
A: Да, установите DATABASE_MODE=sqlite в .env

**Q: Как тестировать платежные системы?**
A: Используйте тестовые/sandbox режимы платежных систем

**Q: Что делать, если тесты не проходят?**
A: Проверьте конфигурацию .env и убедитесь, что все сервисы запущены

## 📋 Чеклист для контрибьюторов

Перед отправкой PR убедитесь:

- [ ] ✅ Код соответствует стандартам проекта
- [ ] 📝 Добавлены комментарии и docstrings
- [ ] 🧪 Функциональность протестирована
- [ ] 📚 Документация обновлена
- [ ] 🔒 Нет чувствительных данных в коде
- [ ] 🐳 Изменения работают в Docker
- [ ] 📋 PR создан с подробным описанием
- [ ] 🏷 Проставлены соответствующие лейблы

## 🎉 Благодарности

Спасибо всем, кто вносит вклад в развитие проекта! Ваша помощь делает Bedolaga Bot лучше для всего сообщества.

---

**Made with ❤️ by community**
