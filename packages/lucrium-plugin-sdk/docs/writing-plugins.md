# Как писать плагины Lucrium

Плагин — обычный Python-пакет в каталоге с `plugin.toml`. Воркер запускает его
**отдельным процессом**, передаёт события площадок и проксирует все обращения к
площадкам/сети через IPC. Плагин не хранит пароли и не ходит в интернет сам,
если это не разрешено манифестом.

Контракт: `api = "1"`. SDK: [`lucrium-plugin-sdk`](../README.md) (Apache-2.0).

## 1. Минимальный плагин

```
my-plugin/
├── plugin.toml
├── config_schema.json
└── my_plugin.py
```

`plugin.toml`:

```toml
[plugin]
id = "my-plugin"          # ^[a-z0-9][a-z0-9_-]{1,63}$
name = "Мой плагин"
version = "0.1.0"         # semver
api = "1"
entry = "my_plugin"       # файл точки входа внутри каталога плагина

[config]
schema = "config_schema.json"

[permissions]
accounts = ["funpay"]     # пусто = все установленные площадки
network = false           # true → разрешён ctx.http
finance = false           # true → разрешены finance.*/transactions.*/orders.refund/deals.cancel
```

`entry` резолвится как файл **внутри** каталога плагина: `my_plugin.py` или
`my_plugin/__init__.py` (для пакета — с относительными импортами). Имя из
манифеста не может указать на любой установленный модуль: `entry =
"lucrium_worker.config"` — ошибка, а не импорт кода воркера. Каталог плагина
добавляется в **конец** `sys.path`, поэтому соседние файлы плагина импортируются
и в момент запуска, и отложенно внутри хуков, но `json`, `asyncio` или
`lucrium_protocol` из каталога плагина stdlib и протокол не подменяют.

`my_plugin.py`:

```python
from __future__ import annotations

from lucrium_plugin_sdk import PluginContext, PluginEvent


async def on_start(ctx: PluginContext) -> None:
    ctx.log.info("started", plugin=ctx.plugin_id)


async def on_event(event: PluginEvent, ctx: PluginContext) -> None:
    if event.kind == "message":
        ctx.log.info("new message", platform=event.platform.value)


async def on_stop(ctx: PluginContext) -> None:
    ctx.log.info("stopped")
```

## 2. Хуки

| Хук | Когда | Ошибка означает |
|---|---|---|
| `on_start(ctx)` | после `plugin.start` с конфигом | процесс завершится с кодом 1 → авто-restart с backoff |
| `on_event(event, ctx)` | на каждое событие площадки | событие считается необработанным; плагин продолжает работу |
| `on_stop(ctx)` | перед остановкой | логируется; процесс всё равно завершается по таймауту |

Правила валидации (проверяются при старте):

- все хуки — `async def` (или возвращают awaitable);
- `on_event` принимает минимум 2 позиционных аргумента, `on_start`/`on_stop` — минимум 1;
- хотя бы один хук обязателен; больше одного хука одного вида — ошибка.

Вместо имён можно использовать декораторы (удобно, когда хуки разнесены по модулям):

```python
from lucrium_plugin_sdk import on_event


@on_event
async def handle_message(event, ctx) -> None: ...
```

Декоратор имеет приоритет над одноимённой функцией без декоратора.

## 3. Конфиг и JSON-схема

`config_schema.json` — обычная JSON Schema (draft 2020-12). Панель **сама**
рендерит из неё форму: автор плагина не пишет фронтенд.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["reply_text"],
  "properties": {
    "reply_text": {"type": "string", "title": "Текст ответа", "default": "Здравствуйте!"},
    "cooldown_seconds": {"type": "integer", "title": "Пауза, сек", "default": 60, "minimum": 0}
  }
}
```

Доступ из плагина — `ctx.config`:

| Вызов | Поведение |
|---|---|
| `ctx.config.get("cooldown_seconds", 60)` | значение по точечному пути или default |
| `ctx.config["reply_text"]` | `KeyError`, если ключа нет |
| `ctx.config.require("reply_text")` | `KeyError` с понятным сообщением |
| `"reply_text" in ctx.config` | проверка наличия |
| `ctx.config.data` | копия сырого dict (для отладки) |

Точечные пути работают и по вложенным объектам: `ctx.config.get("ai.model")`.

> Воркер валидирует присланный панелью конфиг по схеме до `plugin.start`.
> Значения по умолчанию из схемы панель подставляет сама; плагин всё равно
> должен иметь разумные fallback'и.

## 4. `PluginContext`

`ctx` — единственный выход наружу.

| Член | Что даёт |
|---|---|
| `ctx.plugin_id` | id из манифеста |
| `ctx.session_id` | сессия текущего события (`None` в `on_start`/`on_stop`) |
| `ctx.session.call(method, params)` | вызов клиента aiosellers **этой** сессии |
| `ctx.session_for(session_id)` | клиент другой сессии (в пределах `permissions.accounts`) |
| `ctx.storage` | персистентный KV плагина |
| `ctx.log` | журнал: `debug/info/warning/error(message, **fields)` |
| `ctx.http` | прямой HTTP (только при `network = true`) |
| `ctx.config` | типизированный конфиг |

### `ctx.session`

```python
message = await ctx.session.call(
    "chats.send_message",
    {"chat_id": 42, "text": "Здравствуйте!"},
)
```

- `method` — dotted-путь метода клиента: `chats.send_message`, `orders.list`,
  `lots.fields`, `deals.confirm`, `products.update_price`.
- `params` — именованные аргументы метода (dict).
- Результат — JSON-совместимое значение (сущности aiosellers сериализуются
  в dict).
- Ошибка площадки → исключение `RpcRemoteError` (`.code`, `.message`,
  `.data`). Это подкласс `TransportError`.
- Имена методов различаются по площадкам — см.
  [docs/platforms.md](../../../docs/platforms.md). Пример автоответчика
  показывает, как выбрать метод по `event.platform`.

> **Финансовые операции по умолчанию запрещены.** Воркер отклоняет
> `finance.*`, `transactions.*`, `orders.refund` и `deals.cancel`, пока в
> манифесте не указано `[permissions] finance = true`. Не запрашивайте это
> разрешение, если плагину действительно не нужно выводить деньги, менять
> реквизиты или оформлять возвраты — такие плагины требуют особенно
> внимательного ревью при публикации в магазине.

В событийном хуке `ctx.session` уже привязан к сессии события. Если плагин
обслуживает несколько аккаунтов, применяется `ctx.session_for(...)`.

### `ctx.storage`

```python
await ctx.storage.set("counter", 1)
value = await ctx.storage.get("counter", default=0)
await ctx.storage.delete("counter")
keys = await ctx.storage.keys("cooldown:")
```

Значения — JSON-совместимые. Хранилище переживает рестарты плагина и
удаляется только при `remove_plugin` с `purge_data = true`. Это правильное
место для дедупликации событий, курсоров и счётчиков.

### `ctx.log`

```python
ctx.log.info("auto-reply sent", chat_id=42, platform="funpay")
```

Пишется в панель в реальном времени (событие `plugin_log`) и в файл воркера.
**Не логируйте секреты** — поля попадают в журнал как есть.

### `ctx.http`

```python
response = await ctx.http.request(
    "POST",
    "https://example.com/hook",
    headers={"Content-Type": "application/json"},
    body=b'{"ok": true}',
    timeout=10,
)
if response.ok:
    data = response.json()
```

Доступен только при `[permissions] network = true`; иначе воркер вернёт
`FORBIDDEN`. Таймаут обязателен на стороне воркера — не полагайтесь на
бесконечные запросы.

## 5. События

`PluginEvent` — нормализованное событие площадки:

| Поле | Тип | Смысл |
|---|---|---|
| `platform` | `Platform` (`funpay`/`playerok`/`ggsel`) | площадка |
| `session_id` | `str` | аккаунт/сессия |
| `kind` | `EventKind` | переносимая категория |
| `payload` | `dict` | сериализованный typed event площадки (без `client`) |
| `ts` | `datetime` | время события (UTC) |

`EventKind`: `initial`, `message`, `review`, `order`, `purchase`, `deal`,
`list`, `other`. Ветвитесь по `kind`, а не по именам событий площадок.

Удобные доступы:

```python
text = event.get("message.text")  # точечный путь, None если нет
chat = event.section("chat")  # dict или {}
```

`initial`-события приходят при старте сессии (история чатов/заказов) — не
отвечайте на них как на новые. `list` — изменился список, полезно для
обновления кэша. Формы payload'ов — в
[docs/platforms.md](../../../docs/platforms.md).

## 6. Тестирование без воркера

SDK даёт фейки, реализующие те же view-протоколы, что и RPC:

```python
from lucrium_plugin_sdk.testing import FakeSession, run_lifecycle


async def test_auto_reply(hub_event):
    session = FakeSession().on("chats.send_message", {"id": 1})
    result = await run_lifecycle(
        "examples/auto-reply",  # каталог с plugin.toml
        config={"reply_text": "Привет!"},
        events=[hub_event],
        session=session,
    )
    assert result.event_errors == []
    assert session.last_call("chats.send_message").params["text"] == "Привет!"
```

| Инструмент | Назначение |
|---|---|
| `run_lifecycle(plugin, config=…, events=…, …)` | прогоняет `on_start` → `on_event`* → `on_stop`, собирает ошибки и побочные эффекты |
| `FakeSession` | записывает вызовы, отдаёт зарегистрированные ответы (`.on(method, value)`) |
| `MemoryStorage` / `ListLog` | KV и журнал в памяти |
| `FakeHttp` | ответы по `(method, url)`, запись запросов |
| `make_context(...)` | контекст из фейков для теста одного хука |
| `FakeHost` | эмулятор воркера для тестов самого рантайма (JSON-RPC в памяти) |

`run_lifecycle` не поднимает исключения хуков: `start_error`, `event_errors`,
`stop_error` — поля результата. Так же ведёт себя продакшн-рантайм.

Для pytest достаточно `pytest-asyncio` (`asyncio_mode = "auto"`).

## 7. Ошибки, таймауты, рестарты

- Исключение в `on_start` → процесс завершается с кодом `1`; воркер шлёт
  `plugin_error` и перезапускает плагин с экспоненциальным backoff.
- Исключение в `on_event` → воркер получает JSON-RPC `error`, пишет
  `plugin_log`; плагин продолжает работать. Одно плохое событие не убивает
  плагин.
- `on_stop` ограничен `timeout_seconds` (по умолчанию 10 с).
- Воркер применяет лимит памяти (RLIMIT) и таймауты на IPC-вызовы.
- Крах плагина не влияет на воркер и другие плагины.

## 8. Упаковка и установка

1. Соберите zip так, чтобы `plugin.toml` лежал **в корне** архива.
2. Установите через панель (`install_plugin`) или положите каталог вручную в
   директорию плагинов воркера.
3. Обновление — `update_plugin` с новым zip; воркер валидирует манифест и
   перезапускает плагин (`auto_restart`).

Версию плагина поднимайте по semver: панель показывает её в списке и
использует для обновлений.

## 9. Чек-лист

- [ ] `plugin.toml` валиден: `id`, `version` (semver), `api = "1"`, `entry`.
- [ ] Хотя бы один хук; все хуки `async`.
- [ ] `config_schema.json` описывает все ключи, которые читает плагин.
- [ ] Нет секретов в `ctx.log` и в `ctx.storage` (креды плагину не нужны).
- [ ] `ctx.http` используется только при `network = true`.
- [ ] Обработка событий идемпотентна (дедупликация через `ctx.storage`).
- [ ] Тесты через `run_lifecycle` покрывают основной сценарий и ошибки.
- [ ] Плагин не падает на незнакомых событиях (`kind == "other"`).

## 10. Справочник публичного API

```python
from lucrium_plugin_sdk import (
    PluginContext,
    PluginEvent,  # контракт
    ConfigView,
    HttpResponse,  # view-типы
    discover_entry,
    load_entry,  # обнаружение точки входа
    load_manifest,
    load_config_schema,  # манифест и схема
    on_start,
    on_event,
    on_stop,  # декораторы
    PluginRuntime,
    run_plugin,  # рантайм (обычно не нужен авторам)
    JsonRpcClient,
    TransportError,
    RpcRemoteError,
)
```
