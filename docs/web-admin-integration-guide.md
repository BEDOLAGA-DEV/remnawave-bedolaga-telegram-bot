# Руководство по интеграции WebSocket и Webhooks в веб-админку

## Содержание

1. [Обзор](#обзор)
2. [Настройка WebSocket подключения](#настройка-websocket-подключения)
3. [Интеграция WebSocket в дашборд](#интеграция-websocket-в-дашборд)
4. [Управление Webhooks через API](#управление-webhooks-через-api)
5. [UI компоненты для Webhooks](#ui-компоненты-для-webhooks)
6. [Примеры реализации](#примеры-реализации)
7. [Обработка ошибок](#обработка-ошибок)
8. [Тестирование](#тестирование)

---

## Обзор

Веб-админка может использовать два механизма для получения обновлений:

1. **WebSocket** - для real-time обновлений в интерфейсе (новые пользователи, платежи, тикеты)
2. **Webhooks** - для настройки внешних интеграций (отправка событий на внешние серверы)

---

## Настройка WebSocket подключения

### Шаг 1: Создать WebSocket менеджер

Создайте утилиту для управления WebSocket подключением:

```typescript
// utils/websocket.ts
class WebSocketManager {
  private ws: WebSocket | null = null;
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 5;
  private reconnectDelay = 1000;
  private listeners: Map<string, Set<Function>> = new Map();
  private apiToken: string;

  constructor(apiToken: string) {
    this.apiToken = apiToken;
  }

  connect(url: string): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      console.log('WebSocket already connected');
      return;
    }

    const wsUrl = `${url}?token=${this.apiToken}`;
    this.ws = new WebSocket(wsUrl);

    this.ws.onopen = () => {
      console.log('WebSocket connected');
      this.reconnectAttempts = 0;
      this.emit('connected', {});
    };

    this.ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        this.handleMessage(data);
      } catch (error) {
        console.error('Failed to parse WebSocket message:', error);
      }
    };

    this.ws.onerror = (error) => {
      console.error('WebSocket error:', error);
      this.emit('error', { error });
    };

    this.ws.onclose = () => {
      console.log('WebSocket disconnected');
      this.emit('disconnected', {});
      this.attemptReconnect(url);
    };

    // Ping для keepalive каждые 30 секунд
    setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ type: 'ping' }));
      }
    }, 30000);
  }

  private handleMessage(data: any): void {
    if (data.type === 'pong') {
      return; // Игнорируем pong
    }

    if (data.type === 'connection') {
      this.emit('connection', data);
      return;
    }

    // Эмитим событие по типу
    this.emit(data.type, data.payload);
  }

  private attemptReconnect(url: string): void {
    if (this.reconnectAttempts >= this.maxReconnectAttempts) {
      console.error('Max reconnect attempts reached');
      return;
    }

    this.reconnectAttempts++;
    const delay = this.reconnectDelay * Math.pow(2, this.reconnectAttempts - 1);

    setTimeout(() => {
      console.log(`Reconnecting... (attempt ${this.reconnectAttempts})`);
      this.connect(url);
    }, delay);
  }

  on(event: string, callback: Function): void {
    if (!this.listeners.has(event)) {
      this.listeners.set(event, new Set());
    }
    this.listeners.get(event)!.add(callback);
  }

  off(event: string, callback: Function): void {
    const callbacks = this.listeners.get(event);
    if (callbacks) {
      callbacks.delete(callback);
    }
  }

  private emit(event: string, data: any): void {
    const callbacks = this.listeners.get(event);
    if (callbacks) {
      callbacks.forEach(callback => {
        try {
          callback(data);
        } catch (error) {
          console.error(`Error in event listener for ${event}:`, error);
        }
      });
    }
  }

  disconnect(): void {
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }

  isConnected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }
}

export default WebSocketManager;
```

### Шаг 2: Инициализация в приложении

```typescript
// App.tsx или main.tsx
import { useEffect, useState } from 'react';
import WebSocketManager from './utils/websocket';
import { getApiToken } from './utils/auth';

function App() {
  const [wsManager, setWsManager] = useState<WebSocketManager | null>(null);

  useEffect(() => {
    const token = getApiToken();
    if (!token) {
      console.warn('No API token found, WebSocket will not connect');
      return;
    }

    const manager = new WebSocketManager(token);
    const wsUrl = process.env.REACT_APP_WS_URL || 'ws://localhost:8080/ws';
    
    manager.connect(wsUrl);
    setWsManager(manager);

    // Обработка событий
    manager.on('user.created', (payload) => {
      console.log('New user created:', payload);
      // Обновить список пользователей
      // Показать уведомление
    });

    manager.on('payment.completed', (payload) => {
      console.log('Payment completed:', payload);
      // Обновить статистику
      // Обновить баланс пользователя
    });

    manager.on('ticket.created', (payload) => {
      console.log('New ticket created:', payload);
      // Обновить список тикетов
      // Показать уведомление
    });

    manager.on('ticket.status_changed', (payload) => {
      console.log('Ticket status changed:', payload);
      // Обновить статус тикета в списке
    });

    manager.on('ticket.message_added', (payload) => {
      console.log('New message in ticket:', payload);
      // Обновить список сообщений в тикете
      // Показать уведомление о новом сообщении
    });

    return () => {
      manager.disconnect();
    };
  }, []);

  return (
    // Ваш компонент приложения
  );
}
```

---

## Интеграция WebSocket в дашборд

### Шаг 1: Создать React Hook для WebSocket

```typescript
// hooks/useWebSocket.ts
import { useEffect, useState, useCallback } from 'react';
import { useWebSocketContext } from '../contexts/WebSocketContext';

export function useWebSocketEvent<T = any>(eventType: string) {
  const { wsManager } = useWebSocketContext();
  const [data, setData] = useState<T | null>(null);

  useEffect(() => {
    if (!wsManager) return;

    const handler = (payload: T) => {
      setData(payload);
    };

    wsManager.on(eventType, handler);

    return () => {
      wsManager.off(eventType, handler);
    };
  }, [wsManager, eventType]);

  return data;
}

// Использование в компоненте
function Dashboard() {
  const newUser = useWebSocketEvent('user.created');
  const newPayment = useWebSocketEvent('payment.completed');
  const newTicket = useWebSocketEvent('ticket.created');

  useEffect(() => {
    if (newUser) {
      // Обновить счетчик пользователей
      // Показать toast уведомление
    }
  }, [newUser]);

  return (
    // Ваш дашборд
  );
}
```

### Шаг 2: Обновление счетчиков в реальном времени

```typescript
// components/DashboardStats.tsx
import { useState, useEffect } from 'react';
import { useWebSocketContext } from '../contexts/WebSocketContext';
import { fetchStats } from '../api/stats';

function DashboardStats() {
  const { wsManager } = useWebSocketContext();
  const [stats, setStats] = useState({
    totalUsers: 0,
    activeSubscriptions: 0,
    openTickets: 0,
    todayRevenue: 0,
  });

  // Загрузка начальных данных
  useEffect(() => {
    loadStats();
  }, []);

  // Подписка на события для обновления
  useEffect(() => {
    if (!wsManager) return;

    const updateOnNewUser = () => {
      setStats(prev => ({ ...prev, totalUsers: prev.totalUsers + 1 }));
    };

    const updateOnPayment = (payload: any) => {
      setStats(prev => ({
        ...prev,
        todayRevenue: prev.todayRevenue + (payload.amount_rubles || 0),
      }));
    };

    const updateOnTicket = () => {
      setStats(prev => ({ ...prev, openTickets: prev.openTickets + 1 }));
    };

    wsManager.on('user.created', updateOnNewUser);
    wsManager.on('payment.completed', updateOnPayment);
    wsManager.on('ticket.created', updateOnTicket);
    wsManager.on('ticket.message_added', updateOnTicketMessage);

    return () => {
      wsManager.off('user.created', updateOnNewUser);
      wsManager.off('payment.completed', updateOnPayment);
      wsManager.off('ticket.created', updateOnTicket);
      wsManager.off('ticket.message_added', updateOnTicketMessage);
    };
  }, [wsManager]);

  const loadStats = async () => {
    try {
      const data = await fetchStats();
      setStats(data);
    } catch (error) {
      console.error('Failed to load stats:', error);
    }
  };

  return (
    <div className="stats-grid">
      <StatCard title="Всего пользователей" value={stats.totalUsers} />
      <StatCard title="Активные подписки" value={stats.activeSubscriptions} />
      <StatCard title="Открытые тикеты" value={stats.openTickets} />
      <StatCard title="Доход сегодня" value={`${stats.todayRevenue} ₽`} />
    </div>
  );
}
```

### Шаг 3: Уведомления о новых событиях

```typescript
// components/NotificationCenter.tsx
import { useState, useEffect } from 'react';
import { useWebSocketContext } from '../contexts/WebSocketContext';
import { toast } from 'react-toastify';

interface Notification {
  id: string;
  type: string;
  message: string;
  timestamp: Date;
}

function NotificationCenter() {
  const { wsManager } = useWebSocketContext();
  const [notifications, setNotifications] = useState<Notification[]>([]);

  useEffect(() => {
    if (!wsManager) return;

    const handleNewUser = (payload: any) => {
      const notification: Notification = {
        id: `user-${payload.user_id}`,
        type: 'user.created',
        message: `Новый пользователь: @${payload.username || payload.telegram_id}`,
        timestamp: new Date(),
      };
      addNotification(notification);
      toast.info(notification.message);
    };

    const handleNewPayment = (payload: any) => {
      const notification: Notification = {
        id: `payment-${payload.transaction_id}`,
        type: 'payment.completed',
        message: `Пополнение баланса: ${payload.amount_rubles} ₽`,
        timestamp: new Date(),
      };
      addNotification(notification);
      toast.success(notification.message);
    };

    const handleNewTicket = (payload: any) => {
      const notification: Notification = {
        id: `ticket-${payload.ticket_id}`,
        type: 'ticket.created',
        message: `Новый тикет: ${payload.title}`,
        timestamp: new Date(),
      };
      addNotification(notification);
      toast.warning(notification.message, {
        onClick: () => {
          // Перейти к тикету
          window.location.href = `/tickets/${payload.ticket_id}`;
        },
      });
    };

    const handleNewMessage = (payload: any) => {
      const notification: Notification = {
        id: `ticket-message-${payload.message_id}`,
        type: 'ticket.message_added',
        message: payload.is_from_admin 
          ? `Новый ответ в тикете #${payload.ticket_id}`
          : `Новое сообщение от пользователя в тикете #${payload.ticket_id}`,
        timestamp: new Date(),
      };
      addNotification(notification);
      toast.info(notification.message, {
        onClick: () => {
          // Перейти к тикету
          window.location.href = `/tickets/${payload.ticket_id}`;
        },
      });
    };

    wsManager.on('user.created', handleNewUser);
    wsManager.on('payment.completed', handleNewPayment);
    wsManager.on('ticket.created', handleNewTicket);
    wsManager.on('ticket.message_added', handleNewMessage);

    return () => {
      wsManager.off('user.created', handleNewUser);
      wsManager.off('payment.completed', handleNewPayment);
      wsManager.off('ticket.created', handleNewTicket);
      wsManager.off('ticket.message_added', handleNewMessage);
    };
  }, [wsManager]);

  const addNotification = (notification: Notification) => {
    setNotifications(prev => [notification, ...prev].slice(0, 50)); // Храним последние 50
  };

  return (
    <div className="notification-center">
      {notifications.map(notif => (
        <NotificationItem key={notif.id} notification={notif} />
      ))}
    </div>
  );
}
```

---

## Управление Webhooks через API

### Шаг 1: API клиент для webhooks

```typescript
// api/webhooks.ts
import { apiClient } from './client';

export interface Webhook {
  id: number;
  name: string;
  url: string;
  event_type: string;
  is_active: boolean;
  description?: string;
  created_at: string;
  updated_at: string;
  last_triggered_at?: string;
  failure_count: number;
  success_count: number;
}

export interface WebhookCreateRequest {
  name: string;
  url: string;
  event_type: string;
  secret?: string;
  description?: string;
}

export interface WebhookUpdateRequest {
  name?: string;
  url?: string;
  secret?: string;
  description?: string;
  is_active?: boolean;
}

export const webhooksApi = {
  // Список webhooks
  list: async (params?: {
    event_type?: string;
    is_active?: boolean;
    limit?: number;
    offset?: number;
  }): Promise<{ items: Webhook[]; total: number }> => {
    const response = await apiClient.get('/webhooks', { params });
    return response.data;
  },

  // Получить webhook
  get: async (id: number): Promise<Webhook> => {
    const response = await apiClient.get(`/webhooks/${id}`);
    return response.data;
  },

  // Создать webhook
  create: async (data: WebhookCreateRequest): Promise<Webhook> => {
    const response = await apiClient.post('/webhooks', data);
    return response.data;
  },

  // Обновить webhook
  update: async (id: number, data: WebhookUpdateRequest): Promise<Webhook> => {
    const response = await apiClient.patch(`/webhooks/${id}`, data);
    return response.data;
  },

  // Удалить webhook
  delete: async (id: number): Promise<void> => {
    await apiClient.delete(`/webhooks/${id}`);
  },

  // Статистика
  getStats: async (): Promise<{
    total_webhooks: number;
    active_webhooks: number;
    total_deliveries: number;
    successful_deliveries: number;
    failed_deliveries: number;
    success_rate: number;
  }> => {
    const response = await apiClient.get('/webhooks/stats');
    return response.data;
  },

  // История доставок
  getDeliveries: async (
    webhookId: number,
    params?: { status?: string; limit?: number; offset?: number }
  ): Promise<{ items: any[]; total: number }> => {
    const response = await apiClient.get(`/webhooks/${webhookId}/deliveries`, { params });
    return response.data;
  },
};
```

### Шаг 2: Список доступных типов событий

```typescript
// constants/webhookEvents.ts
export const WEBHOOK_EVENT_TYPES = [
  {
    value: 'user.created',
    label: 'Создание пользователя',
    description: 'Отправляется при регистрации нового пользователя',
  },
  {
    value: 'payment.completed',
    label: 'Завершение платежа',
    description: 'Отправляется при успешном пополнении баланса',
  },
  {
    value: 'transaction.created',
    label: 'Создание транзакции',
    description: 'Отправляется при создании любой транзакции',
  },
  {
    value: 'ticket.created',
    label: 'Создание тикета',
    description: 'Отправляется при создании нового тикета поддержки',
  },
  {
    value: 'ticket.status_changed',
    label: 'Изменение статуса тикета',
    description: 'Отправляется при изменении статуса тикета',
  },
  {
    value: 'ticket.message_added',
    label: 'Новое сообщение в тикете',
    description: 'Отправляется при добавлении нового сообщения в тикет (от пользователя или админа)',
  },
] as const;

export type WebhookEventType = typeof WEBHOOK_EVENT_TYPES[number]['value'];
```

---

## UI компоненты для Webhooks

### Шаг 1: Форма создания/редактирования webhook

```typescript
// components/WebhookForm.tsx
import { useState } from 'react';
import { webhooksApi, WebhookCreateRequest, WebhookUpdateRequest } from '../api/webhooks';
import { WEBHOOK_EVENT_TYPES } from '../constants/webhookEvents';

interface WebhookFormProps {
  webhook?: Webhook;
  onSuccess: () => void;
  onCancel: () => void;
}

function WebhookForm({ webhook, onSuccess, onCancel }: WebhookFormProps) {
  const [formData, setFormData] = useState({
    name: webhook?.name || '',
    url: webhook?.url || '',
    event_type: webhook?.event_type || '',
    secret: '',
    description: webhook?.description || '',
    is_active: webhook?.is_active ?? true,
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);

    try {
      if (webhook) {
        await webhooksApi.update(webhook.id, formData);
      } else {
        await webhooksApi.create(formData);
      }
      onSuccess();
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Ошибка при сохранении webhook');
    } finally {
      setLoading(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="webhook-form">
      <div className="form-group">
        <label>Название *</label>
        <input
          type="text"
          value={formData.name}
          onChange={(e) => setFormData({ ...formData, name: e.target.value })}
          required
        />
      </div>

      <div className="form-group">
        <label>URL *</label>
        <input
          type="url"
          value={formData.url}
          onChange={(e) => setFormData({ ...formData, url: e.target.value })}
          required
          placeholder="https://example.com/webhook"
        />
      </div>

      <div className="form-group">
        <label>Тип события *</label>
        <select
          value={formData.event_type}
          onChange={(e) => setFormData({ ...formData, event_type: e.target.value })}
          required
          disabled={!!webhook} // Нельзя менять тип события для существующего webhook
        >
          <option value="">Выберите тип события</option>
          {WEBHOOK_EVENT_TYPES.map((event) => (
            <option key={event.value} value={event.value}>
              {event.label}
            </option>
          ))}
        </select>
        {formData.event_type && (
          <small>
            {WEBHOOK_EVENT_TYPES.find((e) => e.value === formData.event_type)?.description}
          </small>
        )}
      </div>

      <div className="form-group">
        <label>Секрет (опционально)</label>
        <input
          type="password"
          value={formData.secret}
          onChange={(e) => setFormData({ ...formData, secret: e.target.value })}
          placeholder="Для подписи payload"
        />
        <small>Если указан, payload будет подписан с помощью HMAC-SHA256</small>
      </div>

      <div className="form-group">
        <label>Описание</label>
        <textarea
          value={formData.description}
          onChange={(e) => setFormData({ ...formData, description: e.target.value })}
          rows={3}
        />
      </div>

      {webhook && (
        <div className="form-group">
          <label>
            <input
              type="checkbox"
              checked={formData.is_active}
              onChange={(e) => setFormData({ ...formData, is_active: e.target.checked })}
            />
            Активен
          </label>
        </div>
      )}

      {error && <div className="error-message">{error}</div>}

      <div className="form-actions">
        <button type="button" onClick={onCancel} disabled={loading}>
          Отмена
        </button>
        <button type="submit" disabled={loading}>
          {loading ? 'Сохранение...' : webhook ? 'Обновить' : 'Создать'}
        </button>
      </div>
    </form>
  );
}
```

### Шаг 2: Список webhooks

```typescript
// components/WebhooksList.tsx
import { useState, useEffect } from 'react';
import { webhooksApi, Webhook } from '../api/webhooks';
import WebhookForm from './WebhookForm';
import WebhookDeliveries from './WebhookDeliveries';

function WebhooksList() {
  const [webhooks, setWebhooks] = useState<Webhook[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedWebhook, setSelectedWebhook] = useState<Webhook | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [editingWebhook, setEditingWebhook] = useState<Webhook | null>(null);

  useEffect(() => {
    loadWebhooks();
  }, []);

  const loadWebhooks = async () => {
    try {
      setLoading(true);
      const data = await webhooksApi.list();
      setWebhooks(data.items);
    } catch (error) {
      console.error('Failed to load webhooks:', error);
    } finally {
      setLoading(false);
    }
  };

  const handleDelete = async (id: number) => {
    if (!confirm('Удалить этот webhook?')) return;

    try {
      await webhooksApi.delete(id);
      loadWebhooks();
    } catch (error) {
      console.error('Failed to delete webhook:', error);
    }
  };

  const handleToggleActive = async (webhook: Webhook) => {
    try {
      await webhooksApi.update(webhook.id, { is_active: !webhook.is_active });
      loadWebhooks();
    } catch (error) {
      console.error('Failed to update webhook:', error);
    }
  };

  if (loading) return <div>Загрузка...</div>;

  return (
    <div className="webhooks-page">
      <div className="page-header">
        <h1>Webhooks</h1>
        <button onClick={() => setShowForm(true)}>Создать webhook</button>
      </div>

      {showForm && (
        <div className="modal">
          <div className="modal-content">
            <h2>{editingWebhook ? 'Редактировать' : 'Создать'} Webhook</h2>
            <WebhookForm
              webhook={editingWebhook || undefined}
              onSuccess={() => {
                setShowForm(false);
                setEditingWebhook(null);
                loadWebhooks();
              }}
              onCancel={() => {
                setShowForm(false);
                setEditingWebhook(null);
              }}
            />
          </div>
        </div>
      )}

      <table className="webhooks-table">
        <thead>
          <tr>
            <th>Название</th>
            <th>URL</th>
            <th>Тип события</th>
            <th>Статус</th>
            <th>Успешно</th>
            <th>Ошибок</th>
            <th>Последний вызов</th>
            <th>Действия</th>
          </tr>
        </thead>
        <tbody>
          {webhooks.map((webhook) => (
            <tr key={webhook.id}>
              <td>{webhook.name}</td>
              <td>
                <code>{webhook.url}</code>
              </td>
              <td>{webhook.event_type}</td>
              <td>
                <span className={`status ${webhook.is_active ? 'active' : 'inactive'}`}>
                  {webhook.is_active ? 'Активен' : 'Неактивен'}
                </span>
              </td>
              <td>{webhook.success_count}</td>
              <td className={webhook.failure_count > 0 ? 'error' : ''}>
                {webhook.failure_count}
              </td>
              <td>
                {webhook.last_triggered_at
                  ? new Date(webhook.last_triggered_at).toLocaleString()
                  : 'Никогда'}
              </td>
              <td>
                <button onClick={() => handleToggleActive(webhook)}>
                  {webhook.is_active ? 'Деактивировать' : 'Активировать'}
                </button>
                <button onClick={() => {
                  setEditingWebhook(webhook);
                  setShowForm(true);
                }}>
                  Редактировать
                </button>
                <button onClick={() => setSelectedWebhook(webhook)}>
                  История
                </button>
                <button onClick={() => handleDelete(webhook.id)} className="danger">
                  Удалить
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {selectedWebhook && (
        <WebhookDeliveries
          webhookId={selectedWebhook.id}
          onClose={() => setSelectedWebhook(null)}
        />
      )}
    </div>
  );
}
```

### Шаг 3: История доставок webhook

```typescript
// components/WebhookDeliveries.tsx
import { useState, useEffect } from 'react';
import { webhooksApi } from '../api/webhooks';

interface WebhookDeliveriesProps {
  webhookId: number;
  onClose: () => void;
}

function WebhookDeliveries({ webhookId, onClose }: WebhookDeliveriesProps) {
  const [deliveries, setDeliveries] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<string>('');

  useEffect(() => {
    loadDeliveries();
  }, [webhookId, statusFilter]);

  const loadDeliveries = async () => {
    try {
      setLoading(true);
      const data = await webhooksApi.getDeliveries(webhookId, {
        status: statusFilter || undefined,
        limit: 50,
      });
      setDeliveries(data.items);
    } catch (error) {
      console.error('Failed to load deliveries:', error);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="modal">
      <div className="modal-content large">
        <div className="modal-header">
          <h2>История доставок</h2>
          <button onClick={onClose}>×</button>
        </div>

        <div className="filters">
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
            <option value="">Все статусы</option>
            <option value="success">Успешно</option>
            <option value="failed">Ошибка</option>
            <option value="pending">Ожидает</option>
          </select>
        </div>

        {loading ? (
          <div>Загрузка...</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Время</th>
                <th>Событие</th>
                <th>Статус</th>
                <th>HTTP код</th>
                <th>Ошибка</th>
                <th>Попытка</th>
              </tr>
            </thead>
            <tbody>
              {deliveries.map((delivery) => (
                <tr key={delivery.id}>
                  <td>{new Date(delivery.created_at).toLocaleString()}</td>
                  <td>{delivery.event_type}</td>
                  <td>
                    <span className={`status ${delivery.status}`}>
                      {delivery.status}
                    </span>
                  </td>
                  <td>{delivery.response_status || '-'}</td>
                  <td>
                    {delivery.error_message ? (
                      <span className="error" title={delivery.error_message}>
                        {delivery.error_message.substring(0, 50)}...
                      </span>
                    ) : (
                      '-'
                    )}
                  </td>
                  <td>{delivery.attempt_number}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
```

---

## Обработка ошибок

### Обработка ошибок WebSocket

```typescript
// utils/websocket.ts (дополнение)
class WebSocketManager {
  // ... существующий код ...

  private handleError(error: Error): void {
    console.error('WebSocket error:', error);
    
    // Уведомление пользователя
    this.emit('error', {
      message: 'Ошибка подключения к серверу',
      error: error.message,
    });

    // Автоматическое переподключение
    if (this.reconnectAttempts < this.maxReconnectAttempts) {
      this.attemptReconnect(this.wsUrl);
    }
  }

  // Показ статуса подключения
  getConnectionStatus(): 'connected' | 'disconnected' | 'connecting' {
    if (!this.ws) return 'disconnected';
    if (this.ws.readyState === WebSocket.OPEN) return 'connected';
    if (this.ws.readyState === WebSocket.CONNECTING) return 'connecting';
    return 'disconnected';
  }
}
```

### Индикатор статуса подключения

```typescript
// components/ConnectionStatus.tsx
import { useWebSocketContext } from '../contexts/WebSocketContext';

function ConnectionStatus() {
  const { wsManager } = useWebSocketContext();
  const [status, setStatus] = useState<'connected' | 'disconnected' | 'connecting'>('disconnected');

  useEffect(() => {
    if (!wsManager) return;

    const updateStatus = () => {
      setStatus(wsManager.getConnectionStatus());
    };

    wsManager.on('connected', updateStatus);
    wsManager.on('disconnected', updateStatus);

    const interval = setInterval(updateStatus, 1000);

    return () => {
      clearInterval(interval);
      wsManager.off('connected', updateStatus);
      wsManager.off('disconnected', updateStatus);
    };
  }, [wsManager]);

  return (
    <div className={`connection-status ${status}`}>
      <span className="status-dot" />
      {status === 'connected' && 'Подключено'}
      {status === 'disconnected' && 'Отключено'}
      {status === 'connecting' && 'Подключение...'}
    </div>
  );
}
```

---

## Тестирование

### Тестирование WebSocket

1. **Проверка подключения:**
   - Откройте консоль браузера
   - Должно появиться сообщение "WebSocket connected"
   - Проверьте индикатор статуса подключения

2. **Проверка событий:**
   - Создайте нового пользователя через API или бота
   - В консоли должно появиться событие `user.created`
   - Дашборд должен обновиться автоматически

3. **Проверка переподключения:**
   - Остановите сервер
   - WebSocket должен отключиться
   - Запустите сервер снова
   - WebSocket должен автоматически переподключиться

### Тестирование Webhooks

1. **Создание webhook:**
   ```bash
   curl -X POST http://localhost:8080/webhooks \
     -H "Authorization: Bearer YOUR_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{
       "name": "Test Webhook",
       "url": "https://webhook.site/your-unique-url",
       "event_type": "user.created"
     }'
   ```

2. **Проверка доставки:**
   - Создайте нового пользователя
   - Проверьте webhook.site - должен прийти запрос
   - Проверьте историю доставок в админке

3. **Тестирование подписи:**
   - Создайте webhook с secret
   - Проверьте заголовок `X-Webhook-Signature`
   - Валидируйте подпись на стороне получателя

---

## Чеклист интеграции

- [ ] Создан WebSocket менеджер
- [ ] WebSocket подключение инициализировано в приложении
- [ ] Реализована обработка событий в компонентах
- [ ] Добавлены real-time обновления на дашборде
- [ ] Реализованы уведомления о новых событиях
- [ ] Создан API клиент для webhooks
- [ ] Реализована форма создания/редактирования webhooks
- [ ] Реализован список webhooks с фильтрацией
- [ ] Реализована история доставок
- [ ] Добавлена обработка ошибок
- [ ] Добавлен индикатор статуса подключения
- [ ] Протестированы все функции

---

## Дополнительные рекомендации

1. **Оптимизация производительности:**
   - Используйте debounce для частых обновлений
   - Кэшируйте данные, которые не требуют real-time обновлений
   - Ограничьте количество одновременно открытых WebSocket соединений

2. **Безопасность:**
   - Всегда используйте HTTPS для webhook URL
   - Храните секреты webhooks в безопасном месте
   - Валидируйте подпись на стороне получателя
   - Ограничьте доступ к управлению webhooks (только для админов)

3. **Мониторинг:**
   - Логируйте все события WebSocket
   - Отслеживайте успешность доставки webhooks
   - Настройте алерты на большое количество ошибок

4. **UX улучшения:**
   - Показывайте индикатор загрузки при обновлении данных
   - Используйте анимации для плавных обновлений
   - Предоставьте возможность отключить уведомления
   - Добавьте фильтры для событий в уведомлениях

