# Lucrium Protocol (пакет)

Pydantic v2-модели протокола **«панель ↔ воркер»** экосистемы Lucrium: envelope, команды, события, снимок состояния, манифесты плагинов, дескрипторы доставки (v2) и JWT-лицензии.

- Лицензия: **Apache-2.0** (авторы плагинов могут писать проприетарные плагины).
- Зависимости: `pydantic>=2.10`, `pyjwt[crypto]`.
- Полную документацию протокола см. в [`docs/protocol.md`](../../docs/protocol.md) корня репозитория.

```python
from lucrium_protocol import build_request, Command, GetStateParams, parse_envelope

request = build_request(Command.GET_STATE, GetStateParams())
socket.send(request.model_dump_json())
```

Каталог:

| Модуль | Назначение |
|---|---|
| `envelope` | `{type, id, ts, payload}`: `Request` / `Response` / `Event`, конструкторы и парсеры |
| `commands` | 17 команд панели + реестр `result`-моделей |
| `events` | события воркера: `state`, `status`, `event` (HubEvent), `plugin_log`, `plugin_error`, `command_result` |
| `state` | `StateSnapshot`, `AccountState`, `PluginState`, `WorkerStatus` |
| `types` | `Platform`, `Capability`, `EventKind`, `ProxyConfig`, `AccountCredentials` |
| `manifest` | `plugin.toml` (§6.1) |
| `descriptor` | дескриптор плагина и верификация артефактов (§6.2, v2) |
| `license` | JWT-лицензии и ревок-листы (§6.2, v2) |
| `ipc` | JSON-RPC 2.0 «воркер ↔ плагин» (§6.3) |
| `auth` | pairing-токен (§3.1) |
