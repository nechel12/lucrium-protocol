# Площадки: возможности, подсистемы, события

Источник истины — форк `aiosellers` (пакет `aiosellers-nechel12`). Воркер
создаёт по одному клиенту площадки на аккаунт, а плагины получают доступ к
клиенту **только** через `ctx.session.call(...)` — dotted-путь метода.

## 1. Матрица возможностей

`Capability` из `aiosellers.core.capabilities`; воркер кладёт её в
`AccountState.capabilities`, панель скрывает недоступные действия.

| Возможность | FunPay | PlayerOK | GGsel |
|---|:---:|:---:|:---:|
| `MESSAGES` | ✅ | ✅ | ✅ |
| `ORDERS` | ✅ | — | ✅ (продажи) |
| `PURCHASES` | ✅ | — | ✅ |
| `DEALS` | — | ✅ | — |
| `REVIEWS` | ✅ | ✅ | ✅ |
| `LIST_MANAGEMENT` | ✅ | ✅ | ✅ |
| `IMAGE_UPLOAD` | ✅ | ✅ | — |
| `ITEM_PRIORITY` | — | ✅ | — |
| `WEBSOCKET` | — (polling) | ✅ | — (polling) |

## 2. Подсистемы и ключевые методы

Ниже — публичные методы клиентов, которые можно вызывать через
`ctx.session.call("<подсистема>.<метод>", {…})`. Полные сигнатуры — в
исходниках `aiosellers`.

### FunPay

| Подсистема | Методы |
|---|---|
| `account` | `me`, `profile`, `get_user`, `buyer_viewing` |
| `categories` | `list`, `get`, `subcategory`, `search` |
| `chats` | `list`, `get`, `get_by_name`, `send_message`, `upload_image`, `mute`, `messages.list` |
| `finance` | `balance`, `balance_on_offer`, `transactions`, `iter_transactions`, `exchange_rate`, `withdraw`, `wallets`, `save_wallets` |
| `lots` | `list_public`, `list_self`, `fields`, `chip_fields`, `save`, `delete`, `raise_category`, `calc`, `hide_all`, `upload_offer_image` |
| `orders` | `list`, `iter`, `get`, `refund`, `reply_review`, `send_review`, `delete_review` |

### PlayerOK

| Подсистема | Методы |
|---|---|
| `account` | `me`, `profile`, `get_user`, `reviews` |
| `chats` | `list`, `iter`, `get`, `send_message`, `mark_as_read`, `message_templates`, `upload_image` |
| `deals` | `get`, `list`, `iter`, `confirm`, `complete`, `report_problem`, `cancel`, `create` |
| `games` | `get_category`, `get`, `list`, `iter`, `get_category_options`, `get_obtaining_types`, `get_agreements`, `accept_agreement`, `get_instructions`, `get_data_fields` |
| `items` | `get`, `list`, `iter`, `list_self`, `iter_self`, `create`, `update`, `remove`, `publish`, `set_normal_priority`, `set_premium_priority` |
| `transactions` | `list`, `iter`, `providers`, `sbp_banks`, `cards`, `delete_card`, `withdraw`, `cancel` |

### GGsel

| Подсистема | Методы |
|---|---|
| `account` | `balance`, `receipts` |
| `categories` | `list` |
| `chats` | `list`, `iter`, `get`, `history`, `send` |
| `products` | `list`, `my`, `get`, `update_price`, `update_prices`, `task_status` |
| `reviews` | `list` |
| `sales` | `last`, `get`, `get_by_unique_code` |

### Различия, о которые легко споткнуться

- **Отправка сообщения**: FunPay/PlayerOK — `chats.send_message(chat_id, text=…)`,
  GGsel — `chats.send(chat_id, text)`.
- **Заказы**: у FunPay это `orders` (продажи и покупки, `side`), у PlayerOK —
  `deals`, у GGsel — `sales` (в событиях всё это приходит как `kind = "order"`
  или `"deal"`).
- **Лоты/товары**: FunPay — `lots`, PlayerOK — `items`, GGsel — `products`.
- **Отзывы**: FunPay отвечает через `orders.reply_review(order_id, text)`;
  у PlayerOK и GGsel в текущем форке есть только чтение отзывов.
- **Приоритет**: только PlayerOK (`items.set_normal_priority` /
  `set_premium_priority`).
- **WebSocket**: только PlayerOK; FunPay и GGsel слушают polling'ом, но для
  плагинов это неважно — события приходят одинаково.

## 3. Креды и прокси

| Платформа | Обязательные поля `AccountCredentials` |
|---|---|
| FunPay | `golden_key` |
| PlayerOK | `access_token` |
| GGsel | `api_key` + `seller_id` |

Прокси — `ProxyConfig` (scheme/host/port/username/password) в аккаунте;
воркер собирает URL через `ProxyConfig.to_url()` и кладёт в
`*.ClientConfig.proxy` (строка, поддерживается всеми тремя клиентами,
включая WebSocket PlayerOK).

## 4. События площадок

`aiosellers` нормализует имена событий в переносимые `EventKind`. Воркер
сериализует оригинальный typed event в `HubEvent.payload` (см. §5).

### FunPay

| `kind` | События |
|---|---|
| `initial` | `INITIAL_CHAT`, `INITIAL_ORDER`, `INITIAL_PURCHASE` |
| `list` | `CHATS_LIST_CHANGED`, `ORDERS_LIST_CHANGED`, `PURCHASES_LIST_CHANGED` |
| `message` | `NEW_MESSAGE`, `LAST_CHAT_MESSAGE_CHANGED` |
| `order` | `NEW_ORDER`, `ORDER_STATUS_CHANGED`, `ORDER_CLOSED`, `ORDER_REFUNDED`, `ORDER_PARTIALLY_REFUNDED` |
| `purchase` | `NEW_PURCHASE`, `PURCHASE_STATUS_CHANGED`, `PURCHASE_CLOSED`, `PURCHASE_REFUNDED` |
| `review` | `NEW_REVIEW`, `REVIEW_CHANGED`, `REVIEW_DELETED` |

### PlayerOK

| `kind` | События |
|---|---|
| `initial` | `CHAT_INITIALIZED` |
| `message` | `NEW_MESSAGE` |
| `deal` | `NEW_DEAL`, `ITEM_PAID`, `ITEM_SENT`, `DEAL_CONFIRMED`, `DEAL_CONFIRMED_AUTOMATICALLY`, `DEAL_ROLLED_BACK`, `DEAL_HAS_PROBLEM`, `DEAL_PROBLEM_RESOLVED`, `DEAL_STATUS_CHANGED` |
| `review` | `NEW_REVIEW` |

### GGsel

| `kind` | События |
|---|---|
| `initial` | `INITIAL_CHAT` |
| `message` | `NEW_MESSAGE` |
| `order` | `NEW_SALE` |
| `review` | `NEW_REVIEW` |

Неизвестные события не ломают поток: срабатывает fallback по подстроке
(`INITIAL`, `REVIEW`, `MESSAGE`, `PURCHASE`, `ORDER`, `DEAL`, `CHANGED`),
иначе `other`.

## 5. Форма `HubEvent.payload`

Правила сериализации (реализует воркер, потребляют плагины и панель):

1. `payload` — поля оригинального события-датакласса.
2. Поле `client` / `_client` **не** попадает в провод.
3. Вложенные сущности (`chat`, `message`, `order`, `deal`, `review`, …)
   сериализуются рекурсивно по тем же правилам.
4. `StrEnum`/`Enum` → строковое значение; `datetime` → ISO 8601 UTC;
   `bytes` в событиях не встречаются.

Примеры (сокращённо):

```json
// FunPay: NEW_MESSAGE
{
  "chat": {"id": 42, "name": "buyer", "unread": true},
  "message": {"id": 7, "text": "Здравствуйте!", "chat_id": 42, "by_bot": false}
}
```

```json
// PlayerOK: NEW_MESSAGE
{
  "chat": {"id": "c-1", "type": "PM", "user_id": "u-1"},
  "message": {"id": "m-1", "text": "Есть?", "chat_id": "c-1", "direction": "IN"}
}
```

```json
// GGsel: NEW_MESSAGE
{
  "chat": {"id": 100, "buyer_email": "buyer@example.com", "product_id": 5},
  "message": {"id": 900, "text": "Готово?", "chat_id": 100, "direction": "buyer"}
}
```

Признак «сообщение от нас» различается:

| Платформа | Поле |
|---|---|
| FunPay | `message.by_bot == true` |
| PlayerOK | `message.direction == "OUT"` |
| GGsel | `message.direction == "seller"` |

Отзывы:

```json
// FunPay: NEW_REVIEW — текст и оценка в message/review
{"message": {"text": "Спасибо!", "by_bot": false}, "order_id": "o-1"}
```

```json
// GGsel: NEW_REVIEW
{"review": {"id": 1, "text": "Всё быстро", "product_id": 5, "type": "positive"}}
```

```json
// PlayerOK: NEW_REVIEW — оценка привязана к сделке
{"chat": {"id": "c-1"}, "deal": {"id": "d-1", "status": "CONFIRMED"}}
```

> Точные поля зависят от версии `aiosellers`; плагины должны читать их
> защищённо (`event.get("message.text")`, а не `event.payload["message"]["text"]`).
