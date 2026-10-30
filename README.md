# Lucrium Protocol

Общие модели и контракты экосистемы **Lucrium** — панели управления магазинами на FunPay / PlayerOK / GGsel.

Этот репозиторий содержит два пакета:

| Пакет | Лицензия | Назначение |
|---|---|---|
| [`lucrium-protocol`](packages/lucrium-protocol) | Apache-2.0 | Pydantic v2-модели протокола «панель ↔ воркер», дескрипторы плагинов, JWT-claims лицензий, JSON-RPC-контракт IPC «воркер ↔ плагин» |
| [`lucrium-plugin-sdk`](packages/lucrium-plugin-sdk) | Apache-2.0 | SDK для авторов плагинов: `PluginContext`, типы событий, декораторы, рантайм, документация и рабочие примеры |

Почему Apache-2.0: авторы плагинов должны иметь возможность писать **проприетарные платные плагины** без/copyleft-обязательств. Ядро (воркер и панель) — AGPLv3, см. соседние репозитории `lucrium-worker` и `lucrium-panel`.

## Статус

v1 — контракт для первой версии (локальная установка плагинов zip-архивом). Форматы для v2 (удалённая доставка подписанных артефактов, JWT-лицензии, ревок-листы) **заложены заранее**, чтобы запуск магазина не потребовал переделки воркера и панели.

## Схема репозитория

```
lucrium-protocol/
├── packages/
│   ├── lucrium-protocol/      # модели протокола (envelope, команды, события, state)
│   └── lucrium-plugin-sdk/    # SDK плагинов + примеры + документация
├── examples/                  # сквозные примеры (см. packages/lucrium-plugin-sdk)
├── docs/                      # документация протокола
└── .github/workflows/         # CI: ruff + mypy + pytest (3.12 / 3.14)
```

## Быстрый старт

```bash
uv sync                       # dev-окружение (uv workspace)
uv run pytest                 # тесты
uv run ruff check .           # линт
uv run mypy packages          # типы
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
```

## Документация

- [Протокол панель ↔ воркер](docs/protocol.md) — envelope, команды, события, heartbeat
- [Как писать плагины](packages/lucrium-plugin-sdk/docs/writing-plugins.md) — полный гайд + контракт
- [Платформы и capability-матрица](docs/platforms.md) — что умеет каждая площадка

## Лицензия

Apache-2.0 — см. [LICENSE](LICENSE).
