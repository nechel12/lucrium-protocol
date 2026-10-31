# Lucrium Protocol

> **Проект в стадии разработки и тестирования.** Пакеты публичны и уже используются
> воркером и панелью, но до версии 1.0 контракт меняется без сохранения
> обратной совместимости — в том числе прямо сейчас, по результатам аудита.
> Помощь с тестированием и находками очень нужна: [CONTRIBUTING.md](CONTRIBUTING.md).
> Для реальных продаж и боевых плагинов экосистема пока не готова.

[![CI](https://github.com/nechel12/lucrium-protocol/actions/workflows/ci.yml/badge.svg)](https://github.com/nechel12/lucrium-protocol/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12%2B-blue)](pyproject.toml)

Общие модели и контракты экосистемы **Lucrium** — панели управления магазинами
на FunPay / PlayerOK / GGsel.

Этот репозиторий содержит два пакета:

| Пакет | Лицензия | Назначение |
|---|---|---|
| [`lucrium-protocol`](packages/lucrium-protocol) | Apache-2.0 | Pydantic v2-модели протокола «панель ↔ воркер», дескрипторы плагинов, JWT-claims лицензий, JSON-RPC-контракт IPC «воркер ↔ плагин» |
| [`lucrium-plugin-sdk`](packages/lucrium-plugin-sdk) | Apache-2.0 | SDK для авторов плагинов: `PluginContext`, типы событий, декораторы, рантайм, документация и рабочие примеры |

Почему Apache-2.0: авторы плагинов должны иметь возможность писать
**проприетарные платные плагины** без copyleft-обязательств. Ядро (воркер и
панель) — AGPLv3, см. соседние репозитории
[`lucrium-worker`](https://github.com/nechel12/lucrium-worker) и
[`lucrium-panel`](https://github.com/nechel12/lucrium-panel).

## Статус

v1 — контракт для первой версии (локальная установка плагинов zip-архивом).
Форматы для v2 (удалённая доставка подписанных артефактов, JWT-лицензии,
ревок-листы) заложены заранее, чтобы запуск магазина не потребовал переделки
воркера и панели. Публикации на PyPI пока нет — подключение git-ссылкой
(см. ниже).

Что стоит знать про текущие изменения контракта (после аудита 2026-10-31):

- `get_state` отдаёт `StateSnapshot` **без кредов**; полные креды — только по
  команде `reveal_credentials`.
- `install_plugin` / `update_plugin` принимают `ack_permissions`, а
  `PluginState` несёт снапшот разрешений плагина (`accounts`, `network`, `finance`).
- `http.request` ограничен по размеру тела, числу и длине заголовков;
  управляющие символы в URL и заголовках отвергаются. Egress-политика
  (запрет loopback/приватных/link-local) — ответственность воркера, она
  описана в [docs/protocol.md](docs/protocol.md).
- Подпись доставленного артефакта проверяется только против доверенных ключей:
  `verify_delivered_artifact(..., trusted_keys=...)`, ключ из самого дескриптора
  больше не является основанием доверия. Подпись ревок-листа обязательна, в
  проверку лицензии попадает только `VerifiedRevocations`.
- В SDK точка входа резолвится как файл **внутри** каталога плагина, каталог
  добавляется в конец `sys.path`, а не в начало.
- `ctx.log` ограничен по размеру полей.

## Схема репозитория

```
lucrium-protocol/
├── packages/
│   ├── lucrium-protocol/      # модели протокола (envelope, команды, события, state)
│   └── lucrium-plugin-sdk/    # SDK плагинов + примеры + документация
├── examples/                  # рабочие примеры плагинов (auto-reply, webhook-forwarder)
├── docs/                      # документация протокола
└── .github/workflows/         # CI: ruff + mypy + pytest (3.12 / 3.14)
```

## Быстрый старт

```bash
uv sync                       # dev-окружение (uv workspace)
uv run pytest                 # тесты
uv run ruff check .           # линт
uv run ruff format --check .  # формат
uv run mypy packages examples # типы (strict)
```

Установка пакетов в свой проект:

```toml
# pyproject.toml
[project]
dependencies = [
  "lucrium-protocol",                       # воркер / панель
  "lucrium-plugin-sdk",                     # авторы плагинов
]
```

Пока пакеты не опубликованы на PyPI, подключение git-ссылкой:

```toml
[tool.uv.sources]
lucrium-protocol = { git = "https://github.com/nechel12/lucrium-protocol", subdirectory = "packages/lucrium-protocol" }
lucrium-plugin-sdk = { git = "https://github.com/nechel12/lucrium-protocol", subdirectory = "packages/lucrium-plugin-sdk" }
```

Чтобы сборка оставалась воспроизводимой, фиксируйте ссылку на тег:
`{ git = "...", subdirectory = "...", tag = "v0.1.3" }`.

## Документация

- [Протокол панель ↔ воркер](docs/protocol.md) — envelope, команды, события, heartbeat
- [Как писать плагины](packages/lucrium-plugin-sdk/docs/writing-plugins.md) — полный гайд + контракт
- [Платформы и capability-матрица](docs/platforms.md) — что умеет каждая площадка
- [Примеры](examples) — автоответчик и форвардер вебхуков, с тестами

## Безопасность

Модели этого репозитория — граница между доверенным воркером и чужим кодом
плагинов, поэтому находки по контракту (валидация, лимиты, доверие подписям)
важны как и баги. Сообщать о них — приватно, как описано в
[SECURITY.md](SECURITY.md).

## Лицензия

Apache-2.0 — см. [LICENSE](LICENSE).
