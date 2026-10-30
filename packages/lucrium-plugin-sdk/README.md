# Lucrium Plugin SDK

SDK для авторов плагинов **Lucrium** (лицензия Apache-2.0 — можно писать и проприетарные платные плагины).

Плагин получает готовый UI настроек из своей JSON-схемы (панель рендерит форму сама), изолируется в отдельном процессе и общается с воркером по JSON-RPC IPC. Единая точка входа:

```python
async def on_start(ctx: PluginContext) -> None: ...
async def on_event(event: PluginEvent, ctx: PluginContext) -> None: ...
async def on_stop(ctx) -> None: ...
```

`ctx` — единственный выход наружу: `ctx.session` (клиент aiosellers сессии), `ctx.config`, `ctx.storage` (KV), `ctx.log`, `ctx.http` (только при `network = true` в манифесте).

## Что внутри

| Модуль | Назначение |
|---|---|
| `lucrium_plugin_sdk.context` | `PluginContext` + типизированные view (`config`, `storage`, `log`, `http`, `session`) |
| `lucrium_plugin_sdk.entry` | контракт `on_start`/`on_event`/`on_stop`, декораторы, обнаружение и валидация точки входа |
| `lucrium_plugin_sdk.manifest` | чтение `plugin.toml` + JSON-схемы конфига |
| `lucrium_plugin_sdk.runtime` | рантайм: подключение к воркеру по `LUCRIUM_PLUGIN_SOCKET`, диспетчеризация хуков |
| `lucrium_plugin_sdk.transport` | тонкий JSON-RPC 2.0 клиент поверх unix socket |
| `lucrium_plugin_sdk.testing` | фейки (`FakeSession`, `MemoryStorage`, …) — тестировать плагин без воркера |

## Быстрый старт

1. Скопируйте пример: [`examples/auto-reply`](../../../examples/auto-reply) или [`examples/webhook-forwarder`](../../../examples/webhook-forwarder).
2. Напишите логику в модуле из `entry`, настройки — в `config_schema.json`.
3. Запакуйте каталог в zip (`plugin.toml` в корне) и установите через панель.
4. Локально прогоняйте тесты через `lucrium_plugin_sdk.testing` — без VPS и площадок.

Полное руководство: **[Как писать плагины](docs/writing-plugins.md)**.

## Установка

```toml
[project]
dependencies = ["lucrium-plugin-sdk"]
```

Пока пакет не опубликован на PyPI — git-ссылкой (см. корневой README репозитория).

## Статус

Контракт `api = "1"` (v1): локальная установка zip-архивом. Форматы удалённой доставки подписанных артефактов (v2) уже описаны в `lucrium-protocol` (`PluginDescriptor`, JWT-лицензии) — SDK и воркер получат их без слома контракта.
