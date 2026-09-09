# Использование API статистики кнопок меню

## Обзор

Система статистики кликов по кнопкам меню позволяет отслеживать, какие кнопки чаще всего нажимают пользователи.

## API Эндпоинты

### 1. Логирование клика по кнопке

**POST** `/menu-layout/stats/log-click`

**Параметры:**
- `button_id` (str) - ID кнопки
- `user_id` (int, optional) - ID пользователя (telegram_id)
- `callback_data` (str, optional) - callback_data кнопки
- `button_type` (str, optional) - тип кнопки: `builtin`, `callback`, `url`, `mini_app`
- `button_text` (str, optional) - текст кнопки на момент клика

**Пример:**
```python
await MenuLayoutService.log_button_click(
    db,
    button_id='menu_balance',
    user_id=123456789,
    callback_data='menu_balance',
    button_type='builtin',
    button_text='💰 Баланс',
)
```

### 2. Получение статистики по конкретной кнопке

**GET** `/menu-layout/stats/buttons/{button_id}?days=30`

**Возвращает:**
- `clicks_total` - общее количество кликов
- `clicks_today` - клики сегодня
- `clicks_week` - клики за неделю
- `clicks_month` - клики за месяц
- `unique_users` - уникальные пользователи
- `last_click_at` - последний клик
- `clicks_by_day` - клики по дням

**Пример:**
```python
stats = await MenuLayoutService.get_button_stats(db, 'menu_balance', days=30)
# Возвращает:
# {
#     "button_id": "menu_balance",
#     "clicks_total": 150,
#     "clicks_today": 5,
#     "clicks_week": 25,
#     "clicks_month": 150,
#     "unique_users": 45,
#     "last_click_at": datetime(...)
# }
```

### 3. Получение общей статистики по всем кнопкам

**GET** `/menu-layout/stats?days=30`

**Возвращает:**
- `items` - список статистики по каждой кнопке
- `total_clicks` - общее количество кликов
- `period_start` - начало периода
- `period_end` - конец периода

**Пример:**
```python
all_stats = await MenuLayoutService.get_all_buttons_stats(db, days=30)
total = await MenuLayoutService.get_total_clicks(db, days=30)
```

## Автоматическое логирование

✅ **Логирование кликов происходит автоматически!**

Все клики по кнопкам автоматически логируются через `ButtonStatsMiddleware`. Middleware перехватывает все `CallbackQuery` события и логирует их в базу данных.

### Как это работает

1. При каждом клике по кнопке middleware автоматически:
   - Извлекает `callback_data` (используется как `button_id`)
   - Получает `user_id` из события
   - Определяет тип кнопки (`builtin`, `callback`, `url`)
   - Извлекает текст кнопки из клавиатуры (если доступен)
   - Логирует в базу данных асинхронно (не блокирует обработку)

2. Middleware активируется автоматически, если `MENU_LAYOUT_ENABLED=True`

3. Логирование происходит в фоновом режиме и не влияет на производительность

### Ручное логирование (опционально)

Если нужно логировать клики вручную (например, для внешних интеграций), можно использовать API:

```python
# Через сервис
await MenuLayoutService.log_button_click(
    db,
    button_id='custom_button',
    user_id=user_id,
    callback_data='custom_callback',
    button_type='callback',
    button_text='Кастомная кнопка',
)

# Или через API эндпоинт
POST / menu - layout / stats / log - click
{
    'button_id': 'custom_button',
    'user_id': 123456789,
    'callback_data': 'custom_callback',
    'button_type': 'callback',
    'button_text': 'Кастомная кнопка',
}
```

### 4. Статистика по типам кнопок

**GET** `/menu-layout/stats/by-type?days=30`

**Возвращает:**
- Статистику кликов по каждому типу кнопок (builtin, callback, url, mini_app)
- Общее количество кликов по типам

**Пример:**
```python
stats = await MenuLayoutService.get_stats_by_button_type(db, days=30)
# Возвращает:
# [
#     {"button_type": "builtin", "clicks_total": 500, "unique_users": 100},
#     {"button_type": "callback", "clicks_total": 200, "unique_users": 50},
#     ...
# ]
```

### 5. Статистика по часам дня

**GET** `/menu-layout/stats/by-hour?button_id=menu_balance&days=30`

**Параметры:**
- `button_id` (optional) - ID кнопки для фильтрации
- `days` (default: 30) - период в днях

**Возвращает:**
- Распределение кликов по часам дня (0-23)

**Пример:**
```python
stats = await MenuLayoutService.get_clicks_by_hour(db, button_id='menu_balance', days=30)
# Возвращает:
# [
#     {"hour": 9, "count": 50},
#     {"hour": 10, "count": 75},
#     ...
# ]
```

### 6. Статистика по дням недели

**GET** `/menu-layout/stats/by-weekday?button_id=menu_balance&days=30`

**Возвращает:**
- Распределение кликов по дням недели (0=понедельник, 6=воскресенье)

**Пример:**
```python
stats = await MenuLayoutService.get_clicks_by_weekday(db, button_id='menu_balance', days=30)
# Возвращает:
# [
#     {"weekday": 0, "weekday_name": "Понедельник", "count": 100},
#     {"weekday": 1, "weekday_name": "Вторник", "count": 120},
#     ...
# ]
```

### 7. Топ пользователей по кликам

**GET** `/menu-layout/stats/top-users?button_id=menu_balance&limit=10&days=30`

**Параметры:**
- `button_id` (optional) - ID кнопки для фильтрации
- `limit` (default: 10) - количество пользователей
- `days` (default: 30) - период в днях

**Возвращает:**
- Список пользователей с наибольшим количеством кликов

**Пример:**
```python
top_users = await MenuLayoutService.get_top_users(db, button_id='menu_balance', limit=10, days=30)
# Возвращает:
# [
#     {"user_id": 123456789, "clicks_count": 50, "last_click_at": datetime(...)},
#     ...
# ]
```

### 8. Сравнение периодов

**GET** `/menu-layout/stats/compare?button_id=menu_balance&current_days=7&previous_days=7`

**Параметры:**
- `button_id` (optional) - ID кнопки для фильтрации
- `current_days` (default: 7) - период текущего сравнения
- `previous_days` (default: 7) - период предыдущего сравнения

**Возвращает:**
- Сравнение текущего и предыдущего периода
- Изменение в абсолютных числах и процентах
- Тренд (up/down/stable)

**Пример:**
```python
comparison = await MenuLayoutService.get_period_comparison(
    db, button_id='menu_balance', current_days=7, previous_days=7
)
# Возвращает:
# {
#     "current_period": {"clicks": 100, "days": 7, ...},
#     "previous_period": {"clicks": 80, "days": 7, ...},
#     "change": {"absolute": 20, "percent": 25.0, "trend": "up"}
# }
```

### 9. Последовательности кликов пользователя

**GET** `/menu-layout/stats/users/{user_id}/sequences?limit=50`

**Параметры:**
- `user_id` (path) - ID пользователя
- `limit` (default: 50) - максимальное количество записей

**Возвращает:**
- Хронологическую последовательность кликов пользователя

**Пример:**
```python
sequences = await MenuLayoutService.get_user_click_sequences(db, user_id=123456789, limit=50)
# Возвращает:
# [
#     {"button_id": "menu_balance", "button_text": "💰 Баланс", "clicked_at": datetime(...)},
#     {"button_id": "menu_subscription", "button_text": "📊 Подписка", "clicked_at": datetime(...)},
#     ...
# ]
```

## Важные замечания

1. **Автоматическое логирование**: Все клики по кнопкам логируются автоматически через `ButtonStatsMiddleware`
2. **Требуется авторизация**: API эндпоинты для получения статистики требуют токен авторизации (`require_api_token`)
3. **button_id**: Используется `callback_data` кнопки как идентификатор
4. **Производительность**: Логирование выполняется асинхронно в фоне и не блокирует обработку запросов
5. **Активация**: Middleware работает только если `MENU_LAYOUT_ENABLED=True` в настройках
6. **Временные зоны**: Все временные метрики используют локальное время сервера

