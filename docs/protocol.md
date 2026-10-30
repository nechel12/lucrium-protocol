# Протокол «панель ↔ воркер»

Контракт первой версии (`protocol = "1"`). Все модели — в пакете
[`lucrium-protocol`](../packages/lucrium-protocol), здесь — человекочитаемое
описание и примеры.

## 1. Транспорт и аутентификация

- **WSS**, воркер слушает порт (по умолчанию `8443`). Панель подключается
  к воркеру: у VPS публичный IP, у панели — нет.
- Аутентификация — **bearer pairing-токен** в HTTP-заголовке рукопожатия:

  ```http
  Authorization: Bearer <pairing-token>
  ```

  Отдельного сообщения `login` нет. Неверный токен → HTTP `401` до открытия
  WebSocket; воркер не раскрывает деталей.
- Токен выдаётся при онбординге (§3.1 спеки) и показывается один раз.
  `rotate_token` отзывает старый и возвращает новый (тоже один раз).
- TLS обязателен: bearer-токен и `get_state` (с кредами) ходят по одному каналу.

## 2. Envelope

Каждое сообщение — JSON-объект:

```json
{ "type": "request", "id": "<uuid>", "ts": "2026-01-15T12:00:00Z", "payload": { } }
```

| Поле | Тип | Назначение |
|---|---|---|
| `type` | `request` \| `response` \| `event` | дискриминатор |
| `id` | string (UUID4 hex) | correlation id; в ответе — тот же |
| `ts` | ISO 8601 UTC | время отправки |
| `payload` | объект | зависит от `type` |

`extra` поля запрещены — неизвестное поле это ошибка парсинга, а не «на будущее».
Версионирование — через `protocol` в `status` и `plugin.api` в манифестах.

### Запрос

`payload` = `{ "method": "<команда>", ...параметры }`.

```json
{
  "type": "request",
  "id": "8f14e45fceea167a5a36dedd4bea2543",
  "ts": "2026-01-15T12:00:00Z",
  "payload": {
    "method": "add_account",
    "account": {
      "platform": "funpay",
      "label": "Основной",
      "credentials": { "platform": "funpay", "golden_key": "gk-..." }
    }
  }
}
```

### Ответ

`payload` содержит **ровно одно** из `result` / `error`:

```json
{ "type": "response", "id": "8f14…", "ts": "…", "payload": { "result": { "account_id": "acc-1" } } }
```

```json
{
  "type": "response",
  "id": "8f14…",
  "ts": "…",
  "payload": { "error": { "code": "invalid_params", "message": "funpay requires golden_key" } }
}
```

### Событие

`payload` = `{ "event": "<имя>", "data": { … } }`:

```json
{
  "type": "event",
  "id": "…",
  "ts": "…",
  "payload": { "event": "event", "data": { "platform": "funpay", "session_id": "acc-1", "kind": "message", "payload": {}, "ts": "…" } }
}
```

## 3. Соединение, heartbeat, переподключение

1. Панель подключается с bearer-токеном.
2. Панель запрашивает `get_state` (или воркер сам шлёт `state` сразу после
   подключения — воркер хранит последний снапшот).
3. Дальше воркер шлёт `state` при изменениях и `status` периодически.
4. **Heartbeat `ping` → `pong`**: панель раз в N секунд отправляет `ping`
   (application-level, потому что браузерный `WebSocket` не умеет ping-фреймы).
   Нет pong за 2–3 интервала → панель переподключается с экспоненциальным
   backoff (1s → 2s → 4s … до 30s, jitter).
5. После переподключения панель сверяет `revision` из `pong`/`state` со своей:
   если пропустила события — `get_state`.

`ping` возвращает `server_time` и `revision` — этого достаточно, чтобы
обнаружить пропущенные `state` без отдельного «hello».

## 4. Команды панели

Полный список — `lucrium_protocol.Command`; params/result-модели —
`COMMAND_PARAMS` / `COMMAND_RESULTS`.

| Команда | Params | Result | Смысл |
|---|---|---|---|
| `get_state` | — | `StateWithCredentials` | полный снапшот **включая креды** (канал аутентифицирован) |
| `add_account` | `AccountInput` | `AccountRef` | добавить аккаунт; сессия стартует по `start_session` |
| `update_account` | `account_id` + патч | `AccountRef` | частичное обновление (label/credentials/proxy/clear_proxy) |
| `remove_account` | `account_id` | `AccountRef` | остановить сессию и удалить аккаунт |
| `start_session` | `account_id` | `SessionRef` | подключить клиента aiosellers |
| `stop_session` | `account_id` | `SessionRef` | штатно отключить |
| `install_plugin` | zip `archive_b64`, `sha256?`, `overwrite`, `auto_start` | `PluginState` | v1: установка zip-архивом |
| `update_plugin` | `plugin_id`, zip, `sha256?`, `auto_restart` | `PluginState` | заменить файлы и перезапустить |
| `remove_plugin` | `plugin_id`, `purge_data` | `PluginRef` | удалить плагин (и его KV) |
| `set_plugin_config` | `plugin_id`, `config` | `PluginConfigResult` | конфиг из JSON-схемы; воркер валидирует и перезапускает |
| `export_state` | `passphrase`, `include_history` | `ExportStateResult` | зашифрованный дамп (`.lcs`) |
| `import_state` | `blob_b64`, `passphrase`, `mode` | `ImportStateResult` | `merge` или `replace` |
| `get_logs` | `scope`, `plugin_id?`, `level?`, `limit`, `cursor?` | `GetLogsResult` | журнал воркера/плагина с пагинацией |
| `rotate_token` | — | `RotateTokenResult` | новый pairing-токен |
| `restart` | `scope` (`worker`/`sessions`), `delay_seconds` | `RestartResult` | отложенный рестарт |
| `ping` | `nonce?` | `PingResult` | heartbeat: эхо nonce, `server_time`, `revision` |

Долгие команды (`install_plugin`, `import_state`) могут отвечать сразу
`{"accepted": …}`-подобным result'ом, а итог присылать событием
`command_result` с `command_id` = `id` исходного запроса. Панель обязана
поддерживать оба пути.

### Креды и маскирование

- `SecretStr` в pydantic маскируется (`"**********"`) даже в JSON — поэтому
  `build_request` и `encode_result` используют `to_wire()`, который раскрывает
  секреты **на проводе**.
- `get_state` — единственный ответ с кредами (`StateWithCredentials`).
- Событие `state` несёт `StateSnapshot` **без кредов**: broadcast не должен
  размножать секреты по всем подключённым панелям.
- Логи и `last_error` не содержат секретов; `ProxyConfig.to_url()` вызывается
  только внутри воркера.

## 5. События воркера

| Событие | Data | Когда |
|---|---|---|
| `state` | `StateSnapshot` | после любого изменения состояния; `revision` монотонно растёт |
| `status` | `WorkerStatus` | heartbeat: uptime, версии, счётчики сессий/плагинов |
| `event` | `HubEvent` | нормализованное событие площадки из aiosellers |
| `plugin_log` | `LogEntry` | строка журнала плагина (реальное время) |
| `plugin_error` | `PluginError` | краш плагина + план рестарта |
| `command_result` | `CommandResult` | асинхронное завершение долгой команды |

`HubEvent`:

```json
{
  "platform": "playerok",
  "session_id": "acc-1",
  "kind": "deal",
  "payload": { "deal": { "id": "d-1", "status": "PAID" } },
  "ts": "2026-01-15T12:00:00Z"
}
```

`payload` — сериализованный оригинальный typed event площадки **без** поля
`client` (клиент aiosellers в провод не попадает). Правила сериализации и
формы payload'ов — в [platforms.md](platforms.md).

`kind` — переносимая категория (`EventKind`): `initial`, `message`, `review`,
`order`, `purchase`, `deal`, `list`, `other`. Плагины ветвятся по `kind`,
а не по именам событий площадок.

## 6. Состояние и ревизии

`StateSnapshot`:

| Поле | Смысл |
|---|---|
| `revision` | монотонный счётчик; панель игнорирует снапшоты с меньшей ревизией |
| `generated_at` | когда собран снапшот |
| `status` | `WorkerStatus` (uptime, версии, счётчики) |
| `accounts` | `AccountState[]` — аккаунт + статус сессии + capabilities + прокси |
| `plugins` | `PluginState[]` — версия, статус процесса, конфиг, `has_config_schema` |

`AccountState.capabilities` — матрица возможностей площадки из aiosellers
(`Capability`), чтобы панель скрывала недоступные действия (см. platforms.md).

## 7. Ошибки

`ErrorCode` (в `Response.payload.error.code`):

| Код | Когда |
|---|---|
| `parse_error` | тело не JSON / не envelope |
| `invalid_request` | нарушен контракт envelope (оба result и error, неизвестный type…) |
| `method_not_found` | неизвестная команда |
| `invalid_params` | params не прошли валидацию |
| `unauthorized` | нет/просрочен токен (до WS — HTTP 401) |
| `forbidden` | действие запрещено правами (например, `ctx.http` без `network = true`) |
| `not_found` | нет аккаунта/плагина/ключа |
| `conflict` | конфликт состояния (повторный старт сессии, id занят) |
| `rate_limited` | слишком часто (heartbeat/логи) |
| `unavailable` | площадка/сессия недоступна, повторите позже |
| `internal` | ошибка воркера (без утечки traceback) |

## 8. IPC «воркер ↔ плагин»

Плагин — **отдельный процесс** (спека §6.3). Общение — JSON-RPC 2.0 поверх
asyncio streams через **unix socket**, фрейминг **NDJSON** (одна JSON-строка
на сообщение, `\n`), лимит строки **16 МиБ**.

Окружение процесса плагина:

| Переменная | Значение |
|---|---|
| `LUCRIUM_PLUGIN_DIR` | каталог плагина с `plugin.toml` |
| `LUCRIUM_PLUGIN_SOCKET` | путь к unix socket воркера |

Запуск: `python -m lucrium_plugin_sdk` (см. `__main__.py`).

### Методы host → плагин

| Метод | Params | Result |
|---|---|---|
| `plugin.start` | `{plugin_id, config}` | `{ready: true}` |
| `plugin.event` | `{event: HubEvent}` | `{handled: true/false}` |
| `plugin.stop` | `{reason?, timeout_seconds}` | `{stopped: true}` |

`plugin.event` может приходить уведомлением (без `id`) — тогда ответ не
отправляется. `plugin.stop` завершает процесс: код `0`.

### Методы плагин → host (только через `ctx`)

| Метод | Params | Result |
|---|---|---|
| `storage.get` | `{key}` | `{value}` (`null` — нет ключа) |
| `storage.set` | `{key, value}` | `{}` |
| `storage.delete` | `{key}` | `{}` |
| `storage.list` | `{prefix}` | `{keys}` |
| `log.write` | `{level, message, fields}` | `{}` (notify, ответ не нужен) |
| `session.call` | `{session_id?, method, params}` | `{data}` — результат метода aiosellers |
| `http.request` | `{method, url, headers?, body_b64?, timeout?}` | `{status, headers, body_b64}` |

- `session.call.method` — dotted-путь (`chats.send_message`, `lots.fields`).
  `session_id = null` допустим только если у плагина ровно одна сессия,
  иначе `INVALID_PARAMS`.
- `http.request` разрешён только при `permissions.network = true`; иначе
  `FORBIDDEN`.
- Ответ на запрос плагина — ровно один из `result`/`error`.

### Коды JSON-RPC

Стандартные: `-32700` parse, `-32600` invalid request, `-32601` method not
found, `-32602` invalid params, `-32603` internal. Прикладные:
`-32001` forbidden, `-32002` timeout, `-32003` rate limited, `-32004` not
found, `-32005` unavailable.

### Жизненный цикл и сбои

1. Воркер запускает процесс, ждёт `plugin.start` → `on_start`.
2. Исключение в `on_start` → процесс завершается с кодом `1` → воркер
   перезапускает с backoff и шлёт `plugin_error`.
3. Исключение в `on_event` **не** роняет плагин: воркер получает `error`,
   пишет `plugin_log` и продолжает.
4. `on_stop` ограничен `timeout_seconds`; зависший хук не блокирует остановку
   навсегда.
5. Воркер применяет таймауты на вызовы, RLIMIT по памяти и авто-restart
   с backoff. Крах одного плагина не влияет на воркер и другие плагины.

## 9. Версионирование

- `WorkerStatus.protocol` = `"1"` — версия этого протокола.
- `plugin.toml [plugin].api` = `"1"` — версия контракта SDK; воркер
  отказывается запускать плагин с неподдерживаемым `api`.
- Новые необязательные поля допустимы только как поля моделей (не «лишние»
  ключи): `extra="forbid"` — осознанная строгость, чтобы опечатки в манифесте
  и payload'ах ловились сразу.
