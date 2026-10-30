# Примеры плагинов

Два рабочих плагина, которые одновременно служат документацией: их тесты
(`tests/`) гоняют реальные хуки через фейки SDK — без воркера и площадок.

| Пример | Что показывает |
|---|---|
| [`auto-reply`](auto-reply) | `ctx.session` (отправка сообщения с учётом площадки), `ctx.storage` (пауза на чат), `ctx.config`, `ctx.log` |
| [`webhook-forwarder`](webhook-forwarder) | `ctx.http` (прямой HTTP при `permissions.network = true`), фильтр событий |

Запуск тестов:

```bash
uv run pytest examples/tests -q
```

## Структура плагина

```
my-plugin/
├── plugin.toml          # манифест: id, версия, api, entry, права, схема конфига
├── config_schema.json   # JSON Schema — панель рендерит форму настроек сама
└── my_plugin.py         # on_start / on_event / on_stop
```

Подробный гайд — в [`packages/lucrium-plugin-sdk/docs/writing-plugins.md`](../packages/lucrium-plugin-sdk/docs/writing-plugins.md).
