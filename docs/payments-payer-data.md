# Данные плательщика в платежах (Platega, MulenPay)

С версии 4.12.0 бот в каждом платеже Platega и MulenPay сообщает провайдеру, кто платит. Настраивать ничего
не нужно — поля заполняются сами. Сборка данных — `app/services/payment/payer_identity.py`.

## Зачем

**Platega.** В документации «Создание платежной ссылки с заданным методом» (`POST /transaction/process`) и
«…без заданного метода» (`POST /v2/transaction/process`) поле `metadata` с полями `userId` и `userName` (строки,
оба обязательные внутри `metadata`) требуется для магазинов отдельных категорий:

> Отсутствие **`metadata.userId`** при наличии требования отключает антифрод-защиту и может привести к отключению магазина.

До 4.12.0 бот `metadata` не передавал вовсе.

**MulenPay.** Провайдер попросил всех мерчантов заполнять поле `client` в `POST /v2/payments`: «почтой, телефоном
или ТГ ид и т.п.». В документации поле есть только в примере запроса (со значением-почтой), формат не описан.

## Что уходит

| Кто платит | Platega `metadata.userId` | Platega `metadata.userName` | MulenPay `client` |
|---|---|---|---|
| Пользователь с Telegram | Telegram ID | @username → имя → почта → `id<Telegram ID>` | подтверждённая почта → Telegram ID |
| Пользователь без Telegram (вход по почте) | `user-<id в боте>` | @username → имя → почта → `user-<id>` | подтверждённая почта → `user-<id>` |
| Гость лендинга | `guest-<первые 16 знаков sha256 токена покупки>` | контакт, который он оставил (почта или @ник) | контакт гостя → `guest-…` |

- Поля всегда непустые — даже если база в момент платежа не ответила (тогда уходит только id).
- **Неподтверждённая почта контактом MulenPay не становится**: адрес назначается до подтверждения, а MulenPay
  фискализирует платёж — чек не должен уйти на чужой ящик.
- Сам токен покупки гостя наружу не уходит: по нему забирают подписку.
- Из имён вычищаются обрезанные эмодзи и управляющие символы (иначе строгий разборщик провайдера отбивает весь
  платёж); длина — до 255 символов.

## Где передаётся

- Platega: пополнение баланса и покупки (`create_platega_payment`), автопродление через СБП
  (`create_platega_sbp_subscription`), оплата с лендинга (`create_guest_payment`). В `PlategaService.create_payment` и
  `create_subscription` плательщик — обязательный параметр: забыть его в новом вызове нельзя.
- Platega, СБП-подписка: дополнительно `paymentDetails.intervalCount = 1` — обязательное поле по документации
  «Создать подписку»; каденс и так «одно списание за период».
- MulenPay: все пути `create_mulenpay_payment`; гостевой путь передаёт контакт гостя явно.

## Тесты

`tests/services/test_payer_identity.py`, `tests/services/test_platega_payer_metadata.py`,
`tests/services/test_platega_service.py`, `tests/services/test_platega_subscription_service.py`,
`tests/services/test_payment_service_mulenpay.py`, `tests/services/test_mulenpay_guest_client.py`.
