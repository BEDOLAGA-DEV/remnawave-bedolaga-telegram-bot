# WebSocket и Webhooks для веб-админки

## Обзор

Реализованы две системы для real-time обновлений и интеграций:

1. **WebSocket** - для real-time обновлений в веб-админке
2. **Webhooks** - для отправки событий во внешние системы

## WebSocket

### Подключение

WebSocket endpoint доступен по адресу: `ws://your-api-host:port/ws`

Для подключения требуется токен API (передается через query параметр):

```javascript
const ws = new WebSocket('ws://localhost:8080/ws?token=YOUR_API_TOKEN');
// или
const ws = new WebSocket('ws://localhost:8080/ws?api_key=YOUR_API_TOKEN');
```

### Формат сообщений

#### Входящие сообщения (от сервера)

```json
{
  "type": "connection",
  "status": "connected",
  "message": "WebSocket connection established"
}
```

```json
{
  "type": "user.created",
  "payload": {
    "user_id": 123,
    "telegram_id": 456789,
    "username": "testuser",
    "first_name": "Test",
    "last_name": "User",
    "referral_code": "refABC123",
    "referred_by_id": null
  },
  "timestamp": "2024-01-15T10:30:00"
}
```

#### Исходящие сообщения (от клиента)

**Ping для keepalive:**
```json
{
  "type": "ping"
}
```

Сервер ответит:
```json
{
  "type": "pong"
}
```

### Поддерживаемые события

- `user.created` - создан новый пользователь
- `payment.completed` - завершен платеж (пополнение баланса)
- `transaction.created` - создана транзакция
- `ticket.created` - создан новый тикет
- `ticket.status_changed` - изменен статус тикета
- `ticket.message_added` - добавлено новое сообщение в тикет (от пользователя или админа)

## Webhooks

### Создание webhook

```bash
POST /webhooks
Authorization: Bearer YOUR_API_TOKEN
Content-Type: application/json

{
  "name": "My Webhook",
  "url": "https://example.com/webhook",
  "event_type": "user.created",
  "secret": "optional-secret-for-signing",
  "description": "Webhook для новых пользователей"
}
```

### Поддерживаемые типы событий

- `user.created` - создан новый пользователь
- `payment.completed` - завершен платеж
- `transaction.created` - создана транзакция
- `ticket.created` - создан новый тикет
- `ticket.status_changed` - изменен статус тикета

### Формат payload

Webhook отправляет POST запрос с JSON payload:

```json
{
  "user_id": 123,
  "telegram_id": 456789,
  "username": "testuser",
  "first_name": "Test",
  "last_name": "User",
  "referral_code": "refABC123",
  "referred_by_id": null
}
```

### Заголовки запроса

- `Content-Type: application/json`
- `X-Webhook-Event: user.created` - тип события
- `X-Webhook-Id: 1` - ID webhook
- `X-Webhook-Signature: sha256=...` - подпись (если указан secret)

### Подпись payload

Если при создании webhook указан `secret`, payload подписывается с помощью HMAC-SHA256:

```python
import hmac
import hashlib

signature = hmac.new(
    secret.encode('utf-8'),
    payload_json.encode('utf-8'),
    hashlib.sha256
).hexdigest()
```

Заголовок: `X-Webhook-Signature: sha256={signature}`

### Проверка подписи (пример на Python)

```python
import hmac
import hashlib
import json

def verify_webhook_signature(payload: dict, signature_header: str, secret: str) -> bool:
    payload_json = json.dumps(payload, sort_keys=True)
    expected_signature = hmac.new(
        secret.encode('utf-8'),
        payload_json.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    
    received_signature = signature_header.replace('sha256=', '')
    return hmac.compare_digest(expected_signature, received_signature)
```

### API эндпоинты

#### Список webhooks
```
GET /webhooks?event_type=user.created&is_active=true&limit=50&offset=0
```

#### Получить webhook
```
GET /webhooks/{webhook_id}
```

#### Обновить webhook
```
PATCH /webhooks/{webhook_id}
{
  "name": "Updated Name",
  "is_active": false
}
```

#### Удалить webhook
```
DELETE /webhooks/{webhook_id}
```

#### Статистика webhooks
```
GET /webhooks/stats
```

#### История доставок
```
GET /webhooks/{webhook_id}/deliveries?status=failed&limit=50&offset=0
```

### Статусы доставки

- `pending` - ожидает отправки
- `success` - успешно доставлен (HTTP 200-299)
- `failed` - ошибка доставки

### Retry логика

В текущей реализации retry не реализован автоматически, но можно добавить через `next_retry_at` поле в `WebhookDelivery`.

## Интеграция событий

События автоматически отправляются при:

1. **Создании пользователя** (`app/database/crud/user.py::create_user`)
2. **Создании транзакции** (`app/database/crud/transaction.py::create_transaction`)
3. **Создании тикета** (`app/database/crud/ticket.py::create_ticket`)
4. **Изменении статуса тикета** (`app/database/crud/ticket.py::update_ticket_status`)

## Примеры использования

### JavaScript WebSocket клиент

```javascript
const ws = new WebSocket('ws://localhost:8080/ws?token=YOUR_TOKEN');

ws.onopen = () => {
  console.log('Connected');
};

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log('Event:', data.type, data.payload);
  
  if (data.type === 'user.created') {
    // Обработка нового пользователя
    updateDashboard(data.payload);
  }
};

ws.onerror = (error) => {
  console.error('WebSocket error:', error);
};

ws.onclose = () => {
  console.log('Disconnected');
};

// Ping для keepalive
setInterval(() => {
  if (ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'ping' }));
  }
}, 30000);
```

### Python Webhook receiver

```python
from flask import Flask, request
import hmac
import hashlib
import json

app = Flask(__name__)
WEBHOOK_SECRET = "your-secret"

@app.route('/webhook', methods=['POST'])
def webhook():
    signature = request.headers.get('X-Webhook-Signature', '')
    event_type = request.headers.get('X-Webhook-Event')
    payload = request.json
    
    # Проверка подписи
    if not verify_signature(payload, signature, WEBHOOK_SECRET):
        return {'error': 'Invalid signature'}, 401
    
    # Обработка события
    if event_type == 'user.created':
        handle_new_user(payload)
    elif event_type == 'payment.completed':
        handle_payment(payload)
    
    return {'status': 'ok'}, 200

def verify_signature(payload, signature, secret):
    payload_json = json.dumps(payload, sort_keys=True)
    expected = hmac.new(
        secret.encode(),
        payload_json.encode(),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature.replace('sha256=', ''))
```

## Безопасность

1. **WebSocket**: Требует валидный API токен
2. **Webhooks**: 
   - Используйте HTTPS для webhook URL
   - Используйте secret для подписи payload
   - Проверяйте подпись на стороне получателя
   - Ограничьте IP адреса получателей (если возможно)

## Мониторинг

- Проверяйте статистику webhooks через `/webhooks/stats`
- Просматривайте историю доставок через `/webhooks/{id}/deliveries`
- Мониторьте логи на наличие ошибок доставки

